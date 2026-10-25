"""Safety matrix for the pure UI projection.

Every row of the approved design's UI state matrix is built from the real domain
DTOs, including the phases the session lane actually derives for those resource
combinations.  Each row asserts the exact action set, the selector lockout and
the absence of unsafe controls, so a projection that merely *looks* reasonable
cannot pass: an unknown or unconfirmed resource must never be presented as
released, and no mode action may be offered while a session owns resources.

The tests are deliberately pure: no Qt, no session implementation, no OS
adapter, no clock and no I/O.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverRecordSet,
    ReceiverView,
)
from airplay_linux.domain.operations import (
    ChallengeId,
    ChallengeView,
    InstanceId,
    LeaseId,
    OperationId,
    OwnerId,
    ProcessIdentity,
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
    AudioState,
    CapabilityReport,
    DisplayState,
    ErrorCode,
    IntentStage,
    Mode,
    ResolutionDecision,
    ResolutionEvidence,
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
HELPER = ProcessIdentity(pid=4001, start_ticks=90001)


# --------------------------------------------------------------------------- fixtures


def capabilities(**overrides: object) -> CapabilityReport:
    """A complete, compatible startup report: every operation positively proven."""
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


def record_set(receiver_id: ReceiverId = RECEIVER) -> ReceiverRecordSet:
    return ReceiverRecordSet(
        records=(service(),),
        receiver_id=receiver_id,
        identity_kind="device",
        canonical_address="192.0.2.10",
        endpoint_fingerprint="endpoint-a-v1",
        endpoint_generation=1,
        persistable=True,
        continuity_verified=True,
        replacement_of=None,
        ambiguous=False,
    )


def receiver_view(online: bool = True, receiver_id: ReceiverId = RECEIVER) -> ReceiverView:
    return ReceiverView(
        record_set=record_set(receiver_id),
        display_name="Living Room",
        online=online,
        pairing_hint=None,
    )


def saved_receiver(**overrides: object) -> SavedReceiver:
    defaults: dict[str, object] = {
        "receiver_id": RECEIVER,
        "service_metadata": (service(),),
        "display_name": "Living Room",
        "last_endpoint": target(),
        "continuity_fingerprint": "endpoint-a-v1",
        "credential_ref": None,
        "remembered_resolution": None,
    }
    return SavedReceiver(**(defaults | overrides))  # type: ignore[arg-type]


def decision(size: Size = HD) -> ResolutionDecision:
    return ResolutionDecision(
        size=size,
        source="user",
        evidence=ResolutionEvidence(
            origin="picker",
            reliable=True,
            receiver_id=RECEIVER,
            endpoint_fingerprint="endpoint-a-v1",
        ),
    )


def stream_handle(**overrides: object) -> StreamHandle:
    defaults: dict[str, object] = {
        "receiver_id": RECEIVER,
        "endpoint_generation": 1,
        "endpoint_fingerprint": "endpoint-a-v1",
        "backend_stream_id": "stream-1",
        "daemon_instance_uuid": InstanceId(UUID(int=7)),
        "lease_id": LeaseId(UUID(int=8)),
        "start_operation_id": OperationId(UUID(int=9)),
    }
    return StreamHandle(**(defaults | overrides))  # type: ignore[arg-type]


def challenge(**overrides: object) -> ChallengeView:
    defaults: dict[str, object] = {
        "challenge_id": ChallengeId(UUID(int=11)),
        "receiver_id": RECEIVER,
        "endpoint_fingerprint": "endpoint-a-v1",
        "endpoint_generation": 1,
        "operation_id": OperationId(UUID(int=12)),
        "session_generation": 1,
        "trust_generation": 1,
        "created_at": 0.0,
        "expires_at": 120.0,
        "credential_kind": "pin",
    }
    return ChallengeView(**(defaults | overrides))  # type: ignore[arg-type]


def ownership(stage: str = "published", **overrides: object) -> OwnershipRecord:
    observed = stage in {"output_observed", "published", "removing"}
    defaults: dict[str, object] = {
        "schema_version": 1,
        "owner_uuid": OwnerId(UUID(int=1)),
        "app_instance_uuid": InstanceId(UUID(int=1)),
        "runtime_directory": "/run/user/1000/airplay-linux/instances/1",
        "creation_operation_id": OperationId(UUID(int=2)),
        "receiver_id": RECEIVER,
        "endpoint_fingerprint": "endpoint-a-v1",
        "desired_state": "active",
        "stage": stage,
        "requested_size": HD,
        "observed_size": HD if observed else None,
        "helper": None if stage == "reserved" else HELPER,
        "connector": "Virtual-v1" if observed else None,
        "output_id": "v1" if observed else None,
        "created_utc": "2026-10-17T00:00:00Z",
        "last_configuration_step": stage,
        "baseline": LayoutSnapshot(outputs={}, baseline_hash="baseline"),
        "last_applied": {},
        "provider_version": "24.08.2",
        "capability_fingerprint": "fingerprint",
    }
    return OwnershipRecord(**(defaults | overrides))  # type: ignore[arg-type]


def inputs(**overrides: object) -> ViewInputs:
    """A complete compatible ``ViewInputs`` for a selected receiver."""
    defaults: dict[str, object] = {
        "receiver": receiver_view(),
        "saved": saved_receiver(),
        "capabilities": capabilities(),
        "selected_mode": Mode.MIRROR,
        "physical_sources": (physical(),),
        "resolution_choices": (HD, SMALL),
    }
    return ViewInputs(**(defaults | overrides))  # type: ignore[arg-type]


def idle(**overrides: object) -> SessionSnapshot:
    """An online, idle receiver whose derived phase is ``Available``."""
    defaults: dict[str, object] = {
        "receiver_id": RECEIVER,
        "target": target(),
        "online": True,
        "phase": SessionPhase.Available,
    }
    return SessionSnapshot(**(defaults | overrides))  # type: ignore[arg-type]


# The brief's canonical example uses these exact fixture names.
@pytest.fixture(name="idle_snapshot")
def idle_snapshot_fixture() -> SessionSnapshot:
    return idle()


@pytest.fixture(name="view_inputs")
def view_inputs_fixture() -> ViewInputs:
    return inputs()


# --------------------------------------------------------------------------- matrix


@dataclass(frozen=True)
class Row:
    name: str
    state: SessionSnapshot
    inputs: ViewInputs
    actions: frozenset[Action]
    forbidden: frozenset[Action]
    mode_enabled: bool
    phase_label: str
    primary: str
    notice_contains: str | None = None


_STARTING_ACTIONS = frozenset(
    {Action.start, Action.pair, Action.choose_resolution, Action.video_only}
)


def _mirroring_state() -> SessionSnapshot:
    return idle(
        phase=SessionPhase.Mirroring,
        mode=Mode.MIRROR,
        intent_active=True,
        intent_stage=IntentStage.active,
        transport=TransportState.Streaming,
        physical_source=physical(),
        stream=stream_handle(),
        audio=AudioState.Enabled,
        trust=TrustState.Trusted,
    )


def _virtual_state() -> SessionSnapshot:
    return idle(
        phase=SessionPhase.VirtualDisplay,
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        transport=TransportState.Streaming,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
        stream=stream_handle(),
        audio=AudioState.Muted,
        muted=True,
        observed_resolution=HD,
        resolution=decision(HD),
        trust=TrustState.Trusted,
    )


def _owned_virtual(**overrides: object) -> SessionSnapshot:
    defaults: dict[str, object] = {
        "phase": SessionPhase.Reconnecting,
        "mode": Mode.VIRTUAL,
        "intent_active": True,
        "intent_stage": IntentStage.active,
        "transport": TransportState.Reconnecting,
        "display": DisplayState.Owned,
        "display_handle": virtual_handle(),
        "ownership_uuid": OwnerId(UUID(int=1)),
        "audio": AudioState.Disabled,
        "observed_resolution": HD,
        "resolution": decision(HD),
    }
    return idle(**(defaults | overrides))  # type: ignore[arg-type]


ROWS: tuple[Row, ...] = (
    Row(
        name="available",
        state=idle(),
        inputs=inputs(),
        actions=frozenset({Action.start, Action.pair, Action.scan}),
        forbidden=frozenset({Action.stop, Action.cancel, Action.mute, Action.retry}),
        mode_enabled=True,
        phase_label="Available",
        primary="Start",
    ),
    Row(
        name="available-virtual",
        state=idle(),
        inputs=inputs(selected_mode=Mode.VIRTUAL),
        actions=frozenset({Action.start, Action.pair, Action.scan}),
        forbidden=frozenset({Action.stop, Action.cancel, Action.mute}),
        mode_enabled=True,
        phase_label="Available",
        primary="Create display",
    ),
    Row(
        name="offline",
        state=idle(phase=SessionPhase.Offline, online=False),
        inputs=inputs(receiver=None, saved=saved_receiver()),
        actions=frozenset({Action.scan, Action.remove_metadata}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute, Action.retry},
        mode_enabled=True,
        phase_label="Offline",
        primary="Scan",
        notice_contains="offline",
    ),
    Row(
        name="recovering-owned",
        state=_owned_virtual(phase=SessionPhase.Recovering, intent_stage=IntentStage.recovering),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.retry, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.change_resolution},
        mode_enabled=False,
        phase_label="Recovering",
        primary="Retry",
        notice_contains="retained",
    ),
    Row(
        name="pairing-none",
        state=idle(
            phase=SessionPhase.Pairing,
            trust=TrustState.Pairing,
            challenge=challenge(),
            transport=TransportState.Pairing,
        ),
        inputs=inputs(),
        actions=frozenset({Action.submit_pairing, Action.cancel, Action.retry}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute},
        mode_enabled=False,
        phase_label="Pairing",
        primary="Submit PIN",
    ),
    Row(
        name="pairing-owned",
        state=idle(
            phase=SessionPhase.Pairing,
            trust=TrustState.Pairing,
            challenge=challenge(),
            transport=TransportState.Idle,
            mode=Mode.VIRTUAL,
            intent_active=True,
            display=DisplayState.Owned,
            display_handle=virtual_handle(),
            ownership_uuid=OwnerId(UUID(int=1)),
            observed_resolution=HD,
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.submit_pairing, Action.cancel}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute, Action.retry},
        mode_enabled=False,
        phase_label="Pairing",
        primary="Submit PIN",
        notice_contains="removes",
    ),
    Row(
        name="resolution-required",
        state=idle(
            phase=SessionPhase.ResolutionRequired,
            mode=Mode.VIRTUAL,
            intent_active=True,
            intent_stage=IntentStage.resolution,
            transport=TransportState.Idle,
            display=DisplayState.Resolving,
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL),
        actions=frozenset({Action.choose_resolution, Action.cancel}),
        forbidden=frozenset({Action.start, Action.pair, Action.stop, Action.mute}),
        mode_enabled=False,
        phase_label="Resolution required",
        primary="Choose",
        notice_contains="size",
    ),
    Row(
        name="creating",
        state=idle(
            phase=SessionPhase.CreatingDisplay,
            mode=Mode.VIRTUAL,
            intent_active=True,
            intent_stage=IntentStage.starting,
            transport=TransportState.Idle,
            display=DisplayState.Creating,
        ),
        inputs=inputs(
            selected_mode=Mode.VIRTUAL, ownership=ownership(stage="helper_started")
        ),
        actions=frozenset({Action.cancel}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute},
        mode_enabled=False,
        phase_label="Creating display",
        primary="Cancel",
        notice_contains="helper_started",
    ),
    Row(
        name="connecting",
        state=idle(
            phase=SessionPhase.Connecting,
            mode=Mode.MIRROR,
            intent_active=True,
            intent_stage=IntentStage.starting,
            transport=TransportState.Connecting,
            physical_source=physical(),
            audio=AudioState.Starting,
        ),
        inputs=inputs(),
        actions=frozenset({Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.cancel, Action.retry},
        mode_enabled=False,
        phase_label="Connecting",
        primary="Stop",
    ),
    Row(
        name="mirroring",
        state=_mirroring_state(),
        inputs=inputs(),
        actions=frozenset({Action.mute, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.cancel, Action.retry},
        mode_enabled=False,
        phase_label="Mirroring",
        primary="Stop",
    ),
    Row(
        name="virtual",
        state=_virtual_state(),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.mute, Action.stop, Action.change_resolution}),
        forbidden=_STARTING_ACTIONS | {Action.cancel, Action.retry},
        mode_enabled=False,
        phase_label="Virtual display",
        primary="Stop",
    ),
    Row(
        name="reconnecting",
        state=_owned_virtual(),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.retry, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.change_resolution},
        mode_enabled=False,
        phase_label="Reconnecting",
        primary="Retry",
        notice_contains="retained",
    ),
    Row(
        name="reconfiguring",
        state=_owned_virtual(
            phase=SessionPhase.Reconfiguring,
            intent_stage=IntentStage.reconfiguring,
            transport=TransportState.Idle,
            display=DisplayState.Reconfiguring,
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.cancel}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute, Action.retry},
        mode_enabled=False,
        phase_label="Reconfiguring",
        primary="Cancel",
        notice_contains="layout",
    ),
    Row(
        name="resolution-mismatch",
        state=_owned_virtual(
            phase=SessionPhase.Available,
            intent_stage=IntentStage.active,
            transport=TransportState.Idle,
            stream=None,
            accepted_resolution=SMALL,
            last_error=diagnostic(ErrorCode.resolution_mismatch),
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.recreate, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.cancel, Action.retry},
        mode_enabled=False,
        phase_label="Available",
        primary="Recreate",
        notice_contains="1280x720",
    ),
    Row(
        name="audio-decision",
        state=_owned_virtual(
            phase=SessionPhase.AudioDecisionRequired,
            intent_stage=IntentStage.audio_decision,
            transport=TransportState.Idle,
            stream=None,
            audio=AudioState.Unavailable,
            last_error=diagnostic(ErrorCode.audio_denied),
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.retry_audio, Action.video_only, Action.cancel}),
        forbidden=frozenset({Action.start, Action.stop, Action.mute, Action.pair}),
        mode_enabled=False,
        phase_label="Audio decision required",
        primary="Retry audio",
    ),
    Row(
        name="audio-unavailable-video-active",
        state=replace(
            _mirroring_state(),
            audio=AudioState.Unavailable,
            last_error=diagnostic(ErrorCode.audio_unavailable),
        ),
        inputs=inputs(),
        actions=frozenset({Action.retry_audio, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.cancel},
        mode_enabled=False,
        phase_label="Mirroring",
        primary="Retry audio",
        notice_contains="mute",
    ),
    Row(
        name="capability-failure",
        state=idle(),
        inputs=inputs(
            capabilities=capabilities(
                mirror=False,
                diagnostics=(
                    replace(
                        diagnostic(ErrorCode.dependency_missing, command="doubletake"),
                        detail="foreground_control",
                    ),
                ),
            )
        ),
        actions=frozenset({Action.retry}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute},
        mode_enabled=True,
        phase_label="Available",
        primary="Retry",
    ),
    Row(
        name="orphan-candidate",
        state=idle(display=DisplayState.None_),
        inputs=inputs(
            orphans=(
                RecoveryItem(
                    kind="orphan",
                    record=ownership(),
                    handle=virtual_handle(),
                    diagnostic=diagnostic(ErrorCode.ownership_mismatch),
                ),
            )
        ),
        actions=frozenset({Action.confirm_orphan, Action.keep_orphan}),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute},
        mode_enabled=True,
        phase_label="Available",
        primary="Remove",
        notice_contains="confirmation",
    ),
    Row(
        name="cleanup-required",
        state=idle(
            phase=SessionPhase.CleanupRequired,
            mode=Mode.VIRTUAL,
            intent_active=False,
            intent_stage=IntentStage.cleanup,
            cleanup_pending=True,
            transport=TransportState.Failed,
            display=DisplayState.CleanupRequired,
            ownership_uuid=OwnerId(UUID(int=1)),
            last_error=diagnostic(ErrorCode.cleanup_timeout),
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.retry_cleanup, Action.keep_cleanup, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.retry},
        mode_enabled=False,
        phase_label="Cleanup required",
        primary="Retry cleanup",
        notice_contains="manual cleanup",
    ),
    Row(
        name="trust-cleanup-required",
        state=idle(
            phase=SessionPhase.TrustCleanupRequired,
            trust=TrustState.CleanupRequired,
            transport=TransportState.Idle,
            display=DisplayState.None_,
        ),
        inputs=inputs(),
        actions=frozenset(
            {Action.retry_revocation, Action.keep_blocked, Action.remove_metadata}
        ),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute, Action.submit_pairing},
        mode_enabled=False,
        phase_label="Trust cleanup required",
        primary="Retry revocation",
        notice_contains="blocked",
    ),
    Row(
        name="trust-revoking-owned",
        state=_owned_virtual(
            phase=SessionPhase.TrustCleanupRequired,
            transport=TransportState.Idle,
            trust=TrustState.RevocationPending,
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset(
            {
                Action.retry_revocation,
                Action.keep_blocked,
                Action.remove_metadata,
                Action.stop,
            }
        ),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.change_resolution},
        mode_enabled=False,
        phase_label="Trust cleanup required",
        primary="Retry revocation",
        notice_contains="blocked",
    ),
    Row(
        name="failed",
        state=_owned_virtual(
            phase=SessionPhase.Failed,
            transport=TransportState.Failed,
            trust=TrustState.Trusted,
            last_error=diagnostic(ErrorCode.transport_failed),
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
        actions=frozenset({Action.retry, Action.stop}),
        forbidden=_STARTING_ACTIONS | {Action.mute, Action.change_resolution},
        mode_enabled=False,
        phase_label="Failed",
        primary="Retry",
        notice_contains="cleanup",
    ),
    Row(
        name="stopping",
        state=idle(
            phase=SessionPhase.Stopping,
            mode=Mode.VIRTUAL,
            intent_active=False,
            intent_stage=IntentStage.stopping,
            cleanup_pending=True,
            transport=TransportState.Stopping,
            display=DisplayState.Removing,
            display_handle=virtual_handle(),
            ownership_uuid=OwnerId(UUID(int=1)),
            stream=stream_handle(),
        ),
        inputs=inputs(selected_mode=Mode.VIRTUAL, ownership=ownership(stage="removing")),
        actions=frozenset(),
        forbidden=_STARTING_ACTIONS | {Action.stop, Action.mute, Action.cancel},
        mode_enabled=False,
        phase_label="Stopping",
        primary="Stopping",
        notice_contains="removal",
    ),
)


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_ui_state_matrix(row: Row) -> None:
    pane = project(row.state, row.inputs)

    assert pane.actions == row.actions
    assert pane.actions.isdisjoint(row.forbidden)
    assert pane.mode_enabled is row.mode_enabled
    assert pane.phase_label == row.phase_label
    assert pane.primary_label == row.primary
    assert [badge.resource for badge in pane.badges[:4]] == [
        "transport",
        "display",
        "audio",
        "trust",
    ]
    if row.notice_contains is not None:
        assert pane.notice is not None
        assert row.notice_contains.lower() in pane.notice.lower()


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_locked_selector_never_offers_a_start_or_pair(row: Row) -> None:
    pane = project(row.state, row.inputs)
    if not pane.mode_enabled:
        assert Action.start not in pane.actions
        assert Action.pair not in pane.actions


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_mute_is_only_offered_for_confirmed_active_audio(row: Row) -> None:
    pane = project(row.state, row.inputs)
    if Action.mute in pane.actions:
        assert pane.mute_enabled is True
        assert row.state.audio in (AudioState.Enabled, AudioState.Muted)
        assert row.state.phase in (SessionPhase.Mirroring, SessionPhase.VirtualDisplay)
    else:
        assert pane.mute_enabled is False


@pytest.mark.parametrize("row", ROWS, ids=[row.name for row in ROWS])
def test_every_row_exposes_authoritative_resource_badges(row: Row) -> None:
    pane = project(row.state, row.inputs)
    assert len(pane.badges) >= 4
    assert {badge.resource for badge in pane.badges} >= {
        "transport",
        "display",
        "audio",
        "trust",
    }
    for badge in pane.badges:
        assert isinstance(badge, Badge)
        assert badge.resource and badge.label
        assert badge.severity in {"ok", "info", "warning", "error"}


# ------------------------------------------------------- trust cleanup outranks start


def test_uncertain_trust_disables_mode_and_start(idle_snapshot, view_inputs):
    pane = project(replace(idle_snapshot, trust=TrustState.CleanupRequired), view_inputs)
    assert not pane.mode_enabled
    assert Action.start not in pane.actions
    assert Action.retry_revocation in pane.actions


def test_revocation_pending_blocks_start_even_with_a_live_session() -> None:
    state = _mirroring_state()
    state = replace(state, trust=TrustState.RevocationPending)
    pane = project(state, inputs())

    assert not pane.mode_enabled
    assert pane.actions.isdisjoint({Action.start, Action.mute, Action.pair})
    assert Action.retry_revocation in pane.actions
    assert Action.keep_blocked in pane.actions
    assert pane.phase_label == "Trust cleanup required"


def test_trust_cleanup_is_not_reported_as_released() -> None:
    state = idle(
        phase=SessionPhase.TrustCleanupRequired,
        trust=TrustState.CleanupRequired,
    )
    pane = project(state, inputs())
    trust = next(badge for badge in pane.badges if badge.resource == "trust")

    assert trust.label == "Trust cleanup required"
    assert trust.severity == "error"
    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.trust_cleanup_required


def test_stale_challenge_is_never_exposed_while_trust_is_uncertain() -> None:
    state = idle(
        phase=SessionPhase.TrustCleanupRequired,
        trust=TrustState.CleanupRequired,
        challenge=challenge(),
        transport=TransportState.Pairing,
    )
    pane = project(state, inputs())

    assert pane.challenge is None
    assert Action.submit_pairing not in pane.actions
    assert Action.retry_revocation in pane.actions


def test_challenge_is_only_exposed_in_the_pairing_presentation() -> None:
    stale = idle(
        phase=SessionPhase.Available,
        trust=TrustState.Pairing,
        challenge=challenge(),
    )
    pane = project(stale, inputs())
    assert pane.challenge is None
    assert Action.submit_pairing not in pane.actions

    pairing = replace(stale, phase=SessionPhase.Pairing, transport=TransportState.Pairing)
    pane = project(pairing, inputs())
    assert pane.challenge == challenge()
    assert Action.submit_pairing in pane.actions


# ---------------------------------------------------------------- unconfirmed display


def test_unconfirmed_display_is_never_badged_as_released() -> None:
    state = idle(
        phase=SessionPhase.CleanupRequired,
        intent_stage=IntentStage.cleanup,
        cleanup_pending=True,
        display=DisplayState.CleanupRequired,
        ownership_uuid=OwnerId(UUID(int=1)),
        transport=TransportState.Failed,
    )
    pane = project(state, inputs(ownership=ownership()))
    display = next(badge for badge in pane.badges if badge.resource == "display")

    assert display.label == "Cleanup unconfirmed"
    assert display.severity == "error"
    assert display.label != "No output"
    assert Action.start not in pane.actions
    assert Action.retry_cleanup in pane.actions


def test_ownership_evidence_without_a_display_state_is_not_released() -> None:
    state = idle(display=DisplayState.None_, display_handle=virtual_handle())
    pane = project(state, inputs(ownership=ownership()))
    display = next(badge for badge in pane.badges if badge.resource == "display")

    assert display.label == "Ownership unconfirmed"
    assert display.severity == "error"
    assert not pane.mode_enabled
    assert Action.start not in pane.actions


def test_failed_display_is_not_reported_as_absent() -> None:
    state = _owned_virtual(phase=SessionPhase.Failed, display=DisplayState.Failed,
                           transport=TransportState.Failed, stream=None)
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))
    display = next(badge for badge in pane.badges if badge.resource == "display")

    assert display.label == "Output failed"
    assert display.severity == "error"


def test_unknown_resources_are_not_badged_as_released() -> None:
    state = idle(trust=TrustState.Unknown)
    pane = project(state, inputs())
    labels = {badge.resource: badge.label for badge in pane.badges}

    assert labels["trust"] == "Trust unknown"
    assert labels["trust"] not in {"Not paired", "Paired"}
    assert labels["display"] == "No output"
    assert labels["audio"] == "Audio off"


# ------------------------------------------------------------------------- audio rows


def test_audio_unavailable_after_readiness_keeps_video_and_disables_mute() -> None:
    state = replace(
        _mirroring_state(),
        audio=AudioState.Unavailable,
        last_error=diagnostic(ErrorCode.audio_unavailable),
    )
    pane = project(state, inputs())

    assert pane.phase_label == "Mirroring"
    assert pane.actions == frozenset({Action.retry_audio, Action.stop})
    assert Action.mute not in pane.actions
    assert pane.mute_enabled is False
    audio = next(badge for badge in pane.badges if badge.resource == "audio")
    assert audio.label == "Audio unavailable"
    assert audio.severity == "error"
    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.audio_unavailable


def test_audio_unavailable_on_a_virtual_session_does_not_offer_reconfiguration() -> None:
    state = replace(
        _virtual_state(),
        audio=AudioState.Unavailable,
        last_error=diagnostic(ErrorCode.audio_unavailable),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert pane.phase_label == "Virtual display"
    assert pane.actions == frozenset({Action.retry_audio, Action.stop})
    assert pane.mute_enabled is False


def test_audio_decision_offers_only_explicit_choices() -> None:
    state = _owned_virtual(
        phase=SessionPhase.AudioDecisionRequired,
        intent_stage=IntentStage.audio_decision,
        transport=TransportState.Idle,
        stream=None,
        audio=AudioState.Unavailable,
        last_error=diagnostic(ErrorCode.audio_denied),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert pane.actions == frozenset(
        {Action.retry_audio, Action.video_only, Action.cancel}
    )
    assert Action.stop not in pane.actions
    assert Action.mute not in pane.actions
    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.audio_denied


# --------------------------------------------------------------- resolution mismatch


def test_resolution_mismatch_offers_recreate_at_the_accepted_size_and_stop() -> None:
    state = _owned_virtual(
        phase=SessionPhase.Available,
        transport=TransportState.Idle,
        stream=None,
        accepted_resolution=SMALL,
        last_error=diagnostic(ErrorCode.resolution_mismatch),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert pane.actions == frozenset({Action.recreate, Action.stop})
    assert Action.start not in pane.actions
    assert Action.change_resolution not in pane.actions
    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.resolution_mismatch
    assert pane.notice is not None and "1280x720" in pane.notice
    display = next(badge for badge in pane.badges if badge.resource == "display")
    assert display.label == "Output owned"


def test_a_mismatch_still_stopping_the_old_stream_is_not_recreatable() -> None:
    state = _owned_virtual(
        phase=SessionPhase.Stopping,
        transport=TransportState.Stopping,
        stream=stream_handle(),
        accepted_resolution=SMALL,
        last_error=diagnostic(ErrorCode.resolution_mismatch),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert pane.actions == frozenset()
    assert Action.recreate not in pane.actions
    assert pane.mode_enabled is False


# ---------------------------------------------------------------- capability failure


def test_capability_failure_names_the_missing_command_and_offers_retry() -> None:
    state = idle()
    report = capabilities(
        virtual_display=False,
        diagnostics=(
            replace(
                diagnostic(ErrorCode.dependency_missing, command="kscreen-doctor"),
                detail="kscreen-doctor",
            ),
        ),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, capabilities=report))

    assert pane.actions == frozenset({Action.retry})
    assert Action.start not in pane.actions
    assert pane.mode_enabled is True
    assert pane.diagnostic is not None
    assert "kscreen-doctor" in pane.diagnostic.message
    assert pane.notice is not None and "unavailable" in pane.notice


def test_capability_diagnostic_prefers_the_modes_own_provider() -> None:
    report = capabilities(
        virtual_display=False,
        diagnostics=(
            # A shared backend audio failure must never be blamed for a missing
            # virtual-display provider.
            replace(diagnostic(ErrorCode.audio_unavailable), detail="doubletake audio"),
            replace(
                diagnostic(ErrorCode.dependency_missing, command="kscreen-doctor"),
                detail="kscreen-doctor",
            ),
            replace(diagnostic(ErrorCode.dependency_incompatible), detail="virtual_source"),
        ),
    )
    pane = project(
        idle(), inputs(selected_mode=Mode.VIRTUAL, capabilities=report)
    )

    assert pane.diagnostic is not None
    assert "kscreen-doctor" in pane.diagnostic.message


def test_mirror_capability_failure_names_the_physical_provider() -> None:
    report = capabilities(
        mirror=False,
        diagnostics=(
            replace(diagnostic(ErrorCode.dependency_incompatible), detail="virtual_source"),
            replace(diagnostic(ErrorCode.dependency_incompatible), detail="physical_source"),
        ),
    )
    pane = project(idle(), inputs(capabilities=report))

    assert pane.diagnostic is not None
    assert pane.diagnostic.detail == "physical_source"


def test_capability_failure_only_affects_that_receivers_selected_mode() -> None:
    report = capabilities(
        mirror=False,
        diagnostics=(
            replace(
                diagnostic(ErrorCode.dependency_missing, command="doubletake"),
                detail="foreground_control",
            ),
        ),
    )
    blocked = project(idle(), inputs(capabilities=report))
    assert Action.start not in blocked.actions

    virtual = project(
        idle(), inputs(selected_mode=Mode.VIRTUAL, capabilities=report)
    )
    assert Action.start in virtual.actions

    other = project(
        idle(receiver_id=OTHER_RECEIVER, target=target(receiver="b")),
        inputs(
            receiver=receiver_view(receiver_id=OTHER_RECEIVER),
            saved=saved_receiver(receiver_id=OTHER_RECEIVER),
        ),
    )
    assert Action.start in other.actions


# ------------------------------------------------------------------------- offline row


def test_offline_shows_last_known_identity_and_permits_scan_and_metadata_removal() -> None:
    state = idle(phase=SessionPhase.Offline, online=False)
    pane = project(state, inputs(receiver=None, saved=saved_receiver()))

    assert pane.title == "Living Room"
    assert pane.actions == frozenset({Action.scan, Action.remove_metadata})
    assert Action.start not in pane.actions
    assert Action.pair not in pane.actions
    assert pane.primary_label == "Scan"
    assert pane.source_enabled is False
    assert pane.resolution_enabled is False
    assert pane.mute_enabled is False
    receiver = next(badge for badge in pane.badges if badge.resource == "receiver")
    assert receiver.label == "Offline"


def test_offline_without_saved_metadata_only_offers_scan() -> None:
    state = idle(phase=SessionPhase.Offline, online=False)
    pane = project(state, inputs(receiver=None, saved=None))

    assert pane.actions == frozenset({Action.scan})
    assert Action.remove_metadata not in pane.actions


# ------------------------------------------------------------------------- pairing rows


def test_pairing_with_owned_output_keeps_the_retained_badge() -> None:
    state = idle(
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        challenge=challenge(),
        transport=TransportState.Idle,
        mode=Mode.VIRTUAL,
        intent_active=True,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))
    display = next(badge for badge in pane.badges if badge.resource == "display")

    assert display.label == "Output owned"
    assert pane.challenge is not None
    assert pane.notice is not None and "removes" in pane.notice
    assert Action.cancel in pane.actions
    assert Action.stop not in pane.actions


# ------------------------------------------------------------------------ orphan rows


def test_orphan_cleanup_requires_confirmation_and_offers_keep() -> None:
    item = RecoveryItem(
        kind="orphan",
        record=ownership(),
        handle=virtual_handle(),
        diagnostic=diagnostic(ErrorCode.ownership_mismatch),
    )
    pane = project(idle(), inputs(orphans=(item,)))

    assert Action.confirm_orphan in pane.actions
    assert Action.keep_orphan in pane.actions
    assert pane.actions == frozenset({Action.confirm_orphan, Action.keep_orphan})
    assert Action.stop not in pane.actions
    assert pane.notice is not None and "confirmation" in pane.notice
    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.ownership_mismatch


def test_orphans_for_another_receiver_do_not_change_this_pane() -> None:
    item = RecoveryItem(
        kind="orphan",
        record=ownership(receiver_id=OTHER_RECEIVER),
        handle=virtual_handle(),
        diagnostic=diagnostic(ErrorCode.ownership_mismatch),
    )
    pane = project(idle(), inputs(orphans=(item,)))

    assert pane.actions == frozenset({Action.start, Action.pair, Action.scan})
    assert Action.confirm_orphan not in pane.actions


def test_ownership_evidence_for_another_receiver_is_ignored() -> None:
    pane = project(
        idle(),
        inputs(ownership=ownership(receiver_id=OTHER_RECEIVER)),
    )

    assert pane.actions == frozenset({Action.start, Action.pair, Action.scan})
    assert pane.notice is None


# ---------------------------------------------------------------- cleanup and failure


def test_cleanup_required_never_claims_release_and_keeps_a_way_forward() -> None:
    state = idle(
        phase=SessionPhase.CleanupRequired,
        intent_stage=IntentStage.cleanup,
        cleanup_pending=True,
        display=DisplayState.CleanupRequired,
        ownership_uuid=OwnerId(UUID(int=1)),
        transport=TransportState.Failed,
    )
    pane = project(state, inputs(ownership=ownership()))

    assert pane.actions == frozenset(
        {Action.retry_cleanup, Action.keep_cleanup, Action.stop}
    )
    assert Action.start not in pane.actions
    assert Action.pair not in pane.actions
    display = next(badge for badge in pane.badges if badge.resource == "display")
    assert display.label == "Cleanup unconfirmed"
    assert display.severity == "error"


def test_cleanup_with_uncertain_trust_exposes_both_barriers() -> None:
    state = idle(
        phase=SessionPhase.CleanupRequired,
        trust=TrustState.CleanupRequired,
        intent_stage=IntentStage.cleanup,
        cleanup_pending=True,
        display=DisplayState.CleanupRequired,
        ownership_uuid=OwnerId(UUID(int=1)),
    )
    pane = project(state, inputs(ownership=ownership()))

    assert {Action.retry_cleanup, Action.retry_revocation, Action.keep_blocked} <= pane.actions
    assert Action.start not in pane.actions
    assert pane.mode_enabled is False


def test_failed_phase_preserves_all_four_authoritative_badges() -> None:
    state = _owned_virtual(
        phase=SessionPhase.Failed,
        transport=TransportState.Failed,
        trust=TrustState.Trusted,
        last_error=diagnostic(ErrorCode.transport_failed),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))
    labels = {badge.resource: badge.label for badge in pane.badges}

    assert labels["transport"] == "Transport failed"
    assert labels["display"] == "Output owned"
    assert labels["audio"] == "Audio off"
    assert labels["trust"] == "Paired"
    assert pane.actions == frozenset({Action.retry, Action.stop})


# ------------------------------------------------------------- visible mode and controls


def test_selector_control_state_follows_the_selected_mode() -> None:
    mirror = project(idle(), inputs())
    assert mirror.source_enabled is True
    assert mirror.resolution_enabled is False
    assert mirror.mute_enabled is False
    assert mirror.primary_label == "Start"

    virtual = project(idle(), inputs(selected_mode=Mode.VIRTUAL))
    assert virtual.source_enabled is False
    assert virtual.resolution_enabled is True
    assert virtual.primary_label == "Create display"
    assert virtual.visible_mode is Mode.VIRTUAL


def test_visible_mode_stays_on_the_session_mode_while_it_is_active() -> None:
    pane = project(
        _virtual_state(),
        inputs(selected_mode=Mode.MIRROR, ownership=ownership()),
    )

    assert pane.visible_mode is Mode.VIRTUAL
    assert pane.primary_label == "Stop"
    assert pane.source_enabled is False
    assert pane.resolution_enabled is True
    assert pane.mute_enabled is True
    assert pane.muted is True


def test_mode_selector_is_re_enabled_after_a_confirmed_stop() -> None:
    stopped = idle(mode=Mode.VIRTUAL)
    pane = project(stopped, inputs(selected_mode=Mode.MIRROR))

    assert pane.mode_enabled is True
    assert pane.visible_mode is Mode.MIRROR
    assert pane.primary_label == "Start"
    assert pane.actions == frozenset({Action.start, Action.pair, Action.scan})


def test_a_trusted_receiver_does_not_offer_pairing() -> None:
    pane = project(idle(trust=TrustState.Trusted), inputs())
    assert Action.pair not in pane.actions
    trust = next(badge for badge in pane.badges if badge.resource == "trust")
    assert trust.label == "Paired"


def test_pairing_is_not_offered_without_the_remembered_pairing_capability() -> None:
    pane = project(
        idle(),
        inputs(capabilities=capabilities(remembered_pairing=False)),
    )
    assert Action.pair not in pane.actions
    assert Action.start in pane.actions


# ------------------------------------------------------------------------ row projection


def test_project_row_summarises_an_idle_receiver() -> None:
    row = project_row(idle(), inputs())

    assert isinstance(row, ReceiverRow)
    assert row.receiver_id == RECEIVER
    assert row.name == "Living Room"
    assert row.mode_label == ""
    assert row.phase_label == "Available"
    assert row.size_label == ""
    assert row.online is True


def test_project_row_summarises_an_active_virtual_session() -> None:
    row = project_row(
        _virtual_state(),
        inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()),
    )

    assert row.mode_label == "Virtual display"
    assert row.phase_label == "Virtual display"
    assert row.size_label == "1920x1080"
    assert row.online is True


def test_project_row_summarises_a_mirror_session() -> None:
    row = project_row(_mirroring_state(), inputs())
    assert row.mode_label == "Mirror"
    assert row.phase_label == "Mirroring"
    assert row.size_label == ""


def test_project_row_uses_saved_identity_when_offline() -> None:
    state = idle(phase=SessionPhase.Offline, online=False)
    row = project_row(state, inputs(receiver=None, saved=saved_receiver()))

    assert row.name == "Living Room"
    assert row.phase_label == "Offline"
    assert row.mode_label == ""
    assert row.online is False


def test_project_row_shows_the_current_output_size_for_a_mismatch() -> None:
    state = _owned_virtual(
        phase=SessionPhase.Available,
        transport=TransportState.Idle,
        stream=None,
        accepted_resolution=SMALL,
        last_error=diagnostic(ErrorCode.resolution_mismatch),
    )
    row = project_row(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert row.size_label == "1920x1080"
    assert row.mode_label == "Virtual display"


# ------------------------------------------------------------------------- robustness


def test_project_rejects_a_non_snapshot() -> None:
    with pytest.raises(TypeError):
        project("not a snapshot", inputs())  # type: ignore[arg-type]


def test_project_rejects_non_inputs() -> None:
    with pytest.raises(TypeError):
        project(idle(), object())  # type: ignore[arg-type]


def test_project_row_rejects_a_non_snapshot() -> None:
    with pytest.raises(TypeError):
        project_row(None, inputs())  # type: ignore[arg-type]


def test_a_non_canonical_receiver_id_never_breaks_projection() -> None:
    state = SessionSnapshot(
        receiver_id=ReceiverId("legacy:1"),
        online=True,
        phase=SessionPhase.CleanupRequired,
        intent_stage=IntentStage.cleanup,
        cleanup_pending=True,
        display=DisplayState.CleanupRequired,
    )
    pane = project(state, inputs(receiver=None, saved=None))

    assert pane.diagnostic is not None
    assert pane.diagnostic.code is ErrorCode.cleanup_timeout
    assert pane.actions == frozenset(
        {Action.retry_cleanup, Action.keep_cleanup, Action.stop}
    )


def test_view_inputs_defaults_to_empty_optional_sequences() -> None:
    inputs = ViewInputs(
        receiver=None,
        saved=None,
        capabilities=capabilities(),
        selected_mode=Mode.MIRROR,
    )

    assert inputs.physical_sources == ()
    assert inputs.resolution_choices == ()
    assert inputs.ownership is None
    assert inputs.orphans == ()


# ------------------------------------------------------------- exhaustive invariants


_ALL_PHASES = tuple(SessionPhase)
_ALL_TRUST = tuple(TrustState)
_ALL_DISPLAY = tuple(DisplayState)
_ALL_AUDIO = tuple(AudioState)
_ALL_TRANSPORT = tuple(TransportState)
_BUSY_TRANSPORT = frozenset(
    {
        TransportState.Pairing,
        TransportState.Connecting,
        TransportState.Streaming,
        TransportState.Reconnecting,
        TransportState.Stopping,
    }
)
_BLOCKING_TRUST = frozenset(
    {
        TrustState.Pairing,
        TrustState.RevocationPending,
        TrustState.CleanupRequired,
    }
)


def _display_badge(pane: PaneState) -> Badge:
    return next(badge for badge in pane.badges if badge.resource == "display")


def test_grid_never_offers_a_start_over_resource_evidence() -> None:
    """Every transport/display/audio/trust combination stays inside the safe set."""
    for online in (True, False):
        for transport in _ALL_TRANSPORT:
            for display in _ALL_DISPLAY:
                for audio in _ALL_AUDIO:
                    for trust in _ALL_TRUST:
                        state = idle(
                            online=online,
                            transport=transport,
                            display=display,
                            audio=audio,
                            trust=trust,
                        )
                        pane = project(state, inputs())
                        evidence = (
                            not online
                            or transport in _BUSY_TRANSPORT
                            or display is not DisplayState.None_
                            or trust in _BLOCKING_TRUST
                            or not pane.mode_enabled
                        )
                        context = (online, transport, display, audio, trust)
                        if evidence:
                            assert Action.start not in pane.actions, context
                            assert Action.pair not in pane.actions, context
                        if trust in (
                            TrustState.RevocationPending,
                            TrustState.CleanupRequired,
                        ):
                            assert pane.mode_enabled is False, context
                            # A bounded teardown may be progress-only, but any
                            # offered action set must include the trust barrier.
                            if pane.actions:
                                assert Action.retry_revocation in pane.actions, context
                                assert Action.keep_blocked in pane.actions, context
                        if display is not DisplayState.None_:
                            assert pane.mode_enabled is False, context
                            assert _display_badge(pane).label != "No output", context
                        if audio is AudioState.Unavailable:
                            assert pane.mute_enabled is False, context
                            assert Action.mute not in pane.actions, context


def test_uncertain_trust_blocks_every_phase() -> None:
    for phase in _ALL_PHASES:
        for trust in (TrustState.RevocationPending, TrustState.CleanupRequired):
            state = idle(phase=phase, trust=trust)
            pane = project(state, inputs())

            # A teardown already running owns the revocation attempt, so its only
            # safe presentation is a progress label with no duplicate command.
            teardown = state.intent_stage is IntentStage.stopping or phase is SessionPhase.Stopping
            context = (phase, trust, teardown)
            assert pane.mode_enabled is False, context
            assert Action.start not in pane.actions, context
            assert Action.pair not in pane.actions, context
            assert Action.submit_pairing not in pane.actions, context
            if not teardown:
                assert Action.retry_revocation in pane.actions, context
                assert Action.keep_blocked in pane.actions, context


def test_unconfirmed_display_is_never_released_in_any_phase() -> None:
    for phase in _ALL_PHASES:
        state = idle(
            phase=phase,
            display=DisplayState.CleanupRequired,
            ownership_uuid=OwnerId(UUID(int=1)),
        )
        pane = project(state, inputs(ownership=ownership()))
        display = _display_badge(pane)

        assert display.label == "Cleanup unconfirmed", phase
        assert display.severity == "error", phase
        assert Action.start not in pane.actions, phase


def test_unavailable_audio_never_enables_mute_in_any_phase() -> None:
    for phase in _ALL_PHASES:
        state = idle(
            phase=phase,
            audio=AudioState.Unavailable,
            stream=stream_handle(),
            transport=TransportState.Streaming,
        )
        pane = project(state, inputs())
        audio = next(badge for badge in pane.badges if badge.resource == "audio")

        assert audio.label == "Audio unavailable", phase
        assert audio.severity == "error", phase
        assert pane.mute_enabled is False, phase
        assert Action.mute not in pane.actions, phase


def test_orphan_actions_are_never_unconditional_anywhere() -> None:
    for phase in _ALL_PHASES:
        without = project(idle(phase=phase), inputs())
        assert Action.confirm_orphan not in without.actions, phase
        assert Action.keep_orphan not in without.actions, phase

    item = RecoveryItem(
        kind="orphan",
        record=ownership(),
        handle=virtual_handle(),
        diagnostic=diagnostic(ErrorCode.ownership_mismatch),
    )
    for phase in _ALL_PHASES:
        pane = project(idle(phase=phase), inputs(orphans=(item,)))
        if Action.confirm_orphan in pane.actions:
            assert Action.keep_orphan in pane.actions, phase


# ------------------------------------------------------------- retained-output guard


def test_available_phase_with_a_retained_output_never_offers_start() -> None:
    state = idle(
        mode=Mode.VIRTUAL,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert pane.actions == frozenset({Action.retry, Action.stop})
    assert Action.start not in pane.actions
    assert pane.mode_enabled is False
    assert _display_badge(pane).label == "Output owned"


def test_durable_ownership_without_a_display_state_is_unconfirmed_cleanup() -> None:
    pane = project(idle(), inputs(ownership=ownership()))

    assert pane.actions == frozenset(
        {Action.retry_cleanup, Action.keep_cleanup, Action.stop}
    )
    assert Action.start not in pane.actions
    assert Action.retry not in pane.actions
    assert pane.mode_enabled is False
    assert _display_badge(pane).label == "Ownership unconfirmed"
    assert _display_badge(pane).severity == "error"


def test_offline_with_a_retained_output_still_exposes_an_explicit_stop() -> None:
    state = idle(
        phase=SessionPhase.Offline,
        online=False,
        mode=Mode.VIRTUAL,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
    )
    pane = project(
        state,
        inputs(receiver=None, saved=saved_receiver(), ownership=ownership()),
    )

    assert Action.stop in pane.actions
    assert Action.scan in pane.actions
    assert Action.start not in pane.actions
    assert Action.pair not in pane.actions


def test_retained_output_under_uncertain_trust_offers_stop_and_revocation() -> None:
    state = idle(
        mode=Mode.VIRTUAL,
        trust=TrustState.CleanupRequired,
        display=DisplayState.Owned,
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
    )
    pane = project(state, inputs(selected_mode=Mode.VIRTUAL, ownership=ownership()))

    assert Action.retry_revocation in pane.actions
    assert Action.keep_blocked in pane.actions
    assert Action.stop in pane.actions
    assert Action.start not in pane.actions
    assert Action.mute not in pane.actions


# ---------------------------------------------------------------- missing local inputs


def test_available_mirror_without_a_monitor_explains_why_start_may_fail() -> None:
    pane = project(idle(), inputs(physical_sources=()))

    assert Action.start in pane.actions
    assert pane.source_enabled is False
    assert pane.notice is not None
    assert "physical monitor" in pane.notice


def test_available_virtual_without_reported_sizes_explains_the_decision() -> None:
    pane = project(
        idle(),
        inputs(selected_mode=Mode.VIRTUAL, resolution_choices=()),
    )

    assert pane.notice is not None
    assert "resolution" in pane.notice.lower()


# ------------------------------------------------------------------------ input shape


def test_view_inputs_normalises_mutable_sequences_to_tuples() -> None:
    inputs = ViewInputs(
        receiver=None,
        saved=None,
        capabilities=capabilities(),
        selected_mode=Mode.MIRROR,
        physical_sources=[physical()],  # type: ignore[arg-type]
        resolution_choices=[HD],  # type: ignore[arg-type]
        orphans=[],  # type: ignore[arg-type]
    )

    assert inputs.physical_sources == (physical(),)
    assert inputs.resolution_choices == (HD,)
    assert inputs.orphans == ()
    assert isinstance(inputs.physical_sources, tuple)


def test_view_inputs_rejects_a_wrong_element_type() -> None:
    with pytest.raises(TypeError):
        ViewInputs(
            receiver=None,
            saved=None,
            capabilities=capabilities(),
            selected_mode=Mode.MIRROR,
            physical_sources=("DP-1",),  # type: ignore[arg-type]
        )

# ------------------------------------------------------------------------- purity


def test_projection_module_stays_free_of_qt_tasks_and_session_internals() -> None:
    """The projection must remain importable and testable without a display server."""
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "airplay_linux"
        / "ui"
        / "state.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = ("PySide6", "qasync", "asyncio", "airplay_linux.session", "airplay_linux.app")
    offenders = sorted(name for name in imported if name.startswith(forbidden))

    assert offenders == []


def test_projection_is_deterministic_and_frozen() -> None:
    state = _virtual_state()
    view = inputs(selected_mode=Mode.VIRTUAL, ownership=ownership())

    first = project(state, view)
    second = project(state, view)
    assert first == second
    assert isinstance(first.actions, frozenset)

    with pytest.raises(FrozenInstanceError):
        first.mode_enabled = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        view.selected_mode = Mode.MIRROR  # type: ignore[misc]


def test_ui_package_reexports_the_projection() -> None:
    from airplay_linux.ui import Action as PackageAction
    from airplay_linux.ui import project as package_project

    assert PackageAction is Action
    assert package_project is project
