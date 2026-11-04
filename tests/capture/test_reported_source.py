"""Pure reported-source provenance checks over one KScreen observation."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from airplay_linux.backend.contract import BackendSource
from airplay_linux.capture.source import (
    SourceRefusal,
    SourceVerificationRefusal,
    verify_reported_source,
)
from airplay_linux.display.inventory import (
    KScreenInventory,
    KScreenReportedSourceVerifier,
    MonitoredOutput,
)
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.domain.resources import Err, Ok, Size


def _output(
    name: str = "DP-1", *, enabled: bool | None = True, connected: bool = True,
    output_type: int | str | None = 14, size: Size | None = Size(width=1920, height=1080),
    position: tuple[int, int] | None = (0, 0), virtual: bool | None = False,
    scale: float | None = 1.0, output_id: str | None = "1",
) -> MonitoredOutput:
    return MonitoredOutput(
        name=name, enabled=enabled, connected=connected, output_type=output_type,
        size=size, position=position, virtual=virtual, scale=scale, output_id=output_id,
    )


def _reported(
    x: int = 0, y: int = 0, *, width: int = 1920, height: int = 1080,
    source_type: int = 1, has_position: bool = True,
) -> BackendSource:
    return BackendSource(
        type=source_type, width=width, height=height, x=x, y=y,
        has_position=has_position,
    )


def _reason(result: object) -> SourceRefusal:
    assert isinstance(result, SourceVerificationRefusal)
    assert result.observation.reason is not None
    return result.observation.reason


def test_same_size_other_position_is_not_requested_source():
    result = verify_reported_source("DP-1", _reported(), (_output(position=(100, 0)),))
    assert isinstance(result, Err)
    assert _reason(result) is SourceRefusal.NO_POSITION_MATCH


def test_enabled_foreign_or_virtual_geometry_collision_refuses():
    requested = _reported()
    for duplicate in (
        _output("HDMI-1"),
        _output("DP-2", virtual=True),
        _output("DP-3", virtual=None),
    ):
        result = verify_reported_source("DP-1", requested, (_output(), duplicate))
        assert isinstance(result, Err)
        assert _reason(result) is SourceRefusal.AMBIGUOUS_POSITION


def _enabled_field_document(*, duplicate_enabled=None, include_duplicate_enabled=True):
    def output(name: str, *, enabled: bool | None = None, include_enabled: bool = True):
        record = {
            "id": 1 if name == "DP-1" else 2, "name": name, "connected": True,
            "type": 14 if name == "DP-1" else 6,
            "pos": {"x": 0, "y": 0},
            "size": {"width": 1920, "height": 1080}, "scale": 1,
        }
        if include_enabled:
            record["enabled"] = enabled
        return record

    return {"outputs": [
        output("DP-1", enabled=True),
        output(
            "HDMI-1", enabled=duplicate_enabled,
            include_enabled=include_duplicate_enabled,
        ),
    ]}


def test_missing_enabled_field_survives_inventory_projection_and_refuses():
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(_InventoryProcess(_enabled_field_document(
            include_duplicate_enabled=False,
        ))),
        physical_inventory_available=True,
    )
    result = asyncio.run(verifier.verify("DP-1", _reported()))
    assert _reason(result) is SourceRefusal.UNREADABLE_LAYOUT


def test_disabled_geometry_match_does_not_create_ambiguity():
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(_InventoryProcess(_enabled_field_document(
            duplicate_enabled=False,
        ))),
        physical_inventory_available=True,
    )
    result = asyncio.run(verifier.verify("DP-1", _reported()))
    assert isinstance(result, Ok)
    assert result.value.matched_connector == "DP-1"


def test_enabled_disconnected_or_unknown_geometry_collision_refuses():
    requested = _reported()
    for duplicate in (
        _output("DP-2", connected=False),
        _output("DP-3", output_type="Unknown", virtual=None),
    ):
        result = verify_reported_source("DP-1", requested, (_output(), duplicate))
        assert isinstance(result, Err)
        assert _reason(result) is SourceRefusal.AMBIGUOUS_POSITION


def test_negative_coordinate_physical_layout_is_accepted():
    result = verify_reported_source(
        "DP-1", _reported(x=-1920, y=-1080),
        (_output(position=(-1920, -1080)),),
    )
    assert isinstance(result, Ok)


def test_stale_unreadable_or_unknown_origin_layout_refuses():
    for output in (
        _output(size=None),
        _output(position=None),
        _output("Unknown-1", output_type=None, virtual=None),
    ):
        result = verify_reported_source("DP-1", _reported(), (output,))
        assert isinstance(result, Err)


def test_scaled_portal_size_corroborates_current_mode_size():
    """The portal reports device-independent pixels; KScreen reports the mode."""
    result = verify_reported_source(
        "DP-1", _reported(width=1280, height=720),
        (_output(scale=1.5),),
    )
    assert isinstance(result, Ok)
    assert result.value.reported.width == 1280
    assert result.value.matched_connector == "DP-1"


def test_inverted_scaled_portal_size_is_refused():
    result = verify_reported_source(
        "DP-1", _reported(width=2880, height=1620),
        (_output(scale=1.5),),
    )
    assert isinstance(result, Err)
    assert _reason(result) is SourceRefusal.SIZE_MISMATCH


def test_shared_position_is_ambiguous_even_when_one_candidate_is_requested():
    result = verify_reported_source(
        "DP-1", _reported(),
        (_output(), _output("HDMI-1", output_id="2")),
    )
    assert isinstance(result, Err)
    assert _reason(result) is SourceRefusal.AMBIGUOUS_POSITION


def test_source_refusals_have_distinct_reasons():
    cases = (
        (_reported(source_type=2), (_output(),), SourceRefusal.NON_MONITOR_SOURCE),
        (_reported(has_position=False), (_output(),), SourceRefusal.MISSING_POSITION),
        (_reported(), (_output(virtual=True),), SourceRefusal.VIRTUAL_OUTPUT),
        (_reported(), (_output("HDMI-1"),), SourceRefusal.DIFFERENT_CONNECTOR),
    )
    for reported, layout, expected in cases:
        assert _reason(verify_reported_source("DP-1", reported, layout)) is expected


def test_helper_owned_output_is_refused():
    owned = SimpleNamespace(desired_state="active", connector="DP-1", output_id="1")
    result = verify_reported_source(
        "DP-1", _reported(), (_output(),), owned_outputs=(owned,),
    )
    assert isinstance(result, Err)
    assert _reason(result) is SourceRefusal.OWNED_OUTPUT


class _InventoryProcess:
    def __init__(self, document):
        self.document = document
        self.calls = []
        self.spawn_calls = 0

    async def run(self, spec: CommandSpec, timeout: float, stdin=None):
        self.calls.append((spec, timeout))
        return Ok(value=CommandOutput(
            exit_code=0, stdout=json.dumps(self.document).encode(), stderr=b"",
        ))

    async def spawn(self, spec):
        self.spawn_calls += 1
        raise AssertionError("source observation must never spawn a stream")


class _Ownerships:
    def __init__(self, records=()):
        self.records = tuple(records)

    async def all(self):
        return Ok(value=self.records)


def _inventory_document(*entries):
    return {"outputs": [
        {
            "id": index + 1, "name": name, "enabled": enabled,
            "connected": connected, "type": output_type,
            "pos": {"x": position[0], "y": position[1]},
            "size": {"width": size[0], "height": size[1]}, "scale": scale,
        }
        for index, (name, enabled, connected, output_type, position, size, scale)
        in enumerate(entries)
    ]}


def test_non_object_output_refuses_partial_layout_through_inventory():
    document = _inventory_document(
        ("DP-1", True, True, 14, (0, 0), (1920, 1080), 1),
    )
    document["outputs"].append("unreadable-entry")
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(_InventoryProcess(document)),
        physical_inventory_available=True,
    )

    result = asyncio.run(verifier.verify("DP-1", _reported()))

    assert _reason(result) is SourceRefusal.INVENTORY_UNAVAILABLE
    assert verifier.last_observation is not None
    assert not verifier.last_observation.accepted
    assert verifier.last_observation.matched_connector is None


def test_two_stream_observations_are_independent_and_typed():
    process = _InventoryProcess(_inventory_document(
        ("DP-1", True, True, 14, (0, 0), (1920, 1080), 1),
        ("HDMI-1", True, True, 6, (1920, 0), (1920, 1080), 1),
    ))
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(process), physical_inventory_available=True,
    )
    wrong = asyncio.run(verifier.verify("DP-1", _reported(x=1920)))
    right = asyncio.run(verifier.verify("DP-1", _reported()))
    assert _reason(wrong) is SourceRefusal.DIFFERENT_CONNECTOR
    assert isinstance(right, Ok)
    assert right.value.reported == _reported()
    assert right.value.matched_connector == "DP-1"


def test_owned_output_is_refused_by_async_verifier_without_spawning():
    process = _InventoryProcess(_inventory_document(
        ("DP-1", True, True, 14, (0, 0), (1920, 1080), 1),
    ))
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(process), physical_inventory_available=True,
        ownerships=_Ownerships((SimpleNamespace(
            desired_state="active", connector="DP-1", output_id="1",
        ),)),
    )
    result = asyncio.run(verifier.verify("DP-1", _reported()))
    assert _reason(result) is SourceRefusal.OWNED_OUTPUT
    assert process.spawn_calls == 0


def test_observation_never_blocks_a_stream_or_spawns():
    process = _InventoryProcess(_inventory_document(
        ("DP-1", True, True, 14, (0, 0), (1920, 1080), 1),
    ))
    verifier = KScreenReportedSourceVerifier(
        KScreenInventory(process), physical_inventory_available=True,
    )
    result = asyncio.run(asyncio.wait_for(
        verifier.verify("DP-1", _reported()), timeout=0.5,
    ))
    assert isinstance(result, Ok)
    assert result.value.matched_connector == "DP-1"
    assert process.spawn_calls == 0


def test_capture_physical_reexports_shared_output_whitelists():
    from airplay_linux.capture import physical
    from airplay_linux.display import inventory

    assert physical.PHYSICAL_OUTPUT_TYPES is inventory.PHYSICAL_OUTPUT_TYPES
    assert physical.PHYSICAL_OUTPUT_TYPE_NAMES is inventory.PHYSICAL_OUTPUT_TYPE_NAMES
