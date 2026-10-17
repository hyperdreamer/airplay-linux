"""Fixed actionable messages, one per :class:`ErrorCode`.

Every diagnostic the application shows comes from this catalog. Adapters hand in
a typed code instead of backend text, so a raw exception, argv line, or PIN can
never become user-visible copy. Recovery actions are part of the catalog too:
the UI derives its buttons from them, and an action offered here is a promise
that this code has a way forward.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.resources import Diagnostic, ErrorCode, RecoveryAction

#: Executable names the environment check may report; nothing else is echoed.
ALLOWED_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "doubletake",
        "doubletake-ctl",
        "kscreen-doctor",
        "krfb-virtualmonitor",
    }
)

MAX_MESSAGE_LENGTH: Final[int] = 240
_RECEIVER_ID: Final[re.Pattern[str]] = re.compile(
    r"airplay-device:(?:"
    r"[0-9a-f]{1,64}|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
)


@dataclass(frozen=True, kw_only=True)
class CatalogEntry:
    """One error code's user-facing copy and the actions that resolve it."""

    message: str
    recovery: tuple[RecoveryAction, ...]
    command_message: str | None = None

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("a catalog message cannot be empty")
        if len(self.message) > MAX_MESSAGE_LENGTH:
            raise ValueError("a catalog message is too long for the selected pane")
        if not self.recovery:
            raise ValueError("every diagnosable failure needs a recovery action")
        if self.command_message is not None and "{command}" not in self.command_message:
            raise ValueError("a command message must name its {command} placeholder")


ENTRIES: Mapping[ErrorCode, CatalogEntry] = MappingProxyType(
    {
        ErrorCode.portal_cancelled: CatalogEntry(
            message=(
                "Screen capture was cancelled, so no monitor is being captured. "
                "Go back and choose a physical monitor to start Mirror again."
            ),
            recovery=(RecoveryAction.back, RecoveryAction.stop),
        ),
        ErrorCode.portal_denied: CatalogEntry(
            message=(
                "The desktop denied screen capture for this monitor. Allow the capture "
                "request in the portal prompt, or choose a different physical monitor."
            ),
            recovery=(RecoveryAction.back, RecoveryAction.stop),
        ),
        ErrorCode.keyring_unavailable: CatalogEntry(
            message=(
                "The system keyring is unavailable, so this receiver cannot be paired or "
                "remembered. Unlock the keyring and retry."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.unsupported_source: CatalogEntry(
            message=(
                "The backend cannot verify this physical monitor. Select a supported "
                "source or update doubletake."
            ),
            recovery=(RecoveryAction.back, RecoveryAction.stop),
        ),
        ErrorCode.unsupported_mode: CatalogEntry(
            message=(
                "This receiver does not advertise the requested mode. Choose a mode the "
                "receiver reports as supported."
            ),
            recovery=(RecoveryAction.choose_mode, RecoveryAction.stop),
        ),
        ErrorCode.unsupported_operation: CatalogEntry(
            message=(
                "The installed backend does not expose this operation. Update doubletake "
                "or go back and choose an operation it reports as available."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.back),
        ),
        ErrorCode.dependency_missing: CatalogEntry(
            message=(
                "A required command is not installed or not on PATH. Install it and retry, "
                "or go back and choose a capability that is available."
            ),
            command_message=(
                "The required command '{command}' is not installed or not on PATH. "
                "Install it and retry, or go back and choose a capability that is available."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.back),
        ),
        ErrorCode.dependency_incompatible: CatalogEntry(
            message=(
                "The installed backend version does not expose the required operations. "
                "Update doubletake and retry, or go back and choose a capability that is "
                "available."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.back),
        ),
        ErrorCode.audio_denied: CatalogEntry(
            message=(
                "The desktop denied audio capture for this receiver. Retry audio after "
                "granting permission, or start video only."
            ),
            recovery=(
                RecoveryAction.retry_audio,
                RecoveryAction.video_only,
                RecoveryAction.stop,
            ),
        ),
        ErrorCode.audio_unavailable: CatalogEntry(
            message=(
                "No usable audio output is available for this receiver. Retry audio after "
                "connecting one, or start video only."
            ),
            recovery=(
                RecoveryAction.retry_audio,
                RecoveryAction.video_only,
                RecoveryAction.stop,
            ),
        ),
        ErrorCode.audio_mute_failed: CatalogEntry(
            message=(
                "Mute could not be confirmed, so the last confirmed audio state is kept. "
                "Retry, or stop the session."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.receiver_rejected: CatalogEntry(
            message=(
                "The receiver rejected this request. Confirm the request on the receiver "
                "before submitting again."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.stale_target: CatalogEntry(
            message=(
                "The receiver endpoint changed before this attempt finished. Retry against "
                "the current endpoint."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.stale_event: CatalogEntry(
            message=(
                "A superseded event arrived and was ignored. Retry if the session did not "
                "update."
            ),
            recovery=(RecoveryAction.retry,),
        ),
        ErrorCode.stale_socket: CatalogEntry(
            message=(
                "The backend socket belongs to a previous daemon instance. Retry to reach "
                "the current daemon."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.ownership_mismatch: CatalogEntry(
            message=(
                "This output does not match this app's ownership evidence, so it is never "
                "removed automatically. Confirm removal, or keep it for manual cleanup."
            ),
            recovery=(RecoveryAction.confirm_remove, RecoveryAction.keep),
        ),
        ErrorCode.helper_failed: CatalogEntry(
            message=(
                "The virtual display helper stopped unexpectedly, so the virtual source is "
                "no longer usable. Retry to create a new output, or stop the session."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.configuration_failed: CatalogEntry(
            message=(
                "The virtual output could not be configured. Retry the configuration, or "
                "stop and remove the output."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.cleanup_timeout: CatalogEntry(
            message=(
                "Removal could not be confirmed. Retry cleanup or keep this resource for "
                "manual cleanup. Stop also ends the attempt without claiming removal."
            ),
            recovery=(
                RecoveryAction.retry_cleanup,
                RecoveryAction.keep,
                RecoveryAction.stop,
            ),
        ),
        ErrorCode.trust_cleanup_required: CatalogEntry(
            message=(
                "Trust revocation is unconfirmed, so new pairing is blocked. Retry "
                "revocation, keep pairing blocked, or remove the saved metadata."
            ),
            recovery=(
                RecoveryAction.retry_revocation,
                RecoveryAction.keep_blocked,
                RecoveryAction.remove_metadata,
            ),
        ),
        ErrorCode.resolution_mismatch: CatalogEntry(
            message=(
                "The receiver did not accept the chosen resolution. Recreate the output at "
                "the accepted mode, or stop and remove the retained output. The output is "
                "never changed silently."
            ),
            recovery=(RecoveryAction.recreate, RecoveryAction.stop),
        ),
        ErrorCode.discovery_failed: CatalogEntry(
            message=(
                "Receiver discovery stopped responding. Retry the scan; other receivers "
                "stay usable."
            ),
            recovery=(RecoveryAction.retry,),
        ),
        ErrorCode.transport_failed: CatalogEntry(
            message=(
                "The stream to the receiver failed. Retry the connection, or stop the "
                "session."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.daemon_failed: CatalogEntry(
            message=(
                "The private backend daemon exited or stopped responding. Retry to restart "
                "it, or stop the session."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.persistence_failed: CatalogEntry(
            message=(
                "Session metadata could not be written safely. Retry before removing any "
                "output, so recovery evidence is not lost."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.stop),
        ),
        ErrorCode.invalid_input: CatalogEntry(
            message=(
                "The submitted value was rejected. Enter a new value to try again."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.back),
        ),
        ErrorCode.pairing_expired: CatalogEntry(
            message=(
                "The pairing request expired. Start pairing again from the receiver."
            ),
            recovery=(RecoveryAction.retry, RecoveryAction.back),
        ),
    }
)


def _text_data(value: object) -> str | None:
    """Copy genuine string character data without consulting ``__class__``."""
    if type(value) is str:
        return value
    try:
        copied = str.encode(value, "utf-8", "surrogatepass")
        text = copied.decode("utf-8", "surrogatepass")
    except Exception:
        return None
    return text if type(text) is str else None


def _validated_receiver_id(receiver_id: object) -> ReceiverId | None:
    """Accept only the canonical public identity shape used for correlation."""
    if receiver_id is None:
        return None
    text = _text_data(receiver_id)
    if text is None:
        raise TypeError("receiver_id must be a ReceiverId or None")
    if _RECEIVER_ID.fullmatch(text) is None:
        raise ValueError("receiver_id must be a canonical public receiver identity")
    return ReceiverId(text)


def _validated_command(command: str | None) -> str | None:
    """Accept only an allowlisted executable name; never echo the argument.

    The name is interpolated into user-visible copy, so a ``str`` subclass may
    not decide what is checked and what is rendered. Its character data is
    copied to an exact built-in string first, membership is decided on that
    primitive, and that same primitive is what the caller formats.
    """
    if command is None:
        return None
    text = _text_data(command)
    if text is None or text not in ALLOWED_COMMANDS:
        raise ValueError(
            "command must be one of the allowlisted executable names; "
            "arbitrary backend text is not a diagnostic",
        )
    return text


def _validated_code(code: object) -> ErrorCode:
    """The genuine enum member for ``code``, or a fixed rejection.

    ``isinstance`` is not proof of an enum member: a class can advertise
    ``__class__`` as :class:`ErrorCode`, so a look-alike ``str`` passes the type
    test while it really renders, compares, and hashes whatever it likes. Only
    the exact enum type may reach the catalog lookup and the returned
    :class:`Diagnostic`; anything else is rejected with fixed text, so no
    catalog ``KeyError`` ever renders the argument.
    """
    if type(code) is not ErrorCode:
        raise TypeError(
            "code must be an ErrorCode from the diagnostics catalog; "
            "arbitrary error text is not a diagnostic",
        )
    return code


def diagnostic(
    code: ErrorCode,
    receiver_id: ReceiverId | None = None,
    *,
    command: str | None = None,
) -> Diagnostic:
    """Build the catalog diagnostic for ``code``; no backend text is accepted."""
    validated = _validated_code(code)
    receiver = _validated_receiver_id(receiver_id)
    entry = ENTRIES[validated]
    name = _validated_command(command)
    message = entry.message
    if name is not None and entry.command_message is not None:
        message = entry.command_message.format(command=name)
    return Diagnostic(
        code=validated,
        message=message,
        recovery=entry.recovery,
        receiver_id=receiver,
        detail=None,
    )
