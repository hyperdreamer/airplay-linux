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
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    SensitiveText,
)
from airplay_linux.domain.records import ActivityEvent
from airplay_linux.domain.resources import (
    AudioState,
    CapabilityReport,
    DisplayState,
    ErrorCode,
    SessionPhase,
    TransportState,
    TrustState,
)

from .catalog import ALLOWED_COMMANDS, diagnostic

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
    }
)

#: Upper bound on one rendered text value; longer values are elided.
MAX_FIELD_TEXT: Final[int] = 120

#: A complete escape as :func:`_escape` emits it; truncation never splits one.
_ESCAPE: Final[re.Pattern[str]] = re.compile(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}")

#: Upper bound on the code token itself.
MAX_CODE_LENGTH: Final[int] = 64


def _enum_values(enum: type[StrEnum]) -> frozenset[str]:
    """The frozen domain's own vocabulary for one resource-shaped field."""
    return frozenset(member.value for member in enum)


#: Fields that accept only a closed vocabulary; the domain owns every value.
_VOCABULARIES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "phase": _enum_values(SessionPhase),
        "transport": _enum_values(TransportState),
        "display": _enum_values(DisplayState),
        "audio": _enum_values(AudioState),
        "trust": _enum_values(TrustState),
        "error_code": _enum_values(ErrorCode),
        "command_name": ALLOWED_COMMANDS,
        # The reservation stages the design's atomic ownership protocol defines.
        "reservation_stage": frozenset(
            {"reserved", "helper_started", "output_observed", "published", "removing"},
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

PRIVATE_VALUE_TYPES: Final[tuple[type, ...]] = (
    SensitiveText,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OpaqueBackendToken,
)

EXPORT_SCHEMA: Final[str] = "airplay-linux-diagnostics/1"

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
        "display.reserved": "A virtual output reservation was recorded.",
        "display.helper_started": "The virtual display helper started.",
        "display.output_observed": "The virtual output appeared.",
        "display.published": "The virtual output was published.",
        "display.removing": "Removal intent was recorded for the virtual output.",
        "display.removed": "The virtual output was removed.",
        "display.cleanup_required": "The virtual output removal could not be confirmed.",
        "capture.selected": "A physical monitor was selected as the capture source.",
        "capture.released": "The capture source was released.",
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
    if not isinstance(code, str) or not code:
        raise ValueError(
            "code must be a non-empty lowercase token such as 'pairing.completed'",
        )
    if len(code) > MAX_CODE_LENGTH:
        raise ValueError("code must be a bounded token of at most 64 characters")
    if not (code[0].isalpha() and code[0].islower()):
        raise ValueError("code must start with a lowercase letter")
    if not all(char.islower() or char.isdigit() or char in "._-" for char in code):
        raise ValueError(
            "code may contain only lowercase letters, digits, '.', '_' and '-'",
        )
    if code not in ACTIVITY_CODES:
        # A token-shaped string can still be backend text, so membership, not
        # shape, decides what a code is; the rejected value is never echoed.
        raise ValueError("code must be one of the registered activity codes")
    return str(code)


def _reject_utc(utc: object) -> str:
    """Require an absolute timestamp and normalize it to canonical UTC."""
    if not isinstance(utc, str):
        raise ValueError("utc must be an absolute ISO-8601 timestamp with an offset")
    try:
        parsed = datetime.fromisoformat(utc)
    except ValueError:
        raise ValueError(
            "utc must be an absolute ISO-8601 timestamp with an offset",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("utc must carry an explicit UTC offset")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _reject_receiver(receiver_id: object) -> ReceiverId | None:
    """Sanitize the identity token the same way as an allowlisted field value."""
    if receiver_id is None:
        return None
    if not isinstance(receiver_id, str):
        raise TypeError("receiver_id must be a ReceiverId string or None")
    return ReceiverId(_bounded(_escape(receiver_id)))


def _escape(text: str) -> str:
    """Render control and format characters as visible escapes, never raw."""
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
    if isinstance(value, PRIVATE_VALUE_TYPES):
        raise ValueError(
            f"value for field {name!r} is a transient private value and cannot be recorded",
        )
    vocabulary = _VOCABULARIES.get(name)
    if vocabulary is not None:
        # Normalize once: a ``str`` subclass may render differently on each call.
        text = str(value) if isinstance(value, str) else None
        if text is None or text not in vocabulary:
            raise ValueError(
                f"value for field {name!r} must be one of the values this field enumerates",
            )
        return text
    if name == "connector":
        return _identifier(name, value, _CONNECTOR)
    if name == "output_id":
        return _identifier(name, value, _OUTPUT_ID)
    if name == "resolution":
        return _resolution(value)
    bounds = _INTEGER_BOUNDS.get(name)
    if bounds is not None:
        low, high = bounds
        if type(value) is not int:
            raise ValueError(
                f"value for field {name!r} must be a whole number between {low} and {high}",
            )
        if not low <= value <= high:
            raise ValueError(
                f"value for field {name!r} must be a whole number between {low} and {high}",
            )
        return value
    if name in _BOOLEAN_FIELDS:
        if type(value) is not bool:
            raise ValueError(f"value for field {name!r} must be a boolean")
        return value
    # Unreachable while ``FIELD_SHAPES`` covers every allowlisted field name;
    # the guard exists so a newly allowlisted field cannot inherit a passthrough.
    raise ValueError(f"field {name!r} declares no value shape and cannot be recorded")


def _identifier(name: str, value: object, pattern: re.Pattern[str]) -> str:
    """Accept only text matching the field's token shape, then escape and bound."""
    text = str(value) if isinstance(value, str) else None
    if text is None or pattern.fullmatch(text) is None:
        raise ValueError(
            f"value for field {name!r} must be a {FIELD_SHAPES[name]} identifier",
        )
    return _bounded(_escape(text))


def _resolution(value: object) -> str:
    """Accept only ``WIDTHxHEIGHT`` inside the domain ``Size`` bounds."""
    match = _RESOLUTION.fullmatch(str(value)) if isinstance(value, str) else None
    if match is None:
        raise ValueError("value for field 'resolution' must be written as WIDTHxHEIGHT")
    width, height = (int(edge) for edge in match.groups())
    for edge in (width, height):
        if not 1 <= edge <= MAX_RESOLUTION_EDGE:
            raise ValueError(
                "value for field 'resolution' must be between 1 and "
                f"{MAX_RESOLUTION_EDGE} on each edge",
            )
    # Canonical form, so the recorded text is re-rendered, never echoed.
    return f"{width}x{height}"


def _validate_fields(fields: object) -> Mapping[str, str | int | bool]:
    if not isinstance(fields, Mapping):
        raise TypeError("fields must be a mapping of allowlisted names to values")
    clean: dict[str, str | int | bool] = {}
    for name, value in fields.items():
        if not isinstance(name, str) or name not in ALLOWED_FIELDS:
            raise ValueError(
                "field is not allowlisted; secrets and backend payloads cannot enter activity",
            )
        clean[name] = _reject_value(name, value)
    return MappingProxyType(clean)


def _message_for(code: str) -> str:
    """Fixed copy for a code; :func:`_reject_code` has already proven membership."""
    return _MESSAGES[code]


def safe_activity(
    code: str,
    receiver_id: ReceiverId | None,
    fields: Mapping[str, str | int | bool],
    utc: str,
) -> ActivityEvent:
    """Build one activity event from sanitized DTO values only."""
    token = _reject_code(code)
    stamp = _reject_utc(utc)
    receiver = _reject_receiver(receiver_id)
    clean = _validate_fields(fields)
    return ActivityEvent(
        utc=stamp,
        code=token,
        receiver_id=receiver,
        message=_message_for(token),
        fields=clean,
    )


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

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        action, self._action = self._action, None
        if action is not None:
            action()


class ActivityLog:
    """A bounded, ordered activity log plus a stable, secret-free export."""

    def __init__(
        self,
        max_entries: int = 1000,
        *,
        capabilities: CapabilityReport | None = None,
    ) -> None:
        if type(max_entries) is not int:
            raise TypeError("max_entries must be an integer")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if capabilities is not None and not isinstance(capabilities, CapabilityReport):
            raise TypeError("capabilities must be a CapabilityReport or None")
        self._max_entries = max_entries
        self._capabilities = capabilities
        self._entries: deque[ActivityEvent] = deque(maxlen=max_entries)
        self._subscribers: list[Callable[[ActivityEvent], None]] = []

    # -- writing ------------------------------------------------------------

    def emit(self, event: ActivityEvent) -> None:
        """Record one sanitized event and notify current subscribers."""
        if not isinstance(event, ActivityEvent):
            raise TypeError("only a domain ActivityEvent can be emitted")
        sanitized = _sanitize_event(event)
        self._entries.append(sanitized)
        for callback in tuple(self._subscribers):
            try:
                callback(sanitized)
            except Exception:
                # A broken presenter must not lose the durable activity record.
                continue

    def subscribe(self, callback: Callable[[ActivityEvent], None]) -> Subscription:
        """Attach a presenter; the returned handle removes exactly this callback."""
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _Subscription(unsubscribe)

    # -- reading ------------------------------------------------------------

    def entries(self) -> tuple[ActivityEvent, ...]:
        return tuple(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

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
        report = self._capabilities
        if report is None:
            return None
        capabilities: dict[str, object] = {
            "environment": _bounded(_escape(report.environment)),
            "discovery": report.discovery,
            "mirror": report.mirror,
            "virtual_display": report.virtual_display,
            "audio": report.audio,
            "remembered_pairing": report.remembered_pairing,
            "additional_virtual_outputs": report.additional_virtual_outputs,
            "diagnostic_codes": sorted(
                {diagnostic(item.code).code.value for item in report.diagnostics},
            ),
        }
        return capabilities
