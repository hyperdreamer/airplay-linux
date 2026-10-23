"""Physical-monitor capture provider: portal selection, leases, and verification."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from uuid import UUID

import pytest

from airplay_linux.backend.contract import BackendFeatures
from airplay_linux.capture.physical import (
    InventoryProbeError,
    KscreenConnectorInventory,
    PhysicalCaptureProvider,
    is_physical_descriptor,
)
from airplay_linux.capture.portal import PortalMonitor
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain import ports
from airplay_linux.domain.operations import LeaseId
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Ok,
    PhysicalMonitorSource,
    Size,
    VirtualOutputSource,
)
from airplay_linux.testing import (
    Deferred,
    FakeCapture,
    FakeProcess,
    SequenceIds,
    physical,
)


def backend_features(**overrides: Any) -> BackendFeatures:
    """A backend profile that accepts physical sources unless a test says otherwise."""
    defaults: dict[str, Any] = {
        "profile": "doubletake-0.5",
        "version": "0.5.0",
        "foreground_control": True,
        "owner_handshake": True,
        "multi_target": True,
        "target_bound_pairing": True,
        "physical_source": True,
        "virtual_source": True,
        "audio_probe": True,
        "audio_restart": True,
        "target_mute": True,
        "keyring": True,
        "provisional_commit": True,
        "cancel_pairing": True,
        "revoke_pairing": True,
        "query_modes": True,
    }
    defaults.update(overrides)
    return BackendFeatures(**defaults)


class FakePortal:
    """Portal double that records exact selections and closes exact sessions."""

    def __init__(self) -> None:
        self.select_calls: list[tuple[str, str | None]] = []
        self.tokens_seen: list[str | None] = []
        self.release_calls: list[str] = []
        self.open: dict[str, PortalMonitor] = {}
        self.started = asyncio.Event()
        self.release_started = asyncio.Event()
        self.release_script: deque[Any] = deque()
        self.release_gate: Deferred[Any] | None = None
        self._scripted: deque[Any] = deque()
        self._counter = 0

    def queue(self, result: Any) -> None:
        self._scripted.append(result)

    async def select_monitor(self, connector: str, restore_token: str | None) -> Any:
        self.select_calls.append((connector, restore_token))
        self.tokens_seen.append(restore_token)
        self.started.set()
        if self._scripted:
            item = self._scripted.popleft()
            if isinstance(item, Deferred):
                item = await item.wait()
            if isinstance(item, Err):
                return item
            if isinstance(item, Ok):
                monitor = item.value
                if isinstance(monitor, PortalMonitor):
                    self.open[monitor.portal_session_id] = monitor
                return item
        self._counter += 1
        monitor = PortalMonitor(
            connector=connector,
            source_type="physical_monitor",
            restore_token=f"token-{self._counter}",
            portal_session_id=f"session-{self._counter}",
        )
        self.open[monitor.portal_session_id] = monitor
        return Ok(value=monitor)

    def is_open(self, monitor: PortalMonitor) -> bool:
        return self.open.get(monitor.portal_session_id) == monitor

    async def release_monitor(self, monitor: PortalMonitor) -> Any:
        self.release_calls.append(monitor.portal_session_id)
        self.release_started.set()
        gate, self.release_gate = self.release_gate, None
        if gate is not None:
            await gate.wait()
        if self.release_script:
            item = self.release_script.popleft()
            if isinstance(item, BaseException):
                raise item
            return item
        self.open.pop(monitor.portal_session_id, None)
        return Ok(value=None)


class Inventory:
    """Mutable connector inventory double, like a sysfs/KScreen probe."""

    def __init__(self, connectors: tuple[str, ...]) -> None:
        self.connectors = tuple(connectors)
        self.calls = 0

    async def __call__(self) -> tuple[str, ...]:
        self.calls += 1
        return self.connectors


def make_provider(
    *,
    connectors: tuple[str, ...] = ("DP-1", "HDMI-A-1", "Virtual-1"),
    features: BackendFeatures | None = None,
    portal: FakePortal | None = None,
    inventory: Any | None = None,
    ids: Any | None = None,
) -> tuple[PhysicalCaptureProvider, FakePortal, Inventory]:
    portal = portal or FakePortal()
    inventory = inventory or Inventory(connectors)
    provider = PhysicalCaptureProvider(
        portal=portal,
        connectors=inventory,
        features=features or backend_features(),
        ids=ids or SequenceIds(seed=900),
    )
    return provider, portal, inventory


# --------------------------------------------------------------------------- descriptors


def test_virtual_output_is_never_a_mirror_descriptor() -> None:
    source = VirtualOutputSource(
        connector="Virtual-1", kscreen_output_id="8", size=Size(width=1920, height=1080)
    )
    assert not is_physical_descriptor(source, ("DP-1",))
    assert is_physical_descriptor(
        PhysicalMonitorSource(connector="DP-1", portal_restore_token=None), ("DP-1",)
    )


def test_window_like_object_is_never_a_mirror_descriptor() -> None:
    class Window:
        connector = "DP-1"

    assert not is_physical_descriptor(Window(), ("DP-1",))


def test_unknown_connector_is_never_a_mirror_descriptor() -> None:
    assert not is_physical_descriptor(physical("DP-9"), ("DP-1",))
    assert not is_physical_descriptor(physical(""), ("",))
    assert not is_physical_descriptor(physical("DP-1"), ())


def test_virtual_connector_name_is_never_physical_even_when_inventoried() -> None:
    assert not is_physical_descriptor(physical("Virtual-1"), ("Virtual-1",))
    assert not is_physical_descriptor(physical("WINDOW:7"), ("WINDOW:7",))


def test_descriptor_requires_a_real_connector_name_and_a_tuple_inventory() -> None:
    assert is_physical_descriptor(physical("eDP-1"), ("eDP-1",))
    assert is_physical_descriptor(physical("HDMI-A-1"), ("DP-1", "HDMI-A-1"))
    assert not is_physical_descriptor(physical("DP-1"), ["DP-1"])  # type: ignore[arg-type]
    assert not is_physical_descriptor(physical("not a connector"), ("not a connector",))


# --------------------------------------------------------------------------- enumeration


async def test_list_physical_returns_unleased_untokened_descriptors() -> None:
    provider, portal, inventory = make_provider()

    result = await provider.list_physical()

    assert isinstance(result, Ok)
    assert [source.connector for source in result.value] == ["DP-1", "HDMI-A-1"]
    assert all(source.capture_lease_id is None for source in result.value)
    assert all(source.portal_restore_token is None for source in result.value)
    assert portal.select_calls == []
    assert inventory.calls == 1


async def test_backend_inability_never_starts_a_portal_session() -> None:
    provider, portal, inventory = make_provider(
        features=backend_features(physical_source=False)
    )

    results = [
        await provider.list_physical(),
        await provider.select("DP-1"),
        await provider.verify(physical("DP-1")),
    ]

    for result in results:
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []
    assert inventory.calls == 0


@pytest.mark.parametrize("connector", ["DP-9", "Virtual-1", "window:12", "", None, 7])
async def test_select_rejects_non_physical_or_unknown_sources(connector: object) -> None:
    provider, portal, inventory = make_provider()

    result = await provider.select(connector)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error.code in {ErrorCode.unsupported_source, ErrorCode.invalid_input}
    assert portal.select_calls == []


async def test_inventory_failure_is_fail_closed() -> None:
    class Broken:
        async def __call__(self) -> tuple[str, ...]:
            raise RuntimeError("kscreen probe failed")

    provider, portal, _ = make_provider(inventory=Broken())

    assert (await provider.list_physical()).error.code is ErrorCode.unsupported_source
    assert (await provider.select("DP-1")).error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []


async def test_inventory_must_return_a_tuple_of_connector_names() -> None:
    class WrongShape:
        async def __call__(self) -> Any:
            return ["DP-1"]

    provider, portal, _ = make_provider(inventory=WrongShape())

    assert (await provider.list_physical()).error.code is ErrorCode.unsupported_source
    assert (await provider.select("DP-1")).error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []


# --------------------------------------------------------------------------- selection


async def test_two_selections_get_distinct_leases_and_independent_sessions() -> None:
    provider, portal, _ = make_provider()

    first = await provider.select("DP-1")
    second = await provider.select("DP-1")

    assert isinstance(first, Ok) and isinstance(second, Ok)
    assert first.value.capture_lease_id is not None
    assert second.value.capture_lease_id is not None
    assert first.value.capture_lease_id != second.value.capture_lease_id
    assert isinstance(first.value.capture_lease_id, UUID)
    assert len(portal.open) == 2
    assert portal.select_calls == [("DP-1", None), ("DP-1", "token-1")]

    assert isinstance(await provider.verify(first.value), Ok)
    assert isinstance(await provider.verify(second.value), Ok)

    await provider.release(first.value)
    assert portal.release_calls == ["session-1"]
    assert list(portal.open) == ["session-2"]

    stale = await provider.verify(first.value)
    assert isinstance(stale, Err)
    assert stale.error.code is ErrorCode.stale_event
    assert isinstance(await provider.verify(second.value), Ok)


async def test_release_is_idempotent_and_never_closes_another_lease() -> None:
    provider, portal, _ = make_provider()
    first = (await provider.select("DP-1")).value
    second = (await provider.select("DP-1")).value

    assert isinstance(await provider.release(first), Ok)
    assert isinstance(await provider.release(first), Ok)
    assert portal.release_calls == ["session-1"]

    forged = PhysicalMonitorSource(
        connector="HDMI-A-1",
        portal_restore_token=second.portal_restore_token,
        capture_lease_id=second.capture_lease_id,
    )
    mismatch = await provider.release(forged)
    assert isinstance(mismatch, Err)
    assert mismatch.error.code is ErrorCode.stale_event
    assert portal.release_calls == ["session-1"]
    assert list(portal.open) == ["session-2"]


async def test_unacquired_descriptors_have_no_lease_and_release_is_a_noop() -> None:
    provider, portal, _ = make_provider()
    listed = (await provider.list_physical()).value

    unverified = await provider.verify(listed[0])
    assert isinstance(unverified, Err)
    assert unverified.error.code is ErrorCode.unsupported_source

    released = await provider.release(listed[0])
    assert isinstance(released, Ok)
    assert released.value is None
    assert portal.release_calls == []


async def test_verify_revalidates_connector_liveness() -> None:
    provider, portal, inventory = make_provider()
    selected = (await provider.select("DP-1")).value

    inventory.connectors = ("HDMI-A-1",)
    result = await provider.verify(selected)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    # The exact handle stays releasable even after its connector disappeared.
    assert isinstance(await provider.release(selected), Ok)
    assert portal.release_calls == ["session-1"]


async def test_verify_revalidates_backend_selection_support() -> None:
    features = backend_features()
    provider, _, _ = make_provider(features=features)
    selected = (await provider.select("DP-1")).value

    object.__setattr__(features, "physical_source", False)
    result = await provider.verify(selected)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source

    object.__setattr__(features, "physical_source", True)
    assert isinstance(await provider.verify(selected), Ok)


async def test_selected_source_cannot_restart_capture_after_its_session_closed() -> None:
    provider, portal, _ = make_provider()
    selected = (await provider.select("DP-1")).value

    # The portal session disappeared underneath the still-remembered lease.
    portal.open.pop("session-1")

    result = await provider.verify(selected)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.stale_event


async def test_portal_binding_a_different_connector_is_released_and_refused() -> None:
    portal = FakePortal()
    monitor = PortalMonitor(
        connector="HDMI-A-1",
        source_type="physical_monitor",
        restore_token="bad-token",
        portal_session_id="session-x",
    )
    portal.queue(Ok(value=monitor))
    provider, _, _ = make_provider(portal=portal)

    result = await provider.select("DP-1")

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert portal.release_calls == ["session-x"]
    assert "bad-token" not in repr(result)


async def test_mismatched_acquisition_cleanup_failure_is_retained_and_retried() -> None:
    portal = FakePortal()
    monitor = PortalMonitor(
        connector="HDMI-A-1",
        source_type="physical_monitor",
        restore_token="bad-token",
        portal_session_id="session-x",
    )
    portal.queue(Ok(value=monitor))
    portal.release_script.append(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    provider, _, _ = make_provider(portal=portal)

    refused = await provider.select("DP-1")

    assert isinstance(refused, Err)
    # The refused source still owns a live session; that is not a plain refusal.
    assert refused.error.code is ErrorCode.cleanup_timeout
    assert "session-x" in portal.open
    assert "bad-token" not in repr(refused)

    # The next selection retries the exact retained session and confirms it.
    recovered = await provider.select("DP-1")

    assert isinstance(recovered, Ok)
    assert "session-x" not in portal.open
    assert portal.release_calls == ["session-x", "session-x"]


async def test_mismatched_acquisition_cleanup_exception_is_retained() -> None:
    portal = FakePortal()
    monitor = PortalMonitor(
        connector="HDMI-A-1",
        source_type="physical_monitor",
        restore_token="bad-token",
        portal_session_id="session-x",
    )
    portal.queue(Ok(value=monitor))
    portal.release_script.append(RuntimeError("the bus went away"))
    provider, _, _ = make_provider(portal=portal)

    refused = await provider.select("DP-1")

    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.cleanup_timeout
    assert "session-x" in portal.open
    assert "the bus went away" not in repr(refused)


async def test_cancelled_mismatch_cleanup_keeps_the_exact_session() -> None:
    portal = FakePortal()
    monitor = PortalMonitor(
        connector="HDMI-A-1",
        source_type="physical_monitor",
        restore_token="bad-token",
        portal_session_id="session-x",
    )
    portal.queue(Ok(value=monitor))
    portal.release_gate = Deferred()
    provider, _, _ = make_provider(portal=portal)

    task = asyncio.create_task(provider.select("DP-1"))
    await portal.release_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "session-x" in portal.open

    portal.release_gate = None
    recovered = await provider.select("DP-1")
    assert isinstance(recovered, Ok)
    assert "session-x" not in portal.open


async def test_portal_must_return_an_adapter_monitor() -> None:
    portal = FakePortal()
    portal.queue(Ok(value=physical("DP-1")))
    provider, _, _ = make_provider(portal=portal)

    result = await provider.select("DP-1")

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source


async def test_portal_denial_is_recoverable_and_creates_no_lease() -> None:
    portal = FakePortal()
    portal.queue(Err(error=diagnostic(ErrorCode.portal_denied)))
    provider, _, _ = make_provider(portal=portal)

    denied = await provider.select("DP-1")
    assert isinstance(denied, Err)
    assert denied.error.code is ErrorCode.portal_denied
    assert portal.open == {}

    recovered = await provider.select("DP-1")
    assert isinstance(recovered, Ok)
    assert recovered.value.capture_lease_id is not None


# --------------------------------------------------------------------------- restore tokens


async def test_restore_token_is_reused_then_erased_after_a_failed_selection() -> None:
    provider, portal, _ = make_provider()

    first = (await provider.select("DP-1")).value
    assert first.portal_restore_token == "token-1"
    await provider.release(first)

    second = (await provider.select("DP-1")).value
    assert portal.tokens_seen == [None, "token-1"]
    assert second.portal_restore_token == "token-2"

    portal.queue(Err(error=diagnostic(ErrorCode.portal_denied)))
    denied = await provider.select("DP-1")
    assert isinstance(denied, Err)
    assert portal.tokens_seen[-1] == "token-2"
    assert "token-2" not in repr(denied)

    await provider.select("DP-1")
    assert portal.tokens_seen[-1] is None


async def test_cancelled_selection_leaves_no_lease_and_no_open_session() -> None:
    portal = FakePortal()
    deferred: Deferred[Any] = Deferred()
    portal.queue(deferred)
    provider, _, _ = make_provider(portal=portal)

    task = asyncio.create_task(provider.select("DP-1"))
    await portal.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    late_monitor = PortalMonitor(
        connector="DP-1",
        source_type="physical_monitor",
        restore_token="late-token",
        portal_session_id="session-late",
    )
    deferred.resolve(Ok(value=late_monitor))
    await asyncio.sleep(0)

    assert portal.open == {}
    late_source = PhysicalMonitorSource(
        connector="DP-1",
        portal_restore_token="late-token",
        capture_lease_id=LeaseId(UUID(int=1)),
    )
    assert isinstance(await provider.verify(late_source), Err)
    await provider.release(late_source)
    assert portal.release_calls == []


# --------------------------------------------------------------------------- inventory adapter


DISPLAYPORT = 14  # KScreen::Output::Type::DisplayPort
HDMI = 6  # KScreen::Output::Type::HDMI
PANEL = 7  # KScreen::Output::Type::Panel
UNKNOWN_TYPE = 0  # KScreen::Output::Type::Unknown


def kscreen_process(outputs: list[dict[str, Any]]) -> FakeProcess:
    """A process double answering the real ``kscreen-doctor -j`` schema."""
    process = FakeProcess()
    process.queue_result(
        "run",
        Ok(
            value=ports.CommandOutput(
                exit_code=0, stdout=json.dumps({"outputs": outputs}).encode(), stderr=b""
            )
        ),
    )
    return process


async def test_kscreen_inventory_is_read_only_and_reports_physical_outputs() -> None:
    process = kscreen_process(
        [
            {"name": "DP-1", "enabled": True, "connected": True, "type": DISPLAYPORT},
            {"name": "HDMI-A-1", "enabled": True, "connected": True, "type": HDMI},
            {"name": "eDP-1", "enabled": True, "connected": True, "type": PANEL},
            {"name": "Virtual-1", "enabled": True, "connected": True, "type": DISPLAYPORT},
            {"name": "DP-2", "enabled": False, "connected": True, "type": DISPLAYPORT},
            {"name": "DP-3", "enabled": True, "connected": False, "type": DISPLAYPORT},
        ]
    )
    inventory = KscreenConnectorInventory(process)

    # ``Virtual-1`` carries a physical DisplayPort type and a virtual refusal
    # name; a neutral name with unproven origin is the CAP-001 matrix below.
    assert await inventory() == ("DP-1", "HDMI-A-1", "eDP-1")
    run = process.journal.calls("process.run")[0]
    assert run["spec"]["argv"] == ("kscreen-doctor", "-j")
    assert run["timeout"] == 5.0
    assert process.journal.calls("process.spawn") == []
    assert process.journal.calls("process.terminate") == []


@pytest.mark.parametrize(
    "entry",
    [
        # A helper-created output can appear with a neutral name and no origin.
        {"name": "Cast-1", "enabled": True, "connected": True, "type": UNKNOWN_TYPE},
        {"name": "Cast-1", "enabled": True, "connected": True},
        {"name": "Cast-1", "enabled": True, "connected": True, "type": None},
        {"name": "Cast-1", "enabled": True, "connected": True, "type": "DisplayPort"},
        {"name": "Cast-1", "enabled": True, "connected": True, "type": "Virtual"},
        {"name": "Cast-1", "enabled": True, "connected": True, "type": 99},
        # Ambiguous connection state is not proof of a connected monitor.
        {"name": "Cast-1", "enabled": True, "connected": "true", "type": DISPLAYPORT},
        {"name": "Cast-1", "enabled": True, "type": DISPLAYPORT},
    ],
)
async def test_unproven_output_origin_is_never_reported_as_physical(entry: dict) -> None:
    process = kscreen_process([entry])

    assert await KscreenConnectorInventory(process)() == ()


async def test_a_neutral_name_with_a_physical_type_is_verified_by_type_only() -> None:
    process = kscreen_process(
        [{"name": "Cast-1", "enabled": True, "connected": True, "type": DISPLAYPORT}]
    )

    assert await KscreenConnectorInventory(process)() == ("Cast-1",)


async def test_unproven_inventory_entries_never_reach_the_provider() -> None:
    process = kscreen_process(
        [{"name": "Cast-1", "enabled": True, "connected": True, "type": UNKNOWN_TYPE}]
    )
    provider, portal, _ = make_provider(inventory=KscreenConnectorInventory(process))

    listed = await provider.list_physical()
    assert isinstance(listed, Ok)
    assert listed.value == ()

    selected = await provider.select("Cast-1")
    assert isinstance(selected, Err)
    assert selected.error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []


@pytest.mark.parametrize(
    "outcome",
    [
        Ok(
            value=ports.CommandOutput(
                exit_code=0, stdout=b"not json", stderr=b""
            )
        ),
        Ok(
            value=ports.CommandOutput(
                exit_code=1, stdout=b'{"outputs": []}', stderr=b"boom"
            )
        ),
        Ok(value=ports.CommandOutput(exit_code=0, stdout=b'{"outputs": {}}', stderr=b"")),
        Err(error=diagnostic(ErrorCode.dependency_missing)),
    ],
)
async def test_kscreen_inventory_probe_failure_is_never_an_empty_inventory(outcome: Any) -> None:
    process = FakeProcess()
    process.queue_result("run", outcome)

    with pytest.raises(InventoryProbeError):
        await KscreenConnectorInventory(process)()


async def test_kscreen_inventory_probe_exception_is_never_an_empty_inventory() -> None:
    class BrokenProcess:
        async def run(self, spec: Any, timeout: float, stdin: Any = None) -> Any:
            raise RuntimeError("spawn failed")

    with pytest.raises(InventoryProbeError):
        await KscreenConnectorInventory(BrokenProcess())()


@pytest.mark.parametrize(
    "outcome",
    [
        Ok(value=ports.CommandOutput(exit_code=0, stdout=b"not json", stderr=b"")),
        Ok(value=ports.CommandOutput(exit_code=2, stdout=b'{"outputs": []}', stderr=b"")),
        Ok(value=ports.CommandOutput(exit_code=0, stdout=b'{"outputs": {}}', stderr=b"")),
        Ok(value=ports.CommandOutput(exit_code=0, stdout=b"[]", stderr=b"")),
        Err(error=diagnostic(ErrorCode.dependency_missing)),
        Err(error=diagnostic(ErrorCode.dependency_incompatible)),
        RuntimeError("wrong shaped probe result"),
    ],
)
async def test_production_inventory_failure_reaches_the_provider(outcome: Any) -> None:
    process = FakeProcess()
    process.queue_result("run", outcome)
    provider, portal, _ = make_provider(inventory=KscreenConnectorInventory(process))

    listed = await provider.list_physical()
    assert isinstance(listed, Err)
    assert listed.error.code is ErrorCode.unsupported_source

    selected = await provider.select("DP-1")
    assert isinstance(selected, Err)
    assert selected.error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []


async def test_a_verified_empty_inventory_is_not_a_probe_failure() -> None:
    provider, portal, _ = make_provider(
        inventory=KscreenConnectorInventory(kscreen_process([]))
    )

    listed = await provider.list_physical()

    assert isinstance(listed, Ok)
    assert listed.value == ()
    selected = await provider.select("DP-1")
    assert isinstance(selected, Err)
    assert selected.error.code is ErrorCode.unsupported_source
    assert portal.select_calls == []


# --------------------------------------------------------------------------- fake parity


async def test_canonical_capture_fake_agrees_that_virtual_outputs_are_not_physical() -> None:
    fake = FakeCapture()
    fake.add_physical(physical("DP-1"))
    fake.add_physical(PhysicalMonitorSource(connector="Virtual-1", portal_restore_token=None))

    listed = await fake.list_physical()

    assert isinstance(listed, Ok)
    assert [source.connector for source in listed.value] == ["DP-1"]
    assert not is_physical_descriptor(
        VirtualOutputSource(
            connector="Virtual-1", kscreen_output_id="8", size=Size(width=1920, height=1080)
        ),
        ("DP-1", "Virtual-1"),
    )
