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
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from airplay_linux.diagnostics import ActivityLog, diagnostic, safe_activity
from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverRecordSet,
    ReceiverTarget,
    ReceiverView,
    ServiceRecord,
)
from airplay_linux.domain.messages import (
    Cancel,
    ChangeResolution,
    ChooseResolution,
    Command,
    CommandReceipt,
    ConfirmOrphanRemoval,
    ConfirmSwitch,
    Forget,
    KeepCleanup,
    Pair,
    RecreateAccepted,
    Retry,
    RetryAudio,
    RetryCleanup,
    RetryRevocation,
    SetMuted,
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
    OperationContext,
    OperationId,
    OwnerId,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import (
    ActivityEvent,
    LayoutSnapshot,
    OwnershipRecord,
    RecoveryItem,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioState,
    CapabilityReport,
    Diagnostic,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    PhysicalMonitorSource,
    RecoveryAction,
    ResolutionDecision,
    ResolutionEvidence,
    Result,
    SessionPhase,
    Size,
    TransportState,
    TrustState,
)
from airplay_linux.testing.adapters import (
    Deferred,
    FakeCapture,
    FakeDiscovery,
    FakeManager,
    MemoryOwnershipStore,
    MemoryReceiverStore,
)
from airplay_linux.testing.builders import physical, service, target

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
    ChangeResolution, RecreateAccepted, SetMuted, RetryAudio, StartVideoOnly,
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

    async def pairing(self, challenge: ChallengeView) -> SensitiveText | None:
        self.pairing_calls.append(challenge)
        gate, self.pairing_gate = self.pairing_gate, None
        if gate is not None:
            return await gate
        return self.pairing_answer

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


class PresenterRig:
    """A real dashboard wired to deterministic ports and preprogrammed dialogs."""

    def __init__(self) -> None:
        self.window = MainWindow()
        self.manager = FakeManager()
        self.discovery = FakeDiscovery()
        self.capture = FakeCapture()
        self.receivers = MemoryReceiverStore()
        self.ownerships = MemoryOwnershipStore()
        self.activity = ActivityLog()
        self.dialogs = FakeDialogs()
        self.capabilities = capability_report()
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
        )
        self._started = False

    async def _request_shutdown(self) -> Result[None]:
        self.shutdown_calls += 1
        gate, self.shutdown_gate = self.shutdown_gate, None
        if gate is not None:
            await gate
        return self.shutdown_result

    async def shutdown_gate(self) -> asyncio.Future[Any]:
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

    def request_mode(self, mode: Mode) -> None:
        self.window.mode_requested.emit(mode.value)

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
    assert start.request.source == source
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
    assert start_a.request.source.connector == "DP-1"

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
    ):
        prepared.click(action)
    await prepared.settle()

    assert prepared.manager.commands == []
    assert prepared.receivers.journal.calls("receiver_store.remove_metadata") == []


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
