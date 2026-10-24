"""Physical-monitor mirror capture: portal selection, verification, release.

Import from here rather than the adapter-private portal monitor type: callers
only ever see :class:`~airplay_linux.domain.resources.PhysicalMonitorSource`.
"""

from __future__ import annotations

from .physical import (
    KscreenConnectorInventory,
    PhysicalCaptureProvider,
    is_physical_descriptor,
)
from .portal import PortalClient

__all__ = [
    "KscreenConnectorInventory",
    "PhysicalCaptureProvider",
    "PortalClient",
    "is_physical_descriptor",
]
