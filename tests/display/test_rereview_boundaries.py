"""Late restoration and complete compositor evidence at the removal boundary."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from airplay_linux.domain.resources import Err, Ok
from airplay_linux.testing.builders import context
from test_removal_and_layout import HELPER, OWNER, Rig, no_live_kde, records


def at_configure_read(rig, change):
    """Interleave at the real CLI read, after restoration has selected values."""
    configure, run = rig.observer.configure, rig.process.run
    armed = changed = False

    async def guarded_configure(*args, **kwargs):
        nonlocal armed
        armed = True
        return await configure(*args, **kwargs)

    async def interleave(spec, timeout, stdin=None):
        nonlocal changed
        if armed and not changed and spec.argv == ("kscreen-doctor", "-j"):
            changed = True
            change()
        return await run(spec, timeout, stdin)

    rig.observer.configure, rig.process.run = guarded_configure, interleave
    return lambda: changed


@pytest.mark.parametrize("changed", [False, True])
async def test_restore_scale_equality_is_checked_at_configuration_read(tmp_path, changed):
    rig = Rig(tmp_path, durable=True)
    record = await rig.seed()
    rig.process.entries[0]["scale"] = 2
    await rig.store.write(replace(record, last_applied={"DP-1": {"scale": 2}}))
    activated = at_configure_read(
        rig, lambda: rig.process.entries[0].__setitem__("scale", 3 if changed else 2),
    )

    result = await rig.remover.remove(OWNER, context())

    assert activated()
    if changed:
        assert rig.process.entries[0]["scale"] == 3, "restoration overwrote a newer user scale"
        assert isinstance(result, Err) and await records(rig)
        assert not rig.process.commands
        assert not any(event.kind == "removed" for event in rig.events)
    else:
        assert rig.process.entries[0]["scale"] == 1
        assert isinstance(result, Ok) and not await records(rig)
        assert rig.events[-1].kind == "removed"


@pytest.mark.parametrize("changed", [False, True])
async def test_restore_mode_equality_is_checked_at_configuration_read(tmp_path, changed):
    rig = Rig(tmp_path, durable=True)
    record = await rig.seed()
    entry = rig.process.entries[0]
    entry["modes"].extend([
        {"id": "3", "size": {"width": 1920, "height": 1080}, "refreshRate": 75},
        {"id": "4", "size": {"width": 1920, "height": 1080}, "refreshRate": 120},
    ])
    entry["currentModeId"] = "3"
    await rig.store.write(replace(record, last_applied={"DP-1": {"mode": "3"}}))
    activated = at_configure_read(
        rig, lambda: entry.__setitem__("currentModeId", "4" if changed else "3"),
    )

    result = await rig.remover.remove(OWNER, context())

    assert activated()
    if changed:
        assert entry["currentModeId"] == "4", "restoration overwrote a newer user refresh mode"
        assert isinstance(result, Err) and await records(rig)
        assert not rig.process.commands
        assert not any(event.kind == "removed" for event in rig.events)
    else:
        assert entry["currentModeId"] == "1"
        assert isinstance(result, Ok) and not await records(rig)
        assert rig.events[-1].kind == "removed"


@pytest.mark.parametrize("stage", ["published", "reserved"])
@pytest.mark.parametrize("geometry", ["known", "unreadable"])
async def test_arrival_between_snapshot_and_outputs_retains_evidence(tmp_path, stage, geometry):
    rig = Rig(tmp_path, durable=True, verifier=False)
    await rig.seed(stage, output="absent")
    observe, run, stop = rig.remover.observe, rig.process.run, rig.helper.stop
    observations = final_reads = 0
    visible_at_signal = []
    arrived = False

    async def final_observe():
        nonlocal observations
        observations += 1
        return await observe()

    async def interleave(spec, timeout, stdin=None):
        nonlocal final_reads, arrived
        if observations == 2 and spec.argv == ("kscreen-doctor", "-j"):
            final_reads += 1
            if final_reads == 2:
                entry = rig.process.entry("7", "Virtual-owned")
                if geometry == "unreadable":
                    entry.pop("size")
                    entry["currentModeId"] = "0"
                rig.process.entries.append(entry)
                arrived = True
        return await run(spec, timeout, stdin)

    async def record_stop(*args, **kwargs):
        visible_at_signal.extend(deepcopy(rig.process.entries))
        return await stop(*args, **kwargs)

    rig.remover.observe, rig.process.run, rig.helper.stop = final_observe, interleave, record_stop

    result = await rig.remover.remove(OWNER, context())

    assert observations == 2 and arrived
    assert not visible_at_signal, "helper signaled with an output visible in the newer raw read"
    assert not rig.process.terminated
    assert isinstance(result, Err) and await records(rig)
    assert rig.process.alive(HELPER)
    assert any(entry["name"] == "Virtual-owned" for entry in rig.process.entries)
    assert not any(event.kind == "removed" for event in rig.events)


@pytest.mark.parametrize("when", ["before_restore", "after_readback"])
async def test_record_survives_until_every_touched_property_matches_baseline(tmp_path, when):
    rig = Rig(tmp_path, durable=True)
    record = await rig.seed()
    entry = rig.process.entries[0]
    entry["scale"] = 2
    await rig.store.write(replace(record, last_applied={"DP-1": {"scale": 2}}))

    if when == "before_restore":
        entry["scale"] = 3
    else:
        # Successful restore/readback cannot erase a later conflicting
        # observation at the final record-deletion boundary.
        restore = rig.remover._restore

        async def after_readback(*args):
            result = await restore(*args)
            assert isinstance(result, Ok)
            entry["scale"] = 3
            return result

        rig.remover._restore = after_readback

    result = await rig.remover.remove(OWNER, context())

    assert isinstance(result, Err)
    assert entry["scale"] == 3
    assert await records(rig)
    assert not any(event.kind == "removed" for event in rig.events)
    if when == "before_restore":
        assert not rig.process.commands
    # A retry can confirm a user-restored baseline without overwriting anything.
    entry["scale"] = 1
    if when == "after_readback":
        rig.remover._restore = restore
    assert isinstance(await rig.remover.remove(OWNER, context()), Ok)
    assert not await records(rig)
