"""Durable reservations, authenticated readiness, and conservative reconciliation.

A successful kill request is not proof of cleanup. Replacement is admitted only
once the recorded pairs and the crash-window ownership scan are both empty and
no live endpoint remains. The same path handles starting, running, and stopping
records, including publications whose directory fsync failed after rename.
"""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import sys
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import UUID, uuid4

from airplay_linux.backend.contract import BackendFeatures, StatusReply
from airplay_linux.backend.control import (
    BackendControl, BackendOperation, EmptyPayload, Handshake, OwnershipAuthority,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity, SensitiveText
from airplay_linux.domain.ports import (
    Clock, CommandSpec, DaemonCleanupIntentStore, DaemonStore, IdFactory,
    LaunchReceiptStore, ProcessExit, ProcessPort, Scheduler, Subscription,
    SupervisorReleaseIntentStore,
)
from airplay_linux.domain.records import AuthorityOwner, DaemonRecord, LaunchReceipt
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result
from airplay_linux.process.authority import AuthorityLease, SharedRootAuthority
from airplay_linux.process.launcher import LaunchCapability
from airplay_linux.process.procfs import (
    IdentityEvidence, IdentityState, OwnedProcessScan, owned_processes,
    read_identity, read_identity_evidence,
)
from airplay_linux.process.supervisor import (
    TERMINATION_GRACE, config_pipe, module_environment, private_directory,
    socket_info, unlink_stale_socket,
)
from airplay_linux.storage import AtomicFiles, StorageError
from airplay_linux.storage.repositories import (
    JsonDaemonCleanupIntentStore, JsonLaunchReceiptStore, JsonSupervisorReleaseIntentStore,
)


@dataclass(frozen=True, kw_only=True)
class DaemonNotice:
    kind: Literal['ready', 'exit', 'cleanup_required']
    instance_uuid: InstanceId
    diagnostic: Diagnostic | None = None


@dataclass
class _Subscription:
    action: Callable[[], None] | None

    def close(self) -> None:
        action, self.action = self.action, None
        if action is not None:
            action()


class _Clock:
    now = staticmethod(time.monotonic)


class _Ids:
    new = staticmethod(uuid4)


def _error(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _token(value: SensitiveText | str) -> str:
    return value.reveal_for_backend() if isinstance(value, SensitiveText) else value


def _scan_owned(instance_uuid: UUID, runtime: str) -> OwnedProcessScan:
    return owned_processes(instance_uuid, runtime)


class DaemonRegistry:
    """Own one current instance; serialize all launch/reconcile/restart decisions.

    ``record_root`` must name the AtomicFiles root backing ``store`` so the
    executable supervisor writes into that same repository. The control factory
    binds each control to the supplied record's private socket and authentication
    data; it must never select an ambient backend socket.

    Restarts have a rolling budget (three attempts per minute by default).
    Failed spawns consume an admitted attempt; successes do not replenish it.
    Rejected capabilities and failed cleanup do not consume launch attempts.
    """

    def __init__(
        self, *, process: ProcessPort, store: DaemonStore,
        control_factory: Callable[[DaemonRecord], BackendControl],
        features: BackendFeatures, runtime_directory: Path,
        record_root: Path | None = None, app: ProcessIdentity | None = None,
        ids: IdFactory | None = None, clock: Clock | None = None,
        scheduler: Scheduler | None = None, protocol_version: str = 'v1',
        executable: str = 'doubletake', startup_timeout: float = 3.0,
        restart_limit: int = 3, restart_window: float = 60.0,
        inspect_process: Callable[[int], IdentityEvidence] = read_identity_evidence,
        scan_owned: Callable[[UUID, str], OwnedProcessScan] = _scan_owned,
        app_instance_uuid: InstanceId | None = None,
        receipts: LaunchReceiptStore | None = None,
        cleanup_intents: DaemonCleanupIntentStore | None = None,
        supervisor_intents: Callable[[str], SupervisorReleaseIntentStore] | None = None,
        authority: SharedRootAuthority | None = None,
        launcher: LaunchCapability | None = None,
        ownership: OwnershipAuthority | None = None,
    ) -> None:
        self.runtime_directory = Path(runtime_directory)
        self._record_root = Path(record_root) if record_root is not None else self.runtime_directory.parent
        self._app = app if app is not None else read_identity(os.getpid())
        if self._app is None:
            raise ValueError('application identity unavailable')
        if restart_limit < 0 or restart_window <= 0 or startup_timeout <= 0:
            raise ValueError('invalid lifecycle bounds')
        self._process = process
        self._store = store
        self._control_factory = control_factory
        self._features = features
        self._ids = ids if ids is not None else _Ids()
        self._clock = clock if clock is not None else _Clock()
        self._scheduler = scheduler
        self._protocol_version = protocol_version
        self._executable = executable
        self._startup_timeout = startup_timeout
        self._restart_limit = restart_limit
        self._restart_window = restart_window
        self._inspect = inspect_process
        self._scan = scan_owned
        # Inactive runtime composition may carry a public feature snapshot
        # before its lifecycle installation. Missing seams gate admission; they
        # must not prevent the deliberately dormant runtime from being built.
        self._public_seams_complete = all(value is not None for value in (
            app_instance_uuid, receipts, cleanup_intents, supervisor_intents,
            authority, launcher, ownership,
        ))
        self._app_instance_uuid = app_instance_uuid if app_instance_uuid is not None else InstanceId(uuid4())
        files = AtomicFiles(self._record_root)
        self._receipts = receipts if receipts is not None else JsonLaunchReceiptStore(files)
        self._cleanup_intents = cleanup_intents if cleanup_intents is not None else JsonDaemonCleanupIntentStore(files)
        self._supervisor_intents = supervisor_intents if supervisor_intents is not None else (
            lambda runtime: JsonSupervisorReleaseIntentStore(AtomicFiles(Path(runtime)))
        )
        self._authority = authority if authority is not None else SharedRootAuthority(
            files, inspect_process=inspect_process,
        )
        self._launcher = launcher
        self._ownership = ownership
        self._instance_owner: AuthorityOwner | None = None
        self._authority_lease: AuthorityLease | None = None
        self._receipt_history: tuple[LaunchReceipt, ...] = ()
        self._endpoint_generations: dict[tuple[str, str], int] = {}
        self._current_receipts: dict[tuple[str, str], LaunchReceipt] = {}
        self._restarts: deque[float] = deque()
        self._has_launched = False
        self._record: DaemonRecord | None = None
        self._ready = False
        self._exited = False
        self._lock = asyncio.Lock()
        self._callbacks: list[Callable[[DaemonNotice], None]] = []
        self._process_subscription = process.subscribe(self._on_exit)

    def subscribe(self, callback: Callable[[DaemonNotice], None]) -> Subscription:
        self._callbacks.append(callback)

        def remove() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _Subscription(action=remove)

    def _emit(self, notice: DaemonNotice) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback(notice)
            except Exception:
                continue

    def _on_exit(self, event: ProcessExit) -> None:
        r = self._record
        if r is None or event.identity not in (r.supervisor, r.daemon) or self._exited:
            return
        self._exited = True
        self._ready = False
        self._emit(DaemonNotice(
            kind='exit', instance_uuid=r.instance_uuid,
            diagnostic=diagnostic(ErrorCode.daemon_failed) if r.lifecycle != 'stopping' else None,
        ))

    def _cleanup_required(self, failure: Err, record: DaemonRecord | None = None) -> Err:
        self._ready = False
        r = record or self._record
        self._emit(DaemonNotice(
            kind='cleanup_required', instance_uuid=r.instance_uuid if r else InstanceId(UUID(int=0)),
            diagnostic=failure.error,
        ))
        return failure

    def _compatible(self) -> bool:
        if self._features.owned_daemon_control and not self._public_seams_complete:
            return False
        return (
            self._features.profile == 'application-contract-v1'
            and self._features.foreground_control and self._features.owner_handshake
            and self._features.keyring
        )

    def _budget_available(self) -> bool:
        now = self._clock.now()
        while self._restarts and self._restarts[0] <= now - self._restart_window:
            self._restarts.popleft()
        return len(self._restarts) < self._restart_limit

    @asynccontextmanager
    async def _operation(self) -> AsyncIterator[Result[AuthorityLease]]:
        async with self._lock:
            if self._instance_owner is None:
                boot = self._authority.boot_id()
                if boot is None:
                    yield self._cleanup_required(_error(ErrorCode.ownership_mismatch))
                    return
                self._instance_owner = AuthorityOwner(
                    schema='airplay-linux/daemon-authority-owner/v1',
                    instance_uuid=self._app_instance_uuid, app=self._app,
                    boot_id=boot, acquired_at=self._clock.now(),
                )
            acquired = await self._authority.acquire(self._instance_owner)
            if isinstance(acquired, Err):
                yield self._cleanup_required(acquired)
                return
            self._authority_lease = acquired.value
            try:
                yield acquired
            finally:
                self._authority_lease = None
                acquired.value.unlock()

    async def ensure(self) -> Result[DaemonRecord]:
        async with self._operation() as acquired:
            if isinstance(acquired, Err):
                return acquired
            if not self._compatible():
                return _error(ErrorCode.dependency_incompatible)
            obligations = await self._receipt_obligations_locked()
            if isinstance(obligations, Err):
                return self._cleanup_required(obligations)
            if self._record is not None and self._ready and not self._exited:
                records = await self._store.all()
                if isinstance(records, Err):
                    return self._cleanup_required(records)
                durable = next((r for r in records.value if r.instance_uuid == self._record.instance_uuid), None)
                if durable != self._record:
                    return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
                proof = await self._authenticate(self._record)
                if isinstance(proof, Err):
                    return self._cleanup_required(proof)
                if self._exited or not self._ready:
                    return self._cleanup_required(_error(ErrorCode.daemon_failed))
                return Ok(value=self._record)
            if self._has_launched and not self._budget_available():
                return _error(ErrorCode.daemon_failed)
            reconciled = await self._reconcile_locked()
            if isinstance(reconciled, Err):
                return reconciled
            return await self._launch()

    async def reconcile_previous(self) -> Result[None]:
        async with self._operation() as acquired:
            if isinstance(acquired, Err):
                return acquired
            return await self._reconcile_locked()

    async def restart(self) -> Result[DaemonRecord]:
        async with self._operation() as acquired:
            if isinstance(acquired, Err):
                return acquired
            if not self._compatible():
                return _error(ErrorCode.dependency_incompatible)
            if self._has_launched and not self._budget_available():
                return _error(ErrorCode.daemon_failed)
            stopped = await self._reconcile_locked()
            if isinstance(stopped, Err):
                return stopped
            return await self._launch()

    async def shutdown(self) -> Result[None]:
        async with self._operation() as acquired:
            if isinstance(acquired, Err):
                return acquired
            return await self._reconcile_locked()

    async def _pause(self) -> None:
        if self._scheduler is None:
            await asyncio.sleep(0.02)
            return
        future = asyncio.get_running_loop().create_future()

        def wake() -> None:
            if not future.done():
                future.set_result(None)

        handle = self._scheduler.call_at(self._clock.now() + 0.02, wake)
        try:
            await future
        finally:
            handle.cancel()

    async def _launch(self) -> Result[DaemonRecord]:
        record = DaemonRecord(
            schema_version=1, instance_uuid=InstanceId(self._ids.new()),
            owner_token=str(self._ids.new()), app=self._app, supervisor=None, daemon=None,
            protocol_version=self._protocol_version,
            socket_path=str(self.runtime_directory / 'daemon.sock'), lifecycle='starting',
            runtime_directory=str(self.runtime_directory),
        )
        try:
            with private_directory(self.runtime_directory, create=True):
                pass
            if socket_info(record) is not None:
                return self._cleanup_required(_error(ErrorCode.stale_socket), record)
        except OSError:
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch), record)
        published = await self._authority.publish_owner(self._instance_owner)
        if isinstance(published, Err):
            return self._cleanup_required(published, record)
        written = await self._store.write(record)
        if isinstance(written, Err):
            return self._cleanup_required(written, record)
        self._record = record
        self._ready, self._exited = False, False
        try:
            descriptor = config_pipe({
                'owner_token': record.owner_token, 'protocol_version': record.protocol_version,
                'record_root': str(self._record_root), 'executable': self._executable,
                'features': asdict(self._features),
            })
        except (OSError, ValueError):
            return self._cleanup_required(_error(ErrorCode.daemon_failed), record)
        try:
            if self._has_launched:
                self._restarts.append(self._clock.now())
            self._has_launched = True
            launched = await self._process.spawn(CommandSpec(
                argv=(
                    sys.executable, '-m', 'airplay_linux.process.supervisor',
                    '--app-pid', str(self._app.pid), '--app-start', str(self._app.start_ticks),
                    '--instance-uuid', str(record.instance_uuid),
                    '--runtime-directory', record.runtime_directory, '--config-fd', str(descriptor),
                ),
                env=module_environment(record.instance_uuid, record.runtime_directory),
                pass_fds=(descriptor,),
            ))
        finally:
            os.close(descriptor)
        if isinstance(launched, Err):
            return self._cleanup_required(launched, record)
        try:
            async with asyncio.timeout(self._startup_timeout):
                while True:
                    records = await self._store.all()
                    if isinstance(records, Err):
                        return self._cleanup_required(records, record)
                    current = next((r for r in records.value if r.instance_uuid == record.instance_uuid), None)
                    if current is None or replace(
                        current, supervisor=None, daemon=None, lifecycle='starting',
                    ) != record:
                        return self._cleanup_required(_error(ErrorCode.ownership_mismatch), record)
                    self._record = current
                    if current.lifecycle != 'starting':
                        return self._cleanup_required(_error(ErrorCode.daemon_failed), current)
                    if current.supervisor is not None and current.supervisor != launched.value:
                        return self._cleanup_required(_error(ErrorCode.ownership_mismatch), current)
                    if current.supervisor is not None and current.daemon is not None:
                        # The socket may appear just after the identity publication.
                        try:
                            endpoint = socket_info(current)
                        except OSError:
                            return self._cleanup_required(_error(ErrorCode.stale_socket), current)
                        if endpoint is not None:
                            proof = await self._authenticate(current)
                            if isinstance(proof, Err):
                                return self._cleanup_required(proof, current)
                            running = replace(current, lifecycle='running')
                            written = await self._store.write(running)
                            if isinstance(written, Err):
                                return self._cleanup_required(written, current)
                            live = self._live_processes(running)
                            if isinstance(live, Err):
                                return self._cleanup_required(live, running)
                            if self._exited or running.supervisor not in live or running.daemon not in live:
                                return self._cleanup_required(_error(ErrorCode.daemon_failed), running)
                            self._record = running
                            self._ready, self._exited = True, False
                            self._emit(DaemonNotice(kind='ready', instance_uuid=running.instance_uuid))
                            return Ok(value=running)
                    if self._classify(launched.value, record) != 'live':
                        return self._cleanup_required(_error(ErrorCode.daemon_failed), current)
                    await self._pause()
        except TimeoutError:
            return self._cleanup_required(_error(ErrorCode.daemon_failed), self._record)

    def _classify(self, identity: ProcessIdentity, record: DaemonRecord) -> str:
        evidence = self._inspect(identity.pid)
        if evidence.state is IdentityState.absent:
            return 'dead'
        if evidence.identity != identity:
            return 'mismatch'
        if evidence.state is IdentityState.terminated:
            return 'dead'
        if evidence.state is not IdentityState.present or not self._process.owned(
            identity, record.instance_uuid, record.runtime_directory,
        ):
            return 'mismatch'
        return 'live'

    def _live_processes(self, record: DaemonRecord) -> tuple[ProcessIdentity, ...] | Err:
        live: list[ProcessIdentity] = []
        try:
            scan = self._scan(record.instance_uuid, record.runtime_directory)
            if not scan.complete:
                return _error(ErrorCode.ownership_mismatch)
            candidates = tuple(dict.fromkeys((
                *(p for p in (record.supervisor, record.daemon) if p is not None),
                *scan.matches,
            )))
            for identity in candidates:
                state = self._classify(identity, record)
                if state == 'mismatch':
                    return _error(ErrorCode.ownership_mismatch)
                if state == 'live':
                    live.append(identity)
        except OSError:
            return _error(ErrorCode.ownership_mismatch)
        return tuple(live)

    async def _authenticate(
        self, record: DaemonRecord, *, allow_partial: bool = False, release: bool = False,
    ) -> Result[Handshake]:
        live = self._live_processes(record)
        if isinstance(live, Err):
            return live
        if not allow_partial and (record.supervisor not in live or record.daemon not in live):
            return _error(ErrorCode.ownership_mismatch)
        try:
            before = socket_info(record)
            if before is None:
                return _error(ErrorCode.stale_socket)
            control = self._control_factory(record)
            try:
                async with asyncio.timeout(self._startup_timeout):
                    proof = await control.handshake(record.owner_token, record.instance_uuid)
                    if isinstance(proof, Err):
                        return proof
                    h = proof.value
                    if (
                        not isinstance(h, Handshake) or h.instance_uuid != record.instance_uuid
                        or _token(h.owner_token) != record.owner_token
                        or (record.daemon is not None and h.daemon != record.daemon)
                        or h.daemon not in live or h.protocol_version != record.protocol_version
                    ):
                        return _error(ErrorCode.ownership_mismatch)
                    status = await control.request(BackendOperation.status, None, EmptyPayload(), None)
                    if isinstance(status, Err):
                        return status
                    s = status.value
                    if not isinstance(s, StatusReply) or (
                        s.instance_uuid != record.instance_uuid or s.owner_token != record.owner_token
                        or s.daemon != h.daemon or s.protocol_version != record.protocol_version
                    ):
                        return _error(ErrorCode.ownership_mismatch)
                    if s.socket_path != record.socket_path:
                        return _error(ErrorCode.stale_socket)
                    after = socket_info(record)
                    if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                        return _error(ErrorCode.stale_socket)
                    checked = self._live_processes(record)
                    if isinstance(checked, Err):
                        return checked
                    if h.daemon not in checked or (not allow_partial and record.supervisor not in checked):
                        return _error(ErrorCode.ownership_mismatch)
                    if release:
                        # This very control has just authenticated the endpoint;
                        # teardown never switches to an unauthenticated control.
                        result = await control.request(BackendOperation.shutdown, None, EmptyPayload(), None)
                        if isinstance(result, Err):
                            return result
                    return proof
            finally:
                await asyncio.wait_for(control.close(), self._startup_timeout)
        except Exception:
            return _error(ErrorCode.ownership_mismatch)

    async def _receipt_obligations_locked(self) -> Result[None]:
        """Derive history without granting public cleanup to the fixture path.

        Public receipt lifecycle execution is installed separately. Until then,
        an unresolved receipt or intent must not reach legacy token teardown.
        """
        receipts = await self._receipts.all()
        if isinstance(receipts, Err):
            return receipts
        intents = await self._cleanup_intents.all()
        if isinstance(intents, Err):
            return intents
        self._receipt_history = receipts.value
        self._endpoint_generations = {}
        self._current_receipts = {}
        grouped: dict[tuple[str, str], list[LaunchReceipt]] = {}
        for receipt in receipts.value:
            grouped.setdefault((receipt.runtime_directory, receipt.socket_name), []).append(receipt)
        for endpoint, history in grouped.items():
            maximum = max(item.endpoint_generation for item in history)
            self._endpoint_generations[endpoint] = maximum
            current = tuple(item for item in history if item.state not in {'failed', 'stopped'})
            if len(current) > 1 or (current and current[0].endpoint_generation != maximum):
                return _error(ErrorCode.ownership_mismatch)
            if current:
                self._current_receipts[endpoint] = current[0]
        if receipts.value or intents.value:
            # These channels are never inferred absent from an empty legacy
            # observation store. Their public stop/recovery writer owns them.
            return _error(ErrorCode.ownership_mismatch)
        return Ok(value=None)

    async def _reconcile_locked(self) -> Result[None]:
        if self._authority_lease is None or self._authority_lease._unlocked:
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        obligations = await self._receipt_obligations_locked()
        if isinstance(obligations, Err):
            return self._cleanup_required(obligations)
        records = await self._store.all()
        if isinstance(records, Err):
            return self._cleanup_required(records)
        if self._record is not None and not any(
            r.instance_uuid == self._record.instance_uuid for r in records.value
        ):
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        # Inspect every supervisor channel before the first destructive action,
        # including an otherwise unrecorded runtime directory.
        runtimes = {str(self.runtime_directory), *(r.runtime_directory for r in records.value)}
        for runtime in runtimes:
            try:
                intent = await self._supervisor_intents(runtime).read()
            except (OSError, StorageError):
                return self._cleanup_required(_error(ErrorCode.persistence_failed))
            if isinstance(intent, Err):
                return self._cleanup_required(intent)
            if intent.value is not None:
                return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        for record in records.value:
            result = await self._stop(record)
            if isinstance(result, Err):
                return self._cleanup_required(result, record)
        # Missing observations cannot prove that an endpoint has gone away.
        # Never remove an unrecorded socket; retain the owner for recovery.
        for runtime in runtimes:
            try:
                with private_directory(Path(runtime)) as descriptor:
                    try:
                        os.stat('daemon.sock', dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    return self._cleanup_required(_error(ErrorCode.stale_socket))
            except FileNotFoundError:
                continue
            except OSError:
                return self._cleanup_required(_error(ErrorCode.stale_socket))
        self._record = None
        self._ready = False
        if self._authority.owner is not None:
            cleared = await self._authority.clear_owner(self._authority_lease.owner)
            if isinstance(cleared, Err):
                return self._cleanup_required(cleared)
        return Ok(value=None)

    async def _stop(self, record: DaemonRecord) -> Result[None]:
        live = self._live_processes(record)
        if isinstance(live, Err):
            return live
        app = self._inspect(record.app.pid)
        if app.state is IdentityState.unreadable or (
            app.state is not IdentityState.absent and app.identity != record.app
        ):
            return _error(ErrorCode.ownership_mismatch)
        try:
            endpoint = socket_info(record)
        except OSError:
            return _error(ErrorCode.stale_socket)
        authenticated = False
        if live and endpoint is not None:
            proof = await self._authenticate(record, allow_partial=True)
            if isinstance(proof, Err):
                return proof
            # The authenticated identity closes a crash window where the daemon
            # bound its socket before its PID/start pair reached the repository.
            record = replace(record, daemon=proof.value.daemon)
            authenticated = True
        elif live and app.state is IdentityState.present and (
            record.lifecycle == 'running' or record.app != self._app
        ):
            return _error(ErrorCode.stale_socket)
        stopping = replace(record, lifecycle='stopping')
        written = await self._store.write(stopping)
        if isinstance(written, Err):
            return written
        if self._record is not None and self._record.instance_uuid == record.instance_uuid:
            self._record = stopping
        if authenticated:
            released = await self._authenticate(stopping, allow_partial=True, release=True)
            if isinstance(released, Err):
                return released
        for identity in live:
            state = self._classify(identity, record)
            if state == 'mismatch':
                return _error(ErrorCode.ownership_mismatch)
            if state == 'live':
                terminated = await self._process.terminate(
                    identity, record.instance_uuid, record.runtime_directory, deadline=TERMINATION_GRACE,
                )
                if isinstance(terminated, Err):
                    return terminated
        survivors = self._live_processes(record)
        if isinstance(survivors, Err):
            return survivors
        if survivors:
            return _error(ErrorCode.cleanup_timeout)
        try:
            # A live socket after the exact children died may belong to a
            # surviving unrecorded stream or to a replacement endpoint. Neither
            # may be unlinked merely because the recorded daemon is gone.
            if socket_info(record) is not None:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.settimeout(0.05)
                    code = probe.connect_ex(record.socket_path)
                    if code not in {errno.ECONNREFUSED, errno.ENOENT}:
                        return _error(ErrorCode.stale_socket)
            unlink_stale_socket(record)
        except OSError:
            return _error(ErrorCode.stale_socket)
        return await self._store.delete(record.instance_uuid)


__all__ = ['DaemonNotice', 'DaemonRegistry']
