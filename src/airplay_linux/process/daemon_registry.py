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
import stat
import sys
import time
from collections import deque
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5

from airplay_linux.backend.contract import BackendFeatures, StatusReply
from airplay_linux.backend.control import (
    BackendControl, BackendOperation, EmptyPayload, Handshake, OwnershipAuthority, OwnershipProof,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity, SensitiveText
from airplay_linux.domain.ports import (
    Clock, CommandSpec, DaemonCleanupIntentStore, DaemonStore, IdFactory,
    LaunchReceiptStore, ProcessExit, ProcessPort, Scheduler, Subscription,
    SupervisorReleaseIntentStore,
)
from airplay_linux.domain.records import (
    AuthorityOwner, DaemonCleanupIntent, DaemonRecord, LaunchReceipt, SupervisorReleaseIntent,
)
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result
from airplay_linux.process.authority import AuthorityLease, SharedRootAuthority
from airplay_linux.process.launcher import LaunchCapability
from airplay_linux.process.ownership import verify_owned_endpoint
from airplay_linux.process.teardown import CleanupOutcomeWriter
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV, RUNTIME_DIRECTORY_ENV, IdentityEvidence, IdentityState, OwnedProcessScan, owned_processes,
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
        self._configured_runtime = self.runtime_directory
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
        if app_instance_uuid is None and features.profile == 'application-contract-v1':
            # Delivered fixture compositions in one application may reopen the
            # same failed launch through another registry. Their shared lease
            # has the same application identity, independent of daemon IDs.
            app_instance_uuid = InstanceId(uuid5(
                NAMESPACE_OID, f'airplay-linux/fixture/{self._app.pid}/{self._app.start_ticks}',
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
        self._launch_correlation: tuple[UUID, int, InstanceId] | None = None
        self._accepted_status: StatusReply | None = None
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
        # Public lifecycle admission remains dormant until composition activates
        # it. Preserve the delivered feature-based contract for fixture profiles.
        if self._features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}:
            return False
        if self._features.owned_daemon_control and not self._public_seams_complete:
            return False
        return (
            self._features.foreground_control and self._features.owner_handshake
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
            reconciled = await self._reconcile()
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
            stopped = await self._reconcile()
            if isinstance(stopped, Err):
                return stopped
            return await self._launch()

    async def shutdown(self) -> Result[None]:
        if (self._features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}
                and not self._public_seams_complete and not self._has_launched and self._record is None):
            # An inactive composition never acquired a daemon lifetime. Runtime
            # shutdown still joins normally; explicit reconciliation retains
            # strict refusal of malformed/unresolved public evidence.
            return Ok(value=None)
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

    def _public_lifecycle(self) -> bool:
        return (
            self._features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}
            and self._public_seams_complete
        )

    def _now(self) -> float:
        return time.time()

    def _new_receipt(self, launch_uuid: UUID, generation: int) -> LaunchReceipt:
        info = os.lstat(self.runtime_directory)
        return LaunchReceipt(
            schema_version=1, launch_uuid=launch_uuid, instance_uuid=self._app_instance_uuid,
            app=self._app, owner_uid=os.getuid(), boot_id=self._authority.boot_id(),
            runtime_directory=str(self.runtime_directory), runtime_device=info.st_dev,
            runtime_inode=info.st_ino, endpoint_generation=generation, socket_name='daemon.sock',
            protocol_version=self._protocol_version, state='reserved',
            daemon_instance_uuid=None, supervisor=None, daemon=None,
            entry_device=None, entry_inode=None, socket_inode=None, peer_pid=None,
            peer_start_ticks=None, peer_uid=None, status_state=None,
            credential_service='unknown', failure=None, updated_at=self._now(),
        )

    async def _receipt_for(self, record: DaemonRecord) -> Result[LaunchReceipt]:
        history = await self._receipts.all()
        if isinstance(history, Err):
            return history
        matches = tuple(r for r in history.value if r.daemon_instance_uuid == record.instance_uuid
                        or (r.state == 'reserved' and r.runtime_directory == record.runtime_directory))
        if len(matches) != 1:
            return _error(ErrorCode.ownership_mismatch)
        receipt = matches[0]
        same_endpoint = tuple(r for r in history.value if r.runtime_directory == receipt.runtime_directory)
        if (receipt.endpoint_generation != max(r.endpoint_generation for r in same_endpoint)
                or sum(r.endpoint_generation == receipt.endpoint_generation for r in same_endpoint) != 1
                or sum(r.state not in {'stopped', 'failed'} for r in same_endpoint) > 1):
            return _error(ErrorCode.stale_event)
        return Ok(value=receipt)

    def _runtime_matches(self, receipt: LaunchReceipt) -> bool:
        try:
            info = os.lstat(receipt.runtime_directory)
            return (
                stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
                and info.st_uid == receipt.owner_uid == os.getuid()
                and (info.st_dev, info.st_ino) == (receipt.runtime_device, receipt.runtime_inode)
                and receipt.boot_id == self._authority.boot_id()
                and receipt.socket_name == 'daemon.sock'
            )
        except (OSError, ValueError):
            return False

    def _exact_process(self, identity: ProcessIdentity | None, uid: int) -> bool:
        if identity is None:
            return False
        observed = self._inspect(identity.pid)
        return (observed.state is IdentityState.present and observed.identity == identity
                and observed.uid == uid)

    async def _join_receipt(self, receipt: LaunchReceipt) -> Result[tuple[DaemonRecord, LaunchReceipt]]:
        """Join the separate supervisor observation; never trust its command-line ID alone."""
        if not self._runtime_matches(receipt):
            return _error(ErrorCode.ownership_mismatch)
        loaded = await self._store.all()
        if isinstance(loaded, Err):
            return loaded
        matches = tuple(r for r in loaded.value if r.runtime_directory == receipt.runtime_directory)
        if len(matches) != 1:
            return _error(ErrorCode.ownership_mismatch)
        record = matches[0]
        if (
            record.app != receipt.app or record.supervisor is None or record.daemon is None
            or record.protocol_version != receipt.protocol_version
            or record.socket_path != str(Path(receipt.runtime_directory) / receipt.socket_name)
            or not self._exact_process(record.supervisor, receipt.owner_uid)
            or not self._exact_process(record.daemon, receipt.owner_uid)
            or self._inspect(record.daemon.pid).parent != record.supervisor.pid
            or (receipt.daemon_instance_uuid is not None and receipt.daemon_instance_uuid != record.instance_uuid)
            or (receipt.supervisor is not None and receipt.supervisor != record.supervisor)
            or (receipt.daemon is not None and receipt.daemon != record.daemon)
            or record.instance_uuid in {receipt.instance_uuid, InstanceId(receipt.launch_uuid)}
        ):
            return _error(ErrorCode.ownership_mismatch)
        if self._launch_correlation is not None and self._launch_correlation != (
            receipt.launch_uuid, receipt.endpoint_generation, record.instance_uuid,
        ):
            return _error(ErrorCode.stale_event)
        joined = replace(
            receipt, state='detached' if receipt.state == 'reserved' else receipt.state,
            daemon_instance_uuid=record.instance_uuid, supervisor=record.supervisor,
            daemon=record.daemon, updated_at=self._now(),
        )
        return Ok(value=(record, joined))

    async def _current_generation(self, receipt: LaunchReceipt) -> Result[int]:
        history = await self._receipts.all()
        if isinstance(history, Err):
            return history
        endpoint = [r for r in history.value if (r.runtime_directory, r.socket_name) ==
                    (receipt.runtime_directory, receipt.socket_name)]
        if not endpoint:
            return _error(ErrorCode.stale_event)
        maximum = max(r.endpoint_generation for r in endpoint)
        live = [r for r in endpoint if r.state not in {'failed', 'stopped'}]
        if (receipt.endpoint_generation != maximum
            or sum(r.endpoint_generation == maximum for r in endpoint) != 1
            or len(live) != 1 or live[0].launch_uuid != receipt.launch_uuid):
            return _error(ErrorCode.stale_event)
        return Ok(value=maximum)

    async def _bind_receipt(self, record: DaemonRecord, receipt: LaunchReceipt) -> Result[LaunchReceipt]:
        generation = await self._current_generation(receipt)
        if isinstance(generation, Err):
            return generation
        verified = verify_owned_endpoint(receipt, current_generation=generation.value)
        if isinstance(verified, Err):
            return verified
        proof = verified.value
        bound = replace(
            receipt, state='bound', entry_device=proof.socket_entry[0], entry_inode=proof.socket_entry[1],
            socket_inode=proof.socket_inode, peer_pid=proof.daemon.pid,
            peer_start_ticks=proof.daemon.start_ticks, peer_uid=proof.peer_uid, updated_at=self._now(),
        )
        written = await self._receipts.write(bound)
        return written if isinstance(written, Err) else Ok(value=bound)

    async def _launch_owned(self) -> Result[DaemonRecord]:
        if not self._public_lifecycle() or self._authority_lease is None:
            return _error(ErrorCode.dependency_incompatible)
        obligations = await self._reconcile_owned_locked()
        if isinstance(obligations, Err):
            return obligations
        history = await self._receipts.all()
        if isinstance(history, Err):
            return history
        launch_uuid = self._ids.new()
        runtime = self._configured_runtime
        try:
            previous = await self._supervisor_intents(str(runtime)).read()
            if isinstance(previous, Err):
                return self._cleanup_required(previous)
            # The old supervisor's one publication is immutable. Give the next
            # instance a separate channel instead of overwriting or reusing it.
            if previous.value is not None:
                runtime = runtime / launch_uuid.hex
            argv = self._launcher.argv(str(runtime / 'daemon.sock'))
        except (ValueError, OSError, StorageError):
            return self._cleanup_required(_error(ErrorCode.dependency_incompatible))
        generation = max((r.endpoint_generation for r in history.value
                          if (r.runtime_directory, r.socket_name) == (str(runtime), 'daemon.sock')), default=0) + 1
        if generation > 2**63 - 1:
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        try:
            if runtime != self._configured_runtime:
                with private_directory(self._configured_runtime) as parent:
                    os.mkdir(runtime.name, 0o700, dir_fd=parent)
                    os.fsync(parent)
            with private_directory(runtime):
                pass
        except OSError:
            return self._cleanup_required(_error(ErrorCode.persistence_failed))
        self.runtime_directory = runtime
        daemon_id = InstanceId(self._ids.new())
        self._launch_correlation = (launch_uuid, generation, daemon_id)
        try:
            receipt = self._new_receipt(launch_uuid, generation)
        except (OSError, ValueError):
            return _error(ErrorCode.persistence_failed)
        reserved = await self._receipts.write(receipt)
        if isinstance(reserved, Err):
            return self._cleanup_required(reserved)
        owner_result = await self._authority.publish_owner(self._instance_owner)
        if isinstance(owner_result, Err):
            return self._cleanup_required(owner_result)
        record = DaemonRecord(
            schema_version=1, instance_uuid=daemon_id, owner_token='unused', app=self._app,
            supervisor=None, daemon=None, protocol_version=receipt.protocol_version,
            socket_path=str(self.runtime_directory / receipt.socket_name), lifecycle='starting',
            runtime_directory=receipt.runtime_directory,
        )
        published = await self._store.write(record)
        if isinstance(published, Err):
            return self._cleanup_required(published, record)
        try:
            config_read = config_pipe({
                'mode': 'owned-daemon', 'launch_uuid': str(launch_uuid),
                'endpoint_generation': generation, 'protocol_version': self._protocol_version,
                'record_root': str(self._record_root), 'executable': argv[0],
                'features': {name: getattr(self._features, name) for name in self._features.__dataclass_fields__},
            })
        except (OSError, ValueError):
            return await self._fail_owned(record, _error(ErrorCode.daemon_failed))
        package_root = str(Path(__file__).resolve().parents[2])
        pythonpath = os.pathsep.join(filter(None, (package_root, os.environ.get('PYTHONPATH'))))
        try:
            if self._has_launched:
                self._restarts.append(self._clock.now())
            self._has_launched = True
            spawned = await self._process.spawn(CommandSpec(
                argv=(sys.executable, '-m', 'airplay_linux.process.supervisor',
                      '--app-pid', str(self._app.pid), '--app-start', str(self._app.start_ticks),
                      '--instance-uuid', str(daemon_id), '--runtime-directory', record.runtime_directory,
                      '--config-fd', str(config_read)),
                env={'PYTHONPATH': pythonpath, OWNER_TOKEN_ENV: str(launch_uuid),
                             RUNTIME_DIRECTORY_ENV: record.runtime_directory}, pass_fds=(config_read,),
            ))
        finally:
            os.close(config_read)
        if isinstance(spawned, Err):
            return await self._fail_owned(record, spawned)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        detached = False
        while asyncio.get_running_loop().time() < deadline:
            joined = await self._join_receipt(receipt)
            if isinstance(joined, Ok):
                record, observed = joined.value
                if record.supervisor != spawned.value:
                    return await self._fail_owned(record, _error(ErrorCode.ownership_mismatch), refuse=True)
                if not detached:
                    saved = await self._receipts.write(observed)
                    if isinstance(saved, Err):
                        return self._cleanup_required(saved, record)
                    receipt, detached = observed, True
                if socket_info(record) is not None:
                    bound = await self._bind_receipt(record, receipt)
                    if isinstance(bound, Err):
                        return await self._fail_owned(record, bound, refuse=True)
                    receipt = bound.value
                    break
            await asyncio.sleep(min(0.02, self._startup_timeout / 4))
        else:
            return await self._fail_owned(record, _error(ErrorCode.daemon_failed))
        while asyncio.get_running_loop().time() < deadline:
            authenticated = await self._authenticate(record)
            if isinstance(authenticated, Ok) and self._accepted_status.state == 'idle':
                ready = replace(receipt, state='ready', status_state='idle',
                                credential_service='keyring_proven', updated_at=self._now())
                written = await self._receipts.write(ready)
                if isinstance(written, Err):
                    return self._cleanup_required(written, record)
                confirmed = await self._ownership.confirm(record, authenticated.value)
                if (isinstance(confirmed, Err) or not self._exact_process(record.daemon, receipt.owner_uid)
                        or not self._exact_process(record.supervisor, receipt.owner_uid)):
                    return await self._fail_owned(record, _error(ErrorCode.daemon_failed), refuse=True)
                record = replace(record, lifecycle='running')
                if isinstance(await self._store.write(record), Err):
                    return await self._fail_owned(record, _error(ErrorCode.persistence_failed))
                # Both publications yield: repeat the complete current proof and
                # both exact child identities before the ready notice.
                confirmed = await self._ownership.confirm(record, authenticated.value)
                if (isinstance(confirmed, Err) or not self._exact_process(record.daemon, receipt.owner_uid)
                        or not self._exact_process(record.supervisor, receipt.owner_uid)):
                    return await self._fail_owned(record, _error(ErrorCode.daemon_failed), refuse=True)
                self._record, self._ready, self._exited = record, True, False
                self._has_launched = True
                self._emit(DaemonNotice(kind='ready', instance_uuid=record.instance_uuid))
                return Ok(value=record)
            await asyncio.sleep(min(0.02, self._startup_timeout / 4))
        return await self._fail_owned(record, _error(ErrorCode.daemon_failed))

    async def _fail_owned(self, record: DaemonRecord, error: Err, *, refuse: bool = False) -> Err:
        cleaned = (_error(ErrorCode.ownership_mismatch) if refuse else
                   await self._stop_owned(record, failure=error.error.code))
        found = await self._receipt_for(record)
        if isinstance(found, Ok) and found.value.state != 'failed':
            failed = replace(found.value, state='failed', failure=error.error.code, updated_at=self._now())
            # stopped is terminal: unsuccessful readiness must have been frozen
            # before cleanup's terminal transition instead.
            if found.value.state != 'stopped':
                written = await self._receipts.write(failed)
                if isinstance(written, Err):
                    return self._cleanup_required(written, record)
        return self._cleanup_required(error if isinstance(cleaned, Ok) else cleaned, record)

    async def _stop_owned(self, record: DaemonRecord, *, failure: ErrorCode | None = None) -> Result[None]:
        found = await self._receipt_for(record)
        if isinstance(found, Err):
            return found
        receipt = found.value
        if receipt.state in {'failed', 'stopped'}:
            return await self._cleanup_terminal(receipt, record)
        joined = await self._join_receipt(receipt)
        if isinstance(joined, Ok):
            record, receipt = joined.value
        elif not self._receipt_absent(receipt):
            return joined
        if receipt.daemon is not None and self._exact_process(receipt.daemon, receipt.owner_uid):
            if receipt.entry_inode is None and socket_info(record) is not None:
                generation = await self._current_generation(receipt)
                if isinstance(generation, Err):
                    return generation
                proof = verify_owned_endpoint(receipt, current_generation=generation.value)
                if isinstance(proof, Err):
                    return proof
                p = proof.value
                receipt = replace(receipt, entry_device=p.socket_entry[0], entry_inode=p.socket_entry[1],
                                  socket_inode=p.socket_inode, peer_pid=p.daemon.pid,
                                  peer_start_ticks=p.daemon.start_ticks, peer_uid=p.peer_uid)
            stopping = replace(receipt, state='stopping', updated_at=self._now())
            persisted = await self._receipts.write(stopping)
            if isinstance(persisted, Err):
                return persisted
            receipt = stopping
            released = await self._authenticate(record, release=True)
            if isinstance(released, Err):
                return released
        else:
            if not self._receipt_absent(receipt):
                return _error(ErrorCode.ownership_mismatch)
            if receipt.state != 'stopping':
                receipt = replace(receipt, state='stopping', updated_at=self._now())
                persisted = await self._receipts.write(receipt)
                if isinstance(persisted, Err):
                    return persisted
            if receipt.daemon is not None:
                released = await self._ownership.release(record)
                if isinstance(released, Err):
                    return released
            elif Path(record.socket_path).exists() or Path(record.socket_path).is_symlink():
                return _error(ErrorCode.stale_socket)
        deadline = asyncio.get_running_loop().time() + self._startup_timeout
        while not self._receipt_absent(receipt):
            if asyncio.get_running_loop().time() >= deadline:
                return _error(ErrorCode.ownership_mismatch)
            await asyncio.sleep(0.01)
        terminal = replace(receipt, state='failed' if failure else 'stopped', failure=failure, updated_at=self._now())
        saved = await self._receipts.write(terminal)
        if isinstance(saved, Err):
            return saved
        removed = await self._store.delete(record.instance_uuid)
        if isinstance(removed, Err):
            return removed
        self._record, self._ready = None, False
        if failure is not None:
            return await self._cleanup_terminal(terminal, record)
        return Ok(value=None)

    def _receipt_absent(self, receipt: LaunchReceipt) -> bool:
        if not self._runtime_matches(receipt):
            return False
        for identity in (receipt.supervisor, receipt.daemon):
            if identity is None:
                continue
            observed = self._inspect(identity.pid)
            if observed.state is IdentityState.absent:
                continue
            if (observed.state is IdentityState.terminated and observed.identity == identity
                    and observed.uid == receipt.owner_uid):
                continue
            return False
        try:
            scan = self._scan(receipt.launch_uuid, receipt.runtime_directory)
            return scan.complete and not scan.matches
        except Exception:
            return False

    async def _publish_cleanup_outcome(self, receipt: LaunchReceipt) -> Result[DaemonCleanupIntent]:
        existing = await self._cleanup_intents.all()
        if isinstance(existing, Err):
            return existing
        matches = [i for i in existing.value if i.launch_uuid == receipt.launch_uuid]
        if len(matches) > 1:
            return _error(ErrorCode.ownership_mismatch)
        intent = matches[0] if matches else DaemonCleanupIntent(
            launch_uuid=receipt.launch_uuid, reason=receipt.failure or ErrorCode.daemon_failed,
            intent_at=self._now(), completed_at=None, outcome=None,
        )
        # Identical publication certifies directory durability after a previous
        # rename/fsync failure; an existing completion never changes.
        saved = (await self._cleanup_intents.write(intent) if matches else
                 await CleanupOutcomeWriter(self._cleanup_intents).record(intent))
        return saved if isinstance(saved, Err) else Ok(value=intent)

    async def _cleanup_terminal(self, receipt: LaunchReceipt, record: DaemonRecord) -> Result[None]:
        """A terminal receipt is history, never signal or replacement authority."""
        published = await self._publish_cleanup_outcome(receipt)
        if isinstance(published, Err):
            return published
        intent = published.value
        if not self._receipt_absent(receipt):
            return _error(ErrorCode.ownership_mismatch)
        if receipt.daemon is not None:
            cleaned = await self._ownership.release(record)
            if isinstance(cleaned, Err):
                return cleaned
        elif Path(record.socket_path).exists() or Path(record.socket_path).is_symlink():
            return _error(ErrorCode.stale_socket)
        if intent.completed_at is None:
            completed = await CleanupOutcomeWriter(self._cleanup_intents).complete(
                intent, outcome='stopped', completed_at=self._now(),
            )
            if isinstance(completed, Err):
                return completed
        return await self._store.delete(record.instance_uuid)

    async def _recover_supervisor_intent(
        self, receipt: LaunchReceipt, record: DaemonRecord, intent: SupervisorReleaseIntent,
    ) -> Result[None]:
        """Observe an already requested release. Recovery never sends a signal."""
        matches = (
            self._runtime_matches(receipt)
            and intent.launch_uuid == receipt.launch_uuid and intent.instance_uuid == receipt.instance_uuid
            and intent.app == receipt.app == record.app
            and intent.supervisor == record.supervisor and intent.daemon == record.daemon
            and record.runtime_directory == receipt.runtime_directory
            and record.socket_path == str(Path(receipt.runtime_directory) / receipt.socket_name)
            and record.protocol_version == receipt.protocol_version
            and record.lifecycle in {'starting', 'running', 'stopping'}
            and record.instance_uuid not in {receipt.instance_uuid, InstanceId(receipt.launch_uuid)}
            and (receipt.supervisor is None or receipt.supervisor == intent.supervisor)
            and (receipt.daemon is None or receipt.daemon == intent.daemon)
            and (receipt.daemon_instance_uuid is None or receipt.daemon_instance_uuid == record.instance_uuid)
        )
        if not matches:
            return _error(ErrorCode.ownership_mismatch)
        published = await self._publish_cleanup_outcome(receipt)
        if isinstance(published, Err):
            return published
        # The outcome document above never authorizes a signal. Freeze failure
        # if this recovery cannot prove completed cleanup before any later retry.
        if receipt.state not in {'failed', 'stopped'}:
            # The durable observation may be ahead of the reserved receipt.
            # Retain its correlated identities before freezing terminal bytes;
            # cleanup can then remove the observation without losing the join.
            receipt = replace(
                receipt, daemon_instance_uuid=record.instance_uuid,
                supervisor=record.supervisor, daemon=record.daemon,
            )
            absent = self._receipt_absent(receipt)
            result = Ok(value=None)
            if absent and receipt.daemon is not None:
                result = await self._ownership.release(record)
            elif absent and (Path(record.socket_path).exists() or Path(record.socket_path).is_symlink()):
                result = _error(ErrorCode.stale_socket)
            stopped = absent and isinstance(result, Ok)
            terminal = replace(
                receipt, state='stopped' if stopped else 'failed',
                failure=None if stopped else ErrorCode.ownership_mismatch, updated_at=self._now(),
            )
            written = await self._receipts.write(terminal)
            if isinstance(written, Err):
                return written
            receipt = terminal
        return await self._cleanup_terminal(receipt, record)

    async def _owned_runtime_channels(
        self, history: tuple[LaunchReceipt, ...], records: tuple[DaemonRecord, ...],
    ) -> Result[dict[str, SupervisorReleaseIntent | None]]:
        """Account for every channel before the first cleanup or publication."""
        for record in records:
            if not any(
                r.runtime_directory == record.runtime_directory and r.app == record.app
                and r.protocol_version == record.protocol_version
                and record.socket_path == str(Path(r.runtime_directory) / r.socket_name)
                for r in history
            ):
                return _error(ErrorCode.ownership_mismatch)
        if self._record is not None and not any(
            r.instance_uuid == self._record.instance_uuid for r in records
        ):
            return _error(ErrorCode.ownership_mismatch)
        runtimes = {str(self._configured_runtime), str(self.runtime_directory),
                    *(r.runtime_directory for r in history), *(r.runtime_directory for r in records)}
        channels = {}
        pending = list(sorted(runtimes, reverse=True))
        while pending:
            runtime = pending.pop()
            receipts = tuple(r for r in history if r.runtime_directory == runtime)
            if any(not self._runtime_matches(r) for r in receipts):
                return _error(ErrorCode.ownership_mismatch)
            if receipts:
                maximum = max(r.endpoint_generation for r in receipts)
                if sum(r.endpoint_generation == maximum for r in receipts) != 1:
                    return _error(ErrorCode.ownership_mismatch)
            try:
                try:
                    info = os.lstat(runtime)
                except FileNotFoundError:
                    channels[runtime] = None
                    continue
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o700):
                    return _error(ErrorCode.ownership_mismatch)
                with private_directory(Path(runtime)) as descriptor:
                    # A missing receipt cannot hide a retained child runtime.
                    # Discover directories without following symlinks, before
                    # any recovery is allowed to modify a known endpoint.
                    with os.scandir(descriptor) as entries:
                        for entry in entries:
                            if entry.name in {'daemon.sock', 'release-intent.json'}:
                                continue
                            if entry.is_symlink():
                                return _error(ErrorCode.ownership_mismatch)
                            if entry.is_dir(follow_symlinks=False):
                                child = str(Path(runtime) / entry.name)
                                if child not in runtimes:
                                    runtimes.add(child)
                                    pending.append(child)
                    try:
                        os.stat('daemon.sock', dir_fd=descriptor, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        if not receipts:
                            return _error(ErrorCode.stale_socket)
                loaded = await self._supervisor_intents(runtime).read()
            except (OSError, StorageError):
                return _error(ErrorCode.persistence_failed)
            if isinstance(loaded, Err):
                return loaded
            intent = loaded.value
            if intent is not None:
                correlated = tuple(r for r in receipts if r.launch_uuid == intent.launch_uuid)
                if len(correlated) != 1:
                    return _error(ErrorCode.ownership_mismatch)
                receipt = correlated[0]
                if (receipt.instance_uuid != intent.instance_uuid or receipt.app != intent.app
                        or receipt.supervisor not in {None, intent.supervisor}
                        or receipt.daemon not in {None, intent.daemon}):
                    return _error(ErrorCode.ownership_mismatch)
            channels[runtime] = intent
        return Ok(value=channels)

    async def _reconcile_owned_locked(self) -> Result[None]:
        checked = await self._receipt_obligations_locked()
        if isinstance(checked, Err):
            return self._cleanup_required(checked)
        records = await self._store.all()
        if isinstance(records, Err):
            return self._cleanup_required(records)
        history = self._receipt_history
        channels = await self._owned_runtime_channels(history, records.value)
        if isinstance(channels, Err):
            return self._cleanup_required(channels)
        for receipt in history:
            endpoint = tuple(r for r in history if r.runtime_directory == receipt.runtime_directory)
            newest = max(r.endpoint_generation for r in endpoint)
            if (sum(r.endpoint_generation == newest for r in endpoint) != 1
                    or sum(r.state not in {'failed', 'stopped'} for r in endpoint) > 1):
                return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
            matches = tuple(r for r in records.value if r.runtime_directory == receipt.runtime_directory)
            if receipt.endpoint_generation < newest:
                if receipt.state not in {'failed', 'stopped'} or not self._receipt_absent(receipt):
                    return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
                continue
            if len(matches) > 1:
                return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
            record = matches[0] if matches else DaemonRecord(
                schema_version=1, instance_uuid=receipt.daemon_instance_uuid or InstanceId(receipt.launch_uuid),
                owner_token='unused', app=receipt.app, supervisor=receipt.supervisor, daemon=receipt.daemon,
                protocol_version=receipt.protocol_version,
                socket_path=str(Path(receipt.runtime_directory) / receipt.socket_name), lifecycle='stopping',
                runtime_directory=receipt.runtime_directory,
            )
            supervisor_intent = channels.value[receipt.runtime_directory]
            if supervisor_intent is not None and supervisor_intent.launch_uuid == receipt.launch_uuid:
                result = await self._recover_supervisor_intent(receipt, record, supervisor_intent)
            elif receipt.state in {'failed', 'stopped'}:
                result = await self._cleanup_terminal(receipt, record)
            else:
                result = await self._stop_owned(record)
            if isinstance(result, Err):
                return self._cleanup_required(result, record)
        # An observation lacking a durable reservation remains unresolved.
        if any(not any(r.runtime_directory == record.runtime_directory for r in history)
               for record in records.value):
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        if history:
            owner = self._authority.owner
            cleared = await self._authority.clear_owner(owner) if owner is not None else Ok(value=None)
            if isinstance(cleared, Err):
                return self._cleanup_required(cleared)
        self._record, self._ready = None, False
        return Ok(value=None)

    async def _launch(self) -> Result[DaemonRecord]:
        if self._features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}:
            return await self._launch_owned()
        return await self._launch_fixture()

    async def _launch_fixture(self) -> Result[DaemonRecord]:
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
    ) -> Result[OwnershipProof | None]:
        if self._features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}:
            if self._ownership is None:
                return _error(ErrorCode.dependency_incompatible)
            if release:
                return await self._ownership.release(record)
            proof = await self._ownership.prove(record)
            if isinstance(proof, Err):
                return proof
            control = self._control_factory(record)
            try:
                async with asyncio.timeout(self._startup_timeout):
                    status = await control.request(BackendOperation.status, None, EmptyPayload(), None)
                    checked = await self._ownership.confirm(record, proof.value)
                    if isinstance(checked, Err):
                        return checked
                    if isinstance(status, Err):
                        return status
                    if not isinstance(status.value, StatusReply):
                        return _error(ErrorCode.dependency_incompatible)
                    self._accepted_status = status.value
                    return proof
            except (OSError, TimeoutError, ValueError):
                return _error(ErrorCode.daemon_failed)
            finally:
                await asyncio.wait_for(control.close(), self._startup_timeout)
        proof = await self._authenticate_fixture(record, allow_partial=allow_partial, release=release)
        if isinstance(proof, Err):
            return proof
        if release:
            return Ok(value=None)
        info = socket_info(record)
        if info is None:
            return _error(ErrorCode.stale_socket)
        return Ok(value=OwnershipProof(
            kind='handshake', daemon=proof.value.daemon,
            socket_entry=(info.st_dev, info.st_ino), generation=1, handshake=proof.value,
        ))

    async def _authenticate_fixture(
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
        if self._public_lifecycle() and any(
            not any(r.launch_uuid == intent.launch_uuid for r in receipts.value) for intent in intents.value
        ):
            return _error(ErrorCode.ownership_mismatch)
        if (receipts.value or intents.value) and not self._public_lifecycle():
            # These channels are never inferred absent from an empty legacy
            # observation store. Their public stop/recovery writer owns them.
            return _error(ErrorCode.ownership_mismatch)
        return Ok(value=None)

    async def _reconcile(self) -> Result[None]:
        """Retain the delivered fixture seam inside the operation's shared lease."""
        return await self._reconcile_locked()

    async def _reconcile_locked(self) -> Result[None]:
        if self._authority_lease is None or self._authority_lease._unlocked:
            return self._cleanup_required(_error(ErrorCode.ownership_mismatch))
        if self._public_lifecycle():
            return await self._reconcile_owned_locked()
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
        if self._public_lifecycle():
            result = await self._stop_owned(record)
            return self._cleanup_required(result, record) if isinstance(result, Err) else result
        return await self._stop_fixture(record)

    async def _stop_fixture(self, record: DaemonRecord) -> Result[None]:
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
