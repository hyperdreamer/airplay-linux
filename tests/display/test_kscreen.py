"""KScreen observation is read-only, strict, and never invents a layout.

Every test drives the adapter through :class:`FakeProcess`; none of them runs the
installed ``kscreen-doctor``. The fixtures are modelled on the installed
``kscreen-doctor -j`` document, including the numeric output/mode ids, a
disconnected-but-describable output, a preexisting unrelated virtual output, and
a mode list with unusable entries.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.kscreen import (
    KScreenMode,
    KScreenObserver,
    KScreenParseError,
    OutputInfo,
    layout_hash,
    parse_outputs,
    select_mode,
)
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.testing.adapters import FakeProcess
from airplay_linux.testing.time import FakeClock, FakeScheduler

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "display"
BASELINE = "kscreen-baseline.json"
TWO_VIRTUAL = "kscreen-two-virtual.json"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def command_output(
    raw: object, exit_code: int = 0, stderr: bytes = b"",
) -> Ok[CommandOutput]:
    return Ok(
        value=CommandOutput(
            exit_code=exit_code, stdout=json.dumps(raw).encode("utf-8"), stderr=stderr,
        ),
    )


def observer(
    process: FakeProcess | None = None, *, clock: FakeClock | None = None, **options: Any,
) -> tuple[KScreenObserver, FakeProcess, FakeClock, FakeScheduler]:
    fake = process or FakeProcess()
    fake_clock = clock or FakeClock()
    scheduler = FakeScheduler(fake_clock)
    return KScreenObserver(fake, fake_clock, scheduler, **options), fake, fake_clock, scheduler


def with_output(raw: dict[str, Any], output_id: str, **changes: Any) -> dict[str, Any]:
    """A copy of a `kscreen-doctor -j` document with one output entry changed."""
    updated = json.loads(json.dumps(raw))
    for entry in updated["outputs"]:
        if str(entry["id"]) == output_id:
            entry.update(changes)
            return updated
    raise AssertionError(f"fixture has no output {output_id}")


def reversed_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return dict(reversed(list(value.items())))


# --------------------------------------------------------------------------- parsing


def test_baseline_fixture_has_unique_output_ids():
    raw = json.loads((FIXTURES / BASELINE).read_text())
    outputs = parse_outputs(raw)
    assert len({o.output_id for o in outputs}) == len(outputs)
    assert any(o.connector == "DP-1" for o in outputs)


def test_baseline_reports_physical_geometry():
    (monitor,) = parse_outputs(fixture(BASELINE))
    assert monitor.output_id == "1"
    assert monitor.connector == "DP-1"
    assert monitor.size == Size(width=1920, height=1080)
    assert monitor.enabled is True
    assert monitor.position == (0, 0)
    assert monitor.scale == 1.0
    assert monitor.virtual is False


def test_two_virtual_fixture_retains_dp1_and_distinct_virtual_outputs():
    outputs = parse_outputs(fixture(TWO_VIRTUAL))
    by_connector = {output.connector: output for output in outputs}
    assert set(by_connector) == {"DP-1", "Virtual-1", "Virtual-2"}
    assert by_connector["DP-1"].virtual is False
    assert by_connector["Virtual-1"].virtual is True
    assert by_connector["Virtual-2"].virtual is True
    virtual_ids = {
        by_connector["Virtual-1"].output_id,
        by_connector["Virtual-2"].output_id,
    }
    assert len(virtual_ids) == 2
    assert by_connector["Virtual-1"].size == Size(width=1920, height=1080)
    assert by_connector["Virtual-2"].size == Size(width=1920, height=1080)


def test_disabled_output_keeps_its_geometry_and_is_reported_disabled():
    raw = with_output(fixture(BASELINE), "1", enabled=False)
    (monitor,) = parse_outputs(raw)
    assert monitor.enabled is False
    assert monitor.size == Size(width=1920, height=1080)


def test_malformed_modes_and_output_ids_are_never_used_as_evidence():
    raw = {
        "outputs": [
            {
                "id": None,
                "name": "DP-9",
                "enabled": True,
                "pos": {"x": 0, "y": 0},
                "scale": 1,
                "size": {"width": 1920, "height": 1080},
                "modes": [],
            },
            {
                "id": 4,
                "name": "Virtual-4",
                "enabled": True,
                "pos": {"x": 0, "y": 0},
                "scale": 1,
                "size": {"width": 1920, "height": 1080},
                "currentModeId": "bad-mode",
                "modes": [
                    {"id": None, "size": {"width": 1920, "height": 1080}},
                    {"id": "9", "size": {"width": 0, "height": 1080}},
                    {"id": "7", "size": {"width": 1920, "height": 1080}, "refreshRate": 60},
                ],
            },
        ],
    }
    outputs = parse_outputs(raw)
    assert [output.output_id for output in outputs] == ["4"]
    selected = select_mode(
        # The public helper receives only modes the parser could trust.
        (_mode("7", 1920, 1080, 60),), Size(width=1920, height=1080),
    )
    assert selected is not None and selected.mode_id == "7"


def test_unavailable_size_is_never_invented():
    raw = {
        "outputs": [
            {
                "id": 7,
                "name": "Virtual-7",
                "enabled": False,
                "connected": False,
                "pos": {"x": 0, "y": 0},
                "scale": 1,
                "size": {"width": 0, "height": 0},
                "modes": [],
            },
        ],
    }
    assert parse_outputs(raw) == ()
    observed, _fake, _clock, _scheduler = observer()
    _fake.queue_result("run", command_output(raw))
    snapshot = asyncio.run(observed.snapshot())
    assert isinstance(snapshot, Ok)
    assert snapshot.value.outputs["Virtual-7"]["enabled"] is False
    assert "width" not in snapshot.value.outputs["Virtual-7"]
    assert "height" not in snapshot.value.outputs["Virtual-7"]


def test_duplicate_output_ids_are_rejected_as_ambiguous_identity():
    raw = fixture(BASELINE)
    duplicate = json.loads(json.dumps(raw["outputs"][0]))
    duplicate["name"] = "DP-2"
    raw["outputs"].append(duplicate)
    with pytest.raises(KScreenParseError):
        parse_outputs(raw)


def test_duplicate_connector_names_are_rejected_as_ambiguous_layout():
    raw = fixture(BASELINE)
    duplicate = json.loads(json.dumps(raw["outputs"][0]))
    duplicate["id"] = 2
    raw["outputs"].append(duplicate)
    with pytest.raises(KScreenParseError):
        parse_outputs(raw)


def test_non_object_document_is_rejected():
    with pytest.raises(KScreenParseError):
        parse_outputs([])
    with pytest.raises(KScreenParseError):
        parse_outputs({"outputs": "DP-1"})


def test_unknown_connector_name_is_not_assumed_virtual():
    raw = with_output(fixture(BASELINE), "1", name="Monitor-1", type=0)
    (output,) = parse_outputs(raw)
    assert output.virtual is None


def test_two_new_outputs_are_visible_against_the_baseline():
    baseline_ids = {output.output_id for output in parse_outputs(fixture(BASELINE))}
    observed = parse_outputs(fixture(TWO_VIRTUAL))
    appeared = [output for output in observed if output.output_id not in baseline_ids]
    assert [output.connector for output in appeared] == ["Virtual-1", "Virtual-2"]
    assert all(output.virtual is True for output in appeared)
    assert all(output.size == Size(width=1920, height=1080) for output in appeared)


def test_preexisting_virtual_output_keeps_its_identity_in_the_baseline():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    snapshot = asyncio.run(observed.snapshot())
    assert isinstance(snapshot, Ok)
    assert set(snapshot.value.outputs) == {"DP-1", "Virtual-1", "Virtual-2"}


@pytest.mark.parametrize("current_mode", [None, "bad-mode", "999"])
def test_unknown_current_geometry_is_not_taken_from_advertised_modes(current_mode):
    """An advertised or preferred mode is never evidence of the current size."""
    raw = with_output(
        fixture(BASELINE), "1", size={"width": 0, "height": 0}, currentModeId=current_mode,
    )
    assert parse_outputs(raw) == ()
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    result = asyncio.run(observed.outputs())
    assert isinstance(result, Ok) and result.value == ()


def test_current_geometry_comes_from_the_exact_current_mode_id():
    raw = with_output(fixture(BASELINE), "1", size={"width": 0, "height": 0})
    (monitor,) = parse_outputs(raw)
    assert monitor.size == Size(width=1920, height=1080)


def test_snapshot_keeps_identity_when_current_geometry_is_unknown():
    raw = with_output(
        fixture(BASELINE), "1", size={"width": 0, "height": 0}, currentModeId="999",
    )
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    snapshot = asyncio.run(observed.snapshot())
    assert isinstance(snapshot, Ok)
    properties = snapshot.value.outputs["DP-1"]
    assert properties["id"] == "1"
    assert properties["enabled"] is True
    assert properties["position"] == "0,0"
    assert properties["scale"] == 1
    assert "width" not in properties and "height" not in properties
    assert "size" not in properties


def test_duplicate_mode_ids_reject_the_whole_document():
    raw = fixture(BASELINE)
    duplicate = json.loads(json.dumps(raw["outputs"][0]["modes"][0]))
    duplicate["size"] = {"width": 1280, "height": 720}
    raw["outputs"][0]["modes"].append(duplicate)
    with pytest.raises(KScreenParseError):
        parse_outputs(raw)
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    result = asyncio.run(observed.outputs())
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


@pytest.mark.parametrize("field", ["scale", "refreshRate"])
def test_malformed_numeric_backend_fields_return_a_typed_failure(field):
    """A JSON number the runtime cannot convert never escapes as an exception."""
    raw = fixture(BASELINE)
    if field == "scale":
        raw["outputs"][0]["scale"] = 10**400
    else:
        raw["outputs"][0]["modes"][0]["refreshRate"] = 10**400
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    result = asyncio.run(observed.outputs())
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


# --------------------------------------------------------------------------- snapshot


def test_snapshot_reports_flat_scalar_properties_for_restoration():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    snapshot = asyncio.run(observed.snapshot())
    assert isinstance(snapshot, Ok)
    assert snapshot.value.outputs["Virtual-1"] == {
        "id": "5",
        "enabled": True,
        "width": 1920,
        "height": 1080,
        "x": 1920,
        "y": 0,
        "position": "1920,0",
        "scale": 1,
        "size": "1920x1080",
        "mode": "51",
    }
    assert snapshot.value.outputs["DP-1"]["x"] == 0


def test_baseline_hash_uses_canonical_sorted_json():
    forward = {
        "DP-1": {"x": 0, "y": 0, "width": 1920, "height": 1080, "scale": 1, "enabled": True},
        "Virtual-1": {"x": 1920, "y": 0, "width": 1920, "height": 1080, "scale": 1},
    }
    reordered = {key: reversed_mapping(value) for key, value in reversed_mapping(forward).items()}
    assert layout_hash(forward) == layout_hash(reordered)
    changed = json.loads(json.dumps(forward))
    changed["DP-1"]["scale"] = 2
    assert layout_hash(forward) != layout_hash(changed)


def test_snapshot_hash_is_stable_when_the_document_key_order_changes():
    first, first_process, _clock, _scheduler = observer()
    second, second_process, _clock, _scheduler = observer()
    raw = fixture(TWO_VIRTUAL)
    shuffled = {
        "tabletModeEngaged": raw["tabletModeEngaged"],
        "screen": reversed_mapping(raw["screen"]),
        "outputs": [
            reversed_mapping({**entry, "pos": reversed_mapping(entry["pos"])})
            for entry in reversed(raw["outputs"])
        ],
    }
    first_process.queue_result("run", command_output(raw))
    second_process.queue_result("run", command_output(shuffled))
    first_snapshot = asyncio.run(first.snapshot())
    second_snapshot = asyncio.run(second.snapshot())
    assert isinstance(first_snapshot, Ok) and isinstance(second_snapshot, Ok)
    assert first_snapshot.value.baseline_hash == second_snapshot.value.baseline_hash


def test_snapshot_adapter_errors_are_typed_and_secret_free():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result(
        "run", Err(error=diagnostic(ErrorCode.dependency_missing, command="kscreen-doctor")),
    )
    result = asyncio.run(observed.snapshot())
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_missing
    assert "kscreen-doctor" in result.error.message


def test_outputs_rejects_malformed_json_and_nonzero_exits():
    malformed, malformed_process, _clock, _scheduler = observer()
    malformed_process.queue_result(
        "run", Ok(value=CommandOutput(exit_code=0, stdout=b"not json", stderr=b"")),
    )
    failed = asyncio.run(malformed.outputs())
    assert isinstance(failed, Err) and failed.error.code == ErrorCode.configuration_failed

    nonzero, nonzero_process, _clock, _scheduler = observer()
    nonzero_process.queue_result(
        "run", Ok(value=CommandOutput(exit_code=1, stdout=b"{}", stderr=b"error")),
    )
    rejected = asyncio.run(nonzero.outputs())
    assert isinstance(rejected, Err) and rejected.error.code == ErrorCode.configuration_failed


def test_observer_rejects_a_command_outside_the_diagnostics_allowlist():
    with pytest.raises(ValueError):
        KScreenObserver(FakeProcess(), FakeClock(), FakeScheduler(FakeClock()), command="sh")


async def test_wait_for_rejects_an_unbounded_deadline_without_reading():
    observed, fake, _clock, _scheduler = observer()
    result = await observed.wait_for(lambda outputs: True, float("nan"))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.invalid_input
    assert fake.runs == []


def test_snapshot_reads_with_a_bounded_timeout():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    asyncio.run(observed.snapshot())
    spec, timeout = fake.runs[0]
    assert spec.argv == ("kscreen-doctor", "-j")
    assert dict(spec.env) == {}
    assert timeout == 5.0


# --------------------------------------------------------------------------- configure


def test_configure_uses_reported_mode_id_and_reads_the_layout_back():
    raw = fixture(BASELINE)
    updated = with_output(
        raw, "1", enabled=True, pos={"x": 1920, "y": 0}, scale=1,
        currentModeId="34",
    )
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(updated))
    result = asyncio.run(
        observed.configure(
            "1",
            {
                "enabled": True,
                "size": Size(width=1920, height=1080),
                "position": "1920,0",
                "scale": 1,
            },
        ),
    )
    assert isinstance(result, Ok)
    command = fake.runs[1][0]
    assert command.argv == (
        "kscreen-doctor",
        "output.1.enable",
        "output.1.mode.34",
        "output.1.position.1920,0",
        "output.1.scale.1",
    )
    assert not any("1920x1080" in token for token in command.argv)
    assert dict(command.env) == {}


def test_configure_prefers_the_reported_preferred_mode_for_a_size():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"size": "1920x1080"}))
    assert isinstance(result, Ok)
    assert "output.1.mode.34" in fake.runs[1][0].argv


def test_configure_accepts_an_explicit_reported_mode_id():
    updated = with_output(fixture(BASELINE), "1", currentModeId="35")
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(updated))
    result = asyncio.run(observed.configure("1", {"mode": "35"}))
    assert isinstance(result, Ok)
    assert "output.1.mode.35" in fake.runs[1][0].argv


def test_configure_rejects_an_unreported_mode_id_without_running_it():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"mode": "999"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.unsupported_mode
    assert len(fake.runs) == 1


def test_configure_rejects_a_size_the_output_does_not_advertise():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"size": Size(width=3840, height=2160)}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.unsupported_mode
    assert len(fake.runs) == 1


def test_configure_rejects_unknown_settings_and_names_without_running_anything():
    observed, fake, _clock, _scheduler = observer()
    result = asyncio.run(observed.configure("1", {"brightness": 1}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.invalid_input
    assert fake.runs == []


def test_configure_rejects_argv_injection_and_malformed_values():
    observed, fake, _clock, _scheduler = observer()
    for values in (
        {"mode": "34 --help"},
        {"mode": "1.enable"},
        {"position": "1,2,3"},
        {"scale": float("nan")},
        {"scale": "not-a-scale"},
        {"scale": 10**400},
    ):
        result = asyncio.run(observed.configure("1", values))
        assert isinstance(result, Err), values
        assert result.error.code == ErrorCode.invalid_input
    assert fake.runs == []


def test_configure_verifies_the_readback_and_fails_when_it_does_not_match():
    raw = fixture(BASELINE)
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    # The readback still reports the old position, so the change is not proven.
    fake.queue_result("run", command_output(raw))
    result = asyncio.run(observed.configure("1", {"position": "1920,0"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


def test_configure_reports_a_failed_kscreen_command():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=2, stdout=b"", stderr=b"denied")))
    result = asyncio.run(observed.configure("1", {"enabled": True}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed
    assert len(fake.runs) == 2


def test_configure_reports_an_output_that_disappeared_before_configuration():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("42", {"enabled": True}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed
    assert len(fake.runs) == 1


def test_configure_accepts_negative_positions_and_fractional_scale():
    updated = with_output(
        fixture(BASELINE), "1", pos={"x": -128, "y": 40}, scale=1.5,
    )
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(updated))
    result = asyncio.run(
        observed.configure("1", {"position": (-128, 40), "scale": "1.5"}),
    )
    assert isinstance(result, Ok)
    assert "output.1.position.-128,40" in fake.runs[1][0].argv
    assert "output.1.scale.1.5" in fake.runs[1][0].argv


def test_configure_rejects_conflicting_mode_and_size():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    result = asyncio.run(observed.configure("5", {"mode": "51", "size": "1280x720"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.invalid_input


def test_configure_cannot_select_a_mode_with_an_invalid_id():
    """A mode entry KScreen cannot name is dropped, never given an invented id."""
    raw = fixture(BASELINE)
    raw["outputs"][0]["modes"].append(
        {"id": None, "size": {"width": 2560, "height": 1440}, "refreshRate": 60},
    )
    for values in ({"size": "2560x1440"}, {"mode": "999"}):
        observed, fake, _clock, _scheduler = observer()
        fake.queue_result("run", command_output(raw))
        result = asyncio.run(observed.configure("1", values))
        assert isinstance(result, Err), values
        assert result.error.code == ErrorCode.unsupported_mode, values
        assert len(fake.runs) == 1, values


def test_configure_cannot_select_a_mode_with_an_invalid_size():
    """A mode whose size is unusable is not repaired into a selectable mode."""
    raw = {
        "outputs": [
            {
                "id": 2,
                "name": "Virtual-9",
                "enabled": True,
                "pos": {"x": 0, "y": 0},
                "scale": 1,
                "size": {"width": 1280, "height": 720},
                "currentModeId": "71",
                "modes": [
                    {"id": "71", "size": {"width": 1280, "height": 720}, "refreshRate": 60},
                    {"id": "72", "size": {"width": 0, "height": 1080}, "refreshRate": 60},
                ],
            },
        ],
    }
    for values in ({"size": "1920x1080"}, {"mode": "72"}):
        observed, fake, _clock, _scheduler = observer()
        fake.queue_result("run", command_output(raw))
        result = asyncio.run(observed.configure("2", values))
        assert isinstance(result, Err), values
        assert result.error.code == ErrorCode.unsupported_mode, values
        assert len(fake.runs) == 1, values


def test_configure_readback_rejects_a_reused_id_with_a_different_connector():
    raw = fixture(BASELINE)
    replaced = with_output(raw, "1", name="Virtual-unrelated", pos={"x": 1920, "y": 0})
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(replaced))
    result = asyncio.run(observed.configure("1", {"position": "1920,0"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


def test_configure_readback_rejects_wrong_dimensions_despite_the_same_mode_id():
    raw = fixture(BASELINE)
    resized = fixture(BASELINE)
    resized["outputs"][0]["size"] = {"width": 1280, "height": 720}
    resized["outputs"][0]["modes"][0]["size"] = {"width": 1280, "height": 720}
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(raw))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(resized))
    result = asyncio.run(observed.configure("1", {"size": "1920x1080"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


def test_readback_must_report_the_requested_mode():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"mode": "35"}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


def test_readback_must_report_the_requested_enabled_state():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"enabled": False}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


def test_readback_must_report_the_requested_scale():
    observed, fake, _clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    fake.queue_result("run", Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b"")))
    fake.queue_result("run", command_output(fixture(BASELINE)))
    result = asyncio.run(observed.configure("1", {"scale": 2}))
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.configuration_failed


# --------------------------------------------------------------------------- waiting


async def test_wait_for_returns_immediately_when_the_predicate_already_matches():
    observed, fake, clock, _scheduler = observer()
    fake.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    seen: list[tuple[OutputInfo, ...]] = []

    def predicate(outputs: tuple[OutputInfo, ...]) -> bool:
        seen.append(outputs)
        return len(outputs) == 3

    result = await observed.wait_for(predicate, clock.now() + 5)
    assert isinstance(result, Ok) and len(result.value) == 3
    assert all(isinstance(output, OutputInfo) for output in result.value)
    # The predicate and the caller see projected outputs, never raw records.
    assert seen[0] == result.value
    assert {output.connector for output in result.value} == {"DP-1", "Virtual-1", "Virtual-2"}
    assert len(fake.runs) == 1


async def test_wait_for_polls_through_the_injected_scheduler():
    process = FakeProcess()
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    observed = KScreenObserver(process, clock, scheduler, poll_interval=0.25)
    process.queue_result("run", command_output(fixture(BASELINE)))
    process.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    task = asyncio.create_task(
        observed.wait_for(lambda outputs: len(outputs) == 3, clock.now() + 5),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()
    scheduler.advance(0.25)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, Ok) and len(result.value) == 3
    assert len(process.runs) == 2


async def test_wait_for_publishes_a_change_it_discovers():
    process = FakeProcess()
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    observed = KScreenObserver(process, clock, scheduler, poll_interval=0.25)
    seen: list[tuple[OutputInfo, ...]] = []
    subscription = observed.subscribe(seen.append)
    process.queue_result("run", command_output(fixture(BASELINE)))
    process.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    task = asyncio.create_task(
        observed.wait_for(lambda outputs: len(outputs) == 3, clock.now() + 5),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert seen == []  # the first poll establishes the baseline silently
    scheduler.advance(0.25)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, Ok)
    assert [sorted(output.connector for output in event) for event in seen] == [
        ["DP-1", "Virtual-1", "Virtual-2"],
    ]
    subscription.close()


async def test_wait_for_times_out_without_claiming_an_output():
    process = FakeProcess()
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    observed = KScreenObserver(process, clock, scheduler, poll_interval=10.0)
    process.queue_result("run", command_output(fixture(BASELINE)))
    process.queue_result("run", command_output(fixture(BASELINE)))
    task = asyncio.create_task(
        observed.wait_for(lambda outputs: len(outputs) == 3, clock.now() + 1.0),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    scheduler.advance(1.0)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.cleanup_timeout


async def test_wait_for_reports_a_missing_kscreen_without_polling_forever():
    observed, fake, clock, _scheduler = observer()
    fake.queue_result(
        "run", Err(error=diagnostic(ErrorCode.dependency_missing, command="kscreen-doctor")),
    )
    result = await asyncio.wait_for(
        observed.wait_for(lambda outputs: True, clock.now() + 5), timeout=1.0,
    )
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_missing
    assert len(fake.runs) == 1


async def test_wait_for_survives_a_transient_read_error_until_the_output_appears():
    process = FakeProcess()
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    observed = KScreenObserver(process, clock, scheduler, poll_interval=0.25)
    process.queue_result(
        "run", Ok(value=CommandOutput(exit_code=0, stdout=b"not json", stderr=b"")),
    )
    process.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    task = asyncio.create_task(
        observed.wait_for(lambda outputs: len(outputs) == 3, clock.now() + 5),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    scheduler.advance(0.25)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, Ok) and len(result.value) == 3


class PollSignallingScheduler(FakeScheduler):
    """A scheduler that reports when a poll tick was scheduled.

    The wait never fires these ticks by itself, so a test controls whether the
    injected clock and the scheduler advance at all.
    """

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock)
        self.scheduled = asyncio.Event()

    def call_at(self, when, callback):
        handle = super().call_at(when, callback)
        self.scheduled.set()
        return handle


async def test_wait_for_obeys_a_real_deadline_when_the_scheduler_never_fires():
    """A frozen clock plus a scheduler that never fires still ends in a timeout.

    The real-time fallback must be terminal. If it only wakes the loop, every
    iteration installs another fallback and a 20 ms deadline can hang.
    """
    clock = FakeClock()
    scheduler = PollSignallingScheduler(clock)
    process = FakeProcess()
    observed = KScreenObserver(process, clock, scheduler)
    for _ in range(30):
        process.queue_result("run", command_output(fixture(BASELINE)))
    task = asyncio.create_task(observed.wait_for(lambda outputs: False, 0.02))
    await asyncio.wait_for(scheduler.scheduled.wait(), timeout=1.0)
    done, _pending = await asyncio.wait({task}, timeout=1.0)
    if task not in done:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task in done, f"a 20 ms deadline was still pending; {len(process.runs)} reads"
    result = task.result()
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.cleanup_timeout
    assert len(process.runs) == 1


async def test_wait_for_rejects_a_match_observed_after_the_deadline():
    entered = asyncio.Event()
    release = asyncio.Event()
    clock = FakeClock()

    class HeldRead(FakeProcess):
        async def run(self, spec, timeout, stdin=None):
            self.runs.append((spec, timeout))
            entered.set()
            await release.wait()
            return command_output(fixture(BASELINE))

    process = HeldRead()
    observed = KScreenObserver(process, clock, FakeScheduler(clock))
    task = asyncio.create_task(observed.wait_for(lambda outputs: True, 1.0))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    clock.advance(2)
    release.set()
    result = await asyncio.wait_for(task, timeout=1.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.cleanup_timeout


async def test_wait_for_does_not_start_a_read_at_the_deadline():
    clock = FakeClock()
    scheduler = PollSignallingScheduler(clock)
    process = FakeProcess()
    observed = KScreenObserver(process, clock, scheduler)
    process.queue_result("run", command_output(fixture(BASELINE)))
    process.queue_result("run", command_output(fixture(BASELINE)))
    task = asyncio.create_task(observed.wait_for(lambda outputs: False, 1.0))
    await asyncio.wait_for(scheduler.scheduled.wait(), timeout=1.0)
    scheduler.advance(1)
    result = await asyncio.wait_for(task, timeout=1.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.cleanup_timeout
    assert len(process.runs) == 1


async def test_wait_for_bounds_each_read_by_the_remaining_budget():
    process = FakeProcess()
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    observed = KScreenObserver(process, clock, scheduler, poll_interval=0.25)
    process.queue_result("run", command_output(fixture(BASELINE)))
    process.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    task = asyncio.create_task(
        observed.wait_for(lambda outputs: len(outputs) == 3, clock.now() + 1.0),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    scheduler.advance(0.25)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(result, Ok)
    # The default 5 s control bound never outlives the operation deadline.
    assert [timeout for _spec, timeout in process.runs] == [1.0, 0.75]


# --------------------------------------------------------------------------- subscriptions


async def test_subscribe_notifies_only_when_an_observation_changes():
    observed, fake, _clock, _scheduler = observer()
    seen: list[tuple[OutputInfo, ...]] = []
    subscription = observed.subscribe(seen.append)
    fake.queue_result("run", command_output(fixture(BASELINE)))
    await observed.outputs()
    assert seen == []  # the first observation establishes the baseline silently
    fake.queue_result("run", command_output(fixture(BASELINE)))
    await observed.outputs()
    assert seen == []
    fake.queue_result("run", command_output(fixture(TWO_VIRTUAL)))
    await observed.outputs()
    assert len(seen) == 1
    assert {output.connector for output in seen[0]} == {"DP-1", "Virtual-1", "Virtual-2"}
    subscription.close()
    fake.queue_result("run", command_output(fixture(BASELINE)))
    await observed.outputs()
    assert len(seen) == 1


async def test_subscribe_does_not_notify_for_a_failed_read():
    observed, fake, _clock, _scheduler = observer()
    seen: list[tuple[OutputInfo, ...]] = []
    subscription = observed.subscribe(seen.append)
    fake.queue_result("run", command_output(fixture(BASELINE)))
    await observed.outputs()
    fake.queue_result(
        "run", Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b"")),
    )
    assert isinstance(await observed.outputs(), Err)
    assert seen == []
    subscription.close()


# --------------------------------------------------------------------------- mode helper


def _mode(mode_id: str, width: int, height: int, refresh: float) -> KScreenMode:
    return KScreenMode(mode_id=mode_id, size=Size(width=width, height=height),
                       refresh_rate=refresh)


def test_select_mode_is_deterministic_without_a_preferred_mode():
    modes = (
        _mode("42", 1920, 1080, 60),
        _mode("43", 1920, 1080, 120),
        _mode("44", 1280, 720, 60),
    )
    selected = select_mode(modes, Size(width=1920, height=1080))
    assert selected is not None
    assert selected.mode_id == "43"  # highest refresh wins a tie, deterministically
