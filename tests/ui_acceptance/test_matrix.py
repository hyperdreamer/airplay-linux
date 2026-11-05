"""The UI state matrix, rendered by the *assembled* runtime's real presenter.

Every test in this module builds the application exactly the way
:func:`airplay_linux.runtime.build_runtime` does for the operator: the production
handler composition (:func:`~airplay_linux.runtime.compose_handlers`), the real
:class:`~airplay_linux.session.SessionManager`, the real
:class:`~airplay_linux.ui.presenter.Presenter`, the real
:class:`~airplay_linux.ui.presenter.QtDialogFactory` and the real
:class:`~airplay_linux.ui.window.MainWindow`.  Only the hardware-facing ports are
the deterministic fakes the project already ships.

That means the assertions are made against the widgets a user actually sees and
the commands the production presenter actually dispatches - never against a
hand-built pane or a re-implemented presenter body.  For each row of the approved
state matrix the module asserts the visible phase copy, the independent resource
badges, which controls are offered or locked, and - for every command - *which
receiver and which retained resource* it targeted.

The states are reached the same way a user reaches them: typed commands and
backend events enter through the production session handlers, or a restored
durable snapshot is installed through the production recovery seam.  There is no
second authority and no short-circuit into presenter state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
)

from airplay_linux.diagnostics import diagnostic
from airplay_linux.diagnostics.activity import ActivityLog
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import (
    DisplayEvent,
    EmptyPayload,
    Forget,
    ModePayload,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    DisplayHandle,
    InstanceId,
    OperationId,
    OpaqueCredentialRef,
    OwnerId,
    ProcessIdentity,
    StreamHandle,
)
from airplay_linux.domain.records import (
    LayoutSnapshot,
    OwnershipRecord,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    CapabilityReport,
    DisplayState,
    Err,
    ErrorCode,
    Mode,
    Ok,
    ResolutionDecision,
    ResolutionEvidence,
    Result,
    SessionPhase,
    Size,
    TrustState,
    VirtualOutputSource,
)
from airplay_linux.runtime import RuntimeConfig, build_runtime
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred, FakeTransport, Journal
from airplay_linux.testing.builders import physical, service, target
from airplay_linux.ui.state import Action
from airplay_linux.ui.window import MainWindow

HD = Size(width=1920, height=1080)
SMALL = Size(width=1280, height=720)
ENVIRONMENT = "KDE Plasma 6 Wayland"
RECEIVER_A = "airplay-device:a"
RECEIVER_B = "airplay-device:b"

#: The object names the window contract publishes for its command buttons.  They
#: are restated here (rather than imported from a private map) so a rename cannot
#: silently make an acceptance assertion vacuous.
BUTTON_NAMES: dict[Action, str] = {
    Action.start: "startButton",
    Action.stop: "stopButton",
    Action.pair: "pairButton",
    Action.scan: "scanButton",
    Action.submit_pairing: "submitPairingButton",
    Action.cancel: "cancelButton",
    Action.remove_metadata: "removeMetadataButton",
    Action.retry: "retryButton",
    Action.choose_resolution: "chooseResolutionButton",
    Action.change_resolution: "changeResolutionButton",
    Action.recreate: "recreateButton",
    Action.retry_audio: "audioRetryButton",
    Action.video_only: "videoOnlyButton",
    Action.retry_cleanup: "retryCleanupButton",
    Action.keep_cleanup: "keepCleanupButton",
    Action.retry_revocation: "retryRevocationButton",
    Action.keep_blocked: "keepBlockedButton",
    Action.confirm_orphan: "confirmOrphanButton",
    Action.keep_orphan: "keepOrphanButton",
}

BADGE_RESOURCES = ("transport", "display", "audio", "trust", "receiver")

#: The delivered state matrix, in the order the approved design lists it.  The
#: inventory is separate from the parameter list so deleting a row is caught.
MATRIX_ROWS: tuple[str, ...] = (
    "available",
    "offline",
    "recovering-owned",
    "pairing-none",
    "pairing-owned",
    "resolution-required",
    "creating",
    "connecting",
    "mirroring",
    "virtual",
    "reconnecting",
    "reconfiguring",
    "resolution-mismatch",
    "audio-decision",
    "audio-unavailable",
    "dependency-failure",
    "orphan",
    "cleanup-required",
    "trust-cleanup-required",
    "failed",
)


# --------------------------------------------------------------------------- ports


class AcceptanceScreenCast:
    """The desktop permission store, replaced so tests never touch the real one."""

    def __init__(self) -> None:
        self.calls = 0

    async def forget_all(self) -> Result[int]:
        self.calls += 1
        return Ok(value=0)


class AcceptanceTransport(FakeTransport):
    """Echo the caller's lease and publish the backend's explicit ready event."""

    def __init__(self, *, ready: bool = True) -> None:
        super().__init__()
        self.ready = ready

    def _new_handle(self, destination, operation):
        handle = super()._new_handle(destination, operation)
        self.streams.pop(handle.lease_id)
        handle = replace(handle, lease_id=operation.transport_lease_id)
        self.streams[handle.lease_id] = handle
        return handle

    async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
        result = await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
        if isinstance(result, Ok) and self.ready:
            self.emit(TransportEvent(
                kind=TransportEventKind.ready,
                receiver_id=destination.receiver_id,
                stream=result.value,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                endpoint_generation=destination.endpoint_generation,
                daemon_instance_uuid=result.value.daemon_instance_uuid,
                context=operation,
                payload=EmptyPayload(),
            ))
        return result

    async def stop_audio(self, handle, operation):
        self._record("stop_audio", handle=handle, context=operation)
        self.audio_started.pop(handle.lease_id, None)
        return Ok(value=None)


# ------------------------------------------------------------------------ builders


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


def remembered(letter: str = "a", size: Size = HD) -> ResolutionDecision:
    """A remembered decision stamped with that receiver's own endpoint."""
    return ResolutionDecision(
        size=size,
        source="stored",
        evidence=ResolutionEvidence(
            origin="remembered",
            reliable=True,
            receiver_id=ReceiverId(f"airplay-device:{letter}"),
            endpoint_fingerprint=f"endpoint-{letter}-v1",
        ),
    )


def saved_receiver(
    letter: str = "a", *, resolution: ResolutionDecision | None = None,
) -> SavedReceiver:
    return SavedReceiver(
        receiver_id=ReceiverId(f"airplay-device:{letter}"),
        service_metadata=(service(address=f"192.0.2.{10 + ord(letter)}"),),
        display_name=f"TV {letter.upper()}",
        last_endpoint=target(letter),
        continuity_fingerprint=f"continuity-{letter}",
        credential_ref=OpaqueCredentialRef(f"credential-{letter}"),
        remembered_resolution=resolution,
    )


def ownership_record(
    letter: str = "a",
    *,
    owner: int = 7,
    stage: str = "output_observed",
    desired_state: str = "active",
    connector: str = "Virtual-v1",
    output_id: str = "v1",
    size: Size = HD,
    helper: ProcessIdentity | None = None,
) -> OwnershipRecord:
    observed = size if stage in ("output_observed", "published") else None
    return OwnershipRecord(
        schema_version=1,
        owner_uuid=OwnerId(UUID(int=owner)),
        app_instance_uuid=InstanceId(UUID(int=1)),
        runtime_directory="/run/user/1000/airplay-linux/instances/1",
        creation_operation_id=OperationId(UUID(int=owner + 100)),
        receiver_id=ReceiverId(f"airplay-device:{letter}"),
        endpoint_fingerprint=f"endpoint-{letter}-v1",
        desired_state=desired_state,
        stage=stage,
        requested_size=size,
        observed_size=observed,
        helper=helper or ProcessIdentity(pid=4000 + owner, start_ticks=90000 + owner),
        connector=connector,
        output_id=output_id,
        created_utc="2026-10-17T00:00:00Z",
        last_configuration_step=stage,
        baseline=LayoutSnapshot(outputs={}, baseline_hash="baseline"),
        last_applied={},
        provider_version="fake-kscreen-1",
        capability_fingerprint="fake-capability",
    )


def handle_of(record: OwnershipRecord) -> DisplayHandle:
    return DisplayHandle(
        owner_uuid=record.owner_uuid,
        source=VirtualOutputSource(
            connector=record.connector or "Virtual-v1",
            kscreen_output_id=record.output_id or "v1",
            size=record.observed_size or record.requested_size,
        ),
        helper=record.helper or ProcessIdentity(pid=1, start_ticks=1),
    )


# -------------------------------------------------------------------------- driver


@dataclass
class UiApplication:
    """The assembled runtime, plus observability the acceptance assertions need."""

    runtime: Any
    rig: SessionRig
    activity: ActivityLog
    dispatched: list[Any]

    # -- identity -------------------------------------------------------

    @property
    def window(self) -> MainWindow:
        return self.runtime.window

    @property
    def presenter(self) -> Any:
        return self.runtime.presenter

    @property
    def manager(self) -> Any:
        return self.runtime.manager

    @property
    def receiver(self) -> ReceiverId:
        return self.rig.receiver_id

    @property
    def journal(self) -> Journal:
        return self.rig.journal

    # -- event loop -----------------------------------------------------

    async def settle(self, rounds: int = 90) -> None:
        """Deliver queued Qt work and every ready actor turn, with no sleeping."""
        for _ in range(rounds):
            QApplication.processEvents()
            QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            await asyncio.sleep(0)
            await self.manager.drain()
        QApplication.processEvents()

    async def start(self) -> UiApplication:
        started = await self.runtime.start()
        assert isinstance(started, Ok), started
        await self.settle()
        return self

    async def start_virtual(self) -> None:
        """Choose Virtual display in the real segmented control, then Start."""
        mode = self.widget("modeVirtual", QPushButton)
        assert mode.isEnabled(), "the mode selector is locked for this state"
        mode.click()
        await self.settle()
        self.click(Action.start)
        await self.settle()

    # -- reachability ---------------------------------------------------

    def discover(
        self, letter: str = "a", *, generation: int = 1, address: str | None = None,
    ) -> Any:
        found = target(letter, generation) if address is None else target(
            letter, generation, address=address,
        )
        self.rig.discovery.add_target(found)
        return found

    def leave(self, letter: str = "a") -> None:
        self.rig.discovery.remove_target(ReceiverId(f"airplay-device:{letter}"))

    async def dispatchem(self, command: Any) -> Any:
        receipt = await self.manager.dispatch(command)
        await self.settle()
        return receipt

    def state(self, letter: str = "a") -> SessionSnapshot:
        return self.manager.snapshot(ReceiverId(f"airplay-device:{letter}"))

    def transport_event(self, kind: Any, letter: str = "a", payload: Any = None) -> TransportEvent:
        state = self.state(letter)
        handle = state.stream
        assert isinstance(handle, StreamHandle), state
        operation = replace(
            self.manager.context(state.receiver_id).operation(),
            operation_id=handle.start_operation_id,
            transport_lease_id=handle.lease_id,
            endpoint_generation=handle.endpoint_generation,
            session_generation=state.session_generation,
            audio_operation_id=state.audio_operation_id,
        )
        return TransportEvent(
            kind=kind,
            receiver_id=state.receiver_id,
            stream=handle,
            endpoint_fingerprint=handle.endpoint_fingerprint,
            endpoint_generation=handle.endpoint_generation,
            daemon_instance_uuid=handle.daemon_instance_uuid,
            context=operation,
            payload=payload or EmptyPayload(),
        )

    def display_event(self, kind: str, letter: str = "a") -> DisplayEvent:
        state = self.state(letter)
        handle = state.display_handle
        assert handle is not None, state
        return DisplayEvent(
            kind=kind,
            receiver_id=state.receiver_id,
            creation_operation_id=None,
            owner_uuid=handle.owner_uuid,
            connector=handle.source.connector,
            output_id=handle.source.kscreen_output_id,
            helper=handle.helper,
            handle=handle,
            diagnostic=None,
        )

    # -- what the user sees ---------------------------------------------

    def widget(self, name: str, kind: type = QLabel) -> Any:
        found = self.window.findChild(kind, name)
        assert found is not None, f"{kind.__name__} {name!r} is not in the window"
        return found

    def visible_text(self, name: str) -> str:
        label = self.widget(name)
        return label.text() if label.isVisible() else ""

    @property
    def title(self) -> str:
        return self.visible_text("paneTitle")

    @property
    def phase(self) -> str:
        return self.visible_text("phaseLabel")

    @property
    def notice(self) -> str:
        return self.visible_text("noticeLabel")

    @property
    def error(self) -> str:
        return self.visible_text("errorLabel")

    @property
    def summary(self) -> str:
        return self.visible_text("summaryLabel")

    @property
    def environment(self) -> str:
        return self.visible_text("environmentLabel")

    @property
    def activity_text(self) -> str:
        return self.widget("activityView", QPlainTextEdit).toPlainText()

    def badge(self, resource: str) -> str:
        label = self.window.findChild(QLabel, f"badge_{resource}")
        if label is None or not label.isVisible():
            return ""
        return label.text()

    @property
    def badges(self) -> dict[str, str]:
        return {
            resource: self.badge(resource)
            for resource in BADGE_RESOURCES
            if self.badge(resource)
        }

    def button(self, action: Action) -> Any:
        if action is Action.mute:
            return self.widget("muteToggle", QToolButton)
        return self.widget(BUTTON_NAMES[action], QPushButton)

    def offered(self) -> set[Action]:
        """Actions whose control is visible and enabled right now."""
        candidates = [*BUTTON_NAMES, Action.mute]
        return {
            action
            for action in candidates
            if self.button(action).isVisible() and self.button(action).isEnabled()
        }

    def click(self, action: Action) -> None:
        button = self.button(action)
        assert button.isVisible(), f"{action.value} control is not visible"
        assert button.isEnabled(), f"{action.value} control is disabled"
        button.click()

    def mode_buttons(self) -> tuple[bool, bool, bool]:
        """(mirror checked, virtual checked, either enabled) for the pane."""
        mirror = self.widget("modeMirror", QPushButton)
        virtual = self.widget("modeVirtual", QPushButton)
        assert mirror.isEnabled() == virtual.isEnabled()
        assert mirror.isChecked() != virtual.isChecked(), "both modes look active"
        return mirror.isChecked(), virtual.isChecked(), mirror.isEnabled()

    def editor(self) -> str:
        """Which editor the pane renders: ``source`` or ``resolution``."""
        stack = self.window.findChild(type(self.window._editor_stack), "editorStack")
        current = stack.currentWidget()
        if current is self.window._source_page:
            return "source"
        if current is self.window._resolution_page:
            return "resolution"
        raise AssertionError("unknown editor page")

    def rows(self) -> tuple[Any, ...]:
        return self.window._model.rows

    def select(self, letter: str = "a") -> None:
        """Select a receiver the way a user does: through the table selection."""
        receiver = ReceiverId(f"airplay-device:{letter}")
        index = self.window._model.index_of(receiver)
        assert index.isValid(), f"receiver {letter!r} has no row: {self.rows()}"
        self.window._table.selectRow(index.row())

    def row(self, letter: str = "a") -> Any:
        receiver = ReceiverId(f"airplay-device:{letter}")
        for row in self.rows():
            if row.receiver_id == receiver:
                return row
        raise AssertionError(f"receiver {letter!r} has no row: {self.rows()}")

    # -- prompts --------------------------------------------------------

    async def prompt(self, name: str, *, timeout: float = 3.0) -> QDialog:
        """Wait for the production dialog factory to open ``name``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for widget in _visible_dialogs():
                if widget.objectName() == name:
                    return widget
            QApplication.processEvents()
            await asyncio.sleep(0)
        raise AssertionError(
            f"prompt {name!r} did not open; visible dialogs="
            f"{[widget.objectName() for widget in _visible_dialogs()]}"
        )

    def prompt_texts(self, dialog: QDialog) -> list[str]:
        return [label.text() for label in dialog.findChildren(QLabel)]

    def prompt_buttons(self, dialog: QDialog) -> dict[str, QPushButton]:
        return {
            button.objectName(): button
            for button in dialog.findChildren(QPushButton)
        }

    # -- observability --------------------------------------------------

    def commands(self, kind: type) -> list[Any]:
        return [command for command in self.dispatched if isinstance(command, kind)]

    def calls(self, name: str) -> list[dict[str, Any]]:
        return self.journal.calls(name)


def field_of(value: Any, name: str) -> Any:
    """Read one journal argument field from a redacted mapping or a record."""
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _visible_dialogs() -> list[QDialog]:
    return [
        widget
        for widget in QApplication.topLevelWidgets()
        if isinstance(widget, QDialog) and widget.isVisible()
    ]


def _wire_journal(rig: SessionRig) -> Journal:
    """Publish one ordered effect journal across every deterministic adapter."""
    journal = rig.journal
    for name in (
        "transport",
        "pairing",
        "capture",
        "audio",
        "discovery",
        "display",
        "sessions",
        "receivers",
        "ownerships",
    ):
        adapter = getattr(rig, name, None)
        if adapter is not None and hasattr(adapter, "journal"):
            adapter.journal = journal
    if hasattr(rig.display, "process"):
        rig.display.process.journal = journal
    return journal


@pytest.fixture
async def build(qtbot: Any, qapp: QApplication, tmp_path: Path):
    """Compose the real application for one test; only hardware ports are fakes."""
    applications: list[UiApplication] = []
    counter = {"n": 0}

    async def make(
        *,
        transport: FakeTransport | None = None,
        audio_result: Result[None] | None = None,
        capabilities: CapabilityReport | None = None,
        saved: tuple[SavedReceiver, ...] = (),
        records: tuple[OwnershipRecord, ...] = (),
        sources: tuple[Any, ...] = (),
        handed: tuple[DisplayHandle, ...] = (),
        checked_in: tuple[SessionSnapshot, ...] = (),
        discovered: tuple[str, ...] = (),
        start: bool = True,
    ) -> UiApplication:
        rig = SessionRig()
        # SessionRig's own manager never becomes a second live authority: the
        # composition below builds the one manager every command goes through.
        for subscription in rig.manager._subscriptions:
            subscription.close()
        rig.transport = transport or AcceptanceTransport()
        # A test that needs a source with tokens supplies the exact source; the
        # default is the plain physical monitor.
        for source in sources or (physical(),):
            rig.capture.add_physical(source)
        if audio_result is not None:
            rig.audio.queue_result("check", audio_result)
        # Receivers that are already advertising when the application starts are
        # visible to startup recovery, exactly as they are in the field.
        for letter in discovered:
            rig.discovery.add_target(target(letter))
        for preference in saved:
            rig.receivers.receivers[preference.receiver_id] = preference
        for state in checked_in:
            rig.sessions.saved[state.receiver_id] = state
        for record in records:
            rig.ownerships.records[record.owner_uuid] = record
        for handle in handed:
            rig.display.handles[handle.owner_uuid] = handle
            # A recovered helper is a live, exactly matching process.
            rig.display.process.register(
                handle.helper,
                rig.display.process.owner_environment(
                    handle.owner_uuid, "/run/user/1000/airplay-linux/instances/1",
                ),
            )
        report = capabilities or capability_report()
        provider = (lambda: report) if capabilities is not None else rig.deps.capabilities
        activity = ActivityLog(max_entries=500, capabilities=report)
        _wire_journal(rig)
        rig.deps = replace(
            rig.deps,
            transport=rig.transport,
            activity=activity,
            capabilities=provider,
        )
        index = counter["n"]
        counter["n"] += 1
        built = await build_runtime(
            RuntimeConfig(
                state_root=tmp_path / f"app{index}" / "state",
                runtime_root=tmp_path / f"app{index}" / "runtime",
                live=True,
            ),
            {"deps": rig.deps, "screencast": AcceptanceScreenCast()},
        )
        assert isinstance(built, Ok), built
        runtime = built.value

        # Observe (never replace) the one dispatch entry point commands use.
        dispatched: list[Any] = []
        original = runtime.manager.dispatch

        async def recording(command: Any) -> Any:
            dispatched.append(command)
            return await original(command)

        runtime.manager.dispatch = recording
        app = UiApplication(runtime, rig, activity, dispatched)
        applications.append(app)
        qtbot.addWidget(app.window)
        if start:
            await app.start()
        return app

    try:
        yield make
    finally:
        for app in reversed(applications):
            for widget in _visible_dialogs():
                widget.reject()
            QApplication.processEvents()
            try:
                await asyncio.wait_for(app.presenter.close(), 6)
            except (TimeoutError, RuntimeError):
                pass
            try:
                await app.settle(10)
            except RuntimeError:
                continue
            try:
                visible = app.window.isVisible()
            except RuntimeError:
                visible = False
            if visible:
                app.window.allow_close()
                app.window.close()
            QApplication.processEvents()


# --------------------------------------------------------------------- the matrix


@pytest.fixture
async def audio_decision_window(build: Callable[..., Any]) -> MainWindow:
    """Render the startup AudioDecisionRequired snapshot through the real presenter.

    The application is composed exactly as it is for the operator
    (``SessionRig(compose_handlers)`` -> ``build_runtime`` -> production
    ``Presenter``/``QtDialogFactory``), then one audio-enabled Start is issued on
    the real control and the resulting ``AudioDecisionRequired`` pane is what the
    fixture hands back.  No external fixture is assumed and no widget is built by
    hand.
    """
    app = await build(audio_result=Err(error=diagnostic(ErrorCode.audio_denied)))
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.state().phase is SessionPhase.AudioDecisionRequired
    assert app.phase == "Audio decision required"
    return app.window


async def test_matrix_inventory_maps_to_collected_parameter_ids(request: Any) -> None:
    collected = [
        item.callspec.params["row"]
        for item in request.session.items
        if item.originalname == "test_matrix_row" and item.path == Path(__file__)
    ]
    assert len(collected) == len(set(collected))
    assert set(collected) == set(MATRIX_ROWS)


@pytest.mark.parametrize("row", MATRIX_ROWS, ids=MATRIX_ROWS)
async def test_matrix_row(row: str, build: Callable[..., Any]) -> None:
    """Each row is reached through the production path and read off the widgets."""
    if row == "available":
        app = await build()
        app.discover("a")
        await app.settle()
        assert app.phase == "Available"
        assert app.badges == {
            "transport": "Idle",
            "display": "No output",
            "audio": "Audio off",
            "trust": "Trust unknown",
        }
        assert app.title == "Living Room 192.0.2.10"
        assert app.notice == ""
        assert app.error == ""
        assert app.offered() == {Action.scan, Action.start, Action.pair}
        assert app.button(Action.start).text() == "Start"
        assert app.mode_buttons() == (True, False, True)
        assert app.editor() == "source"
        assert app.row("a").phase_label == "Available"
        assert app.row("a").mode_label == ""
        app.click(Action.start)
        await app.settle()
        assert [type(command).__name__ for command in app.dispatched] == ["Start"]
        start = app.dispatched[0]
        assert start.receiver_id == ReceiverId(RECEIVER_A)
        assert start.request.mode is Mode.MIRROR
        # Mirror asks the desktop; it never names a source itself.
        assert start.request.source is None
        assert start.request.audio == AudioPolicy(enabled=True)
        targets = [
            field_of(call["target"], "receiver_id")
            for call in app.calls("transport.start_stream")
        ]
        assert targets == [ReceiverId(RECEIVER_A)]
        return

    if row == "offline":
        app = await build(saved=(saved_receiver("a"),))
        app.leave("a")  # never discovered: only the saved identity is known
        await app.settle()
        assert app.phase == "Offline"
        assert app.badges == {
            "transport": "Idle",
            "display": "No output",
            "audio": "Audio off",
            "trust": "Trust unknown",
            "receiver": "Offline",
        }
        assert app.title == "TV A"
        assert "offline" in app.notice.lower()
        assert app.offered() == {Action.scan, Action.remove_metadata}
        assert app.button(Action.start).isEnabled() is False
        assert app.row("a").online is False
        assert app.row("a").phase_label == "Offline"
        app.click(Action.remove_metadata)
        await app.settle()
        removed = [call["receiver_id"] for call in app.calls("receiver_store.remove_metadata")]
        assert removed == [ReceiverId(RECEIVER_A)]
        assert ReceiverId(RECEIVER_A) not in app.rig.receivers.receivers
        assert app.dispatched == []
        return

    if row == "recovering-owned":
        record = ownership_record("a", owner=11, stage="published")
        handle = handle_of(record)
        checked_in = SessionSnapshot(
            receiver_id=ReceiverId(RECEIVER_A),
            online=False,
            mode=Mode.VIRTUAL,
            intent_active=True,
            session_generation=3,
        )
        app = await build(
            discovered=("a",),
            saved=(saved_receiver("a", resolution=remembered(size=HD)),),
            records=(record,),
            handed=(handle,),
            checked_in=(checked_in,),
        )
        await app.settle()
        assert app.phase == "Recovering"
        assert app.badges["display"] == "Output owned"
        assert app.badges["transport"] == "Idle"
        assert "retained" in app.notice.lower()
        assert app.offered() == {Action.retry, Action.stop}
        assert app.mode_buttons() == (False, True, False)
        assert app.editor() == "resolution"
        assert app.window._resolution.currentText() == "1920x1080"
        assert app.row("a").mode_label == "Virtual display"
        assert app.row("a").size_label == "1920x1080"
        # Retrying restores the stream onto the *same* output; it never recreates.
        app.click(Action.retry)
        await app.settle()
        assert app.phase == "Virtual display"
        assert app.state().display_handle == handle
        assert app.rig.display.created == []
        assert app.rig.display.removed == []
        assert app.rig.ownerships.records[record.owner_uuid] == record
        assert [
            field_of(call["target"], "receiver_id")
            for call in app.calls("transport.start_stream")
        ] == [ReceiverId(RECEIVER_A)]
        return

    if row == "pairing-none":
        app = await build()
        app.discover("a")
        await app.settle()
        app.click(Action.pair)
        await app.settle()
        prompt = await app.prompt("pairingDialog")
        assert app.phase == "Pairing"
        assert app.badges["trust"] == "Pairing"
        assert app.badges["display"] == "No output"
        assert "PIN or password" in app.notice
        assert app.offered() == {
            Action.submit_pairing, Action.cancel, Action.retry,
        }
        assert app.mode_buttons()[2] is False
        buttons = app.prompt_buttons(prompt)
        assert buttons["pairingSubmitButton"].isEnabled() is False
        assert "pairingRetryButton" in buttons
        assert any("Pairing with" in text for text in app.prompt_texts(prompt))
        assert [type(command).__name__ for command in app.dispatched] == ["Pair"]
        assert app.dispatched[0].receiver_id == ReceiverId(RECEIVER_A)
        pin = "8642"
        field = prompt.findChild(QLineEdit, "secretField")
        field.insert(pin)
        buttons["pairingSubmitButton"].click()
        await app.settle()
        assert app.phase == "Available"
        assert app.badges["trust"] == "Paired"
        assert Action.pair not in app.offered()
        assert Action.start in app.offered()
        submit, = app.commands(type(app.dispatched[1]))
        assert submit.receiver_id == ReceiverId(RECEIVER_A)
        assert submit.value.reveal_for_backend() == pin
        assert pin not in app.activity_text
        assert pin not in " ".join([app.title, app.phase, app.notice, app.error])
        return

    if row == "pairing-owned":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        assert app.phase == "Virtual display"
        owned = app.state().display_handle
        assert owned is not None
        app.rig.transport.emit(app.transport_event(TransportEventKind.pairing_required))
        await app.settle()
        prompt = await app.prompt("pairingDialog")
        assert app.phase == "Pairing"
        assert app.badges["display"] == "Output owned"
        assert "output stays owned during pairing" in app.notice
        assert app.state().display_handle == owned
        assert Action.retry not in app.offered()
        assert {Action.submit_pairing, Action.cancel} <= app.offered()
        buttons = app.prompt_buttons(prompt)
        assert "pairingRetryButton" not in buttons
        assert any("Virtual-v1" in text for text in app.prompt_texts(prompt))
        # Cancelling the attempt removes exactly the retained output it named.
        buttons["pairingCancelButton"].click()
        await app.settle()
        assert app.phase == "Available"
        assert app.state().display_handle is None
        assert app.rig.display.removed == [owned.owner_uuid]
        assert app.rig.transport.streams == {}
        assert app.rig.ownerships.records == {}
        cancel, = app.commands(type(app.dispatched[-1]))
        assert cancel.receiver_id == ReceiverId(RECEIVER_A)
        return

    if row == "resolution-required":
        app = await build()
        app.discover("a")
        await app.settle()
        app.widget("modeVirtual", QPushButton).click()
        await app.settle()
        app.click(Action.start)
        await app.settle()
        assert app.phase == "Resolution required"
        assert app.badges["display"] == "Resolving output"
        assert "Choose the output size" in app.notice
        assert app.offered() == {Action.choose_resolution, Action.cancel}
        assert app.editor() == "resolution"
        assert app.button(Action.stop).isEnabled() is False
        assert app.row("a").phase_label == "Resolution required"
        assert app.rig.display.created == []
        app.click(Action.cancel)
        await app.settle()
        assert app.phase == "Available"
        assert app.state().display is DisplayState.None_
        assert app.rig.display.created == [] and app.rig.transport.streams == {}
        return

    if row == "creating":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        gate = Deferred()
        store = app.rig.ownerships
        write = store.write

        async def held(record):
            if record.stage == "published":
                await gate.wait()
            return await write(record)

        store.write = held
        try:
            await app.start_virtual()
            assert app.phase == "Creating display"
            assert app.badges["display"] == "Creating output"
            assert app.badges["transport"] == "Idle"
            assert app.notice.startswith("Reservation stage:")
            assert "not usable until publication is confirmed" in app.notice
            assert app.offered() == {Action.cancel}
            assert app.mode_buttons()[2] is False
            assert app.state().display_handle is None
            assert app.rig.transport.streams == {}
            assert app.row("a").phase_label == "Creating display"
        finally:
            gate.resolve(None)
            await app.settle()
        assert app.phase == "Virtual display"
        assert app.state().display is DisplayState.Owned
        assert app.state().display_handle is not None
        assert app.rig.display.created == [app.state().display_handle]
        return

    if row == "connecting":
        app = await build(
            transport=AcceptanceTransport(ready=False),
            saved=(saved_receiver("a", resolution=remembered()),),
        )
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        assert app.phase == "Connecting"
        assert app.badges["transport"] == "Connecting"
        assert app.badges["display"] == "Output owned"
        assert app.badges["audio"] == "Audio starting"
        assert app.offered() == {Action.stop}
        assert app.mode_buttons()[2] is False
        handle = app.state().stream
        assert handle is not None
        app.rig.transport.emit(app.transport_event(TransportEventKind.ready))
        await app.settle()
        assert app.phase == "Virtual display"
        assert app.state().stream == handle
        assert app.badges["audio"] == "Audio on"
        return

    if row == "mirroring":
        app = await build()
        app.discover("a")
        await app.settle()
        app.click(Action.start)
        await app.settle()
        assert app.phase == "Mirroring"
        assert app.badges == {
            "transport": "Streaming",
            "display": "No output",
            "audio": "Audio on",
            "trust": "Trust unknown",
        }
        assert app.offered() == {Action.stop, Action.mute}
        assert app.editor() == "source"
        assert app.window._source.currentData() == "DP-1"
        mute = app.button(Action.mute)
        mute.click()
        await app.settle()
        assert app.rig.transport.muted == {app.state().stream.lease_id: True}
        assert app.badges["audio"] == "Muted"
        app.click(Action.stop)
        await app.settle()
        assert app.phase == "Available"
        assert app.rig.transport.streams == {}
        return

    if row == "virtual":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        assert app.phase == "Virtual display"
        assert app.badges["display"] == "Output owned"
        assert app.offered() == {
            Action.stop, Action.change_resolution, Action.mute,
        }
        assert app.editor() == "resolution"
        assert app.window._resolution.currentData() == HD
        handle = app.state().display_handle
        assert handle is not None and handle.source.size == HD
        assert app.row("a").mode_label == "Virtual display"
        assert app.row("a").size_label == "1920x1080"
        # The picker offers exactly the sizes this receiver's own evidence names;
        # cancelling it commits nothing and keeps the owned output untouched.
        app.click(Action.change_resolution)
        await app.settle()
        dialog = await app.prompt("resolutionDialog")
        buttons = app.prompt_buttons(dialog)
        combo = dialog.findChild(QComboBox, "resolutionChoice")
        choices = tuple(combo.itemData(index) for index in range(combo.count()))
        assert choices == (HD,)
        assert buttons["confirmResolutionButton"].isEnabled()
        buttons["cancelResolutionButton"].click()
        await app.settle()
        assert app.phase == "Virtual display"
        assert app.state().display_handle == handle
        assert app.rig.display.removed == []
        return

    if row == "reconnecting":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        retained = app.state().display_handle
        app.rig.transport.emit(app.transport_event(TransportEventKind.network_lost))
        await app.settle()
        assert app.phase == "Reconnecting"
        assert app.badges["transport"] == "Reconnecting"
        assert app.state().display_handle == retained
        assert "owned resources are retained" in app.notice
        assert app.offered() == {Action.retry, Action.stop}
        app.click(Action.retry)
        await app.settle()
        assert app.phase == "Virtual display"
        assert app.state().display_handle == retained
        assert len(app.rig.display.created) == 1
        return

    if row == "reconfiguring":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        before = app.state().display_handle
        gate = Deferred()
        app.rig.transport.queue_result("stop_stream", gate)
        try:
            app.click(Action.change_resolution)
            await app.settle()
            dialog = await app.prompt("resolutionDialog")
            app.prompt_buttons(dialog)["confirmResolutionButton"].click()
            await app.settle()
            assert app.phase == "Reconfiguring"
            assert "desktop layout may change temporarily" in app.notice.lower()
            assert app.offered() == {Action.cancel}
            assert app.state().display_handle == before
            assert app.rig.display.removed == []
            app.click(Action.cancel)
            await app.settle()
        finally:
            gate.resolve(Ok(value=None))
            await app.settle()
        assert app.phase == "Available"
        assert app.rig.display.removed == [before.owner_uuid]
        assert app.rig.transport.streams == {}
        assert app.rig.ownerships.records == {}
        return

    if row == "resolution-mismatch":
        app = await build(saved=(saved_receiver("a", resolution=remembered()),))
        app.discover("a")
        await app.settle()
        await app.start_virtual()
        retained = app.state().display_handle
        app.rig.transport.emit(app.transport_event(
            TransportEventKind.accepted_mode,
            payload=ModePayload(size=SMALL, reliable=True),
        ))
        await app.settle()
        # The session lane derives an idle phase for the stopped stream; the
        # mismatch decision owns the pane, and the owned display is still badged,
        # so the row can never be read as an idle "start something" state.
        assert app.phase == "Available"
        assert Action.start not in app.offered()
        assert app.state().accepted_resolution == SMALL
        assert app.badges["display"] == "Output owned"
        assert "accepted 1280x720" in app.notice
        assert app.offered() == {Action.recreate, Action.stop}
        assert app.error == diagnostic(
            ErrorCode.resolution_mismatch, ReceiverId(RECEIVER_A)
        ).message
        app.click(Action.recreate)
        await app.settle()
        assert app.state().display is DisplayState.Owned
        assert app.state().accepted_resolution is None
        assert app.state().display_handle.source.size == SMALL
        assert app.state().display_handle.owner_uuid != retained.owner_uuid
        assert len(app.rig.display.outputs) == 1
        return

    if row == "audio-decision":
        app = await build(audio_result=Err(error=diagnostic(ErrorCode.audio_denied)))
        app.discover("a")
        await app.settle()
        app.click(Action.start)
        await app.settle()
        assert app.phase == "Audio decision required"
        assert app.badges["audio"] == "Audio unavailable"
        assert app.badges["display"] == "No output"
        # The start is paused before a stream exists; the transport badge names
        # the paused connect intent, never a stream this row does not have.
        assert app.badges["transport"] == "Connecting"
        assert app.offered() == {
            Action.retry_audio, Action.video_only, Action.cancel,
        }
        assert app.error == diagnostic(
            ErrorCode.audio_denied, ReceiverId(RECEIVER_A)
        ).message
        assert app.rig.transport.streams == {}
        app.click(Action.video_only)
        await app.settle()
        assert app.phase == "Mirroring"
        assert app.badges["audio"] == "Audio off"
        assert app.state().audio_policy == AudioPolicy(enabled=False)
        assert len(app.rig.transport.streams) == 1
        return

    if row == "audio-unavailable":
        app = await build()
        app.discover("a")
        await app.settle()
        app.click(Action.start)
        await app.settle()
        assert app.phase == "Mirroring"
        handle = app.state().stream
        app.rig.transport.emit(app.transport_event(TransportEventKind.audio_failed))
        await app.settle()
        assert app.phase == "Mirroring"
        assert app.badges["audio"] == "Audio unavailable"
        assert app.state().stream == handle
        assert app.offered() == {Action.retry_audio, Action.stop}
        assert app.button(Action.mute).isEnabled() is False
        assert "Mute is disabled until audio is retried" in app.notice
        # A stale mute signal must not reach the transport while audio is down.
        app.window.mute_requested.emit(True)
        await app.settle()
        assert app.rig.transport.muted == {}
        app.click(Action.retry_audio)
        await app.settle()
        assert app.badges["audio"] == "Audio on"
        assert app.state().stream == handle
        audio_calls = app.calls("transport.start_audio")
        # The retry re-targets the exact same retained stream, never a new one.
        assert [field_of(call["handle"], "lease_id") for call in audio_calls] == [
            handle.lease_id, handle.lease_id,
        ]
        return

    if row == "dependency-failure":
        report = capability_report(
            virtual_display=False,
            diagnostics=(diagnostic(
                ErrorCode.dependency_missing,
                ReceiverId(RECEIVER_A),
                command="krfb-virtualmonitor",
            ),),
        )
        app = await build(capabilities=report)
        app.discover("a")
        await app.settle()
        app.widget("modeVirtual", QPushButton).click()
        await app.settle()
        assert app.phase == "Available"
        assert app.badges["display"] == "No output"
        assert app.offered() == {Action.retry}
        assert "krfb-virtualmonitor" in app.error
        assert "not installed or not on PATH" in app.error
        assert app.mode_buttons() == (False, True, True)
        # Unrelated UI stays usable: mirror mode is still a startable capability.
        app.widget("modeMirror", QPushButton).click()
        await app.settle()
        assert Action.start in app.offered()
        app.click(Action.start)
        await app.settle()
        assert app.phase == "Mirroring"
        assert app.rig.display.created == []
        return

    if row == "orphan":
        # Two durable output records for one receiver: the provider cannot pick
        # one as "the" owned output, so both are orphan candidates.  The session
        # reconciliation owns the approval; the pane must name the exact owner
        # it is about to remove, and Keep must never touch either record.
        first = ownership_record("a", owner=21, stage="output_observed")
        second = ownership_record(
            "a", owner=22, stage="output_observed", connector="Virtual-v2", output_id="v2",
        )
        app = await build(records=(first, second))
        app.discover("a")
        await app.settle()
        assert app.phase == "Cleanup required"
        assert app.badges["display"] == "Cleanup unconfirmed"
        assert app.notice == (
            "The owned resource could not be confirmed removed; retry cleanup or "
            "keep it for manual cleanup."
        )
        assert app.offered() == {
            Action.retry_cleanup,
            Action.keep_cleanup,
            Action.confirm_orphan,
            Action.keep_orphan,
            Action.stop,
        }
        assert app.error == diagnostic(
            ErrorCode.ownership_mismatch, ReceiverId(RECEIVER_A)
        ).message
        # Keep is the safe acknowledgement: nothing is dispatched or removed.
        app.click(Action.keep_orphan)
        await app.settle()
        assert app.dispatched == []
        assert app.rig.display.removed == []
        assert set(app.rig.ownerships.records) == {first.owner_uuid, second.owner_uuid}
        # The first confirmation names exactly the first record's owner.
        app.click(Action.confirm_orphan)
        await app.settle()
        dialog = await app.prompt("orphanDialog")
        evidence = app.prompt_texts(dialog)
        assert any(str(first.owner_uuid) in text for text in evidence)
        assert any("Virtual-v1" in text for text in evidence)
        assert any(str(first.helper.pid) in text for text in evidence)
        assert any(str(first.helper.start_ticks) in text for text in evidence)
        assert not any(str(second.owner_uuid) in text for text in evidence)
        app.prompt_buttons(dialog)["confirmOrphanButton"].click()
        await app.settle()
        approval, = app.calls("display.approve_orphan")
        assert approval["owner_uuid"] == first.owner_uuid
        assert approval["confirmed"] is True
        assert app.rig.display.removed == [first.owner_uuid]
        assert set(app.rig.ownerships.records) == {second.owner_uuid}
        assert app.phase == "Cleanup required"
        # The second confirmation removes only the remaining exact owner.
        app.click(Action.confirm_orphan)
        await app.settle()
        dialog = await app.prompt("orphanDialog")
        app.prompt_buttons(dialog)["confirmOrphanButton"].click()
        await app.settle()
        second_approval = app.calls("display.approve_orphan")[-1]
        assert second_approval["owner_uuid"] == second.owner_uuid
        assert app.rig.display.removed == [first.owner_uuid, second.owner_uuid]
        assert app.rig.ownerships.records == {}
        assert app.phase == "Available"
        assert app.badges["display"] == "No output"
        assert app.error == ""
        return

    if row == "cleanup-required":
        app = await build()
        app.discover("a")
        await app.settle()
        app.click(Action.start)
        await app.settle()
        handle = app.state().stream
        app.rig.transport.queue_result(
            "stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)),
        )
        app.click(Action.stop)
        await app.settle()
        assert app.phase == "Cleanup required"
        assert app.badges["display"] == "No output"
        assert app.badges["transport"] == "Stopping"
        # The stream was not released, so its audio control is still live: the
        # pane must not claim "Audio off" while the transport still holds it.
        assert app.badges["audio"] == "Audio on"
        assert app.state().stream == handle
        assert app.error == diagnostic(
            ErrorCode.cleanup_timeout, ReceiverId(RECEIVER_A)
        ).message
        assert Action.start not in app.offered()
        assert {Action.retry_cleanup, Action.keep_cleanup} <= app.offered()
        # Keep keeps the uncertainty: it dispatches nothing and releases nothing.
        app.click(Action.keep_cleanup)
        await app.settle()
        assert app.rig.transport.streams == {handle.lease_id: handle}
        assert app.state().cleanup_pending
        assert app.phase == "Cleanup required"
        # Retry releases exactly the retained stream this receiver still owns.
        app.click(Action.retry_cleanup)
        await app.settle()
        assert app.phase == "Available"
        assert app.rig.transport.streams == {}
        assert app.state().cleanup_pending is False
        assert app.badges["display"] == "No output"
        return

    if row == "trust-cleanup-required":
        app = await build(saved=(saved_receiver("a"),))
        app.discover("a")
        await app.settle()
        app.rig.pairing.queue_result(
            "forget_trust", Err(error=diagnostic(ErrorCode.trust_cleanup_required)),
        )
        await app.dispatchem(Forget(receiver_id=ReceiverId(RECEIVER_A)))
        assert app.phase == "Trust cleanup required"
        assert app.badges["trust"] == "Trust cleanup required"
        assert app.badges["display"] == "No output"
        assert app.error == diagnostic(
            ErrorCode.trust_cleanup_required, ReceiverId(RECEIVER_A)
        ).message
        assert app.offered() == {
            Action.retry_revocation, Action.keep_blocked, Action.remove_metadata,
        }
        assert Action.start not in app.offered()
        assert Action.pair not in app.offered()
        assert app.mode_buttons()[2] is False
        app.click(Action.keep_blocked)
        await app.settle()
        # Keep blocked is an acknowledgement: no command, no trust change.
        assert [type(command).__name__ for command in app.dispatched] == ["Forget"]
        assert app.state().trust is TrustState.CleanupRequired
        app.click(Action.retry_revocation)
        await app.settle()
        assert app.phase == "Available"
        assert app.badges["trust"] == "Not paired"
        assert app.state().trust is TrustState.Untrusted
        assert Action.start in app.offered()
        return

    if row == "failed":
        app = await build()
        app.discover("a")
        await app.settle()
        app.click(Action.start)
        await app.settle()
        retained = app.state().stream
        assert retained is not None

        async def failing_restart():
            return Err(error=diagnostic(ErrorCode.daemon_failed))

        object.__setattr__(app.runtime.deps, "restart_daemon", failing_restart)
        app.rig.transport.emit(app.transport_event(TransportEventKind.daemon_exit))
        await app.settle()
        assert app.phase == "Failed"
        assert app.badges["transport"] == "Transport failed"
        assert app.badges["display"] == "No output"
        # The exact stream is still owned: a failed local recovery must keep it
        # explicit rather than claiming the session released it.
        assert app.state().stream == retained
        assert app.offered() == {Action.retry, Action.stop}
        assert app.notice == (
            "The session failed. Owned resources stay explicit until cleanup is "
            "confirmed."
        )
        app.click(Action.stop)
        await app.settle()
        assert app.phase == "Available"
        assert app.rig.transport.streams == {}
        assert app.state().stream is None
        stopped = [
            field_of(call["handle"], "lease_id") for call in app.calls("transport.stop_stream")
        ]
        assert stopped[-1] == retained.lease_id
        return

    raise AssertionError(f"unimplemented matrix row: {row}")


# --------------------------------------------------------------- the seed check


async def test_audio_decision_has_three_explicit_choices(
    audio_decision_window: MainWindow,
) -> None:
    window = audio_decision_window
    assert window.findChild(QPushButton, "audioRetryButton").isEnabled()
    assert window.findChild(QPushButton, "videoOnlyButton").isEnabled()
    assert window.findChild(QPushButton, "cancelButton").isEnabled()
