"""Physical-monitor-only capture selection, verification, and lease release.

Mirror mode may only capture a real monitor connector.  This module takes the
connector inventory from a separately verified source (KScreen JSON, sysfs, or
the composition root's equivalent), asks the portal for exactly that monitor,
and remembers each acquired portal session under its own capture lease.  A
window, a helper-created virtual output, an unknown connector, or a portal
stream that cannot be bound to the requested connector is never accepted.

Restore tokens live only in this adapter's private memory (and in the selected
:class:`PhysicalMonitorSource`); they are never persisted, logged, or put in a
diagnostic.  A failed or cancelled selection erases that connector's token so
the next attempt reacquires permission through the portal.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Final
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import LeaseId
from airplay_linux.domain.ports import CommandOutput, CommandSpec, IdFactory, ProcessPort
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Ok,
    PhysicalMonitorSource,
    Result,
)

from .portal import PortalClient, PortalMonitor

#: Read-only inventory probe; it never changes a display configuration.
KSCREEN_PROBE: Final[tuple[str, ...]] = ("kscreen-doctor", "-j")
INVENTORY_TIMEOUT: Final[float] = 5.0

#: ``KScreen::Output::Type`` members that denote a physical monitor connector.
#: The installed KScreen schema serialises this enum as an integer and has no
#: virtual member, so an absent, string, ``Unknown`` (0), television, or
#: out-of-range value is unproven origin and is never reported as physical.
PHYSICAL_OUTPUT_TYPES: Final[frozenset[int]] = frozenset(
    {
        1,  # VGA
        2,  # DVI
        3,  # DVII
        4,  # DVIA
        5,  # DVID
        6,  # HDMI
        7,  # Panel
        14,  # DisplayPort
    }
)

_CONNECTOR_NAME: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){0,3}"
)
_NON_PHYSICAL_MARKERS: Final[tuple[str, ...]] = (
    "virtual",
    "window",
    "screen-cast",
    "screencast",
    "region",
    "workspace",
)


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _is_physical_connector(connector: object) -> bool:
    """A real output connector name, never a window/region/virtual identity."""
    if type(connector) is not str or not 1 <= len(connector) <= 64:
        return False
    lowered = connector.casefold()
    if any(marker in lowered for marker in _NON_PHYSICAL_MARKERS):
        return False
    return _CONNECTOR_NAME.fullmatch(connector) is not None


def _is_physical_output_type(output_type: object) -> bool:
    """Whether a KScreen output type positively identifies physical hardware.

    Absence of a virtual marker is not evidence of a physical origin: only a
    known physical connector type in the installed schema is accepted.
    """
    return type(output_type) is int and output_type in PHYSICAL_OUTPUT_TYPES


def _physical_connectors(connectors: object) -> tuple[str, ...]:
    if type(connectors) is not tuple:
        return ()
    selected: list[str] = []
    for connector in connectors:
        if _is_physical_connector(connector) and connector not in selected:
            selected.append(connector)
    return tuple(selected)


def is_physical_descriptor(source: object, connectors: tuple[str, ...]) -> bool:
    """Whether ``source`` is a physical monitor that this inventory contains.

    Only the exact domain descriptor is accepted: an application window, a
    helper-created virtual output, and any duck-typed object with a
    ``connector`` attribute are all rejected.
    """
    if type(source) is not PhysicalMonitorSource:
        return False
    if type(connectors) is not tuple:
        return False
    connector = source.connector
    if not _is_physical_connector(connector):
        return False
    return connector in connectors


class _UuidFactory:
    def new(self):
        return uuid4()


class KscreenConnectorInventory:
    """Read-only ``kscreen-doctor -j`` connector inventory.

    An output is reported only once its physical origin is positively proven: it
    is enabled, it is explicitly connected, its name is a real connector name
    that is not a window/virtual identity, and its ``type`` is a known physical
    connector type from the installed KScreen schema.  An unproven entry is
    simply absent from the inventory, which callers treat as "nothing can be
    verified" rather than as permission to capture something else.

    A failed probe raises :class:`InventoryProbeError`; an empty tuple therefore
    always means a successfully verified empty inventory.
    """

    def __init__(self, process: ProcessPort, *, timeout: float = INVENTORY_TIMEOUT) -> None:
        self._process = process
        self._timeout = timeout

    async def __call__(self) -> tuple[str, ...]:
        try:
            result = await self._process.run(
                CommandSpec(argv=KSCREEN_PROBE, env={}), self._timeout
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return ()
        if not isinstance(result, Ok) or type(result.value) is not CommandOutput:
            return ()
        output = result.value
        if output.exit_code != 0:
            return ()
        try:
            document = json.loads(output.stdout)
        except (TypeError, ValueError):
            return ()
        if type(document) is not dict:
            return ()
        outputs = document.get("outputs")
        if type(outputs) is not list:
            return ()

        selected: list[str] = []
        for entry in outputs:
            if type(entry) is not dict:
                continue
            connector = entry.get("name")
            if not _is_physical_connector(connector):
                continue
            if entry.get("enabled") is not True:
                continue
            if entry.get("connected") is not True:
                continue
            if not _is_physical_output_type(entry.get("type")):
                continue
            if connector not in selected:
                selected.append(connector)
        return tuple(selected)


class PhysicalCaptureProvider:
    """CapturePort implementation backed by monitor-only portal sessions."""

    def __init__(
        self,
        portal: PortalClient,
        connectors: Callable[[], Awaitable[tuple[str, ...]]],
        features: object,
        *,
        ids: IdFactory | None = None,
    ) -> None:
        self._portal = portal
        self._connectors = connectors
        self._features = features
        self._ids = ids or _UuidFactory()
        self._leases: dict[LeaseId, PortalMonitor] = {}
        #: Exact acquired sessions whose release was never confirmed.  A refused
        #: source still owns a live portal session, so it is retained here until a
        #: later retry proves it closed instead of being discarded with its lease.
        self._unconfirmed: dict[str, PortalMonitor] = {}
        self._restore_tokens: dict[str, str] = {}
        self._select_lock = asyncio.Lock()

    # -- capability and inventory ------------------------------------------

    def _supports_physical(self) -> bool:
        try:
            return self._features.supports("physical_source") is True
        except Exception:
            return False

    async def _inventory(self) -> Result[tuple[str, ...]]:
        """Probe the connector inventory; a failed probe is a typed failure.

        An exception or a wrongly shaped probe result must never look like an
        empty but healthy inventory, so callers fail closed instead of offering
        a physical source that was never verified.
        """
        try:
            connectors = await self._connectors()
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure(ErrorCode.unsupported_source)
        if type(connectors) is not tuple:
            return _failure(ErrorCode.unsupported_source)
        return Ok(value=_physical_connectors(connectors))

    # -- CapturePort --------------------------------------------------------

    async def list_physical(self) -> Result[tuple[PhysicalMonitorSource, ...]]:
        if not self._supports_physical():
            return _failure(ErrorCode.unsupported_source)
        inventory = await self._inventory()
        if isinstance(inventory, Err):
            return inventory
        return Ok(
            value=tuple(
                PhysicalMonitorSource(
                    connector=connector,
                    portal_restore_token=None,
                    capture_lease_id=None,
                )
                for connector in inventory.value
            )
        )

    async def select(self, connector: str) -> Result[PhysicalMonitorSource]:
        if type(connector) is not str or not connector:
            return _failure(ErrorCode.invalid_input)
        if not self._supports_physical():
            return _failure(ErrorCode.unsupported_source)

        async with self._select_lock:
            for session, monitor in tuple(self._unconfirmed.items()):
                if isinstance(await self._release_exact(monitor), Ok):
                    self._unconfirmed.pop(session, None)
            inventory = await self._inventory()
            if isinstance(inventory, Err):
                return inventory
            requested = PhysicalMonitorSource(
                connector=connector, portal_restore_token=None
            )
            if not is_physical_descriptor(requested, inventory.value):
                return _failure(ErrorCode.unsupported_source)

            token = self._restore_tokens.get(connector)
            try:
                acquired = await self._portal.select_monitor(connector, token)
            except asyncio.CancelledError:
                self._restore_tokens.pop(connector, None)
                raise
            except Exception:
                self._restore_tokens.pop(connector, None)
                return _failure(ErrorCode.dependency_incompatible)

            if isinstance(acquired, Err):
                # Denial, cancellation, or an expired token: reacquire next time.
                self._restore_tokens.pop(connector, None)
                return acquired

            monitor = acquired.value
            if (
                type(monitor) is not PortalMonitor
                or monitor.connector != connector
                or monitor.source_type != "physical_monitor"
            ):
                self._restore_tokens.pop(connector, None)
                if type(monitor) is PortalMonitor:
                    released = await self._release_exact(monitor)
                    if isinstance(released, Err):
                        # The refused source still owns a live session: report the
                        # unconfirmed cleanup rather than a plain refusal.
                        return _failure(ErrorCode.cleanup_timeout)
                return _failure(ErrorCode.unsupported_source)

            lease = LeaseId(self._ids.new())
            self._leases[lease] = monitor
            if monitor.restore_token is not None:
                self._restore_tokens[connector] = monitor.restore_token
            else:
                self._restore_tokens.pop(connector, None)
            return Ok(
                value=PhysicalMonitorSource(
                    connector=connector,
                    portal_restore_token=monitor.restore_token,
                    capture_lease_id=lease,
                )
            )

    async def verify(self, source: PhysicalMonitorSource) -> Result[None]:
        if not self._supports_physical():
            return _failure(ErrorCode.unsupported_source)

        inventory = await self._inventory()
        if isinstance(inventory, Err):
            return inventory
        if not is_physical_descriptor(source, inventory.value):
            return _failure(ErrorCode.unsupported_source)

        lease = source.capture_lease_id
        if lease is None:
            return _failure(ErrorCode.unsupported_source)
        try:
            monitor = self._leases.get(lease)
        except TypeError:
            return _failure(ErrorCode.unsupported_source)
        if monitor is None:
            return _failure(ErrorCode.stale_event)
        if monitor.connector != source.connector:
            return _failure(ErrorCode.stale_event)
        try:
            still_open = self._portal.is_open(monitor)
        except Exception:
            still_open = False
        if still_open is not True:
            # A stale selected source can never silently restart capture.
            return _failure(ErrorCode.stale_event)
        return Ok(value=None)

    async def release(self, source: PhysicalMonitorSource) -> Result[None]:
        if type(source) is not PhysicalMonitorSource:
            return _failure(ErrorCode.invalid_input)
        lease = source.capture_lease_id
        if lease is None:
            return Ok(value=None)
        try:
            monitor = self._leases.get(lease)
        except TypeError:
            return Ok(value=None)
        if monitor is None:
            return Ok(value=None)
        if monitor.connector != source.connector:
            return _failure(ErrorCode.stale_event)
        try:
            released = await self._portal.release_monitor(monitor)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure(ErrorCode.cleanup_timeout)
        if isinstance(released, Err):
            # Keep the lease: the session is not confirmed released yet.
            return released
        self._leases.pop(lease, None)
        return Ok(value=None)

    async def _release_exact(self, monitor: PortalMonitor) -> Result[None]:
        """Release one exact monitor, retaining it until the release is confirmed."""
        session = monitor.portal_session_id
        try:
            released = await self._portal.release_monitor(monitor)
        except asyncio.CancelledError:
            # The caller gave up; the session is still owned by this provider.
            self._unconfirmed[session] = monitor
            raise
        except Exception:
            self._unconfirmed[session] = monitor
            return _failure(ErrorCode.cleanup_timeout)
        if isinstance(released, Err):
            self._unconfirmed[session] = monitor
            return released
        self._unconfirmed.pop(session, None)
        return Ok(value=None)


__all__ = [
    "KscreenConnectorInventory",
    "PhysicalCaptureProvider",
    "is_physical_descriptor",
]
