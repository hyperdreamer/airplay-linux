"""Widget-level tests for the dense operations dashboard and its prompts.

Every assertion here is deliberately narrow about *rendering*: the widgets are
handed an already-decided projection (``PaneState``/``ReceiverRow``) and must
show exactly that.  Tests build projections by hand with implausible content so
a widget that recomputed its own phase or action set would fail immediately.
They also pin the accessibility contract (tooltips, accessible names, tab order)
and the transient, never-echoed handling of pairing secrets.

No test needs a visible desktop: ``QT_QPA_PLATFORM=offscreen`` is enough.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from PySide6.QtCore import QEvent, QModelIndex, QObject, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableView,
    QToolButton,
    QWidget,
)

from airplay_linux.domain.identity import ReceiverId, ReceiverRecordSet, ReceiverView
from airplay_linux.domain.operations import ChallengeId, ChallengeView, OperationId, OwnerId
from airplay_linux.domain.records import (
    LayoutSnapshot,
    OwnershipRecord,
    RecoveryItem,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    CapabilityReport,
    DisplayState,
    Mode,
    SessionPhase,
    Size,
    TransportState,
    TrustState,
)
from airplay_linux.testing.builders import physical, service, target, virtual_handle
from airplay_linux.ui.state import (
    Action,
    Badge,
    PaneState,
    ReceiverRow,
    ViewInputs,
    project,
    project_row,
)

RECEIVER = ReceiverId("airplay-device:a")
OTHER_RECEIVER = ReceiverId("airplay-device:b")
HD = Size(width=1920, height=1080)
SMALL = Size(width=1280, height=720)


# --------------------------------------------------------------------------- fixtures


def receiver_row(  # noqa: PLR0913 - mirrors the frozen row record
    name: str = "Living Room",
    receiver: str = "a",
    mode: str = "Mirror",
    phase: str = "Streaming",
    size: str = "",
    online: bool = True,
) -> ReceiverRow:
    return ReceiverRow(
        receiver_id=ReceiverId(f"airplay-device:{receiver}"),
        name=name,
        mode_label=mode,
        phase_label=phase,
        size_label=size,
        online=online,
    )


def capabilities(**overrides: object) -> CapabilityReport:
    defaults: dict[str, object] = {
        "environment": "kde-plasma-6-wayland",
        "discovery": True,
        "mirror": True,
        "virtual_display": True,
        "audio": True,
        "remembered_pairing": True,
        "additional_virtual_outputs": True,
        "diagnostics": (),
    }
    return CapabilityReport(**(defaults | overrides))  # type: ignore[arg-type]


def view_inputs(**overrides: object) -> ViewInputs:
    defaults: dict[str, object] = {
        "receiver": ReceiverView(
            record_set=ReceiverRecordSet(
                records=(service(),),
                receiver_id=RECEIVER,
                identity_kind="device",
                canonical_address="192.0.2.10",
                endpoint_fingerprint="endpoint-a-v1",
                endpoint_generation=1,
                persistable=True,
                continuity_verified=True,
                replacement_of=None,
                ambiguous=False,
            ),
            display_name="Living Room",
            online=True,
            pairing_hint=None,
        ),
        "saved": None,
        "capabilities": capabilities(),
        "selected_mode": Mode.MIRROR,
        "physical_sources": (physical(),),
        "resolution_choices": (HD, SMALL),
    }
    return ViewInputs(**(defaults | overrides))  # type: ignore[arg-type]


def idle_snapshot(**overrides: object) -> SessionSnapshot:
    defaults: dict[str, object] = {
        "receiver_id": RECEIVER,
        "target": target(),
        "online": True,
        "phase": SessionPhase.Available,
    }
    return SessionSnapshot(**(defaults | overrides))  # type: ignore[arg-type]


def pane(  # noqa: PLR0913 - explicit projection fields keep each test self-documenting
    *,
    title: str = "Living Room",
    phase_label: str = "Available",
    badges: tuple[Badge, ...] | None = None,
    actions: frozenset[Action] = frozenset(),
    mode_enabled: bool = True,
    visible_mode: Mode = Mode.MIRROR,
    primary_label: str = "Start",
    source_enabled: bool = False,
    resolution_enabled: bool = False,
    mute_enabled: bool = False,
    muted: bool = False,
    challenge: ChallengeView | None = None,
    notice: str | None = None,
    diagnostic: object | None = None,
) -> PaneState:
    return PaneState(
        title=title,
        phase_label=phase_label,
        badges=(
            (
                Badge(resource="transport", label="Streaming", severity="ok"),
                Badge(resource="display", label="No output", severity="info"),
                Badge(resource="audio", label="Audio on", severity="ok"),
                Badge(resource="trust", label="Paired", severity="ok"),
            )
            if badges is None
            else badges
        ),
        actions=actions,
        mode_enabled=mode_enabled,
        visible_mode=visible_mode,
        primary_label=primary_label,
        source_enabled=source_enabled,
        resolution_enabled=resolution_enabled,
        mute_enabled=mute_enabled,
        muted=muted,
        challenge=challenge,
        notice=notice,
        diagnostic=diagnostic,  # type: ignore[arg-type]
    )


def challenge(kind: str = "pin") -> ChallengeView:
    return ChallengeView(
        challenge_id=ChallengeId(UUID(int=11)),
        receiver_id=RECEIVER,
        endpoint_fingerprint="endpoint-a-v1",
        endpoint_generation=1,
        operation_id=OperationId(UUID(int=12)),
        session_generation=1,
        trust_generation=1,
        created_at=0.0,
        expires_at=120.0,
        credential_kind=kind,  # type: ignore[arg-type]
    )


def ownership(**overrides: object) -> OwnershipRecord:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "owner_uuid": OwnerId(UUID(int=5)),
        "app_instance_uuid": OwnerId(UUID(int=5)),
        "runtime_directory": "/run/user/1000/airplay-linux/instances/5",
        "creation_operation_id": OperationId(UUID(int=6)),
        "receiver_id": RECEIVER,
        "endpoint_fingerprint": "endpoint-a-v1",
        "desired_state": "active",
        "stage": "published",
        "requested_size": HD,
        "observed_size": HD,
        "helper": None,
        "connector": "Virtual-v1",
        "output_id": "v1",
        "created_utc": "2026-10-17T00:00:00Z",
        "last_configuration_step": "published",
        "baseline": LayoutSnapshot(outputs={}, baseline_hash="baseline"),
        "last_applied": {},
        "provider_version": "24.08.2",
        "capability_fingerprint": "fingerprint",
    }
    return OwnershipRecord(**(defaults | overrides))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- models


def test_receiver_model_renders_each_projected_column() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace(
        (
            receiver_row(name="Living Room", mode="Mirror", phase="Streaming"),
            receiver_row(
                name="Office TV",
                receiver="b",
                mode="Virtual display",
                phase="Virtual display",
                size="3840x2160",
            ),
        )
    )

    assert model.rowCount() == 2
    assert model.columnCount() == 4
    assert [model.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == [
        "Device",
        "Mode",
        "State",
        "Size",
    ]
    assert [
        model.data(model.index(0, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Living Room", "Mirror", "Streaming", ""]
    assert [
        model.data(model.index(1, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Office TV", "Virtual display", "Virtual display", "3840x2160"]


def test_receiver_model_maps_indexes_back_to_receiver_id() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(), receiver_row(name="Office TV", receiver="b")))

    assert model.receiver_id(model.index(0, 0)) == RECEIVER
    assert model.receiver_id(model.index(1, 3)) == OTHER_RECEIVER
    assert model.receiver_id(QModelIndex()) is None
    assert model.receiver_id(model.index(2, 0)) is None


def test_receiver_model_offline_and_empty_states_stay_readable() -> None:
    """An offline row and an empty list must both say what they are in words."""
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    assert model.rowCount() == 0
    # An empty model still exposes its headers so the list is not a blank hole.
    assert model.headerData(0, Qt.Orientation.Horizontal) == "Device"

    model.replace(
        (
            receiver_row(
                name="Bedroom TV",
                receiver="b",
                mode="",
                phase="Offline",
                size="",
                online=False,
            ),
        )
    )
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.AccessibleTextRole) == "Bedroom TV, offline"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Offline"
    assert (
        model.data(model.index(0, 2), Qt.ItemDataRole.AccessibleTextRole) == "Offline"
    )
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == ""
    assert (
        model.data(model.index(0, 3), Qt.ItemDataRole.AccessibleTextRole)
        == "No output size"
    )
    tooltip = model.data(index, Qt.ItemDataRole.ToolTipRole)
    assert "offline" in tooltip.lower()
    assert "Bedroom TV" in tooltip


def test_receiver_model_replace_is_one_atomic_reset() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(),))
    resets: list[None] = []
    model.modelReset.connect(lambda: resets.append(None))

    model.replace((receiver_row(name="Office TV", receiver="b"),))

    assert len(resets) == 1
    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole) == "Office TV"


def test_receiver_model_rejects_non_row_values_without_losing_rows() -> None:
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace((receiver_row(),))

    with pytest.raises(TypeError):
        model.replace((object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        model.replace("Living Room")  # type: ignore[arg-type]

    assert model.rowCount() == 1
    assert model.receiver_id(model.index(0, 0)) == RECEIVER


def test_receiver_model_reports_labels_verbatim_without_inventing_state() -> None:
    """The model is a view of the projection: odd labels survive unchanged."""
    from airplay_linux.ui.models import ReceiverListModel

    model = ReceiverListModel()
    model.replace(
        (
            receiver_row(
                name="Mystery Box",
                receiver="m",
                mode="Sideways",
                phase="Quantum flux",
                size="1x1",
            ),
        )
    )

    assert [
        model.data(model.index(0, column), Qt.ItemDataRole.DisplayRole)
        for column in range(4)
    ] == ["Mystery Box", "Sideways", "Quantum flux", "1x1"]
    assert model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsSelectable
    assert not model.flags(model.index(0, 0)) & Qt.ItemFlag.ItemIsEditable
# --------------------------------------------------------------------------- window


def test_dashboard_has_text_commands(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    assert window.findChild(QPushButton, "scanButton").text() == "Scan"
    assert window.findChild(QPushButton, "startButton").text() == "Start"
    assert window.findChild(QPushButton, "stopButton").text() == "Stop"


def test_window_constructs_and_updates_without_showing_or_touching_the_host(
    qtbot, monkeypatch
) -> None:
    """Construction and rendering are inert: no window, process, socket or bus."""
    import asyncio
    import os
    import socket
    import subprocess

    import dbus_next.aio
    from PySide6.QtCore import QProcess

    from airplay_linux.domain.records import RecoveryItem
    from airplay_linux.ui.dialogs import (
        ConfirmStopDialog,
        OrphanDialog,
        PairingDialog,
        ResolutionDialog,
    )
    from airplay_linux.ui.window import MainWindow

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("UI crossed a show/process/network/bus boundary")

    for target, names in (
        (subprocess, ("Popen", "run", "call", "check_call", "check_output")),
        (os, ("system", "posix_spawn", "posix_spawnp")),
        (asyncio, ("create_subprocess_exec", "create_subprocess_shell")),
        (QProcess, ("start", "startDetached")),
        (socket, ("create_connection",)),
        (socket.socket, ("connect", "connect_ex")),
        (dbus_next.aio.MessageBus, ("connect",)),
        (QWidget, ("show",)),
    ):
        for name in names:
            if hasattr(target, name):
                monkeypatch.setattr(target, name, forbidden)

    inputs = view_inputs()
    snapshot = idle_snapshot()
    window = MainWindow()
    qtbot.addWidget(window)
    assert not window.isVisible()

    window.render_receivers((project_row(snapshot, inputs),))
    window.render_selected(project(snapshot, inputs))
    window.set_sources(inputs.physical_sources)
    window.set_resolutions(inputs.resolution_choices)
    window.set_summary(1, 0, "Headless construction")
    window.set_activity(())
    assert not window.isVisible()

    item = RecoveryItem(
        kind="orphan", record=ownership(), handle=None, diagnostic=None
    )
    for dialogue in (
        PairingDialog(challenge("pin")),
        ConfirmStopDialog(Mode.VIRTUAL),
        ResolutionDialog((HD, SMALL)),
        OrphanDialog(item),
    ):
        qtbot.addWidget(dialogue)
        assert not dialogue.isVisible()


def test_receiver_table_and_selected_pane_coexist(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton, QSplitter, QTableView, QWidget

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    table = window.findChild(QTableView, "receiverTable")
    selected = window.findChild(QWidget, "selectedPane")
    activity = window.findChild(QWidget, "activityView")

    assert table is not None and selected is not None and activity is not None
    assert table.model().columnCount() == 4
    splitter = window.findChild(QSplitter, "dashboardSplitter")
    assert splitter is not None
    panel = window.findChild(QWidget, "receiverPanel")
    assert splitter.indexOf(panel) >= 0
    assert splitter.indexOf(selected) >= 0
    assert panel.isAncestorOf(table)
    # The bottom activity strip sits outside the splitter, under both panes.
    assert window.findChild(QPushButton, "scanButton").text() == "Scan"


def test_render_receivers_shows_rows_and_an_empty_state(qtbot) -> None:
    from PySide6.QtWidgets import QLabel, QTableView

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    table = window.findChild(QTableView, "receiverTable")
    empty = window.findChild(QLabel, "emptyStateLabel")

    window.render_receivers((receiver_row(), receiver_row(name="Office TV", receiver="b")))
    assert table.model().rowCount() == 2
    assert not empty.isVisibleTo(window)

    window.render_receivers(())
    assert table.model().rowCount() == 0
    assert empty.isVisibleTo(window)
    assert "No receivers" in empty.text()


def test_window_renders_an_actual_projection_verbatim(qtbot) -> None:
    """End to end through ``project``/``project_row``: no widget-side decisions."""
    from PySide6.QtWidgets import QLabel, QTableView, QWidget

    from airplay_linux.ui.window import MainWindow

    snapshot = idle_snapshot()
    inputs = view_inputs()
    row = project_row(snapshot, inputs)
    pane_state = project(snapshot, inputs)

    window = MainWindow()
    qtbot.addWidget(window)
    window.render_receivers((row,))
    window.render_selected(pane_state)

    table = window.findChild(QTableView, "receiverTable")
    assert table.model().data(table.model().index(0, 2), Qt.ItemDataRole.DisplayRole) == (
        pane_state.phase_label
    )
    assert window.findChild(QLabel, "paneTitle").text() == pane_state.title
    assert window.findChild(QLabel, "phaseLabel").text() == pane_state.phase_label
    if pane_state.notice is not None:
        assert window.findChild(QLabel, "noticeLabel").text() == pane_state.notice

    badges_row = window.findChild(QWidget, "resourceBadges")
    assert badges_row is not None
    rendered = {
        label.accessibleName(): label.text()
        for label in badges_row.findChildren(QLabel)
    }
    for badge in pane_state.badges:
        assert rendered[f"{badge.resource} badge"] == badge.label


def test_phase_label_renders_the_projection_even_when_it_is_not_available(qtbot) -> None:
    """The phase is an opaque projected label, never a widget-side constant."""
    from PySide6.QtWidgets import QLabel

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    for label in (
        "Mirroring",
        "Cleanup required",
        "Receiver supplied a phase I cannot infer",
    ):
        window.render_selected(
            pane(phase_label=label, actions=frozenset({Action.stop}))
        )
        assert window.findChild(QLabel, "phaseLabel").text() == label


def test_only_the_visible_modes_editor_is_shown(qtbot) -> None:
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    source = window.findChild(QComboBox, "sourceCombo")
    resolution = window.findChild(QComboBox, "resolutionCombo")

    window.render_selected(pane(visible_mode=Mode.MIRROR, source_enabled=True))
    assert source.isVisibleTo(window)
    assert not resolution.isVisibleTo(window)

    window.render_selected(pane(visible_mode=Mode.VIRTUAL, resolution_enabled=True))
    assert resolution.isVisibleTo(window)
    assert not source.isVisibleTo(window)


def test_mode_segments_stay_exclusive_and_disabled_means_disabled(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    mirror = window.findChild(QPushButton, "modeMirror")
    virtual = window.findChild(QPushButton, "modeVirtual")
    requested: list[str] = []
    window.mode_requested.connect(requested.append)

    window.render_selected(pane(visible_mode=Mode.MIRROR, mode_enabled=True))
    assert mirror.isChecked()
    assert not virtual.isChecked()

    mirror.click()  # already the rendered mode: no redundant request
    assert requested == []
    virtual.click()
    assert requested == ["virtual"]
    assert virtual.isChecked() and not mirror.isChecked()

    window.render_selected(pane(visible_mode=Mode.VIRTUAL, mode_enabled=False))
    assert virtual.isChecked() and not mirror.isChecked()
    assert not mirror.isEnabled() and not virtual.isEnabled()
    mirror.click()
    virtual.click()
    assert requested == ["virtual"]


def test_stop_is_enabled_only_because_the_projection_says_so(qtbot) -> None:
    """A phase label that *looks* active must not switch a control on by itself."""
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    start = window.findChild(QPushButton, "startButton")
    stop = window.findChild(QPushButton, "stopButton")

    window.render_selected(
        pane(phase_label="Mirroring", actions=frozenset({Action.start}))
    )
    assert start.isEnabled()
    assert not stop.isEnabled()

    window.render_selected(
        pane(phase_label="Available", actions=frozenset({Action.stop}))
    )
    assert stop.isEnabled()
    assert not start.isEnabled()


def test_start_button_label_follows_the_projection_primary_label(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    start = window.findChild(QPushButton, "startButton")

    window.render_selected(
        pane(
            actions=frozenset({Action.start}),
            visible_mode=Mode.VIRTUAL,
            primary_label="Create display",
        )
    )
    assert start.text() == "Create display"

    # An unrelated primary label does not rename the start command.
    window.render_selected(
        pane(
            actions=frozenset({Action.choose_resolution, Action.cancel}),
            primary_label="Choose",
        )
    )
    assert start.text() == "Start"
    assert not start.isEnabled()


def test_scan_is_offered_by_the_fresh_window_and_the_projection(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    scan = window.findChild(QPushButton, "scanButton")
    requested: list[str] = []
    window.action_requested.connect(requested.append)

    assert scan.isEnabled()  # nothing rendered yet: scanning is still allowed
    scan.click()
    assert requested == ["scan"]

    window.render_selected(pane(actions=frozenset({Action.stop})))
    assert not scan.isEnabled()

    window.render_selected(pane(actions=frozenset({Action.scan})))
    assert scan.isEnabled()


def test_audio_decision_prompt_is_explicit_and_pane_local(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    retry = window.findChild(QPushButton, "audioRetryButton")
    video_only = window.findChild(QPushButton, "videoOnlyButton")
    cancel = window.findChild(QPushButton, "cancelButton")
    requested: list[str] = []
    window.action_requested.connect(requested.append)

    window.render_selected(
        pane(actions=frozenset({Action.retry_audio, Action.video_only, Action.cancel}))
    )
    for button, label in (
        (retry, "Retry audio"),
        (video_only, "Start video only"),
        (cancel, "Cancel"),
    ):
        assert button.isVisibleTo(window)
        assert button.isEnabled()
        assert button.text() == label
        assert button.toolTip()

    retry.click()
    video_only.click()
    cancel.click()
    assert requested == ["retry_audio", "video_only", "cancel"]

    window.render_selected(pane(actions=frozenset({Action.stop})))
    for button in (retry, video_only, cancel):
        assert not button.isVisibleTo(window)
        assert not button.isEnabled()


def test_cleanup_and_orphan_choices_are_explicit(qtbot) -> None:
    from PySide6.QtWidgets import QPushButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    requested: list[str] = []
    window.action_requested.connect(requested.append)

    window.render_selected(
        pane(
            phase_label="Cleanup required",
            actions=frozenset(
                {Action.retry_cleanup, Action.keep_cleanup, Action.stop}
            ),
        )
    )
    retry = window.findChild(QPushButton, "retryCleanupButton")
    keep = window.findChild(QPushButton, "keepCleanupButton")
    assert retry.isVisibleTo(window) and retry.isEnabled()
    assert keep.isVisibleTo(window) and keep.isEnabled()
    retry.click()
    keep.click()

    window.render_selected(
        pane(
            phase_label="Available",
            actions=frozenset({Action.confirm_orphan, Action.keep_orphan}),
        )
    )
    assert not window.findChild(QPushButton, "retryCleanupButton").isVisibleTo(window)
    confirm = window.findChild(QPushButton, "confirmOrphanButton")
    keep_orphan = window.findChild(QPushButton, "keepOrphanButton")
    assert confirm.isVisibleTo(window) and keep_orphan.isVisibleTo(window)
    confirm.click()
    keep_orphan.click()

    assert requested == [
        "retry_cleanup",
        "keep_cleanup",
        "confirm_orphan",
        "keep_orphan",
    ]


ACTION_CONTROLS: tuple[tuple[Action, str], ...] = (
    (Action.start, "startButton"),
    (Action.stop, "stopButton"),
    (Action.pair, "pairButton"),
    (Action.scan, "scanButton"),
    (Action.submit_pairing, "submitPairingButton"),
    (Action.cancel, "cancelButton"),
    (Action.remove_metadata, "removeMetadataButton"),
    (Action.retry, "retryButton"),
    (Action.choose_resolution, "chooseResolutionButton"),
    (Action.change_resolution, "changeResolutionButton"),
    (Action.recreate, "recreateButton"),
    (Action.retry_audio, "audioRetryButton"),
    (Action.video_only, "videoOnlyButton"),
    (Action.retry_cleanup, "retryCleanupButton"),
    (Action.keep_cleanup, "keepCleanupButton"),
    (Action.retry_revocation, "retryRevocationButton"),
    (Action.keep_blocked, "keepBlockedButton"),
    (Action.confirm_orphan, "confirmOrphanButton"),
    (Action.keep_orphan, "keepOrphanButton"),
)

CONTEXTUAL_ACTIONS: frozenset[Action] = frozenset(
    action for action, _ in ACTION_CONTROLS
) - {
    Action.start,
    Action.stop,
    Action.pair,
    Action.scan,
}


@pytest.mark.parametrize(("action", "object_name"), ACTION_CONTROLS)
def test_every_projected_action_has_exactly_one_enabled_control(
    qtbot, action: Action, object_name: str
) -> None:
    """Each Action in the projection enables its own control and hides the rest."""
    from PySide6.QtWidgets import QAbstractButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    requested: list[str] = []
    window.action_requested.connect(requested.append)
    window.render_selected(pane(actions=frozenset({action})))

    control = window.findChild(QAbstractButton, object_name)
    assert control is not None, object_name
    assert control.isVisibleTo(window), object_name
    assert control.isEnabled(), object_name
    assert control.text(), object_name

    for other, other_name in ACTION_CONTROLS:
        if other in CONTEXTUAL_ACTIONS and other is not action:
            hidden = window.findChild(QAbstractButton, other_name)
            assert not hidden.isVisibleTo(window), other_name

    control.click()
    assert requested == [action.value]


def test_every_disabled_command_refuses_click_and_keyboard_activation(qtbot) -> None:
    """A disabled control must never reach the presenter, however it is activated."""
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    actions: list[str] = []
    modes: list[str] = []
    mute: list[bool] = []
    window.action_requested.connect(actions.append)
    window.mode_requested.connect(modes.append)
    window.mute_requested.connect(mute.append)

    window.render_selected(
        pane(actions=frozenset(), mode_enabled=False, mute_enabled=False)
    )
    for _, object_name in ACTION_CONTROLS:
        control = window.findChild(QAbstractButton, object_name)
        assert control is not None and not control.isEnabled(), object_name
        control.click()
        QTest.keyClick(control, Qt.Key.Key_Space)
    for object_name in ("modeMirror", "modeVirtual", "muteToggle"):
        control = window.findChild(QAbstractButton, object_name)
        assert control is not None and not control.isEnabled(), object_name
        control.click()
        QTest.keyClick(control, Qt.Key.Key_Space)

    assert actions == []
    assert modes == []
    assert mute == []


def test_mute_toggle_is_labelled_and_emits_the_requested_state(qtbot) -> None:
    from PySide6.QtWidgets import QToolButton

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    mute = window.findChild(QToolButton, "muteToggle")
    assert mute is not None
    assert mute.text() == ""  # icon-only: the tooltip and name carry the meaning
    assert not mute.icon().isNull()
    assert mute.toolTip()
    assert mute.accessibleName() == "Mute receiver audio"

    requests: list[bool] = []
    window.mute_requested.connect(requests.append)

    window.render_selected(pane(mute_enabled=True, muted=False))
    assert mute.isEnabled()
    assert not mute.isChecked()
    assert requests == []  # rendering never emits a mute request

    mute.click()
    assert requests == [True]
    assert mute.isChecked()

    window.render_selected(pane(mute_enabled=True, muted=True))
    assert mute.isChecked()
    mute.click()
    assert requests == [True, False]

    window.render_selected(pane(mute_enabled=False, muted=True))
    assert not mute.isEnabled()
    mute.click()
    assert requests == [True, False]


def test_badges_are_replaced_in_place_on_every_render(qtbot) -> None:
    """Re-rendering must never stack a stale badge on top of a fresh one."""
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)

    window.render_selected(
        pane(
            badges=(
                Badge(resource="transport", label="Streaming", severity="ok"),
                Badge(resource="display", label="Output owned", severity="ok"),
            )
        )
    )
    strip = window.findChild(QWidget, "resourceBadges")
    visible = [label for label in strip.findChildren(QLabel) if label.isVisibleTo(window)]
    assert [label.text() for label in visible] == ["Streaming", "Output owned"]
    assert [label.accessibleName() for label in visible] == [
        "transport badge",
        "display badge",
    ]

    window.render_selected(
        pane(
            badges=(Badge(resource="transport", label="Idle", severity="info"),),
            phase_label="Available",
        )
    )
    visible = [label for label in strip.findChildren(QLabel) if label.isVisibleTo(window)]
    assert [label.text() for label in visible] == ["Idle"]

    window.render_selected(pane(badges=()))
    assert [
        label for label in strip.findChildren(QLabel) if label.isVisibleTo(window)
    ] == []


def test_notice_and_error_labels_render_the_projection_and_hide_when_empty(qtbot) -> None:
    from PySide6.QtWidgets import QLabel

    from airplay_linux.diagnostics import diagnostic
    from airplay_linux.domain.resources import ErrorCode
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    notice = window.findChild(QLabel, "noticeLabel")
    error = window.findChild(QLabel, "errorLabel")

    window.render_selected(pane(notice=None, diagnostic=None))
    assert not notice.isVisibleTo(window)
    assert not error.isVisibleTo(window)

    failure = diagnostic(ErrorCode.cleanup_timeout, RECEIVER)
    window.render_selected(
        pane(notice="Retained output is still owned.", diagnostic=failure)
    )
    assert notice.isVisibleTo(window)
    assert notice.text() == "Retained output is still owned."
    assert error.isVisibleTo(window)
    assert error.text() == failure.message


def test_summary_reports_counts_and_environment(qtbot) -> None:
    from PySide6.QtWidgets import QLabel

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.set_summary(available=3, sessions=2, environment="KDE Wayland")

    summary = window.findChild(QLabel, "summaryLabel")
    environment = window.findChild(QLabel, "environmentLabel")
    assert summary.text() == "3 TVs available  2 sessions"
    assert summary.accessibleName()
    assert environment.text() == "KDE Wayland"

    window.set_summary(available=1, sessions=1, environment="KDE Plasma 6 Wayland")
    assert summary.text() == "1 TV available  1 session"
    assert environment.text() == "KDE Plasma 6 Wayland"


def test_activity_view_is_read_only_and_replaces_its_events(qtbot) -> None:
    from PySide6.QtWidgets import QPlainTextEdit

    from airplay_linux.domain.records import ActivityEvent
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    activity = window.findChild(QPlainTextEdit, "activityView")
    assert activity.isReadOnly()
    assert activity.accessibleName() == "Activity log"

    events = (
        ActivityEvent(
            utc="2026-10-17T10:00:00Z",
            code="pairing_completed",
            receiver_id=RECEIVER,
            message="Pairing completed; receiver is ready",
            fields={},
        ),
        ActivityEvent(
            utc="2026-10-17T10:00:05Z",
            code="display_published",
            receiver_id=RECEIVER,
            message="Virtual output published",
            fields={},
        ),
    )
    window.set_activity(events)
    text = activity.toPlainText()
    assert "Pairing completed; receiver is ready" in text
    assert "Virtual output published" in text
    assert text.index("Pairing completed") < text.index("Virtual output published")

    window.set_activity(events)
    assert activity.toPlainText().count("Pairing completed; receiver is ready") == 1


def focus_chain(start: object) -> list[object]:
    """Every widget reachable from ``start`` by following the focus chain once."""
    chain: list[object] = []
    current = start.nextInFocusChain()
    while current is not start and len(chain) < 500:
        chain.append(current)
        current = current.nextInFocusChain()
    return chain


def test_keyboard_navigation_reaches_every_enabled_command(qtbot) -> None:
    from PySide6.QtWidgets import QAbstractButton, QComboBox

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.render_receivers((receiver_row(),))
    window.render_selected(
        pane(
            actions=frozenset({action for action, _ in ACTION_CONTROLS}),
            mode_enabled=True,
            source_enabled=True,
            resolution_enabled=True,
            mute_enabled=True,
        )
    )

    chain = focus_chain(window)
    for button in window.findChildren(QAbstractButton):
        if not button.isVisibleTo(window) or not button.isEnabled():
            continue
        assert button.focusPolicy() != Qt.FocusPolicy.NoFocus, button.objectName()
        assert button in chain, button.objectName()
        if not button.text():
            # Icon-only controls must still be describable and learnable.
            assert button.accessibleName(), button.objectName()
            assert button.toolTip(), button.objectName()
    for combo in window.findChildren(QComboBox):
        if not combo.isVisibleTo(window) or not combo.isEnabled():
            continue
        assert combo.focusPolicy() != Qt.FocusPolicy.NoFocus, combo.objectName()
        assert combo in chain, combo.objectName()
        assert combo.accessibleName(), combo.objectName()


def test_receiver_selection_is_reported_as_receiver_id_text(qtbot) -> None:
    from PySide6.QtCore import QItemSelectionModel
    from PySide6.QtWidgets import QTableView

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    table = window.findChild(QTableView, "receiverTable")
    window.render_receivers(
        (receiver_row(), receiver_row(name="Office TV", receiver="b"))
    )
    selected: list[str] = []
    window.receiver_selected.connect(selected.append)

    table.selectionModel().setCurrentIndex(
        table.model().index(0, 0),
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows,
    )
    assert selected == ["airplay-device:a"]

    # Presenter-driven selection is silent, so rendering cannot echo a request.
    window.select_receiver(OTHER_RECEIVER)
    assert selected == ["airplay-device:a"]
    assert table.currentIndex().row() == 1

    # A re-render keeps the presenter's selection instead of inventing a new one.
    window.render_receivers(
        (receiver_row(), receiver_row(name="Office TV", receiver="b"))
    )
    assert table.currentIndex().row() == 1
    assert selected == ["airplay-device:a"]


def test_close_is_emitted_once_and_deferred_to_the_presenter(qtbot) -> None:
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    closing: list[bool] = []
    window.closing_requested.connect(lambda: closing.append(True))

    assert window.close() is False  # deferred: cleanup has not been confirmed
    assert closing == [True]
    assert window.isVisible()

    assert window.close() is False  # idempotent: no second shutdown future
    assert closing == [True]

    window.allow_close()
    assert window.close() is True
    assert not window.isVisible()
    assert closing == [True]


def test_sources_and_resolutions_are_populated_without_emitting_requests(qtbot) -> None:
    """Populating choices is rendering, not intent: no signal may fire."""
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    source = window.findChild(QComboBox, "sourceCombo")
    resolution = window.findChild(QComboBox, "resolutionCombo")
    sources: list[str] = []
    sizes: list[tuple[int, int]] = []
    window.source_selected.connect(sources.append)
    window.resolution_selected.connect(lambda w, h: sizes.append((w, h)))

    window.set_sources((physical("DP-1"), physical("HDMI-A-1")))
    window.set_resolutions((HD, SMALL))
    assert [source.itemText(i) for i in range(source.count())] == ["DP-1", "HDMI-A-1"]
    assert [resolution.itemText(i) for i in range(resolution.count())] == [
        "1920x1080",
        "1280x720",
    ]
    assert sources == [] and sizes == []

    source.setCurrentIndex(1)
    resolution.setCurrentIndex(1)
    assert sources == ["HDMI-A-1"]
    assert sizes == [(1280, 720)]


def test_presenter_selection_of_choices_is_silent(qtbot) -> None:
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    source = window.findChild(QComboBox, "sourceCombo")
    resolution = window.findChild(QComboBox, "resolutionCombo")
    emitted: list[object] = []
    window.source_selected.connect(emitted.append)
    window.resolution_selected.connect(lambda w, h: emitted.append((w, h)))
    window.set_sources((physical("DP-1"), physical("HDMI-A-1")))
    window.set_resolutions((HD, SMALL))

    window.select_source("HDMI-A-1")
    window.select_resolution(SMALL)
    assert source.currentIndex() == 1
    assert resolution.currentData() == SMALL
    assert emitted == []

    # An unknown choice is ignored instead of inventing an entry.
    window.select_source("DP-9")
    window.select_resolution(Size(width=640, height=480))
    assert source.currentIndex() == 1
    assert resolution.currentData() == SMALL
    assert emitted == []


def test_choice_controls_explain_themselves(qtbot) -> None:
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    for name in ("sourceCombo", "resolutionCombo"):
        combo = window.findChild(QComboBox, name)
        assert combo is not None
        assert combo.accessibleName()
        assert combo.toolTip()
# --------------------------------------------------------------------------- dialogs


def test_pairing_prompt_masks_a_pin_and_returns_it_transiently(qtbot, caplog) -> None:
    import logging

    from PySide6.QtWidgets import QLineEdit

    from airplay_linux.domain.operations import SensitiveText
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    field = dialogue.findChild(QLineEdit, "secretField")
    submit = dialogue.findChild(QPushButton, "pairingSubmitButton")
    assert field is not None and submit is not None
    assert dialogue.isModal()
    assert field.echoMode() == QLineEdit.EchoMode.Password
    assert field.inputMethodHints() & Qt.InputMethodHint.ImhSensitiveData
    assert "PIN" in field.placeholderText() or "PIN" in field.accessibleName()

    with caplog.at_level(logging.DEBUG):
        qtbot.keyClicks(field, "482913")
        submit.click()

    taken = dialogue.take_input()
    assert isinstance(taken, SensitiveText)
    assert taken.reveal_for_backend() == "482913"
    # The widget keeps no copy once the value has been handed over.
    assert field.text() == ""
    assert dialogue.take_input() is None
    # Nothing about the secret is echoed, titled, labelled, or logged.
    assert "482913" not in caplog.text
    assert "482913" not in dialogue.windowTitle()
    for label in dialogue.findChildren(QLabel):
        assert "482913" not in label.text()
    assert "482913" not in repr(taken)


def test_pairing_prompt_accepts_an_unrestricted_password(qtbot) -> None:
    from PySide6.QtWidgets import QLineEdit

    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("password"))
    qtbot.addWidget(dialogue)
    field = dialogue.findChild(QLineEdit, "secretField")
    assert field.echoMode() == QLineEdit.EchoMode.Password
    assert field.maxLength() == 32767  # default: no arbitrary password cap
    # A password must not be typed through a numeric-only keypad.
    assert not field.inputMethodHints() & Qt.InputMethodHint.ImhDigitsOnly
    assert dialogue.windowTitle().lower().startswith("pair")

    secret = "correct horse battery staple!"
    field.setText(secret)
    dialogue.confirm_input()

    taken = dialogue.take_input()
    assert taken is not None
    assert taken.reveal_for_backend() == secret
    assert field.text() == ""


def test_cancelled_pairing_prompt_clears_the_field_and_returns_nothing(qtbot) -> None:
    from PySide6.QtWidgets import QLineEdit

    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    field = dialogue.findChild(QLineEdit, "secretField")
    field.setText("111111")

    dialogue.reject()

    assert dialogue.take_input() is None
    assert field.text() == ""


def test_pairing_prompt_forgets_the_secret_from_its_undo_history(qtbot) -> None:
    """A closed prompt must not replay the typed secret through Ctrl+Z."""
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    field = dialogue.findChild(QLineEdit, "secretField")
    field.setText("482913")
    dialogue.reject()

    assert field.text() == ""
    assert not field.isUndoAvailable()
    field.undo()
    field.undo()
    assert field.text() == ""


def test_pairing_prompt_does_not_submit_an_empty_secret(qtbot) -> None:
    from PySide6.QtWidgets import QLineEdit

    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    dialogue.findChild(QLineEdit, "secretField").setText("   ")
    dialogue.confirm_input()

    assert dialogue.take_input() is None


def test_pairing_prompt_shows_identity_retained_output_and_expiry(qtbot) -> None:
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"), receiver_label="Living Room")
    qtbot.addWidget(dialogue)
    texts = " ".join(label.text() for label in dialogue.findChildren(QLabel))
    assert "Living Room" in texts
    assert "120" in texts  # the pairing expiry, in seconds of the design budget

    retained = PairingDialog(challenge("pin"), retained_output="Virtual-v1")
    qtbot.addWidget(retained)
    notice = " ".join(label.text() for label in retained.findChildren(QLabel))
    assert "Virtual-v1" in notice
    assert "removed" in notice.lower()


def pairing_inventory(dialogue) -> set[Action]:
    """Map a pairing prompt's real buttons back to the actions they represent."""
    offered: set[Action] = set()
    for button in dialogue.findChildren(QPushButton):
        if button.objectName() == "pairingSubmitButton":
            offered.add(Action.submit_pairing)
        elif button.objectName() == "pairingCancelButton":
            offered.add(Action.cancel)
        elif button.text().strip() == "Retry":
            offered.add(Action.retry)
        else:
            pytest.fail(f"unprojected pairing control {button.objectName()}")
    return offered


def test_pairing_modal_offers_exactly_the_projection_actions(qtbot) -> None:
    """Both real pairing projections expose their choices inside the prompt."""
    from airplay_linux.ui.dialogs import PairingDialog

    plain_inputs = view_inputs()
    plain = idle_snapshot(
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        transport=TransportState.Pairing,
        challenge=challenge("pin"),
    )
    retained_inputs = view_inputs(selected_mode=Mode.VIRTUAL, ownership=ownership())
    retained = idle_snapshot(
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        transport=TransportState.Idle,
        mode=Mode.VIRTUAL,
        intent_active=True,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
        observed_resolution=HD,
        challenge=challenge("pin"),
    )

    plain_pane = project(plain, plain_inputs)
    retained_pane = project(retained, retained_inputs)
    assert plain_pane.actions == frozenset(
        {Action.submit_pairing, Action.cancel, Action.retry}
    )
    assert retained_pane.actions == frozenset(
        {Action.submit_pairing, Action.cancel}
    )

    cases = (
        (plain_pane, PairingDialog(plain_pane.challenge)),
        (
            retained_pane,
            PairingDialog(retained_pane.challenge, retained_output="Virtual-v1"),
        ),
    )
    for pane_state, dialogue in cases:
        qtbot.addWidget(dialogue)
        assert dialogue.isModal()
        offered = pairing_inventory(dialogue)
        assert offered == pane_state.actions
        assert len(dialogue.findChildren(QPushButton)) == len(pane_state.actions)


class WindowModalityRecorder(QObject):
    """Count the window-blocking notifications Qt delivers to one window."""

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self.blocked = 0
        self.unblocked = 0
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt
        if event.type() is QEvent.Type.WindowBlocked:
            self.blocked += 1
        elif event.type() is QEvent.Type.WindowUnblocked:
            self.unblocked += 1
        return False


def pairing_projection() -> PaneState:
    """The real no-retained-output pairing projection and its offered actions."""
    snapshot = idle_snapshot(
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        transport=TransportState.Pairing,
        challenge=challenge("pin"),
    )
    state = project(snapshot, view_inputs())
    assert state.actions == frozenset(
        {Action.submit_pairing, Action.cancel, Action.retry}
    )
    return state


@pytest.mark.parametrize("present", ("open", "show"))
def test_dashboard_retry_stays_reachable_while_the_pairing_prompt_is_open(
    qtbot, present: str
) -> None:
    """An open pairing prompt must not block the dashboard control it projects."""
    from airplay_linux.ui.dialogs import PairingDialog
    from airplay_linux.ui.window import MainWindow

    state = pairing_projection()
    window = MainWindow()
    qtbot.addWidget(window)
    window.render_selected(state)
    window.show()
    QApplication.processEvents()

    events = WindowModalityRecorder(window)
    requested: list[str] = []
    window.action_requested.connect(requested.append)

    dialogue = PairingDialog(state.challenge, actions=state.actions, parent=window)
    qtbot.addWidget(dialogue)
    getattr(dialogue, present)()
    QApplication.processEvents()

    # Still an explicit modal window that offers exactly the projected choices...
    assert QApplication.activeModalWidget() is dialogue
    assert dialogue.isModal()
    assert dialogue.windowModality() == Qt.WindowModality.WindowModal
    assert pairing_inventory(dialogue) == state.actions
    # ...but because it does not own the dashboard, Qt never blocks the dashboard.
    assert events.blocked == 0
    retry = window.findChild(QPushButton, "retryButton")
    assert retry.isEnabled() and retry.isVisibleTo(window)
    QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
    assert requested == ["retry"]

    dialogue.reject()
    QApplication.processEvents()
    assert events.blocked == 0
    assert not dialogue.isVisible()


@pytest.mark.parametrize("kind", ("stop", "resolution", "orphan"))
def test_destructive_confirmation_prompts_still_block_the_dashboard(
    qtbot, kind: str
) -> None:
    """Only pairing is owner-less; a destructive decision keeps the lockout."""
    from airplay_linux.ui.dialogs import ConfirmStopDialog, OrphanDialog, ResolutionDialog
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    QApplication.processEvents()
    if kind == "stop":
        dialogue = ConfirmStopDialog(Mode.VIRTUAL, parent=window)
    elif kind == "resolution":
        dialogue = ResolutionDialog((HD, SMALL), parent=window)
    else:
        dialogue = OrphanDialog(
            RecoveryItem(
                kind="orphan", record=ownership(helper=None), handle=None, diagnostic=None
            ),
            parent=window,
        )
    qtbot.addWidget(dialogue)
    events = WindowModalityRecorder(window)

    dialogue.open()
    QApplication.processEvents()
    assert QApplication.activeModalWidget() is dialogue
    assert events.blocked >= 1

    dialogue.reject()
    QApplication.processEvents()
    assert events.unblocked >= 1


def test_a_dashboard_command_dismisses_the_pairing_prompt_before_signalling(qtbot) -> None:
    """A command supersedes the attempt, so the typed secret is gone before it emits."""
    from airplay_linux.ui.dialogs import PairingDialog
    from airplay_linux.ui.window import MainWindow

    state = pairing_projection()
    window = MainWindow()
    qtbot.addWidget(window)
    window.render_selected(state)
    # Opening the prompt on the dashboard tracks it automatically, so no command
    # can be issued while a typed PIN is still sitting in the field.
    dialogue = PairingDialog(state.challenge, actions=state.actions, parent=window)
    qtbot.addWidget(dialogue)
    dialogue.open()
    QApplication.processEvents()
    field = dialogue.findChild(QLineEdit, "secretField")
    field.insert("dismiss-before-signal-sentinel-3307")

    observed: list[tuple[str, str, bool]] = []
    window.action_requested.connect(
        lambda action: observed.append((action, field.text(), dialogue.isVisible()))
    )
    window.findChild(QPushButton, "retryButton").click()

    assert observed == [("retry", "", False)]
    assert not field.isUndoAvailable()
    assert dialogue.take_input() is None


def test_closing_the_dashboard_also_wipes_an_open_pairing_prompt(qtbot) -> None:
    """A deferred window close must still drop the typed secret immediately."""
    from airplay_linux.ui.dialogs import PairingDialog
    from airplay_linux.ui.window import MainWindow

    state = pairing_projection()
    window = MainWindow()
    qtbot.addWidget(window)
    window.render_selected(state)
    dialogue = PairingDialog(state.challenge, actions=state.actions, parent=window)
    qtbot.addWidget(dialogue)
    dialogue.open()
    QApplication.processEvents()
    field = dialogue.findChild(QLineEdit, "secretField")
    field.insert("close-dashboard-sentinel-4471")

    window.close()  # deferred to the presenter, but the prompt is not deferred
    QApplication.processEvents()

    assert not dialogue.isVisible()
    assert field.text() == ""
    assert not field.isUndoAvailable()
    assert dialogue.take_input() is None


def test_pairing_prompt_ends_with_the_dashboard_that_owns_it(qtbot) -> None:
    """Without a Qt parent the prompt must still not outlive the dashboard."""
    import shiboken6

    from airplay_linux.ui.dialogs import PairingDialog
    from airplay_linux.ui.window import MainWindow

    window = MainWindow()  # deliberately not registered: this test destroys it
    dialogue = PairingDialog(challenge("pin"), parent=window)
    qtbot.addWidget(dialogue)
    dialogue.open()
    QApplication.processEvents()
    field = dialogue.findChild(QLineEdit, "secretField")
    field.insert("owner-lifetime-sentinel-9182")

    shiboken6.delete(window)
    QApplication.processEvents()

    assert not dialogue.isVisible()
    assert field.text() == ""
    assert not field.isUndoAvailable()


def test_pairing_retry_abandons_the_secret_and_asks_for_a_new_challenge(qtbot) -> None:
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("password"))
    qtbot.addWidget(dialogue)
    retry = dialogue.findChild(QPushButton, "pairingRetryButton")
    assert retry is not None and retry.text() == "Retry"
    field = dialogue.findChild(QLineEdit, "secretField")
    field.insert("retry-sentinel-8821")
    requested: list[bool] = []
    dialogue.retry_requested.connect(lambda: requested.append(True))

    retry.click()

    assert requested == [True]
    assert field.text() == ""
    assert not field.isUndoAvailable()
    assert dialogue.take_input() is None


PAIRING_COMPLETION_EXITS: tuple[tuple[bool, str], ...] = (
    (False, "accept"),
    (False, "reject"),
    (False, "close"),
    (False, "done_accepted"),
    (False, "done_rejected"),
    (True, "accept"),
    (True, "reject"),
    (True, "close"),
    (True, "done_accepted"),
    (True, "done_rejected"),
    (True, "cancel_button"),
    (True, "escape"),
    (True, "hide"),
)


@pytest.mark.parametrize(("shown", "exit_path"), PAIRING_COMPLETION_EXITS)
def test_pairing_completion_always_wipes_the_secret_and_its_undo(
    qtbot, shown: bool, exit_path: str
) -> None:
    """Every exit - including inherited done/accept and programmatic close."""
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    if shown:
        dialogue.show()
        QApplication.processEvents()
    field = dialogue.findChild(QLineEdit, "secretField")
    secret = f"completion-sentinel-{exit_path}-739102"
    field.insert(secret)
    assert field.text() == secret

    if exit_path == "accept":
        dialogue.accept()
    elif exit_path == "reject":
        dialogue.reject()
    elif exit_path == "close":
        dialogue.close()
    elif exit_path == "hide":
        dialogue.hide()
    elif exit_path == "cancel_button":
        dialogue.findChild(QPushButton, "pairingCancelButton").click()
    elif exit_path == "escape":
        QTest.keyClick(dialogue, Qt.Key.Key_Escape)
    else:
        result = (
            QDialog.DialogCode.Accepted
            if exit_path == "done_accepted"
            else QDialog.DialogCode.Rejected
        )
        dialogue.done(result)
    QApplication.processEvents()

    assert field.text() == ""
    assert not field.isUndoAvailable()
    field.undo()
    field.undo()
    assert field.text() == ""
    assert dialogue.take_input() is None
    assert secret not in repr(vars(dialogue))
    if shown:
        assert not dialogue.isVisible()


@pytest.mark.parametrize("exit_path", ("reject", "close", "done_rejected"))
def test_a_late_cancellation_after_submit_drops_the_pending_secret(
    qtbot, exit_path: str
) -> None:
    """A submitted value still needs an immediate take; a later cancel drops it."""
    from airplay_linux.ui.dialogs import PairingDialog

    dialogue = PairingDialog(challenge("pin"))
    qtbot.addWidget(dialogue)
    field = dialogue.findChild(QLineEdit, "secretField")
    field.insert("pending-submit-sentinel-5514")
    dialogue.confirm_input()
    assert field.text() == ""

    if exit_path == "reject":
        dialogue.reject()
    elif exit_path == "close":
        dialogue.close()
    else:
        dialogue.done(QDialog.DialogCode.Rejected)

    assert dialogue.take_input() is None


def test_confirm_stop_dialog_requires_an_explicit_choice(qtbot) -> None:
    from airplay_linux.ui.dialogs import ConfirmStopDialog

    dialogue = ConfirmStopDialog(Mode.VIRTUAL)
    qtbot.addWidget(dialogue)
    assert dialogue.confirmed() is False
    assert "Stop" in dialogue.windowTitle()
    texts = " ".join(label.text() for label in dialogue.findChildren(QLabel))
    assert "Virtual display" in texts
    assert "cleanup" in texts.lower() or "remove" in texts.lower()

    confirm = dialogue.findChild(QPushButton, "confirmStopButton")
    keep = dialogue.findChild(QPushButton, "keepSessionButton")
    assert confirm.text() and keep.text()
    confirm.click()
    assert dialogue.confirmed() is True


def test_confirm_stop_dialog_default_answer_keeps_the_session(qtbot) -> None:
    from airplay_linux.ui.dialogs import ConfirmStopDialog

    dialogue = ConfirmStopDialog(Mode.MIRROR)
    qtbot.addWidget(dialogue)
    dialogue.reject()
    assert dialogue.confirmed() is False


def test_resolution_prompt_never_guesses(qtbot) -> None:
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.dialogs import ResolutionDialog

    dialogue = ResolutionDialog((HD, SMALL))
    qtbot.addWidget(dialogue)
    combo = dialogue.findChild(QComboBox, "resolutionChoice")
    assert combo.count() == 2
    assert combo.itemText(0) == "1920x1080"
    assert combo.itemText(1) == "1280x720"
    # No preselected guess about the receiver's resolution.
    assert dialogue.decision() is None

    texts = " ".join(label.text() for label in dialogue.findChildren(QLabel))
    assert "layout" in texts.lower()

    confirm = dialogue.findChild(QPushButton, "confirmResolutionButton")
    confirm.click()
    assert dialogue.decision() == HD


def test_resolution_prompt_cancel_returns_no_decision(qtbot) -> None:
    from PySide6.QtWidgets import QComboBox

    from airplay_linux.ui.dialogs import ResolutionDialog

    dialogue = ResolutionDialog((HD,))
    qtbot.addWidget(dialogue)
    dialogue.findChild(QComboBox, "resolutionChoice").setCurrentIndex(0)
    dialogue.reject()
    assert dialogue.decision() is None


def test_orphan_prompt_shows_evidence_and_requires_confirmation(qtbot) -> None:
    from airplay_linux.ui.dialogs import OrphanDialog

    record = ownership(stage="published")
    item = RecoveryItem(kind="orphan", record=record, handle=None, diagnostic=None)
    dialogue = OrphanDialog(item)
    qtbot.addWidget(dialogue)

    assert dialogue.confirmed() is False
    texts = " ".join(label.text() for label in dialogue.findChildren(QLabel))
    assert "Virtual-v1" in texts
    assert "v1" in texts
    assert str(record.owner_uuid) in texts
    assert "not confirmed" in texts.lower() or "unconfirmed" in texts.lower()

    confirm = dialogue.findChild(QPushButton, "confirmOrphanButton")
    keep = dialogue.findChild(QPushButton, "keepOrphanButton")
    assert "Keep" in keep.text()
    assert "Remove" in confirm.text()
    dialogue.reject()
    assert dialogue.confirmed() is False

    kept = OrphanDialog(item)
    qtbot.addWidget(kept)
    kept.findChild(QPushButton, "confirmOrphanButton").click()
    assert kept.confirmed() is True


def test_orphan_prompt_reports_a_missing_helper_instead_of_implying_one(qtbot) -> None:
    from airplay_linux.ui.dialogs import OrphanDialog

    item = RecoveryItem(
        kind="orphan", record=ownership(helper=None), handle=None, diagnostic=None
    )
    dialogue = OrphanDialog(item)
    qtbot.addWidget(dialogue)
    texts = " ".join(label.text() for label in dialogue.findChildren(QLabel))
    assert "helper" in texts.lower()


def test_every_dialog_button_is_text_and_every_icon_control_has_a_tooltip(qtbot) -> None:
    from airplay_linux.domain.records import RecoveryItem
    from airplay_linux.ui.dialogs import (
        ConfirmStopDialog,
        OrphanDialog,
        PairingDialog,
        ResolutionDialog,
    )

    dialogues = (
        PairingDialog(challenge("pin")),
        ConfirmStopDialog(Mode.MIRROR),
        ResolutionDialog((HD,)),
        OrphanDialog(
            RecoveryItem(
                kind="orphan",
                record=ownership(helper=None),
                handle=None,
                diagnostic=None,
            )
        ),
    )
    for dialogue in dialogues:
        qtbot.addWidget(dialogue)
        for button in dialogue.findChildren(QAbstractButton):
            if button.objectName() == "iconButton":
                assert button.toolTip()
            else:
                assert button.text().strip(), button.objectName()
        for field in dialogue.findChildren(QLineEdit):
            assert field.accessibleName()
