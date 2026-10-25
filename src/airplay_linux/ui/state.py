"""Pure projection of independent resource facts into a safe dashboard pane.

The session lane owns what a resource *is*: :class:`SessionSnapshot` carries the
independent transport, display, audio and trust states together with the phase
already derived from them.  This module owns what the dashboard may therefore
*offer*.  It never recomputes the phase, never presents an unconfirmed resource
as released, and never exposes an action the session lane would refuse to act on.

Three rules drive every decision:

* safety first - trust and cleanup uncertainty outrank a convenient-looking idle
  label, and a mode action is removed whenever any resource could still exist;
* independence - the four resource badges always come from their own authority,
  even when the derived phase is ``Failed`` or the pane is showing a decision;
* one vocabulary - labels, notices and primary actions are static source-written
  text.  A typed :class:`~airplay_linux.domain.resources.Diagnostic` is passed
  through for the presenter to render, never re-worded here.

The projection is free of Qt, I/O, clocks and tasks, so the whole safety matrix is
testable as a deterministic function of typed domain records.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverView
from airplay_linux.domain.operations import ChallengeView
from airplay_linux.domain.records import (
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
    ErrorCode,
    IntentStage,
    Mode,
    PhysicalMonitorSource,
    SessionPhase,
    Size,
    TransportState,
    TrustState,
)

#: Badge severity vocabulary; the widget layer maps these to colours.
SEVERITY_OK: Final[str] = "ok"
SEVERITY_INFO: Final[str] = "info"
SEVERITY_WARNING: Final[str] = "warning"
SEVERITY_ERROR: Final[str] = "error"


class Action(StrEnum):
    """One explicit user command the dashboard may offer.

    The members are commands, not labels: the presenter maps each to a typed
    domain command and decides whether a confirmation prompt is needed.
    """

    start = "start"
    stop = "stop"
    pair = "pair"
    submit_pairing = "submit_pairing"
    cancel = "cancel"
    scan = "scan"
    remove_metadata = "remove_metadata"
    retry = "retry"
    choose_resolution = "choose_resolution"
    change_resolution = "change_resolution"
    recreate = "recreate"
    retry_audio = "retry_audio"
    video_only = "video_only"
    mute = "mute"
    retry_cleanup = "retry_cleanup"
    keep_cleanup = "keep_cleanup"
    retry_revocation = "retry_revocation"
    keep_blocked = "keep_blocked"
    confirm_orphan = "confirm_orphan"
    keep_orphan = "keep_orphan"


@dataclass(frozen=True)
class Badge:
    """One compact, independently derived resource badge."""

    resource: str
    label: str
    severity: str


@dataclass(frozen=True)
class ViewInputs:
    """Everything the projection needs that is not a session resource fact.

    ``receiver``/``saved`` describe the identity shown online/offline,
    ``capabilities`` is the frozen startup evidence, and ``ownership``/``orphans``
    are the display provider's reconciliation results.  Sequences are normalised
    to tuples so a projection can never depend on a caller's mutable container.
    """

    receiver: ReceiverView | None
    saved: SavedReceiver | None
    capabilities: CapabilityReport
    selected_mode: Mode
    physical_sources: tuple[PhysicalMonitorSource, ...] = ()
    resolution_choices: tuple[Size, ...] = ()
    ownership: OwnershipRecord | None = None
    orphans: tuple[RecoveryItem, ...] = ()

    def __post_init__(self) -> None:
        if self.receiver is not None and not isinstance(self.receiver, ReceiverView):
            raise TypeError("receiver must be a ReceiverView or None")
        if self.saved is not None and not isinstance(self.saved, SavedReceiver):
            raise TypeError("saved must be a SavedReceiver or None")
        if not isinstance(self.capabilities, CapabilityReport):
            raise TypeError("capabilities must be a CapabilityReport")
        if not isinstance(self.selected_mode, Mode):
            raise TypeError("selected_mode must be a Mode")
        if self.ownership is not None and not isinstance(self.ownership, OwnershipRecord):
            raise TypeError("ownership must be an OwnershipRecord or None")
        _freeze_sequence(self, "physical_sources", PhysicalMonitorSource)
        _freeze_sequence(self, "resolution_choices", Size)
        _freeze_sequence(self, "orphans", RecoveryItem)


@dataclass(frozen=True)
class PaneState:
    """The selected-receiver pane, ready for a presenter to render verbatim."""

    title: str
    phase_label: str
    badges: tuple[Badge, ...]
    actions: frozenset[Action]
    mode_enabled: bool
    visible_mode: Mode
    primary_label: str
    source_enabled: bool
    resolution_enabled: bool
    mute_enabled: bool
    muted: bool
    challenge: ChallengeView | None
    notice: str | None
    diagnostic: Diagnostic | None


@dataclass(frozen=True)
class ReceiverRow:
    """One dense device-list row: identity, active mode, phase and output size."""

    receiver_id: ReceiverId
    name: str
    mode_label: str
    phase_label: str
    size_label: str
    online: bool


# --------------------------------------------------------------------------- labels

_PHASE_LABELS: Final[Mapping[SessionPhase, str]] = MappingProxyType(
    {
        SessionPhase.Available: "Available",
        SessionPhase.Offline: "Offline",
        SessionPhase.ResolutionRequired: "Resolution required",
        SessionPhase.AudioDecisionRequired: "Audio decision required",
        SessionPhase.CreatingDisplay: "Creating display",
        SessionPhase.Pairing: "Pairing",
        SessionPhase.Connecting: "Connecting",
        SessionPhase.Mirroring: "Mirroring",
        SessionPhase.VirtualDisplay: "Virtual display",
        SessionPhase.Reconnecting: "Reconnecting",
        SessionPhase.Reconfiguring: "Reconfiguring",
        SessionPhase.Recovering: "Recovering",
        SessionPhase.Stopping: "Stopping",
        SessionPhase.CleanupRequired: "Cleanup required",
        SessionPhase.TrustCleanupRequired: "Trust cleanup required",
        SessionPhase.Failed: "Failed",
    }
)

_MODE_LABELS: Final[Mapping[Mode, str]] = MappingProxyType(
    {
        Mode.MIRROR: "Mirror",
        Mode.VIRTUAL: "Virtual display",
    }
)

_TRANSPORT_LABELS: Final[Mapping[TransportState, tuple[str, str]]] = MappingProxyType(
    {
        TransportState.Idle: ("Idle", SEVERITY_INFO),
        TransportState.Pairing: ("Pairing", SEVERITY_WARNING),
        TransportState.Connecting: ("Connecting", SEVERITY_WARNING),
        TransportState.Streaming: ("Streaming", SEVERITY_OK),
        TransportState.Reconnecting: ("Reconnecting", SEVERITY_WARNING),
        TransportState.Stopping: ("Stopping", SEVERITY_WARNING),
        TransportState.Failed: ("Transport failed", SEVERITY_ERROR),
    }
)

_AUDIO_LABELS: Final[Mapping[AudioState, tuple[str, str]]] = MappingProxyType(
    {
        AudioState.Disabled: ("Audio off", SEVERITY_INFO),
        AudioState.Starting: ("Audio starting", SEVERITY_WARNING),
        AudioState.Enabled: ("Audio on", SEVERITY_OK),
        AudioState.Muted: ("Muted", SEVERITY_OK),
        AudioState.Unavailable: ("Audio unavailable", SEVERITY_ERROR),
    }
)

_TRUST_LABELS: Final[Mapping[TrustState, tuple[str, str]]] = MappingProxyType(
    {
        TrustState.Unknown: ("Trust unknown", SEVERITY_WARNING),
        TrustState.Untrusted: ("Not paired", SEVERITY_INFO),
        TrustState.Trusted: ("Paired", SEVERITY_OK),
        TrustState.Pairing: ("Pairing", SEVERITY_WARNING),
        TrustState.RevocationPending: ("Revoking trust", SEVERITY_WARNING),
        TrustState.CleanupRequired: ("Trust cleanup required", SEVERITY_ERROR),
    }
)

_DISPLAY_LABELS: Final[Mapping[DisplayState, tuple[str, str]]] = MappingProxyType(
    {
        DisplayState.Owned: ("Output owned", SEVERITY_OK),
        DisplayState.Resolving: ("Resolving output", SEVERITY_WARNING),
        DisplayState.Creating: ("Creating output", SEVERITY_WARNING),
        DisplayState.Reconfiguring: ("Reconfiguring output", SEVERITY_WARNING),
        DisplayState.Removing: ("Removing output", SEVERITY_WARNING),
        DisplayState.CleanupRequired: ("Cleanup unconfirmed", SEVERITY_ERROR),
        DisplayState.Failed: ("Output failed", SEVERITY_ERROR),
    }
)

_NOTICE_TRUST_CLEANUP: Final[str] = (
    "Trust revocation is unconfirmed, so pairing and new starts stay blocked "
    "until it is retried or kept blocked."
)
_NOTICE_CLEANUP: Final[str] = (
    "The owned resource could not be confirmed removed; retry cleanup or keep it "
    "for manual cleanup."
)
_NOTICE_BOTH_BARRIERS: Final[str] = (
    "Cleanup and trust revocation are both unconfirmed; no new pairing or start is "
    "allowed until both are resolved."
)
_NOTICE_STOPPING: Final[str] = "Stopping and confirming the removal of owned resources."
_NOTICE_PAIRING: Final[str] = "Enter the PIN or password shown by the receiver."
_NOTICE_PAIRING_OWNED: Final[str] = (
    "The output stays owned during pairing. Cancel stops the attempt and removes "
    "the retained output."
)
_NOTICE_RESOLUTION_REQUIRED: Final[str] = (
    "Choose the output size before a virtual output is reserved."
)
_NOTICE_RECONFIGURING: Final[str] = (
    "The previous output is removed before the new one is created. Desktop layout "
    "may change temporarily."
)
_NOTICE_RECONFIGURING_TRUST: Final[str] = (
    "The previous output is removed before the new one is created. Desktop layout "
    "may change temporarily, and unconfirmed trust revocation blocks any new "
    "pairing or start."
)
_NOTICE_RECOVERING: Final[str] = (
    "An owned virtual output is retained while the receiver connection is restored."
)
_NOTICE_RECONNECTING: Final[str] = (
    "The receiver connection is being retried; owned resources are retained."
)
_NOTICE_AUDIO_DECISION: Final[str] = (
    "Audio is unavailable before video is ready. Retry audio, start video only, or "
    "cancel."
)
_NOTICE_AUDIO_UNAVAILABLE: Final[str] = (
    "Video stays active, but audio is unavailable. Mute is disabled until audio is "
    "retried."
)
_NOTICE_OFFLINE: Final[str] = (
    "This receiver is offline. Its last known identity and saved metadata stay "
    "available."
)
_NOTICE_ORPHAN: Final[str] = (
    "This output is not confirmed as owned by this session. Removal requires "
    "explicit confirmation."
)
_NOTICE_CAPABILITY: Final[str] = (
    "The selected mode is unavailable until its missing capability is fixed. Retry "
    "after fixing it, or go back and choose an available mode."
)
_NOTICE_FAILED: Final[str] = (
    "The session failed. Owned resources stay explicit until cleanup is confirmed."
)
_NOTICE_RETAINED: Final[str] = (
    "A retained output is still owned. Retry the session or stop to release it."
)
_NOTICE_CREATING: Final[str] = (
    "Reservation stage: {stage}. The virtual output is not usable until publication "
    "is confirmed, and the desktop layout can change while it is created."
)
_NOTICE_MISMATCH: Final[str] = (
    "The receiver accepted {size}. The output is still owned: recreate it at that "
    "size, or stop to remove it."
)

#: Diagnostic details that name a capability belonging to one mode.  Details are
#: fixed vocabulary written by the capability probe, never backend text.
_MODE_KEYWORDS: Final[Mapping[Mode, tuple[str, ...]]] = MappingProxyType(
    {
        Mode.MIRROR: ("physical", "capture", "portal"),
        Mode.VIRTUAL: ("virtual", "kscreen", "krfb", "helper"),
    }
)


# --------------------------------------------------------------------------- public


def project(snapshot: SessionSnapshot, inputs: ViewInputs) -> PaneState:
    """Project one receiver's authoritative facts into a safe pane."""
    _require(snapshot, inputs)
    ownership = _ownership_for(snapshot, inputs)
    orphans = _orphans_for(snapshot, inputs)
    decision = _decide(snapshot, inputs, ownership, orphans)
    return PaneState(
        title=_title(snapshot, inputs),
        phase_label=_phase_label(snapshot),
        badges=_badges(snapshot, ownership),
        actions=decision.actions,
        mode_enabled=_mode_enabled(snapshot, ownership),
        visible_mode=_visible_mode(snapshot, inputs),
        primary_label=decision.primary,
        source_enabled=_source_enabled(snapshot, inputs),
        resolution_enabled=_resolution_enabled(snapshot, inputs),
        mute_enabled=Action.mute in decision.actions,
        muted=bool(snapshot.muted),
        challenge=_actionable_challenge(snapshot),
        notice=decision.notice,
        diagnostic=_diagnostic(snapshot, inputs, decision, orphans),
    )


def project_row(snapshot: SessionSnapshot, inputs: ViewInputs) -> ReceiverRow:
    """Project the dense device-list row for one receiver."""
    _require(snapshot, inputs)
    ownership = _ownership_for(snapshot, inputs)
    visible = _visible_mode(snapshot, inputs)
    return ReceiverRow(
        receiver_id=snapshot.receiver_id,
        name=_title(snapshot, inputs),
        mode_label=_MODE_LABELS[visible] if _session_owns_mode(snapshot) else "",
        phase_label=_phase_label(snapshot),
        size_label=_row_size_label(snapshot, ownership, visible),
        online=bool(snapshot.online),
    )


# --------------------------------------------------------------------------- decision


@dataclass(frozen=True, kw_only=True)
class _Decision:
    kind: str
    actions: frozenset[Action]
    primary: str
    notice: str | None


def _decide(
    state: SessionSnapshot,
    inputs: ViewInputs,
    ownership: OwnershipRecord | None,
    orphans: tuple[RecoveryItem, ...],
) -> _Decision:
    trust_uncertain = _trust_uncertain(state)
    cleanup_uncertain = _cleanup_uncertain(state) or _unreflected_ownership(state, ownership)
    in_progress = _teardown_in_progress(state)

    # A durable ownership record the snapshot does not reflect is unconfirmed
    # cleanup even when the display phase looks idle: never release it silently.
    if cleanup_uncertain and in_progress:
        return _Decision(
            kind="stopping", actions=frozenset(), primary="Stopping",
            notice=_NOTICE_STOPPING,
        )
    if cleanup_uncertain:
        actions: set[Action] = {Action.retry_cleanup, Action.keep_cleanup}
        if trust_uncertain:
            actions.update({Action.retry_revocation, Action.keep_blocked})
            if _saved_for(state, inputs) is not None:
                actions.add(Action.remove_metadata)
        if orphans:
            actions.update({Action.confirm_orphan, Action.keep_orphan})
        if _needs_teardown(state) or _retained_display(state, ownership):
            actions.add(Action.stop)
        return _Decision(
            kind="cleanup",
            actions=frozenset(actions),
            primary="Retry cleanup",
            notice=_NOTICE_BOTH_BARRIERS if trust_uncertain else _NOTICE_CLEANUP,
        )

    # The session lane derives Reconfiguring ahead of trust cleanup, so the
    # user's progress control stays Cancel while the old output is replaced.
    if _reconfiguring(state):
        actions = {Action.cancel}
        notice = _NOTICE_RECONFIGURING
        if trust_uncertain:
            actions.update({Action.retry_revocation, Action.keep_blocked})
            if _saved_for(state, inputs) is not None:
                actions.add(Action.remove_metadata)
            notice = _NOTICE_RECONFIGURING_TRUST
        return _Decision(
            kind="reconfiguring",
            actions=frozenset(actions),
            primary="Cancel",
            notice=notice,
        )

    if in_progress:
        return _Decision(
            kind="stopping", actions=frozenset(), primary="Stopping",
            notice=_NOTICE_STOPPING,
        )

    # Trust cleanup is independent of transport and display: no start, pair,
    # audio or resolution action can survive it.  A retained resource still gets
    # an explicit Stop so it can be released instead of trapping the user.
    if trust_uncertain:
        actions = {Action.retry_revocation, Action.keep_blocked}
        if _saved_for(state, inputs) is not None:
            actions.add(Action.remove_metadata)
        if orphans:
            actions.update({Action.confirm_orphan, Action.keep_orphan})
        if _needs_teardown(state) or _retained_display(state, ownership):
            actions.add(Action.stop)
        return _Decision(
            kind="trust_cleanup",
            actions=frozenset(actions),
            primary="Retry revocation",
            notice=_NOTICE_TRUST_CLEANUP,
        )

    if _resolution_mismatch(state):
        return _Decision(
            kind="mismatch",
            actions=frozenset({Action.recreate, Action.stop}),
            primary="Recreate",
            notice=_mismatch_notice(state),
        )

    phase = state.phase
    if phase is SessionPhase.ResolutionRequired:
        return _Decision(
            kind="resolution_required",
            actions=frozenset({Action.choose_resolution, Action.cancel}),
            primary="Choose",
            notice=_NOTICE_RESOLUTION_REQUIRED,
        )
    if phase is SessionPhase.AudioDecisionRequired:
        return _Decision(
            kind="audio_decision",
            actions=frozenset({Action.retry_audio, Action.video_only, Action.cancel}),
            primary="Retry audio",
            notice=_NOTICE_AUDIO_DECISION,
        )
    if phase is SessionPhase.CreatingDisplay:
        return _Decision(
            kind="creating",
            actions=frozenset({Action.cancel}),
            primary="Cancel",
            notice=_creating_notice(ownership),
        )
    if phase is SessionPhase.Pairing:
        owned = _display_owned(state)
        # A pairing that already retained an output is resolved only by submitting
        # the challenge or cancelling (which removes the output); a blind retry is
        # not the design's action for that row.
        actions = {Action.cancel} if owned else {Action.cancel, Action.retry}
        challenge = _actionable_challenge(state)
        if challenge is not None:
            actions.add(Action.submit_pairing)
            primary = (
                "Submit PIN"
                if challenge.credential_kind == "pin"
                else "Submit password"
            )
        else:
            primary = "Pair"
        return _Decision(
            kind="pairing",
            actions=frozenset(actions),
            primary=primary,
            notice=_NOTICE_PAIRING_OWNED if owned else _NOTICE_PAIRING,
        )
    if phase is SessionPhase.Connecting:
        return _Decision(
            kind="connecting",
            actions=frozenset({Action.stop}),
            primary="Stop",
            notice=None,
        )
    if phase in (SessionPhase.Mirroring, SessionPhase.VirtualDisplay):
        if state.audio is AudioState.Unavailable:
            return _Decision(
                kind="audio_unavailable",
                actions=frozenset({Action.retry_audio, Action.stop}),
                primary="Retry audio",
                notice=_NOTICE_AUDIO_UNAVAILABLE,
            )
        actions = {Action.stop}
        # Changing a retained output's resolution is a confirmed-recreation
        # transaction; it is not offered while trust is uncertain (handled above)
        # or while audio still needs an explicit decision.
        if phase is SessionPhase.VirtualDisplay:
            actions.add(Action.change_resolution)
        if _can_mute(state):
            actions.add(Action.mute)
        return _Decision(
            kind="virtual" if phase is SessionPhase.VirtualDisplay else "mirroring",
            actions=frozenset(actions),
            primary="Stop",
            notice=None,
        )
    if phase in (SessionPhase.Reconnecting, SessionPhase.Recovering):
        return _Decision(
            kind="recovering" if phase is SessionPhase.Recovering else "reconnecting",
            actions=frozenset({Action.retry, Action.stop}),
            primary="Retry",
            notice=_NOTICE_RECOVERING
            if phase is SessionPhase.Recovering
            else _NOTICE_RECONNECTING,
        )
    if phase is SessionPhase.Offline:
        actions = {Action.scan}
        if _saved_for(state, inputs) is not None:
            actions.add(Action.remove_metadata)
        if _needs_teardown(state):
            actions.add(Action.stop)
        return _Decision(
            kind="offline",
            actions=frozenset(actions),
            primary="Scan",
            notice=_NOTICE_OFFLINE,
        )
    if phase is SessionPhase.Failed:
        actions = {Action.retry, Action.stop}
        if orphans:
            actions.update({Action.confirm_orphan, Action.keep_orphan})
        return _Decision(
            kind="failed",
            actions=frozenset(actions),
            primary="Retry",
            notice=_NOTICE_FAILED,
        )
    if phase is SessionPhase.Available:
        if orphans:
            return _Decision(
                kind="orphan",
                actions=frozenset({Action.confirm_orphan, Action.keep_orphan}),
                primary="Remove",
                notice=_NOTICE_ORPHAN,
            )
        # A phase that looks idle must never hide a retained output: the only
        # safe controls are an explicit retry or a confirmed Stop.
        if _retained_display(state, ownership):
            return _Decision(
                kind="retained",
                actions=frozenset({Action.retry, Action.stop}),
                primary="Stop",
                notice=_NOTICE_RETAINED,
            )
        if _capability_blocked(state, inputs):
            return _Decision(
                kind="capability",
                actions=frozenset({Action.retry}),
                primary="Retry",
                notice=_NOTICE_CAPABILITY,
            )
        actions = {Action.scan}
        if _can_start(state, inputs):
            actions.add(Action.start)
        if _can_pair(state, inputs):
            actions.add(Action.pair)
        return _Decision(
            kind="available",
            actions=frozenset(actions),
            primary="Create display"
            if _visible_mode(state, inputs) is Mode.VIRTUAL
            else "Start",
            notice=_available_notice(state, inputs),
        )

    # An unknown or malformed phase is not a licence to offer anything.
    return _Decision(
        kind="unknown", actions=frozenset(), primary="Unavailable", notice=None
    )


# --------------------------------------------------------------------------- guards


_IDLE_PHASES: Final[frozenset[SessionPhase]] = frozenset(
    {SessionPhase.Available, SessionPhase.Offline, SessionPhase.Failed}
)

_ACTIVE_TRANSPORT: Final[frozenset[TransportState]] = frozenset(
    {
        TransportState.Pairing,
        TransportState.Connecting,
        TransportState.Streaming,
        TransportState.Reconnecting,
        TransportState.Stopping,
    }
)

_UNCERTAIN_TRUST: Final[frozenset[TrustState]] = frozenset(
    {TrustState.RevocationPending, TrustState.CleanupRequired}
)


def _trust_uncertain(state: SessionSnapshot) -> bool:
    """Trust is blocked independently of every transport/display fact."""
    return state.trust in _UNCERTAIN_TRUST or state.phase is SessionPhase.TrustCleanupRequired


def _cleanup_uncertain(state: SessionSnapshot) -> bool:
    """A display removal is unconfirmed, or cleanup is durably pending."""
    return (
        bool(state.cleanup_pending)
        or state.display is DisplayState.CleanupRequired
        or state.phase is SessionPhase.CleanupRequired
    )


def _reconfiguring(state: SessionSnapshot) -> bool:
    """A virtual output is being replaced; Cancel is the only progress control."""
    return (
        state.phase is SessionPhase.Reconfiguring
        or state.intent_stage is IntentStage.reconfiguring
        or state.display is DisplayState.Reconfiguring
    )


def _teardown_in_progress(state: SessionSnapshot) -> bool:
    """A bounded teardown is still running; no destructive control is offered."""
    if state.display is DisplayState.CleanupRequired:
        return False
    if state.intent_stage is IntentStage.cleanup:
        return False
    if _reconfiguring(state):
        return False
    if state.phase is SessionPhase.Stopping:
        return True
    if state.display is DisplayState.Removing:
        return True
    return bool(state.cleanup_pending and state.intent_stage is IntentStage.stopping)


def _session_owns_mode(state: SessionSnapshot) -> bool:
    """Whether any resource or intent still belongs to the current session."""
    return (
        bool(state.intent_active)
        or bool(state.cleanup_pending)
        or state.stream is not None
        or state.physical_source is not None
        or state.display_handle is not None
        or state.ownership_uuid is not None
        or state.display is not DisplayState.None_
        or state.transport in _ACTIVE_TRANSPORT
        or state.phase not in _IDLE_PHASES
    )


def _display_owned(state: SessionSnapshot) -> bool:
    return (
        state.display is DisplayState.Owned
        or state.display_handle is not None
        or state.ownership_uuid is not None
    )


def _has_ownership_evidence(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> bool:
    """Any durable ownership record or exact handle contradicts "no output"."""
    return (
        state.display_handle is not None
        or state.ownership_uuid is not None
        or ownership is not None
    )


def _retained_display(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> bool:
    """Whether a display resource, exact handle or durable record still exists."""
    return (
        state.display is not DisplayState.None_
        or _has_ownership_evidence(state, ownership)
    )


def _unreflected_ownership(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> bool:
    """A durable record exists that the snapshot's absent display does not show."""
    return (
        ownership is not None
        and state.display is DisplayState.None_
        and state.display_handle is None
        and state.ownership_uuid is None
    )


def _needs_teardown(state: SessionSnapshot) -> bool:
    """Whether an explicit Stop would still have an owned resource to release."""
    return (
        bool(state.intent_active)
        or bool(state.cleanup_pending)
        or state.stream is not None
        or state.physical_source is not None
        or state.display_handle is not None
        or state.ownership_uuid is not None
        or state.display is not DisplayState.None_
        or state.transport
        in (
            TransportState.Pairing,
            TransportState.Connecting,
            TransportState.Streaming,
            TransportState.Reconnecting,
        )
    )


def _resolution_mismatch(state: SessionSnapshot) -> bool:
    error = state.last_error
    return (
        state.accepted_resolution is not None
        and isinstance(error, Diagnostic)
        and error.code is ErrorCode.resolution_mismatch
    )


def _mode_supported(capabilities: CapabilityReport, mode: Mode) -> bool:
    return capabilities.mirror if mode is Mode.MIRROR else capabilities.virtual_display


def _capability_blocked(state: SessionSnapshot, inputs: ViewInputs) -> bool:
    return (
        state.phase is SessionPhase.Available
        and not _session_owns_mode(state)
        and not _mode_supported(inputs.capabilities, inputs.selected_mode)
    )


def _can_start(state: SessionSnapshot, inputs: ViewInputs) -> bool:
    return (
        bool(state.online)
        and state.target is not None
        and state.phase is SessionPhase.Available
        and not _session_owns_mode(state)
        and not _trust_uncertain(state)
        and not _cleanup_uncertain(state)
        and state.trust is not TrustState.Pairing
        and state.challenge is None
        and state.stream is None
        and state.physical_source is None
        and state.display_handle is None
        and state.ownership_uuid is None
        and state.display is DisplayState.None_
        and _mode_supported(inputs.capabilities, inputs.selected_mode)
    )


def _can_pair(state: SessionSnapshot, inputs: ViewInputs) -> bool:
    return (
        bool(inputs.capabilities.remembered_pairing)
        and bool(state.online)
        and state.target is not None
        and state.phase is SessionPhase.Available
        and not _session_owns_mode(state)
        and not _trust_uncertain(state)
        and not _cleanup_uncertain(state)
        and not state.intent_active
        and not state.cleanup_pending
        and state.challenge is None
        and state.stream is None
        and state.physical_source is None
        and state.display_handle is None
        and state.ownership_uuid is None
        and state.display is DisplayState.None_
        and state.trust in (TrustState.Unknown, TrustState.Untrusted)
    )


def _can_mute(state: SessionSnapshot) -> bool:
    return (
        state.phase in (SessionPhase.Mirroring, SessionPhase.VirtualDisplay)
        and state.stream is not None
        and state.audio in (AudioState.Enabled, AudioState.Muted)
        and not _trust_uncertain(state)
        and not _cleanup_uncertain(state)
    )


def _mode_enabled(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> bool:
    """The segmented mode control is locked for any owned, stopping or blocked session."""
    return (
        not _session_owns_mode(state)
        and not _trust_uncertain(state)
        and not _cleanup_uncertain(state)
        and not _unreflected_ownership(state, ownership)
        and not _teardown_in_progress(state)
        and state.trust is not TrustState.Pairing
        and state.challenge is None
    )


def _visible_mode(state: SessionSnapshot, inputs: ViewInputs) -> Mode:
    """Render only the active session's mode while it owns resources."""
    if _session_owns_mode(state) and state.mode is not None:
        return state.mode
    return inputs.selected_mode


def _source_enabled(state: SessionSnapshot, inputs: ViewInputs) -> bool:
    return (
        _visible_mode(state, inputs) is Mode.MIRROR
        and bool(state.online)
        and state.phase in (SessionPhase.Available, SessionPhase.Failed)
        and not _session_owns_mode(state)
        and not _trust_uncertain(state)
        and not _cleanup_uncertain(state)
        and _mode_supported(inputs.capabilities, Mode.MIRROR)
        and bool(inputs.physical_sources)
    )


def _resolution_enabled(state: SessionSnapshot, inputs: ViewInputs) -> bool:
    if _visible_mode(state, inputs) is not Mode.VIRTUAL:
        return False
    if (
        _trust_uncertain(state)
        or _cleanup_uncertain(state)
        or _teardown_in_progress(state)
    ):
        return False
    if state.phase is SessionPhase.ResolutionRequired:
        return True
    if state.phase is SessionPhase.VirtualDisplay:
        return True
    if state.phase in (SessionPhase.Available, SessionPhase.Failed) and not _session_owns_mode(state):
        return _mode_supported(inputs.capabilities, Mode.VIRTUAL)
    return False


def _actionable_challenge(state: SessionSnapshot) -> ChallengeView | None:
    """Expose a challenge only while the session lane still treats it as live."""
    if (
        state.challenge is None
        or state.trust is not TrustState.Pairing
        or state.phase is not SessionPhase.Pairing
        or _cleanup_uncertain(state)
        or _teardown_in_progress(state)
    ):
        return None
    return state.challenge


# --------------------------------------------------------------------------- values


def _require(snapshot: object, inputs: object) -> None:
    if not isinstance(snapshot, SessionSnapshot):
        raise TypeError("projection requires a SessionSnapshot")
    if not isinstance(inputs, ViewInputs):
        raise TypeError("projection requires ViewInputs")


def _freeze_sequence(owner: ViewInputs, name: str, kind: type) -> None:
    values = getattr(owner, name)
    try:
        normalized = tuple(values)
    except TypeError as error:  # pragma: no cover - defensive, tuple() of a scalar
        raise TypeError(f"{name} must be a sequence") from error
    for value in normalized:
        if not isinstance(value, kind):
            raise TypeError(f"{name} must contain only {kind.__name__} values")
    if not isinstance(values, tuple):
        object.__setattr__(owner, name, normalized)


def _ownership_for(
    state: SessionSnapshot, inputs: ViewInputs
) -> OwnershipRecord | None:
    record = inputs.ownership
    if record is None or record.receiver_id != state.receiver_id:
        return None
    return record


def _orphans_for(
    state: SessionSnapshot, inputs: ViewInputs
) -> tuple[RecoveryItem, ...]:
    return tuple(
        item for item in inputs.orphans if item.record.receiver_id == state.receiver_id
    )


def _saved_for(state: SessionSnapshot, inputs: ViewInputs) -> SavedReceiver | None:
    saved = inputs.saved
    if saved is None or saved.receiver_id != state.receiver_id:
        return None
    return saved


def _title(state: SessionSnapshot, inputs: ViewInputs) -> str:
    view = inputs.receiver
    if view is not None and view.record_set.receiver_id == state.receiver_id:
        if view.display_name:
            return view.display_name
    saved = _saved_for(state, inputs)
    if saved is not None and saved.display_name:
        return saved.display_name
    return str(state.receiver_id)


def _phase_label(state: SessionSnapshot) -> str:
    # The session lane derives ``TrustCleanupRequired`` ahead of the transport
    # states, so a snapshot that still carries an active transport label with
    # uncertain trust must not imply that pairing or a start is available.  A
    # real teardown or cleanup barrier keeps its own derived label; only the
    # trust barrier has to win over a transport-looking phase here.
    phase = state.phase
    if (
        _trust_uncertain(state)
        and not _cleanup_uncertain(state)
        and not _teardown_in_progress(state)
        and not _reconfiguring(state)
    ):
        phase = SessionPhase.TrustCleanupRequired
    return _PHASE_LABELS.get(phase, "Unavailable")


def _format_size(size: Size) -> str:
    return f"{size.width}x{size.height}"


def _selected_size(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> Size | None:
    """The best-known current output size, never a guess derived from a mode."""
    candidates: list[Size | None] = [
        state.observed_resolution,
        state.accepted_resolution,
        state.resolution.size if state.resolution is not None else None,
        state.display_handle.source.size if state.display_handle is not None else None,
        ownership.observed_size if ownership is not None else None,
        ownership.requested_size if ownership is not None else None,
    ]
    for size in candidates:
        if isinstance(size, Size):
            return size
    return None


def _row_size_label(
    state: SessionSnapshot, ownership: OwnershipRecord | None, mode: Mode
) -> str:
    if not _session_owns_mode(state) or mode is not Mode.VIRTUAL:
        return ""
    size = _selected_size(state, ownership)
    return _format_size(size) if size is not None else ""


def _available_notice(state: SessionSnapshot, inputs: ViewInputs) -> str | None:
    mode = _visible_mode(state, inputs)
    if mode is Mode.MIRROR and not inputs.physical_sources:
        return (
            "No physical monitor is available to capture. Connect a monitor and retry."
        )
    if mode is Mode.VIRTUAL and not inputs.resolution_choices:
        return (
            "The receiver did not provide a reliable resolution. Choose a supported "
            "size before creating the display."
        )
    return None


def _mismatch_notice(state: SessionSnapshot) -> str:
    accepted = state.accepted_resolution
    size = _format_size(accepted) if isinstance(accepted, Size) else "another size"
    return _NOTICE_MISMATCH.format(size=size)


def _creating_notice(ownership: OwnershipRecord | None) -> str:
    stage = ownership.stage if ownership is not None else "reserved"
    return _NOTICE_CREATING.format(stage=stage)


# --------------------------------------------------------------------------- badges


def _badges(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> tuple[Badge, ...]:
    badges = [
        _transport_badge(state),
        _display_badge(state, ownership),
        _audio_badge(state),
        _trust_badge(state),
    ]
    if not state.online:
        badges.append(Badge(resource="receiver", label="Offline", severity=SEVERITY_WARNING))
    return tuple(badges)


def _transport_badge(state: SessionSnapshot) -> Badge:
    label, severity = _TRANSPORT_LABELS.get(
        state.transport, ("Transport unknown", SEVERITY_ERROR)
    )
    return Badge(resource="transport", label=label, severity=severity)


def _display_badge(
    state: SessionSnapshot, ownership: OwnershipRecord | None
) -> Badge:
    known = _DISPLAY_LABELS.get(state.display)
    if known is not None:
        label, severity = known
        return Badge(resource="display", label=label, severity=severity)
    # ``DisplayState.None_`` is only "no output" when no ownership evidence
    # contradicts it; an unconfirmed record is never rendered as released.
    if _has_ownership_evidence(state, ownership):
        return Badge(
            resource="display", label="Ownership unconfirmed", severity=SEVERITY_ERROR
        )
    return Badge(resource="display", label="No output", severity=SEVERITY_INFO)


def _audio_badge(state: SessionSnapshot) -> Badge:
    label, severity = _AUDIO_LABELS.get(
        state.audio, ("Audio unknown", SEVERITY_ERROR)
    )
    return Badge(resource="audio", label=label, severity=severity)


def _trust_badge(state: SessionSnapshot) -> Badge:
    label, severity = _TRUST_LABELS.get(
        state.trust, ("Trust unknown", SEVERITY_WARNING)
    )
    return Badge(resource="trust", label=label, severity=severity)


# ----------------------------------------------------------------------- diagnostics


def _diagnostic(
    state: SessionSnapshot,
    inputs: ViewInputs,
    decision: _Decision,
    orphans: tuple[RecoveryItem, ...],
) -> Diagnostic | None:
    if decision.kind == "trust_cleanup":
        return state.last_error or _catalog(
            ErrorCode.trust_cleanup_required, state.receiver_id
        )
    if decision.kind == "cleanup":
        return state.last_error or _catalog(ErrorCode.cleanup_timeout, state.receiver_id)
    if decision.kind == "mismatch":
        return state.last_error or _catalog(
            ErrorCode.resolution_mismatch, state.receiver_id
        )
    if decision.kind == "audio_decision":
        return state.last_error or _catalog(
            ErrorCode.audio_unavailable, state.receiver_id
        )
    if decision.kind == "audio_unavailable":
        return state.last_error or _catalog(
            ErrorCode.audio_unavailable, state.receiver_id
        )
    if decision.kind == "capability":
        return _capability_diagnostic(state, inputs)
    if decision.kind == "orphan":
        return _orphan_diagnostic(state, orphans)
    return state.last_error


def _catalog(code: ErrorCode, receiver_id: ReceiverId) -> Diagnostic:
    """Build a catalog diagnostic, dropping a non-canonical identity from a broken feed."""
    try:
        return diagnostic(code, receiver_id)
    except (TypeError, ValueError):
        return diagnostic(code)


def _orphan_diagnostic(
    state: SessionSnapshot, orphans: tuple[RecoveryItem, ...]
) -> Diagnostic:
    for item in orphans:
        if item.diagnostic is not None:
            return item.diagnostic
    return _catalog(ErrorCode.ownership_mismatch, state.receiver_id)


def _capability_diagnostic(
    state: SessionSnapshot, inputs: ViewInputs
) -> Diagnostic:
    """The report's most specific frozen failure for the selected mode.

    The catalog message names an exact missing command whenever the probe could.
    Ranking keeps the pane from blaming an unrelated dependency: a failure whose
    detail names this mode's own provider comes first, then any named missing
    command, then a broader incompatibility.
    """
    report = inputs.capabilities
    keywords = _MODE_KEYWORDS[inputs.selected_mode]
    for item in report.diagnostics:
        detail = (item.detail or "").lower()
        if any(word in detail for word in keywords):
            return item
    for item in report.diagnostics:
        if item.code is ErrorCode.dependency_missing:
            return item
    for item in report.diagnostics:
        if item.code is ErrorCode.dependency_incompatible:
            return item
    if report.diagnostics:
        return report.diagnostics[0]
    return _catalog(ErrorCode.unsupported_operation, state.receiver_id)


__all__ = [
    "Action",
    "Badge",
    "PaneState",
    "ReceiverRow",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_OK",
    "SEVERITY_WARNING",
    "ViewInputs",
    "project",
    "project_row",
]
