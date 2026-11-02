"""One foreground daemon, tied to its application and to an exact child identity.

The executable entry point and its tiny exec launcher deliberately share this
module: neither subprocess uses a threaded ``preexec_fn``. Authentication crosses
both launches through inherited pipes, never through command arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import signal
import socket
import stat
import sys
import time
from typing import Literal, TYPE_CHECKING
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from airplay_linux.backend.contract import BackendFeatures, StatusReply
from airplay_linux.backend.control import (
    BackendControl, BackendOperation, EmptyPayload, Handshake,
    PublishedDoubletakeControl, PublishedDoubletakeProfile,
)
from airplay_linux.domain.operations import InstanceId, ProcessIdentity, SensitiveText
from airplay_linux.domain.ports import CommandSpec, DaemonStore, ProcessPort
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.records import DaemonRecord, DaemonReservation, LaunchReceipt, SupervisorReleaseIntent
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV, RUNTIME_DIRECTORY_ENV, IdentityState, read_identity,
    read_identity_evidence,
)
from airplay_linux.storage.atomic import AtomicFiles, StorageError
from airplay_linux.storage.repositories import (
    JsonDaemonStore, JsonLaunchReceiptStore, JsonSupervisorReleaseIntentStore,
)

if TYPE_CHECKING:
    from airplay_linux.domain.ports import LaunchReceiptStore, SupervisorReleaseIntentStore
    from airplay_linux.process.ownership import EndpointObservations
    from airplay_linux.process.procfs import IdentityEvidence


_RESERVATION_FIELDS = (
    'schema_version', 'launch_uuid', 'instance_uuid', 'app', 'owner_uid', 'boot_id',
    'runtime_directory', 'runtime_device', 'runtime_inode', 'endpoint_generation',
    'socket_name', 'protocol_version',
)


def _refused(code: ErrorCode = ErrorCode.ownership_mismatch):
    return Err(error=diagnostic(code))

CONFIG_FD_ENV = 'AIRPLAY_LINUX_CONFIG_FD'
CONFIG_LIMIT = 4096
TERMINATION_GRACE = 3.0


def foreground_argv(executable: str, socket_path: str) -> tuple[str, ...]:
    return (executable, '-socket', socket_path, '-cred-backend', 'keyring')


def _parent_death_signal(signum: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signum, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError('parent death signal unavailable')


def _parent_matches(identity: ProcessIdentity) -> bool:
    evidence = read_identity_evidence(identity.pid)
    return (
        os.getppid() == identity.pid
        and evidence.state is IdentityState.present
        and evidence.identity == identity
    )


@contextmanager
def private_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Walk from / with O_NOFOLLOW; keep operations anchored to the final fd."""
    if not path.is_absolute() or any(p in {'.', '..'} for p in path.parts):
        raise OSError('invalid runtime directory')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            final = index == len(path.parts) - 2
            if create and final:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_fd = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_fd
        info = os.fstat(descriptor)
        if path == Path('/') or info.st_uid != os.getuid():
            raise OSError('foreign runtime directory')
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
        yield descriptor
    finally:
        os.close(descriptor)


def socket_info(record: DaemonRecord) -> os.stat_result | None:
    path, runtime = Path(record.socket_path), Path(record.runtime_directory)
    if path.parent != runtime or not path.name or len(os.fsencode(path)) >= 108:
        raise OSError('invalid private socket')
    with private_directory(runtime) as descriptor:
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise OSError('foreign socket')
        return info


def unlink_stale_socket(record: DaemonRecord) -> None:
    """The caller must first prove all exact recorded/scanned processes dead."""
    info = socket_info(record)
    if info is None:
        return
    with private_directory(Path(record.runtime_directory)) as descriptor:
        now = os.stat(Path(record.socket_path).name, dir_fd=descriptor, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (now.st_dev, now.st_ino):
            raise OSError('socket changed')
        os.unlink(Path(record.socket_path).name, dir_fd=descriptor)
        os.fsync(descriptor)


def config_pipe(config: dict) -> int:
    payload = json.dumps(config, separators=(',', ':')).encode()
    if len(payload) > CONFIG_LIMIT:
        raise ValueError('configuration too large')
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
    try:
        os.write(write_fd, payload)  # At most PIPE_BUF, so this write is atomic.
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    return read_fd


def _read_config(descriptor: int) -> dict:
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('configuration must be an inherited private pipe')
        os.set_blocking(descriptor, False)
        payload = os.read(descriptor, CONFIG_LIMIT + 1)
        if len(payload) > CONFIG_LIMIT or os.read(descriptor, 1):
            raise ValueError('configuration too large')
        value = json.loads(payload)
        if type(value) is not dict:
            raise ValueError('invalid configuration')
        return value
    finally:
        os.close(descriptor)


def module_environment(instance_uuid: UUID, runtime: str) -> dict[str, str]:
    return {
        OWNER_TOKEN_ENV: str(instance_uuid), RUNTIME_DIRECTORY_ENV: runtime,
        'PYTHONPATH': str(Path(__file__).resolve().parents[2]),
    }


class DaemonSupervisor:
    """A synchronous executable host for one async, foreground child lifetime.

    The registry writes the initial reservation and alone promotes it to running.
    This process publishes its own/child identities and retains stopping evidence
    for reconciliation. ``control_factory`` is also the deterministic test seam;
    no application-contract fixture is ever selected for an installed backend.
    """

    def __init__(
        self, *, app: ProcessIdentity, instance_uuid: InstanceId,
        runtime_directory: Path, config_fd: int, process: ProcessPort | None = None,
        store: DaemonStore | None = None,
        control_factory: Callable[[DaemonRecord], BackendControl] | None = None,
        reservation: DaemonReservation | None = None,
        receipts: LaunchReceiptStore | None = None,
        supervisor_intents: SupervisorReleaseIntentStore | None = None,
        inspect_process: Callable[[int], IdentityEvidence] = read_identity_evidence,
        own_identity: Callable[[], ProcessIdentity | None] | None = None,
        observations: EndpointObservations | None = None,
        receipt_source: Callable[[], Result[tuple[LaunchReceipt, ...]]] | None = None,
    ) -> None:
        self.app = app
        self.instance_uuid = instance_uuid
        self.runtime_directory = Path(runtime_directory)
        self.config_fd = config_fd
        self._process = process if process is not None else LinuxProcessRunner()
        self._store = store
        self._control_factory = control_factory
        self._reservation = reservation
        self._receipts = receipts
        self._supervisor_intents = supervisor_intents
        self._inspect = inspect_process
        self._own_identity = own_identity or (lambda: read_identity(os.getpid()))
        self._observations = observations
        self._receipt_source = receipt_source
        self._receipt_files: AtomicFiles | None = None
        self.cleanup_required: str | None = None

    def run(self) -> int:
        try:
            return asyncio.run(self._run())
        except (OSError, ValueError, TypeError, KeyError, StorageError):
            # Neither the pipe payload nor backend exception text is printable.
            return 1

    async def _run(self) -> int:
        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stopping.set)
        _parent_death_signal(signal.SIGTERM)
        config = _read_config(self.config_fd)
        features = BackendFeatures(**config['features'])
        if config.get('mode') == 'owned-daemon':
            return await self._run_owned(config, features, stopping)
        if not (features.foreground_control and features.owner_handshake and features.keyring):
            return 1
        if not _parent_matches(self.app):
            return 1
        with private_directory(self.runtime_directory):
            pass
        store = self._store or JsonDaemonStore(AtomicFiles(Path(config['record_root'])))
        records = await store.all()
        if isinstance(records, Err):
            return 1
        record = next((r for r in records.value if r.instance_uuid == self.instance_uuid), None)
        if (
            record is None or record.app != self.app or record.lifecycle != 'starting'
            or record.supervisor is not None or record.daemon is not None
            or record.owner_token != config['owner_token']
            or record.protocol_version != config['protocol_version']
            or record.runtime_directory != str(self.runtime_directory)
        ):
            return 1
        if socket_info(record) is not None:
            return 1
        identity = read_identity(os.getpid())
        if identity is None:
            return 1
        record = replace(record, supervisor=identity)
        if isinstance(await store.write(record), Err):
            return 1
        if stopping.is_set() or not _parent_matches(self.app):
            return 1
        child_fd = config_pipe({
            'owner_token': record.owner_token, 'instance_uuid': str(record.instance_uuid),
            'protocol_version': record.protocol_version, 'socket_path': record.socket_path,
        })
        exits: dict[ProcessIdentity, int] = {}

        def exited(event) -> None:
            exits[event.identity] = event.exit_code

        subscription = self._process.subscribe(exited)
        try:
            try:
                child = await self._process.spawn(CommandSpec(
                    argv=(
                        sys.executable, '-m', 'airplay_linux.process.supervisor',
                        '--launch-child', str(identity.pid), str(identity.start_ticks), '--',
                        *foreground_argv(config['executable'], record.socket_path),
                    ),
                    env={**module_environment(self.instance_uuid, str(self.runtime_directory)),
                         CONFIG_FD_ENV: str(child_fd)},
                    pass_fds=(child_fd,),
                ))
            finally:
                os.close(child_fd)
            if isinstance(child, Err):
                await store.write(replace(record, lifecycle='stopping'))
                return 1
            record = replace(record, daemon=child.value)
            published = await store.write(record)
            if isinstance(published, Ok):
                while (
                    not stopping.is_set() and child.value not in exits
                    and _parent_matches(self.app)
                ):
                    try:
                        await asyncio.wait_for(stopping.wait(), 0.05)
                    except TimeoutError:
                        pass
            # No shutdown request or TERM may precede durable removal intent,
            # including a failed daemon-identity publication after spawn.
            record = replace(record, lifecycle='stopping')
            if isinstance(await store.write(record), Err):
                # Exiting this executable activates the launcher's SIGKILL
                # parent-death protection even when storage cannot publish.
                return 1
            if child.value not in exits:
                give_up = loop.time() + TERMINATION_GRACE
                await self._request_release(record, features)
                result = await self._process.terminate(
                    child.value, self.instance_uuid, record.runtime_directory,
                    deadline=max(0.0, give_up - loop.time()),
                )
                if isinstance(result, Err):
                    return 1
            return 1 if child.value in exits and not stopping.is_set() and _parent_matches(self.app) else 0
        finally:
            subscription.close()

    def _snapshot(self) -> Result[tuple[LaunchReceipt, ...]]:
        if self._receipt_source is not None:
            return self._receipt_source()
        from airplay_linux.storage.codecs import RecordError, decode_launch_receipt
        from airplay_linux.storage.repositories import _lifecycle_all, LAUNCH_RECEIPT_DIRECTORY
        if self._receipt_files is None:
            return _refused()
        try:
            return Ok(value=_lifecycle_all(self._receipt_files, LAUNCH_RECEIPT_DIRECTORY, decode_launch_receipt))
        except (StorageError, RecordError):
            return _refused(ErrorCode.persistence_failed)

    def _intent_snapshot(self) -> Result[SupervisorReleaseIntent | None]:
        from airplay_linux.storage.codecs import RecordError, decode_supervisor_release_intent
        from airplay_linux.storage.repositories import SUPERVISOR_RELEASE_INTENT_NAME
        try:
            payload = AtomicFiles(self.runtime_directory).read(SUPERVISOR_RELEASE_INTENT_NAME)
            return Ok(value=None if payload is None else decode_supervisor_release_intent(payload))
        except (StorageError, RecordError):
            return _refused(ErrorCode.persistence_failed)

    def _public_authority(self, features: BackendFeatures):
        from airplay_linux.process.ownership import EvidenceAuthority
        from airplay_linux.process.procfs import TerminationEvidence
        return EvidenceAuthority(
            receipts=self._snapshot, process=self._process, evidence_factory=TerminationEvidence,
            observations=self._observations, features=features, reservation=self._reservation,
            supervisor_intent=self._intent_snapshot,
        )

    def _app_absence(self) -> str | None:
        evidence = self._inspect(self.app.pid)
        if evidence.state in {IdentityState.absent, IdentityState.terminated}:
            return 'application-exited'
        if evidence.state is IdentityState.present and evidence.identity != self.app:
            return 'parent-mismatch'
        return None

    async def _publish_release_intent(
        self, record: DaemonRecord, reason: Literal['application-exited', 'parent-mismatch'],
    ) -> Result[SupervisorReleaseIntent]:
        reservation = self._reservation
        if (
            reservation is None or self._receipts is None or self._supervisor_intents is None
            or self._store is None or reason != self._app_absence()
            or record.app != self.app or record.instance_uuid != self.instance_uuid
            or record.supervisor is None or record.supervisor != self._own_identity()
            or record.daemon is None or reservation.app != self.app
            or record.runtime_directory != reservation.runtime_directory
            or record.socket_path != str(Path(reservation.runtime_directory) / reservation.socket_name)
            or record.protocol_version != reservation.protocol_version
        ):
            return _refused()
        observations = await self._store.all()
        if isinstance(observations, Err):
            return observations
        matches = [r for r in observations.value if r.app == self.app
                   and r.runtime_directory == record.runtime_directory
                   and r.lifecycle in {'starting', 'running', 'stopping'}]
        if len(matches) != 1 or any(getattr(matches[0], n) != getattr(record, n) for n in (
            'instance_uuid', 'app', 'supervisor', 'daemon', 'runtime_directory', 'socket_path', 'protocol_version',
        )):
            return _refused()
        child = self._inspect(record.daemon.pid)
        parent = self._inspect(record.supervisor.pid)
        if (
            child.state is not IdentityState.present or child.identity != record.daemon
            or child.parent != record.supervisor.pid or child.uid != reservation.owner_uid
            or parent.state is not IdentityState.present or parent.identity != record.supervisor
            or parent.uid != reservation.owner_uid
        ):
            return _refused()
        receipts = await self._receipts.all()
        if isinstance(receipts, Err):
            return receipts
        endpoint = [r for r in receipts.value if r.runtime_directory == record.runtime_directory
                    and r.socket_name == reservation.socket_name]
        selected = [r for r in endpoint if r.launch_uuid == reservation.launch_uuid]
        if endpoint:
            maximum = max(r.endpoint_generation for r in endpoint)
            live = [r for r in endpoint if r.state not in {'failed', 'stopped'}]
            if len(selected) != 1 or len(live) != 1 or live[0] != selected[0]:
                return _refused()
            r = selected[0]
            if (r.endpoint_generation != maximum or r.state not in {'reserved', 'detached', 'bound', 'ready'}
                or any(getattr(r, n) != getattr(reservation, n) for n in _RESERVATION_FIELDS)
                or r.daemon_instance_uuid not in {None, record.instance_uuid}
                or r.supervisor not in {None, record.supervisor} or r.daemon not in {None, record.daemon}):
                return _refused()
        # With no receipt, only the reservation captured from the launch pipe's
        # original durable reservation can supply these fields. Never fabricate it.
        with private_directory(self.runtime_directory) as fd:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != (reservation.runtime_device, reservation.runtime_inode):
                return _refused()
        previous = await self._supervisor_intents.read()
        if isinstance(previous, Err):
            return previous
        intent = SupervisorReleaseIntent(
            schema_version=1, launch_uuid=reservation.launch_uuid,
            instance_uuid=reservation.instance_uuid, app=self.app,
            supervisor=record.supervisor, daemon=record.daemon, reason=reason, intent_at=time.monotonic(),
        )
        if previous.value is not None:
            old = previous.value
            if replace(old, intent_at=intent.intent_at) != intent:
                return _refused()
            intent = old
        written = await self._supervisor_intents.write(intent)
        return written if isinstance(written, Err) else Ok(value=intent)

    def _intent_unavailable(self, result):
        self.cleanup_required = 'intent-unavailable'
        sys.stderr.write('{"kind":"cleanup_required","reason":"intent-unavailable"}\n')
        return result

    async def _release_owned(self, record: DaemonRecord, features: BackendFeatures) -> Result[None]:
        from airplay_linux.process import ownership
        authority = self._public_authority(features)
        snapshot = self._snapshot()
        if isinstance(snapshot, Err):
            return snapshot
        selected = [r for r in snapshot.value if r.launch_uuid == self._reservation.launch_uuid]
        origin = selected[0] if len(selected) == 1 else None
        if origin is None or origin.state in {'reserved', 'detached', 'bound', 'ready'}:
            reason = self._app_absence()
            if reason is None:
                return _refused()
            published = await self._publish_release_intent(record, reason)
            if isinstance(published, Err):
                return self._intent_unavailable(published)
        elif origin.state != 'stopping':
            return _refused()
        # Reuse the verifier's exact receipt joins and anchored stage checks,
        # including its synchronous callback after pidfd open. A bound socket
        # during the observation/receipt gap still requires the full endpoint
        # chain; a missing receipt never supplies invented reservation fields.
        try:
            receipt = authority._select(record, cleanup=True)
            reservation = receipt if receipt is not None else self._reservation
            authority._intent(reservation, receipt, record)

            def stage():
                authority._unchanged(receipt, record)
                authority._intent(reservation, receipt, record)
                if ((receipt is not None and receipt.entry_inode is not None)
                        or socket_info(record) is not None):
                    # No stored bound fields yet: let the verifier independently
                    # capture them, then compare the complete evidence on repeat.
                    bound = receipt if receipt is not None and receipt.entry_inode is not None else None
                    return authority._stage(reservation, bound, record, stage='bound')
                return authority._stage(reservation, receipt, record, stage='pre_bind')

            evidence = stage()

            def revalidate():
                try:
                    fresh = stage()
                    return Ok(value=fresh) if fresh == evidence else _refused()
                except (ownership._Refusal, OSError):
                    return _refused()

            terminated = await self._process.terminate(
                record.daemon, reservation.launch_uuid, reservation.runtime_directory,
                TERMINATION_GRACE, strict=True, evidence=evidence, revalidate=revalidate,
            )
            if isinstance(terminated, Err):
                return terminated
            death = self._inspect(record.daemon.pid)
            if death.state not in {IdentityState.absent, IdentityState.terminated}:
                return _refused(ErrorCode.cleanup_timeout)
            if death.state is IdentityState.terminated and (death.identity != record.daemon or death.uid != reservation.owner_uid):
                return _refused()
            authority._clean(reservation, receipt, record, evidence.expected_entry)
            return Ok(value=None)
        except ownership._Refusal as refusal:
            return _refused(refusal.code)
        except OSError:
            return _refused()

    async def _run_owned(self, config: dict, features: BackendFeatures, stopping: asyncio.Event) -> int:
        if not (features.owned_daemon_control and features.keyring) or not _parent_matches(self.app):
            return 1
        if 'owner_token' in config or type(config.get('endpoint_generation')) is not int:
            return 1
        self._receipt_files = AtomicFiles(Path(config['record_root']))
        self._receipts = self._receipts or JsonLaunchReceiptStore(self._receipt_files)
        self._store = self._store or JsonDaemonStore(self._receipt_files)
        self._supervisor_intents = self._supervisor_intents or JsonSupervisorReleaseIntentStore(AtomicFiles(self.runtime_directory))
        receipts, records = await self._receipts.all(), await self._store.all()
        if isinstance(receipts, Err) or isinstance(records, Err):
            return 1
        launch = UUID(config['launch_uuid'])
        candidates = [r for r in receipts.value if r.launch_uuid == launch]
        if len(candidates) != 1:
            return 1
        r = candidates[0]
        self._reservation = DaemonReservation(**{n: getattr(r, n) for n in _RESERVATION_FIELDS})
        matches = [o for o in records.value if o.app == self.app and o.runtime_directory == str(self.runtime_directory)]
        if (len(matches) != 1 or matches[0].instance_uuid != self.instance_uuid
            or matches[0].lifecycle != 'starting' or matches[0].supervisor is not None or matches[0].daemon is not None
            or r.state != 'reserved' or r.app != self.app or r.endpoint_generation != config['endpoint_generation']
            or r.protocol_version != config['protocol_version'] or r.runtime_directory != str(self.runtime_directory)
            or max(o.endpoint_generation for o in receipts.value if o.runtime_directory == r.runtime_directory) != r.endpoint_generation
            or any(o.launch_uuid != launch and o.runtime_directory == r.runtime_directory and o.state not in {'failed', 'stopped'} for o in receipts.value)):
            return 1
        record = matches[0]
        identity = self._own_identity()
        if identity is None or socket_info(record) is not None:
            return 1
        record = replace(record, supervisor=identity)
        if isinstance(await self._store.write(record), Err) or stopping.is_set() or not _parent_matches(self.app):
            return 1
        from airplay_linux.process.launcher import ProductionLaunchCapability
        capability = ProductionLaunchCapability(features, self.runtime_directory,
            executable=config['executable'], production_graph=True)
        exits = set()
        subscription = self._process.subscribe(lambda event: exits.add(event.identity))
        try:
            child = await self._process.spawn(CommandSpec(
                argv=(sys.executable, '-m', 'airplay_linux.process.supervisor', '--launch-owned-child',
                      str(identity.pid), str(identity.start_ticks), '--', *capability.argv(record.socket_path)),
                env=module_environment(launch, str(self.runtime_directory)),
            ))
            if isinstance(child, Err):
                return 1
            record = replace(record, daemon=child.value)
            published = await self._store.write(record)
            if isinstance(published, Err):
                return 1
            while not stopping.is_set() and child.value not in exits and _parent_matches(self.app):
                try:
                    await asyncio.wait_for(stopping.wait(), 0.05)
                except TimeoutError:
                    pass
            observed = replace(record, lifecycle='stopping')
            if isinstance(await self._store.write(observed), Err):
                return 1
            if child.value in exits:
                return 1
            result = await self._request_release(observed, features)
            return 0 if isinstance(result, Ok) else 1
        finally:
            subscription.close()

    async def _request_release(self, record: DaemonRecord, features: BackendFeatures) -> Result[None]:
        if features.profile in {'doubletake-0.5', 'doubletake-0.5+source'}:
            if self._reservation is None:
                return _refused(ErrorCode.dependency_incompatible)
            return await self._release_owned(record, features)
        await self._request_fixture_release(record, features)
        return Ok(value=None)

    async def _request_fixture_release(self, record: DaemonRecord, features: BackendFeatures) -> None:
        control = None
        try:
            before = socket_info(record)
            if before is None or record.daemon is None or not self._process.owned(
                record.daemon, record.instance_uuid, record.runtime_directory,
            ):
                return
            if self._control_factory is not None:
                control = self._control_factory(record)
            else:
                control = PublishedDoubletakeControl(
                    self._process, PublishedDoubletakeProfile(
                        socket_path=record.socket_path, features=features,
                        owner_token=record.owner_token, instance_uuid=record.instance_uuid,
                        protocol_version=record.protocol_version,
                    ), timeout=0.25,
                )
            async with asyncio.timeout(0.25):
                proof = await control.handshake(record.owner_token, record.instance_uuid)
                if isinstance(proof, Err) or not isinstance(proof.value, Handshake):
                    return
                h = proof.value
                token = h.owner_token.reveal_for_backend() if isinstance(h.owner_token, SensitiveText) else h.owner_token
                if (
                    h.instance_uuid != record.instance_uuid or token != record.owner_token
                    or h.daemon != record.daemon or h.protocol_version != record.protocol_version
                ):
                    return
                status = await control.request(BackendOperation.status, None, EmptyPayload(), None)
                if isinstance(status, Err) or not isinstance(status.value, StatusReply):
                    return
                s = status.value
                if (
                    s.instance_uuid != record.instance_uuid or s.owner_token != record.owner_token
                    or s.daemon != record.daemon or s.protocol_version != record.protocol_version
                    or s.socket_path != record.socket_path
                ):
                    return
                after = socket_info(record)
                if after is None or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                    return
                if not self._process.owned(record.daemon, record.instance_uuid, record.runtime_directory):
                    return
                await control.request(BackendOperation.shutdown, None, EmptyPayload(), None)
        except Exception:
            pass
        finally:
            if control is not None:
                try:
                    await asyncio.wait_for(control.close(), 0.25)
                except Exception:
                    pass


def _launch_child(arguments: list[str], *, owned: bool = False) -> int:
    if len(arguments) < 5 or arguments[2] != '--':
        return 1
    parent = ProcessIdentity(pid=int(arguments[0]), start_ticks=int(arguments[1]))
    # SIGKILL cannot be ignored by a foreground backend. A hard supervisor death
    # therefore cannot strand one even if it ignores graceful SIGTERM.
    _parent_death_signal(signal.SIGKILL)
    if not _parent_matches(parent):
        return 1
    if not owned:
        descriptor = int(os.environ[CONFIG_FD_ENV])
        os.set_inheritable(descriptor, True)
    argv = arguments[3:]
    os.execvpe(argv[0], argv, os.environ)
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments and arguments[0] == '--launch-owned-child':
            return _launch_child(arguments[1:], owned=True)
        if arguments and arguments[0] == '--launch-child':
            return _launch_child(arguments[1:])
        parser = argparse.ArgumentParser(description='Own one private foreground AirPlay daemon')
        parser.add_argument('--app-pid', type=int, required=True)
        parser.add_argument('--app-start', type=int, required=True)
        parser.add_argument('--instance-uuid', type=UUID, required=True)
        parser.add_argument('--runtime-directory', type=Path, required=True)
        parser.add_argument('--config-fd', type=int, required=True)
        options = parser.parse_args(arguments)
        return DaemonSupervisor(
            app=ProcessIdentity(pid=options.app_pid, start_ticks=options.app_start),
            instance_uuid=InstanceId(options.instance_uuid),
            runtime_directory=options.runtime_directory, config_fd=options.config_fd,
        ).run()
    except (OSError, ValueError, KeyError):
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
