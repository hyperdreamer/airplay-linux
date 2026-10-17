"""Operation fences, exact resource handles, and transient private values."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, NewType
from uuid import UUID

from .identity import _validate_generation

if TYPE_CHECKING:
    from .identity import ReceiverId
    from .resources import Diagnostic, VirtualOutputSource


OperationId = NewType("OperationId", UUID)
LeaseId = NewType("LeaseId", UUID)
ChallengeId = NewType("ChallengeId", UUID)
OwnerId = NewType("OwnerId", UUID)
InstanceId = NewType("InstanceId", UUID)


class _PrivateValue:
    """No dict, pickle, or dataclass projection can expose the private value."""

    __slots__ = ("__value",)

    def __init__(self, value: str) -> None:
        if hasattr(self, "_PrivateValue__value"):
            raise AttributeError("private values are immutable")
        if not isinstance(value, str):
            raise TypeError("private values must be text")
        object.__setattr__(self, "_PrivateValue__value", value)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("private values are immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("private values are immutable")

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"

    def __str__(self) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented
        return self.__value == other.__value

    def __hash__(self) -> int:
        return hash((type(self), self.__value))

    def __getstate__(self) -> object:
        raise TypeError("private values cannot be serialized")

    def __reduce__(self) -> object:
        raise TypeError("private values cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("private values cannot be serialized")


class SensitiveText(_PrivateValue):
    """Transient user input, revealed only at the backend boundary."""

    __slots__ = ()

    def reveal_for_backend(self) -> str:
        return self._PrivateValue__value


class OpaqueCredentialRef(_PrivateValue):
    __slots__ = ()


class OpaqueProvisionalCredentialRef(_PrivateValue):
    __slots__ = ()


class OpaqueBackendToken(_PrivateValue):
    __slots__ = ()


@dataclass(frozen=True, kw_only=True)
class OperationContext:
    operation_id: OperationId
    session_generation: int
    endpoint_generation: int
    transport_lease_id: LeaseId | None = None
    trust_generation: int | None = None
    audio_operation_id: OperationId | None = None

    def __post_init__(self) -> None:
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("endpoint_generation", self.endpoint_generation)
        if self.trust_generation is not None:
            _validate_generation("trust_generation", self.trust_generation)


@dataclass(frozen=True, kw_only=True)
class ProcessIdentity:
    pid: int
    start_ticks: int


@dataclass(frozen=True, kw_only=True)
class StreamHandle:
    receiver_id: ReceiverId
    endpoint_generation: int
    endpoint_fingerprint: str
    backend_stream_id: str
    daemon_instance_uuid: InstanceId
    lease_id: LeaseId
    start_operation_id: OperationId

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)


@dataclass(frozen=True, kw_only=True)
class DisplayHandle:
    owner_uuid: OwnerId
    source: VirtualOutputSource
    helper: ProcessIdentity


@dataclass(frozen=True, kw_only=True)
class PairingChallenge:
    """Coordinator-private; publish a ChallengeView to consumers instead."""

    challenge_id: ChallengeId
    receiver_id: ReceiverId
    endpoint_fingerprint: str
    endpoint_generation: int
    operation_id: OperationId
    session_generation: int
    trust_generation: int
    created_at: float
    expires_at: float
    credential_kind: Literal["pin", "password"]
    backend_token: OpaqueBackendToken = field(repr=False)

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("trust_generation", self.trust_generation)


@dataclass(frozen=True, kw_only=True)
class ChallengeView:
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    endpoint_fingerprint: str
    endpoint_generation: int
    operation_id: OperationId
    session_generation: int
    trust_generation: int
    created_at: float
    expires_at: float
    credential_kind: Literal["pin", "password"]

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)
        _validate_generation("session_generation", self.session_generation)
        _validate_generation("trust_generation", self.trust_generation)


@dataclass(frozen=True, kw_only=True)
class PairingResult:
    kind: Literal[
        "Trusted", "InvalidInput", "ReceiverRejected", "Cancelled",
        "CredentialUnavailable", "TrustCleanupRequired", "TransportFailure",
    ]
    credential_ref: OpaqueCredentialRef | None
    diagnostic: Diagnostic | None
