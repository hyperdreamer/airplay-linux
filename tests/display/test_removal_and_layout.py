"""Conservative teardown exercised over real helper, KScreen and ownership adapters."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kde import KdeVirtualDisplayProvider
from airplay_linux.display.kscreen import KScreenObserver, layout_hash
from airplay_linux.display.locking import CreationLock
from airplay_linux.display.removal import DisplayRemover, eligible_restore
from airplay_linux.domain.operations import InstanceId, OperationId, OwnerId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.records import LayoutSnapshot, OwnershipRecord
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.storage import AtomicFiles, JsonOwnershipStore
from airplay_linux.process import IdentityEvidence, IdentityState
from airplay_linux.testing.adapters import FakeProcess, MemoryOwnershipStore, SequenceIds
from airplay_linux.testing.builders import context
from airplay_linux.testing.time import FakeClock, FakeScheduler


# Audit hooks cannot be removed. Scope its assertion to these display tests so
# collection does not change another module's missing-dependency semantics.
_GUARD_ACTIVE = False


def _no_live_kde(event, args):
    if _GUARD_ACTIVE and event in {"subprocess.Popen", "os.posix_spawn", "os.exec"}:
        executable = args[0]
        if isinstance(executable, (str, bytes)) and os.path.basename(os.fsdecode(executable)) in {
            "kscreen-doctor", "krfb-virtualmonitor",
        }:
            raise AssertionError("real KDE commands are forbidden in display tests")


sys.addaudithook(_no_live_kde)


@pytest.fixture(autouse=True)
def no_live_kde():
    global _GUARD_ACTIVE
    _GUARD_ACTIVE = True
    try:
        yield
    finally:
        _GUARD_ACTIVE = False


@pytest.mark.parametrize("command", ("kscreen-doctor", "krfb-virtualmonitor"))
def test_os_boundary_guard_rejects_real_kde_execution(command):
    with pytest.raises(AssertionError, match="forbidden"):
        _no_live_kde("subprocess.Popen", (command,))


SIZE = Size(width=1920, height=1080)
OWNER = OwnerId(UUID(int=80))
HELPER = ProcessIdentity(pid=7000, start_ticks=8000)
RUNTIME = "/run/user/1000/airplay-linux/instances/test-display"


class Desktop(FakeProcess):
    """A compositor/process world; the production adapters retain their own logic."""
    def __init__(self):
        super().__init__()
        self.entries = [self.entry("1", "DP-1", physical=True)]
        self.associations = {}
        self.commands = []
        self.stop_records = []
        self.store = None
        self.scan_accessible = True
        self.sticky_output = False
        self.no_configuration_effect = False
        self.lose_geometry = False

    @staticmethod
    def entry(oid, connector, physical=False):
        return dict(id=int(oid), name=connector, type="DisplayPort" if physical else "Virtual",
                    enabled=True, pos={"x": 0, "y": 0}, scale=1,
                    size={"width": 1920, "height": 1080}, currentModeId="1",
                    modes=[{"id": "1", "size": {"width": 1920, "height": 1080}, "refreshRate": 60},
                           {"id": "2", "size": {"width": 1280, "height": 720}, "refreshRate": 60}])

    def environments(self):
        if not self.scan_accessible:
            raise PermissionError("unreadable process evidence")
        return super().environments()

    async def verify_output(self, owner, runtime, identity, candidate):
        if not self.owned(identity, owner, runtime) or self.associations.get(identity) != (
            candidate.output_id, candidate.connector,
        ):
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        return Ok(value=None)

    async def run(self, spec, timeout, stdin=None):
        self.runs.append((spec, timeout))
        if spec.argv[0] == "krfb-virtualmonitor":
            text = "--resolution <WxH> --name NAME" if "--help" in spec.argv else "krfb-virtualmonitor 24.08.2"
            return Ok(value=CommandOutput(exit_code=0, stdout=text.encode(), stderr=b""))
        assert spec.argv[0] == "kscreen-doctor"
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
                entry["size"] = next(m["size"] for m in entry["modes"] if m["id"] == value[0])
            else:
                raise AssertionError("invented KScreen operation")
        return Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b""))

    async def spawn(self, spec):
        result = await super().spawn(spec)
        self.entries.append(self.entry("7", "Virtual-owned"))
        self.associations[result.value] = ("7", "Virtual-owned")
        return result

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline):
        self.stop_records.append((await self.store.all()).value)
        return await super().terminate(identity, owner_uuid, runtime_directory, deadline)

    def _forget(self, identity):
        pair = self.associations.pop(identity, None)
        if pair is not None and self.lose_geometry:
            for entry in self.entries:
                if (str(entry["id"]), entry["name"]) == pair:
                    entry.pop("size", None)
                    entry["currentModeId"] = "0"
        if pair is not None and not self.sticky_output:
            self.entries = [e for e in self.entries if (str(e["id"]), e["name"]) != pair]
        super()._forget(identity)


class Observer(KScreenObserver):
    """Only polling time is driven locally; all reads/configuration use the real CLI adapter."""
    def __init__(self, process, clock):
        super().__init__(process, clock, FakeScheduler(clock))
        self.deadlines = []

    async def wait_for(self, predicate, deadline):
        self.deadlines.append(deadline - self._clock.now())
        result = await self.outputs()
        if isinstance(result, Ok) and predicate(result.value):
            return result
        self._clock.advance(max(0, deadline - self._clock.now()))
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))


class Rig:
    def __init__(self, tmp_path, *, durable=False, release=True, verifier=True):
        self.process = Desktop()
        self.clock = FakeClock()
        self.observer = Observer(self.process, self.clock)
        self.helper = VirtualHelper(self.process, self.clock)
        self.store = JsonOwnershipStore(AtomicFiles(tmp_path / "state")) if durable else MemoryOwnershipStore()
        self.process.store = self.store
        self.lock = CreationLock(tmp_path / "runtime")
        self.released = release
        self.release_calls = []
        self.verifier = self.process.verify_output if verifier else None
        self.remover = DisplayRemover(
            self.helper, self.observer, self.store, self.lock, self.clock,
            process=self.process, output_verifier=self.verifier, transport_released=self.confirm_release,
        )
        self.events = []
        self.remover.subscribe(self.events.append)

    async def confirm_release(self, record, operation):
        self.release_calls.append((record.owner_uuid, operation))
        if self.released:
            return Ok(value=None)
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    async def seed(self, stage="published", desired="active", helper="alive", output="exact"):
        baseline = (await self.observer.snapshot()).value
        if output != "absent":
            entry = self.process.entry("7", "Virtual-owned")
            if output == "misconfigured":
                entry.update(size={"width": 1280, "height": 720}, currentModeId="2", enabled=False,
                             pos={"x": 900, "y": 200})
            self.process.entries.append(entry)
            if output == "ambiguous":
                self.process.entries.append(self.process.entry("7", "Virtual-other"))
        if helper == "alive" or helper == "inaccessible":
            self.process.register(HELPER, self.process.owner_environment(OWNER, RUNTIME))
            self.process.associations[HELPER] = ("7", "Virtual-owned")
        if helper == "reused":
            self.process.register(replace(HELPER, start_ticks=8001), self.process.owner_environment(UUID(int=99), "/foreign"))
        self.process.scan_accessible = helper != "inaccessible"
        observed = stage in {"output_observed", "published", "removing"}
        record = OwnershipRecord(
            schema_version=1, owner_uuid=OWNER, app_instance_uuid=InstanceId(UUID(int=1)),
            runtime_directory=RUNTIME, creation_operation_id=OperationId(UUID(int=10)),
            receiver_id="airplay-device:a", endpoint_fingerprint="endpoint-a",
            desired_state=desired, stage=stage, requested_size=SIZE,
            observed_size=SIZE if observed else None, helper=None if stage == "reserved" else HELPER,
            connector="Virtual-owned" if observed else None, output_id="7" if observed else None,
            created_utc="2026-10-17T00:00:00Z", last_configuration_step=stage,
            baseline=baseline, last_applied={}, provider_version="24.08.2", capability_fingerprint="test",
        )
        assert isinstance(await self.store.write(record), Ok)
        return record

    def recovery(self):
        from airplay_linux.display.recovery import DisplayRecovery
        return DisplayRecovery(self.helper, self.observer, self.store, self.lock, self.clock, self.remover,
                               output_verifier=self.verifier)

    def provider(self):
        creator = DisplayCreator(self.helper, self.observer, self.store, self.lock, RUNTIME,
                                 UUID(int=1), self.clock, FakeScheduler(self.clock), SequenceIds(90),
                                 output_verifier=self.verifier)
        provider = KdeVirtualDisplayProvider(creator, self.recovery(), self.remover, self.observer, self.helper)
        provider.subscribe(self.events.append)
        return provider


async def records(rig):
    return (await rig.store.all()).value


def test_user_change_is_not_restored():
    before = LayoutSnapshot(outputs={"DP-1": {"x": 0, "scale": 1}}, baseline_hash="a")
    current = LayoutSnapshot(outputs={"DP-1": {"x": 900, "scale": 1}}, baseline_hash="b")
    assert eligible_restore(before, {"DP-1": {"x": 1920}}, current) == {}


def test_restore_only_touched_unchanged_properties_on_same_surviving_output():
    before = LayoutSnapshot(outputs={"DP-1": {"id": "1", "x": 0, "scale": 1},
                                     "DP-2": {"id": "2", "scale": 1}}, baseline_hash="a")
    current = LayoutSnapshot(outputs={"DP-1": {"id": "1", "x": 1920, "scale": 2},
                                      "DP-2": {"id": "22", "scale": 2},
                                      "Virtual-owned": {"x": 0}}, baseline_hash="b")
    applied = {"DP-1": {"x": 1920}, "DP-2": {"scale": 2}, "Virtual-owned": {"x": 0}}
    assert eligible_restore(before, applied, current) == {"DP-1": {"x": 0}}


@pytest.mark.parametrize("step", MemoryOwnershipStore.STEPS)
async def test_failed_removal_intent_blocks_all_destructive_actions(tmp_path, step):
    rig = Rig(tmp_path)
    await rig.seed()
    rig.store.inject_failure(step)
    result = await rig.remover.remove(OWNER, context())
    assert isinstance(result, Err) and result.error.code == ErrorCode.persistence_failed
    assert rig.process.alive(HELPER) and len(rig.process.entries) == 2
    assert not rig.process.terminated and not rig.process.commands
    assert await records(rig)


async def test_mark_removing_alone_does_not_touch_resources(tmp_path):
    rig = Rig(tmp_path, durable=True)
    await rig.seed()
    assert isinstance(await rig.remover.mark_removing(OWNER), Ok)
    persisted = (await JsonOwnershipStore(AtomicFiles(tmp_path / "state")).all()).value[0]
    assert (persisted.stage, persisted.desired_state) == ("removing", "removing")
    assert rig.process.alive(HELPER) and len(rig.process.entries) == 2
    assert not rig.process.terminated and not rig.release_calls


async def test_transport_release_is_required_before_helper_stop(tmp_path):
    rig = Rig(tmp_path, release=False)
    await rig.seed()
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert not rig.process.terminated and rig.process.alive(HELPER)
    assert (await records(rig))[0].desired_state == "removing"
    rig.released = True
    assert isinstance(await rig.remover.remove(OWNER, context()), Ok)
    assert not await records(rig)


async def test_missing_transport_confirmation_fails_closed(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    remover = DisplayRemover(rig.helper, rig.observer, rig.store, rig.lock, rig.clock,
                             process=rig.process, output_verifier=rig.verifier)
    assert isinstance(await remover.remove(OWNER, context()), Err)
    assert rig.process.alive(HELPER) and await records(rig)


async def test_removal_confirms_exact_process_and_output_then_deletes_record(tmp_path):
    rig = Rig(tmp_path, durable=True)
    await rig.seed()
    foreign = rig.process.entry("8", "Virtual-foreign")
    rig.process.entries.append(foreign)
    assert isinstance(await rig.remover.remove(OWNER, context(endpoint_generation=9, generation=4)), Ok)
    assert rig.process.terminated == [HELPER]
    assert all(r.desired_state == "removing" and r.stage == "removing"
               for batch in rig.process.stop_records for r in batch)
    assert rig.process.entries == [rig.process.entry("1", "DP-1", physical=True), foreign]
    assert not await records(rig)
    assert [event.kind for event in rig.events][-1] == "removed"
    assert rig.events[-1].helper == HELPER and rig.events[-1].handle is None


@pytest.mark.parametrize("evidence", ["reused", "inaccessible"])
async def test_uncertain_process_is_never_signalled(tmp_path, evidence):
    rig = Rig(tmp_path)
    await rig.seed(helper=evidence)
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert not rig.process.terminated and len(rig.process.entries) == 2
    assert await records(rig)


@pytest.mark.parametrize("collision", ["connector", "id", "physical", "association"])
async def test_unowned_or_reused_output_is_never_removed(tmp_path, collision):
    rig = Rig(tmp_path)
    await rig.seed()
    entry = rig.process.entries[1]
    if collision == "connector":
        entry["id"] = 88
    elif collision == "id":
        entry["name"] = "Virtual-foreign"
    elif collision == "physical":
        entry["name"], entry["type"] = "HDMI-1", "HDMI"
    else:
        rig.process.associations.clear()
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert not rig.process.terminated and len(rig.process.entries) == 2
    assert await records(rig)


async def test_missing_association_authority_cannot_remove_live_output(tmp_path):
    rig = Rig(tmp_path, verifier=False)
    await rig.seed()
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert not rig.process.terminated and await records(rig)


async def test_full_snapshot_blocks_false_disappearance_when_geometry_is_lost(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    rig.process.sticky_output = True
    rig.process.lose_geometry = True

    result = await rig.remover.remove(OWNER, context())

    assert isinstance(result, Err)
    assert await records(rig)
    assert any(entry["name"] == "Virtual-owned" for entry in rig.process.entries)
    assert rig.events[-1].kind == "cleanup_required"


async def test_sticky_output_retains_record_until_disappearance(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    rig.process.sticky_output = True
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert await records(rig) and not rig.process.alive(HELPER)
    assert rig.observer.deadlines == [5.0]
    assert rig.events[-1].kind == "cleanup_required"
    rig.process.entries.pop()
    assert isinstance(await rig.remover.remove(OWNER, context()), Ok)
    assert not await records(rig)


@pytest.mark.parametrize("stage", ("published", "reserved"))
async def test_contradictory_observation_never_stops_helper_without_association_proof(tmp_path, stage):
    rig = Rig(tmp_path, verifier=False)
    await rig.seed(stage, output="absent")
    original_run = rig.process.run
    reads = 0

    async def interleaved_run(spec, timeout, stdin=None):
        nonlocal reads
        if spec.argv[1:] == ("-j",):
            reads += 1
            if reads == 4:
                rig.process.entries.append(rig.process.entry("7", "Virtual-owned"))
        return await original_run(spec, timeout, stdin)

    rig.process.run = interleaved_run
    result = await rig.remover.remove(OWNER, context())

    assert reads >= 4
    assert isinstance(result, Err)
    assert rig.process.alive(HELPER)
    assert any(entry["name"] == "Virtual-owned" for entry in rig.process.entries)
    assert await records(rig)


async def test_recovery_does_not_configure_reassigned_output(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("output_observed", output="misconfigured")
    original_run = rig.process.run
    reads = 0
    foreign = rig.process.entry("7", "HDMI-foreign", physical=True)
    foreign.update(enabled=False, pos={"x": 777, "y": 888},
                   size={"width": 1280, "height": 720}, currentModeId="2")
    foreign_before = deepcopy(foreign)

    async def interleaved_run(spec, timeout, stdin=None):
        nonlocal reads
        if spec.argv[1:] == ("-j",):
            reads += 1
            if reads == 7:
                rig.process.entries[1] = foreign
        return await original_run(spec, timeout, stdin)

    rig.process.run = interleaved_run
    item, = await rig.recovery().reconcile()

    assert reads >= 7
    assert item.kind == "cleanup_required"
    assert rig.process.entries[1] == foreign_before
    assert not rig.process.commands


async def test_restore_does_not_configure_reassigned_output(tmp_path):
    rig = Rig(tmp_path)
    record = await rig.seed()
    rig.process.entries[0]["pos"] = {"x": 1920, "y": 900}
    await rig.store.write(replace(record, last_applied={"DP-1": {"x": 1920}}))
    original_run = rig.process.run
    reads = 0
    foreign = rig.process.entry("1", "HDMI-foreign", physical=True)
    foreign.update(enabled=False, pos={"x": 777, "y": 888},
                   size={"width": 1280, "height": 720}, currentModeId="2")
    foreign_before = deepcopy(foreign)

    async def interleaved_run(spec, timeout, stdin=None):
        nonlocal reads
        if spec.argv[1:] == ("-j",):
            reads += 1
            if reads == 8:
                rig.process.entries[0] = foreign
        return await original_run(spec, timeout, stdin)

    rig.process.run = interleaved_run
    result = await rig.remover.remove(OWNER, context())

    assert reads >= 8
    assert isinstance(result, Err)
    assert rig.process.entries[0] == foreign_before
    assert not rig.process.commands
    assert await records(rig)


@pytest.mark.parametrize("changed_position", [(1930, 900), (1920, 950)])
async def test_restore_refuses_when_touched_or_companion_coordinate_changes(tmp_path, changed_position):
    rig = Rig(tmp_path)
    record = await rig.seed()
    rig.process.entries[0]["pos"] = {"x": 1920, "y": 900}
    await rig.store.write(replace(record, last_applied={"DP-1": {"x": 1920}}))
    original_run = rig.process.run
    reads = 0

    async def interleaved_run(spec, timeout, stdin=None):
        nonlocal reads
        if spec.argv[1:] == ("-j",):
            reads += 1
            if reads == 8:
                rig.process.entries[0]["pos"] = {
                    "x": changed_position[0], "y": changed_position[1],
                }
        return await original_run(spec, timeout, stdin)

    rig.process.run = interleaved_run
    result = await rig.remover.remove(OWNER, context())

    assert reads >= 8
    assert isinstance(result, Err)
    assert rig.process.entries[0]["pos"] == {
        "x": changed_position[0], "y": changed_position[1],
    }
    assert not rig.process.commands
    assert await records(rig)


async def test_false_stop_acknowledgement_does_not_delete_evidence(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    rig.process.queue_result("terminate", Ok(value=None))
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert rig.process.alive(HELPER) and await records(rig)


@pytest.mark.parametrize("confirmed", [False, True])
async def test_orphan_requires_confirmation_and_documented_removal_capability(tmp_path, confirmed):
    rig = Rig(tmp_path)
    original = await rig.seed(helper="dead")
    result = await rig.remover.approve_orphan(OWNER, confirmed, context())
    assert not rig.process.terminated and not rig.process.commands
    assert len(rig.process.entries) == 2 and await records(rig)
    if confirmed:
        assert isinstance(result, Err)
        assert "manual" in result.error.message.lower()
    else:
        assert isinstance(result, Ok) and (await records(rig))[0] == original


async def test_confirmation_revalidates_persisted_orphan_identity(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed(helper="dead")
    rig.process.entries[1]["id"] = 99
    result = await rig.remover.approve_orphan(OWNER, True, context())
    assert isinstance(result, Err) and result.error.code == ErrorCode.ownership_mismatch
    assert not rig.process.commands and len(rig.process.entries) == 2


async def test_restore_layout_after_disappearance_and_confirm_readback(tmp_path):
    rig = Rig(tmp_path)
    record = await rig.seed()
    # Creation shifted this property. Another receiver/user changed scale afterwards.
    rig.process.entries[0].update(pos={"x": 1920, "y": 900}, scale=2)
    baseline = replace(record.baseline, outputs={"DP-1": {**record.baseline.outputs["DP-1"], "y": 100}})
    await rig.store.write(replace(record, baseline=baseline, last_applied={"DP-1": {"x": 1920, "y": 0, "scale": 1}}))
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert rig.process.entries[0]["pos"] == {"x": 0, "y": 900}
    assert rig.process.entries[0]["scale"] == 2
    # Eligible x is restored, but touched y/scale still differ from baseline.
    assert await records(rig)
    assert not any(event.kind == "removed" for event in rig.events)


async def test_unconfirmed_layout_restore_keeps_cleanup_record(tmp_path):
    rig = Rig(tmp_path)
    record = await rig.seed()
    rig.process.entries[0]["pos"]["x"] = 1920
    await rig.store.write(replace(record, last_applied={"DP-1": {"x": 1920}}))
    rig.process.no_configuration_effect = True
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert await records(rig) and len(rig.process.entries) == 1


async def test_provider_publishes_initial_reservation_and_each_verified_stage(tmp_path):
    rig = Rig(tmp_path)
    provider = rig.provider()
    result = await provider.create("airplay-device:a", "endpoint-a", SIZE, context())
    assert isinstance(result, Ok)
    stages = [event for event in rig.events if event.kind in {"reserved", "helper_started", "output_observed", "published"}]
    assert [event.kind for event in stages] == ["reserved", "helper_started", "output_observed", "published"]
    assert all(event.handle is None for event in stages[:-1])
    assert stages[-1].handle == result.value
    assert all(event.owner_uuid == result.value.owner_uuid for event in stages)
    assert isinstance(await provider.prepare_teardown("airplay-device:a", context().operation_id, context()), Ok)
    assert rig.process.alive(result.value.helper) and not rig.process.terminated


async def test_provider_reports_disappearance_without_a_usable_handle(tmp_path):
    rig = Rig(tmp_path)
    provider = rig.provider()
    result = await provider.create("airplay-device:a", "endpoint-a", SIZE, context())
    assert isinstance(result, Ok)
    rig.process.entries.pop()
    await rig.observer.outputs()
    await provider.flush_events()
    disappeared = [event for event in rig.events if event.kind == "disappeared"]
    assert disappeared and disappeared[-1].owner_uuid == result.value.owner_uuid
    assert disappeared[-1].helper == result.value.helper and disappeared[-1].handle is None


async def test_provider_reports_helper_exit_with_persisted_identity(tmp_path):
    rig = Rig(tmp_path)
    provider = rig.provider()
    result = await provider.create("airplay-device:a", "endpoint-a", SIZE, context())
    assert isinstance(result, Ok)
    rig.process.sticky_output = True
    rig.process.exit(result.value.helper)
    await provider.flush_events()
    exited = [event for event in rig.events if event.kind == "helper_exit"]
    assert exited and exited[-1].helper == result.value.helper and exited[-1].handle is None
    assert (await records(rig))[0].desired_state == "removing"


async def test_reused_output_during_association_await_blocks_helper_stop(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    original = rig.process.verify_output

    async def replaced(*args):
        result = await original(*args)
        rig.process.entries[1]["id"] = 99
        return result

    rig.remover = DisplayRemover(rig.helper, rig.observer, rig.store, rig.lock, rig.clock,
                                 process=rig.process, output_verifier=replaced,
                                 transport_released=rig.confirm_release)
    result = await rig.remover.remove(OWNER, context())
    assert isinstance(result, Err)
    assert not rig.process.terminated and rig.process.alive(HELPER)
    assert await records(rig)


async def test_layout_restore_does_not_change_other_receivers_owned_output(tmp_path):
    rig = Rig(tmp_path)
    record = await rig.seed()
    other_owner = OwnerId(UUID(int=81))
    other_helper = ProcessIdentity(pid=7001, start_ticks=8001)
    rig.process.entries.append(rig.process.entry("8", "Virtual-other"))
    rig.process.register(other_helper, rig.process.owner_environment(other_owner, RUNTIME))
    rig.process.associations[other_helper] = ("8", "Virtual-other")
    second = replace(record, owner_uuid=other_owner, receiver_id="airplay-device:b", helper=other_helper,
                     output_id="8", connector="Virtual-other")
    await rig.store.write(second)
    baseline = replace(record.baseline, outputs={**record.baseline.outputs,
                                                "Virtual-other": {"id": "8", "x": 1920, "y": 0}})
    await rig.store.write(replace(record, baseline=baseline, last_applied={"Virtual-other": {"x": 0}}))
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert rig.process.alive(other_helper)
    assert rig.process.entries[-1]["pos"] == {"x": 0, "y": 0}
    assert not rig.process.commands
    assert second in await records(rig) and len(await records(rig)) == 2
    assert not any(event.kind == "removed" for event in rig.events)


async def test_process_identity_none_without_absence_evidence_blocks_deletion(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed(helper="dead", output="absent")
    probes = []

    def unreadable(pid):
        probes.append(pid)
        return IdentityEvidence(pid=pid, state=IdentityState.unreadable)

    remover = DisplayRemover(rig.helper, rig.observer, rig.store, rig.lock, rig.clock,
                             process=rig.process, output_verifier=rig.verifier,
                             transport_released=rig.confirm_release, identity_probe=unreadable)
    assert isinstance(await remover.remove(OWNER, context()), Err)
    assert probes == [HELPER.pid] and await records(rig)
    remover = DisplayRemover(rig.helper, rig.observer, rig.store, rig.lock, rig.clock,
                             process=rig.process, output_verifier=rig.verifier,
                             transport_released=rig.confirm_release,
                             identity_probe=lambda pid: IdentityEvidence(pid=pid, state=IdentityState.absent))
    assert isinstance(await remover.remove(OWNER, context()), Ok)
    assert not await records(rig)


async def test_provider_cannot_return_handle_after_post_create_record_disappears(tmp_path):
    rig = Rig(tmp_path)
    provider = rig.provider()
    lookup = rig.remover.lookup

    async def missing(owner):
        await rig.store.delete(owner)
        return await lookup(owner)

    rig.remover.lookup = missing
    result = await provider.create("airplay-device:a", "endpoint-a", SIZE, context())
    assert isinstance(result, Err)
    assert any(event.kind == "cleanup_required" and event.handle is None for event in rig.events)


async def test_provider_revalidates_source_after_post_creation_verifier_await(tmp_path):
    rig = Rig(tmp_path)
    provider = rig.provider()
    verify = rig.remover.verify_output

    async def changed(*args, **kwargs):
        result = await verify(*args, **kwargs)
        rig.process.entries[1]["pos"] = {"x": 900, "y": 900}
        return result

    rig.remover.verify_output = changed
    result = await provider.create("airplay-device:a", "endpoint-a", SIZE, context())
    assert isinstance(result, Err)
    persisted, = await records(rig)
    assert persisted.desired_state == "removing" and persisted.stage == "removing"
    assert not rig.process.terminated


async def test_concurrent_removal_acknowledgements_are_idempotent(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    async with rig.lock.acquire():
        first = asyncio.create_task(rig.remover.remove(OWNER, context()))
        second = asyncio.create_task(rig.remover.remove(OWNER, context(generation=99)))
        for _ in range(10):
            await asyncio.sleep(0)
    outcomes = await asyncio.gather(first, second)
    assert all(isinstance(result, Ok) for result in outcomes)
    assert rig.process.terminated == [HELPER] and not await records(rig)
