"""Pure reported-source provenance checks over one KScreen observation."""

from __future__ import annotations

from airplay_linux.backend.contract import BackendSource
from airplay_linux.capture.source import verify_reported_source
from airplay_linux.display.inventory import MonitoredOutput
from airplay_linux.domain.resources import Err, Ok, Size


def _output(
    name: str = "DP-1", *, enabled: bool = True, connected: bool = True,
    output_type: int | str | None = 14, size: Size | None = Size(width=1920, height=1080),
    position: tuple[int, int] | None = (0, 0), virtual: bool | None = False,
) -> MonitoredOutput:
    return MonitoredOutput(
        name=name, enabled=enabled, connected=connected, output_type=output_type,
        size=size, position=position, virtual=virtual,
    )


def _reported(x: int = 0, y: int = 0) -> BackendSource:
    return BackendSource(type=1, width=1920, height=1080, x=x, y=y)


def test_same_size_other_position_is_not_requested_source():
    result = verify_reported_source("DP-1", _reported(), (_output(position=(100, 0)),))
    assert isinstance(result, Err)


def test_enabled_foreign_or_virtual_geometry_collision_refuses():
    requested = _reported()
    for duplicate in (
        _output("HDMI-1"),
        _output("DP-2", virtual=True),
        _output("DP-3", virtual=None),
    ):
        assert isinstance(
            verify_reported_source("DP-1", requested, (_output(), duplicate)),
            Err,
        )


def test_disabled_geometry_match_does_not_create_ambiguity():
    result = verify_reported_source(
        "DP-1", _reported(),
        (_output(), _output("HDMI-1", enabled=False, connected=True)),
    )
    assert isinstance(result, Ok)


def test_enabled_disconnected_or_unknown_geometry_collision_refuses():
    requested = _reported()
    for duplicate in (
        _output("DP-2", connected=False),
        _output("DP-3", output_type="Unknown", virtual=None),
    ):
        assert isinstance(
            verify_reported_source("DP-1", requested, (_output(), duplicate)),
            Err,
        )


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
        assert isinstance(verify_reported_source("DP-1", _reported(), (output,)), Err)
