"""Crash recovery matrix: no source becomes usable without all ownership evidence."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.testing.adapters import MemoryOwnershipStore
from airplay_linux.testing.builders import context
from test_removal_and_layout import HELPER, OWNER, Rig, no_live_kde, records


STAGES = ("reserved", "helper_started", "output_observed", "published", "removing")

# Expected outcomes derived from the brief. Rows are (helper, output), columns
# are the four active stages. A removing stage always follows removal intent.
ACTIVE = {
    ("alive", "absent"): ("cleanup_required",) * 4,
    ("alive", "exact"): ("recovering",) * 4,
    ("alive", "misconfigured"): ("recovering",) * 4,
    ("alive", "ambiguous"): ("cleanup_required",) * 4,
    ("dead", "absent"): ("discarded", "cleanup_required", "cleanup_required", "cleanup_required"),
    ("dead", "exact"): ("cleanup_required", "cleanup_required", "orphan", "orphan"),
    ("dead", "misconfigured"): ("cleanup_required", "cleanup_required", "orphan", "orphan"),
    ("dead", "ambiguous"): ("cleanup_required",) * 4,
    ("reused", "absent"): ("discarded", "cleanup_required", "cleanup_required", "cleanup_required"),
    ("reused", "exact"): ("cleanup_required",) * 4,
    ("reused", "misconfigured"): ("cleanup_required",) * 4,
    ("reused", "ambiguous"): ("cleanup_required",) * 4,
    ("inaccessible", "absent"): ("cleanup_required",) * 4,
    ("inaccessible", "exact"): ("cleanup_required",) * 4,
    ("inaccessible", "misconfigured"): ("cleanup_required",) * 4,
    ("inaccessible", "ambiguous"): ("cleanup_required",) * 4,
}


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("desired", ("active", "removing"))
@pytest.mark.parametrize("helper", ("alive", "dead", "reused", "inaccessible"))
@pytest.mark.parametrize("output", ("absent", "exact", "misconfigured", "ambiguous"))
async def test_reconcile_every_stage_and_evidence_combination(tmp_path, stage, desired, helper, output):
    rig = Rig(tmp_path)
    record = await rig.seed(stage, desired, helper, output)
    items = await rig.recovery().reconcile()
    assert len(items) == 1
    item = items[0]
    assert item.record.owner_uuid == record.owner_uuid
    if desired == "removing" or stage == "removing":
        assert item.kind in {"removing", "cleanup_required"}
        assert item.handle is None
        assert all(r.stage == "removing" and r.desired_state == "removing" for r in await records(rig))
        assert not rig.process.commands
    else:
        assert item.kind == ACTIVE[(helper, output)][STAGES.index(stage)]
        if item.kind == "recovering":
            assert item.handle is not None and item.handle.helper == HELPER
            persisted = (await records(rig))[0]
            assert persisted.stage == "published" and persisted.desired_state == "active"
            assert persisted.helper == HELPER and persisted.output_id == "7"
            assert item.handle.source.size == record.requested_size
            actual = (await rig.observer.outputs()).value[-1]
            assert actual.enabled and actual.position == (0, 0) and actual.size == record.requested_size
        else:
            assert item.handle is None
        if helper != "alive" or output == "ambiguous":
            assert not rig.process.terminated and not rig.process.commands
    # A recycled/inaccessible identity is never signalled, even in removing intent.
    if helper in {"reused", "inaccessible"}:
        assert not rig.process.terminated


async def test_launch_before_pid_persistence_records_exact_start_before_repair(tmp_path):
    rig = Rig(tmp_path, durable=True)
    await rig.seed("reserved")
    observed_stages = []
    write = rig.store.write

    async def trace(record):
        result = await write(record)
        observed_stages.append((record.stage, record.helper))
        return result

    rig.store.write = trace
    item, = await rig.recovery().reconcile()
    assert item.kind == "recovering"
    assert observed_stages[0] == ("helper_started", HELPER)
    assert observed_stages[-1] == ("published", HELPER)


async def test_no_records_leaves_unrelated_outputs_and_helpers_alone(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    await rig.store.delete(OWNER)
    assert await rig.recovery().reconcile() == ()
    assert rig.process.alive(HELPER) and len(rig.process.entries) == 2
    assert not rig.process.commands and not rig.process.terminated


async def test_empty_world_has_no_recovery_items(tmp_path):
    rig = Rig(tmp_path)
    assert await rig.recovery().reconcile() == ()
    assert not rig.process.commands and not rig.process.terminated


async def test_live_helper_without_output_waits_five_seconds_then_cleans_exact_helper(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("helper_started", output="absent")
    item, = await rig.recovery().reconcile()
    assert item.kind == "cleanup_required" and item.handle is None
    assert rig.observer.deadlines[0] == 5.0
    assert rig.process.terminated == [HELPER]
    assert all(r.stage == "removing" for batch in rig.process.stop_records for r in batch)


async def test_reserved_record_not_discarded_when_complete_snapshot_has_unknown_output(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("reserved", helper="dead", output="absent")
    entry = rig.process.entry("7", "Virtual-unknown")
    entry.pop("size")
    entry["currentModeId"] = "0"
    rig.process.entries.append(entry)
    assert len((await rig.observer.outputs()).value) == 1
    item, = await rig.recovery().reconcile()
    assert item.kind == "cleanup_required" and await records(rig)


@pytest.mark.parametrize("stage", ("helper_started", "output_observed", "published"))
async def test_association_authority_is_required_for_recovery(tmp_path, stage):
    rig = Rig(tmp_path, verifier=False)
    await rig.seed(stage)
    item, = await rig.recovery().reconcile()
    assert item.kind == "cleanup_required" and item.handle is None
    assert item.diagnostic.code == ErrorCode.unsupported_operation
    assert not rig.process.commands and not rig.process.terminated


@pytest.mark.parametrize("stage", ("reserved", "helper_started", "output_observed", "published"))
@pytest.mark.parametrize("step", MemoryOwnershipStore.STEPS)
async def test_failed_recovery_write_never_publishes_usable_handle(tmp_path, stage, step):
    rig = Rig(tmp_path)
    await rig.seed(stage, output="misconfigured")
    rig.store.inject_failure(step)
    item, = await rig.recovery().reconcile()
    assert item.kind == "cleanup_required" and item.handle is None
    assert await records(rig)


async def test_removing_intent_waits_for_transport_confirmation_on_restart(tmp_path):
    rig = Rig(tmp_path, release=False)
    await rig.seed("helper_started", "removing")
    item, = await rig.recovery().reconcile()
    assert item.kind in {"removing", "cleanup_required"} and item.handle is None
    assert rig.process.alive(HELPER) and not rig.process.terminated
    rig.released = True
    item, = await rig.recovery().reconcile()
    assert item.handle is None and not rig.process.alive(HELPER)
    assert not await records(rig)


async def test_recovery_configuration_is_under_provider_lock(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("output_observed", output="misconfigured")
    async with rig.lock.acquire():
        task = asyncio.create_task(rig.recovery().reconcile())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not rig.process.commands and not task.done()
    item, = await task
    assert item.kind == "recovering"


async def test_removal_during_repair_cannot_be_overwritten_by_publication(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("output_observed", output="misconfigured")
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.process.verify_output

    async def held(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    recovery = DisplayRecovery(rig.helper, rig.observer, rig.store, rig.lock, rig.clock, rig.remover,
                               output_verifier=held)
    task = asyncio.create_task(recovery.reconcile())
    await asyncio.wait_for(entered.wait(), 1)
    marked = asyncio.create_task(rig.remover.mark_removing(OWNER))
    for _ in range(10):
        await asyncio.sleep(0)
    release.set()
    item, = await asyncio.wait_for(task, 1)
    assert isinstance(await marked, Ok)
    assert item.handle is None and item.kind != "recovering"
    assert all(r.desired_state == "removing" for r in await records(rig))
    assert not rig.process.commands


async def test_identity_change_during_association_await_blocks_configuration(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed("output_observed", output="misconfigured")
    original = rig.process.verify_output

    async def change_identity(*args):
        result = await original(*args)
        rig.process.register(replace(HELPER, start_ticks=9999), {})
        return result

    recovery = DisplayRecovery(rig.helper, rig.observer, rig.store, rig.lock, rig.clock, rig.remover,
                               output_verifier=change_identity)
    item, = await recovery.reconcile()
    assert item.kind == "cleanup_required" and item.handle is None
    assert not rig.process.commands and not rig.process.terminated


async def test_helper_absence_is_rechecked_at_removal_record_deletion(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    snapshot = rig.observer.snapshot

    async def late_helper():
        result = await snapshot()
        if rig.process.terminated:
            rig.process.register(HELPER, rig.process.owner_environment(OWNER, "/run/user/1000/airplay-linux/instances/test-display"))
        return result

    rig.observer.snapshot = late_helper
    assert isinstance(await rig.remover.remove(OWNER, context()), Err)
    assert await records(rig)


async def test_changed_geometry_during_last_verification_never_yields_handle(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    original = rig.process.verify_output
    calls = 0

    async def changed(*args):
        nonlocal calls
        result = await original(*args)
        calls += 1
        # The last verification follows publication's awaited durable write.
        if calls == 4:
            rig.process.entries[1].update(pos={"x": 400, "y": 300})
        return result

    recovery = DisplayRecovery(rig.helper, rig.observer, rig.store, rig.lock, rig.clock, rig.remover,
                               output_verifier=changed)
    item, = await recovery.reconcile()
    assert calls == 4
    assert item.kind == "cleanup_required" and item.handle is None


async def test_removing_admission_wins_suspended_active_publication(tmp_path):
    rig = Rig(tmp_path)
    await rig.seed()
    write = rig.store.write
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(record):
        if record.stage == "published" and record.desired_state == "active":
            entered.set()
            await release.wait()
        return await write(record)

    rig.store.write = held
    task = asyncio.create_task(rig.recovery().reconcile())
    await asyncio.wait_for(entered.wait(), 1)
    removing = asyncio.create_task(rig.remover.mark_removing(OWNER))
    for _ in range(10):
        await asyncio.sleep(0)
    assert not removing.done()
    release.set()
    item, = await asyncio.wait_for(task, 1)
    assert isinstance(await removing, Ok)
    assert item.handle is None and item.kind == "cleanup_required"
    assert (await records(rig))[0].desired_state == "removing"


async def test_store_read_failure_is_visible_to_composition(tmp_path):
    rig = Rig(tmp_path)
    rig.store.queue_result("all", Err(error=diagnostic(ErrorCode.persistence_failed)))
    recovery = rig.recovery()
    assert await recovery.reconcile() == ()
    assert recovery.last_diagnostic.code == ErrorCode.persistence_failed
