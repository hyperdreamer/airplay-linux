"""Physical-monitor mirror capture adapters."""

from __future__ import annotations

from typing import Any

__all__ = [
    "KscreenConnectorInventory",
    "PhysicalCaptureProvider",
    "PortalClient",
    "is_physical_descriptor",
]


def __getattr__(name: str) -> Any:
    if name in {"KscreenConnectorInventory", "PhysicalCaptureProvider", "is_physical_descriptor"}:
        from .physical import KscreenConnectorInventory, PhysicalCaptureProvider, is_physical_descriptor
        return {
            "KscreenConnectorInventory": KscreenConnectorInventory,
            "PhysicalCaptureProvider": PhysicalCaptureProvider,
            "is_physical_descriptor": is_physical_descriptor,
        }[name]
    if name == "PortalClient":
        from .portal import PortalClient
        return PortalClient
    raise AttributeError(name)
