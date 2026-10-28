"""Adversarial transport contracts over a real, privately supervised fake daemon.

Every test here runs the production adapter stack -- :class:`LinuxProcessRunner`,
:class:`DaemonRegistry`, the executable supervisor child, and
:class:`DoubleTakeTransport` -- against a private fake executable selected only
through a temporary PATH.  The fake daemon speaks the test-only newline-delimited
JSON protocol (see ``tests/fixtures/processes/fake_doubletake.py``); it is never
the installed ``doubletake``, proves no live support, and writes every request to
a private journal the assertions read.

The contracts pinned here:

* exactly one target command is dispatched per connect/stop, and a repeated stop
  sends nothing;
* a stale endpoint generation, a foreign receiver's mute reply, a stale/foreign
  reply id, and a replaced endpoint at the same address are all rejected without
  mutating the replacement;
* an unfenced target-addressed mutation is refused before any I/O at all;
* a dead daemon fences its surviving stream instead of pretending to clean it;
* a second application identity cannot adopt a live endpoint.

Manual commands: ``pytest -q -m "not live_backend and not live_kde and not
live_hardware" tests/adapter_contracts/test_transport_contract.py``.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    ConnectPayload,
    PublishedDoubletakeControl,
    UnsupportedControl,
)
from airplay_linux.backend.contract import BackendFeatures
from airplay_linux.domain.messages import TransportEventKind
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import (
    AudioPolicy,
    Err,
    ErrorCode,
    Ok,
    PhysicalMonitorSource,
)
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    IdentityState,
    OwnedProcessScan,
    read_identity_evidence,
)
from airplay_linux.storage import AtomicFiles, JsonDaemonStore, JsonReceiverStore
from airplay_linux.testing.builders import context, physical, target
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402


class _Clock:
    now = staticmethod(time.monotonic)


class _Scheduler:
    def call_at(self, when, callback):
        return asyncio.get_running_loop().call_at(when, callback)


class _Ids:
    new = staticmethod(uuid4)


class _Capture:
    """Only the connector the private environment actually advertises."""

    def __init__(self, connectors=("DP-1",)):
        self.connectors = tuple(connectors)

    async def verify(self, source):
        if isinstance(source, PhysicalMonitorSource) and source.connector in self.connectors:
            return Ok(value=None)
        return Err(error=_code(ErrorCode.unsupported_source))

    async def list_physical(self):
        return Ok(value=tuple(
            PhysicalMonitorSource(connector=connector, portal_restore_token=None)
            for connector in self.connectors
        ))

    async def select(self, connector):
        if connector not in self.connectors:
            return Err(error=_code(ErrorCode.unsupported_source))
        return Ok(value=PhysicalMonitorSource(connector=connector, portal_restore_token=None))

    async def release(self, source):
        return Ok(value=None)


def _code(code: ErrorCode):
    from airplay_linux.diagnostics import diagnostic
    return diagnostic(code)


def _control_factory(runner: LinuxProcessRunner):
    def make(record):
        return PublishedDoubletakeControl(runner, ApplicationContractProfile(
            socket_path=record.socket_path,
            owner_token=record.owner_token,
            instance_uuid=record.instance_uuid,
        ))
    return make


class _IncompleteScan:
    """An owner scan whose candidate population could not be fully inspected."""

    matches = ()
    complete = False


@dataclass
class World:
    runner: LinuxProcessRunner
    registry: DaemonRegistry
    transport: DoubleTakeTransport
    runtime: Path
    journal: Path

    def operations(self, *, process="daemon") -> list[str]:
        return [
            entry["operation"] for entry in fake_doubletake.journal(self.journal)
            if entry["process"] == process and entry.get("operation")
        ]

    def entries(self, **fields) -> list[dict]:
        return [
            entry for entry in fake_doubletake.journal(self.journal)
            if all(entry.get(name) == value for name, value in fields.items())
        ]


def _confined_scan(runner, owner_uuid, runtime_directory, proc_root=Path("/proc")):
    """The real owner-evidence scan, confined to this user's inspectable processes.

    Linux makes ``/proc/<pid>/environ`` unreadable for unrelated non-dumpable
    processes (a compositor, a session bus, other users' services), so a global
    scan reports itself *incomplete* on any normal desktop -- the production
    registry then refuses, which the ``fail_closed`` test below pins.  For the
    private functional world the candidate population is restricted to processes
    this test can inspect; ownership itself is still proved by the real runner
    against real procfs evidence, not by a fake environment table.
    """
    matches = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if (proc_root / entry).stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        evidence = read_identity_evidence(pid, proc_root)
        if evidence.state is not IdentityState.present or evidence.identity is None:
            continue
        if runner.owned(evidence.identity, owner_uuid, runtime_directory):
            matches.append(evidence.identity)
    return OwnedProcessScan(
        matches=tuple(sorted(matches, key=lambda item: (item.pid, item.start_ticks))),
        complete=True,
    )


async def build_world(tmp_path, monkeypatch, *, daemon_env=None, ctl_env=None) -> World:
    shims = fake_doubletake.install(
        tmp_path / "bin",
        env={
            "doubletake": dict(daemon_env or {}),
            "doubletake-ctl": dict(ctl_env or {}),
        },
    )
    fake_doubletake.private_path(monkeypatch, tmp_path / "bin")

    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    files = AtomicFiles(state)
    daemons, receivers = JsonDaemonStore(files), JsonReceiverStore(files)
    profile = ApplicationContractProfile(socket_path=str(runtime / "daemon.sock"))
    features: BackendFeatures = profile.features
    runner = LinuxProcessRunner()
    factory = _control_factory(runner)
    registry = DaemonRegistry(
        process=runner,
        store=daemons,
        control_factory=factory,
        features=features,
        runtime_directory=runtime,
        record_root=state,
        startup_timeout=10.0,
        scan_owned=lambda owner, where: _confined_scan(runner, owner, where),
    )
    transport = DoubleTakeTransport(
        factory,
        registry,
        _Capture(),
        BackendKeyringStore(UnsupportedControl(), features),
        receivers,
        TrustFenceRegistry(),
        features,
        _Clock(),
        _Scheduler(),
        _Ids(),
    )
    return World(
        runner=runner,
        registry=registry,
        transport=transport,
        runtime=runtime,
        journal=fake_doubletake.daemon_journal_path(runtime),
    )


async def _close(world: World) -> None:
    try:
        await asyncio.wait_for(world.transport.shutdown(), 20.0)
    except Exception:
        pass


async def test_exactly_one_owned_stream_is_connected_and_stopped(tmp_path, monkeypatch):
    world = await build_world(tmp_path, monkeypatch)
    events = []
    world.transport.subscribe(events.append)
    receiver = target("a")
    world.transport.update_target(receiver, receiver.receiver_id)
    ctx = context(generation=1, lease=1)
    try:
        started = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=True), ctx,
        )
        assert isinstance(started, Ok), started
        handle = started.value
        assert [event.kind for event in events].count(TransportEventKind.ready) == 1

        connects = world.entries(process="daemon", operation="connect")
        assert len(connects) == 1
        assert connects[0]["target"] == receiver.canonical_host
        assert connects[0]["request_id"]

        first = await world.transport.stop_stream(handle, ctx)
        assert isinstance(first, Ok), first
        disconnects = world.entries(process="daemon", operation="disconnect")
        assert [entry["target"] for entry in disconnects] == [receiver.canonical_host]
        assert world.entries(process="daemon", operation="disconnect")[0]["cleanup"] is True

        # A second stop is already released: it must not signal anything twice.
        second = await world.transport.stop_stream(handle, ctx)
        assert isinstance(second, Ok), second
        assert len(world.entries(process="daemon", operation="disconnect")) == 1
    finally:
        await _close(world)


async def test_a_stale_endpoint_generation_dispatches_nothing(tmp_path, monkeypatch):
    world = await build_world(tmp_path, monkeypatch)
    receiver = target("a")
    world.transport.update_target(receiver, receiver.receiver_id)
    try:
        result = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=False),
            context(generation=1, endpoint_generation=2, lease=1),
        )
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_target
        assert world.entries(process="daemon", operation="connect") == []
    finally:
        await _close(world)


async def test_unfenced_target_mutation_is_refused_before_any_io(tmp_path, monkeypatch):
    """The real control fails closed on a missing dispatch fence, without I/O."""
    world = await build_world(tmp_path, monkeypatch)
    receiver = target("a")
    ctx = context(generation=1, lease=1)
    try:
        record = await world.registry.ensure()
        assert isinstance(record, Ok), record
        control = PublishedDoubletakeControl(world.runner, ApplicationContractProfile(
            socket_path=record.value.socket_path,
            owner_token=record.value.owner_token,
            instance_uuid=record.value.instance_uuid,
        ))
        proof = await control.handshake(record.value.owner_token, record.value.instance_uuid)
        assert isinstance(proof, Ok), proof
        before = len(fake_doubletake.journal(world.journal))

        refused = await control.request(
            BackendOperation.connect, receiver, ConnectPayload(), ctx,
        )
        assert isinstance(refused, Err)
        assert refused.error.code is ErrorCode.dependency_incompatible
        # No ownership probe ran either: the journal did not grow at all.
        assert len(fake_doubletake.journal(world.journal)) == before
        await control.close()
    finally:
        await _close(world)


async def test_a_reply_for_another_request_is_never_accepted(tmp_path, monkeypatch):
    """A foreign/stale reply id must not be adopted as the connect answer."""
    world = await build_world(
        tmp_path, monkeypatch,
        daemon_env={
            "AIRPLAY_FAKE_DAEMON_STALE_REPLY": "1",
            "AIRPLAY_FAKE_DAEMON_STALE_REPLY_OP": "connect",
        },
    )
    events = []
    world.transport.subscribe(events.append)
    receiver = target("a")
    world.transport.update_target(receiver, receiver.receiver_id)
    ctx = context(generation=1, lease=1)
    try:
        result = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=True), ctx,
        )
        assert isinstance(result, Err), result
        assert TransportEventKind.ready not in [event.kind for event in events]
        # The daemon did receive the connect (a side effect happened), so the
        # adapter must compensate with exactly one cleanup command.
        assert len(world.entries(process="daemon", operation="connect")) == 1
        assert [entry["target"] for entry in world.entries(
            process="daemon", operation="disconnect",
        )] == [receiver.canonical_host]
        ctl = world.entries(process="ctl")
        assert [entry["outcome"] for entry in ctl].count("uncorrelated") == 1
    finally:
        await _close(world)


async def test_a_foreign_receivers_mute_reply_never_confirms_this_target(
    tmp_path, monkeypatch,
):
    world = await build_world(
        tmp_path, monkeypatch,
        daemon_env={"AIRPLAY_FAKE_DAEMON_FOREIGN_MUTE_TARGET": "192.0.2.99"},
    )
    receiver = target("a")
    world.transport.update_target(receiver, receiver.receiver_id)
    ctx = context(generation=1, lease=1)
    try:
        started = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=True), ctx,
        )
        assert isinstance(started, Ok), started
        handle = started.value
        assert isinstance(await world.transport.start_audio(handle, ctx), Ok)
        muted = await world.transport.set_muted(handle, True, ctx)
        assert isinstance(muted, Err)
        assert muted.error.code is ErrorCode.audio_mute_failed
        # The command did reach the daemon for this exact target; only its
        # foreign status projection was refused, so no mute is claimed.
        mute_calls = world.entries(process="daemon", operation="mute")
        assert [entry["target"] for entry in mute_calls] == [receiver.canonical_host]
        await world.transport.stop_stream(handle, ctx)
    finally:
        await _close(world)


async def test_a_replaced_endpoint_at_the_same_address_is_never_told_to_stop(
    tmp_path, monkeypatch,
):
    world = await build_world(tmp_path, monkeypatch)
    old = target("a", endpoint_generation=1)
    new = target("a", endpoint_generation=2)
    world.transport.update_target(old, old.receiver_id)
    ctx = context(generation=1, lease=1)
    try:
        started = await world.transport.start_stream(
            old, physical("DP-1"), AudioPolicy(enabled=False), ctx,
        )
        assert isinstance(started, Ok), started
        handle = started.value

        world.transport.update_target(new, new.receiver_id)
        stopped = await world.transport.stop_stream(handle, ctx)
        assert isinstance(stopped, Err)
        assert stopped.error.code is ErrorCode.cleanup_timeout
        # Address ownership changed: the replacement must not receive the old
        # stream's disconnect.
        assert world.entries(process="daemon", operation="disconnect") == []
    finally:
        await _close(world)


async def test_a_dead_daemon_fences_the_surviving_stream(tmp_path, monkeypatch):
    world = await build_world(tmp_path, monkeypatch)
    events = []
    world.transport.subscribe(events.append)
    receiver = target("a")
    world.transport.update_target(receiver, receiver.receiver_id)
    ctx = context(generation=1, lease=1)
    try:
        started = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=False), ctx,
        )
        assert isinstance(started, Ok), started
        handle = started.value
        assert [event.kind for event in events].count(TransportEventKind.ready) == 1

        record = await world.registry.ensure()
        assert isinstance(record, Ok), record
        daemon_identity = record.value.daemon
        os.kill(daemon_identity.pid, signal.SIGKILL)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if TransportEventKind.daemon_exit in [event.kind for event in events]:
                break
            await asyncio.sleep(0.02)
        assert TransportEventKind.daemon_exit in [event.kind for event in events]

        survivors = await world.transport.stop_stream(handle, ctx)
        assert isinstance(survivors, Err)
        assert survivors.error.code is ErrorCode.cleanup_timeout
        # Nothing was sent to the dead daemon for the retired stream.
        assert world.entries(process="daemon", operation="disconnect") == []

        # The registry admits one replacement; the new daemon is a new instance.
        restarted = await world.transport.start_stream(
            receiver, physical("DP-1"), AudioPolicy(enabled=False), ctx,
        )
        assert isinstance(restarted, Ok), restarted
        replacement = await world.registry.ensure()
        assert isinstance(replacement, Ok), replacement
        assert replacement.value.instance_uuid != record.value.instance_uuid
        assert replacement.value.daemon.pid != daemon_identity.pid
        await world.transport.stop_stream(restarted.value, ctx)
    finally:
        await _close(world)


async def test_an_incomplete_owner_scan_never_adopts_a_daemon(tmp_path, monkeypatch):
    """Doubt about *any* candidate blocks adoption instead of guessing.

    The default production scan reads every ``/proc`` entry.  Linux makes
    ``/proc/<pid>/environ`` unreadable for unrelated non-dumpable processes (a
    compositor, the session bus, another user's services), so a real desktop can
    legitimately produce an incomplete scan.  This test pins the conservative
    contract for that condition: the registry refuses rather than adopting an
    endpoint whose ownership it could not fully verify.
    """
    world = await build_world(tmp_path, monkeypatch)
    world.registry._scan = lambda owner, runtime: _IncompleteScan()
    try:
        result = await world.registry.ensure()
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.ownership_mismatch
        records = await world.registry._store.all()
        assert isinstance(records, Ok)
        assert [record.lifecycle for record in records.value] != ["running"]
        assert world.entries(process="ctl") == []
    finally:
        await _close(world)
        fake_doubletake.reap_private(str(tmp_path))


async def test_a_second_application_identity_cannot_adopt_a_live_endpoint(
    tmp_path, monkeypatch,
):
    world = await build_world(tmp_path, monkeypatch)
    try:
        record = await world.registry.ensure()
        assert isinstance(record, Ok), record
        live = record.value
        before = len(fake_doubletake.journal(world.journal))

        # Forge a record that describes this exact endpoint as belonging to a
        # different application instance.  It lives in a separate store root so
        # the first registry is untouched.
        foreign_root = tmp_path / "foreign-state"
        foreign_root.mkdir(mode=0o700)
        foreign_store = JsonDaemonStore(AtomicFiles(foreign_root))
        forged = replace(
            live,
            app=ProcessIdentity(pid=os.getpid(), start_ticks=live.app.start_ticks + 1),
        )
        written = await foreign_store.write(forged)
        assert isinstance(written, Ok), written

        second = DaemonRegistry(
            process=world.runner,
            store=foreign_store,
            control_factory=_control_factory(world.runner),
            features=ApplicationContractProfile(
                socket_path=str(world.runtime / "daemon.sock"),
            ).features,
            runtime_directory=world.runtime,
            record_root=foreign_root,
            startup_timeout=3.0,
            scan_owned=lambda owner, where: _confined_scan(
                world.runner, owner, where,
            ),
        )
        adopted = await second.ensure()
        assert isinstance(adopted, Err)
        assert adopted.error.code is ErrorCode.ownership_mismatch
        # The live endpoint was neither asked to shut down nor signalled.
        assert len(fake_doubletake.journal(world.journal)) == before
        assert read_identity_evidence(live.daemon.pid).state is IdentityState.present
        assert read_identity_evidence(live.supervisor.pid).state is IdentityState.present
        await second.shutdown()
    finally:
        await _close(world)
