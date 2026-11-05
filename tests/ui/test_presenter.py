"""Presenter tests: GUI intent becomes fenced domain commands, nothing else.

The presenter is the only place where a dashboard signal may turn into a domain
command.  Every test here therefore drives the *real* :class:`MainWindow` widget
tree through its public signals and asserts three things at once:

* only typed domain commands reach ``ManagerPort.dispatch`` (and each action
  gets its own fresh command rather than a cached one);
* the pane is rendered from the projection, so a disabled control genuinely
  refuses even when a signal is emitted programmatically;
* asynchronous completions - dispatch receipts, prompt answers, delayed source
  and ownership loads - are applied only while they still belong to the current
  intent, and are never applied to a receiver the user has since selected.

Ports are the deterministic fakes from :mod:`airplay_linux.testing.adapters`, so
no test needs a network, a keyring, a desktop or a real clock.  The dashboard is
shown headless; the asyncio loop comes from pytest-asyncio and Qt events are
pumped explicitly by ``rig.settle`` (which never sleeps for wall-clock time).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QWidget

from airplay_linux.diagnostics import ActivityLog, diagnostic, safe_activity
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.messages import (
    Cancel,
    ChangeResolution,
    ChooseResolution,
    Command,
    CommandReceipt,
    ConfirmOrphanRemoval,
    ConfirmSwitch,
    DiscoveryEvent,
    Forget,
    KeepCleanup,
    Pair,
    RecreateAccepted,
    Retry,
    RetryAudio,
    RetryCleanup,
    RetryRevocation,
    SetMuted,
    SetVolume,
    Start,
    StartVideoOnly,
    Stop,
    SubmitPairing,
)
from airplay_linux.domain.operations import (
    ChallengeId,
    ChallengeView,
    InstanceId,
    LeaseId,
    OperationId,
    OwnerId,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import (
    LayoutSnapshot,
    OwnershipRecord,
    RecoveryItem,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    CapabilityReport,
    Diagnostic,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    RecoveryAction,
    ResolutionDecision,
    ResolutionEvidence,
    Result,
    SessionPhase,
    Size,
    TransportState,
    TrustState,
    VolumeLevel,
)
from airplay_linux.testing.adapters import (
    Deferred,
    FakeCapture,
    FakeDiscovery,
    FakeManager,
    MemoryOwnershipStore,
    MemoryReceiverStore,
)
from airplay_linux.testing.builders import physical, service, target, virtual_handle

from airplay_linux.ui.dialogs import PairingAnswer
from airplay_linux.ui.presenter import Presenter, QtDialogFactory
from airplay_linux.ui.state import Action
from airplay_linux.ui.window import MainWindow

RECEIVER_A = ReceiverId("airplay-device:a")
RECEIVER_B = ReceiverId("airplay-device:b")
ENVIRONMENT = "KDE Plasma 6 Wayland"
HD = Size(width=1920, height=1080)
HD_READY = Size(width=1280, height=720)

#: Every type the frozen ``Command`` union admits; nothing else may be dispatched.
COMMAND_TYPES: tuple[type, ...] = (
    Start, Stop, Cancel, Retry, SubmitPairing, Pair, Forget, ChooseResolution,
    ChangeResolution, RecreateAccepted, SetMuted, SetVolume, RetryAudio, StartVideoOnly,
    RetryCleanup, RetryRevocation, KeepCleanup, ConfirmSwitch, ConfirmOrphanRemoval,
)


def receiver_id(letter: str) -> ReceiverId:
    return ReceiverId(f"airplay-device:{letter}")


def capability_report(**overrides: Any) -> CapabilityReport:
    fields: dict[str, Any] = {
        "environment": ENVIRONMENT,
        "discovery": True,
        "mirror": True,
        "virtual_display": True,
        "audio": True,
        "remembered_pairing": True,
        "additional_virtual_outputs": True,
        "diagnostics": (),
    }
    fields.update(overrides)
    return CapabilityReport(**fields)


def decision(letter: str = "a", size: Size = HD) -> Any:
    """A remembered decision stamped with that receiver's endpoint."""
    return ResolutionDecision(
        size=size,
        source="stored",
        evidence=ResolutionEvidence(
            origin="remembered",
            reliable=True,
            receiver_id=receiver_id(letter),
            endpoint_fingerprint=f"endpoint-{letter}-v1",
        ),
    )


def saved_receiver(letter: str = "a", **overrides: Any) -> SavedReceiver:
    receiver = receiver_id(letter)
    fields: dict[str, Any] = {
        "receiver_id": receiver,
        "service_metadata": (service(address=f"192.0.2.{ord(letter)}"),),
        "display_name": f"TV {letter.upper()}",
        "last_endpoint": target(letter),
        "continuity_fingerprint": f"continuity-{letter}",
        "credential_ref": None,
        "remembered_resolution": None,
    }
    fields.update(overrides)
    return SavedReceiver(**fields)


def ownership_record(
    letter: str = "a",
    *,
    owner: int = 7,
    desired_state: str = "active",
    stage: str = "output_observed",
    connector: str = "Virtual-v1",
    output_id: str = "v1",
    size: Size = HD,
) -> OwnershipRecord:
    observed = size if stage in ("output_observed", "published") else None
    return OwnershipRecord(
        schema_version=1,
        owner_uuid=OwnerId(UUID(int=owner)),
        app_instance_uuid=InstanceId(UUID(int=1)),
        runtime_directory="/run/user/1000/airplay-linux/instances/1",
        creation_operation_id=OperationId(UUID(int=owner + 100)),
        receiver_id=receiver_id(letter),
        endpoint_fingerprint=f"endpoint-{letter}-v1",
        desired_state=desired_state,
        stage=stage,
        requested_size=size,
        observed_size=observed,
        helper=ProcessIdentity(pid=4000 + owner, start_ticks=90000 + owner),
        connector=connector if desired_state != "removing" else connector,
        output_id=output_id,
        created_utc="2026-10-17T00:00:00Z",
        last_configuration_step=stage,
        baseline=LayoutSnapshot(outputs={}, baseline_hash="baseline"),
        last_applied={},
        provider_version="fake-kscreen-1",
        capability_fingerprint="fake-capability",
    )


def challenge(receiver: ReceiverId = RECEIVER_A, *, challenge_id: int = 11) -> ChallengeView:
    return ChallengeView(
        challenge_id=ChallengeId(UUID(int=challenge_id)),
        receiver_id=receiver,
        endpoint_fingerprint=f"endpoint-{receiver}-v1",
        endpoint_generation=1,
        operation_id=OperationId(UUID(int=challenge_id + 500)),
        session_generation=1,
        trust_generation=0,
        created_at=0.0,
        expires_at=120.0,
        credential_kind="pin",
    )


def stream_handle(receiver: ReceiverId = RECEIVER_A, *, lease: int = 3) -> StreamHandle:
    return StreamHandle(
        receiver_id=receiver,
        endpoint_generation=1,
        endpoint_fingerprint=f"endpoint-{receiver}-v1",
        backend_stream_id=f"stream-{lease}",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=LeaseId(UUID(int=lease)),
        start_operation_id=OperationId(UUID(int=900 + lease)),
    )


# --------------------------------------------------------------------------- dialogs


class FakeDialogs:
    """An async ``DialogFactory`` whose answers are preprogrammed by a test.

    A ``*_gate`` future keeps one prompt outstanding, which is how the stale
    prompt/selection tests observe a result that arrives much later.
    """

    def __init__(self) -> None:
        self.pairing_calls: list[ChallengeView] = []
        self.pairing_contexts: list[tuple[frozenset[Action], str | None]] = []
        self.invalidated_pairings: list[ChallengeView] = []
        self.stop_calls: list[Mode] = []
        self.resolution_calls: list[tuple[Size, ...]] = []
        self.orphan_calls: list[RecoveryItem] = []
        self.pairing_answer: SensitiveText | None = None
        self.stop_answer: bool = False
        self.resolution_answer: Size | None = None
        self.orphan_answer: bool = False
        self.pairing_gate: asyncio.Future[Any] | None = None
        self.stop_gate: asyncio.Future[Any] | None = None
        self.resolution_gate: asyncio.Future[Any] | None = None
        self.orphan_gate: asyncio.Future[Any] | None = None

    def gate(self, name: str) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        setattr(self, f"{name}_gate", future)
        return future

    async def pairing(
        self,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action] | None = None,
        retained_output: str | None = None,
    ) -> SensitiveText | PairingAnswer | None:
        self.pairing_calls.append(challenge)
        self.pairing_contexts.append((actions or frozenset(), retained_output))
        gate, self.pairing_gate = self.pairing_gate, None
        if gate is not None:
            return await gate
        return self.pairing_answer

    def invalidate_pairing(self, challenge: ChallengeView) -> None:
        self.invalidated_pairings.append(challenge)

    async def confirm_stop(self, mode: Mode) -> bool:
        self.stop_calls.append(mode)
        gate, self.stop_gate = self.stop_gate, None
        if gate is not None:
            return await gate
        return self.stop_answer

    async def resolution(self, choices: tuple[Size, ...]) -> Size | None:
        self.resolution_calls.append(choices)
        gate, self.resolution_gate = self.resolution_gate, None
        if gate is not None:
            return await gate
        return self.resolution_answer

    async def orphan(self, item: RecoveryItem) -> bool:
        self.orphan_calls.append(item)
        gate, self.orphan_gate = self.orphan_gate, None
        if gate is not None:
            return await gate
        return self.orphan_answer


# ------------------------------------------------------------------------------ rig


class FakeScreenCast:
    """A permission store double: one call, one canned outcome.

    ``commands`` is the manager's journal, sampled when the clear runs, so a test
    can prove the clear happened before anything was dispatched.
    """

    def __init__(self, result: Result[Any] | None = None, commands: list[Any] | None = None) -> None:
        self.result: Result[Any] = Ok(value=2) if result is None else result
        self.calls = 0
        self.commands = commands
        self.command_counts: list[int] = []

    async def forget_all(self) -> Result[Any]:
        self.calls += 1
        if self.commands is not None:
            self.command_counts.append(len(self.commands))
        return self.result


class PresenterRig:
    """A real dashboard wired to deterministic ports and preprogrammed dialogs."""

    def __init__(self, screencast: Any | None = None) -> None:
        self.window = MainWindow()
        self.manager = FakeManager()
        self.discovery = FakeDiscovery()
        self.capture = FakeCapture()
        self.receivers = MemoryReceiverStore()
        self.ownerships = MemoryOwnershipStore()
        self.activity = ActivityLog()
        self.dialogs = FakeDialogs()
        self.screencast = screencast if screencast is not None else FakeScreenCast()
        if isinstance(self.screencast, FakeScreenCast) and self.screencast.commands is None:
            # Sample the manager's journal from inside the clear, so a test can
            # prove the clear ran before any command was dispatched.
            self.screencast.commands = self.manager.commands
        self.capabilities = capability_report()
        #: The report a capability re-probe publishes, if the test staged one.
        self.refreshed_capabilities: CapabilityReport | None = None
        self.refresh_calls = 0
        self.shutdown_result: Result[None] = Ok(value=None)
        self.shutdown_calls = 0
        self.shutdown_gate: asyncio.Future[Any] | None = None
        self.presenter = Presenter(
            window=self.window,
            manager=self.manager,
            discovery=self.discovery,
            capture=self.capture,
            receivers=self.receivers,
            capabilities=lambda: self.capabilities,
            activity=self.activity,
            ownerships=self.ownerships,
            dialogs=self.dialogs,
            request_shutdown=self._request_shutdown,
            refresh_capabilities=self._refresh_capabilities,
            screencast=self.screencast,
        )
        self._started = False

    async def _refresh_capabilities(self) -> None:
        self.refresh_calls += 1
        if self.refreshed_capabilities is not None:
            self.capabilities = self.refreshed_capabilities

    async def _request_shutdown(self) -> Result[None]:
        self.shutdown_calls += 1
        gate, self.shutdown_gate = self.shutdown_gate, None
        if gate is not None:
            await gate
        return self.shutdown_result

    def hold_shutdown(self) -> asyncio.Future[Any]:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.shutdown_gate = future
        return future

    async def start(self) -> None:
        if not self._started:
            self._started = True
            await self.presenter.start()
        await self.settle()

    async def settle(self, rounds: int = 40) -> None:
        """Deliver queued Qt work and every ready task, with no wall-clock sleep."""
        for _ in range(rounds):
            QApplication.processEvents()
            await asyncio.sleep(0)
        QApplication.processEvents()

    # -- environment ----------------------------------------------------

    def discover(
        self, letter: str = "a", *, generation: int = 1, online: bool = True,
    ) -> ReceiverTarget:
        found = target(letter, generation)
        self.discovery.add_target(found)
        if not online:
            self.discovery.remove_target(receiver_id(letter))
        return found

    def snapshot(self, letter: str = "a", **overrides: Any) -> SessionSnapshot:
        """Publish one authoritative session snapshot for ``letter``."""
        receiver = receiver_id(letter)
        generation = overrides.pop("generation", 1)
        online = overrides.pop("online", True)
        defaults: dict[str, Any] = {
            "receiver_id": receiver,
            "target": target(letter, generation) if online else None,
            "online": online,
            "phase": SessionPhase.Available if online else SessionPhase.Offline,
            "trust": TrustState.Untrusted,
            "transport": TransportState.Idle,
        }
        defaults.update(overrides)
        published = SessionSnapshot(**defaults)
        self.manager.emit_snapshot(published)
        return published

    def select(self, letter: str = "a") -> None:
        self.window.receiver_selected.emit(str(receiver_id(letter)))

    # -- observability --------------------------------------------------

    def rows(self) -> tuple[Any, ...]:
        return self.window._model.rows

    def row_ids(self) -> list[ReceiverId]:
        return [row.receiver_id for row in self.rows()]

    def pane(self) -> Any:
        return self.window._pane

    def offered(self) -> frozenset[Action]:
        pane = self.pane()
        return frozenset() if pane is None else pane.actions

    def commands_of(self, kind: type) -> list[Any]:
        return [command for command in self.manager.commands if isinstance(command, kind)]

    def error_text(self) -> str:
        return self.window._error.text()

    def activity_text(self) -> str:
        return self.window._activity.toPlainText()

    def summary_text(self) -> str:
        return self.window._summary.text()

    def environment_text(self) -> str:
        return self.window._environment.text()

    def click(self, action: Action | str) -> None:
        value = action.value if isinstance(action, Action) else action
        self.window.action_requested.emit(value)

    def choose_source(self, connector: str) -> None:
        self.window.source_selected.emit(connector)

    def choose_resolution(self, size: Size) -> None:
        self.window.resolution_selected.emit(size.width, size.height)

    def request_mode(self, mode: Mode | str) -> None:
        self.window.mode_requested.emit(mode.value if isinstance(mode, Mode) else mode)

    def request_mute(self, muted: bool) -> None:
        self.window.mute_requested.emit(muted)


@pytest.fixture
async def rig(qtbot: Any) -> Any:
    """A started dashboard rig, torn down without leaking pending tasks."""
    prepared = PresenterRig()
    qtbot.addWidget(prepared.window)
    try:
        yield prepared
    finally:
        for name in ("pairing", "stop", "resolution", "orphan", "shutdown"):
            gate = getattr(prepared.dialogs, f"{name}_gate", None)
            if gate is not None and not gate.done():
                gate.set_result(None)
        await prepared.settle(10)
        await prepared.presenter.close()
        await prepared.settle(10)


async def started(rig: PresenterRig) -> PresenterRig:
    await rig.start()
    return rig


# --------------------------------------------------------------------- rendering


async def test_start_renders_the_receiver_union_and_subscribes_to_every_source(
    rig: PresenterRig,
) -> None:
    """The list is discovery + saved metadata; the pane is the projection itself."""
    rig.receivers.receivers[RECEIVER_B] = saved_receiver("b")
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")

    await prepared.settle()

    assert prepared.row_ids() == [RECEIVER_A, RECEIVER_B]
    assert prepared.pane() is not None
    assert prepared.pane().title == "Living Room 192.0.2.10"
    # Both channels are live: a discovery change and a manager snapshot re-render.
    prepared.discover("b")
    prepared.snapshot("b", online=True)
    prepared.manager.emit_snapshot(
        SessionSnapshot(
            receiver_id=RECEIVER_A,
            target=target("a"),
            online=True,
            mode=Mode.MIRROR,
            intent_active=True,
            phase=SessionPhase.Mirroring,
            transport=TransportState.Streaming,
            audio=AudioState.Enabled,
            trust=TrustState.Trusted,
            stream=stream_handle(),
            session_generation=2,
        )
    )
    await prepared.settle()

    assert prepared.pane().phase_label == "Mirroring"
    assert prepared.summary_text() == "2 TVs available  1 session"
    assert prepared.environment_text() == ENVIRONMENT
    assert prepared.discovery.journal.names().count("discovery.refresh") == 1
    # Every rendered selection loads the host monitor list; the boot order may
    # visit an offline saved record before the discovered receiver.
    assert prepared.capture.journal.names().count("capture.list_physical") >= 1


async def test_activity_strip_renders_only_the_safe_log(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.activity.emit(
        safe_activity("discovery.started", None, {}, "2026-10-17T00:00:00Z")
    )
    await prepared.settle()

    assert "Receiver discovery started." in prepared.activity_text()
    event = prepared.activity.entries()[0]
    assert prepared.activity_text() == f"{event.utc}  {event.message}"


async def test_scan_refreshes_discovery_and_keeps_every_receiver_visible(
    rig: PresenterRig,
) -> None:
    """Scan is a user action for *all* receivers, not just the selected one."""
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    prepared.discover("b")
    prepared.snapshot("b", online=True)
    await prepared.settle()
    prepared.select("a")
    await prepared.settle()

    prepared.click(Action.scan)
    await prepared.settle()

    assert prepared.discovery.journal.names().count("discovery.refresh") == 2
    assert prepared.row_ids() == [RECEIVER_A, RECEIVER_B]
    # A later update to the *other* receiver still updates its own row.
    prepared.manager.emit_snapshot(
        SessionSnapshot(
            receiver_id=RECEIVER_B,
            target=target("b"),
            online=True,
            mode=Mode.VIRTUAL,
            intent_active=True,
            phase=SessionPhase.VirtualDisplay,
            transport=TransportState.Streaming,
            display=DisplayState.Owned,
            audio=AudioState.Enabled,
            trust=TrustState.Trusted,
            session_generation=2,
        )
    )
    await prepared.settle()

    rows = {row.receiver_id: row for row in prepared.rows()}
    assert rows[RECEIVER_B].phase_label == "Virtual display"
    assert rows[RECEIVER_A].phase_label == "Available"
    assert prepared.pane().title == "Living Room 192.0.2.10"


async def test_a_saved_offline_receiver_is_hidden_once_metadata_is_removed(
    rig: PresenterRig,
) -> None:
    record = ownership_record("c", owner=9)
    rig.ownerships.records[record.owner_uuid] = record
    rig.receivers.receivers[RECEIVER_B] = saved_receiver("b")
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()

    assert set(prepared.row_ids()) == {RECEIVER_A, RECEIVER_B, receiver_id("c")}

    prepared.select("b")
    await prepared.settle()
    assert Action.remove_metadata in prepared.offered()

    prepared.click(Action.remove_metadata)
    await prepared.settle()

    assert prepared.receivers.journal.calls("receiver_store.remove_metadata") == [
        {"receiver_id": RECEIVER_B}
    ]
    assert RECEIVER_B not in prepared.row_ids()
    # Metadata removal never touches durable display ownership evidence.
    assert prepared.ownerships.records[record.owner_uuid] == record
    assert prepared.ownerships.journal.names().count("ownership_store.delete") == 0


# ---------------------------------------------------------------------- commands


async def test_only_typed_domain_commands_reach_the_manager(rig: PresenterRig) -> None:
    """One fresh command object per action, never a composed port call."""
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()

    prepared.click(Action.start)
    prepared.click(Action.start)
    await prepared.settle()

    assert len(prepared.manager.commands) == 2
    for command in prepared.manager.commands:
        assert isinstance(command, Command), type(command)
    assert all(type(command) in COMMAND_TYPES for command in prepared.manager.commands)
    first, second = prepared.manager.commands
    assert isinstance(first, Start) and isinstance(second, Start)
    assert first is not second  # a fresh command per user action
    # The presenter consumes the ManagerPort only: no session implementation exists
    # in this process at all.
    assert "airplay_linux.session.engine" not in type(prepared.manager).__module__


async def test_start_dispatches_a_typed_request_for_the_selected_mode(
    rig: PresenterRig,
) -> None:
    source = physical("DP-1")
    rig.capture.add_physical(source)
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()

    prepared.choose_source("DP-1")
    prepared.click(Action.start)
    await prepared.settle()

    (start,) = prepared.commands_of(Start)
    assert start.receiver_id == RECEIVER_A
    assert start.request.mode is Mode.MIRROR
    # The desktop chooses the screen; the request never names one.
    assert start.request.source is None
    assert start.request.resolution is None
    assert start.request.audio.enabled is True

    # An idle mode change is local selection, not a command: the next Start is
    # the command that carries it.
    prepared.manager.commands.clear()
    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    assert prepared.manager.commands == []
    prepared.click(Action.start)
    await prepared.settle()

    (start,) = prepared.commands_of(Start)
    assert start.request.mode is Mode.VIRTUAL
    assert start.request.source is None


async def test_sources_and_resolutions_are_receiver_local(rig: PresenterRig) -> None:
    rig.capture.add_physical(physical("DP-1"))
    rig.capture.add_physical(physical("HDMI-A-1"))
    rig.receivers.receivers[RECEIVER_A] = saved_receiver(
        "a", remembered_resolution=decision("a", HD)
    )
    rig.receivers.receivers[RECEIVER_B] = saved_receiver(
        "b", remembered_resolution=decision("b", HD_READY)
    )
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    prepared.snapshot("a")
    prepared.snapshot("b")
    await prepared.settle()

    prepared.select("a")
    await prepared.settle()
    prepared.choose_source("DP-1")
    await prepared.settle()
    prepared.select("b")
    await prepared.settle()
    prepared.choose_source("HDMI-A-1")
    await prepared.settle()

    # The editors show each receiver's own choice, never the other receiver's.
    prepared.select("a")
    await prepared.settle()
    assert prepared.window._source.currentText() == "DP-1"
    prepared.click(Action.start)
    await prepared.settle()
    (start_a,) = prepared.commands_of(Start)
    assert start_a.receiver_id == RECEIVER_A
    # Mirror asks the desktop for a screen; it never names one itself.
    assert start_a.request.source is None

    prepared.manager.commands.clear()
    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    assert prepared.window._resolution.currentText() == f"{HD.width}x{HD.height}"
    prepared.click(Action.start)
    await prepared.settle()
    (start_a,) = prepared.commands_of(Start)
    assert start_a.receiver_id == RECEIVER_A
    assert start_a.request.source is None
    assert start_a.request.resolution.size == HD
    assert start_a.request.resolution.source == "stored"

    prepared.manager.commands.clear()
    prepared.select("b")
    await prepared.settle()
    assert prepared.window._source.currentText() == "HDMI-A-1"
    prepared.request_mode(Mode.VIRTUAL)
    prepared.click(Action.start)
    await prepared.settle()
    (start_b,) = prepared.commands_of(Start)
    assert start_b.receiver_id == RECEIVER_B
    assert start_b.request.resolution.size == HD_READY


async def test_the_resolution_picker_offers_only_this_receivers_sizes(
    rig: PresenterRig,
) -> None:
    rig.receivers.receivers[RECEIVER_A] = saved_receiver(
        "a", remembered_resolution=decision("a", HD)
    )
    rig.receivers.receivers[RECEIVER_B] = saved_receiver(
        "b", remembered_resolution=decision("b", HD_READY)
    )
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    prepared.snapshot("a", phase=SessionPhase.ResolutionRequired, mode=Mode.VIRTUAL,
                      intent_active=True, intent_stage=IntentStage.resolution,
                      transport=TransportState.Idle, trust=TrustState.Untrusted)
    prepared.snapshot("b", phase=SessionPhase.ResolutionRequired, mode=Mode.VIRTUAL,
                      intent_active=True, intent_stage=IntentStage.resolution,
                      transport=TransportState.Idle, trust=TrustState.Untrusted)
    await prepared.settle()
    prepared.select("a")
    await prepared.settle()
    assert Action.choose_resolution in prepared.offered()

    prepared.dialogs.resolution_answer = HD
    prepared.click(Action.choose_resolution)
    await prepared.settle()

    assert prepared.dialogs.resolution_calls == [(HD,)]
    (command,) = prepared.commands_of(ChooseResolution)
    assert command.receiver_id == RECEIVER_A
    assert command.decision.size == HD
    assert command.decision.source == "user"
    assert command.decision.evidence.receiver_id == RECEIVER_A
    assert command.decision.evidence.endpoint_fingerprint == "endpoint-a-v1"

    # A dismissed picker dispatches nothing and never guesses a size.
    prepared.manager.commands.clear()
    prepared.dialogs.resolution_answer = None
    prepared.click(Action.choose_resolution)
    await prepared.settle()
    assert prepared.manager.commands == []


async def test_a_disabled_action_genuinely_refuses(rig: PresenterRig) -> None:
    """A programmatic signal cannot bypass the projection's action set."""
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a", trust=TrustState.Pairing, phase=SessionPhase.Pairing)
    await prepared.settle()

    assert Action.start not in prepared.offered()
    for action in (
        Action.start, Action.stop, Action.retry_cleanup, Action.keep_cleanup,
        Action.confirm_orphan, Action.change_resolution, Action.recreate,
        Action.retry_audio, Action.video_only, Action.remove_metadata,
        Action.submit_pairing, Action.choose_resolution,
    ):
        prepared.click(action)
    await prepared.settle()

    assert prepared.manager.commands == []
    assert prepared.receivers.journal.calls("receiver_store.remove_metadata") == []


async def test_a_capability_retry_reprobes_instead_of_replaying_the_frozen_report(
    rig: PresenterRig,
) -> None:
    """The pane promises "retry after fixing it"; Retry must re-read the evidence."""
    rig.capabilities = capability_report(
        mirror=False,
        diagnostics=(
            replace(
                diagnostic(ErrorCode.dependency_incompatible), detail="physical_source"
            ),
        ),
    )
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()
    assert Action.start not in prepared.offered()
    assert Action.retry in prepared.offered()

    prepared.refreshed_capabilities = capability_report()
    prepared.click(Action.retry)
    await prepared.settle()

    assert prepared.refresh_calls == 1
    # The gate is gone, so the same selection can start, and no session command
    # was invented for a capability question.
    assert Action.start in prepared.offered()
    assert prepared.commands_of(Retry) == []


async def test_a_rejected_command_renders_a_catalog_diagnostic(
    rig: PresenterRig,
) -> None:
    """A refused dispatch is shown as fixed catalog copy, never backend text."""
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    prepared.manager.queue_result(
        "dispatch",
        CommandReceipt(
            operation_id=OperationId(UUID(int=42)),
            accepted=False,
            diagnostic=Diagnostic(
                code=ErrorCode.transport_failed,
                message="raw-backend-sentinel-9137",
                recovery=(RecoveryAction.retry,),
                detail="argv sentinel",
            ),
        ),
    )
    await prepared.settle()

    prepared.choose_source("DP-1")
    prepared.click(Action.start)
    await prepared.settle()

    assert len(prepared.manager.commands) == 1
    expected = diagnostic(ErrorCode.transport_failed, RECEIVER_A).message
    assert prepared.error_text() == expected
    assert "sentinel" not in prepared.error_text()
    assert "sentinel" not in prepared.activity_text()


async def test_a_dispatch_in_flight_does_not_block_the_dashboard(
    rig: PresenterRig,
) -> None:
    """Dispatch is scheduled; the pane keeps rendering while the port is pending."""
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    deferred = Deferred()
    prepared.manager.queue_result("dispatch", deferred)
    await prepared.settle()

    prepared.choose_source("DP-1")
    prepared.click(Action.start)
    await prepared.settle()

    assert len(prepared.manager.commands) == 1  # dispatched immediately
    assert prepared.pane().phase_label == "Available"
    assert Action.stop not in prepared.offered()

    deferred.resolve(
        CommandReceipt(
            operation_id=OperationId(UUID(int=43)), accepted=True, diagnostic=None
        )
    )
    await prepared.settle()
    assert prepared.error_text() == ""


# ------------------------------------------------------------------ snapshots


def mirror_snapshot(prepared: PresenterRig, letter: str = "a", **overrides: Any) -> SessionSnapshot:
    """One established Mirror session with audio and a live stream."""
    receiver = receiver_id(letter)
    defaults: dict[str, Any] = {
        "mode": Mode.MIRROR,
        "intent_active": True,
        "intent_stage": IntentStage.active,
        "phase": SessionPhase.Mirroring,
        "transport": TransportState.Streaming,
        "audio": AudioState.Enabled,
        "trust": TrustState.Trusted,
        "stream": stream_handle(receiver),
        "session_generation": 3,
    }
    defaults.update(overrides)
    return prepared.snapshot(letter, **defaults)


def virtual_snapshot(prepared: PresenterRig, letter: str = "a", **overrides: Any) -> SessionSnapshot:
    """One established Virtual display session with an owned output."""
    handle = virtual_handle(owner=5, output="v1")
    receiver = receiver_id(letter)
    defaults: dict[str, Any] = {
        "mode": Mode.VIRTUAL,
        "intent_active": True,
        "intent_stage": IntentStage.active,
        "phase": SessionPhase.VirtualDisplay,
        "transport": TransportState.Streaming,
        "display": DisplayState.Owned,
        "display_handle": handle,
        "ownership_uuid": handle.owner_uuid,
        "audio": AudioState.Enabled,
        "trust": TrustState.Trusted,
        "observed_resolution": HD,
        "session_generation": 3,
    }
    defaults.update(overrides)
    return prepared.snapshot(letter, **defaults)


# ------------------------------------------------------------- mode switching


async def test_unconfirmed_switch_never_queues_next_start(rig: PresenterRig) -> None:
    """The brief's regression: a declined confirmation queues nothing at all."""
    rig.discover("a")
    prepared = await started(rig)
    mirror_snapshot(prepared)
    await prepared.settle()
    prepared.dialogs.stop_answer = False

    prepared.request_mode("virtual")
    await prepared.settle()

    assert prepared.dialogs.stop_calls == [Mode.VIRTUAL]
    assert not any(
        isinstance(command, ConfirmSwitch) and command.confirmed
        for command in prepared.manager.commands
    )
    assert prepared.manager.commands == []
    assert prepared.pane().visible_mode is Mode.MIRROR


async def test_confirmed_switch_keeps_the_old_mode_until_cleanup_completes(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    mirror_snapshot(prepared)
    await prepared.settle()
    prepared.dialogs.stop_answer = True

    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()

    (confirm,) = prepared.commands_of(ConfirmSwitch)
    assert confirm.receiver_id == RECEIVER_A
    assert confirm.confirmed is True
    assert confirm.request.mode is Mode.VIRTUAL
    assert prepared.commands_of(Start) == []
    # Live mode switching stays locked: the old mode is still rendered and the
    # new mode is only queued behind the confirmed teardown.
    assert prepared.pane().visible_mode is Mode.MIRROR
    assert prepared.pane().mode_enabled is False

    # A second request in the same session generation does not prompt again.
    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    assert prepared.dialogs.stop_calls == [Mode.VIRTUAL]
    assert len(prepared.commands_of(ConfirmSwitch)) == 1

    # Only the manager's new snapshot moves the pane to the queued mode.
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.starting,
        phase=SessionPhase.Connecting,
        transport=TransportState.Connecting,
        trust=TrustState.Trusted,
        session_generation=4,
    )
    await prepared.settle()
    assert prepared.pane().visible_mode is Mode.VIRTUAL


async def test_a_confirmation_for_a_superseded_session_is_dropped(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    mirror_snapshot(prepared)
    await prepared.settle()
    gate = prepared.dialogs.gate("stop")

    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    assert prepared.dialogs.stop_calls == [Mode.VIRTUAL]

    # A newer session generation arrived while the prompt was open.
    prepared.snapshot(
        "a",
        mode=Mode.MIRROR,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.Mirroring,
        transport=TransportState.Streaming,
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
        stream=stream_handle(RECEIVER_A, lease=4),
        session_generation=4,
    )
    await prepared.settle()
    gate.set_result(True)
    await prepared.settle()

    assert prepared.commands_of(ConfirmSwitch) == []


async def test_switching_is_refused_while_cleanup_is_uncertain(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        phase=SessionPhase.CleanupRequired,
        display=DisplayState.CleanupRequired,
        cleanup_pending=True,
        intent_stage=IntentStage.cleanup,
        trust=TrustState.Trusted,
        transport=TransportState.Idle,
        ownership_uuid=OwnerId(UUID(int=31)),
        session_generation=3,
    )
    await prepared.settle()

    prepared.request_mode(Mode.MIRROR)
    await prepared.settle()

    assert prepared.dialogs.stop_calls == []
    assert prepared.manager.commands == []
    assert Action.start not in prepared.offered()
    assert Action.retry_cleanup in prepared.offered()


async def test_an_idle_mode_request_is_local_and_per_receiver(rig: PresenterRig) -> None:
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    prepared.snapshot("a")
    prepared.snapshot("b")
    await prepared.settle()

    prepared.select("a")
    await prepared.settle()
    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    prepared.select("b")
    await prepared.settle()

    assert prepared.pane().visible_mode is Mode.MIRROR
    prepared.select("a")
    await prepared.settle()
    assert prepared.pane().visible_mode is Mode.VIRTUAL
    assert prepared.dialogs.stop_calls == []
    assert prepared.manager.commands == []


# ---------------------------------------------------------------------- audio


@pytest.mark.parametrize("capable", [False, True])
async def test_audio_actions_affect_only_the_selected_receiver(rig: PresenterRig, capable) -> None:
    rig.capabilities = capability_report(volume=capable)
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    mirror_snapshot(prepared, "a")
    mirror_snapshot(prepared, "b")
    await prepared.settle()
    prepared.select("a")
    await prepared.settle()

    prepared.request_mute(True)
    await prepared.settle()
    (mute,) = prepared.commands_of(SetMuted)
    assert mute.receiver_id == RECEIVER_A
    assert mute.muted is True
    assert Action.retry_audio not in prepared.offered()

    prepared.manager.commands.clear()
    prepared.select("b")
    await prepared.settle()
    mirror_snapshot(prepared, "b", audio=AudioState.Unavailable, muted=False)
    await prepared.settle()
    assert prepared.pane().mute_enabled is False
    assert Action.retry_audio in prepared.offered()

    prepared.click(Action.retry_audio)
    await prepared.settle()
    (retry,) = prepared.commands_of(RetryAudio)
    assert retry.receiver_id == RECEIVER_B


async def test_volume_dispatch_captures_receiver_and_never_writes_preferences(rig: PresenterRig):
    rig.capabilities = capability_report(volume=True)
    rig.discover("a")
    rig.discover("b")
    await rig.start()
    mirror_snapshot(rig, "a", effective_volume=30)
    mirror_snapshot(rig, "b", effective_volume=60)
    await rig.settle()
    rig.select("a")
    rig.window.volume_requested.emit(44)
    rig.select("b")  # change selection before either scheduled dispatch starts
    rig.window.volume_requested.emit(71)
    await rig.settle()
    assert [(c.receiver_id, c.level) for c in rig.commands_of(SetVolume)] == [
        (RECEIVER_A, VolumeLevel(value=44)), (RECEIVER_B, VolumeLevel(value=71)),
    ]
    assert all(type(command) in COMMAND_TYPES for command in rig.manager.commands)
    assert not rig.receivers.journal.calls("receiver_store.put")
    rig.manager.commands.clear()
    rig.select("a")
    rig.presenter._request_render()
    await rig.settle()
    assert rig.manager.commands == []
    assert rig.window._volume_readout.text() == "30%"


@pytest.mark.parametrize("invalid", [-1, 101, True, 1.5, "30", None])
async def test_volume_presenter_validates_exact_integer(rig: PresenterRig, invalid):
    rig.capabilities = capability_report(volume=True)
    rig.discover()
    await rig.start()
    mirror_snapshot(rig)
    await rig.settle()
    rig.presenter._on_volume_requested(invalid)
    await rig.settle()
    assert rig.manager.commands == []


@pytest.mark.parametrize("block", ["capability", "selection", "stream", "audio", "trust", "cleanup", "close"])
async def test_volume_disabled_malicious_signal_is_rejected(rig: PresenterRig, block):
    rig.capabilities = capability_report(volume=block != "capability")
    if block != "selection":
        rig.discover()
    await rig.start()
    if block != "selection":
        changes = {
            "stream": {"stream": None}, "audio": {"audio": AudioState.Unavailable},
            "trust": {"trust": TrustState.Pairing}, "cleanup": {"cleanup_pending": True},
            "close": {"audio_close_pending": True, "audio_policy": AudioPolicy(enabled=False)},
        }.get(block, {})
        mirror_snapshot(rig, **changes)
    await rig.settle()
    rig.window.volume_requested.emit(42)
    rig.window.mute_requested.emit(True) if block in {"trust", "cleanup", "close"} else None
    await rig.settle()
    assert rig.manager.commands == []


@pytest.mark.parametrize("stage", ["shutting_down", "disposed"])
async def test_volume_close_lifecycle_rejects_signals(rig: PresenterRig, stage):
    rig.capabilities = capability_report(volume=True)
    rig.discover()
    await rig.start()
    mirror_snapshot(rig)
    await rig.settle()
    if stage == "shutting_down":
        gate = rig.hold_shutdown()
        rig.window.closing_requested.emit()
    else:
        await rig.presenter.close()
    rig.window.volume_requested.emit(42)
    await rig.settle()
    assert rig.manager.commands == []
    if stage == "shutting_down":
        gate.set_result(None)
        await rig.settle()


@pytest.mark.parametrize("capable,enabled", [(True, True), (False, True), (True, False)])
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_volume_start_request_uses_only_approved_capable_preference(rig: PresenterRig, capable, enabled, mode):
    rig.capabilities = capability_report(volume=capable)
    rig.discover()
    rig.receivers.receivers[RECEIVER_A] = saved_receiver(remembered_volume=35)
    await rig.start()
    rig.snapshot(intent_active=not enabled, audio_policy=AudioPolicy(enabled=enabled))
    await rig.settle()
    request = rig.presenter._start_request(RECEIVER_A, mode)
    assert request.volume == (VolumeLevel(value=35) if capable and enabled else None)
    assert request.audio.enabled is enabled
    assert rig.manager.commands == []
    if enabled:
        rig.request_mode(mode)
        rig.click(Action.start)
        await rig.settle()
        assert rig.commands_of(Start)[0].request.volume == request.volume


@pytest.mark.parametrize("identity", ["absent", "replacement", "ambiguous", "nonpersistable", "unverified", "changed-fallback", "invalid-volume"])
async def test_volume_start_drops_unjustified_or_invalid_saved_preference(rig: PresenterRig, identity):
    from airplay_linux.storage.codecs import decode_receiver, encode_receiver
    rig.capabilities = capability_report(volume=True)
    rig.discover()
    saved = saved_receiver(remembered_volume=35)
    if identity == "invalid-volume":
        raw = json.loads(encode_receiver(saved)) | {"remembered_volume": 0}
        saved = decode_receiver(json.dumps(raw).encode())
    if identity != "absent":
        rig.receivers.receivers[RECEIVER_A] = saved
    if identity in {"replacement", "ambiguous", "nonpersistable", "unverified", "changed-fallback"}:
        view = rig.discovery.current(RECEIVER_A)
        changes = {
            "replacement": {"replacement_of": RECEIVER_B}, "ambiguous": {"ambiguous": True},
            "nonpersistable": {"persistable": False},
            "unverified": {"identity_kind": "service", "continuity_verified": False},
            "changed-fallback": {"identity_kind": "service", "continuity_verified": True},
        }[identity]
        rig.discovery.views[RECEIVER_A] = replace(view, record_set=replace(view.record_set, **changes))
    await rig.start()
    rig.snapshot()
    await rig.settle()
    rig.click(Action.start)
    await rig.settle()
    assert rig.commands_of(Start)[0].request.volume is None


async def test_volume_snapshot_reload_coalesces_and_latest_preference_wins(rig: PresenterRig):
    rig.capabilities = capability_report(volume=True)
    rig.discover("a")
    rig.discover("b")
    old = saved_receiver("a", remembered_volume=10)
    other = saved_receiver("b", remembered_volume=85)
    rig.receivers.receivers.update({RECEIVER_A: old, RECEIVER_B: other})
    await rig.start()
    snapshot = mirror_snapshot(rig)
    rig.snapshot("b")
    await rig.settle()
    calls = len(rig.receivers.journal.calls("receiver_store.all"))
    first, second = Deferred(), Deferred()
    rig.receivers.queue_result("all", first)
    rig.receivers.queue_result("all", second)
    rig.receivers.receivers[RECEIVER_A] = replace(old, remembered_volume=20)
    rig.manager.emit_snapshot(snapshot)  # equal publication still refreshes
    await rig.settle()
    assert len(rig.receivers.journal.calls("receiver_store.all")) == calls + 1
    rig.select("b")
    rig.receivers.receivers[RECEIVER_A] = replace(old, remembered_volume=60)
    rig.manager.emit_snapshot(snapshot)
    rig.manager.emit_snapshot(snapshot)
    await rig.settle()
    assert len(rig.receivers.journal.calls("receiver_store.all")) == calls + 1
    first.resolve(Ok(value=(replace(old, remembered_volume=20), other)))
    await rig.settle()
    assert len(rig.receivers.journal.calls("receiver_store.all")) == calls + 2
    assert rig.presenter._start_request(RECEIVER_A, Mode.MIRROR).volume == VolumeLevel(value=10)
    second.resolve(Ok(value=(replace(old, remembered_volume=60), other)))
    await rig.settle()
    assert rig.presenter._start_request(RECEIVER_A, Mode.MIRROR).volume == VolumeLevel(value=60)
    assert rig.presenter._start_request(RECEIVER_B, Mode.MIRROR).volume == VolumeLevel(value=85)
    assert rig.presenter._selected == RECEIVER_B
    assert rig.manager.commands == []


async def test_volume_delayed_reload_rechecks_replaced_identity(rig: PresenterRig):
    rig.capabilities = capability_report(volume=True)
    rig.discover()
    old = saved_receiver(remembered_volume=25)
    rig.receivers.receivers[RECEIVER_A] = old
    await rig.start()
    snapshot = mirror_snapshot(rig)
    await rig.settle()
    gate = Deferred()
    rig.receivers.queue_result("all", gate)
    rig.manager.emit_snapshot(snapshot)
    await rig.settle()
    view = rig.discovery.current(RECEIVER_A)
    replacement = replace(view, record_set=replace(view.record_set, replacement_of=RECEIVER_B))
    rig.discovery.views[RECEIVER_A] = replacement
    rig.discovery.emit(DiscoveryEvent(kind="updated", receiver=replacement, diagnostic=None))
    await rig.settle()
    gate.resolve(Ok(value=(replace(old, remembered_volume=60),)))
    await rig.settle()
    assert rig.presenter._start_request(RECEIVER_A, Mode.MIRROR).volume is None
    assert rig.manager.commands == []


@pytest.mark.parametrize("supported", [False, True])
async def test_volume_empty_window_uses_current_capability_reason(rig: PresenterRig, supported):
    rig.capabilities = capability_report(volume=supported)
    await rig.start()
    assert rig.window._volume_slider.toolTip() == (
        "Select a receiver to change its volume." if supported
        else "The installed backend does not support receiver volume."
    )
    assert rig.window._volume_readout.text() == "—"
    assert rig.manager.commands == []


async def test_audio_decision_requires_an_explicit_choice(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        mode=Mode.MIRROR,
        intent_active=True,
        intent_stage=IntentStage.audio_decision,
        phase=SessionPhase.AudioDecisionRequired,
        transport=TransportState.Idle,
        audio=AudioState.Unavailable,
        trust=TrustState.Trusted,
        physical_source=physical("DP-1"),
        session_generation=3,
    )
    await prepared.settle()

    # Entering the phase never infers a video-only start or an audio retry.
    assert prepared.manager.commands == []
    assert Action.retry_audio in prepared.offered()
    assert Action.video_only in prepared.offered()

    prepared.click(Action.retry_audio)
    await prepared.settle()
    assert prepared.commands_of(StartVideoOnly) == []
    (retry,) = prepared.commands_of(RetryAudio)
    assert retry.receiver_id == RECEIVER_A

    prepared.manager.commands.clear()
    prepared.click(Action.video_only)
    await prepared.settle()
    assert prepared.commands_of(RetryAudio) == []
    (video_only,) = prepared.commands_of(StartVideoOnly)
    assert video_only.receiver_id == RECEIVER_A


async def test_unavailable_audio_locks_out_mute(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    virtual_snapshot(prepared, audio=AudioState.Unavailable)
    await prepared.settle()

    assert prepared.pane().mute_enabled is False
    prepared.request_mute(True)
    await prepared.settle()

    assert prepared.commands_of(SetMuted) == []


# ------------------------------------------------------- cleanup and orphans


async def test_orphan_removal_needs_confirmation_and_the_exact_owner(
    rig: PresenterRig,
) -> None:
    record = ownership_record("a", owner=7)
    rig.ownerships.records[record.owner_uuid] = record
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()
    assert Action.confirm_orphan in prepared.offered()

    prepared.dialogs.orphan_answer = False
    prepared.click(Action.confirm_orphan)
    await prepared.settle()
    (item,) = prepared.dialogs.orphan_calls
    assert item.record.owner_uuid == record.owner_uuid
    assert prepared.commands_of(ConfirmOrphanRemoval) == []

    prepared.dialogs.orphan_answer = True
    prepared.click(Action.confirm_orphan)
    await prepared.settle()
    (command,) = prepared.commands_of(ConfirmOrphanRemoval)
    assert command.receiver_id == RECEIVER_A
    assert command.owner_uuid == record.owner_uuid
    assert command.confirmed is True
    # The durable record is still the provider's to delete; the UI never does.
    assert prepared.ownerships.records[record.owner_uuid] == record


async def test_a_confirmation_for_an_orphan_that_vanished_is_dropped(
    rig: PresenterRig,
) -> None:
    record = ownership_record("a", owner=8)
    rig.ownerships.records[record.owner_uuid] = record
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()
    gate = prepared.dialogs.gate("orphan")

    prepared.click(Action.confirm_orphan)
    await prepared.settle()
    # A scan re-reads ownership evidence while the prompt is open.
    del rig.ownerships.records[record.owner_uuid]
    prepared.click(Action.scan)
    await prepared.settle()
    gate.set_result(True)
    await prepared.settle()

    assert prepared.commands_of(ConfirmOrphanRemoval) == []


async def test_keep_blocked_and_keep_cleanup_keep_the_barrier_visible(
    rig: PresenterRig,
) -> None:
    record = ownership_record("a", owner=9, desired_state="removing", stage="removing")
    rig.ownerships.records[record.owner_uuid] = record
    rig.receivers.receivers[RECEIVER_A] = saved_receiver("a")
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        phase=SessionPhase.TrustCleanupRequired,
        trust=TrustState.CleanupRequired,
        transport=TransportState.Idle,
    )
    await prepared.settle()

    assert Action.retry_revocation in prepared.offered()
    assert Action.keep_blocked in prepared.offered()
    assert Action.start not in prepared.offered()

    prepared.click(Action.keep_blocked)
    await prepared.settle()

    assert prepared.manager.commands == []
    assert prepared.receivers.journal.calls("receiver_store.remove_metadata") == []
    badges = {badge.resource: badge for badge in prepared.pane().badges}
    assert badges["trust"].label == "Trust cleanup required"
    assert badges["trust"].severity == "error"
    assert Action.retry_revocation in prepared.offered()

    # Cleanup keeps its resource evidence too; KeepCleanup records the choice.
    prepared.snapshot(
        "a",
        display=DisplayState.CleanupRequired,
        phase=SessionPhase.CleanupRequired,
        cleanup_pending=True,
        intent_stage=IntentStage.cleanup,
        trust=TrustState.Trusted,
        ownership_uuid=record.owner_uuid,
        session_generation=2,
    )
    await prepared.settle()
    assert Action.keep_cleanup in prepared.offered()
    prepared.click(Action.keep_cleanup)
    await prepared.settle()
    (command,) = prepared.commands_of(KeepCleanup)
    assert command.receiver_id == RECEIVER_A
    assert rig.ownerships.records[record.owner_uuid] == record
    assert Action.retry_cleanup in prepared.offered()


async def test_remove_metadata_never_bypasses_trust_cleanup(rig: PresenterRig) -> None:
    record = ownership_record("a", owner=10)
    rig.ownerships.records[record.owner_uuid] = record
    rig.receivers.receivers[RECEIVER_A] = saved_receiver("a")
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        phase=SessionPhase.TrustCleanupRequired,
        trust=TrustState.CleanupRequired,
        transport=TransportState.Idle,
        display=DisplayState.None_,
    )
    await prepared.settle()

    prepared.click(Action.remove_metadata)
    await prepared.settle()

    assert prepared.receivers.journal.calls("receiver_store.remove_metadata") == [
        {"receiver_id": RECEIVER_A}
    ]
    assert prepared.commands_of(Forget) == []
    assert rig.ownerships.records[record.owner_uuid] == record
    assert prepared.ownerships.journal.names().count("ownership_store.delete") == 0
    badges = {badge.resource: badge for badge in prepared.pane().badges}
    assert badges["trust"].label == "Trust cleanup required"
    assert Action.retry_revocation in prepared.offered()


async def test_cleanup_and_mismatch_actions_dispatch_their_own_commands(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        phase=SessionPhase.CleanupRequired,
        display=DisplayState.CleanupRequired,
        cleanup_pending=True,
        intent_stage=IntentStage.cleanup,
        trust=TrustState.Trusted,
    )
    await prepared.settle()
    prepared.click(Action.retry_cleanup)
    await prepared.settle()
    (retry,) = prepared.commands_of(RetryCleanup)
    assert retry.receiver_id == RECEIVER_A

    prepared.manager.commands.clear()
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.VirtualDisplay,
        transport=TransportState.Idle,
        display=DisplayState.Owned,
        display_handle=virtual_handle(owner=5, output="v1"),
        ownership_uuid=OwnerId(UUID(int=5)),
        accepted_resolution=HD_READY,
        last_error=diagnostic(ErrorCode.resolution_mismatch, RECEIVER_A),
        trust=TrustState.Trusted,
        session_generation=4,
    )
    await prepared.settle()
    prepared.click(Action.recreate)
    await prepared.settle()
    (recreate,) = prepared.commands_of(RecreateAccepted)
    assert recreate.receiver_id == RECEIVER_A


async def test_change_resolution_dispatches_the_chosen_size(rig: PresenterRig) -> None:
    # The receiver remembered HD_READY from an earlier verified setup and is now
    # running HD; both are the only sizes the pane may offer.
    rig.receivers.receivers[RECEIVER_A] = saved_receiver(
        "a", remembered_resolution=decision("a", HD_READY)
    )
    rig.discover("a")
    prepared = await started(rig)
    virtual_snapshot(prepared, observed_resolution=HD)
    await prepared.settle()
    assert Action.change_resolution in prepared.offered()

    prepared.dialogs.resolution_answer = HD_READY
    prepared.click(Action.change_resolution)
    await prepared.settle()

    assert prepared.dialogs.resolution_calls == [(HD, HD_READY)]
    (command,) = prepared.commands_of(ChangeResolution)
    assert command.receiver_id == RECEIVER_A
    assert command.decision.size == HD_READY
    assert command.decision.evidence.endpoint_fingerprint == "endpoint-a-v1"


async def test_a_resolution_choice_for_a_replaced_endpoint_is_dropped(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    opened = virtual_snapshot(prepared)
    await prepared.settle()
    assert opened.session_generation == 3
    assert opened.target is not None and opened.target.endpoint_generation == 1
    gate = prepared.dialogs.gate("resolution")

    prepared.click(Action.change_resolution)
    await prepared.settle()
    # The same session re-announces itself at a replacement endpoint while the
    # picker is open: ``session_generation`` is deliberately unchanged, so only
    # the endpoint fingerprint/generation half of the fence can reject the answer.
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.VirtualDisplay,
        transport=TransportState.Streaming,
        display=DisplayState.Owned,
        display_handle=virtual_handle(owner=5, output="v1"),
        ownership_uuid=OwnerId(UUID(int=5)),
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
        observed_resolution=HD,
        session_generation=3,
        generation=2,
    )
    await prepared.settle()
    current = prepared.presenter._snapshot(RECEIVER_A)
    assert current.session_generation == opened.session_generation
    assert current.target == target("a", 2)
    assert current.target.endpoint_fingerprint != opened.target.endpoint_fingerprint
    gate.set_result(HD)
    await prepared.settle()

    assert prepared.commands_of(ChangeResolution) == []
    assert prepared.commands_of(ChooseResolution) == []


async def test_a_resolution_choice_for_a_bumped_endpoint_generation_is_dropped(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    opened = virtual_snapshot(prepared)
    await prepared.settle()
    assert opened.target is not None
    gate = prepared.dialogs.gate("resolution")

    prepared.click(Action.change_resolution)
    await prepared.settle()
    # The endpoint generation is a high-water mark: re-verifying saved evidence
    # advances it while the fingerprint stays the same. The session does not
    # advance, so only the endpoint-generation comparison can reject the answer.
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.VirtualDisplay,
        transport=TransportState.Streaming,
        display=DisplayState.Owned,
        display_handle=virtual_handle(owner=5, output="v1"),
        ownership_uuid=OwnerId(UUID(int=5)),
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
        observed_resolution=HD,
        session_generation=3,
        target=replace(opened.target, endpoint_generation=2),
    )
    await prepared.settle()
    current = prepared.presenter._snapshot(RECEIVER_A)
    assert current.session_generation == opened.session_generation
    assert current.target is not None
    assert current.target.endpoint_generation == 2
    assert current.target.endpoint_fingerprint == opened.target.endpoint_fingerprint
    gate.set_result(HD)
    await prepared.settle()

    assert prepared.commands_of(ChangeResolution) == []
    assert prepared.commands_of(ChooseResolution) == []


async def test_a_resolution_choice_for_a_superseded_session_is_dropped(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    virtual_snapshot(prepared)
    await prepared.settle()
    gate = prepared.dialogs.gate("resolution")

    prepared.click(Action.change_resolution)
    await prepared.settle()
    # A newer session generation arrived while the picker was open; the endpoint
    # itself is unchanged, so only the generation fence can reject the answer.
    prepared.snapshot(
        "a",
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.VirtualDisplay,
        transport=TransportState.Streaming,
        display=DisplayState.Owned,
        display_handle=virtual_handle(owner=5, output="v1"),
        ownership_uuid=OwnerId(UUID(int=5)),
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
        observed_resolution=HD,
        session_generation=4,
        generation=1,
    )
    await prepared.settle()
    current = prepared.presenter._snapshot(RECEIVER_A)
    assert current.session_generation == 4
    assert current.target == target("a", 1)
    gate.set_result(HD)
    await prepared.settle()

    assert prepared.commands_of(ChangeResolution) == []
    assert prepared.commands_of(ChooseResolution) == []


# -------------------------------------------------------------------- pairing


async def test_pairing_submits_to_the_captured_receiver_without_leaking(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    challenge_view = challenge(RECEIVER_A)
    pin = SensitiveText("8642")
    prepared.dialogs.pairing_answer = pin
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge_view,
        transport=TransportState.Pairing,
    )
    await prepared.settle()

    assert prepared.dialogs.pairing_calls == [challenge_view]
    (command,) = prepared.commands_of(SubmitPairing)
    assert command.receiver_id == RECEIVER_A
    assert command.challenge_id == challenge_view.challenge_id
    assert command.value == pin
    # The secret never reaches a representation, the activity strip or the pane.
    assert "8642" not in repr(prepared.presenter)
    assert "8642" not in repr(vars(prepared.presenter))
    assert "8642" not in prepared.activity_text()
    assert "8642" not in prepared.activity.export()
    assert "8642" not in prepared.error_text()
    assert "8642" not in repr(command)


async def test_a_pending_prompt_targets_its_captured_receiver(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    first = challenge(RECEIVER_A, challenge_id=11)
    gate = prepared.dialogs.gate("pairing")
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=first,
        transport=TransportState.Pairing,
    )
    await prepared.settle()
    assert prepared.dialogs.pairing_calls == [first]

    rig.discover("b")
    prepared.snapshot(
        "b",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge(RECEIVER_B, challenge_id=21),
        transport=TransportState.Pairing,
    )
    await prepared.settle()
    # A prompt only opens for the selected receiver.
    assert len(prepared.dialogs.pairing_calls) == 1

    prepared.select("b")
    prepared.dialogs.pairing_answer = None
    await prepared.settle()
    assert len(prepared.dialogs.pairing_calls) == 2

    gate.set_result(SensitiveText("1111"))
    await prepared.settle()

    (command,) = prepared.commands_of(SubmitPairing)
    assert command.receiver_id == RECEIVER_A
    assert command.challenge_id == first.challenge_id


async def test_a_prompt_for_a_superseded_challenge_drops_the_secret(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    first = challenge(RECEIVER_A, challenge_id=11)
    gate = prepared.dialogs.gate("pairing")
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=first,
        transport=TransportState.Pairing,
        session_generation=1,
    )
    await prepared.settle()

    prepared.dialogs.pairing_answer = None
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge(RECEIVER_A, challenge_id=12),
        transport=TransportState.Pairing,
        session_generation=1,
    )
    await prepared.settle()
    gate.set_result(SensitiveText("2222"))
    await prepared.settle()

    assert prepared.commands_of(SubmitPairing) == []
    assert "2222" not in repr(vars(prepared.presenter))


async def test_a_dismissed_pairing_prompt_leaves_the_challenge_live(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    challenge_view = challenge(RECEIVER_A)
    prepared.dialogs.pairing_answer = None
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge_view,
        transport=TransportState.Pairing,
    )
    await prepared.settle()
    assert prepared.dialogs.pairing_calls == [challenge_view]
    assert prepared.commands_of(SubmitPairing) == []
    assert Action.submit_pairing in prepared.offered()
    assert Action.cancel in prepared.offered()

    prepared.dialogs.pairing_answer = SensitiveText("4321")
    prepared.click(Action.submit_pairing)
    await prepared.settle()

    assert len(prepared.dialogs.pairing_calls) == 2
    (command,) = prepared.commands_of(SubmitPairing)
    assert command.challenge_id == challenge_view.challenge_id


async def test_pairing_retry_is_dispatched_explicitly(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge(RECEIVER_A),
        transport=TransportState.Pairing,
    )
    await prepared.settle()

    prepared.click(Action.retry)
    await prepared.settle()

    (retry,) = prepared.commands_of(Retry)
    assert retry.receiver_id == RECEIVER_A


# ------------------------------------------------------- stale/asynchronous


async def test_a_stale_source_list_cannot_overwrite_the_newer_one(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    prepared.snapshot("a")
    prepared.snapshot("b")
    await prepared.settle()
    prepared.select("a")
    await prepared.settle()

    older = Deferred()
    newer = Deferred()
    prepared.capture.queue_result("list_physical", older)
    prepared.capture.queue_result("list_physical", newer)
    prepared.select("b")
    await prepared.settle()
    prepared.select("a")
    await prepared.settle()

    newer.resolve(Ok(value=(physical("HDMI-A-1"),)))
    await prepared.settle()
    older.resolve(Ok(value=(physical("DP-1"),)))
    await prepared.settle()

    assert prepared.window._source.count() == 1
    assert prepared.window._source.itemText(0) == "HDMI-A-1"


async def test_a_stale_ownership_load_cannot_overwrite_the_newer_one(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()

    older = Deferred()
    newer = Deferred()
    prepared.ownerships.queue_result("all", older)
    prepared.ownerships.queue_result("all", newer)
    prepared.click(Action.scan)
    await prepared.settle()
    prepared.click(Action.scan)
    await prepared.settle()

    stale = ownership_record("a", owner=41)
    fresh = ownership_record("a", owner=42, connector="Virtual-v2", output_id="v2")
    newer.resolve(Ok(value=(fresh,)))
    await prepared.settle()
    older.resolve(Ok(value=(stale,)))
    await prepared.settle()

    assert stale.owner_uuid not in prepared.ownerships.records  # store untouched
    assert prepared.presenter._records.get(stale.owner_uuid) is None
    assert prepared.presenter._records.get(fresh.owner_uuid) == fresh


async def test_a_stale_snapshot_cannot_overwrite_newer_state(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    mirror_snapshot(prepared, session_generation=5)
    await prepared.settle()
    assert prepared.pane().phase_label == "Mirroring"

    prepared.snapshot("a", session_generation=2, phase=SessionPhase.Available)
    await prepared.settle()

    assert prepared.pane().phase_label == "Mirroring"
    assert Action.stop in prepared.offered()


async def test_a_stale_dispatch_refusal_is_not_rendered(rig: PresenterRig) -> None:
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a", session_generation=1)
    deferred = Deferred()
    prepared.manager.queue_result("dispatch", deferred)
    await prepared.settle()

    prepared.click(Action.start)
    await prepared.settle()
    assert len(prepared.manager.commands) == 1

    # A newer intent now owns the receiver.
    prepared.snapshot(
        "a",
        mode=Mode.MIRROR,
        intent_active=True,
        intent_stage=IntentStage.starting,
        phase=SessionPhase.Connecting,
        transport=TransportState.Connecting,
        trust=TrustState.Untrusted,
        session_generation=2,
    )
    await prepared.settle()
    deferred.resolve(
        CommandReceipt(
            operation_id=OperationId(UUID(int=44)),
            accepted=False,
            diagnostic=Diagnostic(
                code=ErrorCode.transport_failed,
                message="stale refusal sentinel",
                recovery=(RecoveryAction.retry,),
            ),
        )
    )
    await prepared.settle()

    assert prepared.error_text() == ""
    assert "sentinel" not in repr(prepared.pane())


# ---------------------------------------------------------------- lifecycle


async def test_start_does_not_wait_for_slow_stores(rig: PresenterRig) -> None:
    """``start`` returns while metadata reads are still outstanding."""
    store_gate = Deferred()
    ownership_gate = Deferred()
    rig.receivers.queue_result("all", store_gate)
    rig.ownerships.queue_result("all", ownership_gate)

    await rig.presenter.start()  # would never return if store I/O were awaited
    assert store_gate.resolved is False
    assert ownership_gate.resolved is False
    await rig.settle()
    assert Action.start not in rig.offered()  # nothing to command yet

    store_gate.resolve(Ok(value=()))
    ownership_gate.resolve(Ok(value=()))
    await rig.settle()


async def test_start_is_idempotent(rig: PresenterRig) -> None:
    await rig.presenter.start()
    await rig.presenter.start()
    await rig.settle()
    assert rig.discovery.journal.names().count("discovery.refresh") == 1


async def test_repeat_close_starts_one_shutdown_future(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.window.show()
    gate = prepared.hold_shutdown()

    assert prepared.window.close() is False  # deferred to the presenter
    await prepared.settle()
    assert prepared.shutdown_calls == 1
    assert prepared.window.isVisible()
    assert "closing" in prepared.window.windowTitle().lower()

    prepared.window.close()  # idempotent while the future is pending
    await prepared.settle()
    assert prepared.shutdown_calls == 1

    gate.set_result(None)
    await prepared.settle()

    assert prepared.window._close_allowed is True
    assert prepared.window.isVisible() is False
    assert prepared.shutdown_calls == 1


async def test_close_joins_the_single_shutdown_future(rig: PresenterRig) -> None:
    rig.discover("a")
    prepared = await started(rig)
    gate = prepared.hold_shutdown()

    first = asyncio.ensure_future(prepared.presenter.close())
    second = asyncio.ensure_future(prepared.presenter.close())
    await prepared.settle()
    assert prepared.shutdown_calls == 1

    gate.set_result(None)
    await asyncio.gather(first, second)
    assert prepared.shutdown_calls == 1
    assert prepared.window._close_allowed is True


async def test_a_failed_shutdown_keeps_the_window_and_exposes_a_retry(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()
    prepared.shutdown_result = Err(error=diagnostic(ErrorCode.cleanup_timeout))

    await prepared.presenter.close()
    await prepared.settle()

    assert prepared.shutdown_calls == 1
    assert prepared.window._close_allowed is False
    assert prepared.error_text() == diagnostic(
        ErrorCode.cleanup_timeout, RECEIVER_A
    ).message
    assert "closing" not in prepared.window.windowTitle().lower()

    prepared.shutdown_result = Ok(value=None)
    await prepared.presenter.close()

    assert prepared.shutdown_calls == 2
    assert prepared.window._close_allowed is True


# ---------------------------------------------------------------- Qt prompts


class CapturingFactory(QtDialogFactory):
    """The concrete factory, capturing each prompt it builds for the test."""

    def __init__(self, window: QWidget | None = None) -> None:
        super().__init__(window)
        self.created: list[Any] = []

    def _pairing_dialog(
        self,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action] | None = None,
        retained_output: str | None = None,
    ) -> Any:
        dialog = super()._pairing_dialog(
            challenge, actions=actions, retained_output=retained_output,
        )
        self.created.append(dialog)
        return dialog

    def _confirm_stop_dialog(self, mode: Mode) -> Any:
        dialog = super()._confirm_stop_dialog(mode)
        self.created.append(dialog)
        return dialog

    def _resolution_dialog(self, choices: tuple[Size, ...]) -> Any:
        dialog = super()._resolution_dialog(choices)
        self.created.append(dialog)
        return dialog

    def _orphan_dialog(self, item: RecoveryItem) -> Any:
        dialog = super()._orphan_dialog(item)
        self.created.append(dialog)
        return dialog


async def pump(rounds: int = 20) -> None:
    for _ in range(rounds):
        QApplication.processEvents()
        await asyncio.sleep(0)
    QApplication.processEvents()


async def test_qt_pairing_prompt_is_nonmodal_and_returns_the_secret(
    rig: PresenterRig,
) -> None:
    from airplay_linux.ui.dialogs import PairingDialog

    factory = CapturingFactory(rig.window)
    task = asyncio.ensure_future(factory.pairing(challenge(RECEIVER_A)))
    await pump()
    (dialog,) = factory.created

    assert isinstance(dialog, PairingDialog)
    # The carried-forward contract: the prompt is never re-parented into the
    # dashboard, so the dashboard keeps receiving input while a PIN is typed.
    assert dialog.parent() is None
    assert dialog.windowModality() == Qt.WindowModality.WindowModal
    assert rig.window.isEnabled() is True
    assert rig.window._tracked_prompt is dialog
    assert "8642" not in dialog.windowTitle()

    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("8642")
    dialog.findChild(QPushButton, "pairingSubmitButton").click()
    assert field.text() == ""
    assert field.isUndoAvailable() is False

    answer = await task
    assert isinstance(answer, PairingAnswer)
    assert answer.action is Action.submit_pairing
    assert isinstance(answer.value, SensitiveText)
    assert answer.value.reveal_for_backend() == "8642"
    assert "8642" not in repr(answer)


async def test_qt_pairing_prompt_dismissal_answers_none_and_wipes(
    rig: PresenterRig,
) -> None:
    factory = CapturingFactory(rig.window)
    task = asyncio.ensure_future(factory.pairing(challenge(RECEIVER_A)))
    await pump()
    (dialog,) = factory.created
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("1357")

    dialog.findChild(QPushButton, "pairingCancelButton").click()

    answer = await task
    assert isinstance(answer, PairingAnswer)
    assert answer.action is Action.cancel
    assert answer.value is None
    assert field.text() == ""
    assert dialog.take_input() is None


async def test_qt_pairing_prompt_ends_with_the_dashboard(rig: PresenterRig) -> None:
    rig.window.show()
    factory = CapturingFactory(rig.window)
    task = asyncio.ensure_future(factory.pairing(challenge(RECEIVER_A)))
    await pump()
    (dialog,) = factory.created

    rig.window.close()  # deferred close, but the tracked prompt is dismissed now
    assert await task is None
    assert dialog.isVisible() is False


async def test_qt_confirm_stop_answers_both_ways(rig: PresenterRig) -> None:
    factory = CapturingFactory(rig.window)
    accepted = asyncio.ensure_future(factory.confirm_stop(Mode.VIRTUAL))
    await pump()
    (dialog,) = factory.created
    dialog.findChild(QPushButton, "confirmStopButton").click()
    assert await accepted is True

    factory.created.clear()
    kept = asyncio.ensure_future(factory.confirm_stop(Mode.MIRROR))
    await pump()
    (dialog,) = factory.created
    dialog.findChild(QPushButton, "keepSessionButton").click()
    assert await kept is False


async def test_qt_resolution_prompt_commits_only_the_confirmed_choice(
    rig: PresenterRig,
) -> None:
    from PySide6.QtWidgets import QComboBox

    factory = CapturingFactory(rig.window)
    task = asyncio.ensure_future(factory.resolution((HD, HD_READY)))
    await pump()
    (dialog,) = factory.created
    combo = dialog.findChild(QComboBox, "resolutionChoice")
    combo.setCurrentIndex(1)
    dialog.findChild(QPushButton, "confirmResolutionButton").click()
    assert await task == HD_READY

    factory.created.clear()
    cancelled = asyncio.ensure_future(factory.resolution((HD,)))
    await pump()
    (dialog,) = factory.created
    dialog.findChild(QPushButton, "cancelResolutionButton").click()
    assert await cancelled is None


async def test_qt_orphan_prompt_defaults_to_keep(rig: PresenterRig) -> None:
    item = RecoveryItem(
        kind="orphan", record=ownership_record("a", owner=77), handle=None,
        diagnostic=None,
    )
    factory = CapturingFactory(rig.window)
    task = asyncio.ensure_future(factory.orphan(item))
    await pump()
    (dialog,) = factory.created
    dialog.findChild(QPushButton, "keepOrphanButton").click()
    assert await task is False

    factory.created.clear()
    confirmed = asyncio.ensure_future(factory.orphan(item))
    await pump()
    (dialog,) = factory.created
    dialog.findChild(QPushButton, "confirmOrphanButton").click()
    assert await confirmed is True


async def test_a_rejected_metadata_removal_keeps_the_record_and_a_catalog_message(
    rig: PresenterRig,
) -> None:
    rig.receivers.receivers[RECEIVER_B] = saved_receiver("b")
    rig.discover("a")
    prepared = await started(rig)
    prepared.snapshot("a")
    await prepared.settle()
    prepared.select("b")
    await prepared.settle()
    prepared.receivers.queue_result(
        "remove_metadata",
        Err(
            error=Diagnostic(
                code=ErrorCode.persistence_failed,
                message="raw-store-sentinel-5512",
                recovery=(RecoveryAction.retry,),
            )
        ),
    )

    prepared.click(Action.remove_metadata)
    await prepared.settle()

    assert prepared.error_text() == diagnostic(
        ErrorCode.persistence_failed, RECEIVER_B
    ).message
    assert "sentinel" not in prepared.error_text()
    # The record is still saved metadata: nothing was dropped optimistically.
    assert prepared.receivers.receivers[RECEIVER_B].display_name == "TV B"
    assert RECEIVER_B in prepared.row_ids()


async def test_overlapping_prompts_each_apply_to_their_own_receiver(
    rig: PresenterRig,
) -> None:
    rig.discover("a")
    rig.discover("b")
    prepared = await started(rig)
    mirror_snapshot(prepared, "a")
    mirror_snapshot(prepared, "b")
    await prepared.settle()

    prepared.select("a")
    await prepared.settle()
    stop_gate = prepared.dialogs.gate("stop")
    prepared.request_mode(Mode.VIRTUAL)
    await prepared.settle()
    assert prepared.dialogs.stop_calls == [Mode.VIRTUAL]

    prepared.select("b")
    prepared.dialogs.pairing_answer = SensitiveText("5150")
    prepared.snapshot(
        "b",
        mode=Mode.MIRROR,
        intent_active=True,
        phase=SessionPhase.Pairing,
        transport=TransportState.Pairing,
        audio=AudioState.Enabled,
        trust=TrustState.Pairing,
        challenge=challenge(RECEIVER_B, challenge_id=31),
        stream=stream_handle(RECEIVER_B),
        session_generation=3,
    )
    await prepared.settle()
    (submit,) = prepared.commands_of(SubmitPairing)
    assert submit.receiver_id == RECEIVER_B

    stop_gate.set_result(True)
    await prepared.settle()
    (confirm,) = prepared.commands_of(ConfirmSwitch)
    assert confirm.receiver_id == RECEIVER_A
    assert confirm.request.mode is Mode.VIRTUAL

    # A later B snapshot never rewrites A's pane state.
    prepared.snapshot(
        "b",
        mode=Mode.MIRROR,
        intent_active=True,
        phase=SessionPhase.Reconnecting,
        transport=TransportState.Reconnecting,
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
        stream=stream_handle(RECEIVER_B),
        session_generation=3,
    )
    await prepared.settle()
    assert prepared.pane().phase_label == "Reconnecting"
    prepared.select("a")
    await prepared.settle()
    assert prepared.pane().phase_label == "Mirroring"


def test_the_presenter_consumes_ports_not_the_session_implementation() -> None:
    """A structural guarantee: the UI layer cannot reach the concrete session."""
    import inspect

    import airplay_linux.ui.presenter as module

    source = inspect.getsource(module)
    assert "airplay_linux.session" not in source
    assert "SessionManager" not in source


@pytest.fixture
async def screen_rig(qtbot: Any) -> Any:
    """A started rig with a scripted ScreenCast permission store."""
    prepared = PresenterRig(FakeScreenCast())
    qtbot.addWidget(prepared.window)
    try:
        yield prepared
    finally:
        await prepared.settle(10)
        await prepared.presenter.close()
        await prepared.settle(10)


async def test_mirror_start_clears_the_remembered_screen_first(screen_rig: Any) -> None:
    """Start asks the desktop every time; nothing remembered may be replayed."""
    screen_rig.capture.add_physical(physical("DP-1"))
    screen_rig.discover("a")
    prepared = await started(screen_rig)
    prepared.snapshot("a")
    await prepared.settle()

    prepared.click(Action.start)
    await prepared.settle()

    assert prepared.screencast.calls == 1
    # The clear ran before anything reached the manager.
    assert prepared.screencast.command_counts == [0]
    assert [type(command) for command in prepared.manager.commands] == [Start]
    events = [event for event in prepared.activity.entries() if event.code.startswith("screen.")]
    assert events[-1].code == "screen.forget"
    assert events[-1].fields["remembered"] == 2


async def test_mirror_start_refuses_when_the_screen_memory_cannot_be_cleared() -> None:
    """A start that cannot clear must not run with a remembered approval."""
    store = FakeScreenCast(Err(error=diagnostic(ErrorCode.dependency_missing)))
    prepared = PresenterRig(store)
    prepared.capture.add_physical(physical("DP-1"))
    prepared.discover("a")
    try:
        await prepared.start()
        prepared.snapshot("a")
        await prepared.settle()

        prepared.click(Action.start)
        await prepared.settle()

        assert store.calls == 1
        assert prepared.manager.commands == []
        events = [event for event in prepared.activity.entries() if event.code.startswith("screen.")]
        assert events[-1].code == "screen.forget_failed"
        assert events[-1].fields["error_code"] == ErrorCode.dependency_missing
    finally:
        await prepared.presenter.close()
        await prepared.settle(10)


async def test_retry_does_not_clear_the_remembered_screen(screen_rig: Any) -> None:
    """Retry repeats a decision already made; it must not re-prompt for a screen."""
    screen_rig.capture.add_physical(physical("DP-1"))
    screen_rig.discover("a")
    prepared = await started(screen_rig)
    prepared.snapshot(
        "a",
        phase=SessionPhase.Failed,
        transport=TransportState.Failed,
        last_error=diagnostic(ErrorCode.transport_failed),
    )
    await prepared.settle()

    assert Action.retry in prepared.offered()
    prepared.click(Action.retry)
    await prepared.settle()

    assert prepared.screencast.calls == 0
