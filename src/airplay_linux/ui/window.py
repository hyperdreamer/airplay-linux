"""Dense, accessible PySide6 dashboard for simultaneous receiver sessions.

The window is deliberately *dumb*.  It owns no transport, display, trust or
audio knowledge: it renders a :class:`~airplay_linux.ui.state.PaneState` and a
tuple of :class:`~airplay_linux.ui.state.ReceiverRow` values and reports the
user's intent through Qt signals.  Every enabled control therefore corresponds
to one action the projection actually offered, and every disabled control to one
it did not - the widgets never re-derive a phase or grow an extra command of
their own.

Three presentation rules are load-bearing for the design:

* one segmented mode control per receiver, with a single stacked editor, so
  Mirror and Virtual display are never offered as simultaneous renderers;
* text buttons for the plain commands (``Scan``, ``Start``, ``Stop``, ``Pair``)
  and a tooltip plus accessible name on every icon-only control;
* the destructive-free defaults stay safe: a fresh window offers nothing but
  ``Scan``, and closing is deferred to the presenter until cleanup confirmed.

The presenter owns which receiver is selected and when the window really closes;
:meth:`MainWindow.select_receiver` is silent and :meth:`MainWindow.allow_close`
is the only way to let ``closeEvent`` through.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from types import MappingProxyType
from typing import Final, Mapping

from PySide6.QtCore import QItemSelectionModel, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.records import ActivityEvent
from airplay_linux.domain.resources import Mode, PhysicalMonitorSource, Size
from airplay_linux.ui.models import ReceiverListModel
from airplay_linux.ui.state import (
    SEVERITY_ERROR,
    SEVERITY_OK,
    SEVERITY_WARNING,
    Action,
    Badge,
    PaneState,
    ReceiverRow,
)

#: Commands whose labels are fixed text; ``start`` adopts the projection's label.
_COMMAND_LABELS: Final[Mapping[Action, tuple[str, str]]] = MappingProxyType(
    {
        Action.start: ("Start", "Start the session in the selected mode"),
        Action.stop: (
            "Stop",
            "Stop this session and confirm removal of everything it owns",
        ),
        Action.pair: ("Pair", "Pair with this receiver using a PIN or password"),
        Action.scan: ("Scan", "Search the local network for AirPlay receivers"),
        Action.submit_pairing: (
            "Submit PIN",
            "Submit the PIN or password the receiver is showing",
        ),
        Action.cancel: ("Cancel", "Cancel the current step and release what it holds"),
        Action.remove_metadata: (
            "Remove saved receiver",
            "Forget this receiver's saved metadata and paired credential",
        ),
        Action.retry: ("Retry", "Retry the current session with the same intent"),
        Action.choose_resolution: (
            "Choose resolution",
            "Choose the size of the virtual output before it is created",
        ),
        Action.change_resolution: (
            "Change resolution",
            "Replace the owned output with one at a different size",
        ),
        Action.recreate: (
            "Recreate output",
            "Recreate the owned output at the size the receiver accepted",
        ),
        Action.retry_audio: ("Retry audio", "Retry audio for this session"),
        Action.video_only: (
            "Start video only",
            "Continue with video and no audio for this session",
        ),
        Action.retry_cleanup: (
            "Retry cleanup",
            "Retry removing the owned resource and confirm its disappearance",
        ),
        Action.keep_cleanup: (
            "Keep for manual cleanup",
            "Keep the unconfirmed resource for manual cleanup instead of retrying",
        ),
        Action.retry_revocation: (
            "Retry revocation",
            "Retry cancelling the receiver's paired credential",
        ),
        Action.keep_blocked: (
            "Keep blocked",
            "Keep pairing blocked for this receiver until it is cleaned up",
        ),
        Action.confirm_orphan: (
            "Remove orphan output",
            "Remove the confirmed orphan output after checking its ownership record",
        ),
        Action.keep_orphan: (
            "Keep orphan output",
            "Keep the orphan output; it is never removed automatically",
        ),
    }
)

#: Persistent commands stay visible and simply disable themselves.
_PERSISTENT: Final[frozenset[Action]] = frozenset(
    {Action.start, Action.stop, Action.pair, Action.scan, Action.mute}
)

#: Primary labels the projection uses for the start command.
_START_LABELS: Final[frozenset[str]] = frozenset({"Start", "Create display"})

_BADGE_COLORS: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        SEVERITY_OK: ("#1b5e20", "#e8f5e9"),
        "info": ("#0d47a1", "#e3f2fd"),
        SEVERITY_WARNING: ("#8a5300", "#fff4e0"),
        SEVERITY_ERROR: ("#b00020", "#fde7ea"),
    }
)


class MainWindow(QMainWindow):
    """The operations dashboard: receiver list, selected pane, activity strip."""

    receiver_selected = Signal(str)
    action_requested = Signal(str)
    mode_requested = Signal(str)
    source_selected = Signal(str)
    resolution_selected = Signal(int, int)
    mute_requested = Signal(bool)
    closing_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AirPlay Linux")
        self.resize(980, 620)

        self._model = ReceiverListModel(self)
        self._pane: PaneState | None = None
        self._selected: ReceiverId | None = None
        self._mode_selectable = False
        self._quiet = False
        self._closing_emitted = False
        self._close_allowed = False
        self._buttons: dict[Action, QPushButton] = {}

        central = QWidget(self)
        central.setObjectName("dashboard")
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._build_toolbar()
        splitter = QSplitter(Qt.Orientation.Horizontal, central)
        splitter.setObjectName("dashboardSplitter")
        splitter.addWidget(self._build_receiver_panel(splitter))
        splitter.addWidget(self._build_pane(splitter))
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        layout.addWidget(splitter, 1)
        layout.addWidget(self._build_activity(central))
        self.setCentralWidget(central)

        self._wire()
        self._apply_tab_order()
        self._render_no_selection()

    # --------------------------------------------------------------------- build

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Operations", self)
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self._buttons[Action.scan] = self._button(Action.scan, toolbar)
        self._scan = self._buttons[Action.scan]
        toolbar.addWidget(self._scan)

        self._summary = QLabel("0 TVs available  0 sessions", toolbar)
        self._summary.setObjectName("summaryLabel")
        self._summary.setAccessibleName("Receiver summary")
        toolbar.addWidget(self._summary)

        self._environment = QLabel("Environment unknown", toolbar)
        self._environment.setObjectName("environmentLabel")
        self._environment.setAccessibleName("Environment")
        toolbar.addWidget(self._environment)

        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self._settings = QPushButton("Settings", toolbar)
        self._settings.setObjectName("settingsButton")
        self._settings.setAccessibleName("Settings")
        self._settings.setToolTip("Open settings for this application")
        self._settings.setEnabled(False)  # settings land with the presenter
        toolbar.addWidget(self._settings)

    def _build_receiver_panel(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        panel.setObjectName("receiverPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 6, 0)
        layout.setSpacing(4)

        header = QLabel("TVs", panel)
        header.setObjectName("receiverHeader")
        layout.addWidget(header)

        self._table = QTableView(panel)
        self._table.setObjectName("receiverTable")
        self._table.setAccessibleName("Receivers")
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(False)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header_view.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, 1)

        self._empty = QLabel("No receivers found. Choose Scan to search again.", panel)
        self._empty.setObjectName("emptyStateLabel")
        self._empty.setAccessibleName("No receivers")
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)
        return panel

    def _build_pane(self, parent: QWidget) -> QWidget:
        pane = QWidget(parent)
        pane.setObjectName("selectedPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(6, 0, 0, 0)
        layout.setSpacing(6)

        title_row = QHBoxLayout()
        self._title = QLabel("No receiver selected", pane)
        self._title.setObjectName("paneTitle")
        font = self._title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 2)
        self._title.setFont(font)
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        self._phase = QLabel("", pane)
        self._phase.setObjectName("phaseLabel")
        title_row.addWidget(self._phase)
        layout.addLayout(title_row)

        self._badges_row = QWidget(pane)
        self._badges_row.setObjectName("badgesRow")
        self._badges_layout = QHBoxLayout(self._badges_row)
        self._badges_layout.setContentsMargins(0, 0, 0, 0)
        self._badges_layout.setSpacing(4)
        self._badges_layout.addStretch(1)
        layout.addWidget(self._badges_row)

        self._mode_selector = QWidget(pane)
        self._mode_selector.setObjectName("modeSelector")
        mode_row = QHBoxLayout(self._mode_selector)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(0)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mirror = QPushButton("Mirror", self._mode_selector)
        self._mirror.setObjectName("modeMirror")
        self._mirror.setToolTip("Mirror a physical monitor to this receiver")
        self._mirror.setAccessibleName("Mirror mode")
        self._mirror.setCheckable(True)
        self._mirror.setChecked(True)
        self._virtual = QPushButton("Virtual display", self._mode_selector)
        self._virtual.setObjectName("modeVirtual")
        self._virtual.setToolTip(
            "Create a virtual output owned by this session at a chosen size"
        )
        self._virtual.setAccessibleName("Virtual display mode")
        self._virtual.setCheckable(True)
        for button in (self._mirror, self._virtual):
            self._mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        layout.addWidget(self._mode_selector)

        self._notice = QLabel("", pane)
        self._notice.setObjectName("noticeLabel")
        self._notice.setAccessibleName("Notice")
        self._notice.setWordWrap(True)
        layout.addWidget(self._notice)

        self._error = QLabel("", pane)
        self._error.setObjectName("errorLabel")
        self._error.setAccessibleName("Error detail")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #b00020;")
        self._error.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._error)

        self._editor_stack = QStackedWidget(pane)
        self._editor_stack.setObjectName("editorStack")
        self._source_page = QWidget(self._editor_stack)
        source_row = QHBoxLayout(self._source_page)
        source_row.setContentsMargins(0, 0, 0, 0)
        source_row.addWidget(QLabel("Physical monitor:", self._source_page))
        self._source = QComboBox(self._source_page)
        self._source.setObjectName("sourceCombo")
        self._source.setAccessibleName("Physical monitor source")
        self._source.setToolTip(
            "Choose the physical monitor Mirrored for this receiver; application "
            "windows and virtual outputs are never captured"
        )
        source_row.addWidget(self._source, 1)
        self._resolution_page = QWidget(self._editor_stack)
        resolution_row = QHBoxLayout(self._resolution_page)
        resolution_row.setContentsMargins(0, 0, 0, 0)
        resolution_row.addWidget(QLabel("Output size:", self._resolution_page))
        self._resolution = QComboBox(self._resolution_page)
        self._resolution.setObjectName("resolutionCombo")
        self._resolution.setAccessibleName("Output resolution")
        self._resolution.setToolTip(
            "Choose the virtual output size; the receiver's own reliable size is "
            "preferred and never guessed"
        )
        resolution_row.addWidget(self._resolution, 1)
        self._editor_stack.addWidget(self._source_page)
        self._editor_stack.addWidget(self._resolution_page)
        layout.addWidget(self._editor_stack)

        primary_row = QHBoxLayout()
        for action in (Action.start, Action.stop, Action.pair):
            self._buttons[action] = self._button(action, pane)
            primary_row.addWidget(self._buttons[action])
        self._mute = QToolButton(pane)
        self._mute.setObjectName("muteToggle")
        self._mute.setCheckable(True)
        self._mute.setAutoRaise(True)
        self._mute.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self._mute.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self._set_mute_affordance(muted=False)
        primary_row.addWidget(self._mute)
        primary_row.addStretch(1)
        layout.addLayout(primary_row)

        actions = QGridLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setHorizontalSpacing(4)
        actions.setVerticalSpacing(4)
        contextual = [
            action for action in _COMMAND_LABELS if action not in _PERSISTENT
        ]
        for position, action in enumerate(contextual):
            button = self._button(action, pane)
            self._buttons[action] = button
            actions.addWidget(button, position // 3, position % 3)
        layout.addLayout(actions)
        layout.addStretch(1)
        return pane

    def _build_activity(self, parent: QWidget) -> QWidget:
        panel = QWidget(parent)
        panel.setObjectName("activityPanel")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        label = QLabel("Activity:", panel)
        label.setObjectName("activityLabel")
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)
        self._activity = QPlainTextEdit(panel)
        self._activity.setObjectName("activityView")
        self._activity.setAccessibleName("Activity log")
        self._activity.setReadOnly(True)
        self._activity.setMaximumHeight(96)
        self._activity.setPlaceholderText("No activity yet")
        layout.addWidget(self._activity, 1)
        return panel

    def _button(self, action: Action, parent: QWidget) -> QPushButton:
        label, tooltip = _COMMAND_LABELS[action]
        button = QPushButton(label, parent)
        button.setObjectName(_BUTTON_NAMES[action])
        button.setText(label)
        button.setToolTip(tooltip)
        button.setAccessibleName(label)
        return button

    # ---------------------------------------------------------------------- wire

    def _wire(self) -> None:
        for action, button in self._buttons.items():
            button.clicked.connect(
                lambda _checked=False, requested=action: self.action_requested.emit(
                    requested.value
                )
            )
        self._mirror.clicked.connect(lambda: self._request_mode(Mode.MIRROR))
        self._virtual.clicked.connect(lambda: self._request_mode(Mode.VIRTUAL))
        self._mute.toggled.connect(self._request_mute)
        self._source.currentIndexChanged.connect(self._source_changed)
        self._resolution.currentIndexChanged.connect(self._resolution_changed)
        self._table.selectionModel().currentRowChanged.connect(self._row_changed)

    def _apply_tab_order(self) -> None:
        order = [self._scan, self._settings, self._table]
        order += [self._mirror, self._virtual]
        order += [self._source, self._resolution]
        order += [self._mute]
        order += [self._buttons[action] for action in _COMMAND_LABELS if action in self._buttons]
        for first, second in zip(order, order[1:], strict=False):
            QWidget.setTabOrder(first, second)

    # ------------------------------------------------------------- render: inputs

    def render_receivers(self, rows: tuple[ReceiverRow, ...]) -> None:
        """Render the device list exactly as the projection produced it."""
        self._model.replace(rows)
        self._empty.setVisible(not self._model.rows)
        self._restore_selection()

    def render_selected(self, pane: PaneState) -> None:
        """Render the selected receiver's pane; nothing here is recomputed."""
        if not isinstance(pane, PaneState):
            raise TypeError("render_selected requires a PaneState")
        self._pane = pane
        self._mode_selectable = pane.mode_enabled
        self._title.setText(pane.title)
        self._phase.setText(pane.phase_label)
        self._render_badges(pane.badges)
        self._render_mode(pane)
        self._render_notice(pane)
        self._render_error(pane)
        self._render_editor(pane)
        self._render_mute(pane)
        self._render_actions(pane)

    def set_activity(self, events: tuple[ActivityEvent, ...]) -> None:
        """Replace the activity strip with the diagnostics layer's safe events."""
        if isinstance(events, (str, bytes)) or not isinstance(events, Iterable):
            raise TypeError("set_activity requires a sequence of ActivityEvent values")
        lines: list[str] = []
        for event in events:
            if not isinstance(event, ActivityEvent):
                raise TypeError("set_activity requires ActivityEvent values")
            lines.append(f"{event.utc}  {event.message}")
        self._activity.setPlainText("\n".join(lines))

    def set_summary(self, available: int, sessions: int, environment: str) -> None:
        """Render the toolbar's available/session counts and environment name."""
        for name, value in (("available", available), ("sessions", sessions)):
            if type(value) is not int or value < 0:
                raise TypeError(f"{name} must be a nonnegative integer")
        if not isinstance(environment, str):
            raise TypeError("environment must be text")
        tv = "TV" if available == 1 else "TVs"
        session = "session" if sessions == 1 else "sessions"
        self._summary.setText(f"{available} {tv} available  {sessions} {session}")
        self._environment.setText(environment)

    def set_sources(self, sources: tuple[PhysicalMonitorSource, ...]) -> None:
        """Populate the physical-monitor editor; the presenter owns the choices."""
        for source in sources:
            if not isinstance(source, PhysicalMonitorSource):
                raise TypeError("sources must contain PhysicalMonitorSource values")
        with QSignalBlocker(self._source):
            self._source.clear()
            for source in sources:
                self._source.addItem(source.connector, source.connector)
        self._source.setPlaceholderText("No physical monitor reported")

    def set_resolutions(self, choices: tuple[Size, ...]) -> None:
        """Populate the output-size editor; the presenter owns the choices."""
        for size in choices:
            if not isinstance(size, Size):
                raise TypeError("choices must contain Size values")
        with QSignalBlocker(self._resolution):
            self._resolution.clear()
            for size in choices:
                self._resolution.addItem(f"{size.width}x{size.height}", size)
        self._resolution.setPlaceholderText("No reliable size reported")

    def select_receiver(self, receiver_id: ReceiverId) -> None:
        """Highlight the presenter's selected receiver without reporting it back."""
        self._selected = receiver_id
        index = self._model.index_of(receiver_id)
        if not index.isValid():
            return
        with self._quiet_selection():
            self._table.setCurrentIndex(index)
            self._table.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )

    # ----------------------------------------------------------------- close path

    def allow_close(self) -> None:
        """Let the next ``closeEvent`` through, after the presenter's cleanup."""
        self._close_allowed = True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        if self._close_allowed:
            event.accept()
            return
        event.ignore()
        if not self._closing_emitted:
            self._closing_emitted = True
            self.closing_requested.emit()

    # ------------------------------------------------------------------- render

    def _render_no_selection(self) -> None:
        self._mode_selectable = False
        self._mirror.setEnabled(False)
        self._virtual.setEnabled(False)
        self._source.setEnabled(False)
        self._resolution.setEnabled(False)
        self._empty.setVisible(True)
        self._render_badges(())
        self._notice.clear()
        self._notice.setVisible(False)
        self._error.clear()
        self._error.setVisible(False)
        self._mute.setEnabled(False)
        with QSignalBlocker(self._mute):
            self._mute.setChecked(False)
        self._set_mute_affordance(muted=False)
        for action, button in self._buttons.items():
            button.setEnabled(action is Action.scan)
            button.setVisible(action is not Action.mute and action in _PERSISTENT)

    def _render_badges(self, badges: tuple[Badge, ...]) -> None:
        while self._badges_layout.count():
            item = self._badges_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for badge in badges:
            label = QLabel(badge.label, self._badges_row)
            label.setObjectName(f"badge_{badge.resource}")
            label.setAccessibleName(f"{badge.resource} badge")
            label.setToolTip(f"{badge.resource} status: {badge.label}")
            foreground, background = _BADGE_COLORS.get(
                badge.severity, _BADGE_COLORS[SEVERITY_WARNING]
            )
            label.setStyleSheet(
                f"color: {foreground}; background: {background}; border-radius: 6px;"
                " padding: 2px 8px;"
            )
            self._badges_layout.addWidget(label)
        self._badges_layout.addStretch(1)

    def _render_mode(self, pane: PaneState) -> None:
        target = self._mirror if pane.visible_mode is Mode.MIRROR else self._virtual
        with QSignalBlocker(self._mirror), QSignalBlocker(self._virtual):
            target.setChecked(True)
            (self._virtual if target is self._mirror else self._mirror).setChecked(False)
        self._mirror.setEnabled(pane.mode_enabled)
        self._virtual.setEnabled(pane.mode_enabled)

    def _render_notice(self, pane: PaneState) -> None:
        if pane.notice:
            self._notice.setText(pane.notice)
            self._notice.setVisible(True)
        else:
            self._notice.clear()
            self._notice.setVisible(False)

    def _render_error(self, pane: PaneState) -> None:
        if pane.diagnostic is not None:
            self._error.setText(pane.diagnostic.message)
            self._error.setVisible(True)
        else:
            self._error.clear()
            self._error.setVisible(False)

    def _render_editor(self, pane: PaneState) -> None:
        mirror = pane.visible_mode is Mode.MIRROR
        self._editor_stack.setCurrentWidget(
            self._source_page if mirror else self._resolution_page
        )
        self._source.setEnabled(pane.source_enabled)
        self._resolution.setEnabled(pane.resolution_enabled)

    def _render_mute(self, pane: PaneState) -> None:
        with QSignalBlocker(self._mute):
            self._mute.setChecked(pane.muted)
        self._mute.setEnabled(pane.mute_enabled)
        self._set_mute_affordance(muted=pane.muted)

    def _render_actions(self, pane: PaneState) -> None:
        start_label = (
            pane.primary_label
            if Action.start in pane.actions and pane.primary_label in _START_LABELS
            else _COMMAND_LABELS[Action.start][0]
        )
        self._buttons[Action.start].setText(start_label)
        for action, button in self._buttons.items():
            offered = action in pane.actions
            button.setEnabled(offered)
            # Persistent commands stay visible so their disabled state is legible;
            # context-only prompts appear only when the projection offers them.
            if action in _PERSISTENT:
                button.setVisible(True)
            else:
                button.setVisible(offered)

    # ------------------------------------------------------------------- signals

    def _request_mode(self, mode: Mode) -> None:
        if not self._mode_selectable:
            return
        if self._pane is not None and mode is self._pane.visible_mode:
            return
        self.mode_requested.emit(mode.value)

    def _request_mute(self, muted: bool) -> None:
        self.mute_requested.emit(bool(muted))

    def _source_changed(self, index: int) -> None:
        if index < 0:
            return
        connector = self._source.itemData(index)
        if isinstance(connector, str) and connector:
            self.source_selected.emit(connector)

    def _resolution_changed(self, index: int) -> None:
        if index < 0:
            return
        chosen = self._resolution.itemData(index)
        if isinstance(chosen, Size):
            self.resolution_selected.emit(chosen.width, chosen.height)

    def _row_changed(self, current: object, _previous: object) -> None:
        if self._quiet:
            return
        receiver_id = self._model.receiver_id(current)
        if receiver_id is None or receiver_id == self._selected:
            return
        self._selected = receiver_id
        self.receiver_selected.emit(str(receiver_id))

    # -------------------------------------------------------------------- helpers

    def _set_mute_affordance(self, *, muted: bool) -> None:
        if muted:
            name = "Unmute receiver audio"
            tooltip = (
                "Audio is muted for this receiver. Activate to unmute; muting is "
                "receiver-local and lasts for this session only."
            )
            pixmap = QStyle.StandardPixmap.SP_MediaVolumeMuted
        else:
            name = "Mute receiver audio"
            tooltip = (
                "Mute this receiver's audio for this session only; the receiver keeps "
                "its own volume setting."
            )
            pixmap = QStyle.StandardPixmap.SP_MediaVolume
        self._mute.setAccessibleName(name)
        self._mute.setToolTip(tooltip)
        icon: QIcon = self.style().standardIcon(pixmap)
        if not icon.isNull():
            self._mute.setIcon(icon)

    def _restore_selection(self) -> None:
        if self._selected is None:
            return
        index = self._model.index_of(self._selected)
        if not index.isValid():
            return
        with self._quiet_selection():
            self._table.setCurrentIndex(index)
            self._table.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.ClearAndSelect
                | QItemSelectionModel.SelectionFlag.Rows,
            )

    @contextmanager
    def _quiet_selection(self):
        previous = self._quiet
        self._quiet = True
        try:
            yield
        finally:
            self._quiet = previous


_BUTTON_NAMES: Final[Mapping[Action, str]] = MappingProxyType(
    {
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
)

__all__ = ["MainWindow"]
