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
* a second application identity cannot adopt a live endpoint;
* a killed application owner leaves no adoptable running record: the supervised
  children are bounded-cleaned and a fresh application starts its own instance;
* a publication-store crash while starting is a typed failure that never claims
  readiness, and recovery stops the exact live children before relaunching.

Manual commands: ``pytest -q -m "not live_backend and not live_kde and not
live_hardware" tests/adapter_contracts/test_transport_contract.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic

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
from airplay_linux.domain.records import DaemonRecord
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
    state.mkdir(mode=0o700, exist_ok=True)
    runtime.mkdir(mode=0o700, exist_ok=True)
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


# ---------------------------------------------------------------------------
# Application-owner death: live streams are cleaned by the supervisor, and the
# surviving record is never adoptable as a running endpoint.
# ---------------------------------------------------------------------------

_REPOSITORY = Path(__file__).resolve().parents[2]

#: A *private child application*: the same production registry/transport stack,
#: started as its own process so the test can kill the owner while its daemon
#: still has a live stream.  It prints one ready line and then waits.
_CHILD_APPLICATION = r'''
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from airplay_linux.backend.control import (
    ApplicationContractProfile,
    PublishedDoubletakeControl,
    UnsupportedControl,
)
from airplay_linux.domain.resources import AudioPolicy, Ok
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    IdentityState,
    OwnedProcessScan,
    owner_evidence,
    read_identity_evidence,
)
from airplay_linux.storage import AtomicFiles, JsonDaemonStore, JsonReceiverStore
from airplay_linux.testing.builders import context, physical, target
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore


class Clock:
    now = staticmethod(time.monotonic)


class Scheduler:
    def call_at(self, when, callback):
        return asyncio.get_running_loop().call_at(when, callback)


class Ids:
    new = staticmethod(uuid4)


class Capture:
    async def verify(self, source):
        return Ok(value=None)

    async def list_physical(self):
        return Ok(value=())

    async def select(self, connector):
        from airplay_linux.domain.resources import PhysicalMonitorSource
        return Ok(value=PhysicalMonitorSource(
            connector=connector, portal_restore_token=None,
        ))

    async def release(self, source):
        return Ok(value=None)


def confined(runner, owner_uuid, runtime_directory, proc_root=Path("/proc")):
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
        if owner_evidence(evidence.identity, owner_uuid, runtime_directory, proc_root):
            matches.append(evidence.identity)
    return OwnedProcessScan(
        matches=tuple(sorted(matches, key=lambda item: (item.pid, item.start_ticks))),
        complete=True,
    )


async def main():
    binary_directory, state, runtime = (
        sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]),
    )
    os.environ["PATH"] = binary_directory + os.pathsep + os.environ.get("PATH", "")
    runner = LinuxProcessRunner()
    files = AtomicFiles(state)
    profile = ApplicationContractProfile(socket_path=str(runtime / "daemon.sock"))
    features = profile.features

    def factory(record):
        return PublishedDoubletakeControl(runner, ApplicationContractProfile(
            socket_path=record.socket_path,
            owner_token=record.owner_token,
            instance_uuid=record.instance_uuid,
        ))

    registry = DaemonRegistry(
        process=runner,
        store=JsonDaemonStore(files),
        control_factory=factory,
        features=features,
        runtime_directory=runtime,
        record_root=state,
        startup_timeout=10.0,
        scan_owned=lambda owner, where: confined(runner, owner, where),
    )
    transport = DoubleTakeTransport(
        factory,
        registry,
        Capture(),
        BackendKeyringStore(UnsupportedControl(), features),
        JsonReceiverStore(files),
        TrustFenceRegistry(),
        features,
        Clock(),
        Scheduler(),
        Ids(),
    )
    receiver = target("a")
    transport.update_target(receiver, receiver.receiver_id)
    started = await transport.start_stream(
        receiver, physical("DP-1"), AudioPolicy(enabled=False),
        context(generation=1, lease=1),
    )
    if not isinstance(started, Ok):
        print(json.dumps({"ready": False, "error": started.error.code.value}), flush=True)
        return 2
    record = await registry.ensure()
    if not isinstance(record, Ok):
        print(json.dumps({"ready": False, "error": record.error.code.value}), flush=True)
        return 3
    print(json.dumps({
        "ready": True,
        "app_pid": os.getpid(),
        "instance_uuid": str(record.value.instance_uuid),
        "socket_path": record.value.socket_path,
        "supervisor": [record.value.supervisor.pid, record.value.supervisor.start_ticks],
        "daemon": [record.value.daemon.pid, record.value.daemon.start_ticks],
    }), flush=True)
    await asyncio.Event().wait()


raise SystemExit(asyncio.run(main()))
'''


def _child_environment(binary_directory: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "HOME")
        if name in os.environ
    }
    environment.update({
        "PATH": f"{binary_directory}{os.pathsep}{environment.get('PATH', '')}",
        "PYTHONPATH": os.pathsep.join([
            str(_REPOSITORY / "src"), str(PROCESS_FIXTURES),
        ]),
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONHASHSEED": "0",
    })
    return environment


async def _spawn_child_application(binary_directory: Path, state: Path, runtime: Path):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", _CHILD_APPLICATION,
        str(binary_directory), str(state), str(runtime),
        cwd=str(_REPOSITORY),
        env=_child_environment(binary_directory),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    line = await asyncio.wait_for(process.stdout.readline(), 30.0)
    if not line:
        stderr = await asyncio.wait_for(process.stderr.read(), 5.0)
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise AssertionError(
            f"private child application exited without readiness "
            f"(exit={process.returncode}): {stderr[-4000:]!r}"
        )
    ready = json.loads(line)
    if ready.get("ready") is not True:
        stderr = await asyncio.wait_for(process.stderr.read(), 5.0)
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise AssertionError(
            f"private child application refused: {ready} {stderr[-2000:]!r}"
        )
    return process, ready


async def _wait_for_gone(pids, timeout: float = 20.0) -> list[tuple[int, object]]:
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while time.monotonic() < deadline and remaining:
        remaining = [
            pid for pid in remaining
            if read_identity_evidence(pid).state
            not in {IdentityState.absent, IdentityState.terminated}
        ]
        if remaining:
            await asyncio.sleep(0.05)
    return [
        (pid, read_identity_evidence(pid).state) for pid in remaining
    ]


async def test_a_killed_application_owner_leaves_no_adoptable_running_endpoint(
    tmp_path, monkeypatch,
):
    """Owner death with a live stream: bounded cleanup, never adoption.

    A private child *application* really starts the production registry and
    transport, so its daemon owns a live stream.  Killing that application must
    end both supervised children through the production parent-death path, and
    the surviving durable record must not look like a running endpoint another
    application may adopt.
    """
    binary_directory = tmp_path / "bin"
    fake_doubletake.install(binary_directory)
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    journal = fake_doubletake.daemon_journal_path(runtime)

    process, ready = await _spawn_child_application(binary_directory, state, runtime)
    old_supervisor, old_daemon = ready["supervisor"][0], ready["daemon"][0]
    try:
        assert fake_doubletake.journal(journal), "the child application never started"
        connects = [
            entry for entry in fake_doubletake.journal(journal)
            if entry["process"] == "daemon" and entry.get("operation") == "connect"
        ]
        assert len(connects) == 1, "the application's daemon never got its stream"
        for pid in (old_supervisor, old_daemon):
            assert read_identity_evidence(pid).state is IdentityState.present

        # The owner dies hard with the stream still open.
        os.kill(process.pid, signal.SIGKILL)
        await asyncio.wait_for(process.wait(), 10.0)
        assert await _wait_for_gone((old_supervisor, old_daemon)) == []

        # The durable record must not claim a running endpoint for a dead app.
        stores = JsonDaemonStore(AtomicFiles(state))
        records = await stores.all()
        assert isinstance(records, Ok), records
        old_record = next(
            record for record in records.value
            if str(record.instance_uuid) == ready["instance_uuid"]
        )
        assert old_record.lifecycle != "running"
        assert old_record.app.pid == process.pid

        # Nobody compensated the dead stream by telling the dead daemon to stop.
        assert [
            entry for entry in fake_doubletake.journal(journal)
            if entry["process"] == "daemon" and entry.get("operation") == "disconnect"
        ] == []

        # A fresh application (this test process) reconciles the stale record and
        # starts its own distinct instance instead of adopting the old one.
        world = await build_world(tmp_path, monkeypatch)
        try:
            ensured = await asyncio.wait_for(world.registry.ensure(), 30.0)
            assert isinstance(ensured, Ok), ensured
            assert str(ensured.value.instance_uuid) != ready["instance_uuid"]
            assert ensured.value.daemon.pid != old_daemon
            assert ensured.value.app.pid == os.getpid()
            assert read_identity_evidence(old_daemon).state in {
                IdentityState.absent, IdentityState.terminated,
            }
            starts = [
                entry for entry in fake_doubletake.journal(journal)
                if entry["process"] == "daemon" and entry.get("outcome") == "start"
            ]
            assert len(starts) == 2
        finally:
            await _close(world)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        fake_doubletake.reap_private(str(tmp_path))


# ---------------------------------------------------------------------------
# Publication-store crash during start.
# ---------------------------------------------------------------------------


class _FailingDaemonStore:
    """A controlled publication failure at one exact lifecycle transition."""

    def __init__(self, inner, *, fail_on: str):
        self._inner = inner
        self._fail_on = fail_on
        self.failures: list[str] = []

    async def write(self, record: DaemonRecord):
        if record.lifecycle == self._fail_on:
            self.failures.append(record.lifecycle)
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        return await self._inner.write(record)

    async def all(self):
        return await self._inner.all()

    async def delete(self, instance_uuid):
        return await self._inner.delete(instance_uuid)


async def test_a_publication_store_crash_during_start_never_claims_readiness(
    tmp_path, monkeypatch,
):
    """The running-record publication fails after the children are already live.

    The registry may not report readiness, and the durable evidence must still say
    ``starting``.  A fresh registry (the restarted application) then has to stop
    the exact live children before it may launch anything else.
    """
    world = await build_world(tmp_path, monkeypatch)
    state = tmp_path / "state"
    failing = _FailingDaemonStore(JsonDaemonStore(AtomicFiles(state)), fail_on="running")
    profile = ApplicationContractProfile(socket_path=str(world.runtime / "daemon.sock"))
    crashing = DaemonRegistry(
        process=world.runner,
        store=failing,
        control_factory=_control_factory(world.runner),
        features=profile.features,
        runtime_directory=world.runtime,
        record_root=state,
        startup_timeout=10.0,
        scan_owned=lambda owner, where: _confined_scan(world.runner, owner, where),
    )
    notices = []
    crashing.subscribe(notices.append)
    launched: list[int] = []
    try:
        crashed = await asyncio.wait_for(crashing.ensure(), 30.0)
        assert isinstance(crashed, Err), crashed
        assert crashed.error.code is ErrorCode.persistence_failed
        assert failing.failures == ["running"]
        # No readiness notice, only the explicit cleanup-required failure.
        assert "ready" not in [notice.kind for notice in notices]
        assert "cleanup_required" in [notice.kind for notice in notices]

        # The children are genuinely live at the instant publication failed.
        records = await failing.all()
        assert isinstance(records, Ok), records
        assert [record.lifecycle for record in records.value] == ["starting"]
        starting = records.value[0]
        for identity in (starting.supervisor, starting.daemon):
            assert identity is not None
            assert read_identity_evidence(identity.pid).state is IdentityState.present
            launched.append(identity.pid)
        assert Path(starting.socket_path).exists()

        # A fresh application must reconcile those exact children, then launch.
        recovered = await asyncio.wait_for(world.registry.ensure(), 30.0)
        assert isinstance(recovered, Ok), recovered
        assert recovered.value.instance_uuid != starting.instance_uuid
        assert await _wait_for_gone(tuple(launched)) == []
        assert read_identity_evidence(recovered.value.daemon.pid).state is IdentityState.present
        remaining = await world.registry._store.all()
        assert isinstance(remaining, Ok)
        assert [record.instance_uuid for record in remaining.value] == [
            recovered.value.instance_uuid,
        ]
        assert [record.lifecycle for record in remaining.value] == ["running"]
        await _close(world)
    finally:
        await crashing.shutdown()
        fake_doubletake.reap_private(str(tmp_path))
