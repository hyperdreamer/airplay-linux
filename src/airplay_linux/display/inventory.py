"""Shared, read-only KScreen inventory and physical-origin projections."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Final

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput, CommandSpec, ProcessPort
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result, Size
from airplay_linux.display.kscreen import KScreenParseError, _parse_records

KSCREEN_PROBE: Final[tuple[str, ...]] = ("kscreen-doctor", "-j")
INVENTORY_TIMEOUT: Final[float] = 5.0

PHYSICAL_OUTPUT_TYPES: Final[frozenset[int]] = frozenset({1, 2, 3, 4, 5, 6, 7, 14})
PHYSICAL_OUTPUT_TYPE_NAMES: Final[frozenset[str]] = frozenset({
    "vga", "dvi", "dvii", "dvi-i", "dvia", "dvid", "hdmi", "panel", "displayport",
})

_CONNECTOR_NAME: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+){0,3}"
)
_NON_PHYSICAL_MARKERS: Final[tuple[str, ...]] = (
    "virtual", "window", "screen-cast", "screencast", "region", "workspace",
)


@dataclass(frozen=True, kw_only=True)
class MonitoredOutput:
    """One output as observed in the same JSON document as its geometry."""

    name: str
    enabled: bool
    connected: bool
    output_type: int | str | None
    size: Size | None
    position: tuple[int, int] | None
    virtual: bool | None

    @property
    def connector(self) -> str:
        """Compatibility name used by the delivered capture provider."""
        return self.name


def _is_physical_connector(connector: object) -> bool:
    if type(connector) is not str or not 1 <= len(connector) <= 64:
        return False
    lowered = connector.casefold()
    if any(marker in lowered for marker in _NON_PHYSICAL_MARKERS):
        return False
    return _CONNECTOR_NAME.fullmatch(connector) is not None


def _is_physical_output_type(output_type: object) -> bool:
    if type(output_type) is int:
        return output_type in PHYSICAL_OUTPUT_TYPES
    if type(output_type) is str:
        return output_type.casefold() in PHYSICAL_OUTPUT_TYPE_NAMES
    return False


def _failure() -> Err:
    return Err(error=diagnostic(ErrorCode.unsupported_source))


def _decode_document(output: CommandOutput) -> object | None:
    if type(output.exit_code) is not int or output.exit_code != 0:
        return None
    try:
        return json.loads(output.stdout)
    except (TypeError, UnicodeDecodeError, ValueError):
        return None


class KScreenInventory:
    """Read one complete KScreen document and retain no ambient layout state."""

    def __init__(self, process: ProcessPort, *, timeout: float = INVENTORY_TIMEOUT) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise TypeError("inventory timeout must be a number")
        if timeout <= 0:
            raise ValueError("inventory timeout must be positive")
        self._process = process
        self._timeout = float(timeout)

    async def observe(self) -> Result[tuple[MonitoredOutput, ...]]:
        return await self._observe(require_geometry=True)

    async def observe_connectors(self) -> Result[tuple[MonitoredOutput, ...]]:
        """Project connector metadata without requiring geometry."""
        return await self._observe(require_geometry=False)

    async def _observe(self, *, require_geometry: bool) -> Result[tuple[MonitoredOutput, ...]]:
        try:
            result = await asyncio.wait_for(
                self._process.run(CommandSpec(argv=KSCREEN_PROBE, env={}), self._timeout),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, Exception):
            return _failure()
        if not isinstance(result, Ok) or type(result.value) is not CommandOutput:
            return _failure()
        document = _decode_document(result.value)
        if document is None:
            return _failure()
        try:
            records = _parse_records(document)
        except (KScreenParseError, TypeError, ValueError):
            return _failure()

        outputs: list[MonitoredOutput] = []
        for record in records:
            monitored = MonitoredOutput(
                name=record.connector,
                enabled=record.enabled is True,
                connected=record.connected is True,
                output_type=record.output_type,
                size=record.size,
                position=record.position,
                virtual=record.virtual,
            )
            if require_geometry and monitored.enabled and (
                monitored.size is None or monitored.position is None
            ):
                return _failure()
            outputs.append(monitored)
        return Ok(value=tuple(outputs))


def _geometry(output: object) -> tuple[tuple[int, int], tuple[int, int]] | None:
    size = getattr(output, "size", None)
    position = getattr(output, "position", None)
    if not isinstance(size, Size) or type(position) is not tuple or len(position) != 2:
        return None
    if any(type(value) is not int for value in position):
        return None
    return (size.width, size.height), position


def verify_reported_source(
    requested: str, reported: object, layout: tuple[object, ...],
) -> Result[None]:
    """Verify backend source geometry against one fresh, typed KScreen layout."""
    from airplay_linux.backend.contract import BackendSource

    if type(requested) is not str or not requested or not _is_physical_connector(requested):
        return _failure()
    if not isinstance(reported, BackendSource):
        return _failure()
    if (
        type(reported.type) is not int or reported.type != 1
        or type(reported.width) is not int or reported.width <= 0
        or type(reported.height) is not int or reported.height <= 0
        or type(reported.x) is not int or type(reported.y) is not int
    ):
        return _failure()
    if type(layout) is not tuple:
        return _failure()

    matches: list[object] = []
    for output in layout:
        enabled = getattr(output, "enabled", None)
        if type(enabled) is not bool:
            return _failure()
        if not enabled:
            continue
        geometry = _geometry(output)
        if geometry is None:
            return _failure()
        (width, height), position = geometry
        if (width, height, *position) == (
            reported.width, reported.height, reported.x, reported.y,
        ):
            matches.append(output)
    if len(matches) != 1:
        return _failure()
    output = matches[0]
    if getattr(output, "name", getattr(output, "connector", None)) != requested:
        return _failure()
    if getattr(output, "connected", None) is not True:
        return _failure()
    if getattr(output, "virtual", None) is not False:
        return _failure()
    if not _is_physical_output_type(getattr(output, "output_type", None)):
        return _failure()
    if not _is_physical_connector(getattr(output, "connector", None)):
        return _failure()
    return Ok(value=None)


class KScreenReportedSourceVerifier:
    """Async verifier that obtains exactly one fresh inventory per check."""

    def __init__(self, inventory: KScreenInventory, *, physical_inventory_available: bool) -> None:
        if not isinstance(inventory, KScreenInventory):
            raise TypeError("source verifier requires a KScreenInventory")
        if type(physical_inventory_available) is not bool:
            raise TypeError("physical inventory availability must be a boolean")
        self.inventory = inventory
        self.physical_inventory_available = physical_inventory_available

    async def verify_reported_source(self, requested: str, reported: object) -> Result[None]:
        if not self.physical_inventory_available:
            return _failure()
        observed = await self.inventory.observe()
        if isinstance(observed, Err):
            return observed
        return verify_reported_source(requested, reported, observed.value)

    async def verify(self, requested: str, reported: object) -> Result[None]:
        return await self.verify_reported_source(requested, reported)


__all__ = [
    "INVENTORY_TIMEOUT",
    "KSCREEN_PROBE",
    "KScreenInventory",
    "KScreenReportedSourceVerifier",
    "MonitoredOutput",
    "PHYSICAL_OUTPUT_TYPE_NAMES",
    "PHYSICAL_OUTPUT_TYPES",
    "verify_reported_source",
]
