"""Explicit disk records and public session snapshots, never generic projections."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from .identity import _validate_generation
from .resources import (
    AudioPolicy,
    AudioState,
    DisplayState,
    IntentStage,
    SessionPhase,
    TransportState,
    TrustState,
)

if TYPE_CHECKING:
    from .identity import ReceiverId, ReceiverTarget, ServiceRecord
    from .operations import (
        ChallengeView,
        DisplayHandle,
        InstanceId,
        LeaseId,
        OpaqueCredentialRef,
        OperationId,
        OwnerId,
        ProcessIdentity,
        StreamHandle,
    )
    from .resources import (
        Diagnostic,
        Mode,
        PhysicalMonitorSource,
        ResolutionDecision,
        Size,
        StartRequest,
    )


def _freeze_properties(
    values: Mapping[str, Mapping[str, str | int | bool]],
) -> Mapping[str, Mapping[str, str | int | bool]]:
    return MappingProxyType({key: MappingProxyType(dict(properties)) for key, properties in values.items()})


@dataclass(frozen=True, kw_only=True)
class LayoutSnapshot:
    outputs: Mapping[str, Mapping[str, str | int | bool]]
    baseline_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "outputs", _freeze_properties(self.outputs))


@dataclass(frozen=True, kw_only=True)
class OwnershipRecord:
    schema_version: int
    owner_uuid: OwnerId
    app_instance_uuid: InstanceId
    runtime_directory: str
    creation_operation_id: OperationId
    receiver_id: ReceiverId
    endpoint_fingerprint: str
    desired_state: Literal["active", "removing"]
    stage: Literal["reserved", "helper_started", "output_observed", "published", "removing"]
    requested_size: Size
    observed_size: Size | None
    helper: ProcessIdentity | None
    connector: str | None
    output_id: str | None
    created_utc: str
    last_configuration_step: str
    baseline: LayoutSnapshot
    last_applied: Mapping[str, Mapping[str, str | int | bool]]
    provider_version: str
    capability_fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "last_applied", _freeze_properties(self.last_applied))


@dataclass(frozen=True, kw_only=True)
class DaemonRecord:
    """Authentication data is confined to runtime storage and omitted from repr."""

    schema_version: int
    instance_uuid: InstanceId
    owner_token: str = field(repr=False)
    app: ProcessIdentity
    supervisor: ProcessIdentity | None
    daemon: ProcessIdentity | None
    protocol_version: str
    socket_path: str
    lifecycle: Literal["starting", "running", "stopping"]
    runtime_directory: str


@dataclass(frozen=True, kw_only=True)
class SessionSnapshot:
    receiver_id: ReceiverId
    target: ReceiverTarget | None = None
    online: bool = False
    mode: Mode | None = None
    intent_active: bool = False
    intent_stage: IntentStage = IntentStage.idle
    phase: SessionPhase = SessionPhase.Offline
    transport: TransportState = TransportState.Idle
    display: DisplayState = DisplayState.None_
    audio: AudioState = AudioState.Disabled
    trust: TrustState = TrustState.Unknown
    physical_source: PhysicalMonitorSource | None = None
    display_handle: DisplayHandle | None = None
    ownership_uuid: OwnerId | None = None
    resolution: ResolutionDecision | None = None
    observed_resolution: Size | None = None
    accepted_resolution: Size | None = None
    audio_policy: AudioPolicy = AudioPolicy()
    muted: bool = False
    stream: StreamHandle | None = None
    challenge: ChallengeView | None = None
    last_error: Diagnostic | None = None
    reconnect_attempt: int = 0
    next_retry_at: float | None = None
    operation_id: OperationId | None = None
    session_generation: int = 0
    transport_lease_id: LeaseId | None = None
    trust_generation: int = 0
    audio_operation_id: OperationId | None = None
    queued_start: StartRequest | None = None
    cleanup_pending: bool = False

    def __post_init__(self) -> None:
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("trust_generation", self.trust_generation)
        _validate_generation("reconnect_attempt", self.reconnect_attempt)

    @classmethod
    def idle(cls, receiver_id: ReceiverId, online: bool = False) -> SessionSnapshot:
        return cls(
            receiver_id=receiver_id,
            online=online,
            phase=SessionPhase.Available if online else SessionPhase.Offline,
        )


@dataclass(frozen=True, kw_only=True)
class SavedReceiver:
    """Non-secret preferences; stores must reject ambiguous receiver identities."""

    receiver_id: ReceiverId
    service_metadata: tuple[ServiceRecord, ...]
    display_name: str
    last_endpoint: ReceiverTarget | None
    continuity_fingerprint: str
    credential_ref: OpaqueCredentialRef | None
    remembered_resolution: ResolutionDecision | None


@dataclass(frozen=True, kw_only=True)
class RecoveryItem:
    kind: Literal["recovering", "removing", "orphan", "cleanup_required", "discarded"]
    record: OwnershipRecord
    handle: DisplayHandle | None
    diagnostic: Diagnostic | None


@dataclass(frozen=True, kw_only=True)
class ActivityEvent:
    """Only the diagnostics layer's allowlisted fields may reach an activity sink."""

    utc: str
    code: str
    receiver_id: ReceiverId | None
    message: str
    fields: Mapping[str, str | int | bool]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
