"""Read-only KScreen observation and verified output configuration.

``kscreen-doctor`` is the only display authority this application has, and it is
a CLI whose ``-j`` document is a snapshot rather than a stream. Two consequences
shape this module:

* A layout is *evidence*. It is parsed strictly: an output this adapter cannot
  address (no numeric KScreen id or no connector name) is never assigned an
  invented identity, a duplicate id or connector is refused as ambiguous, and a
  reported size is the only geometry trusted. Current geometry comes from a
  reported current size or the exact current mode id, never from a preferred or
  merely advertised mode. Missing geometry is not guessed; such an output is
  simply not offered as a candidate, while the baseline still records its
  existence by connector so a preexisting output can never look new.
* Configuration is an *atomic command followed by a readback*. The installed
  contract takes ``output.<id>.<setting>`` tokens, where the mode value is the
  numeric mode id KScreen reports, never a ``WIDTHxHEIGHT`` string. Every
  requested value must be visible in the layout read afterwards, for the same
  connector and at the same resolution that was requested; a command that exits
  zero but changes nothing -- or changes another output -- is a failure, not a
  success.

Polling is asynchronous and deadline-based through the injected
:class:`~airplay_linux.domain.ports.Scheduler`; nothing here sleeps on the actor
thread. The deadline bounds one operation: no read starts at or after it, each
read is bounded by the budget that remains, and evidence that arrives after it is
never accepted. Subscribers observe changes that a poll actually discovered,
because the KScreen CLI has no push channel of its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from airplay_linux.diagnostics import ALLOWED_COMMANDS, diagnostic
from airplay_linux.domain.ports import Clock, CommandSpec, ProcessPort, Scheduler, Subscription
from airplay_linux.domain.records import LayoutSnapshot
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result, Size

#: The installed display-management command; it is in the diagnostics allowlist.
KSCREEN_COMMAND: Final[str] = "kscreen-doctor"

#: Control-call bound. It is the design's helper/KScreen appearance bound, reused
#: so a display read cannot outlive the interval the UI was promised.
KSCREEN_TIMEOUT: Final[float] = 5.0

#: How often a wait re-reads the layout while its predicate is unsatisfied.
DEFAULT_POLL_INTERVAL: Final[float] = 0.25

#: Settings this adapter knows how to translate into the installed CLI contract.
CONFIGURABLE_SETTINGS: Final[frozenset[str]] = frozenset(
    {"enabled", "mode", "size", "position", "scale"},
)

_DIGITS: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,10}")
_SIZE_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\s*([0-9]{1,5})\s*[xX]\s*([0-9]{1,5})\s*",
)
_POSITION_TEXT: Final[re.Pattern[str]] = re.compile(
    r"\s*(-?[0-9]{1,9})\s*,\s*(-?[0-9]{1,9})\s*",
)
_SCALE_TEXT: Final[re.Pattern[str]] = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*")

#: Connector names KScreen uses for compositor-created virtual outputs.
_VIRTUAL_CONNECTOR: Final[re.Pattern[str]] = re.compile(r"^virtual(?:[-_].*)?$", re.IGNORECASE)

#: Prefixes of the physical connector families in the installed KScreen contract.
_PHYSICAL_CONNECTOR: Final[re.Pattern[str]] = re.compile(
    r"^(?:e?DP|HDMI|DVI|VGA|LVDS|DSI|USB|TV|Panel)", re.IGNORECASE,
)

#: ``KScreen::Output::Type`` values 1..14 name physical connector families.
#: ``0`` is ``Unknown``: a virtual output whose connector name is unrecognised
#: stays unknown rather than being guessed in either direction.
_PHYSICAL_TYPES: Final[frozenset[int]] = frozenset(range(1, 15))

_MAX_SCALE: Final[float] = 64.0
_MAX_COORDINATE: Final[int] = 2**31 - 1


class KScreenParseError(ValueError):
    """A layout document that cannot be used as display evidence.

    The message describes the violated rule, never the offending backend value,
    so it is safe to classify without echoing a document into a diagnostic.
    """


@dataclass(frozen=True, kw_only=True)
class OutputInfo:
    """One KScreen output, projected onto the facts this application acts on."""

    output_id: str
    connector: str
    size: Size
    enabled: bool
    position: tuple[int, int]
    scale: float
    virtual: bool | None


@dataclass(frozen=True, kw_only=True)
class KScreenMode:
    """A mode KScreen reports for an output; ``mode_id`` is the id the CLI takes."""

    mode_id: str
    size: Size
    refresh_rate: float


@dataclass(frozen=True, kw_only=True)
class _Mode:
    mode_id: str
    size: Size
    refresh_rate: float


@dataclass(frozen=True, kw_only=True)
class _Record:
    """Everything one output entry established, including what was unreadable."""

    output_id: str | None
    connector: str
    enabled: bool | None
    position: tuple[int, int] | None
    scale: float | None
    size: Size | None
    current_mode_id: str | None
    modes: tuple[_Mode, ...]
    preferred_modes: tuple[str, ...]
    virtual: bool | None


@dataclass(frozen=True, kw_only=True)
class _Request:
    """A pre-validated configuration request; no backend call has happened yet."""

    enabled: bool | None = None
    mode_id: str | None = None
    size: Size | None = None
    position: tuple[int, int] | None = None
    scale: float | None = None


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _id_token(value: object) -> str | None:
    """A canonical KScreen numeric id, or ``None``; arbitrary text is refused."""
    if type(value) is int and 0 <= value <= 2**31 - 1:
        return str(value)
    if type(value) is str and _DIGITS.fullmatch(value):
        return str(int(value))
    return None


def _size_value(value: object) -> Size | None:
    if not isinstance(value, Mapping):
        return None
    width = value.get("width")
    height = value.get("height")
    if type(width) is not int or type(height) is not int:
        return None
    try:
        return Size(width=width, height=height)
    except (TypeError, ValueError):
        return None


def _position_value(value: object) -> tuple[int, int] | None:
    if not isinstance(value, Mapping):
        return None
    x = value.get("x")
    y = value.get("y")
    if type(x) is not int or type(y) is not int:
        return None
    if not (-_MAX_COORDINATE <= x <= _MAX_COORDINATE and -_MAX_COORDINATE <= y <= _MAX_COORDINATE):
        return None
    return (x, y)


def _finite_float(value: object) -> float | None:
    """``value`` as a finite float, or ``None``; conversion never raises.

    ``10**400`` is a syntactically valid JSON integer that the runtime cannot
    represent, so conversion failure is treated as an unusable value rather than
    allowed to escape the adapter as an ``OverflowError``.
    """
    if type(value) is bool or type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _document_number(value: object) -> float | None:
    """A finite number from backend evidence, or ``None`` for a non-number.

    A JSON *number* that is not a finite, representable value makes the document
    unusable rather than merely one unreadable property: it is refused as a
    typed parse error so the caller sees a failed read, not an exception.
    """
    number = _finite_float(value)
    if number is None and type(value) in (int, float) and type(value) is not bool:
        raise KScreenParseError("KScreen numeric value is not a finite number")
    return number


def _scale_value(value: object) -> float | None:
    number = _document_number(value)
    if number is None or not 0.0 < number <= _MAX_SCALE:
        return None
    return number


def _flat_scale(value: float) -> int | str:
    """A layout property value: an int when integral, else an exact decimal text."""
    if float(value).is_integer():
        return int(value)
    return repr(float(value))


def _parse_modes(value: object) -> tuple[_Mode, ...]:
    """The usable modes of one output; malformed entries are not selectable.

    A mode without a numeric id or with an invalid size cannot be named to the
    CLI, so it is dropped rather than repaired. A duplicate id is ambiguous and
    refuses the whole read: selecting either entry could configure a mode the
    caller did not ask for. A numeric field the runtime cannot represent refuses
    the read for the same reason -- the document is not usable evidence.
    """
    if not isinstance(value, list):
        return ()
    modes: list[_Mode] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, Mapping):
            continue
        mode_id = _id_token(entry.get("id"))
        size = _size_value(entry.get("size"))
        if mode_id is None or size is None:
            continue
        if mode_id in seen:
            raise KScreenParseError("duplicate KScreen mode id")
        seen.add(mode_id)
        refresh_rate = _document_number(entry.get("refreshRate"))
        modes.append(
            _Mode(
                mode_id=mode_id,
                size=size,
                refresh_rate=refresh_rate if refresh_rate is not None else 0.0,
            ),
        )
    return tuple(modes)


def _current_mode(value: object) -> str | None:
    mode_id = _id_token(value)
    # KScreen reports "0" for "no current mode"; it is not a mode id.
    if mode_id is None or mode_id == "0":
        return None
    return mode_id


def _preferred_modes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    preferred: list[str] = []
    for entry in value:
        mode_id = _id_token(entry)
        if mode_id is not None and mode_id not in preferred:
            preferred.append(mode_id)
    return tuple(preferred)


def _current_mode_size(
    current_mode_id: str | None, modes: tuple[_Mode, ...],
) -> Size | None:
    """The size of the mode KScreen reports as *current*, or ``None``.

    Only an exact current-mode identity proves current geometry. An advertised
    mode is a supported candidate, never evidence about what is displayed now, so
    preferred or first-listed modes are deliberately not consulted here; they
    belong to :func:`select_mode`, which chooses a requested configuration.
    """
    if current_mode_id is None:
        return None
    for mode in modes:
        if mode.mode_id == current_mode_id:
            return mode.size
    return None


def _virtual_value(connector: str, output_type: object) -> bool | None:
    """Whether the output is virtual, physical, or not provable from this entry."""
    if _VIRTUAL_CONNECTOR.match(connector):
        return True
    if _PHYSICAL_CONNECTOR.match(connector):
        return False
    if type(output_type) is str and output_type.strip().lower() == "virtual":
        return True
    if type(output_type) is int and output_type in _PHYSICAL_TYPES:
        return False
    return None


def _parse_records(raw: object) -> tuple[_Record, ...]:
    """Parse one ``kscreen-doctor -j`` document into per-output evidence.

    The document shape is strict because a partial layout cannot serve as a
    baseline: ``outputs`` must be a list. An entry that is not an object, or that
    has no connector name, is unaddressable and skipped. Duplicate output ids or
    connector names make the layout ambiguous and are refused outright.
    """
    if not isinstance(raw, Mapping):
        raise KScreenParseError("layout document is not an object")
    entries = raw.get("outputs")
    if not isinstance(entries, list):
        raise KScreenParseError("layout document has no outputs list")
    records: list[_Record] = []
    seen_ids: set[str] = set()
    seen_connectors: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        connector = entry.get("name")
        if type(connector) is not str or not connector:
            continue
        if connector in seen_connectors:
            raise KScreenParseError("duplicate KScreen connector name")
        seen_connectors.add(connector)
        output_id = _id_token(entry.get("id"))
        if output_id is not None:
            if output_id in seen_ids:
                raise KScreenParseError("duplicate KScreen output id")
            seen_ids.add(output_id)
        modes = _parse_modes(entry.get("modes"))
        current_mode_id = _current_mode(entry.get("currentModeId"))
        preferred = _preferred_modes(entry.get("preferredModes"))
        enabled = entry.get("enabled")
        records.append(
            _Record(
                output_id=output_id,
                connector=connector,
                enabled=enabled if type(enabled) is bool else None,
                position=_position_value(entry.get("pos")),
                scale=_scale_value(entry.get("scale")),
                size=_size_value(entry.get("size"))
                or _current_mode_size(current_mode_id, modes),
                current_mode_id=current_mode_id,
                modes=modes,
                preferred_modes=preferred,
                virtual=_virtual_value(connector, entry.get("type")),
            ),
        )
    return tuple(records)


def parse_outputs(raw: object) -> tuple[OutputInfo, ...]:
    """The fully describable outputs of a ``kscreen-doctor -j`` document.

    Outputs whose geometry is unavailable are omitted: :class:`OutputInfo`
    carries a real :class:`Size`, and inventing one would let a wrong output look
    like the requested virtual monitor. They remain visible in
    :meth:`KScreenObserver.snapshot` by connector.
    """
    return _outputs(_parse_records(raw))


def select_mode(
    modes: Iterable[KScreenMode], size: Size, preferred: Iterable[str] = (),
) -> KScreenMode | None:
    """The reported mode to use for ``size``, or ``None`` when none is advertised.

    Preference order is deterministic: an output-preferred mode, then the highest
    refresh rate, then the lowest mode id. No refresh guess is made from a
    ``WIDTHxHEIGHT`` label; only a mode KScreen actually reports can be selected.
    """
    candidates = [mode for mode in modes if mode.size == size]
    if not candidates:
        return None
    for mode_id in preferred:
        for mode in candidates:
            if mode.mode_id == mode_id:
                return mode
    return min(candidates, key=lambda mode: (-mode.refresh_rate, int(mode.mode_id)))


def layout_hash(outputs: Mapping[str, Mapping[str, str | int | bool]]) -> str:
    """A canonical fingerprint of a flat layout mapping.

    Canonical means sorted keys and no insignificant whitespace, so a document
    whose keys arrived in another order hashes identically.
    """
    canonical = {
        str(connector): {str(name): value for name, value in properties.items()}
        for connector, properties in outputs.items()
    }
    payload = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _snapshot_properties(record: _Record) -> dict[str, str | int | bool]:
    properties: dict[str, str | int | bool] = {}
    if record.output_id is not None:
        properties["id"] = record.output_id
    if record.enabled is not None:
        properties["enabled"] = record.enabled
    if record.size is not None:
        properties["width"] = record.size.width
        properties["height"] = record.size.height
        properties["size"] = f"{record.size.width}x{record.size.height}"
    if record.position is not None:
        x, y = record.position
        properties["x"], properties["y"] = x, y
        properties["position"] = f"{x},{y}"
    if record.scale is not None:
        properties["scale"] = _flat_scale(record.scale)
    if record.current_mode_id is not None:
        properties["mode"] = record.current_mode_id
    return properties


def _snapshot(records: tuple[_Record, ...]) -> LayoutSnapshot:
    outputs = {record.connector: _snapshot_properties(record) for record in records}
    return LayoutSnapshot(outputs=outputs, baseline_hash=layout_hash(outputs))


def _outputs(records: tuple[_Record, ...]) -> tuple[OutputInfo, ...]:
    outputs: list[OutputInfo] = []
    for record in records:
        if (
            record.output_id is None
            or record.enabled is None
            or record.position is None
            or record.scale is None
            or record.size is None
        ):
            continue
        outputs.append(
            OutputInfo(
                output_id=record.output_id,
                connector=record.connector,
                size=record.size,
                enabled=record.enabled,
                position=record.position,
                scale=record.scale,
                virtual=record.virtual,
            ),
        )
    outputs.sort(key=lambda output: int(output.output_id))
    return tuple(outputs)


def _scale_text(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _size_request(value: object) -> Size | None:
    if isinstance(value, Size):
        return value
    if type(value) is str:
        matched = _SIZE_TEXT.fullmatch(value)
        if matched is None:
            return None
        width, height = int(matched.group(1)), int(matched.group(2))
    elif type(value) in (tuple, list) and len(value) == 2:
        width, height = value
        if type(width) is not int or type(height) is not int:
            return None
    else:
        return None
    try:
        return Size(width=width, height=height)
    except (TypeError, ValueError):
        return None


def _position_request(value: object) -> tuple[int, int] | None:
    if type(value) is str:
        matched = _POSITION_TEXT.fullmatch(value)
        if matched is None:
            return None
        x, y = int(matched.group(1)), int(matched.group(2))
    elif type(value) in (tuple, list) and len(value) == 2:
        x, y = value
        if type(x) is not int or type(y) is not int:
            return None
    else:
        return None
    if not (-_MAX_COORDINATE <= x <= _MAX_COORDINATE and -_MAX_COORDINATE <= y <= _MAX_COORDINATE):
        return None
    return (x, y)


def _scale_request(value: object) -> float | None:
    if type(value) is str:
        matched = _SCALE_TEXT.fullmatch(value)
        if matched is None:
            return None
        try:
            number = float(matched.group(1))
        except (OverflowError, ValueError):
            return None
    else:
        number = _finite_float(value)
    if number is None or not 0.0 < number <= _MAX_SCALE:
        return None
    return number


def _request(values: object) -> _Request | None:
    """Validate a configuration mapping before any backend call.

    Unknown settings are refused rather than ignored: a caller that asked for a
    property this adapter does not apply must not believe it was applied.
    """
    if not isinstance(values, Mapping) or not values:
        return None
    if any(type(name) is not str or name not in CONFIGURABLE_SETTINGS for name in values):
        return None
    enabled = values.get("enabled")
    if "enabled" in values and type(enabled) is not bool:
        return None
    mode_id = _id_token(values["mode"]) if "mode" in values else None
    if "mode" in values and mode_id is None:
        return None
    size = _size_request(values["size"]) if "size" in values else None
    if "size" in values and size is None:
        return None
    position = _position_request(values["position"]) if "position" in values else None
    if "position" in values and position is None:
        return None
    scale = _scale_request(values["scale"]) if "scale" in values else None
    if "scale" in values and scale is None:
        return None
    return _Request(
        enabled=enabled if "enabled" in values else None,
        mode_id=mode_id,
        size=size,
        position=position,
        scale=scale,
    )


def _resolve_mode(record: _Record, request: _Request) -> Result[KScreenMode | None]:
    """The reported mode a request names, never a fabricated ``WIDTHxHEIGHT`` id."""
    if request.mode_id is not None:
        selected = next(
            (mode for mode in record.modes if mode.mode_id == request.mode_id), None,
        )
        if selected is None:
            return _failure(ErrorCode.unsupported_mode)
        if request.size is not None and selected.size != request.size:
            return _failure(ErrorCode.invalid_input)
        return Ok(value=KScreenMode(
            mode_id=selected.mode_id, size=selected.size, refresh_rate=selected.refresh_rate,
        ))
    if request.size is not None:
        selected = select_mode(record.modes, request.size, record.preferred_modes)
        if selected is None:
            return _failure(ErrorCode.unsupported_mode)
        return Ok(value=selected)
    return Ok(value=None)


def _tokens(output_id: str, request: _Request, mode: KScreenMode | None) -> tuple[str, ...]:
    tokens: list[str] = []
    if request.enabled is not None:
        tokens.append(
            f"output.{output_id}.enable" if request.enabled
            else f"output.{output_id}.disable",
        )
    if mode is not None:
        tokens.append(f"output.{output_id}.mode.{mode.mode_id}")
    if request.position is not None:
        x, y = request.position
        tokens.append(f"output.{output_id}.position.{x},{y}")
    if request.scale is not None:
        tokens.append(f"output.{output_id}.scale.{_scale_text(request.scale)}")
    return tuple(tokens)


def _verified(
    updated: _Record, original: _Record, request: _Request, mode: KScreenMode | None,
) -> bool:
    """Whether the readback proves every requested setting took effect.

    Readback is evidence about *this connector's current instance*, so it is
    fenced before any property is compared: a numeric output id can be reused by
    another connector, and a mode table can change between the command and the
    read. A requested mode must therefore still be current *and* still have the
    dimensions it had when it was selected, so an exit-zero command cannot be
    read as success for a different output or a different resolution.
    """
    if updated.connector != original.connector:
        return False
    if request.enabled is not None and updated.enabled != request.enabled:
        return False
    if mode is not None:
        if updated.current_mode_id != mode.mode_id:
            return False
        selected = next(
            (entry for entry in updated.modes if entry.mode_id == mode.mode_id), None,
        )
        if selected is None or selected.size != mode.size:
            return False
        if updated.size is not None and updated.size != mode.size:
            return False
    if request.position is not None and updated.position != request.position:
        return False
    if request.scale is not None:
        if updated.scale is None:
            return False
        if not math.isclose(updated.scale, request.scale, rel_tol=1e-9, abs_tol=1e-9):
            return False
    return True


class _Subscription:
    __slots__ = ("_action", "_closed")

    def __init__(self, action: Callable[[], None]) -> None:
        self._action: Callable[[], None] | None = action
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        action, self._action = self._action, None
        if action is not None:
            action()


class KScreenObserver:
    """Read and configure KScreen layouts without ever assuming an outcome."""

    def __init__(
        self,
        process: ProcessPort,
        clock: Clock,
        scheduler: Scheduler,
        *,
        command: str = KSCREEN_COMMAND,
        timeout: float = KSCREEN_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        if command not in ALLOWED_COMMANDS:
            raise ValueError("the display command must be an allowlisted executable name")
        if not (math.isfinite(float(timeout)) and float(timeout) > 0):
            raise ValueError("the display command timeout must be positive and finite")
        if not (math.isfinite(float(poll_interval)) and float(poll_interval) > 0):
            raise ValueError("the display poll interval must be positive and finite")
        self._process = process
        self._clock = clock
        self._scheduler = scheduler
        self._command = command
        self._timeout = float(timeout)
        self._poll_interval = float(poll_interval)
        self._subscribers: list[Callable[[tuple[OutputInfo, ...]], None]] = []
        self._observed: tuple[OutputInfo, ...] | None = None
        self._configure_lock = asyncio.Lock()

    # -- observation --------------------------------------------------------

    async def _records(self, timeout: float | None = None) -> Result[tuple[_Record, ...]]:
        """Run ``kscreen-doctor -j`` once and parse it, or return a typed failure.

        ``timeout`` bounds this one command; a caller with an operation deadline
        passes its *remaining* budget so a read can never outlive the operation it
        is evidence for. ``None`` uses the adapter's own control-call bound.
        """
        budget = self._timeout if timeout is None else max(0.0, float(timeout))
        result = await self._process.run(
            CommandSpec(argv=(self._command, "-j"), env={}), min(self._timeout, budget),
        )
        if isinstance(result, Err):
            return result
        output = result.value
        if output.exit_code != 0:
            return _failure(ErrorCode.configuration_failed)
        try:
            text = output.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return _failure(ErrorCode.configuration_failed)
        try:
            raw = json.loads(text)
        except (ValueError, RecursionError):
            return _failure(ErrorCode.configuration_failed)
        try:
            return Ok(value=_parse_records(raw))
        except (KScreenParseError, OverflowError):
            # A malformed number must leave as a failed read. Parsing already
            # refuses what it cannot represent; ``OverflowError`` stays caught as
            # a net so no conversion can escape the adapter's error contract.
            return _failure(ErrorCode.configuration_failed)

    def _publish(self, outputs: tuple[OutputInfo, ...]) -> None:
        """Notify subscribers only when a *successful* read observed a change.

        The first observation establishes the baseline silently; a failed read
        establishes nothing. A subscriber failure cannot break display polling,
        so one faulty callback does not starve the others.
        """
        previous = self._observed
        self._observed = outputs
        if previous is None or previous == outputs:
            return
        for callback in tuple(self._subscribers):
            try:
                callback(outputs)
            except Exception:  # noqa: BLE001 - a subscriber must not break observation
                continue

    async def snapshot(self) -> Result[LayoutSnapshot]:
        """The current layout as a restorable flat baseline, with a canonical hash."""
        result = await self._records()
        if isinstance(result, Err):
            return result
        self._publish(_outputs(result.value))
        return Ok(value=_snapshot(result.value))

    async def outputs(self) -> Result[tuple[OutputInfo, ...]]:
        """The fully describable current outputs."""
        result = await self._records()
        if isinstance(result, Err):
            return result
        outputs = _outputs(result.value)
        self._publish(outputs)
        return Ok(value=outputs)

    async def wait_for(
        self,
        predicate: Callable[[tuple[OutputInfo, ...]], bool],
        deadline: float,
    ) -> Result[tuple[OutputInfo, ...]]:
        """Poll until ``predicate`` holds, or report that it never was confirmed.

        ``deadline`` is an absolute value of the injected clock, and it is one
        operation deadline rather than a per-iteration one: a read may not begin
        at or after it, each read is bounded by the budget that remains, and a
        read that finishes at or after it is not evidence any more. Expiry is
        terminal. A read error is never mistaken for a match: missing
        dependencies are reported at once, while a transient layout failure is
        retried until the deadline and then reported instead of a false timeout.
        A timeout is :attr:`~airplay_linux.domain.resources.ErrorCode.cleanup_timeout`,
        so the caller can offer cleanup rather than claim success.
        """
        if not callable(predicate):
            return _failure(ErrorCode.invalid_input)
        if not (type(deadline) in (int, float) and math.isfinite(float(deadline))):
            return _failure(ErrorCode.invalid_input)
        expires_at = float(deadline)
        last_error: Err | None = None
        while True:
            now = self._clock.now()
            if now >= expires_at:
                return self._expired(last_error)
            result = await self._records(expires_at - now)
            if isinstance(result, Ok):
                if self._clock.now() >= expires_at:
                    # The read answered, but not within the interval we promised
                    # to observe; a late layout is not evidence about the deadline.
                    return _failure(ErrorCode.cleanup_timeout)
                outputs = _outputs(result.value)
                self._publish(outputs)
                last_error = None
                if predicate(outputs):
                    return Ok(value=outputs)
            else:
                last_error = result
                if result.error.code in (
                    ErrorCode.dependency_missing, ErrorCode.dependency_incompatible,
                ):
                    return result
            now = self._clock.now()
            if now >= expires_at:
                return self._expired(last_error)
            expired = await self._sleep_until(
                min(now + self._poll_interval, expires_at), expires_at,
            )
            if expired:
                # The real-time fallback fired, which means no poll tick arrived
                # before the deadline; the injected clock may never advance, so
                # this is a terminal timeout rather than another iteration.
                return self._expired(last_error)

    @staticmethod
    def _expired(last_error: Err | None) -> Err:
        """The outcome of an expired wait: the last read failure, else a timeout."""
        return last_error if last_error is not None else _failure(ErrorCode.cleanup_timeout)

    async def _sleep_until(self, when: float, deadline: float) -> bool:
        """Wait for a scheduler wake; only the deadline has a real-time fallback.

        Poll ticks must come from the injected scheduler so a test can drive them
        deterministically. The overall deadline additionally uses a real timer so
        a scheduler that never fires ends the wait instead of hanging; its
        duration is the deadline distance, not a poll interval. Returns whether
        that real-time fallback ended the wait, which is a terminal timeout: it
        must not be treated as a poll tick, or a frozen clock would install one
        fallback after another forever.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        expired = False

        def wake() -> None:
            if not future.done():
                future.set_result(None)

        def expire() -> None:
            nonlocal expired
            expired = True
            wake()

        scheduled = self._scheduler.call_at(when, wake)
        fallback = loop.call_later(max(0.0, deadline - self._clock.now()), expire)
        try:
            await future
        finally:
            scheduled.cancel()
            fallback.cancel()
            if not future.done():
                future.cancel()
        return expired

    # -- configuration ------------------------------------------------------

    async def configure(
        self, output_id: str, values: Mapping[str, str | int | bool]
    ) -> Result[None]:
        """Apply one atomic KScreen setting set and prove it in the readback.

        Supported keys are ``enabled``, ``mode``, ``size``, ``position`` and
        ``scale``. ``mode`` must be a mode id KScreen reports for this output;
        ``size`` is resolved to such an id rather than being passed as text.
        """
        target = _id_token(output_id) if type(output_id) is str else None
        request = _request(values)
        if target is None or request is None:
            return _failure(ErrorCode.invalid_input)
        async with self._configure_lock:
            result, observed = await self._configure_locked(target, request)
        if observed is not None:
            self._publish(observed)
        return result

    async def _configure_locked(
        self, output_id: str, request: _Request,
    ) -> tuple[Result[None], tuple[OutputInfo, ...] | None]:
        current = await self._records()
        if isinstance(current, Err):
            return current, None
        record = next(
            (item for item in current.value if item.output_id == output_id), None,
        )
        if record is None:
            return _failure(ErrorCode.configuration_failed), None
        resolved = _resolve_mode(record, request)
        if isinstance(resolved, Err):
            return resolved, None
        mode = resolved.value
        tokens = _tokens(output_id, request, mode)
        if not tokens:
            return _failure(ErrorCode.invalid_input), None
        applied = await self._process.run(
            CommandSpec(argv=(self._command, *tokens), env={}), self._timeout,
        )
        if isinstance(applied, Err):
            return applied, None
        if applied.value.exit_code != 0:
            return _failure(ErrorCode.configuration_failed), None
        readback = await self._records()
        if isinstance(readback, Err):
            return readback, None
        updated = next(
            (item for item in readback.value if item.output_id == output_id), None,
        )
        if updated is None or not _verified(updated, record, request, mode):
            return _failure(ErrorCode.configuration_failed), None
        return Ok(value=None), _outputs(readback.value)

    # -- subscription -------------------------------------------------------

    def subscribe(
        self, callback: Callable[[tuple[OutputInfo, ...]], None],
    ) -> Subscription:
        """Observe layout changes that a successful read actually discovered."""
        if not callable(callback):
            raise TypeError("a layout subscriber must be callable")
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _Subscription(unsubscribe)


__all__ = [
    "CONFIGURABLE_SETTINGS",
    "DEFAULT_POLL_INTERVAL",
    "KSCREEN_COMMAND",
    "KSCREEN_TIMEOUT",
    "KScreenMode",
    "KScreenObserver",
    "KScreenParseError",
    "OutputInfo",
    "layout_hash",
    "parse_outputs",
    "select_mode",
]
