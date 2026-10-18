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
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import Err, Ok
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV, RUNTIME_DIRECTORY_ENV, IdentityState, read_identity,
    read_identity_evidence,
)
from airplay_linux.storage.atomic import AtomicFiles, StorageError
from airplay_linux.storage.repositories import JsonDaemonStore

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
    ) -> None:
        self.app = app
        self.instance_uuid = instance_uuid
        self.runtime_directory = Path(runtime_directory)
        self.config_fd = config_fd
        self._process = process if process is not None else LinuxProcessRunner()
        self._store = store
        self._control_factory = control_factory

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

    async def _request_release(self, record: DaemonRecord, features: BackendFeatures) -> None:
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


def _launch_child(arguments: list[str]) -> int:
    if len(arguments) < 5 or arguments[2] != '--':
        return 1
    parent = ProcessIdentity(pid=int(arguments[0]), start_ticks=int(arguments[1]))
    # SIGKILL cannot be ignored by a foreground backend. A hard supervisor death
    # therefore cannot strand one even if it ignores graceful SIGTERM.
    _parent_death_signal(signal.SIGKILL)
    if not _parent_matches(parent):
        return 1
    descriptor = int(os.environ[CONFIG_FD_ENV])
    os.set_inheritable(descriptor, True)
    argv = arguments[3:]
    os.execvpe(argv[0], argv, os.environ)
    return 1


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
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
