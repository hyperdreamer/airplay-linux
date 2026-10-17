"""Typed, secret-free builders for deterministic adapter tests."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from airplay_linux.domain.identity import ReceiverId, ReceiverTarget, ServiceRecord
from airplay_linux.domain.operations import (
    DisplayHandle,
    LeaseId,
    OperationContext,
    OperationId,
    OwnerId,
    ProcessIdentity,
)
from airplay_linux.domain.resources import PhysicalMonitorSource, Size, VirtualOutputSource

DEFAULT_ADDRESS = "192.0.2.10"
DEFAULT_PORT = 7000


def target(
    receiver: str = "a",
    endpoint_generation: int = 1,
    address: str = "192.0.2.10",
) -> ReceiverTarget:
    """Build a fresh endpoint-bound target for a synthetic receiver."""
    return ReceiverTarget(
        receiver_id=ReceiverId(f"airplay-device:{receiver}"),
        service_records=(service(address=address),),
        canonical_host=address,
        service_port=DEFAULT_PORT,
        protocol="airplay",
        endpoint_fingerprint=f"endpoint-{receiver}-v{endpoint_generation}",
        endpoint_generation=endpoint_generation,
    )


def physical(connector: str = "DP-1") -> PhysicalMonitorSource:
    """Build a physical monitor source; fake capture never offers anything else."""
    return PhysicalMonitorSource(connector=connector, portal_restore_token=None)


def virtual_handle(owner: int = 1, output: str = "v1") -> DisplayHandle:
    """Build an exact owned virtual-output handle for stale/foreign-handle tests."""
    return DisplayHandle(
        owner_uuid=OwnerId(UUID(int=owner)),
        source=VirtualOutputSource(
            connector=f"Virtual-{output}",
            kscreen_output_id=output,
            size=Size(width=1920, height=1080),
        ),
        helper=ProcessIdentity(pid=4000 + owner, start_ticks=90000 + owner),
    )


def context(
    generation: int = 1,
    endpoint_generation: int = 1,
    lease: int | None = 1,
    trust_generation: int = 0,
    **overrides: Any,
) -> OperationContext:
    """Build a typed operation fence; every field is explicit and overridable."""
    defaults: dict[str, Any] = {
        "operation_id": OperationId(UUID(int=1000 + generation)),
        "session_generation": generation,
        "endpoint_generation": endpoint_generation,
        "transport_lease_id": None if lease is None else LeaseId(UUID(int=lease)),
        "trust_generation": trust_generation,
        "audio_operation_id": None,
    }
    return _build(OperationContext, defaults, overrides)


def service(
    kind: str = "airplay",
    address: str = DEFAULT_ADDRESS,
    **overrides: Any,
) -> ServiceRecord:
    """Build a discovery service record; ``kind`` selects the backend service type."""
    service_types = {"airplay": "_airplay._tcp", "raop": "_raop._tcp"}
    if kind not in service_types:
        raise ValueError(f"unknown service kind: {kind}")
    defaults: dict[str, Any] = {
        "key": f"tv-{kind}:{address}",
        "service_type": service_types[kind],
        "instance": f"Living Room {address}",
        "model": "AppleTV6,2",
        "device_id": "AA:BB:CC:DD:EE:FF",
        "host": "living-room.local",
        "addresses": (address,),
        "port": DEFAULT_PORT,
        "txt": {"model": "AppleTV6,2"},
        "seen_at": 1.0,
    }
    return _build(ServiceRecord, defaults, overrides)


def _build(cls: type, defaults: dict[str, Any], overrides: dict[str, Any]) -> Any:
    unknown = sorted(set(overrides) - set(defaults))
    if unknown:
        names = ", ".join(unknown)
        raise TypeError(f"{cls.__name__} has no field(s): {names}")
    return cls(**(defaults | overrides))
