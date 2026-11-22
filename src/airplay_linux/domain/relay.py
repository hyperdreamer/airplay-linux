"""Start-bound credential relay records and secret-free observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .identity import _validate_generation

if TYPE_CHECKING:
    from uuid import UUID

    from .identity import ReceiverId, ReceiverTarget
    from .operations import (
        ChallengeId,
        ChallengeView,
        InstanceId,
        LeaseId,
        OperationId,
        ProcessIdentity,
    )
    from .resources import AudioPolicy, ErrorCode, NoReplyReason, VolumeLevel


CredentialKind = Literal["pin", "password"]
CredentialPromptLifecycle = Literal["live", "ended"]
CredentialPromptStateName = Literal["editing", "submitting", "observing"]
CredentialPromptReport = Literal[
    "connected", "not_observed_streaming", "outcome_unknown",
]
SettlementPhase = Literal["awaiting_reply", "observing", "cleanup"]


@dataclass(frozen=True, kw_only=True)
class RelayStartOptions:
    """Options snapshotted from the owning Mirror Start."""

    target_volume_proven: bool
    audio: AudioPolicy | None
    initial_volume: VolumeLevel | None


@dataclass(frozen=True, kw_only=True)
class CredentialRequest:
    """One matching target-row request, owned by one pending Start."""

    receiver_id: ReceiverId
    target: ReceiverTarget
    credential_kind: CredentialKind
    start_operation_id: OperationId
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    generation: int
    observed_at: float
    start_options: RelayStartOptions

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("generation", self.generation)


@dataclass(frozen=True, kw_only=True)
class CredentialPromptState:
    """Transient actor projection for one local credential prompt."""

    request: CredentialRequest
    challenge: ChallengeView
    state: CredentialPromptStateName
    submission_id: OperationId | None
    report: CredentialPromptReport | None


@dataclass(frozen=True, kw_only=True)
class CredentialEffectOwner:
    """Immutable ownership tuple carried through credential effects."""

    challenge_id: ChallengeId
    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    relay_generation: int
    marker_generation: int

    def __post_init__(self) -> None:
        for name in (
            "endpoint_generation", "session_generation", "trust_generation",
            "relay_generation", "marker_generation",
        ):
            _validate_generation(name, getattr(self, name))


@dataclass(frozen=True, kw_only=True)
class Submitted:
    """A complete positive or negative credential command reply."""

    reply_at: float
    accepted: bool
    post_dispatch_error: ErrorCode | None

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a boolean")


@dataclass(frozen=True, kw_only=True)
class NoReply:
    """A dispatched command whose complete reply was not decoded."""

    reason: NoReplyReason


@dataclass(frozen=True, kw_only=True)
class CredentialSubmissionSettlement:
    """Actor-local ownership for one dispatched credential submission."""

    submission_id: OperationId
    read_id: OperationId | None
    owner: CredentialEffectOwner
    marker_generation: int
    stop_operation_id: OperationId | None
    report_suppressed: bool
    phase: SettlementPhase

    def __post_init__(self) -> None:
        _validate_generation("marker_generation", self.marker_generation)
        if type(self.report_suppressed) is not bool:
            raise TypeError("report_suppressed must be a boolean")


@dataclass(frozen=True, kw_only=True)
class PendingStartCleanupOwner:
    """Exact owner captured before a pending Start is superseded by Stop."""

    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    stop_operation_id: OperationId
    credential_submission_id: OperationId | None
    marker_generation: int | None

    def __post_init__(self) -> None:
        for name in (
            "endpoint_generation", "session_generation", "trust_generation",
        ):
            _validate_generation(name, getattr(self, name))
        if (self.credential_submission_id is None) != (self.marker_generation is None):
            raise ValueError(
                "credential submission and marker generation must be captured together"
            )
        if self.marker_generation is not None:
            _validate_generation("marker_generation", self.marker_generation)


@dataclass(frozen=True, kw_only=True)
class TerminationProof:
    """Positive, exact-instance process termination evidence."""

    daemon_instance_uuid: InstanceId
    daemon: ProcessIdentity
    runtime_directory: str
    launch_uuid: UUID
    receipt_state: Literal["stopped", "failed"]
    cleanup_outcome: Literal["stopped"] | None
    cleanup_completed_at: float | None
    process_evidence: Literal["pid_absent", "pidfd_exited"]
    identity_verified_at: float
    observed_at: float


def relay_credential_representable(value: str) -> bool:
    """Return whether a transient credential is safe as the final argv token."""

    return not value.startswith("-")


__all__ = [
    "CredentialEffectOwner", "CredentialKind", "CredentialPromptState",
    "CredentialPromptLifecycle", "CredentialPromptReport", "CredentialPromptStateName",
    "CredentialRequest", "CredentialSubmissionSettlement", "NoReply", "PendingStartCleanupOwner",
    "RelayStartOptions", "SettlementPhase", "Submitted", "TerminationProof",
    "relay_credential_representable",
]
