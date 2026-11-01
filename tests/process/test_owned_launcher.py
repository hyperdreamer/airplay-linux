"""Staged public lifecycle exercised through real stores and independent OS facts."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from airplay_linux.backend.control import BackendOperation, PublishedDoubletakeProfile, StatusReply
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonCleanupIntent, SupervisorReleaseIntent
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process import ownership
from airplay_linux.process.authority import SharedRootAuthority
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.launcher import ProductionLaunchCapability
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, OwnedProcessScan, TerminationEvidence
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage.repositories import (
    JsonDaemonCleanupIntentStore, JsonDaemonStore, JsonLaunchReceiptStore,
    JsonSupervisorReleaseIntentStore,
)
from airplay_linux.testing.adapters import FakeProcess, SequenceIds

APP = ProcessIdentity(pid=110, start_ticks=10)
SUPERVISOR = ProcessIdentity(pid=120, start_ticks=20)
DAEMON = ProcessIdentity(pid=130, start_ticks=30)
APP_ID = InstanceId(UUID(int=900))
BOOT = str(UUID(int=901))
FEATURES = replace(
    PublishedDoubletakeProfile(socket_path='/unused').features,
    owned_daemon_control=True, keyring=True,
)


class Crash(BaseException):
    pass


class Facts(ownership.EndpointObservations):
    """Real anchored directories/entries; simulated kernel process/socket tables."""

    def __init__(self, case):
        self.case = case
        self.rows = ()
        self.peer_pid = DAEMON.pid
        self.on_peer = None
        self.bad_argv = False

    def identity(self, pid):
        c = self.case
        if pid in c.unreadable:
            return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
        identity = c.process.identity(pid)
        return IdentityEvidence(
            pid=pid, state=IdentityState.present if identity else IdentityState.absent,
            identity=identity, uid=os.getuid(),
            parent=SUPERVISOR.pid if pid == DAEMON.pid else APP.pid,
        )

    def peer(self, directory, name):
        self.case.events.append('peer')
        if self.on_peer is not None:
            callback, self.on_peer = self.on_peer, None
            callback()
        if self.case.process.identity(DAEMON.pid) is None:
            raise ConnectionRefusedError(errno.ECONNREFUSED, 'offline')
        return self.peer_pid, os.getuid(), os.getgid()

    def unix_inodes(self, path):
        return self.rows if path == str(self.case.runtime / 'daemon.sock') else ()

    def holds_socket(self, pid, inode):
        return pid == DAEMON.pid and inode == 800

    def cmdline(self, pid):
        assert pid == DAEMON.pid
        path = '/wrong/daemon.sock' if self.bad_argv else str(self.case.runtime / 'daemon.sock')
        return b'\0'.join(os.fsencode(a) for a in ('doubletake', '-daemonize', '-socket', path, '-cred-backend', 'keyring')) + b'\0'

    def boot_id(self):
        return BOOT

    def unlink(self, name, *, dir_fd):
        self.case.hit('unlink')
        super().unlink(name, dir_fd=dir_fd)


class Process(FakeProcess):
    def __init__(self, case):
        super().__init__()
        self.case = case
        self.register(APP, {})

    async def spawn(self, spec):
        c = self.case
        c.hit('spawn')
        history = (await c.receipts.all()).value
        assert history, 'public launch spawned before its durable reservation'
        receipt = history[-1]
        assert receipt.state == 'reserved'
        c.runtime = Path(receipt.runtime_directory)
        assert c.authority.owner is not None
        assert len(spec.pass_fds) == 1
        config = json.loads(os.read(spec.pass_fds[0], 4096))
        assert config['launch_uuid'] == str(receipt.launch_uuid)
        assert config['endpoint_generation'] == receipt.endpoint_generation
        assert 'owner_token' not in config
        c.configs.append(config)
        records = (await c.store.all()).value
        record = next(r for r in records if r.app == APP)
        self.spawned.append(spec)
        env = self.owner_environment(receipt.launch_uuid, record.runtime_directory)
        self.register(SUPERVISOR, env)
        self.register(DAEMON, env)
        c.record = replace(record, supervisor=SUPERVISOR, daemon=DAEMON)
        await c.store.write(c.record)
        c.hit('supervisor-publication')
        if c.bind_on_spawn:
            c.bind()
        return Ok(value=SUPERVISOR)

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline, **kwargs):
        c = self.case
        receipts = (await c.receipts.all()).value
        intent = await c.supervisor_intents(str(c.runtime)).read()
        assert any(r.state == 'stopping' for r in receipts) or (
            isinstance(intent, Ok) and intent.value is not None
        ), 'signal preceded durable pre-signal intent'
        assert kwargs.get('strict') is True
        c.hit('signal')
        result = await super().terminate(identity, owner_uuid, runtime_directory, deadline, **kwargs)
        if isinstance(result, Ok) and self.identity(identity.pid) is None:
            c.facts.rows = ()
            # The real supervisor observes its child's exit and exits itself.
            self.exit(SUPERVISOR)
        c.hit('exit-proof')
        return result


class Control:
    def __init__(self, case):
        self.case = case

    async def handshake(self, *args):
        raise AssertionError('public lifecycle attempted a fixture handshake')

    async def request(self, operation, target, payload, context):
        assert operation is BackendOperation.status
        c = self.case
        c.hit('status-poll')
        return Ok(value=StatusReply(
            state=c.status, device=None, target=None, has_audio=False,
            audio_muted=False, needs_credential=False, credential_kind=None, streams=(),
        ))

    async def close(self):
        pass


class OwnedCase:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.runtime = tmp_path / 'r'
        self.runtime.mkdir(mode=0o700, exist_ok=True)
        self.events = []
        self.configs = []
        self.hooks = {}
        self.unreadable = set()
        self.sockets = []
        self.bind_on_spawn = True
        self.status = 'idle'
        self.record = None
        self.process = Process(self)
        self.facts = Facts(self)
        monkeypatch.setattr(ownership, '_observations', self.facts)
        self.reopen()

    def hit(self, boundary):
        self.events.append(boundary)
        action = self.hooks.pop(boundary, None)
        if action is not None:
            action()

    def bind(self):
        self.hit('bind')
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(self.runtime / 'daemon.sock'))
        self.sockets.append(endpoint)
        self.facts.rows = (800,)

    def replace_entry(self):
        (self.runtime / 'daemon.sock').unlink()
        self.bind()

    def source(self):
        from airplay_linux.storage.codecs import decode_launch_receipt
        try:
            files = AtomicFiles(self.root)
            receipts = tuple(decode_launch_receipt(files.read('daemon/receipts/' + name))
                             for name in files.names('daemon/receipts'))
            return Ok(value=receipts)
        except Exception:
            return Err(error=diagnostic(ErrorCode.persistence_failed))

    def supervisor_intents(self, runtime):
        return JsonSupervisorReleaseIntentStore(AtomicFiles(Path(runtime)))

    def reopen(self):
        files = AtomicFiles(self.root)
        self.store = JsonDaemonStore(files)
        self.receipts = JsonLaunchReceiptStore(files)
        self.cleanup = JsonDaemonCleanupIntentStore(files)
        self.authority = SharedRootAuthority(files, inspect_process=self.facts.identity, boot_id=lambda: BOOT)
        self.launcher = ProductionLaunchCapability(FEATURES, self.runtime, production_graph=True)
        self.ownership = ownership.EvidenceAuthority(
            receipts=self.source, process=self.process, evidence_factory=TerminationEvidence,
            observations=self.facts, features=FEATURES,
        )
        write = self.receipts.write

        async def observed_write(receipt):
            if receipt.state in {'failed', 'stopped'}:
                self.hit('terminal-write')
            boundary = {'reserved': 'reservation-write', 'bound': 'receipt-write',
                        'stopping': 'stopping-write', 'ready': 'ready-write'}.get(receipt.state)
            if boundary:
                self.hit(boundary)
            result = await write(receipt)
            self.events.append('receipt:' + receipt.state)
            if isinstance(result, Ok) and receipt.state in {'failed', 'stopped'}:
                self.hit('after-terminal-write')
            if receipt.state == 'detached':
                self.hit('after-detached-write')
            if receipt.state == 'ready':
                self.hit('after-ready-write')
            return result

        self.receipts.write = observed_write
        cleanup_write = self.cleanup.write

        async def observed_cleanup(intent):
            boundary = 'cleanup-intent-publication' if intent.completed_at is None else 'cleanup-intent-completion'
            self.hit(boundary)
            result = await cleanup_write(intent)
            if isinstance(result, Ok):
                self.hit('after-' + boundary)
            return result

        self.cleanup.write = observed_cleanup
        self.registry = DaemonRegistry(
            process=self.process, store=self.store, features=FEATURES,
            control_factory=lambda r: Control(self), runtime_directory=self.runtime,
            record_root=self.root, app=APP, app_instance_uuid=APP_ID,
            ids=SequenceIds(seed=1000), inspect_process=self.facts.identity,
            scan_owned=self.scan, receipts=self.receipts, cleanup_intents=self.cleanup,
            supervisor_intents=self.supervisor_intents, authority=self.authority,
            launcher=self.launcher, ownership=self.ownership, startup_timeout=0.06,
        )
        self.notices = []
        self.registry.subscribe(self.notices.append)

    def scan(self, owner, runtime):
        return OwnedProcessScan(matches=tuple(
            p for pid in self.process.environments()
            if (p := self.process.identity(pid)) is not None and self.process.owned(p, owner, runtime)
        ), complete=True)

    async def launch(self):
        # Exercise the dormant lifecycle under the real local/shared locks.
        # Task 9 owns selecting it from public admission.
        async with self.registry._operation() as acquired:
            assert isinstance(acquired, Ok)
            return await self.registry._launch()

    async def current(self):
        return (await self.receipts.all()).value[-1]

    async def stop(self):
        async with self.registry._operation() as acquired:
            assert isinstance(acquired, Ok)
            return await self.registry._stop(self.record)

    def absence(self):
        self.process.exit(DAEMON)
        self.process.exit(SUPERVISOR)
        self.facts.rows = ()

    def close(self):
        for endpoint in self.sockets:
            endpoint.close()


@pytest.fixture
def candidate_failure_cases(tmp_path, monkeypatch):
    # External process and bus entrypoints must be intercepted before scenarios.
    from airplay_linux.process.linux import LinuxProcessRunner
    from dbus_next.aio import MessageBus

    async def forbidden(*args, **kwargs):
        raise AssertionError('offline boundary attempted')

    monkeypatch.setattr(LinuxProcessRunner, 'run', forbidden)
    monkeypatch.setattr(LinuxProcessRunner, 'spawn', forbidden)
    monkeypatch.setattr(MessageBus, 'connect', forbidden)
    c = OwnedCase(tmp_path, monkeypatch)
    yield c
    c.close()


async def test_pre_bind_verification_failure_observes_exact_child_death(candidate_failure_cases):
    c = candidate_failure_cases
    c.bind_on_spawn = False
    result = await c.launch()
    assert isinstance(result, Err)
    assert c.process.terminated == [DAEMON]
    assert c.process.identity(DAEMON.pid) is None
    assert c.process.strict_terminations[0].stage == 'pre_bind'
    assert (await c.current()).state == 'failed'
    assert c.events.index('receipt:stopping') < c.events.index('signal')
    assert not any(n.kind == 'ready' for n in c.notices)


async def test_pre_bind_unreadable_identity_retains_without_signals_or_replacement(candidate_failure_cases):
    c = candidate_failure_cases
    c.bind_on_spawn = False
    c.hooks['supervisor-publication'] = lambda: c.unreadable.add(DAEMON.pid)
    result = await c.launch()
    assert isinstance(result, Err)
    assert c.process.terminated == []
    assert c.process.identity(DAEMON.pid) == DAEMON
    assert (await c.current()).state == 'failed'
    assert isinstance(await c.registry.reconcile_previous(), Err)
    assert len(c.process.spawned) == 1
    assert c.notices[-1].kind == 'cleanup_required'


async def test_bound_mismatch_refuses_and_retains_cleanup_obligation(candidate_failure_cases):
    c = candidate_failure_cases
    assert isinstance(await c.launch(), Ok)
    c.facts.peer_pid = 999
    result = await c.stop()
    assert isinstance(result, Err)
    assert not c.process.terminated
    assert (c.runtime / 'daemon.sock').exists()
    assert isinstance(await c.registry.reconcile_previous(), Err)
    assert len(c.process.spawned) == 1


@pytest.fixture
async def reopen_receipt_store(candidate_failure_cases):
    c = candidate_failure_cases
    c.bind_on_spawn = False
    c.hooks['supervisor-publication'] = lambda: c.unreadable.add(DAEMON.pid)
    assert isinstance(await c.launch(), Err)
    c.unreadable.clear()
    c.reopen()
    receipt = await c.current()
    assert receipt.state == 'failed'
    c.failed_path = c.root / 'daemon' / 'receipts' / (str(receipt.launch_uuid) + '.json')

    async def drive(action):
        c.reopen()
        if action == 'publish_intent':
            # Reconciliation publishes a real outcome intent; no direct byte edits.
            assert isinstance(await c.registry.reconcile_previous(), Err)
        elif action == 'complete_intent':
            c.absence()
            assert await c.registry.reconcile_previous() == Ok(value=None)
        elif action == 'retry_cleanup':
            assert await c.registry.shutdown() == Ok(value=None)
        elif action == 'reopen_registry':
            assert await c.registry.reconcile_previous() == Ok(value=None)
        else:
            raise AssertionError(action)

    c.drive = drive
    return c


async def test_failed_receipt_survives_cleanup_retry_and_restart(reopen_receipt_store):
    c = reopen_receipt_store
    before = c.failed_path.read_bytes()
    for action in ('publish_intent', 'complete_intent', 'retry_cleanup', 'reopen_registry'):
        await c.drive(action)
        assert c.failed_path.read_bytes() == before
    intents = (await c.cleanup.all()).value
    assert len(intents) == 1 and intents[0].outcome == 'stopped'


async def test_entry_replacement_during_first_bind_never_becomes_ready(candidate_failure_cases):
    c = candidate_failure_cases
    c.facts.on_peer = c.replace_entry
    assert isinstance(await c.launch(), Err)
    assert not any(n.kind == 'ready' for n in c.notices)
    assert 'receipt:bound' not in c.events
    assert not c.process.terminated
    assert (c.runtime / 'daemon.sock').exists()


@pytest.mark.parametrize('origin', ['reserved', 'detached', 'bound', 'ready', 'failed'])
async def test_stopping_from_reserved_and_failed_persists_intent_before_signal(candidate_failure_cases, origin):
    c = candidate_failure_cases
    if origin == 'failed':
        c.unreadable.add(DAEMON.pid)
        assert isinstance(await c.launch(), Err)
        before = (c.root / 'daemon/receipts' / (str((await c.current()).launch_uuid) + '.json')).read_bytes()
        c.unreadable.clear()
        assert isinstance(await c.registry.reconcile_previous(), Err)
        assert c.process.terminated == []
        assert (c.root / 'daemon/receipts' / (str((await c.current()).launch_uuid) + '.json')).read_bytes() == before
        return
    boundary = {'reserved': 'supervisor-publication', 'detached': 'receipt-write',
                'bound': 'status-poll', 'ready': None}[origin]
    if boundary is not None:
        c.hooks[boundary] = lambda: (_ for _ in ()).throw(Crash())
        with pytest.raises(Crash):
            await c.launch()
        c.record = (await c.store.all()).value[0]
    else:
        assert isinstance(await c.launch(), Ok)
    assert (await c.current()).state == origin
    assert await c.stop() == Ok(value=None)
    assert c.events.index('receipt:stopping') < c.events.index('signal')
    assert c.process.identity(DAEMON.pid) is None
    assert (await c.current()).state == 'stopped'


@pytest.mark.parametrize('boundary', [
    'reservation-write', 'spawn', 'supervisor-publication', 'bind', 'receipt-write',
    'status-poll', 'stopping-write', 'signal', 'exit-proof', 'unlink',
    'after-ready-write', 'cleanup-intent-publication', 'cleanup-intent-completion',
])
async def test_each_actual_crash_boundary_retains_safe_outcome(candidate_failure_cases, boundary):
    c = candidate_failure_cases
    if boundary in {'cleanup-intent-publication', 'cleanup-intent-completion'}:
        c.status = 'streaming'
    if boundary in {'stopping-write', 'signal', 'exit-proof', 'unlink'}:
        assert isinstance(await c.launch(), Ok)
        c.notices.clear()
        operation = c.stop
    else:
        operation = c.launch
    c.hooks[boundary] = lambda: (_ for _ in ()).throw(Crash())
    with pytest.raises(Crash):
        await operation()
    assert boundary in c.events
    assert not c.registry._authority_lease
    assert not any(n.kind == 'ready' for n in c.notices)
    c.reopen()
    result = await c.registry.reconcile_previous()
    assert len(c.process.spawned) <= 1
    if isinstance(result, Err):
        assert c.notices[-1].kind == 'cleanup_required'
    else:
        assert c.process.identity(DAEMON.pid) is None
    assert not any(n.kind == 'ready' for n in c.notices)


@pytest.mark.parametrize('boundary', ['after-ready-write', 'running-write'])
@pytest.mark.parametrize('failure', ['death', 'supervisor-death', 'entry-replacement'])
async def test_death_during_ready_write_never_emits_ready(candidate_failure_cases, boundary, failure):
    c = candidate_failure_cases
    write = c.store.write

    async def observe(record):
        result = await write(record)
        if record.lifecycle == 'running':
            c.hit('running-write')
        return result

    c.store.write = observe
    c.hooks[boundary] = {
        'death': lambda: c.process.exit(DAEMON),
        'supervisor-death': lambda: c.process.exit(SUPERVISOR),
        'entry-replacement': c.replace_entry,
    }[failure]
    assert isinstance(await c.launch(), Err)
    assert not any(n.kind == 'ready' for n in c.notices)
    assert (await c.current()).state == 'failed'


async def test_isolated_launcher_is_unreachable_from_guarded_production(candidate_failure_cases, tmp_path, monkeypatch):
    c = candidate_failure_cases
    assert c.launcher.argv(str(c.runtime / 'daemon.sock')) == (
        'doubletake', '-daemonize', '-socket', str(c.runtime / 'daemon.sock'), '-cred-backend', 'keyring',
    )
    assert isinstance(await c.launch(), Ok)
    assert (await c.current()).state == 'ready'
    assert [n.kind for n in c.notices] == ['ready']
    # Invoke the real build_runtime proof, including its independently checked
    # bus/process tripwires and final run/spawn recorder.
    from test_no_production_spawn import offline_boundaries, test_phase_guard_blocks_eligible_production_launch
    tripwire = offline_boundaries.__wrapped__(monkeypatch)
    production = tmp_path / 'production'
    production.mkdir()
    await test_phase_guard_blocks_eligible_production_launch(production, monkeypatch, tripwire)


@pytest.mark.parametrize('operation', ['reconcile', 'launch'])
@pytest.mark.parametrize('channel', [
    'empty', 'socket', 'malformed-supervisor', 'orphan-supervisor',
    'orphan-cleanup', 'completed-orphan-cleanup', 'malformed-cleanup',
])
async def test_all_runtime_channels_are_accounted_before_recovery_or_launch(candidate_failure_cases, channel, operation):
    c = candidate_failure_cases
    path = None
    frozen = None
    if channel == 'socket':
        c.bind()
        path = c.runtime / 'daemon.sock'
    elif channel == 'malformed-supervisor':
        path = c.runtime / 'release-intent.json'
        path.write_bytes(b'{')
    elif channel == 'orphan-supervisor':
        intent = SupervisorReleaseIntent(
            schema_version=1, launch_uuid=UUID(int=700), instance_uuid=APP_ID,
            app=APP, supervisor=SUPERVISOR, daemon=DAEMON,
            reason='application-exited', intent_at=1.0,
        )
        assert await c.supervisor_intents(str(c.runtime)).write(intent) == Ok(value=None)
        path = c.runtime / 'release-intent.json'
    elif channel in {'orphan-cleanup', 'completed-orphan-cleanup'}:
        intent = DaemonCleanupIntent(
            launch_uuid=UUID(int=700), reason=ErrorCode.daemon_failed, intent_at=1.0,
            completed_at=None, outcome=None,
        )
        assert await c.cleanup.write(intent) == Ok(value=None)
        if channel == 'completed-orphan-cleanup':
            intent = replace(intent, completed_at=2.0, outcome='stopped')
            assert await c.cleanup.write(intent) == Ok(value=None)
        path = c.root / 'daemon/cleanup-intents' / (str(intent.launch_uuid) + '.json')
    elif channel == 'malformed-cleanup':
        path = c.root / 'daemon/cleanup-intents' / (str(UUID(int=700)) + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{')
    if path is not None and channel != 'socket':
        frozen = path.read_bytes()
    socket_entry = path.stat().st_ino if channel == 'socket' else None
    c.reopen()
    result = await (c.launch() if operation == 'launch' else c.registry.reconcile_previous())
    if channel == 'empty':
        assert isinstance(result, Ok)
        assert len(c.process.spawned) == (operation == 'launch')
    else:
        assert isinstance(result, Err)
        expected = (ErrorCode.persistence_failed if channel.startswith('malformed') else
                    ErrorCode.stale_socket if channel == 'socket' else ErrorCode.ownership_mismatch)
        assert result.error.code is expected
        assert c.notices[-1].kind == 'cleanup_required'
        assert not c.process.spawned and not c.process.terminated
        assert not (await c.receipts.all()).value
        if frozen is not None:
            assert path.read_bytes() == frozen
        else:
            assert path.stat().st_ino == socket_entry


@pytest.mark.parametrize('operation', ['reconcile', 'launch'])
@pytest.mark.parametrize('channel', ['socket', 'orphan-supervisor', 'malformed-supervisor'])
@pytest.mark.parametrize('depth', [1, 2])
async def test_orphaned_child_runtime_channels_block_admission(candidate_failure_cases, channel, depth, operation):
    c = candidate_failure_cases
    orphan = c.runtime
    for level in range(depth):
        orphan = orphan / (UUID(int=700).hex if level == 0 else 'child')
        orphan.mkdir(mode=0o700)
    path = orphan / ('daemon.sock' if channel == 'socket' else 'release-intent.json')
    if channel == 'socket':
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(path))
        c.sockets.append(endpoint)
        frozen = path.stat().st_ino
    else:
        if channel == 'orphan-supervisor':
            intent = SupervisorReleaseIntent(
                schema_version=1, launch_uuid=UUID(int=700), instance_uuid=APP_ID,
                app=APP, supervisor=SUPERVISOR, daemon=DAEMON,
                reason='application-exited', intent_at=1.0,
            )
            assert await c.supervisor_intents(str(orphan)).write(intent) == Ok(value=None)
        else:
            path.write_bytes(b'{')
        frozen = path.read_bytes()
    c.reopen()
    result = await (c.launch() if operation == 'launch' else c.registry.reconcile_previous())
    assert isinstance(result, Err)
    assert result.error.code is (ErrorCode.stale_socket if channel == 'socket' else
                                 ErrorCode.persistence_failed if channel == 'malformed-supervisor' else
                                 ErrorCode.ownership_mismatch)
    assert c.notices[-1].kind == 'cleanup_required'
    assert not c.process.spawned and not c.process.terminated
    assert not (await c.receipts.all()).value
    assert (path.stat().st_ino if channel == 'socket' else path.read_bytes()) == frozen


@pytest.mark.parametrize('unaccounted', ['orphan-cleanup', 'malformed-supervisor', 'socket'])
async def test_unaccounted_channel_blocks_cleanup_of_another_known_endpoint(candidate_failure_cases, unaccounted):
    c = candidate_failure_cases
    assert isinstance(await c.launch(), Ok)
    receipt = await c.current()
    receipt_path = c.root / 'daemon/receipts' / (str(receipt.launch_uuid) + '.json')
    before = receipt_path.read_bytes()
    other = c.root / 'other'
    other.mkdir(mode=0o700)
    # The configured endpoint is relevant even when all receipts name another one.
    c.registry.runtime_directory = other
    if unaccounted == 'malformed-supervisor':
        (other / 'release-intent.json').write_bytes(b'{')
    elif unaccounted == 'socket':
        endpoint = socket.socket(socket.AF_UNIX)
        endpoint.bind(str(other / 'daemon.sock'))
        c.sockets.append(endpoint)
    else:
        assert await c.cleanup.write(DaemonCleanupIntent(
            launch_uuid=UUID(int=700), reason=ErrorCode.daemon_failed, intent_at=1.0,
            completed_at=None, outcome=None,
        )) == Ok(value=None)
    assert isinstance(await c.registry.reconcile_previous(), Err)
    assert not c.process.terminated
    assert receipt_path.read_bytes() == before
    assert c.process.identity(DAEMON.pid) == DAEMON
