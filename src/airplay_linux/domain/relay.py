"""Secret-free mirror pairing relay records and their single eligibility predicate.

Every record here is a transient projection: none holds credential text, and no
persistent codec admits one. :func:`can_submit` is the one predicate shared by the
presentation, the actor's command admission and the transport's final spawn fence,
so it performs no I/O, schedules no timer and derives no deadline of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .identity import _validate_generation

if TYPE_CHECKING:
    from uuid import UUID

    from airplay_linux.backend.contract import AckReply, StatusReply

    from .identity import ReceiverId, ReceiverTarget
    from .operations import ChallengeView, InstanceId, LeaseId, ProcessIdentity
    from .resources import AudioPolicy, ErrorCode, VolumeLevel


CredentialKind = Literal["pin", "password"]
RequestState = Literal["live", "succeeded", "Ended", "Expired", "Stale", "cancelled"]
DispatchState = Literal["none", "no_spawn", "acknowledged", "uncertain", "settled"]
ReleaseState = Literal["none", "settling", "released", "retained"]
RelayPhase = Literal["none", "permission", "credential", "handshake", "source", "closed"]
RelayNotice = Literal[
    "credential_requested", "pin_rejected", "pairing_failed", "pairing_cancelled",
    "receiver_changed", "daemon_stopping", "uncertain", "waiting_start",
]


@dataclass(frozen=True, kw_only=True)
class RelayStartOptions:
    """Options snapshotted once from the admitted original connect."""

    target_volume_proven: bool
    audio: AudioPolicy | None
    initial_volume: VolumeLevel | None


@dataclass(frozen=True, kw_only=True)
class CredentialRequest:
    """One target's matching ``pin_required`` evidence; never the credential."""

    receiver_id: ReceiverId
    target: ReceiverTarget
    credential_kind: CredentialKind
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
class SuccessPending:
    """Set once on matching streaming evidence; cleared by trust commit or failure."""

    generation: int
    attempt: int
    observed_at: float

    def __post_init__(self) -> None:
        _validate_generation("generation", self.generation)
        _validate_generation("attempt", self.attempt)


@dataclass(frozen=True, kw_only=True)
class RelayLeaseView:
    """A frozen copy of the transport lease's relay state, never a second authority."""

    receiver_id: ReceiverId
    target: ReceiverTarget
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    owner_live: bool
    generation: int
    request: CredentialRequest | None
    request_state: RequestState
    request_waiting: bool
    challenge: ChallengeView | None
    submit_inflight: int | None
    submit_fenced: bool
    dispatch_state: DispatchState
    success_pending: SuccessPending | None
    prepared_source: bool
    streaming: bool
    consumed_submissions: int
    release_state: ReleaseState
    phase: RelayPhase
    expires_at: float | None
    notice: RelayNotice | None

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("generation", self.generation)
        _validate_generation("consumed_submissions", self.consumed_submissions)


@dataclass(frozen=True, kw_only=True)
class TerminationProof:
    """Positive, exact-instance process termination evidence.

    ``cleanup_completed_at`` copies the persisted ``DaemonCleanupIntent`` wall-clock
    value unchanged; ``identity_verified_at`` and ``observed_at`` are monotonic and
    are never compared with it.
    """

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


@dataclass(frozen=True, kw_only=True)
class ReleaseEvidence:
    """One release attempt's sanitized evidence; never a credential or argv."""

    reason: ErrorCode | None
    observed_at: float
    disconnect_ack: AckReply | None = None
    status: StatusReply | None = None
    daemon_instance_uuid: InstanceId | None = None
    endpoint_generation: int | None = None
    termination: TerminationProof | None = None

    def __post_init__(self) -> None:
        if self.endpoint_generation is not None:
            _validate_generation("endpoint_generation", self.endpoint_generation)


def can_submit(lease: RelayLeaseView) -> bool:
    r = lease.request
    c = lease.challenge
    return (
        # A request generation is live for exactly this target and owner.
        r is not None
        and c is not None
        and lease.owner_live
        and lease.request_state == "live"
        and lease.request_waiting
        and r.generation == lease.generation
        and r.receiver_id == lease.receiver_id == c.receiver_id
        and r.target == lease.target
        and r.endpoint_generation == lease.endpoint_generation == c.endpoint_generation
        and r.session_generation == lease.session_generation == c.session_generation
        and r.owner_lease == lease.owner_lease
        and r.daemon_instance_uuid == lease.daemon_instance_uuid
        # No superseded or stale challenge is answerable.
        and c.credential_kind == r.credential_kind
        and c.endpoint_fingerprint == lease.target.endpoint_fingerprint
        # The acquisition and release barriers are shared by all callers.
        and lease.submit_inflight is None
        and not lease.submit_fenced
        and lease.dispatch_state in {"none", "settled"}
        # Progress retires input before trust completion.
        and lease.success_pending is None
        and not lease.prepared_source
        and not lease.streaming
    )


def relay_credential_representable(
    value: str, *, target_volume_proven: bool,
) -> bool:
    """Whether the selected CLI can carry this transient credential as its last token.

    The extended table's parser interprets ``--audio``/``--volume`` and rejects other
    tokens beginning with ``-``; it supplies no end-of-options escape. The value is
    examined and never retained, rewritten or logged.
    """
    return not (target_volume_proven and value.startswith("-"))
