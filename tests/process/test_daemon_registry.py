"""Lifecycle publication and conservative recovery at the registry boundary."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass, replace
from functools import wraps
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.backend.control import (
    AckReply, ApplicationContractProfile, BackendOperation, Handshake, StatusReply,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, OwnedProcessScan
from airplay_linux.testing.adapters import FakeProcess, MemoryDaemonStore, SequenceIds

APP = ProcessIdentity(pid=100, start_ticks=10)
SUPERVISOR = ProcessIdentity(pid=200, start_ticks=20)
DAEMON = ProcessIdentity(pid=300, start_ticks=30)
FEATURES = ApplicationContractProfile(socket_path='/unused').features


@dataclass(frozen=True)
class IncompleteScan:
    matches: tuple[ProcessIdentity, ...] = ()
    complete: bool = False


class Clock:
    value = 0.0

    def now(self):
        return self.value


class Control:
    def __init__(self, harness, record):
        self.harness = harness
        self.record = record

    async def handshake(self, owner_token, instance_uuid):
        h = self.harness
        h.order.append('handshake')
        assert h.store.records[instance_uuid].lifecycle != 'running' or h.ready
        if h.handshake_gate is not None:
            await h.handshake_gate.wait()
        if h.bad_handshake:
            return Ok(value=Handshake(
                instance_uuid=instance_uuid, owner_token='foreign',
                daemon=self.record.daemon or DAEMON, protocol_version='v1',
            ))
        return Ok(value=Handshake(
            instance_uuid=instance_uuid, owner_token=owner_token,
            daemon=self.record.daemon or DAEMON, protocol_version='v1',
        ))

    async def request(self, operation, target, payload, context):
        h, r = self.harness, self.record
        if operation is BackendOperation.shutdown:
            assert h.store.records[r.instance_uuid].lifecycle == 'stopping'
            h.order.append('shutdown')
            return Ok(value=AckReply(operation='shutdown'))
        return Ok(value=StatusReply(
            state='idle', device=None, target=None, has_audio=False,
            audio_muted=False, needs_credential=False, credential_kind=None,
            streams=(), owner_token=r.owner_token, instance_uuid=r.instance_uuid,
            daemon=r.daemon or DAEMON, protocol_version='v1',
            socket_path='/foreign.sock' if h.bad_socket else r.socket_path,
        ))

    async def close(self):
        pass


class Process(FakeProcess):
    def __init__(self, harness):
        super().__init__()
        self.harness = harness
        self.register(APP, {})
        self.fail_spawn = False
        self.keep_alive = False

    async def spawn(self, spec):
        h = self.harness
        r = next(iter(h.store.records.values()))
        assert r.lifecycle == 'starting'
        assert r.supervisor is None and r.daemon is None
        assert r.owner_token not in ' '.join(spec.argv)
        assert r.owner_token not in repr(dict(spec.env))
        assert len(spec.pass_fds) == 1
        h.config_fds.append(spec.pass_fds[0])
        h.configs.append(json.loads(os.read(spec.pass_fds[0], 4096)))
        h.order.append('spawn')
        self.spawned.append(spec)
        if self.fail_spawn:
            return Err(error=diagnostic(ErrorCode.daemon_failed))
        self.register(SUPERVISOR, self.owner_environment(r.instance_uuid, r.runtime_directory))
        self.register(DAEMON, self.owner_environment(r.instance_uuid, r.runtime_directory))
        await h.store.write(replace(r, supervisor=SUPERVISOR, daemon=DAEMON))
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(r.socket_path)
        h.sockets.append(endpoint)
        return Ok(value=SUPERVISOR)

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline):
        h = self.harness
        assert h.store.records[owner_uuid].lifecycle == 'stopping'
        h.order.append('term')
        if self.keep_alive:
            return Ok(value=None)
        return await super().terminate(identity, owner_uuid, runtime_directory, deadline)


class Harness:
    def __init__(self, tmp_path):
        self.store = MemoryDaemonStore()
        self.clock = Clock()
        self.order = []
        self.config_fds = []
        self.configs = []
        self.sockets = []
        self.handshake_gate = None
        self.bad_handshake = False
        self.bad_socket = False
        self.ready = False
        self.unreadable = set()
        self.process = Process(self)
        self.registry = DaemonRegistry(
            process=self.process, store=self.store,
            control_factory=lambda r: Control(self, r), features=FEATURES,
            runtime_directory=tmp_path / 'instance', record_root=tmp_path,
            app=APP, ids=SequenceIds(seed=1000), clock=self.clock,
            inspect_process=self.inspect, scan_owned=self.scan,
            restart_limit=2, restart_window=60.0, startup_timeout=0.2,
        )
        self.notices = []
        self.registry.subscribe(self.notices.append)

    def inspect(self, pid):
        if pid in self.unreadable:
            return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
        identity = self.process.identity(pid)
        return IdentityEvidence(
            pid=pid, state=IdentityState.present if identity else IdentityState.absent,
            identity=identity, uid=os.getuid(),
        )

    def scan(self, owner, runtime):
        return OwnedProcessScan(
            matches=tuple(
                identity for pid in self.process.environments()
                if (identity := self.process.identity(pid)) is not None
                and self.process.owned(identity, owner, runtime)
            ),
            complete=True,
        )

    async def start(self):
        result = await self.registry.ensure()
        if isinstance(result, Ok):
            self.ready = True
        return result

    def close(self):
        for endpoint in self.sockets:
            endpoint.close()


def run_async(test):
    @wraps(test)
    def run(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))
    return run


@pytest.fixture
def harness(tmp_path):
    h = Harness(tmp_path)
    try:
        yield h
    finally:
        h.close()


@run_async
async def test_starting_is_durable_before_spawn_and_running_waits_for_handshake(harness):
    h = harness
    h.handshake_gate = asyncio.Event()
    pending = asyncio.create_task(h.start())
    for _ in range(100):
        if 'handshake' in h.order:
            break
        await asyncio.sleep(0)
    assert 'handshake' in h.order
    record = next(iter(h.store.records.values()))
    assert record.lifecycle == 'starting'
    assert h.notices == []
    h.handshake_gate.set()
    result = await pending
    assert isinstance(result, Ok)
    assert result.value.lifecycle == 'running'
    assert result.value.supervisor == SUPERVISOR and result.value.daemon == DAEMON
    assert h.order[:2] == ['spawn', 'handshake']
    assert [n.kind for n in h.notices] == ['ready']
    assert h.configs[0]['owner_token'] == result.value.owner_token
    for fd in h.config_fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@run_async
async def test_ensure_is_idempotent_and_serializes_concurrent_launches(harness):
    results = await asyncio.gather(harness.start(), harness.start())
    assert all(isinstance(r, Ok) for r in results)
    assert results[0].value == results[1].value
    assert len(harness.process.spawned) == 1


@run_async
async def test_incompatible_backend_is_never_launched(harness):
    harness.registry._features = replace(FEATURES, foreground_control=False)
    result = await harness.start()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert not harness.process.spawned and not harness.store.records


@run_async
async def test_public_profile_with_fixture_flags_cannot_select_foreground_launch(harness):
    h = harness
    h.registry._features = replace(
        FEATURES, profile='doubletake-0.5+source', owned_daemon_control=True,
    )
    result = await h.start()
    assert isinstance(result, Err) and result.error.code is ErrorCode.dependency_incompatible
    assert not h.process.spawned
    assert not h.store.records


@run_async
async def test_failed_starting_publication_never_spawns(harness):
    harness.store.queue_result('write', Err(error=diagnostic(ErrorCode.persistence_failed)))
    result = await harness.start()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    assert not harness.process.spawned


@run_async
async def test_shutdown_publishes_stopping_before_shutdown_request_and_term(harness):
    assert isinstance(await harness.start(), Ok)
    assert await harness.registry.shutdown() == Ok(value=None)
    assert not harness.store.records
    assert harness.process.identity(SUPERVISOR.pid) is None
    assert harness.process.identity(DAEMON.pid) is None
    assert harness.order.index('shutdown') < harness.order.index('term')
    assert not (harness.registry.runtime_directory / 'daemon.sock').exists()


@run_async
async def test_failed_stopping_publication_blocks_all_destructive_cleanup(harness):
    assert isinstance(await harness.start(), Ok)
    harness.store.queue_result('write', Err(error=diagnostic(ErrorCode.persistence_failed)))
    result = await harness.registry.shutdown()
    assert isinstance(result, Err)
    assert harness.process.terminated == []
    assert 'shutdown' not in harness.order
    assert harness.notices[-1].kind == 'cleanup_required'


@run_async
@pytest.mark.parametrize('mismatch', ['pid', 'owner', 'socket', 'handshake', 'unreadable'])
async def test_live_mismatched_evidence_blocks_cleanup_and_replacement(harness, mismatch):
    h = harness
    started = await h.start()
    assert isinstance(started, Ok)
    r = started.value
    if mismatch == 'pid':
        h.process.register(replace(DAEMON, start_ticks=999), h.process.owner_environment(r.instance_uuid, r.runtime_directory))
    elif mismatch == 'owner':
        h.process.register(DAEMON, {})
    elif mismatch == 'socket':
        h.bad_socket = True
    elif mismatch == 'handshake':
        h.bad_handshake = True
    else:
        h.unreadable.add(DAEMON.pid)
    result = await h.registry.restart()
    assert isinstance(result, Err)
    assert h.process.terminated == []
    assert len(h.process.spawned) == 1
    assert Path(r.socket_path).exists()
    assert h.notices[-1].kind == 'cleanup_required'


@run_async
async def test_missing_socket_with_live_running_process_is_not_stale_cleanup(harness):
    result = await harness.start()
    Path(result.value.socket_path).unlink()
    refused = await harness.registry.restart()
    assert isinstance(refused, Err)
    assert not harness.process.terminated
    assert len(harness.process.spawned) == 1


@run_async
@pytest.mark.parametrize('lifecycle', ['starting', 'running', 'stopping'])
async def test_dead_previous_records_release_stale_socket_only_after_process_proof(harness, lifecycle):
    h = harness
    result = await h.start()
    r = replace(result.value, lifecycle=lifecycle)
    h.store.records[r.instance_uuid] = r
    h.process.exit(APP)
    h.process.exit(SUPERVISOR)
    h.process.exit(DAEMON)
    assert await h.registry.reconcile_previous() == Ok(value=None)
    assert not Path(r.socket_path).exists()
    assert not h.store.records


@run_async
@pytest.mark.parametrize('lifecycle', ['starting', 'stopping'])
async def test_parent_dead_crash_window_finds_and_releases_unrecorded_owned_children(harness, lifecycle):
    h = harness
    result = await h.start()
    r = replace(result.value, lifecycle=lifecycle, supervisor=None, daemon=None)
    h.store.records[r.instance_uuid] = r
    h.process.exit(APP)
    assert await h.registry.reconcile_previous() == Ok(value=None)
    assert set(h.process.terminated) == {SUPERVISOR, DAEMON}
    assert not h.store.records


@run_async
async def test_shutdown_rechecks_status_after_stopping_publication(harness):
    h = harness
    await h.start()
    write = h.store.write

    async def publish(record):
        result = await write(record)
        if record.lifecycle == 'stopping':
            h.bad_socket = True
        return result

    h.store.write = publish
    refused = await h.registry.shutdown()
    assert isinstance(refused, Err)
    assert 'shutdown' not in h.order and not h.process.terminated
    assert len(h.process.spawned) == 1


@run_async
async def test_daemon_death_during_running_publication_never_emits_ready(harness):
    h = harness
    write = h.store.write

    async def publish(record):
        result = await write(record)
        if record.lifecycle == 'running':
            h.process.exit(DAEMON, 7)
        return result

    h.store.write = publish
    refused = await h.start()
    assert isinstance(refused, Err)
    assert 'ready' not in [n.kind for n in h.notices]


@run_async
async def test_failed_running_publication_retains_starting_and_never_emits_ready(harness):
    h = harness
    write = h.store.write

    async def publish(record):
        if record.lifecycle == 'running':
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        return await write(record)

    h.store.write = publish
    refused = await h.start()
    assert isinstance(refused, Err)
    assert next(iter(h.store.records.values())).lifecycle == 'starting'
    assert 'ready' not in [n.kind for n in h.notices]
    assert len(h.process.spawned) == 1


@run_async
async def test_running_ensure_observes_durable_stopping_record(harness):
    h = harness
    started = await h.start()
    h.store.records[started.value.instance_uuid] = replace(started.value, lifecycle='stopping')
    refused = await h.start()
    assert isinstance(refused, Err)
    assert len(h.process.spawned) == 1


@run_async
async def test_surviving_previous_processes_prohibit_replacement_streams(harness):
    assert isinstance(await harness.start(), Ok)
    harness.process.keep_alive = True
    result = await harness.registry.restart()
    assert isinstance(result, Err)
    assert len(harness.process.spawned) == 1
    assert harness.store.records
    assert harness.notices[-1].kind == 'cleanup_required'


@run_async
async def test_incomplete_owner_scan_blocks_cleanup_and_replacement(harness):
    started = await harness.start()
    assert isinstance(started, Ok)
    harness.registry._scan = lambda owner, runtime: IncompleteScan()

    refused = await harness.registry.restart()

    assert isinstance(refused, Err)
    assert harness.process.terminated == []
    assert len(harness.process.spawned) == 1
    assert harness.store.records
    assert harness.notices[-1].kind == 'cleanup_required'


@run_async
async def test_scan_includes_a_stream_not_named_in_the_record(harness):
    h = harness
    started = await h.start()
    r = started.value
    stream = ProcessIdentity(pid=400, start_ticks=40)
    h.process.register(stream, h.process.owner_environment(r.instance_uuid, r.runtime_directory))
    assert await h.registry.shutdown() == Ok(value=None)
    assert set(h.process.terminated) == {SUPERVISOR, DAEMON, stream}


@run_async
async def test_listening_socket_is_not_unlinked_when_recorded_processes_are_dead(harness):
    result = await harness.start()
    harness.sockets[-1].listen(1)
    harness.process.exit(SUPERVISOR)
    harness.process.exit(DAEMON)
    refused = await harness.registry.reconcile_previous()
    assert isinstance(refused, Err)
    assert Path(result.value.socket_path).exists()


@run_async
async def test_exit_notices_only_name_the_current_exact_process_pair(harness):
    await harness.start()
    harness.process.emit(type('Exit', (), {'identity': replace(SUPERVISOR, start_ticks=999)})())
    assert [n.kind for n in harness.notices] == ['ready']
    harness.process.exit(SUPERVISOR, 9)
    assert [n.kind for n in harness.notices] == ['ready', 'exit']
    assert harness.notices[-1].diagnostic.code is ErrorCode.daemon_failed


@run_async
async def test_failed_launches_consume_restart_budget_and_success_does_not_reset_it(harness):
    h = harness
    await h.start()
    assert isinstance(await h.registry.restart(), Ok)
    h.process.fail_spawn = True
    assert isinstance(await h.registry.restart(), Err)
    attempts = len(h.process.spawned)
    exhausted = await h.registry.ensure()
    assert isinstance(exhausted, Err)
    assert len(h.process.spawned) == attempts == 3
    h.clock.value = 61.0
    h.process.fail_spawn = False
    assert isinstance(await h.start(), Ok)
    assert len(h.process.spawned) == 4


@run_async
@pytest.mark.parametrize('states', [
    (('reserved', 1), ('ready', 1)),
    (('reserved', 1), ('stopped', 2)),
    (('failed', 4),),
    (('ready', 3),),
])
async def test_receipt_obligations_block_legacy_cleanup_and_replacement(harness, states):
    from types import SimpleNamespace
    from airplay_linux.domain.records import LaunchReceipt

    h = harness
    started = await h.start()
    assert isinstance(started, Ok)
    r = started.value
    receipts = []
    for index, (state, generation) in enumerate(states):
        receipts.append(LaunchReceipt(
            schema_version=1, launch_uuid=UUID(int=index + 20),
            instance_uuid=InstanceId(UUID(int=10)), app=APP, owner_uid=os.getuid(),
            boot_id=h.registry._authority.boot_id(), runtime_directory=r.runtime_directory,
            runtime_device=0, runtime_inode=1, endpoint_generation=generation,
            socket_name='daemon.sock', protocol_version='v1', state=state,
            daemon_instance_uuid=None if state == 'reserved' else r.instance_uuid,
            supervisor=None if state == 'reserved' else SUPERVISOR,
            daemon=None if state == 'reserved' else DAEMON,
            entry_device=None, entry_inode=None, socket_inode=None,
            peer_pid=None, peer_start_ticks=None, peer_uid=None,
            status_state=None, credential_service='unknown',
            failure=ErrorCode.daemon_failed if state == 'failed' else None, updated_at=1.0,
        ))

    async def all_receipts():
        return Ok(value=tuple(receipts))

    h.registry._receipts = SimpleNamespace(all=all_receipts)
    before = (h.registry._record_root / 'daemon/authority.owner').read_bytes()
    for operation in (h.registry.ensure, h.registry.restart, h.registry.shutdown):
        refused = await operation()
        assert isinstance(refused, Err)
        assert not h.process.terminated
        assert len(h.process.spawned) == 1
        assert Path(r.socket_path).exists()
        assert (h.registry._record_root / 'daemon/authority.owner').read_bytes() == before
    assert h.notices[-1].kind == 'cleanup_required'


@run_async
async def test_supervisor_intent_without_receipt_is_retained_without_signals(harness):
    from types import SimpleNamespace
    from airplay_linux.domain.records import SupervisorReleaseIntent

    h = harness
    started = await h.start()
    assert isinstance(started, Ok)
    intent = SupervisorReleaseIntent(
        schema_version=1, launch_uuid=UUID(int=99), instance_uuid=InstanceId(UUID(int=10)),
        app=APP, supervisor=SUPERVISOR, daemon=DAEMON,
        reason='application-exited', intent_at=1.0,
    )

    async def read_intent():
        return Ok(value=intent)

    h.registry._supervisor_intents = lambda runtime: SimpleNamespace(read=read_intent)
    refused = await h.registry.shutdown()
    assert isinstance(refused, Err)
    assert not h.process.terminated
    assert h.store.records
    assert (h.registry._record_root / 'daemon/authority.owner').exists()


@run_async
async def test_missing_observation_does_not_clear_owner_or_abandon_live_daemon(harness):
    h = harness
    assert isinstance(await h.start(), Ok)
    owner_path = h.registry._record_root / 'daemon/authority.owner'
    before = owner_path.read_bytes()
    h.store.records.clear()
    refused = await h.registry.shutdown()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.ownership_mismatch
    assert owner_path.read_bytes() == before
    assert not h.process.terminated
    assert h.process.identity(DAEMON.pid) == DAEMON
    assert (h.registry.runtime_directory / 'daemon.sock').exists()


@run_async
async def test_orphan_endpoint_retains_owner_without_an_observation(harness):
    h = harness
    assert isinstance(await h.start(), Ok)
    owner_path = h.registry._record_root / 'daemon/authority.owner'
    before = owner_path.read_bytes()
    h.registry._record = None
    h.store.records.clear()
    refused = await h.registry.reconcile_previous()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_socket
    assert owner_path.read_bytes() == before
    assert not h.process.terminated
    assert (h.registry.runtime_directory / 'daemon.sock').exists()


@run_async
async def test_registry_exception_unlocks_and_retains_owner(harness):
    import fcntl

    h = harness
    assert isinstance(await h.start(), Ok)
    owner_path = h.registry._record_root / 'daemon/authority.owner'
    before = owner_path.read_bytes()

    async def broken():
        raise RuntimeError('injected store failure')

    h.store.all = broken
    with pytest.raises(RuntimeError, match='injected store failure'):
        await h.registry.shutdown()
    assert owner_path.read_bytes() == before
    descriptor = os.open(owner_path.with_name('authority.lock'), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
    assert not h.process.terminated


@run_async
async def test_shutdown_acquires_once_and_cancellation_unlocks(harness):
    import fcntl

    h = harness
    assert hasattr(h.registry, '_authority'), 'registry missing shared-root authority'
    assert isinstance(await h.start(), Ok)
    owner_path = h.registry._record_root / 'daemon/authority.owner'
    before = owner_path.read_bytes()
    authority = h.registry._authority
    acquire = authority.acquire
    acquisitions = []

    async def counted(instance):
        acquisitions.append(instance)
        return await acquire(instance)

    authority.acquire = counted
    entered = asyncio.Event()
    all_records = h.store.all

    async def paused():
        entered.set()
        await asyncio.Future()

    h.store.all = paused
    pending = asyncio.create_task(h.registry.shutdown())
    await asyncio.wait_for(entered.wait(), 1.0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(acquisitions) == 1
    assert owner_path.read_bytes() == before
    assert not h.process.terminated
    descriptor = os.open(owner_path.with_name('authority.lock'), os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
    h.store.all = all_records
    acquisitions.clear()
    h.process.keep_alive = True
    refused = await h.registry.shutdown()
    assert isinstance(refused, Err)
    assert len(acquisitions) == 1
    assert owner_path.read_bytes() == before
    h.process.keep_alive = False
    acquisitions.clear()
    assert await h.registry.shutdown() == Ok(value=None)
    assert len(acquisitions) == 1
    assert not owner_path.exists()


@run_async
async def test_private_runtime_rejects_symlink_directory_and_socket(harness, tmp_path):
    h = harness
    destination = tmp_path / 'elsewhere'
    destination.mkdir()
    h.registry.runtime_directory.symlink_to(destination, target_is_directory=True)
    refused = await h.start()
    assert isinstance(refused, Err)
    assert not h.process.spawned
    h.registry.runtime_directory.unlink()
    result = await h.start()
    assert isinstance(result, Ok)
    r = result.value
    Path(r.socket_path).unlink()
    marker = destination / 'untouched'
    marker.write_text('preserved')
    Path(r.socket_path).symlink_to(marker)
    refused = await h.registry.shutdown()
    assert isinstance(refused, Err)
    assert marker.read_text() == 'preserved'
    assert not h.process.terminated


@pytest.fixture
def application_daemon_identity_cases(tmp_path, monkeypatch):
    from test_owned_launcher import candidate_failure_cases
    yield from candidate_failure_cases.__wrapped__(tmp_path, monkeypatch)


async def test_receipt_joins_application_and_observed_daemon_instance_without_token(application_daemon_identity_cases):
    c = application_daemon_identity_cases
    assert isinstance(await c.launch(), Ok)
    r = await c.current()
    assert r.instance_uuid != r.launch_uuid != r.daemon_instance_uuid
    assert r.instance_uuid != r.daemon_instance_uuid
    assert r.daemon_instance_uuid == c.record.instance_uuid
    assert c.record.owner_token == 'unused'
    payload = (c.root / 'daemon/receipts' / (str(r.launch_uuid) + '.json')).read_bytes()
    assert b'owner_token' not in payload and b'unused' not in payload
    assert 'unused' not in repr(r)
    assert isinstance(await c.registry._join_receipt(replace(r, daemon_instance_uuid=r.instance_uuid)), Err)
    assert c.configs[0]['launch_uuid'] == str(r.launch_uuid)
    assert c.configs[0]['endpoint_generation'] == r.endpoint_generation
    assert 'owner_token' not in c.configs[0]


@pytest.mark.parametrize('pipe', [None, 'stale', 'conflicting'])
async def test_restart_reconstructs_unique_observation_gap_without_config_pipe(application_daemon_identity_cases, monkeypatch, pipe):
    import json
    from test_owned_launcher import Crash, DAEMON
    from airplay_linux.process import daemon_registry as module
    c = application_daemon_identity_cases
    c.hooks['supervisor-publication'] = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await c.launch()
    assert (await c.current()).daemon_instance_uuid is None
    c.reopen()
    def forbidden_pipe(*args, **kwargs):
        raise AssertionError('restart required original launch pipe')

    monkeypatch.setattr(module, 'config_pipe', forbidden_pipe)
    rfd, wfd = os.pipe()
    payload = json.dumps({'launch_uuid': str(UUID(int=999)), 'endpoint_generation': 999,
                          'daemon_instance_uuid': str(UUID(int=998))}).encode()
    os.write(wfd, payload)
    os.close(wfd)
    if pipe is not None:
        monkeypatch.setenv('AIRPLAY_LINUX_CONFIG_FD', str(rfd if pipe == 'conflicting' else -1))
    else:
        monkeypatch.delenv('AIRPLAY_LINUX_CONFIG_FD', raising=False)
    try:
        assert await c.registry.reconcile_previous() == Ok(value=None)
        assert os.read(rfd, 4096) == payload
    finally:
        os.close(rfd)
    assert c.process.terminated == [DAEMON]
    receipt = await c.current()
    assert receipt.state == 'stopped'
    assert receipt.daemon_instance_uuid == c.record.instance_uuid


@pytest.mark.parametrize('count', [0, 2, 3])
async def test_observation_join_refuses_ambiguous_restart_records(application_daemon_identity_cases, monkeypatch, count):
    from test_owned_launcher import Crash
    c = application_daemon_identity_cases
    c.hooks['supervisor-publication'] = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await c.launch()
    if count == 0:
        assert await c.store.delete(c.record.instance_uuid) == Ok(value=None)
    else:
        for index in range(count - 1):
            assert await c.store.write(replace(c.record, instance_uuid=InstanceId(UUID(int=1111 + index)),
                                              lifecycle='stopping')) == Ok(value=None)
    monkeypatch.setenv('AIRPLAY_LINUX_CONFIG_FD', '-1')
    c.reopen()
    assert isinstance(await c.registry.reconcile_previous(), Err)
    assert c.process.terminated == []
    assert (await c.current()).state == 'reserved'
    assert len(c.process.spawned) == 1


async def test_missing_cleanup_intent_is_rederived_without_authorising_teardown(application_daemon_identity_cases):
    from test_owned_launcher import DAEMON
    c = application_daemon_identity_cases
    c.facts.on_peer = lambda: setattr(c.facts, 'peer_pid', 999)
    assert isinstance(await c.launch(), Err)
    receipt = await c.current()
    path = c.root / 'daemon/receipts' / (str(receipt.launch_uuid) + '.json')
    frozen = path.read_bytes()
    assert receipt.state == 'failed'
    assert not (await c.cleanup.all()).value
    c.reopen()
    assert isinstance(await c.registry.reconcile_previous(), Err)
    assert len((await c.cleanup.all()).value) == 1
    assert not c.process.terminated
    assert c.process.identity(DAEMON.pid) == DAEMON
    assert (await c.current()) == receipt
    assert isinstance(await c.launch(), Err) and len(c.process.spawned) == 1
    c.absence()
    # This receipt never pinned the mismatched entry; an independent removal is
    # required as well as child absence before an outcome can complete.
    (c.runtime / 'daemon.sock').unlink()
    c.reopen()
    assert await c.registry.reconcile_previous() == Ok(value=None)
    completion = (await c.cleanup.all()).value[0]
    assert completion.outcome == 'stopped'
    assert await c.registry.reconcile_previous() == Ok(value=None)
    assert (await c.cleanup.all()).value == (completion,)
    assert path.read_bytes() == frozen and not c.process.terminated


@pytest.mark.parametrize('absence', ['present', 'absent', 'unreadable', 'mismatched'])
@pytest.mark.parametrize('boundary', [None, 'cleanup-intent-publication', 'terminal-write', 'cleanup-intent-completion'])
async def test_supervisor_intent_recovery_terminalises_without_resignalling(application_daemon_identity_cases, absence, boundary):
    from test_owned_launcher import APP_ID, DAEMON, Crash
    from airplay_linux.domain.records import SupervisorReleaseIntent
    c = application_daemon_identity_cases
    assert isinstance(await c.launch(), Ok)
    receipt = await c.current()
    intent = SupervisorReleaseIntent(
        schema_version=1, launch_uuid=receipt.launch_uuid, instance_uuid=APP_ID,
        app=receipt.app, supervisor=receipt.supervisor, daemon=receipt.daemon,
        reason='application-exited', intent_at=1.0,
    )
    await c.supervisor_intents(str(c.runtime)).write(intent)
    before = (c.runtime / 'release-intent.json').read_bytes()
    c.reopen()
    if absence == 'absent':
        c.absence()
    elif absence == 'unreadable':
        c.unreadable.add(DAEMON.pid)
    elif absence == 'mismatched':
        c.process.register(replace(DAEMON, start_ticks=999), {})
    if boundary is not None and (absence == 'absent' or boundary != 'cleanup-intent-completion'):
        c.hooks[boundary] = lambda: (_ for _ in ()).throw(Crash())
        with pytest.raises(Crash):
            await c.registry.reconcile_previous()
        assert boundary in c.events
        c.reopen()
    result = await c.registry.reconcile_previous()
    assert isinstance(result, Ok if absence == 'absent' else Err)
    assert c.process.terminated == []
    assert (await c.current()).state == ('stopped' if absence == 'absent' else 'failed')
    assert len((await c.cleanup.all()).value) == 1
    failed = c.root / 'daemon/receipts' / (str(receipt.launch_uuid) + '.json')
    frozen = failed.read_bytes()
    c.unreadable.clear()
    c.process.exit(c.process.identity(DAEMON.pid) or DAEMON)
    c.absence()
    assert await c.registry.reconcile_previous() == Ok(value=None)
    completion = (await c.cleanup.all()).value[0]
    assert completion.outcome == 'stopped'
    assert await c.registry.reconcile_previous() == Ok(value=None)
    assert (await c.cleanup.all()).value == (completion,)
    assert failed.read_bytes() == frozen
    assert (c.runtime / 'release-intent.json').read_bytes() == before
    assert c.process.terminated == []
