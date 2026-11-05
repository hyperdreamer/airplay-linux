"""An allowlisted activity channel, bounded log, and secret-free export.

The activity log is a real output channel: it is rendered, copied, and attached
to bug reports. Only allowlisted field names with primitive values may enter it,
and a field *value* is untrusted input rather than a trusted label: every value
must satisfy that field's declared shape exactly. Vocabulary fields accept only
the values the frozen domain enumerates, identifier fields accept only a strict
token shape, and numeric fields accept only bounded integers. Anything else is
rejected instead of being escaped, truncated, or echoed back, so backend text
and secret bytes have no shape they can wear. Each code is drawn from a closed
vocabulary with fixed message copy, and hand-built events are re-validated on
emission, so a caller cannot route backend text around :func:`safe_activity`.

Nothing here inspects the type of a caller value: every value crosses
:mod:`airplay_linux.diagnostics.boundary` once, which accepts exact built-in
types only and never runs a hook of a caller-supplied object. That module is also
the only place a rejection is built, and the only place a value is dispatched on,
so no site here asks an object what type it is.

Accepting a value is also not the same as owning it. A :class:`CapabilityReport`
is frozen, but the caller keeps its own alias, and ``object.__setattr__`` can
retype that alias to a subclass, or rewrite one of its leaves, at any time after
the log has accepted it. So the report's *evidence* -- the environment label,
the seven capability flags and the validated diagnostic codes -- is normalized
once, at acceptance, into :class:`_CapabilitySnapshot` and only safe built-in
leaves are retained. Normalizing includes sanitizing: the environment allowlist
and its fixed ``Unknown`` fallback are applied *before* the snapshot is built, so
the text a caller supplied never becomes retained state and cannot be reached
through the snapshot's ``repr``. The export reads that private snapshot and never
the caller's object, so a hook installed later cannot run and a mutation made
later cannot change the document.

The two outcomes of reading evidence -- a snapshot, or a refusal deferred to the
export -- are kept in separate slots, so the export selects by which slot is
filled rather than by inspecting what it holds.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, NamedTuple, cast

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.records import ActivityEvent
from airplay_linux.domain.resources import (
    AudioState,
    CapabilityReport,
    Diagnostic,
    DisplayState,
    ErrorCode,
    SessionPhase,
    TransportState,
    TrustState,
)

from . import boundary
from .boundary import Vocabulary
from .catalog import ALLOWED_COMMANDS

if TYPE_CHECKING:
    from airplay_linux.domain.ports import Subscription

#: Field names an activity event may carry, and no others.
#:
#: A receiver *display name* is deliberately absent: it is free-form text a
#: receiver advertises, so no value shape can keep a secret out of it. The
#: recorded identity is ``receiver_id``, which is escaped and bounded below.
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "phase",
        "transport",
        "display",
        "audio",
        "trust",
        "command_name",
        "retry_seconds",
        "resolution",
        "connector",
        "output_id",
        "reservation_stage",
        "endpoint_generation",
        "error_code",
        "explicit_video_only",
        "remembered",
        "level",
        "reason",
    }
)

#: Upper bound on one rendered text value; longer values are elided.
MAX_FIELD_TEXT: Final[int] = 120

#: A complete escape as :func:`_escape` emits it; truncation never splits one.
_ESCAPE: Final[re.Pattern[str]] = re.compile(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}")

#: Upper bound on the code token itself.
MAX_CODE_LENGTH: Final[int] = 64

#: Fixed rejection text for a field name outside the allowlist.
_FIELD_NAME_MESSAGE: Final[str] = (
    "field is not allowlisted; secrets and backend payloads cannot enter activity"
)

#: Fields that accept only a closed vocabulary; the domain owns every value.
#:
#: Each vocabulary is captured once at import time, so a genuine enum member is
#: recognized by identity, and neither equality nor hashing decides what counts
#: as a member.
_VOCABULARIES: Final[Mapping[str, Vocabulary]] = MappingProxyType(
    {
        "phase": boundary.captured_enum(SessionPhase),
        "transport": boundary.captured_enum(TransportState),
        "display": boundary.captured_enum(DisplayState),
        "audio": boundary.captured_enum(AudioState),
        "trust": boundary.captured_enum(TrustState),
        "error_code": boundary.captured_enum(ErrorCode),
        "reason": boundary.captured_texts((
            "backend_unsupported", "startup_refused", "receipt_pending", "receipt_missing",
            "no_audio", "invalid_preference", "audio_closed", "close_unconfirmed",
        )),
        "command_name": boundary.captured_texts(ALLOWED_COMMANDS),
        # The reservation stages the design's atomic ownership protocol defines.
        "reservation_stage": boundary.captured_texts(
            ("reserved", "helper_started", "output_observed", "published", "removing"),
        ),
    }
)

#: A KDE connector identity, such as ``DP-1``, ``HDMI-A-1``, ``Virtual-v1``.
_CONNECTOR: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z]{2,12}(?:-(?:[A-Z]|[a-z][0-9]{1,3}|[0-9]{1,3})){1,3}",
)

#: A KScreen output identity: a small index, a virtual index, or a UUID.
_OUTPUT_ID: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{1,12}|v[0-9]{1,12}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
)

#: ``WIDTHxHEIGHT``, with the same 1..16384 bound the domain's ``Size`` holds.
_RESOLUTION: Final[re.Pattern[str]] = re.compile(r"([0-9]{1,5})x([0-9]{1,5})")

MAX_RESOLUTION_EDGE: Final[int] = 16384

#: Fields that accept only a whole number inside an inclusive bound.
_INTEGER_BOUNDS: Final[Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "retry_seconds": (0, 86400),
        "endpoint_generation": (0, 2**31 - 1),
        "remembered": (0, 10_000),
        "level": (0, 100),
    }
)

#: Fields that accept only a boolean, and never the integer ``0`` or ``1``.
_BOOLEAN_FIELDS: Final[frozenset[str]] = frozenset({"explicit_video_only"})

#: The declared value shape of every allowlisted field, derived from the same
#: registries the enforcement reads, so the two cannot drift apart.
FIELD_SHAPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        **{name: "vocabulary" for name in _VOCABULARIES},
        "connector": "connector",
        "output_id": "output_id",
        "resolution": "resolution",
        **{name: "integer" for name in _INTEGER_BOUNDS},
        **{name: "boolean" for name in _BOOLEAN_FIELDS},
    }
)

EXPORT_SCHEMA: Final[str] = "airplay-linux-diagnostics/1"
_SAFE_ENVIRONMENTS: Final[frozenset[str]] = frozenset({"KDE Plasma 6 Wayland"})
_UNKNOWN_ENVIRONMENT: Final[str] = "Unknown"

#: The environment labels a report may carry, captured once so membership is
#: decided on character data and the accepted label is this module's own text.
#: The allowlist is applied when the report is accepted, not when the document is
#: built: an unrecognized label is replaced by :data:`_UNKNOWN_ENVIRONMENT` before
#: anything is retained, so the private snapshot never holds caller text.
_ENVIRONMENTS: Final[Vocabulary] = boundary.captured_texts(_SAFE_ENVIRONMENTS)

#: Fixed text of each capability-evidence refusal, written once here and passed
#: to the boundary call that enforces it. A report's leaves are normalized when
#: the log accepts it, but a refusal is still reported when the document is
#: built, so the refusal text and kind a caller sees do not change with the
#: moment the evidence is read.
_ENVIRONMENT_MESSAGE: Final[str] = "capabilities environment must be a string"
_FLAGS_MESSAGE: Final[str] = "capability flags must be booleans"
_DIAGNOSTICS_MESSAGE: Final[str] = (
    "capability diagnostics must be an immutable tuple of diagnostics"
)
_DIAGNOSTIC_ITEM_MESSAGE: Final[str] = (
    "capability diagnostics must be domain Diagnostic records; "
    "arbitrary objects are not capability evidence"
)
_DIAGNOSTIC_CODE_MESSAGE: Final[str] = (
    "capability diagnostics must carry a registered error code"
)

#: The seven capability flags, in the order the export document lists them.
_CAPABILITY_FLAGS: Final[tuple[str, ...]] = (
    "discovery",
    "mirror",
    "virtual_display",
    "audio",
    "remembered_pairing",
    "additional_virtual_outputs",
    "volume",
)

#: Fixed message copy per code; a new code needs a line here, not a raw string.
_MESSAGES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "discovery.started": "Receiver discovery started.",
        "discovery.stopped": "Receiver discovery stopped.",
        "discovery.failed": "Receiver discovery reported a failure.",
        "session.available": "Receiver is available.",
        "session.offline": "Receiver went offline; saved metadata is unchanged.",
        "session.starting": "Session start requested.",
        "session.active": "Session is active.",
        "session.stopping": "Session stop requested.",
        "session.stopped": "Session stopped.",
        "session.failed": "Session failed; recovery actions are available.",
        "pairing": "Pairing was requested by the receiver.",
        "pairing.requested": "Pairing was requested by the receiver.",
        "pairing.completed": "Pairing completed; the receiver is ready.",
        "pairing.cancelled": "Pairing was cancelled; trust is unchanged.",
        "pairing.expired": "Pairing expired; start pairing again from the receiver.",
        "pairing.rejected": "The receiver rejected the submitted pairing value.",
        "trust.invalidated": "Saved trust for this receiver was invalidated.",
        "trust.revocation_pending": "Trust revocation is pending.",
        "trust.cleanup_required": "Trust revocation is unconfirmed; new pairing is blocked.",
        "transport.connecting": "Connecting to the receiver.",
        "transport.ready": "The stream to the receiver is ready.",
        "transport.reconnecting": "The stream dropped; reconnecting in the background.",
        "transport.stopped": "The transport was released.",
        "audio.enabled": "Audio is enabled for this session.",
        "audio.unavailable": "Audio is unavailable for this session.",
        "audio.muted": "The receiver was muted for this session.",
        "audio.unmuted": "The receiver was unmuted for this session.",
        "audio.recovered": "Audio recovered; the video stream was preserved.",
        "audio.volume_applied": "Receiver volume was applied.",
        "audio.volume_failed": "Receiver audio control change could not be confirmed.",
        "audio.volume_unavailable": "Receiver volume control is unavailable.",
        "display.reserved": "A virtual output reservation was recorded.",
        "display.helper_started": "The virtual display helper started.",
        "display.output_observed": "The virtual output appeared.",
        "display.published": "The virtual output was published.",
        "display.removing": "Removal intent was recorded for the virtual output.",
        "display.removed": "The virtual output was removed.",
        "display.cleanup_required": "The virtual output removal could not be confirmed.",
        "capture.selected": "A physical monitor was selected as the capture source.",
        "capture.released": "The capture source was released.",
        "screen.forget": "Remembered screen-sharing choices were cleared.",
        "screen.forget_failed": "Remembered screen-sharing choices could not be cleared.",
        "resolution.required": "A resolution must be chosen before this start continues.",
        "resolution.remembered": "The chosen resolution was remembered for this receiver.",
        "audio.decision_required": "An explicit audio decision is required before start.",
        "mode.deferred": "The requested mode is queued until cleanup is confirmed.",
        "process.failed": "A backend process reported a failure.",
        "session": "Session lifecycle event.",
    }
)

#: Every code the channel can record; anything else is rejected, not echoed.
ACTIVITY_CODES: Final[frozenset[str]] = frozenset(_MESSAGES)


def _reject_code(code: object) -> str:
    """Validate the code token, then require it to be a known code."""
    text = boundary.exact_text(
        code,
        message="code must be a non-empty lowercase token such as 'pairing.completed'",
    )
    if not text:
        raise boundary.rejection(
            "code must be a non-empty lowercase token such as 'pairing.completed'",
        )
    if len(text) > MAX_CODE_LENGTH:
        raise boundary.rejection("code must be a bounded token of at most 64 characters")
    if not (text[0].isalpha() and text[0].islower()):
        raise boundary.rejection("code must start with a lowercase letter")
    if not all(char.islower() or char.isdigit() or char in "._-" for char in text):
        raise boundary.rejection(
            "code may contain only lowercase letters, digits, '.', '_' and '-'",
        )
    if text not in ACTIVITY_CODES:
        # A token-shaped string can still be backend text, so membership, not
        # shape, decides what a code is; the rejected value is never echoed.
        raise boundary.rejection("code must be one of the registered activity codes")
    # The exact string that was checked is the string that is stored: the code
    # cannot render, compare or hash as anything else afterwards.
    return text


def _reject_utc(utc: object) -> str:
    """Require an absolute timestamp and normalize it to canonical UTC."""
    text = boundary.exact_text(
        utc,
        message="utc must be an absolute ISO-8601 timestamp with an offset",
    )
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is None:
        raise boundary.rejection(
            "utc must be an absolute ISO-8601 timestamp with an offset",
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise boundary.rejection("utc must carry an explicit UTC offset")
    try:
        normalized = parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        normalized = None
    if normalized is None:
        raise boundary.rejection("utc could not be normalized to canonical UTC")
    return normalized


def _escape(text: str) -> str:
    """Render control and format characters as visible escapes, never raw.

    ``text`` is an exact built-in string :func:`boundary.exact_text` returned,
    so the characters rendered here are the characters that were validated.
    """
    pieces: list[str] = []
    for char in text:
        if char.isprintable():
            pieces.append(char)
        else:
            code = ord(char)
            pieces.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
            continue
        if unicodedata.category(char) in {"Cf", "Co", "Cs"}:
            pieces[-1] = f"\\u{ord(char):04x}"
    return "".join(pieces)


def _bounded(text: str) -> str:
    """Elide to the bound without ever cutting an escape sequence in half."""
    if len(text) <= MAX_FIELD_TEXT:
        return text
    cut = MAX_FIELD_TEXT - 1
    for match in _ESCAPE.finditer(text):
        if match.start() >= cut:
            break
        if match.end() > cut:
            # The cut falls inside this escape; end the value before it instead.
            cut = match.start()
            break
    return text[:cut] + "\N{HORIZONTAL ELLIPSIS}"


def _reject_value(name: str, value: object) -> str | int | bool:
    """Accept only a value of the shape this field declares, or reject it.

    Shape is the boundary, not an allowlist of names: a field that accepts text
    accepts one closed vocabulary or one strict token form, so a secret or a
    backend message has no shape it can wear. Nothing is echoed on rejection.
    """
    if boundary.is_private_value(value):
        raise boundary.rejection(
            f"value for field {name!r} is a transient private value and cannot be recorded",
        )
    vocabulary = _VOCABULARIES.get(name)
    if vocabulary is not None:
        return boundary.vocabulary_text(
            value,
            vocabulary,
            message=(
                f"value for field {name!r} must be one of the values this field enumerates"
            ),
        )
    if name == "connector":
        return _identifier(name, value, _CONNECTOR)
    if name == "output_id":
        return _identifier(name, value, _OUTPUT_ID)
    if name == "resolution":
        return _resolution(value)
    bounds = _INTEGER_BOUNDS.get(name)
    if bounds is not None:
        low, high = bounds
        return boundary.exact_bounded_int(
            value,
            low=low,
            high=high,
            message=(
                f"value for field {name!r} must be a whole number between {low} and {high}"
            ),
        )
    if name in _BOOLEAN_FIELDS:
        # A boolean field accepts the exact ``bool`` type only, never the
        # integers ``0`` and ``1``, which are a different type.
        return boundary.exact_only(
            value,
            bool,
            kind=ValueError,
            message=f"value for field {name!r} must be a boolean",
        )  # type: ignore[return-value]
    # Unreachable while ``FIELD_SHAPES`` covers every allowlisted field name;
    # the guard exists so a newly allowlisted field cannot inherit a passthrough.
    raise boundary.rejection(f"field {name!r} declares no value shape and cannot be recorded")


def _identifier(name: str, value: object, pattern: re.Pattern[str]) -> str:
    """Accept only text matching the field's token shape, then escape and bound."""
    message = f"value for field {name!r} must be a {FIELD_SHAPES[name]} identifier"
    text = boundary.exact_text(value, message=message)
    if pattern.fullmatch(text) is None:
        raise boundary.rejection(message)
    return _bounded(_escape(text))


def _resolution(value: object) -> str:
    """Accept only ``WIDTHxHEIGHT`` inside the domain ``Size`` bounds."""
    message = "value for field 'resolution' must be written as WIDTHxHEIGHT"
    match = _RESOLUTION.fullmatch(boundary.exact_text(value, message=message))
    if match is None:
        raise boundary.rejection(message)
    width, height = (int(edge) for edge in match.groups())
    for edge in (width, height):
        if not 1 <= edge <= MAX_RESOLUTION_EDGE:
            raise boundary.rejection(
                "value for field 'resolution' must be between 1 and "
                f"{MAX_RESOLUTION_EDGE} on each edge",
            )
    # Canonical form, so the recorded text is re-rendered, never echoed.
    return f"{width}x{height}"


def _validate_fields(fields: object) -> Mapping[str, str | int | bool]:
    # The boundary decides the container's exact type before anything is read
    # from it, so a mapping subclass cannot run its ``items``, ``keys`` or
    # iteration hooks inside the diagnostics package.
    clean: dict[str, str | int | bool] = {}
    for raw_name, value in boundary.exact_mapping_items(fields):
        name = boundary.exact_text(raw_name, message=_FIELD_NAME_MESSAGE)
        if name not in ALLOWED_FIELDS:
            raise boundary.rejection(_FIELD_NAME_MESSAGE)
        clean[name] = _reject_value(name, value)
    return MappingProxyType(clean)


def _message_for(code: str) -> str:
    """Fixed copy for a code; :func:`_reject_code` has already proven membership.

    The lookup is total and its fallback is a fixed literal, so an impossible
    miss can never surface as an exception whose text carries the token.
    """
    return _MESSAGES.get(code) or _MESSAGES["session"]


@boundary.rejecting
def safe_activity(
    code: str,
    receiver_id: ReceiverId | None,
    fields: Mapping[str, str | int | bool],
    utc: str,
) -> ActivityEvent:
    """Build one activity event from sanitized DTO values only."""
    token = _reject_code(code)
    stamp = _reject_utc(utc)
    receiver = boundary.exact_receiver_id(receiver_id)
    clean = _validate_fields(fields)
    return ActivityEvent(
        utc=stamp,
        code=token,
        receiver_id=receiver,
        message=_message_for(token),
        fields=clean,
    )


def _capability_codes(diagnostics: object) -> tuple[str, ...]:
    """Validate the capability-diagnostic collection before reading any item.

    This is a record boundary, not a renderer: only the exact frozen tuple type
    holding exact :class:`Diagnostic` records with genuine :class:`ErrorCode`
    members may contribute a code. The collection and each item's type are
    decided *before* any attribute is read, so a caller object whose ``code``
    hook raises, advertises a borrowed ``__class__``, or carries backend text is
    never consulted, and the returned strings are the frozen vocabulary's own.

    The result is a plain built-in tuple of built-in strings, so it is a private
    snapshot rather than a view of anything the caller can still reach.
    """
    diagnostics = boundary.exact_only(
        diagnostics,
        tuple,
        message=_DIAGNOSTICS_MESSAGE,
    )
    codes: set[str] = set()
    for item in diagnostics:  # type: ignore[attr-defined]
        item = boundary.exact_only(
            item,
            Diagnostic,
            message=_DIAGNOSTIC_ITEM_MESSAGE,
        )
        code = boundary.genuine_error_code(item.code)  # type: ignore[attr-defined]
        # The code is a captured genuine member, so its text comes from the
        # frozen vocabulary rather than from anything the caller holds.
        codes.add(
            boundary.vocabulary_text(
                code,
                boundary.ERROR_CODE_VOCABULARY,
                kind=TypeError,
                message=_DIAGNOSTIC_CODE_MESSAGE,
            ),
        )
    return tuple(sorted(codes))


class _CapabilitySnapshot(NamedTuple):
    """Accepted capability evidence, as exact built-in leaves only.

    Every field is an exact built-in the boundary produced: the environment label
    is one the design allows -- or the fixed ``Unknown`` standing in for one it
    does not -- each flag is a real ``bool``, and the codes are the frozen
    vocabulary's own text. Nothing here holds a reference to the caller's report,
    to its diagnostics collection, or to any diagnostic record, so no later
    mutation or retype of those objects can change what the export says.
    ``NamedTuple`` makes the retained record a tuple of immutable built-in
    leaves, so ``object.__setattr__`` cannot rewrite an accepted value.

    The subtree that is deliberately *absent* is the diagnostic ``message`` -- the
    field that holds receiver and backend text -- so a report's prose cannot be
    reached through this record at all. Since the environment allowlist is applied
    before construction, the text a caller supplied is not retained here either;
    the generated ``__repr__`` and ``__eq__`` therefore render and compare safe
    leaves only, and neither can consult a caller object.
    """

    environment: str
    flags: tuple[tuple[str, bool], ...]
    diagnostic_codes: tuple[str, ...]


class _CapabilityRefusal(NamedTuple):
    """A refusal the boundary issued while reading accepted evidence.

    A report that cannot be normalized still has to be refused at the export,
    because that is the point at which a caller observes capability evidence, and
    refusing at construction instead would change the observable contract. So the
    refusal is *recorded*: ``kind`` is one of the two built-in kinds and ``text``
    is the fixed copy :mod:`airplay_linux.diagnostics.boundary` already wrote for
    it, both taken from that module's own record rather than from the caller's
    failure. Nothing the caller supplied is reachable from here.

    This is a state, not a variant of the evidence: the log keeps it in its own
    slot (see :class:`ActivityLog`), so the export selects it by which slot is
    filled instead of asking an object what type it is.

    Recording it also makes the refusal stable: the second export re-issues the
    same kind and the same text without touching the report again, which is what
    keeps a hostile report from being consulted twice.
    """

    kind: type[Exception]
    text: str

    def issue(self) -> Exception:
        """A fresh rejection carrying this recorded kind and fixed text.

        The kind is handed back to the boundary, which is the only module that
        builds rejections; nothing here inspects a value to decide what to raise.
        """
        return boundary.rejection_of(self.kind, self.text)


@boundary.rejecting
def _normalized_capabilities(report: object) -> _CapabilitySnapshot:
    """Read one accepted report's evidence exactly once into a sanitized snapshot.

    Each leaf is read here, through :mod:`airplay_linux.diagnostics.boundary`,
    and only what the boundary returned is retained. That includes the
    environment, whose allowlist and ``Unknown`` fallback are applied at this
    point rather than at export: the text a caller supplied never becomes part of
    this module's retained state, so no rendering of that state can carry it.

    The wrapper turns every failure -- a caller ``BaseException`` included --
    into one of this module's own fixed rejections, so the caller of this function
    never has to inspect whatever raised.
    """
    environment = boundary.vocabulary_label(
        getattr(report, "environment"),
        _ENVIRONMENTS,
        fallback=_UNKNOWN_ENVIRONMENT,
        message=_ENVIRONMENT_MESSAGE,
    )
    flags: list[tuple[str, bool]] = []
    for name in _CAPABILITY_FLAGS:
        value = boundary.exact_only(
            getattr(report, name),
            bool,
            message=_FLAGS_MESSAGE,
        )
        flags.append((name, cast(bool, value)))
    return _CapabilitySnapshot(
        environment=environment,
        flags=tuple(flags),
        diagnostic_codes=_capability_codes(getattr(report, "diagnostics")),
    )


def _accepted_capabilities(
    capabilities: object,
) -> tuple[_CapabilitySnapshot | None, _CapabilityRefusal | None]:
    """Take the private snapshot of an accepted report, or record its refusal.

    This is the only moment the caller's report is read. Afterwards the log holds
    nothing that refers to it, so retyping the caller's alias to instrument
    ``__getattribute__``, rewriting one of its leaves, or replacing its
    diagnostics tuple cannot reach the export.

    The two outcomes are returned as two slots rather than as one value the export
    has to identify, so selection is a question of which slot is filled and never
    a question about an object's type.

    A refusal is deferred rather than raised here: the export document is where a
    caller observes capability evidence, so an unacceptable leaf keeps refusing
    at export with the same built-in kind and the same fixed text it always did.
    """
    if capabilities is None:
        return None, None
    try:
        return cast(_CapabilitySnapshot, _normalized_capabilities(capabilities)), None
    except (ValueError, TypeError) as refusal:
        # The boundary keeps the text it wrote beside every rejection it built, so
        # this site records those two source-written values. Reading that record
        # is what keeps the caller's own failure from lending anything, and what
        # keeps this site free of a "which type is this" question: the pair is
        # total, and a failure that is not the boundary's degrades to the fixed
        # literal inside the boundary itself.
        kind, text = boundary.refusal_record(refusal)
        return None, _CapabilityRefusal(kind=kind, text=text)


def _sanitize_event(event: ActivityEvent) -> ActivityEvent:
    """Re-derive copy and fields, so a hand-built event cannot smuggle content."""
    token = _reject_code(event.code)
    return safe_activity(token, event.receiver_id, event.fields, event.utc)


class _Subscription:
    """Removal handle for one activity subscriber; ``close`` is idempotent."""

    __slots__ = ("_action", "_closed")

    def __init__(self, action: Callable[[], None]) -> None:
        self._action: Callable[[], None] | None = action
        self._closed = False

    @boundary.rejecting
    def close(self) -> None:
        """Detach exactly the callback this handle was created for.

        Wrapped like every other public entry point: removal runs inside the
        diagnostics boundary, so a failure raised from there -- including
        ``BaseException`` -- becomes a fixed rejection instead of escaping as
        itself.
        """
        if self._closed:
            return
        self._closed = True
        action, self._action = self._action, None
        if action is not None:
            action()


class ActivityLog:
    """A bounded, ordered activity log plus a stable, secret-free export."""

    @boundary.rejecting
    def __init__(
        self,
        max_entries: int = 1000,
        *,
        capabilities: CapabilityReport | None = None,
    ) -> None:
        max_entries = boundary.exact_only(
            max_entries,
            int,
            message="max_entries must be an integer",
        )
        if max_entries < 1:  # type: ignore[operator]
            raise boundary.rejection("max_entries must be at least 1")
        if capabilities is not None:
            capabilities = boundary.exact_only(
                capabilities,
                CapabilityReport,
                message="capabilities must be a CapabilityReport or None",
            )
        self._max_entries = max_entries
        # The caller's report is read here and only here. What is retained is a
        # private snapshot of sanitized built-in leaves, or -- in its own slot --
        # the refusal to record; the export selects by which slot is filled.
        self._capabilities, self._capability_refusal = _accepted_capabilities(capabilities)
        self._entries: deque[ActivityEvent] = deque(maxlen=max_entries)
        self._subscribers: list[Callable[[ActivityEvent], None]] = []

    # -- writing ------------------------------------------------------------

    @boundary.rejecting
    def emit(self, event: ActivityEvent) -> None:
        """Retain a private event and notify subscribers with independent copies."""
        event = boundary.exact_only(
            event,
            ActivityEvent,
            message="only a domain ActivityEvent can be emitted",
        )
        sanitized = _sanitize_event(event)  # type: ignore[arg-type]
        self._entries.append(sanitized)
        for callback in tuple(self._subscribers):
            try:
                # The domain constructor also copies the immutable field map.
                # A presenter can mutate its record without reaching ours or
                # another presenter's copy, even through object.__setattr__.
                callback(replace(sanitized))
            except Exception:
                # A broken presenter must not lose the durable activity record.
                continue

    @boundary.rejecting
    def subscribe(self, callback: Callable[[ActivityEvent], None]) -> Subscription:
        """Attach a presenter; the returned handle removes exactly this callback."""
        if not callable(callback):
            raise boundary.type_rejection("callback must be callable")
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            # Identity, not equality. A presenter is caller code by definition,
            # so asking it who it equals would run its ``__eq__`` inside this
            # boundary -- and the handle already holds the object that was
            # registered, so identity is the exact answer to "remove this one".
            # ``len`` and indexing on an exact builtin list run no caller hook.
            for index in range(len(self._subscribers)):
                if self._subscribers[index] is callback:
                    del self._subscribers[index]
                    return

        return _Subscription(unsubscribe)

    # -- reading ------------------------------------------------------------

    def entries(self) -> tuple[ActivityEvent, ...]:
        """Return independent event snapshots in arrival order."""
        return tuple(replace(event) for event in self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    @boundary.rejecting
    def export(self) -> str:
        """Serialize counts, capabilities, and safe activity, and nothing else."""
        by_code: dict[str, int] = {}
        for event in self._entries:
            by_code[event.code] = by_code.get(event.code, 0) + 1
        document = {
            "schema": EXPORT_SCHEMA,
            "counts": {
                "entries": len(self._entries),
                "capacity": self._max_entries,
                "by_code": dict(sorted(by_code.items())),
            },
            "capabilities": self._capabilities_document(),
            "activity": [
                {
                    "utc": event.utc,
                    "code": event.code,
                    "receiver_id": event.receiver_id,
                    "message": event.message,
                    "fields": dict(event.fields),
                }
                for event in self._entries
            ],
        }
        return json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True)

    def _capabilities_document(self) -> dict[str, object] | None:
        """The capability section, selected by retained state and nothing else.

        No caller object is read here, and no retained value is asked what type
        it is. The log holds two separate states -- accepted evidence and a
        deferred refusal -- so this method branches on which slot is filled, and
        a caller that rewrote a slot simply finds it rejected by the boundary.

        The snapshot was taken when the log accepted the report, so a caller that
        retyped its alias to instrument ``__getattribute__``, rewrote a leaf, or
        replaced the diagnostics tuple afterwards changes nothing about this
        document and runs no code of its own while it is built.
        """
        refusal = self._capability_refusal
        if refusal is not None:
            raise cast(
                _CapabilityRefusal,
                boundary.exact_only(
                    refusal,
                    _CapabilityRefusal,
                    kind=ValueError,
                    message=boundary.UNEXPECTED_FAILURE,
                ),
            ).issue()
        evidence = self._capabilities
        if evidence is None:
            return None
        # Anything else is not the state this log accepted: retained evidence
        # was replaced from outside, so there is nothing to normalize and the
        # fixed literal is the only honest answer.
        snapshot = cast(
            _CapabilitySnapshot,
            boundary.exact_only(
                evidence,
                _CapabilitySnapshot,
                kind=ValueError,
                message=boundary.UNEXPECTED_FAILURE,
            ),
        )
        capabilities: dict[str, object] = {
            # Already the safe label: the allowlist ran when the report was
            # accepted, so nothing has to be re-decided from caller text here.
            "environment": snapshot.environment,
            **dict(snapshot.flags),
            "diagnostic_codes": list(snapshot.diagnostic_codes),
        }
        return capabilities
