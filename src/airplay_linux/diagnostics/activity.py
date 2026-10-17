"""An allowlisted activity channel, bounded log, and secret-free export.

The activity log is a real output channel: it is rendered, copied, and attached
to bug reports. Only allowlisted field names with primitive values may enter it,
every text value is escaped and bounded, and each code has fixed message copy.
Hand-built events are re-validated on emission, so a caller cannot route backend
text or a private value around :func:`safe_activity`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
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
from airplay_linux.domain.resources import CapabilityReport

from .catalog import diagnostic

if TYPE_CHECKING:
    from airplay_linux.domain.ports import Subscription

#: Field names an activity event may carry, and no others.
ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "receiver_name",
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

_FALLBACK_MESSAGE: Final[str] = "Activity recorded."


def _reject_code(code: object) -> str:
    """Validate the code token without ever echoing it back."""
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
    return code


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
    """Keep primitive, sanitized values and reject anything that could be raw."""
    if isinstance(value, PRIVATE_VALUE_TYPES):
        raise ValueError(
            f"value for field {name!r} is a transient private value and cannot be recorded",
        )
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _bounded(_escape(value))
    raise ValueError(
        f"value for field {name!r} must be sanitized text, an integer, or a boolean",
    )


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
    return _MESSAGES.get(code, _FALLBACK_MESSAGE)


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
