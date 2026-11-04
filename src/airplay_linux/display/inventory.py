"""Shared, read-only KScreen inventory and physical-origin projections."""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput, CommandSpec, ProcessPort
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result, Size
from airplay_linux.backend.contract import BackendSource
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
    enabled: bool | None
    connected: bool
    output_type: int | str | None
    size: Size | None
    position: tuple[int, int] | None
    virtual: bool | None
    scale: float | None = None
    output_id: str | None = None

    @property
    def connector(self) -> str:
        """Compatibility name used by the delivered capture provider."""
        return self.name


class SourceRefusal(StrEnum):
    """Stable, sanitized reasons for a source observation refusal."""

    INVALID_REQUESTED_CONNECTOR = "invalid_requested_connector"
    MISSING_SOURCE = "missing_source"
    MALFORMED_SOURCE = "malformed_source"
    MISSING_POSITION = "missing_position"
    NON_MONITOR_SOURCE = "non_monitor_source"
    UNREADABLE_LAYOUT = "unreadable_layout"
    NO_POSITION_MATCH = "no_position_match"
    AMBIGUOUS_POSITION = "ambiguous_position"
    SIZE_MISMATCH = "size_mismatch"
    DIFFERENT_CONNECTOR = "different_connector"
    VIRTUAL_OUTPUT = "virtual_output"
    UNKNOWN_ORIGIN = "unknown_origin"
    OWNED_OUTPUT = "owned_output"
    DISCONNECTED_OUTPUT = "disconnected_output"
    INVENTORY_UNAVAILABLE = "inventory_unavailable"
    OWNERSHIP_UNAVAILABLE = "ownership_unavailable"
    PHYSICAL_INVENTORY_UNAVAILABLE = "physical_inventory_unavailable"


@dataclass(frozen=True, kw_only=True)
class SourceObservation:
    """Typed evidence from one source check, including a refusal reason."""

    reported: BackendSource | None
    matched_connector: str | None
    reason: SourceRefusal | None = None

    def __post_init__(self) -> None:
        if self.reported is not None and not isinstance(self.reported, BackendSource):
            raise TypeError("reported source must be a BackendSource or None")
        if self.matched_connector is not None and type(self.matched_connector) is not str:
            raise TypeError("matched connector must be text or None")
        if self.reason is not None and not isinstance(self.reason, SourceRefusal):
            raise TypeError("source observation reason must be a SourceRefusal")
        accepted = self.reason is None
        if accepted and (self.reported is None or not self.matched_connector):
            raise ValueError("accepted source observation is incomplete")
        if not accepted and self.matched_connector is not None:
            raise ValueError("refused source observation cannot name a match")

    @property
    def accepted(self) -> bool:
        return self.reason is None


@dataclass(frozen=True, kw_only=True)
class SourceVerificationRefusal(Err):
    """An ``Err`` that retains the typed observation behind the refusal."""

    observation: SourceObservation

    def __post_init__(self) -> None:
        if self.observation.reason is None:
            raise ValueError("source refusal must contain a refusal reason")


def _refusal(
    reason: SourceRefusal, reported: BackendSource | None = None,
) -> SourceVerificationRefusal:
    observation = SourceObservation(
        reported=reported, matched_connector=None, reason=reason,
    )
    return SourceVerificationRefusal(
        error=replace(diagnostic(ErrorCode.unsupported_source), detail=reason.value),
        observation=observation,
    )


def _failure(reason: SourceRefusal = SourceRefusal.INVENTORY_UNAVAILABLE) -> SourceVerificationRefusal:
    return _refusal(reason)


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
                enabled=record.enabled,
                connected=record.connected is True,
                output_type=record.output_type,
                size=record.size,
                position=record.position,
                virtual=record.virtual,
                scale=record.scale,
                output_id=record.output_id,
            )
            if require_geometry and monitored.enabled and (
                monitored.size is None or monitored.position is None
            ):
                return _failure()
            outputs.append(monitored)
        return Ok(value=tuple(outputs))


def _geometry(output: object) -> tuple[Size, tuple[int, int], float] | None:
    size = getattr(output, "size", None)
    position = getattr(output, "position", None)
    scale = getattr(output, "scale", None)
    if not isinstance(size, Size) or type(position) is not tuple or len(position) != 2:
        return None
    if any(type(value) is not int for value in position):
        return None
    if type(scale) not in (int, float) or isinstance(scale, bool):
        return None
    if not math.isfinite(float(scale)) or float(scale) <= 0:
        return None
    return size, position, float(scale)


def _owned_output(output: object, owned_outputs: tuple[object, ...]) -> bool:
    connector = getattr(output, "connector", getattr(output, "name", None))
    output_id = getattr(output, "output_id", None)
    for record in owned_outputs:
        if getattr(record, "desired_state", None) not in {"active", "removing"}:
            continue
        record_connector = getattr(record, "connector", None)
        record_output_id = getattr(record, "output_id", None)
        if record_connector == connector:
            return True
        if output_id is not None and record_output_id == output_id:
            return True
    return False


def verify_reported_source(
    requested: str, reported: object, layout: tuple[object, ...], *,
    owned_outputs: tuple[object, ...] = (),
) -> Result[SourceObservation]:
    """Verify one reported source against one fresh, typed KScreen layout.

    Position is the primary key.  Portal dimensions are only corroboration after
    a unique enabled position match, converted from device-independent pixels to
    the output's current-mode dimensions using its observed scale.
    """
    if type(requested) is not str or not requested or not _is_physical_connector(requested):
        return _refusal(SourceRefusal.INVALID_REQUESTED_CONNECTOR)
    if reported is None:
        return _refusal(SourceRefusal.MISSING_SOURCE)
    if not isinstance(reported, BackendSource):
        return _refusal(SourceRefusal.MALFORMED_SOURCE)
    if reported.has_position is not True:
        return _refusal(SourceRefusal.MISSING_POSITION, reported)
    if reported.type != 1:
        return _refusal(SourceRefusal.NON_MONITOR_SOURCE, reported)
    if (
        type(reported.width) is not int or reported.width <= 0
        or type(reported.height) is not int or reported.height <= 0
        or type(reported.x) is not int or type(reported.y) is not int
    ):
        return _refusal(SourceRefusal.MALFORMED_SOURCE, reported)
    if type(layout) is not tuple or type(owned_outputs) is not tuple:
        return _refusal(SourceRefusal.UNREADABLE_LAYOUT, reported)

    matches: list[tuple[object, Size, float]] = []
    for output in layout:
        enabled = getattr(output, "enabled", None)
        if type(enabled) is not bool:
            return _refusal(SourceRefusal.UNREADABLE_LAYOUT, reported)
        if not enabled:
            continue
        geometry = _geometry(output)
        if geometry is None:
            return _refusal(SourceRefusal.UNREADABLE_LAYOUT, reported)
        size, position, scale = geometry
        if position == (reported.x, reported.y):
            matches.append((output, size, scale))
    if not matches:
        return _refusal(SourceRefusal.NO_POSITION_MATCH, reported)
    if len(matches) != 1:
        return _refusal(SourceRefusal.AMBIGUOUS_POSITION, reported)

    output, current_size, scale = matches[0]
    # The portal reports device-independent pixels; the observed layout reports
    # the output's current-mode dimensions. Converting forward (logical * scale)
    # is the only direction that compares like with like on a scaled output.
    native_size = (round(reported.width * scale), round(reported.height * scale))
    if native_size != (current_size.width, current_size.height):
        return _refusal(SourceRefusal.SIZE_MISMATCH, reported)
    connector = getattr(output, "connector", getattr(output, "name", None))
    if connector != requested:
        return _refusal(SourceRefusal.DIFFERENT_CONNECTOR, reported)
    if getattr(output, "virtual", None) is True:
        return _refusal(SourceRefusal.VIRTUAL_OUTPUT, reported)
    if getattr(output, "virtual", None) is None:
        return _refusal(SourceRefusal.UNKNOWN_ORIGIN, reported)
    if _owned_output(output, owned_outputs):
        return _refusal(SourceRefusal.OWNED_OUTPUT, reported)
    if getattr(output, "connected", None) is not True:
        return _refusal(SourceRefusal.DISCONNECTED_OUTPUT, reported)
    if not _is_physical_output_type(getattr(output, "output_type", None)):
        return _refusal(SourceRefusal.UNKNOWN_ORIGIN, reported)
    if not _is_physical_connector(connector):
        return _refusal(SourceRefusal.UNKNOWN_ORIGIN, reported)
    return Ok(value=SourceObservation(
        reported=reported, matched_connector=connector, reason=None,
    ))


class KScreenReportedSourceVerifier:
    """Async verifier that obtains exactly one fresh inventory per check."""

    def __init__(
        self, inventory: KScreenInventory, *, physical_inventory_available: bool,
        ownerships: object | None = None,
    ) -> None:
        if not isinstance(inventory, KScreenInventory):
            raise TypeError("source verifier requires a KScreenInventory")
        if type(physical_inventory_available) is not bool:
            raise TypeError("physical inventory availability must be a boolean")
        self.inventory = inventory
        self.physical_inventory_available = physical_inventory_available
        self.ownerships = ownerships
        self.last_observation: SourceObservation | None = None

    async def observe_reported_source(
        self, requested: str, reported: object,
    ) -> SourceObservation:
        if not self.physical_inventory_available:
            return SourceObservation(
                reported=reported if isinstance(reported, BackendSource) else None,
                matched_connector=None,
                reason=SourceRefusal.PHYSICAL_INVENTORY_UNAVAILABLE,
            )
        observed = await self.inventory.observe()
        if isinstance(observed, Err):
            if isinstance(observed, SourceVerificationRefusal):
                return observed.observation
            return SourceObservation(
                reported=reported if isinstance(reported, BackendSource) else None,
                matched_connector=None,
                reason=SourceRefusal.INVENTORY_UNAVAILABLE,
            )
        owned_outputs: tuple[object, ...] = ()
        if self.ownerships is not None:
            try:
                ownership_result = await self.ownerships.all()
            except Exception:
                return SourceObservation(
                    reported=reported if isinstance(reported, BackendSource) else None,
                    matched_connector=None,
                    reason=SourceRefusal.OWNERSHIP_UNAVAILABLE,
                )
            if not isinstance(ownership_result, Ok) or type(ownership_result.value) is not tuple:
                return SourceObservation(
                    reported=reported if isinstance(reported, BackendSource) else None,
                    matched_connector=None,
                    reason=SourceRefusal.OWNERSHIP_UNAVAILABLE,
                )
            owned_outputs = ownership_result.value
        checked = verify_reported_source(
            requested, reported, observed.value, owned_outputs=owned_outputs,
        )
        if isinstance(checked, Ok):
            return checked.value
        if isinstance(checked, SourceVerificationRefusal):
            return checked.observation
        return SourceObservation(
            reported=reported if isinstance(reported, BackendSource) else None,
            matched_connector=None,
            reason=SourceRefusal.UNREADABLE_LAYOUT,
        )

    async def verify_reported_source(
        self, requested: str, reported: object,
    ) -> Result[SourceObservation]:
        observation = await self.observe_reported_source(requested, reported)
        self.last_observation = observation
        if not observation.accepted:
            return _refusal(observation.reason, observation.reported)
        return Ok(value=observation)

    async def verify(self, requested: str, reported: object) -> Result[SourceObservation]:
        return await self.verify_reported_source(requested, reported)


__all__ = [
    "INVENTORY_TIMEOUT",
    "KSCREEN_PROBE",
    "KScreenInventory",
    "KScreenReportedSourceVerifier",
    "MonitoredOutput",
    "PHYSICAL_OUTPUT_TYPES",
    "PHYSICAL_OUTPUT_TYPE_NAMES",
    "SourceObservation",
    "SourceRefusal",
    "SourceVerificationRefusal",
    "verify_reported_source",
]
