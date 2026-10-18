"""Real foreground children in disposable private runtimes, with bounded waits."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.backend.control import (
    AckReply, ApplicationContractProfile, BackendOperation, Handshake, StatusReply,
)
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import Ok
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV, RUNTIME_DIRECTORY_ENV, IdentityState,
    read_identity, read_identity_evidence,
)
from airplay_linux.process.supervisor import DaemonSupervisor, foreground_argv
from airplay_linux.storage.atomic import AtomicFiles
from airplay_linux.storage.repositories import JsonDaemonStore
from airplay_linux.testing.adapters import FakeProcess

INSTANCE = InstanceId(UUID('11111111-2222-3333-4444-555555555555'))
FEATURES = ApplicationContractProfile(socket_path='/unused').features
SRC = str(Path(__file__).resolve().parents[2] / 'src')


def test_foreground_command_never_daemonizes():
    argv = foreground_argv('/usr/bin/doubletake', '/run/test/private.sock')
    assert '-daemonize' not in argv
    assert '-socket' in argv
    assert argv[argv.index('-socket') + 1] == '/run/test/private.sock'
    assert argv[argv.index('-cred-backend') + 1] == 'keyring'


def wait_until(predicate, timeout=8.0):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError('bounded child observation timed out')


def gone(identity):
    evidence = read_identity_evidence(identity.pid)
    return evidence.state in {IdentityState.absent, IdentityState.terminated}


def fake_daemon(tmp_path, *, ignore_term=False, exit_immediately=False):
    executable = tmp_path / 'fake-daemon'
    executable.write_text(f'''#!{sys.executable}
import json, os, signal, socket, sys, time
config_fd = int(os.environ['AIRPLAY_LINUX_CONFIG_FD'])
with os.fdopen(config_fd) as source:
    config = json.load(source)
path = sys.argv[sys.argv.index('-socket') + 1]
endpoint = socket.socket(socket.AF_UNIX)
endpoint.bind(path)
def terminate(signum, frame):
    open(os.path.join(os.path.dirname(path), 'term'), 'w').write('received')
    {'return' if ignore_term else 'raise SystemExit(0)'}
signal.signal(signal.SIGTERM, terminate)
with open(os.path.join(os.path.dirname(path), 'ready.json'), 'w') as target:
    json.dump({{'pid': os.getpid(), 'config': config, 'argv': sys.argv}}, target)
{'raise SystemExit(7)' if exit_immediately else 'time.sleep(60)'}
''')
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize('mismatch', ['owner', 'instance', 'protocol', 'daemon', 'socket', None])
def test_release_request_requires_full_ownership_proof(tmp_path, mismatch):
    runtime = tmp_path / 'private'
    runtime.mkdir(mode=0o700)
    record = DaemonRecord(
        schema_version=1, instance_uuid=INSTANCE, owner_token='private-owner-token',
        app=ProcessIdentity(pid=100, start_ticks=10),
        supervisor=ProcessIdentity(pid=200, start_ticks=20),
        daemon=ProcessIdentity(pid=300, start_ticks=30), protocol_version='v1',
        socket_path=str(runtime / 'daemon.sock'), runtime_directory=str(runtime), lifecycle='stopping',
    )
    process = FakeProcess()
    process.register(record.daemon, process.owner_environment(INSTANCE, str(runtime)))
    operations = []

    class Control:
        async def handshake(self, owner_token, instance_uuid):
            proof = Handshake(
                instance_uuid=INSTANCE, owner_token=record.owner_token,
                daemon=record.daemon, protocol_version='v1',
            )
            change = {
                'owner': {'owner_token': 'foreign'},
                'instance': {'instance_uuid': InstanceId(UUID(int=0))},
                'protocol': {'protocol_version': 'incompatible'},
                'daemon': {'daemon': replace(record.daemon, start_ticks=999)},
            }.get(mismatch, {})
            return Ok(value=replace(proof, **change))

        async def request(self, operation, target, payload, context):
            operations.append(operation)
            return Ok(value=StatusReply(
                state='idle', device=None, target=None, has_audio=False, audio_muted=False,
                needs_credential=False, credential_kind=None, streams=(),
                instance_uuid=INSTANCE, owner_token=record.owner_token, daemon=record.daemon,
                protocol_version='v1', socket_path='/foreign.sock' if mismatch == 'socket' else record.socket_path,
            ))

        async def close(self):
            pass

    supervisor = DaemonSupervisor(
        app=record.app, instance_uuid=INSTANCE, runtime_directory=runtime,
        config_fd=-1, process=process, control_factory=lambda r: Control(),
    )
    with socket.socket(socket.AF_UNIX) as endpoint:
        endpoint.bind(record.socket_path)
        asyncio.run(supervisor._request_release(record, FEATURES))
    assert (BackendOperation.shutdown in operations) is (mismatch is None)


def test_registry_launches_and_releases_real_supervisor_and_socket(tmp_path):
    """Exercise the FD, storage and procfs boundaries together, without a backend."""
    executable = tmp_path / 'socket-daemon'
    executable.write_text(f'''#!{sys.executable}
import json, os, socket, sys
from airplay_linux.process.procfs import read_identity
with os.fdopen(int(os.environ['AIRPLAY_LINUX_CONFIG_FD'])) as source:
    config = json.load(source)
identity = read_identity(os.getpid())
endpoint = socket.socket(socket.AF_UNIX)
endpoint.bind(config['socket_path'])
endpoint.listen(4)
while True:
    connection, _ = endpoint.accept()
    with connection, connection.makefile('rb') as source:
        operation = json.loads(source.readline())['operation']
        status = dict(config, daemon={{'pid': identity.pid, 'start_ticks': identity.start_ticks}})
        connection.sendall(json.dumps(status).encode() + b'\\n')
        if operation == 'shutdown':
            break
''')
    executable.chmod(0o700)

    class SocketControl:
        def __init__(self, record):
            self.record = record

        async def exchange(self, operation):
            reader, writer = await asyncio.open_unix_connection(self.record.socket_path)
            try:
                writer.write(json.dumps({'operation': operation}).encode() + b'\n')
                await writer.drain()
                return json.loads(await reader.readline())
            finally:
                writer.close()
                await writer.wait_closed()

        async def handshake(self, owner_token, instance_uuid):
            data = await self.exchange('status')
            return Ok(value=Handshake(
                instance_uuid=InstanceId(UUID(data['instance_uuid'])),
                owner_token=data['owner_token'], daemon=ProcessIdentity(**data['daemon']),
                protocol_version=data['protocol_version'],
            ))

        async def request(self, operation, target, payload, context):
            data = await self.exchange(operation.value)
            if operation is BackendOperation.shutdown:
                return Ok(value=AckReply(operation='shutdown'))
            return Ok(value=StatusReply(
                state='idle', device=None, target=None, has_audio=False, audio_muted=False,
                needs_credential=False, credential_kind=None, streams=(),
                instance_uuid=InstanceId(UUID(data['instance_uuid'])),
                owner_token=data['owner_token'], daemon=ProcessIdentity(**data['daemon']),
                protocol_version=data['protocol_version'], socket_path=data['socket_path'],
            ))

        async def close(self):
            pass

    async def scenario():
        process = LinuxProcessRunner()
        store = JsonDaemonStore(AtomicFiles(tmp_path))
        registry = DaemonRegistry(
            process=process, store=store, features=FEATURES,
            control_factory=SocketControl, runtime_directory=tmp_path / 'private',
            record_root=tmp_path, executable=str(executable),
        )
        notices = []
        registry.subscribe(notices.append)
        try:
            started = await registry.ensure()
            assert isinstance(started, Ok)
            record = started.value
            assert (await store.all()).value == (record,)
            assert record.lifecycle == 'running'
            assert process.owned(record.supervisor, record.instance_uuid, record.runtime_directory)
            assert process.owned(record.daemon, record.instance_uuid, record.runtime_directory)
            assert [n.kind for n in notices] == ['ready']
            assert await registry.shutdown() == Ok(value=None)
            assert (await store.all()).value == ()
            assert gone(record.supervisor) and gone(record.daemon)
            assert not Path(record.socket_path).exists()
        finally:
            # Preserve isolation even if an assertion fails during startup.
            for record in (await store.all()).value:
                for identity in (record.supervisor, record.daemon):
                    if identity is not None:
                        await process.terminate(identity, record.instance_uuid, record.runtime_directory, 0.1)

    asyncio.run(scenario())


class Running:
    def __init__(self, tmp_path, *, ignore_term=False, exit_immediately=False,
                 dead_app=False, incompatible=False, external_app=False):
        self.runtime = tmp_path / 'private'
        self.runtime.mkdir(mode=0o700)
        self.store = JsonDaemonStore(AtomicFiles(tmp_path))
        self.daemon = None
        self.app_process = None
        self.supervisor = None
        executable = fake_daemon(tmp_path, ignore_term=ignore_term, exit_immediately=exit_immediately)
        config_read, config_write = os.pipe()
        app = read_identity(os.getpid())
        if external_app:
            app_ready = tmp_path / 'app-ready'
            supervisor_pid = tmp_path / 'supervisor-pid'
            code = '''import os, subprocess, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
sys.stdin.buffer.read(1)
child = subprocess.Popen([sys.executable, '-m', 'airplay_linux.process.supervisor',
    '--app-pid', str(os.getpid()), '--app-start', sys.argv[3],
    '--instance-uuid', sys.argv[4], '--runtime-directory', sys.argv[5],
    '--config-fd', sys.argv[6]], pass_fds=(int(sys.argv[6]),),
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(60)
'''
            # The helper reads its own start ticks just before spawning so the
            # command uses the exact identity also recorded by this test.
            code = code.replace("'--app-start', sys.argv[3]", "'--app-start', str(__import__('airplay_linux.process.procfs', fromlist=['read_identity']).read_identity(os.getpid()).start_ticks)")
            self.app_process = subprocess.Popen(
                [sys.executable, '-c', code, str(app_ready), str(supervisor_pid),
                 'unused', str(INSTANCE), str(self.runtime), str(config_read)],
                env={**os.environ, 'PYTHONPATH': SRC}, pass_fds=(config_read,),
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            wait_until(app_ready.exists)
            app = read_identity(self.app_process.pid)
        if dead_app:
            app = ProcessIdentity(pid=app.pid, start_ticks=app.start_ticks + 999)
        record = DaemonRecord(
            schema_version=1, instance_uuid=INSTANCE, owner_token='private-owner-token',
            app=app, supervisor=None, daemon=None, protocol_version='v1',
            socket_path=str(self.runtime / 'daemon.sock'), lifecycle='starting',
            runtime_directory=str(self.runtime),
        )
        assert asyncio.run(self.store.write(record)) == Ok(value=None)
        features = asdict(FEATURES)
        if incompatible:
            features['foreground_control'] = False
        payload = json.dumps({
            'owner_token': record.owner_token, 'protocol_version': 'v1',
            'executable': str(executable), 'record_root': str(tmp_path),
            'features': features,
        }).encode()
        os.write(config_write, payload)
        os.close(config_write)
        environment = {
            **os.environ, 'PYTHONPATH': SRC,
            OWNER_TOKEN_ENV: str(INSTANCE), RUNTIME_DIRECTORY_ENV: str(self.runtime),
        }
        try:
            if external_app:
                self.app_process.stdin.write(b'1')
                self.app_process.stdin.flush()
                wait_until(supervisor_pid.exists)
                self.supervisor_identity = read_identity(int(supervisor_pid.read_text()))
            else:
                self.supervisor = subprocess.Popen(
                    [sys.executable, '-m', 'airplay_linux.process.supervisor',
                     '--app-pid', str(app.pid), '--app-start', str(app.start_ticks),
                     '--instance-uuid', str(INSTANCE), '--runtime-directory', str(self.runtime),
                     '--config-fd', str(config_read)],
                    env=environment, pass_fds=(config_read,),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                )
                self.supervisor_identity = read_identity(self.supervisor.pid)
        finally:
            os.close(config_read)

    def wait_ready(self):
        path = self.runtime / 'ready.json'
        wait_until(path.exists)
        data = json.loads(path.read_text())
        self.daemon = read_identity(data['pid'])
        return data

    def close(self):
        if self.supervisor is not None:
            if self.supervisor.poll() is None:
                self.supervisor.terminate()
                try:
                    self.supervisor.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.supervisor.kill()
                    self.supervisor.wait(timeout=2)
            self.supervisor.stderr.close()
        elif self.supervisor_identity is not None and not gone(self.supervisor_identity):
            os.kill(self.supervisor_identity.pid, signal.SIGTERM)
            wait_until(lambda: gone(self.supervisor_identity), timeout=6)
        if self.daemon is not None and not gone(self.daemon):
            os.kill(self.daemon.pid, signal.SIGKILL)
            wait_until(lambda: gone(self.daemon))
        if self.app_process is not None:
            self.app_process.kill()
            self.app_process.wait(timeout=2)
            self.app_process.stdin.close()


def test_supervisor_publishes_exact_pairs_and_config_never_enters_argv(tmp_path):
    child = Running(tmp_path)
    try:
        data = child.wait_ready()
        wait_until(lambda: asyncio.run(child.store.all()).value[0].daemon is not None)
        r = asyncio.run(child.store.all()).value[0]
        assert r.lifecycle == 'starting'  # Only the authenticated registry publishes running.
        assert r.supervisor == child.supervisor_identity
        assert r.daemon == child.daemon
        assert data['config']['owner_token'] == 'private-owner-token'
        assert data['config']['instance_uuid'] == str(INSTANCE)
        assert 'private-owner-token' not in repr(data['argv'])
        assert (child.runtime.stat().st_mode & 0o777) == 0o700
    finally:
        child.close()
    assert gone(child.daemon)


@pytest.mark.parametrize('reason', ['parent_identity', 'incompatible'])
def test_supervisor_refuses_launch_after_parent_race_or_incompatible_features(tmp_path, reason):
    child = Running(tmp_path, dead_app=reason == 'parent_identity', incompatible=reason == 'incompatible')
    try:
        assert child.supervisor.wait(timeout=3) != 0
        assert not (child.runtime / 'ready.json').exists()
        assert asyncio.run(child.store.all()).value[0].daemon is None
    finally:
        child.close()


def test_direct_child_death_ends_supervisor_and_retains_stopping_evidence(tmp_path):
    child = Running(tmp_path, exit_immediately=True)
    try:
        wait_until((child.runtime / 'ready.json').exists)
        assert child.supervisor.wait(timeout=5) != 0
        r = asyncio.run(child.store.all()).value[0]
        assert r.lifecycle == 'stopping'
        assert gone(r.daemon)
    finally:
        child.close()


def test_shutdown_escalates_after_three_seconds_and_reaps_exact_child(tmp_path):
    child = Running(tmp_path, ignore_term=True)
    try:
        child.wait_ready()
        start = time.monotonic()
        child.supervisor.terminate()
        child.supervisor.wait(timeout=6)
        elapsed = time.monotonic() - start
        assert 2.8 <= elapsed < 5.5
        assert (child.runtime / 'term').read_text() == 'received'
        assert read_identity_evidence(child.daemon.pid).state is IdentityState.absent
        assert asyncio.run(child.store.all()).value[0].lifecycle == 'stopping'
    finally:
        child.close()


def test_killing_supervisor_cannot_leave_a_daemon_that_ignores_term(tmp_path):
    child = Running(tmp_path, ignore_term=True)
    try:
        child.wait_ready()
        child.supervisor.kill()
        child.supervisor.wait(timeout=2)
        wait_until(lambda: gone(child.daemon), timeout=2)
    finally:
        child.close()


def test_real_parent_death_releases_child_and_ends_supervisor(tmp_path):
    child = Running(tmp_path, external_app=True)
    try:
        child.wait_ready()
        child.app_process.kill()
        child.app_process.wait(timeout=2)
        wait_until(lambda: gone(child.daemon), timeout=5)
        wait_until(lambda: gone(child.supervisor_identity), timeout=5)
        assert asyncio.run(child.store.all()).value[0].lifecycle == 'stopping'
    finally:
        child.close()
