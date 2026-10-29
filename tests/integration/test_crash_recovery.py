"""Crash evidence is read from real JSON by a fresh assembled runtime.

The process boundary models KScreen and helpers in memory. DisplayCreator,
DisplayRecovery, DisplayRemover, AtomicFiles and all session coordinators remain
production implementations. No desktop commands or real helpers are executed.
"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kde import KdeVirtualDisplayProvider
from airplay_linux.display.kscreen import KScreenObserver
from airplay_linux.display.locking import CreationLock
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.display.removal import DisplayRemover
from airplay_linux.domain.messages import ConfirmOrphanRemoval, ConfirmSwitch, Retry, RetryCleanup, Stop
from airplay_linux.domain.operations import InstanceId, OperationId, OwnerId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.records import OwnershipRecord, SessionSnapshot
from airplay_linux.domain.resources import (
    AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok,
    SessionPhase, TransportState, TrustState,
)
from airplay_linux.storage import (
    AtomicFiles, JsonOwnershipStore, JsonReceiverStore, JsonSessionStore, PosixFilesystem,
)
from airplay_linux.testing.adapters import Deferred, FakeProcess
from airplay_linux.testing.builders import target
from airplay_linux.testing.time import FakeClock, FakeScheduler

from test_lifecycle_matrix import HD, app_factory, decision, ordered, request, resources

OWNER = OwnerId(UUID(int=800))
HELPER = ProcessIdentity(pid=7800, start_ticks=8800)
STAGES = ("reserved", "helper_started", "output_observed", "published", "removing")
ATOMIC_STEPS = ("write", "fsync", "rename", "directory_fsync")


class Compositor(FakeProcess):
    def __init__(self):
        super().__init__()
        self.entries = [self.output(1, "DP-1", physical=True)]
        self.associations = {}
        self.commands = []
        self.store = None
        self.stop_records = []
        self.sticky_output = False
        self.no_configuration_effect = False
        self.accessible = True
        self.next_output = 7

    @staticmethod
    def output(oid, connector, physical=False):
        return dict(id=oid, name=connector, type="DisplayPort" if physical else "Virtual",
                    enabled=True, pos={"x": 0, "y": 0}, scale=1,
                    size={"width": 1920, "height": 1080}, currentModeId="1",
                    modes=[{"id": "1", "size": {"width": 1920, "height": 1080}, "refreshRate": 60},
                           {"id": "2", "size": {"width": 1280, "height": 720}, "refreshRate": 60}])

    def environments(self):
        if not self.accessible:
            raise PermissionError("inaccessible fake procfs")
        return super().environments()

    async def verify_output(self, owner, runtime, helper, candidate):
        if self.owned(helper, owner, runtime) and self.associations.get(helper) == (
            candidate.output_id, candidate.connector,
        ):
            return Ok(value=None)
        return Err(error=diagnostic(ErrorCode.ownership_mismatch))

    async def run(self, spec, timeout, stdin=None):
        self.runs.append((spec, timeout))
        self.journal.record("compositor.run", argv=spec.argv)
        if spec.argv[0] == "krfb-virtualmonitor":
            text = "--resolution <WxH> --name NAME" if "--help" in spec.argv else "krfb-virtualmonitor 24.08.2"
            return Ok(value=CommandOutput(exit_code=0, stdout=text.encode(), stderr=b""))
        assert spec.argv[0] == "kscreen-doctor", spec.argv
        if spec.argv[1:] == ("-j",):
            return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps({"outputs": self.entries}).encode(), stderr=b""))
        self.commands.extend(spec.argv[1:])
        for token in spec.argv[1:]:
            _, oid, setting, *value = token.split(".")
            entry = next(item for item in self.entries if str(item["id"]) == oid)
            if self.no_configuration_effect:
                continue
            if setting in {"enable", "disable"}:
                entry["enabled"] = setting == "enable"
            elif setting == "position":
                x, y = map(int, value[0].split(","))
                entry["pos"] = {"x": x, "y": y}
            elif setting == "scale":
                entry["scale"] = float(".".join(value))
            elif setting == "mode":
                entry["currentModeId"] = value[0]
                entry["size"] = next(mode["size"] for mode in entry["modes"] if mode["id"] == value[0])
            else:
                raise AssertionError(f"unexpected compositor operation: {token}")
        return Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b""))

    async def spawn(self, spec):
        result = await super().spawn(spec)
        if isinstance(result, Ok):
            oid = self.next_output
            self.next_output += 1
            pair = (str(oid), f"Virtual-{oid}")
            self.entries.append(self.output(oid, pair[1]))
            self.associations[result.value] = pair
        return result

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline):
        self.stop_records.append((await self.store.all()).value)
        return await super().terminate(identity, owner_uuid, runtime_directory, deadline)

    def _forget(self, identity):
        pair = self.associations.pop(identity, None)
        if pair is not None and not self.sticky_output:
            self.entries = [entry for entry in self.entries if (str(entry["id"]), entry["name"]) != pair]
        super()._forget(identity)


class PollObserver(KScreenObserver):
    async def wait_for(self, predicate, deadline):
        result = await self.outputs()
        if isinstance(result, Ok) and predicate(result.value):
            return result
        # One deterministic poll represents the complete observation budget.
        self._clock.advance(max(0, deadline - self._clock.now()))
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))


class CrashFilesystem(PosixFilesystem):
    """One real publication fails; record the exact surviving bytes at failure."""
    def __init__(self, root):
        self.root, self.armed, self.published = root, None, False
        self.survivors = None

    def arm(self, step):
        self.armed, self.published = step, False

    def _fail(self, step):
        if self.armed == step:
            self.armed = None
            self.survivors = {str(path.relative_to(self.root)): path.read_bytes()
                              for path in self.root.rglob("*.json")}
            raise OSError("atomic-failure-canary")

    def write(self, parent, name, payload):
        self._fail("write")
        return super().write(parent, name, payload)

    def fsync_file(self, parent, name):
        self._fail("fsync")
        return super().fsync_file(parent, name)

    def replace(self, parent, source, destination):
        self._fail("rename")
        result = super().replace(parent, source, destination)
        self.published = True
        return result

    def fsync_directory(self, descriptor):
        if self.published:
            self._fail("directory_fsync")
        return super().fsync_directory(descriptor)


class World:
    def __init__(self, directory):
        self.directory = directory
        self.process, self.clock = Compositor(), FakeClock()
        self.scheduler = FakeScheduler(self.clock)
        self.filesystem = CrashFilesystem(directory / "state")
        self.files = AtomicFiles(directory / "state")
        # Fail ownership publication only; concurrent session snapshots use
        # their own filesystem and must not consume the armed crash boundary.
        self.store = JsonOwnershipStore(AtomicFiles(directory / "state", filesystem=self.filesystem))
        self.sessions = JsonSessionStore(self.files)
        self.receivers = JsonReceiverStore(self.files)
        self.process.store = self.store
        self.observer = PollObserver(self.process, self.clock, self.scheduler)
        self.provider = None
        self.release = True
        self.writes = []
        original = self.store.write

        async def trace(record):
            result = await original(record)
            self.writes.append((record, result))
            return result
        self.store.write = trace

    def configure(self, rig):
        # Reopen actual stores: no in-memory SessionSnapshot/RecoveryItem is
        # installed into the runtime under test.
        rig.clock, rig.scheduler = self.clock, self.scheduler
        rig.sessions, rig.receivers, rig.ownerships = self.sessions, self.receivers, self.store
        self.process.journal = rig.journal
        helper = VirtualHelper(self.process, self.clock)
        lock = CreationLock(self.directory / "runtime")

        async def released(record, operation):
            if self.release and not any(stream.receiver_id == record.receiver_id
                                        for stream in rig.transport.streams.values()):
                return Ok(value=None)
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))

        remover = DisplayRemover(helper, self.observer, self.store, lock, self.clock,
                                 process=self.process, output_verifier=self.process.verify_output,
                                 transport_released=released)
        recovery = DisplayRecovery(helper, self.observer, self.store, lock, self.clock, remover,
                                   output_verifier=self.process.verify_output)
        creator = DisplayCreator(helper, self.observer, self.store, lock,
                                 str(self.directory / "runtime"), InstanceId(UUID(int=1)),
                                 self.clock, self.scheduler, rig.ids,
                                 output_verifier=self.process.verify_output)
        rig.display = self.provider = KdeVirtualDisplayProvider(creator, recovery, remover,
                                                               self.observer, helper)
        rig.deps = replace(rig.deps, display=rig.display, sessions=self.sessions, receivers=self.receivers,
                           ownerships=self.store, clock=self.clock, scheduler=self.scheduler)

    async def seed(self, *, stage="published", desired="active", helper="alive", output=True,
                   trust=TrustState.Unknown):
        baseline = (await self.observer.snapshot()).value
        if output:
            self.process.entries.append(self.process.output(7, "Virtual-owned"))
        if helper == "alive":
            self.process.register(HELPER, self.process.owner_environment(OWNER, str(self.directory / "runtime")))
            self.process.associations[HELPER] = ("7", "Virtual-owned")
        elif helper == "reused":
            self.process.register(replace(HELPER, start_ticks=HELPER.start_ticks + 1),
                                  self.process.owner_environment(OwnerId(UUID(int=999)), "/foreign"))
        observed = stage in {"output_observed", "published", "removing"}
        record = OwnershipRecord(
            schema_version=1, owner_uuid=OWNER, app_instance_uuid=InstanceId(UUID(int=1)),
            runtime_directory=str(self.directory / "runtime"), creation_operation_id=OperationId(UUID(int=10)),
            receiver_id=target().receiver_id, endpoint_fingerprint=target().endpoint_fingerprint,
            desired_state=desired, stage=stage, requested_size=HD, observed_size=HD if observed else None,
            helper=None if stage == "reserved" else HELPER,
            connector="Virtual-owned" if observed else None, output_id="7" if observed else None,
            created_utc="2026-10-17T00:00:00Z", last_configuration_step=stage,
            baseline=baseline, last_applied={}, provider_version="24.08.2", capability_fingerprint="acceptance",
        )
        assert isinstance(await self.store.write(record), Ok)
        state = replace(SessionSnapshot.idle(target().receiver_id), mode=Mode.VIRTUAL,
                        intent_active=True, intent_stage=IntentStage.active, resolution=decision(),
                        session_generation=4, trust=trust)
        assert isinstance(await self.sessions.save(state), Ok)
        return record

    async def records(self):
        result = await JsonOwnershipStore(AtomicFiles(self.directory / "state")).all()
        assert isinstance(result, Ok), result
        return result.value


async def recovered_app(app_factory, world, *, online=True, destination=None):
    def configure(rig):
        world.configure(rig)
        if online:
            rig.discovery.add_target(destination or target())
    app = await app_factory(configure=configure, directory=world.directory)
    return app


async def output_publication_transition(app_factory):
    """An observed output is not usable until the final publication is durable."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="la-publish-") as directory:
        world = World(Path(directory))
        app = await app_factory(configure=world.configure, directory=world.directory)
        publish = Deferred()
        write = world.store.write

        async def held_publication(record):
            if record.stage == "published":
                await publish.wait()
            return await write(record)

        world.store.write = held_publication
        try:
            await app.start()
            before = app.state()
            assert before.display is DisplayState.Creating and before.display_handle is None
            assert before.transport is TransportState.Idle and before.stream is None
            durable, = await world.records()
            assert durable.stage == "output_observed"
            assert durable.last_configuration_step == "position"
            assert world.process.alive(durable.helper) and len(world.process.entries) == 2
            assert not app.transport.streams and not app.journal.calls("transport.start_stream")
            publish.resolve(None)
            await app.settle()
            after = app.state()
            published, = await world.records()
            assert published.stage == "published" and published.desired_state == "active"
            assert after.display is DisplayState.Owned and after.transport is TransportState.Streaming
            assert after.display_handle.owner_uuid == published.owner_uuid == durable.owner_uuid
            assert after.display_handle.helper == published.helper == durable.helper
            assert after.display_handle.source.kscreen_output_id == published.output_id
            assert len(app.journal.calls("transport.start_stream")) == 1
        finally:
            if not publish.resolved:
                publish.resolve(None)
            await app.settle()
            await app.runtime.shutdown()


async def test_creator_rollback_and_late_removal_share_a_confirmed_release(app_factory, tmp_path):
    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    write = world.store.write

    async def reject_publication(record):
        if record.stage == "published":
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        return await write(record)

    world.store.write = reject_publication
    events = []
    subscription = world.provider.subscribe(events.append)
    try:
        await app.start()
    finally:
        subscription.close()
    owner = world.writes[0][0].owner_uuid
    assert not await world.records()
    assert len(world.process.entries) == 1 and len(world.process.terminated) == 1
    operation = app.manager.context(target().receiver_id).operation()
    for _ in range(2):
        assert isinstance(await world.provider.mark_removing(owner), Ok)
        assert isinstance(await world.provider.remove(owner, operation), Ok)
    assert not await world.records()
    assert len(world.process.terminated) == 1
    assert app.state().display is DisplayState.None_ and not app.state().cleanup_pending
    assert any(event.kind == "cleanup_required" and event.diagnostic is not None
               and event.diagnostic.code is ErrorCode.persistence_failed for event in events)
    # A fresh provider lifetime cannot infer the old provider's receipt from an
    # empty store, even when it uses the exact same store and resource ports.
    original = world.provider.remover
    fresh = DisplayRemover(original.helper, original.observer, original.store,
                           original.creation_lock, original.clock)
    missing = await fresh.mark_removing(owner)
    assert isinstance(missing, Err) and missing.error.code is ErrorCode.ownership_mismatch


async def test_late_removal_waits_for_creator_deletion_acknowledgement(app_factory, tmp_path):
    import asyncio

    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    write, delete = world.store.write, world.store.delete
    acknowledgement = Deferred()

    async def reject_publication(record):
        if record.stage == "published":
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        return await write(record)

    async def held_deletion(owner):
        result = await delete(owner)
        await acknowledgement.wait()
        return result

    world.store.write, world.store.delete = reject_publication, held_deletion
    late_removal = None
    try:
        await app.start()
        owner = world.writes[0][0].owner_uuid
        assert not await world.records()  # unlink visible; no acknowledgement yet
        late_removal = asyncio.create_task(world.provider.mark_removing(owner))
        await app.settle()
        assert not late_removal.done()
        acknowledgement.resolve(None)
        await app.settle()
        assert isinstance(await late_removal, Ok)
        assert not await world.records()
        assert app.state().display is DisplayState.None_ and not app.state().cleanup_pending
    finally:
        if not acknowledgement.resolved:
            acknowledgement.resolve(None)
        await app.settle()
        if late_removal is not None:
            await late_removal


@pytest.mark.parametrize("previous_record", [False, True], ids=["never-owned", "externally-deleted"])
async def test_absent_display_record_without_release_receipt_fails_closed(
    app_factory, tmp_path, previous_record,
):
    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    if previous_record:
        await world.seed(stage="reserved", helper="dead", output=False)
        # Even a remembered reservation is not a release receipt.
        assert (await world.provider.remover.lookup(OWNER)).value is not None
        assert isinstance(await world.store.delete(OWNER), Ok)
    operation = app.manager.context(target().receiver_id).operation()
    for result in (await world.provider.mark_removing(OWNER),
                   await world.provider.remove(OWNER, operation)):
        assert isinstance(result, Err) and result.error.code is ErrorCode.ownership_mismatch
    assert not world.process.terminated and not await world.records()


async def test_failed_creator_deletion_cannot_certify_an_absent_record(app_factory, tmp_path):
    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    write, delete = world.store.write, world.store.delete

    async def reject_publication(record):
        if record.stage == "published":
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        return await write(record)

    async def unconfirmed_deletion(owner):
        # Model unlink becoming visible but its durability acknowledgement
        # failing: absence alone must never mint a confirmed-release receipt.
        assert isinstance(await delete(owner), Ok)
        return Err(error=diagnostic(ErrorCode.persistence_failed))

    world.store.write, world.store.delete = reject_publication, unconfirmed_deletion
    await app.start()
    owner = world.writes[0][0].owner_uuid
    assert not await world.records() and len(world.process.entries) == 1
    operation = app.manager.context(target().receiver_id).operation()
    for result in (await world.provider.mark_removing(owner),
                   await world.provider.remove(owner, operation)):
        assert isinstance(result, Err) and result.error.code is ErrorCode.ownership_mismatch
    assert app.state().display is DisplayState.CleanupRequired and app.state().cleanup_pending


async def partial_creation_transition(app_factory):
    import tempfile
    from pathlib import Path
    from airplay_linux.domain.messages import Cancel
    from airplay_linux.testing.adapters import Deferred

    with tempfile.TemporaryDirectory(prefix="la-partial-") as directory:
        world = World(Path(directory))
        app = await app_factory(configure=world.configure, directory=world.directory)
        launch = Deferred()
        world.process.queue_result("spawn", launch)
        late_helper = ProcessIdentity(pid=6088, start_ticks=7088)
        try:
            await app.start()
            assert app.state().display is DisplayState.Creating
            durable, = await world.records()
            assert durable.stage == "reserved" and durable.helper is None
            await app.send(Cancel(receiver_id=target().receiver_id))
            assert app.state().cleanup_pending and not app.state().intent_active
            assert not app.transport.streams
            launch.resolve(Ok(value=late_helper))
            await app.settle()
            assert not await world.records()
            assert not world.process.alive(late_helper)
            assert world.process.terminated == [late_helper]
            assert len(world.process.entries) == 1
            assert not app.manager.pending_resources(target().receiver_id)
            assert all(record.desired_state == "removing" for batch in world.process.stop_records for record in batch)
            # Resource release is confirmed above. The actor must retire its
            # remembered owner, rather than retry removing the deleted record.
            assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                              TrustState.Unknown, False)
            assert app.state().ownership_uuid is None and not app.state().cleanup_pending
        finally:
            if not launch.resolved:
                launch.resolve(Ok(value=late_helper))
            await app.settle()
            await app.runtime.shutdown()


async def recovery_transition(case, app_factory):
    # These transition IDs are exercised through the same real disk path as
    # the detailed crash boundary cases below.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory(prefix="la-recovery-") as directory:
        world = World(Path(directory))
        await world.seed()
        app = await recovered_app(app_factory, world, online=case == "adopt_recovering")
        before = app.state()
        assert resources(before) == (TransportState.Idle, DisplayState.Owned, AudioState.Disabled,
                                      TrustState.Unknown, True)
        assert before.phase is SessionPhase.Recovering and not app.transport.streams
        if case == "adopt_recovering":
            await app.send(Retry(receiver_id=target().receiver_id))
            assert app.state().transport is TransportState.Streaming
            assert app.state().display_handle == before.display_handle
            assert not world.process.spawned
        else:
            await app.send(Retry(receiver_id=target().receiver_id))
            assert app.state().stream is None and not app.transport.streams
            assert world.process.alive(HELPER)
        await app.runtime.shutdown()


@pytest.mark.parametrize("stage", STAGES)
async def test_fresh_runtime_reconciles_every_durable_ownership_stage(app_factory, tmp_path, stage):
    world = World(tmp_path)
    await world.seed(stage=stage)
    app = await recovered_app(app_factory, world)
    if stage == "removing":
        assert not app.state().intent_active and app.state().display_handle is None
        assert not app.transport.streams
        assert not await world.records() and not world.process.alive(HELPER)
    else:
        before = app.state()
        assert before.phase is SessionPhase.Recovering and before.display is DisplayState.Owned
        assert before.stream is None and before.display_handle.helper == HELPER
        durable, = await world.records()
        assert durable.stage == "published" and durable.desired_state == "active"
        if stage == "reserved":
            writes = [record for record, result in world.writes[1:] if isinstance(result, Ok)]
            assert writes[0].stage == "helper_started" and writes[0].helper == HELPER
        await app.send(Retry(receiver_id=target().receiver_id))
        assert app.state().transport is TransportState.Streaming
        assert app.state().display_handle == before.display_handle
        assert not world.process.spawned
    assert all(record.desired_state == "removing" for batch in world.process.stop_records for record in batch)


@pytest.mark.parametrize("stage", STAGES[:-1])
@pytest.mark.parametrize("step", ATOMIC_STEPS)
async def test_atomic_recovery_publication_failure_never_exports_a_usable_handle(
    app_factory, tmp_path, stage, step,
):
    world = World(tmp_path)
    original = await world.seed(stage=stage)
    # A repair forces a durable write at every recovery stage.
    world.process.entries[1].update(enabled=False, currentModeId="2",
                                     size={"width": 1280, "height": 720})
    world.filesystem.arm(step)
    app = await recovered_app(app_factory, world)
    assert app.state().display is DisplayState.CleanupRequired
    assert app.state().display_handle is None and not app.transport.streams
    assert world.filesystem.survivors is not None
    for payload in world.filesystem.survivors.values():
        assert isinstance(json.loads(payload), dict)
        assert b"atomic-failure-canary" not in payload
    records = await world.records()
    assert records and records[0].owner_uuid == original.owner_uuid
    assert not world.process.terminated


async def test_durable_removing_intent_and_stop_ack_gate_helper_release_and_mode_switch(
    app_factory, tmp_path,
):
    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    before = await app.start(audio=True)
    held = Deferred()
    app.transport.queue_result("stop_stream", held)
    snapshots_at_stop = []
    original_stop = app.transport.stop_stream

    async def observe_stop(handle, operation):
        snapshots_at_stop.append(await world.records())
        return await original_stop(handle, operation)

    app.transport.stop_stream = observe_stop
    app.journal.clear()
    try:
        await app.send(ConfirmSwitch(receiver_id=before.receiver_id,
                                     request=request(Mode.MIRROR), confirmed=True))
        # Reopen from disk while stop is suspended; an in-memory removing flag
        # cannot establish the durable admission boundary.
        durable, = await world.records()
        assert durable.owner_uuid == before.ownership_uuid
        assert (durable.desired_state, durable.stage) == ("removing", "removing")
        assert snapshots_at_stop == [(durable,)]
        assert len(app.journal.calls("transport.stop_stream")) == 1
        assert world.process.alive(before.display_handle.helper)
        assert len(world.process.entries) == 2 and not world.process.terminated
        assert not app.journal.calls("capture.select")
        assert not app.journal.calls("transport.stop_audio")
        held.resolve(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
        await app.settle()
        assert app.state().phase is SessionPhase.CleanupRequired
        assert world.process.alive(before.display_handle.helper)
        assert not app.journal.calls("capture.select")
        await app.send(RetryCleanup(receiver_id=before.receiver_id))
        assert app.state().phase is SessionPhase.Mirroring
        assert app.state().display_handle is None and not app.state().cleanup_pending
        assert not await world.records() and len(world.process.entries) == 1
        assert world.process.terminated == [before.display_handle.helper]
        assert len(app.transport.streams) == 1
        assert all(record.desired_state == "removing" for batch in world.process.stop_records for record in batch)
        ordered(app.journal, "transport.stop_stream", "transport.stop_audio", "process.terminate",
                "capture.select", "transport.start_stream")
    finally:
        if not held.resolved:
            held.resolve(Ok(value=None))
        await app.settle()


@pytest.mark.parametrize("step", ATOMIC_STEPS)
async def test_failed_durable_removal_intent_prevents_transport_and_helper_release(
    app_factory, tmp_path, step,
):
    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    before = await app.start()
    world.filesystem.arm(step)
    app.journal.clear()
    await app.send(Stop(receiver_id=before.receiver_id))
    assert app.state().phase is SessionPhase.CleanupRequired
    assert app.state().cleanup_pending and world.process.alive(before.display_handle.helper)
    assert not app.journal.calls("transport.stop_stream")
    assert not world.process.terminated
    assert world.filesystem.survivors is not None
    durable, = await world.records()
    assert durable.owner_uuid == before.display_handle.owner_uuid
    assert durable.desired_state == ("removing" if step == "directory_fsync" else "active")
    await app.send(RetryCleanup(receiver_id=before.receiver_id))
    assert not await world.records() and not app.transport.streams
    assert app.state().display is DisplayState.None_


async def test_removing_intent_survives_restart_without_republishing_output(app_factory, tmp_path):
    world = World(tmp_path)
    await world.seed(stage="helper_started", desired="removing")
    world.release = False
    app = await recovered_app(app_factory, world)
    assert not app.state().intent_active and app.state().cleanup_pending
    assert app.state().display_handle is None
    assert not app.transport.streams and not world.process.terminated
    assert all(record.desired_state == "removing" for record in await world.records())
    assert not any(record.desired_state == "active" for record, _ in world.writes[1:])
    world.release = True
    await app.send(RetryCleanup(receiver_id=target().receiver_id))
    assert not await world.records() and not world.process.alive(HELPER)


async def test_missing_helper_requires_explicit_orphan_confirmation_and_preserves_evidence(
    app_factory, tmp_path,
):
    world = World(tmp_path)
    original = await world.seed(helper="dead")
    app = await recovered_app(app_factory, world)
    assert app.state().phase is SessionPhase.CleanupRequired and app.state().display_handle is None
    for command in (RetryCleanup(receiver_id=target().receiver_id), Stop(receiver_id=target().receiver_id),
                    ConfirmOrphanRemoval(receiver_id=target().receiver_id, owner_uuid=OWNER, confirmed=False)):
        await app.send(command)
        assert (await world.records())[0] == original
        assert len(world.process.entries) == 2 and not world.process.commands
    await app.send(ConfirmOrphanRemoval(receiver_id=target().receiver_id, owner_uuid=OWNER, confirmed=True))
    # This provider has no documented safe orphan-removal operation. Approval
    # must retain evidence and explain manual cleanup, never invent a command.
    assert app.state().cleanup_pending
    retained, = await world.records()
    assert retained == replace(original, desired_state="removing", stage="removing",
                               last_configuration_step="removing")
    assert not world.process.terminated and len(world.process.entries) == 2


@pytest.mark.parametrize("evidence", ["no_record", "reused_pid", "inaccessible"])
async def test_recovery_never_signals_foreign_or_unproven_resources(app_factory, tmp_path, evidence):
    world = World(tmp_path)
    await world.seed(helper="reused" if evidence == "reused_pid" else "alive")
    if evidence == "no_record":
        assert isinstance(await world.store.delete(OWNER), Ok)
    if evidence == "inaccessible":
        world.process.accessible = False
    entries = deepcopy(world.process.entries)
    app = await recovered_app(app_factory, world)
    assert app.state().display_handle is None and not app.transport.streams
    await app.send(Stop(receiver_id=target().receiver_id))
    await app.runtime.shutdown()
    assert not world.process.terminated and not world.process.commands
    assert world.process.entries == entries
    if evidence != "no_record":
        assert await world.records()


@pytest.mark.parametrize("changed_endpoint", [False, True])
async def test_recovery_respects_offline_and_changed_endpoint_without_new_creation(
    app_factory, tmp_path, changed_endpoint,
):
    world = World(tmp_path)
    await world.seed()
    app = await recovered_app(app_factory, world, online=changed_endpoint,
                              destination=target(endpoint_generation=2, address="192.0.2.99"))
    before = app.state()
    assert before.display is DisplayState.Owned and before.stream is None
    if not changed_endpoint:
        await app.send(Retry(receiver_id=target().receiver_id))
        assert app.state().stream is None
        await app.online(target(endpoint_generation=2, address="192.0.2.99"))
        assert app.state().stream is None
    else:
        await app.send(Retry(receiver_id=target().receiver_id))
        assert app.state().stream.endpoint_fingerprint == target(endpoint_generation=2).endpoint_fingerprint
        call, = app.journal.calls("transport.start_stream")
        assert call["target"]["canonical_host"] == "192.0.2.99"
    assert app.state().display_handle == before.display_handle and not world.process.spawned


async def test_recovery_trust_tombstone_blocks_reconnect_until_revocation_confirmed(app_factory, tmp_path):
    world = World(tmp_path)
    await world.seed(trust=TrustState.CleanupRequired)
    def configure(rig):
        world.configure(rig)
        rig.discovery.add_target(target())
        rig.pairing.queue_result("retry_revocation", Err(error=diagnostic(ErrorCode.trust_cleanup_required)))
    app = await app_factory(configure=configure, directory=tmp_path)
    assert app.state().trust is TrustState.CleanupRequired
    await app.send(Retry(receiver_id=target().receiver_id))
    assert not app.transport.streams and app.state().display_handle.helper == HELPER
    assert not world.process.spawned


@pytest.mark.parametrize("user_change", [False, True])
async def test_layout_restore_follows_exact_helper_and_output_disappearance(
    app_factory, tmp_path, user_change,
):
    world = World(tmp_path)
    record = await world.seed()
    app = await recovered_app(app_factory, world)
    await app.send(Retry(receiver_id=target().receiver_id))
    # Model the property changed during creation; changes after it belong to
    # the user and must survive removal.
    world.process.entries[0]["pos"]["x"] = 900 if user_change else 1920
    assert isinstance(await world.store.write(replace(record, last_applied={"DP-1": {"x": 1920}})), Ok)
    world.process.commands.clear()
    await app.send(Stop(receiver_id=target().receiver_id))
    assert world.process.entries[0]["pos"]["x"] == (900 if user_change else 0)
    assert len(world.process.entries) == 1 and not world.process.alive(HELPER)
    assert not app.transport.streams
    assert all(item.desired_state == "removing" for batch in world.process.stop_records for item in batch)
    if user_change:
        assert not world.process.commands
        assert app.state().phase is SessionPhase.CleanupRequired and await world.records()
    else:
        assert world.process.commands == ["output.1.position.0,0"]
        assert app.state().display is DisplayState.None_ and not await world.records()
    names = app.journal.names()
    stop = names.index("transport.stop_stream")
    helper = names.index("process.terminate")
    assert stop < helper
    restore = [i for i, (name, args) in enumerate(app.journal.entries)
               if name == "compositor.run" and any(".position." in str(v) for v in args["argv"])]
    assert all(helper < i for i in restore)
