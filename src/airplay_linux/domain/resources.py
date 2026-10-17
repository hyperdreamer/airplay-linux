"""Independent resource states, start intent, and typed public results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Generic, Literal, TypeAlias, TypeVar

if TYPE_CHECKING:
    from .identity import ReceiverId
    from .operations import LeaseId


class Mode(StrEnum):
    MIRROR = "mirror"
    VIRTUAL = "virtual"


class TransportState(StrEnum):
    Idle = "Idle"
    Pairing = "Pairing"
    Connecting = "Connecting"
    Streaming = "Streaming"
    Reconnecting = "Reconnecting"
    Stopping = "Stopping"
    Failed = "Failed"


class DisplayState(StrEnum):
    None_ = "None"
    Resolving = "Resolving"
    Creating = "Creating"
    Owned = "Owned"
    Reconfiguring = "Reconfiguring"
    Removing = "Removing"
    Failed = "Failed"
    CleanupRequired = "CleanupRequired"


class AudioState(StrEnum):
    Disabled = "Disabled"
    Starting = "Starting"
    Enabled = "Enabled"
    Muted = "Muted"
    Unavailable = "Unavailable"


class TrustState(StrEnum):
    Unknown = "Unknown"
    Untrusted = "Untrusted"
    Trusted = "Trusted"
    Pairing = "Pairing"
    RevocationPending = "RevocationPending"
    CleanupRequired = "CleanupRequired"


class SessionPhase(StrEnum):
    Available = "Available"
    Offline = "Offline"
    ResolutionRequired = "ResolutionRequired"
    AudioDecisionRequired = "AudioDecisionRequired"
    CreatingDisplay = "CreatingDisplay"
    Pairing = "Pairing"
    Connecting = "Connecting"
    Mirroring = "Mirroring"
    VirtualDisplay = "VirtualDisplay"
    Reconnecting = "Reconnecting"
    Reconfiguring = "Reconfiguring"
    Recovering = "Recovering"
    Stopping = "Stopping"
    CleanupRequired = "CleanupRequired"
    TrustCleanupRequired = "TrustCleanupRequired"
    Failed = "Failed"


class IntentStage(StrEnum):
    idle = "idle"
    starting = "starting"
    active = "active"
    resolution = "resolution"
    audio_decision = "audio_decision"
    reconfiguring = "reconfiguring"
    recovering = "recovering"
    stopping = "stopping"
    cleanup = "cleanup"


class ErrorCode(StrEnum):
    portal_cancelled = "portal_cancelled"
    portal_denied = "portal_denied"
    keyring_unavailable = "keyring_unavailable"
    unsupported_source = "unsupported_source"
    unsupported_mode = "unsupported_mode"
    unsupported_operation = "unsupported_operation"
    dependency_missing = "dependency_missing"
    dependency_incompatible = "dependency_incompatible"
    audio_denied = "audio_denied"
    audio_unavailable = "audio_unavailable"
    audio_mute_failed = "audio_mute_failed"
    receiver_rejected = "receiver_rejected"
    stale_target = "stale_target"
    stale_event = "stale_event"
    stale_socket = "stale_socket"
    ownership_mismatch = "ownership_mismatch"
    helper_failed = "helper_failed"
    configuration_failed = "configuration_failed"
    cleanup_timeout = "cleanup_timeout"
    trust_cleanup_required = "trust_cleanup_required"
    resolution_mismatch = "resolution_mismatch"
    discovery_failed = "discovery_failed"
    transport_failed = "transport_failed"
    daemon_failed = "daemon_failed"
    persistence_failed = "persistence_failed"
    invalid_input = "invalid_input"
    pairing_expired = "pairing_expired"


class RecoveryAction(StrEnum):
    retry = "retry"
    stop = "stop"
    back = "back"
    choose_mode = "choose_mode"
    video_only = "video_only"
    retry_audio = "retry_audio"
    retry_cleanup = "retry_cleanup"
    retry_revocation = "retry_revocation"
    keep_blocked = "keep_blocked"
    remove_metadata = "remove_metadata"
    recreate = "recreate"
    confirm_remove = "confirm_remove"
    keep = "keep"


@dataclass(frozen=True, kw_only=True)
class Diagnostic:
    """A sanitized message; adapters must never include raw backend exceptions."""

    code: ErrorCode
    message: str
    recovery: tuple[RecoveryAction, ...]
    receiver_id: ReceiverId | None = None
    detail: str | None = None


T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class Ok(Generic[T]):
    value: T


@dataclass(frozen=True, kw_only=True)
class Err:
    error: Diagnostic


Result: TypeAlias = Ok[T] | Err


@dataclass(frozen=True, kw_only=True)
class Size:
    width: int
    height: int

    def __post_init__(self) -> None:
        for name in ("width", "height"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be an integer")
            if not 1 <= value <= 16384:
                raise ValueError(f"{name} must be between 1 and 16384")


@dataclass(frozen=True, kw_only=True)
class PhysicalMonitorSource:
    connector: str
    portal_restore_token: str | None
    capture_lease_id: LeaseId | None = None


@dataclass(frozen=True, kw_only=True)
class VirtualOutputSource:
    connector: str
    kscreen_output_id: str
    size: Size


CaptureSource: TypeAlias = PhysicalMonitorSource | VirtualOutputSource


@dataclass(frozen=True, kw_only=True)
class AudioPolicy:
    enabled: bool = True


@dataclass(frozen=True, kw_only=True)
class ResolutionEvidence:
    origin: str
    reliable: bool
    receiver_id: ReceiverId
    endpoint_fingerprint: str | None


@dataclass(frozen=True, kw_only=True)
class ResolutionDecision:
    size: Size
    source: Literal["stored", "discovery", "user"]
    evidence: ResolutionEvidence


@dataclass(frozen=True, kw_only=True)
class ReceiverCapabilities:
    """A missing capability response is None, rather than an invented mode."""

    modes: tuple[Size, ...]
    reliable: bool
    evidence: str


@dataclass(frozen=True, kw_only=True)
class CapabilityReport:
    environment: str
    discovery: bool
    mirror: bool
    virtual_display: bool
    audio: bool
    remembered_pairing: bool
    additional_virtual_outputs: bool
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, kw_only=True)
class StartRequest:
    mode: Mode
    source: PhysicalMonitorSource | None = None
    resolution: ResolutionDecision | None = None
    audio: AudioPolicy = AudioPolicy()

    def __post_init__(self) -> None:
        if self.source is not None and not isinstance(self.source, PhysicalMonitorSource):
            raise TypeError("start source must be a physical monitor")
