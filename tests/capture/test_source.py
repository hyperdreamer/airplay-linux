"""Source provenance verification tests."""

from __future__ import annotations

from airplay_linux.backend.contract import BackendSource
from airplay_linux.capture.source import verify_reported_source
from airplay_linux.display.inventory import MonitoredOutput
from airplay_linux.domain.resources import Err, Ok, Size


def _layout(*outputs: MonitoredOutput) -> tuple[MonitoredOutput, ...]:
    return outputs


def _monitor(name: str = "DP-1", *, connected: bool = True, enabled: bool = True,
             output_type: int | str | None = 14, size: Size | None = Size(width=1920, height=1080),
             position: tuple[int, int] | None = (0, 0)) -> MonitoredOutput:
    return MonitoredOutput(
        name=name, enabled=enabled, connected=connected,
        output_type=output_type, size=size, position=position,
        virtual=(not _physical_name(name) if name else None),
    )


def _physical_name(name: str) -> bool:
    return not name.casefold().startswith(("virtual", "window", "region"))


def test_reported_source_requires_unique_physical_monitor():
    reported = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    assert isinstance(verify_reported_source("DP-1", reported, _layout(_monitor())), Ok)
    assert isinstance(
        verify_reported_source("DP-1", reported, _layout(_monitor("Virtual-1", output_type=14))),
        Err,
    )
    assert isinstance(
        verify_reported_source("DP-1", reported, _layout(_monitor(), _monitor("HDMI-1"))),
        Err,
    )


def test_reported_source_rejects_unreadable_geometry_and_nonphysical_origin():
    reported = BackendSource(type=1, width=1920, height=1080, x=-1920, y=0)
    for layout in (
        _layout(_monitor(position=None)),
        _layout(_monitor(output_type="unknown")),
        _layout(_monitor(connected=False)),
        _layout(_monitor("Virtual-1", output_type=0)),
    ):
        assert isinstance(verify_reported_source("DP-1", reported, layout), Err)


def test_reported_source_rejects_non_monitor_type_and_nonpositive_size():
    layout = _layout(_monitor())
    for reported in (
        BackendSource(type=2, width=1920, height=1080, x=0, y=0),
        BackendSource(type=1, width=0, height=1080, x=0, y=0),
    ):
        assert isinstance(verify_reported_source("DP-1", reported, layout), Err)
