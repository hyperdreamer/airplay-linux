"""Discovery identities and endpoint-bound receiver descriptions."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, NewType


ReceiverId = NewType("ReceiverId", str)


def _validate_generation(name: str, value: int) -> None:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")


@dataclass(frozen=True, kw_only=True)
class ServiceRecord:
    key: str
    service_type: Literal["_airplay._tcp", "_raop._tcp"]
    instance: str
    model: str
    device_id: str | None
    host: str
    addresses: tuple[str, ...]
    port: int
    txt: Mapping[str, str]
    seen_at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "txt", MappingProxyType(dict(self.txt)))


@dataclass(frozen=True, kw_only=True)
class ReceiverRecordSet:
    records: tuple[ServiceRecord, ...]
    receiver_id: ReceiverId
    identity_kind: Literal["device", "fingerprint", "endpoint", "ambiguous"]
    canonical_address: str
    endpoint_fingerprint: str
    endpoint_generation: int
    persistable: bool
    continuity_verified: bool
    replacement_of: ReceiverId | None
    ambiguous: bool

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)


@dataclass(frozen=True, kw_only=True)
class ReceiverTarget:
    receiver_id: ReceiverId
    service_records: tuple[ServiceRecord, ...]
    canonical_host: str
    service_port: int
    protocol: str
    endpoint_fingerprint: str
    endpoint_generation: int

    def __post_init__(self) -> None:
        _validate_generation("endpoint_generation", self.endpoint_generation)


@dataclass(frozen=True, kw_only=True)
class ReceiverView:
    record_set: ReceiverRecordSet
    display_name: str
    online: bool
    pairing_hint: str | None
