"""Physical-capture contracts over a real, read-only ``kscreen-doctor`` probe.

The inventory and provider are the production adapters; the ``kscreen-doctor``
command is a private fake that reads and writes exactly one file inside
``tmp_path``.  The portal is a small test double, so no session bus, portal or
desktop is touched.  The invariants pinned here are the ones that decide whether
the application may capture anything at all:

* a connector is reported only with positively proven physical origin -- a
  virtual output, an unknown type, a disabled or a disconnected output is never
  offered, and an unreadable/ambiguous probe is a failure, not an empty list;
* selecting a connector that the latest probe does not advertise (a window, a
  virtual output, a removed monitor) creates no portal session;
* verification re-probes the desktop and fails when the connector disappeared;
* release is exact: it releases the one acquired session, once.

Manual commands: ``pytest -q tests/adapter_contracts/test_capture_contract.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from airplay_linux.capture.physical import (
    InventoryProbeError,
    KscreenConnectorInventory,
    PhysicalCaptureProvider,
)
from airplay_linux.capture.portal import PortalMonitor
from airplay_linux.domain.resources import Err, ErrorCode, Ok, PhysicalMonitorSource
from airplay_linux.process.linux import LinuxProcessRunner

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402
import fake_kscreen  # noqa: E402


class _Portal:
    """Private in-process portal peer: only connector-scoped monitor sessions."""

    def __init__(self):
        self.selected: list[str] = []
        self.released: list[str] = []
        self.open: set[str] = set()

    async def select_monitor(self, connector, restore_token=None):
        self.selected.append(connector)
        session = f"session-{len(self.selected)}"
        self.open.add(session)
        return Ok(value=PortalMonitor(
            connector=connector,
            source_type="physical_monitor",
            restore_token=None,
            portal_session_id=session,
        ))

    def is_open(self, monitor):
        return monitor.portal_session_id in self.open

    async def release_monitor(self, monitor):
        self.released.append(monitor.portal_session_id)
        self.open.discard(monitor.portal_session_id)
        return Ok(value=None)


def _features():
    from airplay_linux.backend.control import ApplicationContractProfile
    return ApplicationContractProfile(socket_path="/run/user/0/private.sock").features


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """A private PATH with a fake ``kscreen-doctor`` reading one state file."""
    state = tmp_path / "fake-kscreen.json"
    fake_kscreen.write_layout(state, fake_kscreen.baseline_layout())
    installed = fake_doubletake.install(
        tmp_path / "bin",
        env={"kscreen-doctor": {"AIRPLAY_FAKE_KSCREEN_STATE": str(state)}},
    )
    fake_doubletake.private_path(monkeypatch, tmp_path / "bin")
    yield LinuxProcessRunner(), state, installed
    fake_doubletake.reap_private(str(tmp_path))


async def test_inventory_reports_only_positively_proven_physical_outputs(desktop):
    runner, state, _ = desktop
    layout = fake_kscreen.baseline_layout()
    layout["outputs"].append(fake_kscreen.virtual_output("Virtual-2", 1280, 720))
    layout["outputs"].append(fake_kscreen.output("HDMI-A-1", 3, kind="unknown"))
    layout["outputs"].append(fake_kscreen.output("DP-2", 4, enabled=False))
    layout["outputs"].append(fake_kscreen.output("DP-3", 5, connected=False))
    layout["outputs"].append(fake_kscreen.output("window:0x1", 6, kind="physical"))
    fake_kscreen.write_layout(state, layout)

    inventory = KscreenConnectorInventory(runner)
    assert await inventory() == ("DP-1",)


async def test_unreadable_or_malformed_layouts_are_failures_not_empty_results(desktop):
    runner, state, installed = desktop
    # A virtual output cannot buy physical origin by claiming a physical type.
    disguised = fake_kscreen.baseline_layout()
    disguised["outputs"].append(fake_kscreen.virtual_output("Virtual-9", 1920, 1080))
    disguised["outputs"][-1]["type"] = 14
    fake_kscreen.write_layout(state, disguised)
    assert await KscreenConnectorInventory(runner)() == ("DP-1",)

    missing = {"outputs": "not-a-list"}
    fake_kscreen.write_layout(state, missing)
    with pytest.raises(InventoryProbeError):
        await KscreenConnectorInventory(runner)()

    broken = fake_doubletake.install(
        (state.parent / "bin-broken"),
        env={"kscreen-doctor": {
            "AIRPLAY_FAKE_KSCREEN_MODE": "malformed",
            "AIRPLAY_FAKE_KSCREEN_STATE": str(state),
        }},
    )
    from airplay_linux.domain.ports import CommandSpec
    broken_runner = LinuxProcessRunner()
    result = await broken_runner.run(
        CommandSpec(argv=(str(broken["kscreen-doctor"]), "-j"), env={}), 5.0,
    )
    assert isinstance(result, Ok) and result.value.stdout == b"{not json"
    with pytest.raises(InventoryProbeError):
        await KscreenConnectorInventory(broken_runner)()


async def test_select_refuses_anything_the_latest_probe_does_not_advertise(desktop):
    runner, state, _ = desktop
    portal = _Portal()
    provider = PhysicalCaptureProvider(
        portal, KscreenConnectorInventory(runner), _features(),
    )
    listed = await provider.list_physical()
    assert isinstance(listed, Ok)
    assert [source.connector for source in listed.value] == ["DP-1"]

    for connector in ("Virtual-1", "window:0x1", "DP-404"):
        refused = await provider.select(connector)
        assert isinstance(refused, Err)
        assert refused.error.code is ErrorCode.unsupported_source
    assert portal.selected == []

    selected = await provider.select("DP-1")
    assert isinstance(selected, Ok), selected
    assert portal.selected == ["DP-1"]
    assert selected.value.capture_lease_id is not None


async def test_verify_reprobes_and_fails_when_the_connector_disappeared(desktop):
    runner, state, _ = desktop
    portal = _Portal()
    provider = PhysicalCaptureProvider(
        portal, KscreenConnectorInventory(runner), _features(),
    )
    selected = await provider.select("DP-1")
    assert isinstance(selected, Ok), selected

    assert isinstance(await provider.verify(selected.value), Ok)

    without_monitor = fake_kscreen.baseline_layout()
    without_monitor["outputs"] = []
    fake_kscreen.write_layout(state, without_monitor)
    gone = await provider.verify(selected.value)
    assert isinstance(gone, Err)
    assert gone.error.code is ErrorCode.unsupported_source

    # Restoring the monitor does not silently re-acquire: the selected source's
    # portal session is still the only proof of liveness.
    fake_kscreen.write_layout(state, fake_kscreen.baseline_layout())
    assert isinstance(await provider.verify(selected.value), Ok)

    released = await provider.release(selected.value)
    assert isinstance(released, Ok)
    assert portal.released == ["session-1"]
    stale = await provider.verify(selected.value)
    assert isinstance(stale, Err)
    assert stale.error.code is ErrorCode.stale_event
    # A repeated release is a no-op, not a second portal release.
    assert isinstance(await provider.release(selected.value), Ok)
    assert portal.released == ["session-1"]


async def test_a_foreign_source_cannot_be_verified(desktop):
    runner, _, _ = desktop
    provider = PhysicalCaptureProvider(_Portal(), KscreenConnectorInventory(runner), _features())
    unselected = PhysicalMonitorSource(connector="DP-1", portal_restore_token=None)
    refused = await provider.verify(unselected)
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.unsupported_source
