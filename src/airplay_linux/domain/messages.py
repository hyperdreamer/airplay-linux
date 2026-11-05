"""Typed commands and resource events, without raw backend messages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal, TypeAlias

from .identity import _validate_generation
from .resources import VolumeLevel

if TYPE_CHECKING:
    from .identity import ReceiverId, ReceiverView
    from .operations import (
        ChallengeId,
        ChallengeView,
        DisplayHandle,
        InstanceId,
        OperationContext,
        OperationId,
        OwnerId,
        ProcessIdentity,
        SensitiveText,
        StreamHandle,
    )
    from .resources import Diagnostic, PhysicalMonitorSource, ResolutionDecision, Result, Size, StartRequest


@dataclass(frozen=True, kw_only=True)
class Start:
    receiver_id: ReceiverId
    request: StartRequest


@dataclass(frozen=True, kw_only=True)
class Stop:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class Cancel:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class Retry:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class SubmitPairing:
    receiver_id: ReceiverId
    challenge_id: ChallengeId
    value: SensitiveText


@dataclass(frozen=True, kw_only=True)
class Pair:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class Forget:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class ChooseResolution:
    receiver_id: ReceiverId
    decision: ResolutionDecision


@dataclass(frozen=True, kw_only=True)
class ChangeResolution:
    receiver_id: ReceiverId
    decision: ResolutionDecision


@dataclass(frozen=True, kw_only=True)
class RecreateAccepted:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class SetMuted:
    receiver_id: ReceiverId
    muted: bool


@dataclass(frozen=True, kw_only=True)
class SetVolume:
    receiver_id: ReceiverId
    level: VolumeLevel

    def __post_init__(self) -> None:
        if type(self.level) is not VolumeLevel:
            raise TypeError("volume command requires a VolumeLevel")


@dataclass(frozen=True, kw_only=True)
class RetryAudio:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class StartVideoOnly:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class RetryCleanup:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class RetryRevocation:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class KeepCleanup:
    receiver_id: ReceiverId


@dataclass(frozen=True, kw_only=True)
class ConfirmSwitch:
    receiver_id: ReceiverId
    request: StartRequest
    confirmed: bool


@dataclass(frozen=True, kw_only=True)
class ConfirmOrphanRemoval:
    receiver_id: ReceiverId
    owner_uuid: OwnerId
    confirmed: bool


Command: TypeAlias = (
    Start | Stop | Cancel | Retry | SubmitPairing | Pair | Forget
    | ChooseResolution | ChangeResolution | RecreateAccepted | SetMuted | SetVolume
    | RetryAudio | StartVideoOnly | RetryCleanup | RetryRevocation
    | KeepCleanup | ConfirmSwitch | ConfirmOrphanRemoval
)


@dataclass(frozen=True, kw_only=True)
class CommandReceipt:
    """Acceptance of an operation, rather than completion of its effects."""

    operation_id: OperationId
    accepted: bool
    diagnostic: Diagnostic | None


class TransportEventKind(StrEnum):
    ready = "ready"
    stopped = "stopped"
    network_lost = "network_lost"
    pairing_required = "pairing_required"
    credential_invalid = "credential_invalid"
    audio_start_failed = "audio_start_failed"
    audio_failed = "audio_failed"
    audio_recovered = "audio_recovered"
    mute_result = "mute_result"
    volume_result = "volume_result"
    accepted_mode = "accepted_mode"
    failed = "failed"
    daemon_exit = "daemon_exit"


@dataclass(frozen=True, kw_only=True)
class EmptyPayload:
    pass


@dataclass(frozen=True, kw_only=True)
class FailurePayload:
    diagnostic: Diagnostic


@dataclass(frozen=True, kw_only=True)
class ModePayload:
    size: Size
    reliable: bool


@dataclass(frozen=True, kw_only=True)
class MutePayload:
    muted: bool
    ok: bool


@dataclass(frozen=True, kw_only=True)
class VolumePayload:
    level: int
    ok: bool

    def __post_init__(self) -> None:
        VolumeLevel(value=self.level)
        if type(self.ok) is not bool:
            raise TypeError("volume result ok must be a boolean")


@dataclass(frozen=True, kw_only=True)
class SourceVerificationPayload:
    """Sanitized, positively verified physical source evidence."""

    connector: str
    source_type: int
    width: int
    height: int
    x: int
    y: int

    def __post_init__(self) -> None:
        if type(self.connector) is not str or not self.connector:
            raise TypeError("source connector must be non-empty text")
        for name in ("source_type", "width", "height", "x", "y"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"source {name} must be an integer")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("source dimensions must be positive")


@dataclass(frozen=True, kw_only=True)
class ChallengePayload:
    challenge: ChallengeView


TransportPayload: TypeAlias = (
    EmptyPayload | FailurePayload | ModePayload | MutePayload | VolumePayload | ChallengePayload
    | SourceVerificationPayload
)


@dataclass(frozen=True, kw_only=True)
class TransportEvent:
    kind: TransportEventKind
    receiver_id: ReceiverId
    stream: StreamHandle | None
    endpoint_fingerprint: str
    endpoint_generation: int
    daemon_instance_uuid: InstanceId
    context: OperationContext
    payload: TransportPayload

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)


@dataclass(frozen=True, kw_only=True)
class DisplayEvent:
    """Fenced by display ownership, independently of network generations."""

    kind: Literal[
        "reserved", "helper_started", "output_observed", "published", "removed",
        "helper_exit", "disappeared", "cleanup_required",
    ]
    receiver_id: ReceiverId
    creation_operation_id: OperationId | None
    owner_uuid: OwnerId
    connector: str | None
    output_id: str | None
    helper: ProcessIdentity | None
    handle: DisplayHandle | None
    diagnostic: Diagnostic | None


@dataclass(frozen=True, kw_only=True)
class DiscoveryEvent:
    kind: Literal["added", "updated", "removed", "error"]
    receiver: ReceiverView | None
    diagnostic: Diagnostic | None


@dataclass(frozen=True, kw_only=True)
class TimerEvent:
    receiver_id: ReceiverId
    kind: Literal["reconnect", "pairing_expiry", "cleanup_deadline", "local_retry"]
    context: OperationContext


@dataclass(frozen=True, kw_only=True)
class EffectEvent:
    """Actor-internal completion; never persist an effect or its resource."""

    receiver_id: ReceiverId
    operation_id: OperationId
    session_generation: int
    kind: str
    result: Result[object]
    resource: StreamHandle | DisplayHandle | PhysicalMonitorSource | None

    def __post_init__(self) -> None:
        _validate_generation("session_generation", self.session_generation)


ResourceEvent: TypeAlias = TransportEvent | DisplayEvent | DiscoveryEvent | TimerEvent | EffectEvent
