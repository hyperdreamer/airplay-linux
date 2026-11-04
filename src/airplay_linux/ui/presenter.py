"""Bind dashboard intent to fenced domain commands, and render the projection.

The presenter is the only bridge between the Qt widgets and the domain.  Widgets
never call a port: they emit a signal, the presenter decides whether the *current
projection* still offers that action, and - if it does - dispatches one fresh
typed command through :class:`~airplay_linux.domain.ports.ManagerPort`.  The
window is never asked to compute a phase or an action set; it renders what
:mod:`airplay_linux.ui.state` decided.

Four rules shape every method here.

*Only commands cross.*  A user action becomes one :class:`Command` object built
at click time.  The session manager mints the ``OperationContext`` (a fresh
operation UUID per command) and fences its resources; the presenter never
fabricates a context, never reuses a command, and never reaches into the concrete
session implementation.  Commands the frozen domain does not define are not
invented: ``Keep`` on a blocked cleanup or a blocked trust barrier is an explicit
acknowledgement of an already-blocked state, so it dispatches nothing and leaves
every badge in place.

*Results are fenced.*  Every asynchronous completion is checked against the
intent that asked for it before it may touch state: a dispatch receipt against
the receiver's session generation and operation id, a pairing answer against the
captured ``ChallengeView`` (receiver, challenge, endpoint and trust generations),
a confirmed mode switch against the captured session generation and endpoint, a
resolution answer against the endpoint fingerprint, an orphan removal against the
exact owner UUID, a host source list or ownership load against a selected-receiver
request token, and stored metadata against the epoch its read started in.  A
stale result is dropped, never applied over newer state.  Durable ownership
evidence is reconciled on every snapshot too: once a receiver's own session
confirms the release of an owner it had claimed, the cached record is retired,
so a completed release is never re-offered as unconfirmed cleanup.  That
reconciliation is subject-scoped - a record this receiver never claimed (the
orphan the recovery controls exist for) and another receiver's record are never
touched.

*The UI never blocks.*  Dispatch, discovery refresh, prompt answers and store
reads are all scheduled on the running loop; ``start`` and ``close`` return
without waiting for I/O.  Closing starts exactly one shutdown future, keeps the
dashboard visible while it runs (so bounded progress stays legible), and only
lets the window go when the composition root confirms cleanup - a failed
shutdown keeps the window open with retryable cleanup exposed.

*Secrets stay local.*  A PIN or password exists only as a parameter of one
scheduled prompt task and as the ``SensitiveText`` handed to one
``SubmitPairing`` command.  It is never stored on the presenter, never passed to
:class:`~airplay_linux.diagnostics.ActivityLog`, never rendered, never put in a
diagnostic, and never formatted into an exception or log line.  Every diagnostic
the presenter *does* render is rebuilt from the diagnostics catalog, so a
receipt carrying adapter text cannot reach the pane.

The pairing prompt is opened through :class:`DialogFactory` (the concrete
:class:`QtDialogFactory` uses the nonmodal task-25 dialogs).  The presenter never
re-parents that prompt into the dashboard: the pairing window adopts itself with
``track_prompt`` and stays outside the dashboard's window hierarchy, which keeps
the dashboard - and its ``Retry`` action - usable while a PIN is being typed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Final, Protocol

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QWidget

from airplay_linux.diagnostics import diagnostic
from airplay_linux.diagnostics.activity import safe_activity
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import (
    Cancel,
    ChangeResolution,
    ChooseResolution,
    Command,
    ConfirmOrphanRemoval,
    ConfirmSwitch,
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
from airplay_linux.domain.operations import ChallengeView, SensitiveText
from airplay_linux.domain.records import (
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
    Mode,
    Ok,
    PhysicalMonitorSource,
    ResolutionDecision,
    ResolutionEvidence,
    Result,
    SessionPhase,
    Size,
    StartRequest,
    TransportState,
    TrustState,
)
from airplay_linux.ui.dialogs import (
    ConfirmStopDialog,
    OrphanDialog,
    PairingAnswer,
    PairingDialog,
    ResolutionDialog,
)
from airplay_linux.ui.state import Action, PaneState, ViewInputs, project, project_row
from airplay_linux.ui.window import MainWindow

if TYPE_CHECKING:
    from airplay_linux.domain.ports import (
        CapturePort,
        DiscoveryPort,
        ManagerPort,
        OwnershipStore,
        ReceiverStore,
        Subscription,
    )

__all__ = ["DialogFactory", "Presenter", "QtDialogFactory"]


class DialogFactory(Protocol):
    """The prompt seam the composition root supplies to the presenter.

    Each method returns an awaitable that resolves with the user's single answer.
    Implementations must not block the event loop (the Qt factory uses
    ``QDialog.open`` plus a ``finished`` continuation) and must resolve with the
    safe default - ``None``/``False`` - when the prompt is dismissed, destroyed or
    invalidated.
    """

    async def pairing(
        self,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action],
        retained_output: str | None,
    ) -> PairingAnswer | None: ...

    def invalidate_pairing(self, challenge: ChallengeView) -> None: ...

    async def confirm_stop(self, mode: Mode) -> bool: ...

    async def resolution(self, choices: tuple[Size, ...]) -> Size | None: ...

    async def orphan(self, item: RecoveryItem) -> bool: ...


#: Transport states that still belong to a live session.
_ACTIVE_TRANSPORT: Final[tuple[TransportState, ...]] = (
    TransportState.Pairing,
    TransportState.Connecting,
    TransportState.Streaming,
    TransportState.Reconnecting,
    TransportState.Stopping,
)

#: Trust states in which pairing and new starts are blocked until cleanup.
_UNCERTAIN_TRUST: Final[tuple[TrustState, ...]] = (
    TrustState.RevocationPending,
    TrustState.CleanupRequired,
)

#: Phases in which a confirmed mode switch must not be queued.
_SWITCH_BLOCKED_PHASES: Final[frozenset[SessionPhase]] = frozenset(
    {
        SessionPhase.Stopping,
        SessionPhase.CleanupRequired,
        SessionPhase.TrustCleanupRequired,
        SessionPhase.Reconfiguring,
        SessionPhase.CreatingDisplay,
        SessionPhase.ResolutionRequired,
    }
)

#: Reservation stages, ordered; a higher stage is stronger ownership evidence.
_STAGE_ORDER: Final[dict[str, int]] = {
    "reserved": 0,
    "helper_started": 1,
    "output_observed": 2,
    "published": 3,
    "removing": 4,
}

#: The only diagnostic the presenter may show when it has no catalog code of its
#: own.  The catalog has no "internal error" code and blaming a backend would be
#: wrong: this copy says a completion was ignored, which is exactly what happened.
_UNEXPECTED: Final[ErrorCode] = ErrorCode.stale_event

_WINDOW_TITLE: Final[str] = "AirPlay Linux"
_CLOSING_TITLE: Final[str] = "AirPlay Linux - closing; confirming cleanup"

_DISABLED_REPORT: Final[CapabilityReport] = CapabilityReport(
    environment="Unknown",
    discovery=False,
    mirror=False,
    virtual_display=False,
    audio=False,
    remembered_pairing=False,
    additional_virtual_outputs=False,
    diagnostics=(),
)

_COMMAND_FACTORIES: Final[dict[Action, Any]] = {
    Action.stop: Stop,
    Action.cancel: Cancel,
    Action.retry: Retry,
    Action.retry_audio: RetryAudio,
    Action.video_only: StartVideoOnly,
    Action.recreate: RecreateAccepted,
    Action.retry_cleanup: RetryCleanup,
    Action.retry_revocation: RetryRevocation,
    Action.keep_cleanup: KeepCleanup,
}


class _Bridge(QObject):
    """Queued-delivery seam: port callbacks only ever run on the GUI thread.

    The three port subscriptions may be invoked from an actor thread.  Emitting a
    signal whose receiver is this QObject (created on the GUI thread) with an
    explicit queued connection moves the payload onto the GUI thread before any
    presenter state is touched; the render signal coalesces bursts of changes.
    """

    snapshot = Signal(object)
    discovery = Signal(object)
    activity = Signal(object)
    render = Signal()

    def __init__(
        self,
        on_snapshot: Callable[[Any], None],
        on_discovery: Callable[[Any], None],
        on_activity: Callable[[Any], None],
        on_render: Callable[[], None],
    ) -> None:
        super().__init__()
        self._on_snapshot = on_snapshot
        self._on_discovery = on_discovery
        self._on_activity = on_activity
        self._on_render = on_render
        self.snapshot.connect(self._snapshot, Qt.ConnectionType.QueuedConnection)
        self.discovery.connect(self._discovery, Qt.ConnectionType.QueuedConnection)
        self.activity.connect(self._activity, Qt.ConnectionType.QueuedConnection)
        self.render.connect(self._render, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _snapshot(self, value: object) -> None:
        self._on_snapshot(value)

    @Slot(object)
    def _discovery(self, value: object) -> None:
        self._on_discovery(value)

    @Slot(object)
    def _activity(self, value: object) -> None:
        self._on_activity(value)

    @Slot()
    def _render(self) -> None:
        self._on_render()


class Presenter:
    """Bind one :class:`MainWindow` to the domain ports it may command."""

    def __init__(
        self,
        window: MainWindow,
        manager: ManagerPort,
        discovery: DiscoveryPort,
        capture: CapturePort,
        receivers: ReceiverStore,
        capabilities: Callable[[], CapabilityReport],
        activity: Any,
        ownerships: OwnershipStore,
        dialogs: DialogFactory,
        request_shutdown: Callable[[], Awaitable[Result[None]]],
        *,
        refresh_capabilities: Callable[[], Awaitable[Any]] | None = None,
        screencast: Any | None = None,
    ) -> None:
        if not isinstance(window, MainWindow):
            raise TypeError("window must be a MainWindow")
        for name, value in (
            ("capabilities", capabilities),
            ("request_shutdown", request_shutdown),
        ):
            if not callable(value):
                raise TypeError(f"{name} must be callable")
        for name in (
            "pairing", "invalidate_pairing", "confirm_stop", "resolution", "orphan",
        ):
            if not callable(getattr(dialogs, name, None)):
                raise TypeError(f"dialogs.{name} must be callable")
        self._window = window
        self._manager = manager
        self._discovery = discovery
        self._capture = capture
        self._receivers = receivers
        self._capabilities = capabilities
        self._activity = activity
        self._ownerships = ownerships
        self._dialogs = dialogs
        self._request_shutdown = request_shutdown
        self._refresh_capabilities_report = refresh_capabilities
        # Desktop state, not a session command: this only reads and clears the
        # portal's remembered screen approvals.  A composition that supplies no
        # permission store fails the action instead of pretending it is absent.
        self._screencast = screencast

        # -- authoritative caches (GUI-thread owned) -----------------------
        self._views: dict[ReceiverId, Any] = {}
        self._snapshots: dict[ReceiverId, SessionSnapshot] = {}
        self._saved: dict[ReceiverId, SavedReceiver] = {}
        self._records: dict[Any, OwnershipRecord] = {}
        # Ownership evidence this dashboard has seen a session claim, and the
        # records it has seen that same session confirm released.  Both are
        # per receiver: a receiver's snapshot may only ever retire its own
        # evidence, never a durable record another receiver still owns.
        self._claimed_owners: dict[ReceiverId, set[Any]] = {}
        self._released_owners: dict[ReceiverId, set[Any]] = {}
        self._sources: tuple[PhysicalMonitorSource, ...] = ()
        self._selected_source: dict[ReceiverId, PhysicalMonitorSource] = {}
        self._selected_resolution: dict[ReceiverId, Size] = {}
        self._idle_modes: dict[ReceiverId, Mode] = {}
        self._rejections: dict[ReceiverId, Diagnostic] = {}
        self._selected: ReceiverId | None = None
        self._selection_auto = True
        self._last_report: CapabilityReport | None = None

        # -- request tokens ------------------------------------------------
        self._saved_loading = False
        self._saved_loaded = False
        self._ownership_epoch = 0
        self._ownerships_loaded = False
        self._ownerships_loading = 0
        self._source_token = 0
        self._sources_loaded_for: ReceiverId | None = None
        self._sources_loading_for: ReceiverId | None = None

        # -- prompt tracking (no secret is ever stored) --------------------
        self._pairing_pending: dict[ReceiverId, ChallengeView] = {}
        self._pairing_seen: dict[ReceiverId, ChallengeView] = {}
        self._switch_pending: set[ReceiverId] = set()
        self._switch_queued: dict[ReceiverId, tuple[int, Mode]] = {}
        self._resolution_pending: set[ReceiverId] = set()
        self._orphan_pending: set[ReceiverId] = set()

        # -- lifecycle -----------------------------------------------------
        self._started = False
        self._shutting_down = False
        self._disposed = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._task_fences: dict[
            asyncio.Task[Any], tuple[Any, tuple[Any, ...] | None]
        ] = {}
        self._subscriptions: list[Subscription] = []
        self._render_pending = False
        self._bridge = _Bridge(
            self._snapshot_arrived,
            self._discovery_arrived,
            self._activity_arrived,
            self._render_requested,
        )

    def __repr__(self) -> str:
        return (
            f"Presenter(receivers={len(self._snapshots)}, "
            f"selected={self._selected!r}, started={self._started})"
        )

    # ------------------------------------------------------------------ public

    async def start(self) -> None:
        """Subscribe, render immediately, and read stored metadata in the background.

        Nothing here waits for discovery, the keyring-backed receiver store or the
        ownership provider: the dashboard appears at once and each completion
        re-renders when it lands.
        """
        if self._started:
            return
        self._started = True
        self._window.receiver_selected.connect(self._on_receiver_selected)
        self._window.action_requested.connect(self._on_action_requested)
        self._window.mode_requested.connect(self._on_mode_requested)
        self._window.source_selected.connect(self._on_source_selected)
        self._window.resolution_selected.connect(self._on_resolution_selected)
        self._window.mute_requested.connect(self._on_mute_requested)
        self._window.closing_requested.connect(self._on_closing_requested)
        self._subscribe(self._manager.subscribe, self._bridge.snapshot.emit)
        self._subscribe(self._discovery.subscribe, self._bridge.discovery.emit)
        self._subscribe(self._activity.subscribe, self._bridge.activity.emit)
        self._bridge.render.emit()
        self._schedule(self._refresh_discovery())
        self._start_saved_load()

    async def close(self) -> None:
        """Begin (or join) the single shutdown future; never raise on a port."""
        task = self._begin_shutdown()
        if task is None:
            return
        await asyncio.shield(task)

    # ----------------------------------------------------------- render inputs

    def _subscribe(
        self, subscribe: Callable[[Any], Subscription], emit: Callable[[Any], None],
    ) -> None:
        self._subscriptions.append(subscribe(emit))

    def _report(self) -> CapabilityReport:
        """Frozen capability evidence, cached if the probe itself fails."""
        try:
            report = self._capabilities()
        except Exception:  # noqa: BLE001 - never surface probe text in the UI
            report = None
        if not isinstance(report, CapabilityReport):
            return self._last_report or _DISABLED_REPORT
        self._last_report = report
        return report

    def _snapshot(self, receiver_id: ReceiverId) -> SessionSnapshot:
        known = self._snapshots.get(receiver_id)
        if known is not None:
            return known
        try:
            snapshot = self._manager.snapshot(receiver_id)
        except Exception:  # noqa: BLE001 - an unreadable snapshot is not a licence
            snapshot = None
        if not isinstance(snapshot, SessionSnapshot):
            return SessionSnapshot.idle(receiver_id)
        self._snapshots[receiver_id] = snapshot
        return snapshot

    def _view(self, receiver_id: ReceiverId) -> Any:
        """The current discovery view, including results found before ``start``."""
        view = self._views.get(receiver_id)
        if view is not None:
            return view
        try:
            view = self._discovery.current(receiver_id)
        except Exception:  # noqa: BLE001 - a discovery read is advisory
            view = None
        if view is not None:
            self._views[receiver_id] = view
        return view

    def _idle_mode(self, receiver_id: ReceiverId) -> Mode:
        return self._idle_modes.get(receiver_id, Mode.MIRROR)

    def _sources_for(self, receiver_id: ReceiverId) -> tuple[PhysicalMonitorSource, ...]:
        """Only the host list loaded for this receiver may be offered to it."""
        if self._sources_loaded_for != receiver_id:
            return ()
        return self._sources

    def _source_for(self, receiver_id: ReceiverId) -> PhysicalMonitorSource | None:
        available = self._sources_for(receiver_id)
        chosen = self._selected_source.get(receiver_id)
        if chosen is not None and chosen in available:
            return chosen
        # The combo renders ``available[0]`` when nothing was chosen yet, so the
        # request uses exactly the monitor the user can see selected.
        return available[0] if available else None

    def _records_for(self, receiver_id: ReceiverId) -> tuple[OwnershipRecord, ...]:
        records = [
            record for record in self._records.values()
            if record.receiver_id == receiver_id
        ]
        return tuple(sorted(records, key=_record_order))

    def _ownership_for(self, receiver_id: ReceiverId) -> OwnershipRecord | None:
        records = self._records_for(receiver_id)
        return records[0] if records else None

    def _reconcile_ownership(self, snapshot: SessionSnapshot) -> None:
        """Retire cached ownership evidence the session has confirmed released.

        The durable store is the provider's authority, but the pane also caches
        its last answer.  Once the receiver's own session has claimed an owner
        and then published the *completed* release, that cached copy is stale:
        keeping it would offer Retry / Keep / Remove for a resource that no
        longer exists while the phase already says the receiver is Available.

        Only evidence this receiver's session actually claimed is retired, so an
        unreflected durable record - the orphan the recovery controls exist for -
        is left exactly as the store reported it.  Nothing here ever inspects or
        drops another receiver's record: a snapshot applies only to its subject.
        """
        receiver_id = snapshot.receiver_id
        claimed = self._claimed_owners.setdefault(receiver_id, set())
        claimed.update(_named_owners(snapshot))
        if not claimed or not _cleanup_confirmed(snapshot):
            return
        released = self._released_owners.setdefault(receiver_id, set())
        for owner_uuid in tuple(claimed):
            claimed.discard(owner_uuid)
            released.add(owner_uuid)
            record = self._records.get(owner_uuid)
            if record is not None and record.receiver_id == receiver_id:
                del self._records[owner_uuid]

    def _is_released(self, record: OwnershipRecord) -> bool:
        """Whether this record's own receiver already confirmed its release."""
        return record.owner_uuid in self._released_owners.get(record.receiver_id, ())

    def _orphans_for(self, receiver_id: ReceiverId) -> tuple[RecoveryItem, ...]:
        """Durable records the session snapshot does not reflect.

        Only an *unreflected* record is an orphan candidate: a record the snapshot
        still names is a retained resource the session owns, and it must never be
        offered for orphan removal.  The manager remains the only remover; the
        presenter just supplies the dialog evidence.
        """
        snapshot = self._snapshot(receiver_id)
        reflected = {
            value for value in (
                snapshot.ownership_uuid,
                snapshot.display_handle.owner_uuid
                if snapshot.display_handle is not None else None,
            ) if value is not None
        }
        items: list[RecoveryItem] = []
        for record in self._records_for(receiver_id):
            if record.owner_uuid in reflected:
                continue
            removing = record.desired_state == "removing" or record.stage == "removing"
            items.append(
                RecoveryItem(
                    kind="removing" if removing else "orphan",
                    record=record,
                    handle=None,
                    diagnostic=None,
                )
            )
        return tuple(items)

    def _resolution_choices(self, receiver_id: ReceiverId) -> tuple[Size, ...]:
        """Known sizes for this receiver: never a guessed or generic default."""
        snapshot = self._snapshot(receiver_id)
        saved = self._saved.get(receiver_id)
        candidates: list[Size | None] = [
            snapshot.accepted_resolution,
            snapshot.observed_resolution,
            snapshot.resolution.size if snapshot.resolution is not None else None,
            snapshot.display_handle.source.size
            if snapshot.display_handle is not None else None,
            saved.remembered_resolution.size
            if saved is not None and saved.remembered_resolution is not None else None,
        ]
        choices: list[Size] = []
        for size in candidates:
            if isinstance(size, Size) and size not in choices:
                choices.append(size)
        return tuple(choices)

    def _selected_size(self, receiver_id: ReceiverId) -> Size | None:
        """The size the editor shows: the user's pick, else the first known one."""
        choices = self._resolution_choices(receiver_id)
        chosen = self._selected_resolution.get(receiver_id)
        if chosen is not None and chosen in choices:
            return chosen
        return choices[0] if choices else None

    def _decision_for(self, receiver_id: ReceiverId, size: Size) -> ResolutionDecision | None:
        """Stamp the receiver's *current* endpoint onto an explicit decision."""
        snapshot = self._snapshot(receiver_id)
        target = snapshot.target if snapshot.online else None
        if target is None:
            return None
        explicit = self._selected_resolution.get(receiver_id) == size
        saved = self._saved.get(receiver_id)
        remembered = (
            saved is not None
            and saved.remembered_resolution is not None
            and saved.remembered_resolution.size == size
        )
        source = "user" if explicit else "stored" if remembered else "discovery"
        origin = "picker" if explicit else "remembered" if remembered else "verified"
        return ResolutionDecision(
            size=size,
            source=source,
            evidence=ResolutionEvidence(
                origin=origin,
                reliable=True,
                receiver_id=receiver_id,
                endpoint_fingerprint=target.endpoint_fingerprint,
            ),
        )

    def _start_request(self, receiver_id: ReceiverId, mode: Mode) -> StartRequest | None:
        """Build the typed start this receiver's current state can accept."""
        snapshot = self._snapshot(receiver_id)
        audio = snapshot.audio_policy if snapshot.intent_active else AudioPolicy()
        if mode is Mode.MIRROR:
            # Mirror carries no requested source: the backend opens the capture
            # session and the desktop decides which screen it gets.
            return StartRequest(mode=Mode.MIRROR, source=None, audio=audio)
        size = self._selected_size(receiver_id)
        decision = self._decision_for(receiver_id, size) if size is not None else None
        return StartRequest(mode=Mode.VIRTUAL, resolution=decision, audio=audio)

    def _inputs(self, receiver_id: ReceiverId, report: CapabilityReport) -> ViewInputs:
        return ViewInputs(
            receiver=self._view(receiver_id),
            saved=self._saved.get(receiver_id),
            capabilities=report,
            selected_mode=self._idle_mode(receiver_id),
            physical_sources=self._sources_for(receiver_id),
            resolution_choices=self._resolution_choices(receiver_id),
            ownership=self._ownership_for(receiver_id),
            orphans=self._orphans_for(receiver_id),
        )

    def _pane(self, receiver_id: ReceiverId) -> PaneState:
        pane = project(self._snapshot(receiver_id), self._inputs(receiver_id, self._report()))
        rejection = self._rejections.get(receiver_id)
        if rejection is not None:
            # Only the actionable-error slot changes; phase, badges and the action
            # set stay exactly as the projection decided them.
            return replace(pane, diagnostic=rejection)
        return pane

    def _candidates(self) -> list[ReceiverId]:
        ordered: dict[ReceiverId, None] = {}
        for receiver_id in self._views:
            ordered.setdefault(receiver_id, None)
        for receiver_id in self._saved:
            ordered.setdefault(receiver_id, None)
        for receiver_id in list(self._snapshots):
            ordered.setdefault(receiver_id, None)
        for record in self._records.values():
            ordered.setdefault(record.receiver_id, None)
        return list(ordered)

    def _visible(self, receiver_id: ReceiverId) -> bool:
        """Discovery, saved metadata, live intent and unresolved ownerships."""
        view = self._view(receiver_id)
        if view is not None and bool(getattr(view, "online", False)):
            return True
        if receiver_id in self._saved:
            return True
        snapshot = self._snapshot(receiver_id)
        if snapshot.online or _owns_anything(snapshot):
            return True
        return bool(self._records_for(receiver_id)) or bool(self._orphans_for(receiver_id))

    def _online(self, receiver_id: ReceiverId) -> bool:
        view = self._view(receiver_id)
        if view is not None and bool(getattr(view, "online", False)):
            return True
        return bool(self._snapshot(receiver_id).online)

    def _union(self) -> list[ReceiverId]:
        # Deterministic dashboard order: reachable receivers first, then the
        # offline records a user may still act on, each by stable identity.
        return sorted(
            (
                receiver_id for receiver_id in self._candidates()
                if self._visible(receiver_id)
            ),
            key=lambda receiver_id: (0 if self._online(receiver_id) else 1, str(receiver_id)),
        )

    # ------------------------------------------------------------------ render

    def _request_render(self) -> None:
        if self._render_pending or self._disposed:
            return
        self._render_pending = True
        self._bridge.render.emit()

    def _render_requested(self) -> None:
        self._render_pending = False
        self._render_now()

    def _render_now(self) -> None:
        if self._disposed or not self._started:
            return
        self._ensure_loads()
        report = self._report()
        receivers = self._union()
        rows = tuple(
            project_row(self._snapshot(receiver_id), self._inputs(receiver_id, report))
            for receiver_id in receivers
        )
        self._window.render_receivers(rows)
        if self._selected not in receivers:
            self._selected = receivers[0] if receivers else None
            self._selection_auto = True
        elif (
            self._selection_auto
            and receivers
            and not self._online(self._selected)
            and self._online(receivers[0])
        ):
            # An auto-selected offline record yields to a receiver discovery just
            # found; an explicit user selection is never taken away from them.
            self._selected = receivers[0]
        selected = self._selected
        available = sum(1 for row in rows if row.online)
        sessions = sum(1 for row in rows if row.mode_label)
        self._window.set_summary(available, sessions, report.environment)
        self._window.set_activity(self._activity.entries())
        if selected is None:
            # The window owns the empty state; reuse it rather than fabricating a
            # PaneState that would carry actions no receiver can honour.
            self._window._render_no_selection()  # noqa: SLF001 - owned empty state
            return
        pane = self._pane(selected)
        self._window.select_receiver(selected)
        self._window.render_selected(pane)
        self._window.set_sources(self._sources_for(selected))
        self._window.set_resolutions(self._resolution_choices(selected))
        source = self._source_for(selected)
        if source is not None:
            self._window.select_source(source.connector)
        size = self._selected_size(selected)
        if size is not None:
            self._window.select_resolution(size)
        self._ensure_selection_loads(selected)
        self._maybe_open_pairing(selected, pane)

    # ------------------------------------------------------------------- loads

    def _ensure_loads(self) -> None:
        if not self._saved_loaded and not self._saved_loading:
            self._start_saved_load()
        if not self._ownerships_loaded and self._ownerships_loading == 0:
            self._reload_ownerships()

    def _ensure_selection_loads(self, receiver_id: ReceiverId) -> None:
        if self._sources_loaded_for != receiver_id:
            self._load_sources(receiver_id)

    def _start_saved_load(self) -> None:
        if self._saved_loading:
            return
        self._saved_loading = True
        self._schedule(self._load_saved())

    async def _load_saved(self) -> None:
        try:
            result = await self._receivers.all()
        except Exception:  # noqa: BLE001 - a store failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.persistence_failed))
        finally:
            self._saved_loading = False
        if self._disposed:
            return
        self._saved_loaded = True
        if isinstance(result, Ok):
            self._saved = {
                saved.receiver_id: saved for saved in result.value
                if isinstance(saved, SavedReceiver)
            }
        else:
            self._reject(self._selected, result.error)
        self._request_render()

    def _reload_ownerships(self) -> None:
        self._ownership_epoch += 1
        self._ownerships_loading += 1
        self._schedule(self._fetch_ownerships(self._ownership_epoch))

    async def _fetch_ownerships(self, token: int) -> None:
        try:
            result = await self._ownerships.all()
        except Exception:  # noqa: BLE001 - a store failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.persistence_failed))
        finally:
            self._ownerships_loading = max(0, self._ownerships_loading - 1)
        # A newer load (a later scan or a local deletion) supersedes this answer
        # even when it arrives successfully.
        if token != self._ownership_epoch or self._disposed:
            return
        if isinstance(result, Ok):
            # A load issued before a confirmed release can land after it.  The
            # answer belongs to the subject that issued it, so a record whose
            # receiver already confirmed the release is not resurrected.
            self._records = {
                record.owner_uuid: record for record in result.value
                if isinstance(record, OwnershipRecord) and not self._is_released(record)
            }
        else:
            self._reject(self._selected, result.error)
        self._ownerships_loaded = True
        self._request_render()

    def _load_sources(self, receiver_id: ReceiverId) -> None:
        if self._sources_loading_for == receiver_id:
            return
        self._sources_loading_for = receiver_id
        self._source_token += 1
        self._schedule(self._fetch_sources(self._source_token, receiver_id))

    async def _fetch_sources(self, token: int, receiver_id: ReceiverId) -> None:
        try:
            result = await self._capture.list_physical()
        except Exception:  # noqa: BLE001 - an adapter failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.unsupported_source, receiver_id))
        finally:
            if self._sources_loading_for == receiver_id:
                self._sources_loading_for = None
        if token != self._source_token or receiver_id != self._selected or self._disposed:
            return
        if isinstance(result, Ok):
            self._sources = tuple(
                source for source in result.value
                if isinstance(source, PhysicalMonitorSource)
            )
        else:
            self._sources = ()
            self._reject(receiver_id, result.error)
        self._sources_loaded_for = receiver_id
        self._request_render()

    async def _start_session(self, receiver_id: Any, request: StartRequest) -> None:
        """Clear the desktop's remembered screen, then dispatch the start.

        Mirror carries no requested source: the backend opens the capture session
        and the portal decides which screen it gets. Clearing the remembered
        approval first is what makes Start ask instead of silently replaying an
        old choice, and a failure to clear refuses the start rather than letting
        it run with state the operator asked to be forgotten.
        """
        if request.mode is Mode.MIRROR:
            cleared = await self._clear_remembered_screen()
            if isinstance(cleared, Err):
                self._reject(receiver_id, cleared.error)
                return
        await self._dispatch(Start(receiver_id=receiver_id, request=request))

    async def _clear_remembered_screen(self) -> Result[None]:
        """Remove every remembered ScreenCast approval; log the outcome."""
        store = self._screencast
        if store is None:
            self._record_screen_memory(None, ErrorCode.dependency_missing)
            return Err(error=diagnostic(ErrorCode.dependency_missing))
        try:
            result = await store.forget_all()
        except Exception:  # noqa: BLE001 - an adapter failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.persistence_failed))
        if isinstance(result, Err):
            self._record_screen_memory(None, result.error.code)
            return result
        self._record_screen_memory(result.value, None)
        return Ok(value=None)

    def _record_screen_memory(self, forgotten: int | None, failure: ErrorCode | None) -> None:
        """Log one screen-memory outcome without letting a sink break the action."""
        fields: dict[str, Any] = (
            {"remembered": forgotten} if forgotten is not None
            else {"error_code": failure if failure is not None else ErrorCode.persistence_failed}
        )
        try:
            self._activity.emit(safe_activity(
                "screen.forget" if failure is None else "screen.forget_failed",
                None, fields,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ))
        except Exception:  # noqa: BLE001 - the activity sink is never authoritative
            pass

    async def _refresh_discovery(self) -> None:
        try:
            result = await self._discovery.refresh()
        except Exception:  # noqa: BLE001 - an adapter failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.discovery_failed))
        if isinstance(result, Err):
            self._reject(self._selected, result.error)
        self._request_render()

    # -------------------------------------------------------- subscription in

    def _snapshot_arrived(self, snapshot: object) -> None:
        if self._disposed or not isinstance(snapshot, SessionSnapshot):
            return
        previous = self._snapshots.get(snapshot.receiver_id)
        if (
            previous is not None
            and snapshot.session_generation < previous.session_generation
        ):
            # A superseded intent's snapshot must never overwrite newer state,
            # even when it is delivered late.
            return
        self._snapshots[snapshot.receiver_id] = snapshot
        pairing = self._pairing_pending.get(snapshot.receiver_id)
        if pairing is not None and self._pane(snapshot.receiver_id).challenge != pairing:
            # Snapshot publication is authoritative even when this receiver is
            # no longer selected.  Expiry, cancellation, and replacement must
            # close the old real prompt before a typed secret can linger there.
            self._pairing_pending.pop(snapshot.receiver_id, None)
            self._invalidate_pairing(pairing)
        queued = self._switch_queued.get(snapshot.receiver_id)
        if queued is not None and snapshot.mode is queued[1]:
            del self._switch_queued[snapshot.receiver_id]
        # An authoritative state supersedes a receipt error for that receiver.
        self._rejections.pop(snapshot.receiver_id, None)
        # Reconcile durable ownership evidence on every publication: a confirmed
        # release must clear the cached record, not linger as phantom cleanup.
        self._reconcile_ownership(snapshot)
        self._request_render()

    def _discovery_arrived(self, event: object) -> None:
        if self._disposed:
            return
        kind = getattr(event, "kind", None)
        view = getattr(event, "receiver", None)
        if view is not None:
            receiver_id = getattr(getattr(view, "record_set", None), "receiver_id", None)
            if receiver_id is not None:
                self._views[receiver_id] = view
        if kind == "error":
            self._reject(self._selected, getattr(event, "diagnostic", None))
        self._request_render()

    def _activity_arrived(self, _event: object) -> None:
        if not self._disposed:
            self._request_render()

    # -------------------------------------------------------------- selection

    def _on_receiver_selected(self, value: str) -> None:
        if self._shutting_down or self._disposed:
            return
        receiver_id = _as_receiver_id(value)
        if receiver_id is None or receiver_id not in self._union():
            return
        if receiver_id == self._selected:
            return
        self._selected = receiver_id
        self._selection_auto = False
        self._sources_loaded_for = None
        self._load_sources(receiver_id)
        self._reload_ownerships()
        self._request_render()

    # ----------------------------------------------------------------- intent

    def _on_action_requested(self, value: str) -> None:
        if self._shutting_down or self._disposed:
            return
        action = _action_of(value)
        if action is None:
            return
        if action is Action.scan:
            # Scan is global: it works with no receiver selected and refreshes
            # every row, never only the selected one.
            self._schedule(self._refresh_discovery())
            selected = self._selected
            if selected is not None:
                self._load_sources(selected)
            # A scan also re-reads durable ownership evidence: an output may have
            # appeared or gone while the dashboard was idle.
            self._reload_ownerships()
            return
        receiver_id = self._selected
        if receiver_id is None:
            return
        pane = self._pane(receiver_id)
        if action not in pane.actions:
            # A disabled control (or a signal that raced a newer snapshot) never
            # becomes a command.
            return
        if action is Action.retry and pane.capability_blocked:
            # Retry here means "I fixed the missing capability": a frozen startup
            # report would keep Start disabled until the whole application was
            # restarted, which is exactly what the pane's notice denies.
            self._schedule(self._refresh_capabilities(receiver_id))
            return
        if action is Action.start:
            request = self._start_request(receiver_id, pane.visible_mode)
            if request is None:
                self._reject(
                    receiver_id,
                    diagnostic(ErrorCode.unsupported_source, receiver_id),
                )
                return
            self._schedule(
                self._start_session(receiver_id, request), receiver_id=receiver_id,
            )
            return
        if action is Action.pair:
            self._schedule(
                self._dispatch(Pair(receiver_id=receiver_id)),
                receiver_id=receiver_id,
            )
            return
        if action is Action.submit_pairing:
            if pane.challenge is not None:
                self._open_pairing(receiver_id, pane.challenge)
            return
        factory = _COMMAND_FACTORIES.get(action)
        if factory is not None:
            self._schedule(
                self._dispatch(factory(receiver_id=receiver_id)),
                receiver_id=receiver_id,
            )
            return
        if action in (Action.keep_blocked, Action.keep_orphan):
            # Both are explicit acknowledgements of an already-safe state: the
            # trust barrier keeps blocking, and an unowned output is never
            # removed automatically, so no command exists (or is invented).
            return
        if action is Action.remove_metadata:
            self._schedule(self._remove_metadata(receiver_id), receiver_id=receiver_id)
            return
        if action is Action.choose_resolution:
            self._schedule(
                self._resolution_prompt(receiver_id, change=False),
                receiver_id=receiver_id,
            )
            return
        if action is Action.change_resolution:
            self._schedule(
                self._resolution_prompt(receiver_id, change=True),
                receiver_id=receiver_id,
            )
            return
        if action is Action.confirm_orphan:
            self._schedule(self._orphan_prompt(receiver_id), receiver_id=receiver_id)

    async def _refresh_capabilities(self, receiver_id: Any) -> None:
        """Re-probe capability evidence, then re-render the same selection."""
        refresh = self._refresh_capabilities_report
        if refresh is None:
            return
        fence = self._fence(receiver_id)
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - a failed re-probe changes nothing
            pass
        if self._shutting_down or self._disposed:
            return
        if fence is not None and fence != self._fence(receiver_id):
            return
        self._request_render()

    def _on_mode_requested(self, value: str) -> None:
        if self._shutting_down or self._disposed:
            return
        mode = _mode_of(value)
        receiver_id = self._selected
        if mode is None or receiver_id is None:
            return
        pane = self._pane(receiver_id)
        snapshot = self._snapshot(receiver_id)
        if pane.visible_mode is mode:
            return
        if pane.mode_enabled:
            # Idle selection is local: it changes which editor the pane renders and
            # which mode the next Start carries, and it dispatches nothing.
            self._idle_modes[receiver_id] = mode
            self._request_render()
            return
        if _owns_anything(snapshot) and _switch_allowed(snapshot):
            if receiver_id in self._switch_pending:
                return
            queued = self._switch_queued.get(receiver_id)
            if queued is not None and queued[0] == snapshot.session_generation:
                # This exact session generation already has a queued switch; a
                # second confirmation prompt would target the same teardown.
                return
            self._schedule(
                self._confirmed_switch(receiver_id, mode), receiver_id=receiver_id,
            )
            return
        # Stopping, cleanup, reconfiguration and uncertain trust: refuse the
        # requested switch outright instead of prompting for an impossible one.

    def _on_source_selected(self, connector: str) -> None:
        if self._shutting_down or self._disposed or not isinstance(connector, str):
            return
        receiver_id = self._selected
        if receiver_id is None:
            return
        pane = self._pane(receiver_id)
        if not pane.source_enabled:
            return
        chosen = next(
            (
                source for source in self._sources_for(receiver_id)
                if source.connector == connector
            ),
            None,
        )
        if chosen is None:
            return
        self._selected_source[receiver_id] = chosen
        self._request_render()

    def _on_resolution_selected(self, width: int, height: int) -> None:
        if self._shutting_down or self._disposed:
            return
        receiver_id = self._selected
        if receiver_id is None:
            return
        try:
            size = Size(width=width, height=height)
        except (TypeError, ValueError):
            return
        pane = self._pane(receiver_id)
        if not (
            pane.resolution_enabled
            or Action.choose_resolution in pane.actions
            or Action.change_resolution in pane.actions
        ):
            return
        if size not in self._resolution_choices(receiver_id):
            return
        self._selected_resolution[receiver_id] = size
        self._request_render()

    def _on_mute_requested(self, muted: bool) -> None:
        if self._shutting_down or self._disposed:
            return
        receiver_id = self._selected
        if receiver_id is None:
            return
        pane = self._pane(receiver_id)
        if Action.mute not in pane.actions:
            # Unavailable audio disables mute; a stale toggle must not command it.
            return
        self._schedule(
            self._dispatch(SetMuted(receiver_id=receiver_id, muted=bool(muted))),
            receiver_id=receiver_id,
        )

    # ------------------------------------------------------------ confirmations

    async def _confirmed_switch(self, receiver_id: ReceiverId, mode: Mode) -> None:
        """Queue the next mode only after an explicit Stop confirmation."""
        self._switch_pending.add(receiver_id)
        try:
            captured = self._fence(receiver_id)
            request = self._start_request(receiver_id, mode)
            if request is None:
                self._reject(
                    receiver_id, diagnostic(ErrorCode.unsupported_source, receiver_id)
                )
                return
            try:
                approved = bool(await self._dialogs.confirm_stop(mode))
            except Exception:  # noqa: BLE001 - a broken prompt is a non-answer
                approved = False
            if not approved:
                return
            # The confirmation belongs to the session that asked for it. A newer
            # session, endpoint or generation means the answer is stale.
            if self._fence(receiver_id) != captured:
                return
            if not _switch_allowed(self._snapshot(receiver_id)):
                return
            snapshot = self._snapshot(receiver_id)
            self._switch_queued[receiver_id] = (snapshot.session_generation, mode)
            await self._dispatch(
                ConfirmSwitch(receiver_id=receiver_id, request=request, confirmed=True)
            )
        finally:
            self._switch_pending.discard(receiver_id)

    def _open_pairing(
        self,
        receiver_id: ReceiverId,
        challenge: ChallengeView,
        pane: PaneState | None = None,
    ) -> None:
        if self._pairing_pending.get(receiver_id) == challenge:
            return
        previous = self._pairing_pending.get(receiver_id)
        if previous is not None:
            self._invalidate_pairing(previous)
        current = pane if pane is not None else self._pane(receiver_id)
        if current.challenge != challenge:
            return
        self._pairing_seen[receiver_id] = challenge
        self._pairing_pending[receiver_id] = challenge
        self._schedule(
            self._pairing_prompt(
                receiver_id,
                challenge,
                actions=current.actions,
                retained_output=current.retained_output,
            ),
            receiver_id=receiver_id,
        )

    def _maybe_open_pairing(self, receiver_id: ReceiverId, pane: PaneState) -> None:
        challenge = pane.challenge
        if challenge is None:
            previous = self._pairing_pending.pop(receiver_id, None)
            if previous is not None:
                self._invalidate_pairing(previous)
            self._pairing_seen.pop(receiver_id, None)
            return
        if receiver_id != self._selected:
            return
        if self._pairing_seen.get(receiver_id) == challenge:
            return
        self._open_pairing(receiver_id, challenge, pane)

    def _invalidate_pairing(self, challenge: ChallengeView) -> None:
        """Close and wipe one prompt without cancelling a caller's fake answer."""
        try:
            self._dialogs.invalidate_pairing(challenge)
        except Exception:  # noqa: BLE001 - invalidation is a safe best effort
            return

    async def _pairing_prompt(
        self,
        receiver_id: ReceiverId,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action],
        retained_output: str | None,
    ) -> None:
        """Read one explicit choice, then dispatch it only if still authorized."""
        answer: object = None
        try:
            answer = await self._dialogs.pairing(
                challenge,
                actions=actions,
                retained_output=retained_output,
            )
        except Exception:  # noqa: BLE001 - a broken prompt is a non-answer
            answer = None
        finally:
            if self._pairing_pending.get(receiver_id) == challenge:
                del self._pairing_pending[receiver_id]

        action: Action | None
        value: SensitiveText | None
        if isinstance(answer, PairingAnswer):
            action, value = answer.action, answer.value
        elif isinstance(answer, SensitiveText):
            # Keep deterministic/fake factories from the original presenter
            # contract usable: a bare secret is an explicit submit answer.
            action, value = Action.submit_pairing, answer
        else:
            action, value = None, None
        if action is None:
            # Ordinary dismissal never mutates trust: the challenge stays live
            # until the user submits, retries, cancels or it expires.
            return
        current = self._pane(receiver_id)
        if current.challenge != challenge or action not in current.actions:
            # Receiver, challenge identity or the projection changed while the
            # prompt was open.  Do not let a stale button become a command.
            return
        if action is Action.submit_pairing:
            if not isinstance(value, SensitiveText):
                return
            command: Command = SubmitPairing(
                receiver_id=receiver_id,
                challenge_id=challenge.challenge_id,
                value=value,
            )
        elif action in (Action.cancel, Action.retry):
            command = _COMMAND_FACTORIES[action](receiver_id=receiver_id)
        else:
            return
        await self._dispatch(command)

    async def _resolution_prompt(self, receiver_id: ReceiverId, *, change: bool) -> None:
        if receiver_id in self._resolution_pending:
            return
        self._resolution_pending.add(receiver_id)
        try:
            captured = self._fence(receiver_id)
            choices = self._resolution_choices(receiver_id)
            try:
                answer = await self._dialogs.resolution(tuple(choices))
            except Exception:  # noqa: BLE001 - a broken prompt is a non-answer
                answer = None
            if not isinstance(answer, Size) or answer not in choices:
                return
            if self._fence(receiver_id) != captured:
                return
            # The picker answer is the receiver's explicit choice from now on.
            self._selected_resolution[receiver_id] = answer
            decision = self._decision_for(receiver_id, answer)
            if decision is None:
                return
            if change:
                command: Command = ChangeResolution(
                    receiver_id=receiver_id, decision=decision
                )
            else:
                command = ChooseResolution(receiver_id=receiver_id, decision=decision)
            await self._dispatch(command)
        finally:
            self._resolution_pending.discard(receiver_id)

    async def _orphan_prompt(self, receiver_id: ReceiverId) -> None:
        if receiver_id in self._orphan_pending:
            return
        self._orphan_pending.add(receiver_id)
        try:
            items = self._orphans_for(receiver_id)
            item = items[0] if items else None
            if item is None:
                return
            owner_uuid = item.record.owner_uuid
            try:
                confirmed = bool(await self._dialogs.orphan(item))
            except Exception:  # noqa: BLE001 - a broken prompt is a non-answer
                confirmed = False
            if not confirmed:
                return
            # The exact owner identity must still be unresolved when the answer
            # arrives, or removal would target a resource the user never saw.
            if not any(
                record.owner_uuid == owner_uuid
                for record in self._records_for(receiver_id)
            ):
                return
            await self._dispatch(
                ConfirmOrphanRemoval(
                    receiver_id=receiver_id, owner_uuid=owner_uuid, confirmed=True
                )
            )
            self._reload_ownerships()
        finally:
            self._orphan_pending.discard(receiver_id)

    async def _remove_metadata(self, receiver_id: ReceiverId) -> None:
        """Forget saved metadata only; trust cleanup and display records stay."""
        try:
            result = await self._receivers.remove_metadata(receiver_id)
        except Exception:  # noqa: BLE001 - a store failure is a typed diagnostic
            result = Err(error=diagnostic(ErrorCode.persistence_failed, receiver_id))
        if isinstance(result, Err):
            self._reject(receiver_id, result.error)
            return
        self._saved.pop(receiver_id, None)
        self._request_render()

    # ---------------------------------------------------------------- dispatch

    async def _dispatch(self, command: Command) -> None:
        """Dispatch one fresh command and apply only a still-current rejection."""
        receiver_id = getattr(command, "receiver_id", None)
        task = asyncio.current_task()
        registered = self._task_fences.get(task) if task is not None else None
        if registered is not None and registered[0] == receiver_id:
            # _schedule captured the fence before this coroutine was allowed to
            # run.  Do not replace that admission fence with a newer one merely
            # because the event loop delayed starting the coroutine.
            fence = registered[1]
        else:
            fence = self._fence(receiver_id) if receiver_id is not None else None
            if task is not None:
                self._task_fences[task] = (receiver_id, fence)
        try:
            receipt = await self._manager.dispatch(command)
        except Exception:  # noqa: BLE001 - never surface port exception text
            if fence is not None and fence != self._fence(receiver_id):
                # An exception from a superseded operation is just as stale as a
                # rejected receipt; it must not diagnose the newer intent.
                return
            self._reject(receiver_id, None)
            return
        if bool(getattr(receipt, "accepted", False)):
            return
        if fence is not None and fence != self._fence(receiver_id):
            # A newer intent already owns this receiver; the old refusal is stale.
            return
        self._reject(receiver_id, getattr(receipt, "diagnostic", None))

    def _fence(self, receiver_id: Any) -> tuple[Any, ...] | None:
        if receiver_id is None:
            return None
        snapshot = self._snapshot(receiver_id)
        target = snapshot.target if snapshot.online else None
        return (
            snapshot.receiver_id,
            snapshot.session_generation,
            snapshot.operation_id,
            target.endpoint_fingerprint if target is not None else None,
            target.endpoint_generation if target is not None else None,
        )

    def _reject(self, receiver_id: Any, error: object) -> None:
        """Render one catalog diagnostic for ``receiver_id``; never backend text."""
        target = receiver_id if receiver_id is not None else self._selected
        if target is None:
            return
        receiver = _as_receiver_id(target)
        if receiver is None:
            return
        safe = _safe_error(error)
        try:
            self._rejections[receiver] = diagnostic(
                safe.code if safe is not None else _UNEXPECTED, receiver
            )
        except (TypeError, ValueError, KeyError):
            self._rejections[receiver] = diagnostic(_UNEXPECTED)
        self._request_render()

    # ------------------------------------------------------------------ closing

    def _begin_shutdown(self) -> asyncio.Task[None] | None:
        if self._disposed:
            return None
        if self._shutdown_task is not None and not self._shutdown_task.done():
            return self._shutdown_task
        self._shutting_down = True
        # Bounded progress stays legible: the window is not hidden or blocked while
        # the composition root stops sessions and confirms cleanup.
        self._window.setWindowTitle(_CLOSING_TITLE)
        task = asyncio.ensure_future(self._shutdown())
        self._shutdown_task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._shutdown_finished)
        return task

    def _on_closing_requested(self) -> None:
        self._begin_shutdown()

    def _shutdown_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._complete_shutdown(Err(error=diagnostic(_UNEXPECTED)))

    async def _shutdown(self) -> None:
        try:
            result = await self._request_shutdown()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - never surface shutdown exception text
            result = Err(error=diagnostic(_UNEXPECTED))
        self._complete_shutdown(result)

    def _complete_shutdown(self, result: object) -> None:
        if not isinstance(result, Ok):
            # Unconfirmed cleanup: keep the dashboard up with a retryable,
            # catalog-only diagnostic. Closing again starts a fresh attempt.
            self._shutting_down = False
            self._shutdown_task = None
            self._window.setWindowTitle(_WINDOW_TITLE)
            error = result.error if isinstance(result, Err) else None
            self._reject(self._selected, error)
            self._request_render()
            return
        self._dispose()

    def _dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._shutting_down = True
        for subscription in self._subscriptions:
            try:
                subscription.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                continue
        self._subscriptions.clear()
        for task in tuple(self._tasks):
            if task is self._shutdown_task:
                continue
            task.cancel()
        self._tasks.clear()
        self._window.allow_close()
        self._window.close()

    # ------------------------------------------------------------------ helpers

    def _schedule(
        self,
        awaitable: Awaitable[Any],
        *,
        receiver_id: ReceiverId | None = None,
    ) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        if receiver_id is not None:
            self._task_fences[task] = (receiver_id, self._fence(receiver_id))
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        task_fence = self._task_fences.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        # A presenter task must never surface a raw exception string (it could
        # quote a prompt answer). Receiver-scoped tasks retain their original
        # fence, so a late exception cannot diagnose newer selected state.
        if task_fence is not None:
            receiver_id, fence = task_fence
            if fence is not None and fence != self._fence(receiver_id):
                return
            self._reject(receiver_id, None)
            return
        self._reject(self._selected, None)


# --------------------------------------------------------------------- helpers


def _owns_anything(snapshot: SessionSnapshot) -> bool:
    """Whether any resource, intent or barrier still belongs to this receiver."""
    return bool(
        snapshot.intent_active
        or snapshot.cleanup_pending
        or snapshot.queued_start is not None
        or snapshot.challenge is not None
        or snapshot.stream is not None
        or snapshot.physical_source is not None
        or snapshot.display_handle is not None
        or snapshot.ownership_uuid is not None
        or snapshot.display is not DisplayState.None_
        or snapshot.transport in _ACTIVE_TRANSPORT
        or snapshot.phase not in (SessionPhase.Available, SessionPhase.Offline)
        or _uncertain_trust(snapshot)
    )


def _uncertain_trust(snapshot: SessionSnapshot) -> bool:
    return snapshot.trust in _UNCERTAIN_TRUST


def _switch_allowed(snapshot: SessionSnapshot) -> bool:
    """A confirmed switch may only stop a live, unblocked session."""
    return bool(
        snapshot.online
        and snapshot.intent_active
        and not snapshot.cleanup_pending
        and snapshot.display is not DisplayState.CleanupRequired
        and not _uncertain_trust(snapshot)
        and snapshot.phase not in _SWITCH_BLOCKED_PHASES
        and snapshot.transport is not TransportState.Stopping
    )


def _named_owners(snapshot: SessionSnapshot) -> set[Any]:
    """The exact owners this authoritative state says the session holds."""
    handle = snapshot.display_handle
    return {
        value for value in (
            snapshot.ownership_uuid,
            handle.owner_uuid if handle is not None else None,
        ) if value is not None
    }


def _cleanup_confirmed(snapshot: SessionSnapshot) -> bool:
    """Whether every resource of this session is confirmed released.

    A failed or partially released state is not evidence of release: the
    provider only reports ``DisplayState.None_`` with no handle, owner, stream,
    capture source or audio control once it has acknowledged the release.
    """
    return (
        snapshot.display is DisplayState.None_
        and snapshot.display_handle is None
        and snapshot.ownership_uuid is None
        and snapshot.stream is None
        and snapshot.physical_source is None
        and snapshot.transport is TransportState.Idle
        and snapshot.audio is AudioState.Disabled
        and not snapshot.cleanup_pending
    )


def _record_order(record: OwnershipRecord) -> tuple[int, int, str]:
    """Removal intent first, then the most advanced reservation stage."""
    return (
        0 if record.desired_state == "removing" else 1,
        -_STAGE_ORDER.get(record.stage, -1),
        str(record.owner_uuid),
    )


def _safe_error(error: object) -> Diagnostic | None:
    """Reduce any receiving diagnostic to a catalog record, or ``None``.

    The returned message and recovery list are always the catalog's fixed copy:
    adapter-supplied ``message``/``detail`` never reach the pane.
    """
    if not isinstance(error, Diagnostic):
        return None
    code = error.code
    if isinstance(code, ErrorCode):
        return diagnostic(code)
    return None


def _action_of(value: object) -> Action | None:
    if not isinstance(value, str):
        return None
    try:
        return Action(value)
    except ValueError:
        return None


def _mode_of(value: object) -> Mode | None:
    if not isinstance(value, str):
        return None
    try:
        return Mode(value)
    except ValueError:
        return None


def _as_receiver_id(value: object) -> ReceiverId | None:
    if not isinstance(value, str) or not value:
        return None
    return ReceiverId(value)


# ------------------------------------------------------------------ Qt prompts


class QtDialogFactory:
    """Nonmodal Qt prompt factory with the safe answer as every default.

    The pairing prompt is built with the dashboard as its owner for iconography
    only: :class:`~airplay_linux.ui.dialogs.PairingDialog` drops the Qt parent and
    keeps itself out of the dashboard's modal chain, so the presenter must never
    adopt or re-parent it. The three destructive/typed decisions stay modal to
    their owner, exactly as the widgets document.
    """

    def __init__(self, window: QWidget | None = None) -> None:
        self._window = window
        self._pairing_prompts: dict[ChallengeView, PairingDialog] = {}

    async def pairing(
        self,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action] | None = None,
        retained_output: str | None = None,
    ) -> PairingAnswer | None:
        if not isinstance(challenge, ChallengeView):
            raise TypeError("pairing requires a ChallengeView")
        dialog = self._pairing_dialog(
            challenge, actions=actions, retained_output=retained_output,
        )
        self._pairing_prompts[challenge] = dialog
        try:
            return await _resolve_dialog(dialog, dialog.take_result, None)
        finally:
            if self._pairing_prompts.get(challenge) is dialog:
                del self._pairing_prompts[challenge]

    def invalidate_pairing(self, challenge: ChallengeView) -> None:
        """Close the exact live prompt for an invalidated challenge."""
        dialog = self._pairing_prompts.get(challenge)
        if dialog is None:
            return
        try:
            dialog.close()
        except RuntimeError:  # pragma: no cover - already destroyed by Qt
            pass

    async def confirm_stop(self, mode: Mode) -> bool:
        if not isinstance(mode, Mode):
            raise TypeError("confirm_stop requires a Mode")
        dialog = self._confirm_stop_dialog(mode)
        return bool(await _resolve_dialog(dialog, dialog.confirmed, False))

    async def resolution(self, choices: tuple[Size, ...]) -> Size | None:
        dialog = self._resolution_dialog(tuple(choices))
        return await _resolve_dialog(dialog, dialog.decision, None)

    async def orphan(self, item: RecoveryItem) -> bool:
        if not isinstance(item, RecoveryItem):
            raise TypeError("orphan requires a RecoveryItem")
        dialog = self._orphan_dialog(item)
        return bool(await _resolve_dialog(dialog, dialog.confirmed, False))

    # -- overridable construction hooks (tests capture the dialogs) ---------

    def _pairing_dialog(
        self,
        challenge: ChallengeView,
        *,
        actions: frozenset[Action] | None = None,
        retained_output: str | None = None,
    ) -> PairingDialog:
        return PairingDialog(
            challenge,
            receiver_label=str(challenge.receiver_id),
            retained_output=retained_output,
            actions=actions,
            parent=self._window,
        )

    def _confirm_stop_dialog(self, mode: Mode) -> ConfirmStopDialog:
        return ConfirmStopDialog(mode, parent=self._window)

    def _resolution_dialog(self, choices: tuple[Size, ...]) -> ResolutionDialog:
        return ResolutionDialog(choices, parent=self._window)

    def _orphan_dialog(self, item: RecoveryItem) -> OrphanDialog:
        return OrphanDialog(item, parent=self._window)


async def _resolve_dialog(dialog: Any, read: Callable[[], Any], default: Any) -> Any:
    """Await one nonblocking dialog answer and always dispose of the prompt."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def finish(*_args: object) -> None:
        if future.done():
            return
        try:
            answer = read()
        except Exception:  # noqa: BLE001 - a destroyed prompt answers nothing
            answer = default
        future.set_result(answer)

    dialog.finished.connect(finish)
    dialog.destroyed.connect(finish)
    dialog.open()
    if future.done():
        # ``open`` can complete synchronously (an owner that is already gone).
        return future.result()
    try:
        return await future
    finally:
        try:
            dialog.close()
        except RuntimeError:  # pragma: no cover - already destroyed
            pass
        dialog.deleteLater()
