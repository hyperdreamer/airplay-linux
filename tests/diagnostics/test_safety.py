"""Diagnostics must stay actionable for the user and silent about private input.

The activity log is an output channel: every value that reaches it, its bounded
export, an emitted entry's repr, or a rejection message is tested for the canary
strings a secret would leave behind.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Final

import pytest

from airplay_linux.diagnostics import (
    ACTIVITY_CODES,
    ALLOWED_COMMANDS,
    ENTRIES,
    ActivityLog,
    diagnostic,
    safe_activity,
)
from airplay_linux.diagnostics.activity import (
    ALLOWED_FIELDS,
    FIELD_SHAPES,
    MAX_FIELD_TEXT,
    _VOCABULARIES,
    _bounded,
    _escape,
)
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
    Diagnostic,
    DisplayState,
    ErrorCode,
    RecoveryAction,
    SessionPhase,
    TransportState,
    TrustState,
)
from airplay_linux.testing import MemoryActivity, SequenceIds

RECEIVER = ReceiverId("airplay-device:001122334455")
UTC = "2026-10-17T00:00:00Z"
CANARY = "canary-8496"

#: A secret that hides inside ordinary-looking text and identifier shapes.
CANARY_TEXT = "raw-backend-canary-8496"

#: Every field the channel can still record, with one legitimate value each.
#: ``receiver_name`` is deliberately absent: it is free-form network text, so no
#: value shape can keep a secret out of it. See
#: ``test_receiver_name_is_no_longer_an_activity_field``.
FIELD_SAMPLES: dict[str, str | int | bool] = {
    "phase": "Mirroring",
    "transport": "Streaming",
    "display": "Owned",
    "audio": "Enabled",
    "trust": "Trusted",
    "command_name": "kscreen-doctor",
    "retry_seconds": 4,
    "resolution": "1920x1080",
    "connector": "DP-1",
    "output_id": "v1",
    "reservation_stage": "published",
    "endpoint_generation": 3,
    "error_code": "cleanup_timeout",
    "explicit_video_only": True,
}

#: Every value the frozen domain enumerates for one vocabulary-shaped field.
ENUMERATED_VALUES: tuple[tuple[str, str], ...] = (
    tuple(
        (name, member.value)
        for name, enum in (
            ("phase", SessionPhase),
            ("transport", TransportState),
            ("display", DisplayState),
            ("audio", AudioState),
            ("trust", TrustState),
            ("error_code", ErrorCode),
        )
        for member in enum
    )
    + tuple(("command_name", command) for command in sorted(ALLOWED_COMMANDS))
    + tuple(
        ("reservation_stage", stage)
        for stage in ("reserved", "helper_started", "output_observed", "published", "removing")
    )
)

SECRET_FIELD_NAMES = (
    "pin",
    "password",
    "credential",
    "correlation_token",
    "owner_token",
    "restore_token",
    "portal_restore_token",
    "argv",
    "stdin",
    "stdout",
    "stderr",
    "exception",
    "traceback",
    "secret",
)

SECRET_VALUES = (
    SensitiveText(CANARY),
    OpaqueCredentialRef(CANARY),
    OpaqueProvisionalCredentialRef(CANARY),
    OpaqueBackendToken(CANARY),
)


# --------------------------------------------------------------- diagnostics


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_has_a_helpful_catalog_diagnostic(code):
    result = diagnostic(code)

    assert isinstance(result, Diagnostic)
    assert result.code is code
    assert result.receiver_id is None
    assert result.detail is None
    assert result.message.strip()
    assert result.recovery, f"{code} must offer at least one recovery action"
    assert all(isinstance(action, RecoveryAction) for action in result.recovery)


#: Non-``str.format`` placeholder syntax a fixed message must never carry: printf
#: verbs, ``<name>`` slots and ``${...}`` interpolation. Braces are checked
#: structurally below, with :mod:`string`'s own parser.
_FOREIGN_PLACEHOLDER: Final[re.Pattern[str]] = re.compile(
    r"%[-+ #0]*[0-9]*(?:\.[0-9]+)?[sdrifXxeEgG]|<[A-Za-z_][A-Za-z0-9_ .-]{0,40}>|\$\{[^{}]*\}",
)


def replacement_fields(template: str) -> tuple[str, ...]:
    """The named or positioned fields ``str.format`` would substitute."""
    return tuple(
        field for _, field, _, _ in string.Formatter().parse(template) if field is not None
    )


def test_catalog_messages_are_distinct_and_fully_rendered():
    messages = [diagnostic(code).message for code in ErrorCode]

    assert len(set(messages)) == len(messages), "each code needs its own message"
    for code, message in zip(ErrorCode, messages, strict=True):
        assert message.strip(), f"{code} needs non-empty copy"
        assert message == message.strip(), f"{code} must be rendered, not padded"
        assert message.isprintable(), f"{code} must carry no control characters"
        assert len(message) <= 240
        # A rendered message is the entry's own fixed literal: no placeholder
        # survived rendering, and nothing was substituted into it.
        assert message == ENTRIES[code].message
        assert replacement_fields(message) == ()
        assert _FOREIGN_PLACEHOLDER.search(message) is None, (
            f"{code} still contains placeholder syntax: {message!r}"
        )


def test_every_catalog_template_declares_only_its_own_fields():
    # ``diagnostic`` substitutes exactly one value, the allowlisted command name,
    # and only where an entry declares that field. Any other field name would be
    # a template slot with no provider, and any unnamed slot a silent echo.
    for code, entry in ENTRIES.items():
        fields = replacement_fields(entry.message)
        assert fields == (), f"{code} interpolates into its fixed message copy: {fields}"
        if entry.command_message is not None:
            assert replacement_fields(entry.command_message) == ("command",), (
                f"{code} must interpolate the command name and nothing else"
            )


def test_no_catalog_message_interpolates_a_caller_supplied_value():
    # ``diagnostic`` accepts exactly two optional values, a receiver identity and
    # a command name; the catalog has no other input. Fixed copy therefore means
    # the message is byte-identical whichever legal argument is supplied, which is
    # the whole of "no interpolated input value". Only ``dependency_missing``
    # renders a command, and only after the allowlist check.
    commanded = {code for code, entry in ENTRIES.items() if entry.command_message is not None}

    for code in ErrorCode:
        base = diagnostic(code).message
        for receiver in (None, RECEIVER, ReceiverId("airplay-device:abcdef")):
            assert diagnostic(code, receiver).message == base, (
                f"{code} must not interpolate the receiver identity"
            )
        if code in commanded:
            continue
        for command in sorted(ALLOWED_COMMANDS):
            assert diagnostic(code, command=command).message == base, (
                f"{code} must not interpolate a command name it does not name"
            )

    entry = ENTRIES[ErrorCode.dependency_missing]
    assert entry.command_message is not None, "the command entry must keep its message"
    for command in sorted(ALLOWED_COMMANDS):
        rendered = diagnostic(ErrorCode.dependency_missing, command=command).message
        assert rendered == entry.command_message.format(command=command)
        assert f"'{command}'" in rendered
        assert replacement_fields(rendered) == ()
        assert _FOREIGN_PLACEHOLDER.search(rendered) is None
        assert CANARY_TEXT not in rendered

    with pytest.raises(ValueError, match="command"):
        diagnostic(ErrorCode.dependency_missing, command=CANARY_TEXT)

    # F-1: a plain string proves nothing about the boundary. The module's own
    # subclass probes are the axis. ``SpoofText`` carries the canary as its data
    # while it compares and hashes as 'doubletake'; ``RenderSpoof`` carries
    # 'doubletake' while every hook renders the canary. Either outcome is a pass
    # -- rejection without an echo, or normalization to the allowlisted name --
    # but no canary may reach the message or the repr.
    for probe in (SpoofText("doubletake"), RenderSpoof("doubletake")):
        try:
            result = diagnostic(ErrorCode.dependency_missing, command=probe)
        except ValueError as error:
            assert SMUGGLED not in str(error)
            assert SMUGGLED not in repr(error)
            continue
        assert type(result.message) is str
        assert SMUGGLED not in result.message
        assert SMUGGLED not in repr(result)


def test_diagnostic_keeps_the_receiver_identity():
    result = diagnostic(ErrorCode.transport_failed, RECEIVER)

    assert result.receiver_id == RECEIVER
    assert type(result.receiver_id) is str


def test_diagnostic_rejects_a_spoofed_receiver_identity_without_reading_it():
    # Only an exact ``str`` is an identity. A subclass is rejected rather than
    # copied, so neither its data nor its rendering can become the retained
    # correlation value.
    spoof = ReceiverId(RenderSpoof("airplay-device:001122334455"))

    with pytest.raises(TypeError) as caught:
        diagnostic(ErrorCode.transport_failed, spoof)

    assert_clean_boundary_error(caught.value, TypeError)
    assert str(caught.value) == "receiver_id must be a ReceiverId or None"


def test_diagnostic_rejects_noncanonical_receiver_ids_without_retaining_them():
    with pytest.raises(ValueError) as caught:
        diagnostic(ErrorCode.transport_failed, ReceiverId("airplay-device:" + CANARY_TEXT))

    assert_clean_boundary_error(caught.value, ValueError)


def test_a_diagnostic_receiver_id_is_preserved_exactly_when_canonical():
    canonical = ReceiverId("airplay-device:abcdef0123456789")
    result = diagnostic(ErrorCode.transport_failed, canonical)

    assert result.receiver_id == canonical
    assert type(result.receiver_id) is str
    assert result.receiver_id == "airplay-device:abcdef0123456789"
    assert CANARY_TEXT not in result.message
    assert CANARY_TEXT not in repr(result)


def test_raising_class_string_subclasses_are_rejected_by_the_catalog():
    # The advertised class is never consulted, so a raising ``__class__`` cannot
    # decide anything here: the value is not an exact ``str`` and is rejected
    # with fixed text that carries none of its own.
    for invoke, expected_type in (
        (
            lambda: diagnostic(
                ErrorCode.dependency_missing,
                ReceiverId(RaisingClassStr("airplay-device:abcdef0123456789")),
            ),
            TypeError,
        ),
        (
            lambda: diagnostic(
                ErrorCode.dependency_missing, command=RaisingClassStr("doubletake")
            ),
            ValueError,
        ),
    ):
        with pytest.raises(expected_type) as caught:
            invoke()
        assert_clean_boundary_error(caught.value, expected_type)


def test_diagnostic_rejects_arbitrary_error_text():
    with pytest.raises(TypeError) as caught:
        diagnostic(CANARY)  # type: ignore[arg-type]

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_every_genuine_error_code_still_builds_its_catalog_diagnostic():
    # The impostor check below tightens the boundary, so the positive case is
    # pinned too: every frozen member must still resolve to its own entry, with
    # a real ``ErrorCode`` and the fixed copy it always had.
    assert len(list(ErrorCode)) > 1, "the probe must iterate the real enumeration"

    for member in ErrorCode:
        result = diagnostic(member)

        assert isinstance(result, Diagnostic)
        assert type(result.code) is ErrorCode
        assert result.code is member
        assert result.message == ENTRIES[member].message
        assert result.recovery == ENTRIES[member].recovery
        assert result.receiver_id is None
        assert result.detail is None


def test_an_error_code_impostor_is_rejected_without_rendering_it():
    # A ``str`` subclass can advertise ``__class__`` as ``ErrorCode``, so
    # ``isinstance`` approves it. Data that happens to match a catalog key would
    # then index the catalog and be returned *as* the code; data that matches
    # nothing reached ``KeyError`` with the raw text in the exception message.
    # Both are rejected with the module's existing fixed type and no echo.
    for data in (ErrorCode.transport_failed.value, "not-a-registered-error-code"):
        impostor = CodeImpostor(data)
        assert isinstance(impostor, ErrorCode), "the probe must pass isinstance"
        assert type(impostor) is not ErrorCode, "the probe must not be the enum"

        with pytest.raises(TypeError) as caught:
            diagnostic(impostor)  # type: ignore[arg-type]

        assert IMPOSTOR_CANARY not in str(caught.value)
        assert IMPOSTOR_CANARY not in repr(caught.value)
        # A ``KeyError`` would have tried to render the unvalidated object.
        assert not isinstance(caught.value, KeyError)


def test_an_error_code_impostor_never_reaches_a_diagnostic():
    # Nothing may be produced that carries the impostor, not even when its data
    # is a genuine enumerated value with a real catalog entry behind it.
    impostor = CodeImpostor(ErrorCode.cleanup_timeout.value)

    with pytest.raises(TypeError) as caught:
        diagnostic(impostor)  # type: ignore[arg-type]

    assert IMPOSTOR_CANARY not in str(caught.value)
    assert IMPOSTOR_CANARY not in repr(caught.value)
    # Every argument the exception carries is checked, so a fixed message plus a
    # stray payload argument cannot slip through behind a clean ``str``.
    for argument in caught.value.args:
        assert IMPOSTOR_CANARY not in repr(argument)


def test_diagnostic_rejects_a_command_outside_the_allowlist():
    with pytest.raises(ValueError, match="command") as caught:
        diagnostic(ErrorCode.dependency_missing, command=CANARY)

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_a_str_subclass_command_cannot_smuggle_text_into_the_diagnostic():
    # F-1: ``diagnostic`` interpolates the command name into user-visible copy,
    # so it holds the boundary ``activity`` already holds: the value's character
    # *data* is the only text that may be validated, and the exact built-in
    # string whose data was validated is the string that is formatted. Each
    # probe is either rejected without echoing itself, or accepted only as its
    # own data; none may put the canary in the message or the repr.
    probes = (
        # Data is the payload; membership and rendering both claim the name.
        SpoofText("doubletake"),
        # Data is the allowlisted name; every hook renders the payload instead.
        RenderSpoof("doubletake"),
        # Claims the allowlisted name by rendering; the data is the payload.
        RenderSpoof(SMUGGLED, "doubletake"),
    )

    for probe in probes:
        try:
            result = diagnostic(ErrorCode.dependency_missing, command=probe)
        except ValueError as error:
            assert SMUGGLED not in str(error)
            assert SMUGGLED not in repr(error)
            continue
        assert type(result.message) is str
        assert "'doubletake'" in result.message
        assert SMUGGLED not in result.message
        assert SMUGGLED not in repr(result)


@pytest.mark.parametrize("command", ["doubletake", "doubletake-ctl", "kscreen-doctor", "krfb-virtualmonitor"])
def test_dependency_diagnostic_names_an_allowlisted_command(command):
    result = diagnostic(ErrorCode.dependency_missing, command=command)

    assert f"'{command}'" in result.message
    assert result.detail is None


def test_dependency_diagnostic_without_a_command_names_no_executable():
    result = diagnostic(ErrorCode.dependency_missing)

    assert "required command" in result.message.lower()
    assert "''" not in result.message


def test_command_is_ignored_by_messages_that_name_no_executable():
    assert (
        diagnostic(ErrorCode.transport_failed, command="doubletake").message
        == diagnostic(ErrorCode.transport_failed).message
    )


def test_trust_cleanup_blocks_pairing_until_revocation_succeeds():
    result = diagnostic(ErrorCode.trust_cleanup_required)

    assert "pairing" in result.message.lower()
    assert "blocked" in result.message.lower()
    assert RecoveryAction.retry_revocation in result.recovery
    assert RecoveryAction.keep_blocked in result.recovery
    # A plain retry would read as "pairing is allowed again"; it is not.
    assert RecoveryAction.retry not in result.recovery


@pytest.mark.parametrize("code", [ErrorCode.audio_denied, ErrorCode.audio_unavailable])
def test_audio_failure_offers_explicit_video_only_and_retry(code):
    recovery = diagnostic(code).recovery

    assert RecoveryAction.retry_audio in recovery
    assert RecoveryAction.video_only in recovery


def test_cleanup_timeout_offers_retry_cleanup():
    result = diagnostic(ErrorCode.cleanup_timeout)

    assert RecoveryAction.retry_cleanup in result.recovery
    assert "removal could not be confirmed" in result.message.lower()


def test_unsupported_source_names_the_physical_monitor_rule():
    result = diagnostic(ErrorCode.unsupported_source, RECEIVER)

    assert "physical monitor" in result.message
    assert result.receiver_id == RECEIVER


def test_ownership_mismatch_never_offers_automatic_removal():
    recovery = diagnostic(ErrorCode.ownership_mismatch).recovery

    assert RecoveryAction.confirm_remove in recovery
    assert RecoveryAction.keep in recovery


# ------------------------------------------------- design receiver identity

#: The `sha256` payload the three hash-derived design forms carry: 64 lowercase
#: hex characters, which is the whole of their declared shape.
FALLBACK_DIGEST: Final[str] = "0123456789abcdef" * 4

assert len(FALLBACK_DIGEST) == 64

#: One legitimate value of every identity form the approved design defines.
#: ``airplay-ambiguous:`` is ephemeral by design, but it is still an identity a
#: diagnostics boundary must be able to record without inventing a fifth form.
DESIGN_RECEIVER_IDS: Final[tuple[str, ...]] = (
    "airplay-device:001122334455",
    "airplay-device:abcdef0123456789",
    f"airplay-fingerprint:{FALLBACK_DIGEST}",
    f"airplay-endpoint:{FALLBACK_DIGEST}",
    f"airplay-ambiguous:{FALLBACK_DIGEST}",
)

#: Values no design form describes: a wrong or missing prefix, a payload of the
#: wrong length, case, or alphabet, embedded control or space characters, free
#: backend text, and secret-bearing suffixes after an otherwise valid prefix.
REJECTED_RECEIVER_IDS: Final[tuple[str, ...]] = (
    "device:001122334455",
    "airplay-devices:001122334455",
    "airplay_device:001122334455",
    "airplay-device:",
    f"airplay-fingerprint:{FALLBACK_DIGEST.upper()}",
    f"airplay-endpoint:{FALLBACK_DIGEST[:-1]}",
    f"airplay-ambiguous:{FALLBACK_DIGEST}a",
    f"airplay-fingerprint:{FALLBACK_DIGEST[:-1]}g",
    f"airplay-endpoint:{FALLBACK_DIGEST}\n",
    f"airplay-ambiguous: {FALLBACK_DIGEST}",
    f"airplay-fingerprint:{FALLBACK_DIGEST}{CANARY_TEXT}",
    f"airplay-endpoint:{FALLBACK_DIGEST};password={CANARY_TEXT}",
    "airplay-device:AirPlay TV",
    f"airplay-device:001122334455 ({CANARY_TEXT})",
    f"airplay-device:{CANARY_TEXT}",
    f"{CANARY_TEXT}:{FALLBACK_DIGEST}",
)


@pytest.mark.parametrize("identity", DESIGN_RECEIVER_IDS)
def test_diagnostic_accepts_every_design_receiver_identity(identity):
    result = diagnostic(ErrorCode.transport_failed, ReceiverId(identity))

    # Byte-for-byte: the accepted value is the exact built-in string that was
    # checked, not a re-rendering, a truncation, or a subclass instance.
    assert result.receiver_id == identity
    assert type(result.receiver_id) is str
    assert result.receiver_id.encode("utf-8") == identity.encode("utf-8")
    assert result.message == diagnostic(ErrorCode.transport_failed).message
    assert CANARY_TEXT not in repr(result)


@pytest.mark.parametrize("identity", DESIGN_RECEIVER_IDS)
def test_safe_activity_accepts_every_design_receiver_identity(identity):
    event = safe_activity("session", ReceiverId(identity), {}, UTC)
    log = ActivityLog()
    log.emit(event)

    assert event.receiver_id == identity
    assert type(event.receiver_id) is str
    assert event.receiver_id.encode("utf-8") == identity.encode("utf-8")
    assert log.entries()[0].receiver_id == identity
    assert json.loads(log.export())["activity"][0]["receiver_id"] == identity
    assert CANARY_TEXT not in repr(event)
    assert CANARY_TEXT not in log.export()


@pytest.mark.parametrize("identity", DESIGN_RECEIVER_IDS)
def test_both_identity_boundaries_reject_a_spoofed_value_of_every_form(identity):
    # A subclass cannot approve itself by rendering a valid identity: the
    # boundary accepts the exact ``str`` type only, for every design form.
    spoof = ReceiverId(RenderSpoof(identity))

    for invoke in (
        lambda: diagnostic(ErrorCode.transport_failed, spoof),
        lambda: safe_activity("session", spoof, {}, UTC),
    ):
        with pytest.raises(TypeError) as caught:
            invoke()
        assert_clean_boundary_error(caught.value, TypeError)
        assert str(caught.value) == "receiver_id must be a ReceiverId or None"


@pytest.mark.parametrize("constructor", ["diagnostic", "safe_activity"])
@pytest.mark.parametrize("identity", REJECTED_RECEIVER_IDS)
def test_neither_identity_boundary_accepts_a_non_design_identity(constructor, identity):
    if constructor == "diagnostic":
        invoke = lambda: diagnostic(ErrorCode.transport_failed, ReceiverId(identity))
    else:
        invoke = lambda: safe_activity("session", ReceiverId(identity), {}, UTC)

    with pytest.raises(ValueError) as caught:
        invoke()

    assert_clean_boundary_error(caught.value, ValueError)
    # Fixed text: nothing from the rejected value, not even a valid prefix or a
    # secret-bearing suffix, is rendered back to the caller.
    assert str(caught.value) == "receiver_id must be a canonical public receiver identity"
    assert identity not in repr(caught.value)
    payload = identity.split(":", 1)[-1].strip()
    if payload:
        assert payload not in str(caught.value)


# ------------------------------------------------- design action matrix

#: The approved design that owns the normative action table. This lane reads it
#: and never edits it. The expected labels are *read from the document* rather
#: than quoted into the test, so a hand-copied row cannot drift from the design
#: and still agree with itself.
DESIGN_PATH: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-10-17-airplay-mirroring-design.md"
)

#: The design's UI state matrix header; the table ends at the first non-row line.
DESIGN_TABLE_HEADER: Final[str] = "| Domain/resource state |"


def design_action_cells() -> dict[str, str]:
    """The design's "Allowed primary actions" cell, keyed by its state cell.

    Read at test time, so a missing or reshaped design fails that row as a test
    failure instead of breaking collection.
    """
    cells: dict[str, str] = {}
    in_table = False
    for line in DESIGN_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(DESIGN_TABLE_HEADER):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            break
        row = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(row) != 3:
            continue
        if all(cell and set(cell) <= set("-: ") for cell in row):
            continue  # the header separator rule
        cells[row[0]] = row[2]
    assert cells, "the design's UI state matrix must be present and parseable"
    return cells


def design_action_labels(cell: str) -> tuple[str, ...]:
    """The actions one design cell enumerates, without its reading "or"."""
    labels: list[str] = []
    for part in cell.split(","):
        label = part.strip()
        if label.lower().startswith("or "):
            label = label[3:].strip()
        labels.append(label)
    return tuple(labels)


#: The design's state rows that name typed failure recovery, and the codes each
#: governs (design:111, :119, :121-128). A code the design reports in more than
#: one row carries the union of those rows, so an entry names every state it
#: answers for and the test checks each state's own contribution separately.
DESIGN_ROWS: tuple[tuple[tuple[str, ...], tuple[ErrorCode, ...]], ...] = (
    (
        ("Resolution mismatch",),
        (ErrorCode.resolution_mismatch,),
    ),
    (
        ("Dependency or capability failure",),
        (
            ErrorCode.unsupported_operation,
            ErrorCode.dependency_missing,
            ErrorCode.dependency_incompatible,
        ),
    ),
    (
        ("Orphan candidate",),
        (ErrorCode.ownership_mismatch,),
    ),
    (
        ("Cleanup required",),
        (ErrorCode.cleanup_timeout,),
    ),
    (
        ("Trust cleanup required",),
        (ErrorCode.trust_cleanup_required,),
    ),
    (
        # One code reports a pre-video audio decision and a runtime audio failure,
        # which are two design rows with different action sets: the union is
        # pinned, and each row must still contribute its own actions exactly.
        (
            "Audio decision required",
            "Audio unavailable (video active)",
        ),
        (ErrorCode.audio_denied, ErrorCode.audio_unavailable),
    ),
    (
        # Recovering (design:111), Reconnecting (design:119) and Failed
        # (design:128) share one action set; that these rows really agree is
        # asserted below rather than assumed in a comment.
        (
            "Recovering (`Output owned`, transport reconnecting)",
            "Reconnecting (`Output owned` or no output)",
            "Failed",
        ),
        (
            ErrorCode.transport_failed,
            ErrorCode.daemon_failed,
            ErrorCode.helper_failed,
            ErrorCode.configuration_failed,
            ErrorCode.receiver_rejected,
            ErrorCode.stale_target,
            ErrorCode.stale_socket,
            ErrorCode.persistence_failed,
        ),
    ),
)

DESIGN_ROW_IDS: Final[tuple[str, ...]] = tuple(
    " + ".join(states) for states, _ in DESIGN_ROWS
)

#: The design rows this audit deliberately does not pin, and why. A row is either
#: covered by :data:`DESIGN_ROWS` or excused here, so a new row in the design's
#: matrix fails the completeness test until it is deliberately classified.
DESIGN_UNPINNED_ROWS: Final[dict[str, str]] = {
    "Available (`Idle`, no output)": "no failure: the pane offers Start, Pair, Scan",
    "Offline (`Idle`, no output)": "no failure: a saved receiver with no record",
    "Pairing (`No output`)": "a healthy prompt, not an error recovery",
    "Pairing (`Output owned`)": "a healthy prompt over a retained output",
    "Resolution required": "a required decision, not an error",
    "Creating output": "progress; only Cancel, which is the Stopping path",
    "Connecting": "progress; only Stop",
    "Mirroring": "a healthy session: mute and stop, not recovery",
    "Virtual display": "a healthy session: mute, stop and change resolution",
    "Reconfiguring": "progress; only Cancel, which is the Stopping path",
}

#: Codes the design gives no row in the action matrix, with the prose that governs
#: them. Their recovery tuples stay exactly as they are: this audit repairs a row
#: that contradicts a designed action, and it never invents one.
UNPINNED_CODES: Final[frozenset[ErrorCode]] = frozenset(
    {
        # design:451 lists these as distinct actionable errors. design:328 makes
        # portal denial and cancellation recoverable capture events, whose pane
        # offers another physical source (``back``) or stop; the matrix names no
        # row for them.
        ErrorCode.portal_cancelled,
        ErrorCode.portal_denied,
        # design:466 requires a usable keyring for remembered pairing, and
        # design:451 keeps a keyring failure actionable. The matrix names no row.
        ErrorCode.keyring_unavailable,
        # design:64/534 surface an unverifiable physical monitor as unsupported so
        # the user chooses another source rather than retrying the descriptor.
        ErrorCode.unsupported_source,
        # design:114 is the "Resolution required" row, which offers Choose and
        # Cancel for a value the user has not supplied yet; it names no recovery
        # for a mode the receiver does not advertise.
        ErrorCode.unsupported_mode,
        # design:365 keeps the prior known audio state on a mute failure and reports
        # the backend error; the matrix enumerates no primary action for that case.
        ErrorCode.audio_mute_failed,
        # Target-local, retryable events the design describes in prose only:
        # design:439 (discovery), design:402 (a stale event changes nothing),
        # design:355 (bounded invalid-input budget and challenge expiry).
        ErrorCode.discovery_failed,
        ErrorCode.stale_event,
        ErrorCode.invalid_input,
        ErrorCode.pairing_expired,
    }
)


#: The design's words to the catalog's typed action. The catalog has no separate
#: cancel token, so the design's ``Cancel`` for an abandoned attempt is ``stop``.
DESIGN_ACTIONS: dict[str, RecoveryAction] = {
    "Recreate at accepted mode": RecoveryAction.recreate,
    "Retry after fix": RecoveryAction.retry,
    "Retry": RecoveryAction.retry,
    "Back": RecoveryAction.back,
    "Remove after confirmation": RecoveryAction.confirm_remove,
    "Keep": RecoveryAction.keep,
    "Keep for manual cleanup": RecoveryAction.keep,
    "Retry cleanup": RecoveryAction.retry_cleanup,
    "Retry revocation": RecoveryAction.retry_revocation,
    "Keep blocked": RecoveryAction.keep_blocked,
    "Remove saved metadata": RecoveryAction.remove_metadata,
    "Retry audio": RecoveryAction.retry_audio,
    "explicit video-only start": RecoveryAction.video_only,
    "Cancel": RecoveryAction.stop,
    "Stop": RecoveryAction.stop,
}


def test_every_design_action_label_maps_to_a_typed_action():
    table = design_action_cells()

    for states, _ in DESIGN_ROWS:
        for state in states:
            assert state in table, f"the design no longer has a {state!r} row"
            labels = design_action_labels(table[state])
            assert labels, f"the design's {state!r} row must name an action"
            for label in labels:
                assert label in DESIGN_ACTIONS, (
                    f"design row {state!r} names an action with no typed token: {label!r}"
                )


def test_every_design_matrix_row_is_pinned_or_deliberately_excused():
    # Both directions, so neither a new design row nor a renamed one can slip past
    # the audit: each row is either governed by a pinned code or excused by name.
    table = design_action_cells()
    pinned = {state for states, _ in DESIGN_ROWS for state in states}
    excused = set(DESIGN_UNPINNED_ROWS)

    assert not pinned & excused, "a design row cannot be both pinned and excused"
    assert pinned | excused == set(table), (
        "every design row must be pinned or excused; unclassified: "
        f"{sorted(set(table) - pinned - excused)}, stale: {sorted(pinned | excused - set(table))}"
    )


def test_design_matrix_rows_do_not_contradict_each_other():
    pinned = [code for _, codes in DESIGN_ROWS for code in codes]

    assert len(pinned) == len(set(pinned)), "a code may be pinned to one row only"


def test_every_error_code_is_pinned_or_deliberately_unpinned():
    pinned = {code for _, codes in DESIGN_ROWS for code in codes}

    assert not pinned & UNPINNED_CODES, "a code cannot be both pinned and excused"
    assert pinned | UNPINNED_CODES == set(ErrorCode), (
        "every code must answer a design row or be deliberately excused, so a new "
        "member cannot escape the audit"
    )


@pytest.mark.parametrize(("states", "codes"), DESIGN_ROWS, ids=DESIGN_ROW_IDS)
def test_catalog_recovery_matches_the_design_action_matrix(states, codes):
    table = design_action_cells()
    expected = frozenset(
        DESIGN_ACTIONS[label]
        for state in states
        for label in design_action_labels(table[state])
    )

    for code in codes:
        recovery = diagnostic(code).recovery
        assert len(set(recovery)) == len(recovery), f"{code} repeats a recovery action"
        # Each design row must contribute its own actions exactly: a merged entry
        # may not drop one row's action or invent an action neither row allows.
        for state in states:
            row = frozenset(DESIGN_ACTIONS[label] for label in design_action_labels(table[state]))
            assert row <= set(recovery), (
                f"{code} must offer every action of design row {state!r}: "
                f"missing {sorted(row - set(recovery))}"
            )
        assert set(recovery) == expected, (
            f"{code} must offer exactly {sorted(expected)} for {list(states)}; "
            f"it offers {sorted(set(recovery))}"
        )


def test_resolution_mismatch_offers_the_only_design_recovery():
    result = diagnostic(ErrorCode.resolution_mismatch)

    assert RecoveryAction.recreate in result.recovery
    assert RecoveryAction.back not in result.recovery
    assert "accepted mode" in result.message.lower()


def test_trust_cleanup_offers_a_way_to_drop_the_saved_metadata():
    recovery = diagnostic(ErrorCode.trust_cleanup_required).recovery

    assert RecoveryAction.remove_metadata in recovery
    assert "metadata" in diagnostic(ErrorCode.trust_cleanup_required).message.lower()


# ------------------------------------------------------------------ activity


def test_allowlist_matches_the_frozen_field_permit():
    assert set(FIELD_SAMPLES) == ALLOWED_FIELDS
    assert MAX_FIELD_TEXT <= 200


def test_every_allowlisted_field_declares_a_value_shape():
    assert set(FIELD_SHAPES) == ALLOWED_FIELDS


def test_receiver_name_is_no_longer_an_activity_field():
    # A receiver display name is free-form network text, so no value shape can
    # keep a secret out of it. The receiver identity itself is ``receiver_id``.
    assert "receiver_name" not in ALLOWED_FIELDS

    with pytest.raises(ValueError, match="field") as caught:
        safe_activity("session", None, {"receiver_name": "Office TV"}, UTC)

    assert "Office TV" not in str(caught.value)


@pytest.mark.parametrize("name", sorted(ALLOWED_FIELDS))
def test_canary_cannot_survive_in_any_allowlisted_field(name):
    try:
        event = safe_activity("session", RECEIVER, {name: CANARY_TEXT}, UTC)
    except ValueError as error:
        assert CANARY_TEXT not in str(error)
        assert CANARY_TEXT not in repr(error)
        return

    log = ActivityLog()
    log.emit(event)
    for rendering in (
        repr(event),
        repr(dict(event.fields)),
        repr(log.entries()),
        log.export(),
    ):
        assert CANARY_TEXT not in rendering


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("connector", f"DP-1 {CANARY_TEXT}"),
        ("output_id", f"v1{CANARY_TEXT}"),
        ("resolution", f"1920x1080 {CANARY_TEXT}"),
        ("phase", f"Mirroring {CANARY_TEXT}"),
        ("command_name", f"kscreen-doctor {CANARY_TEXT}"),
        ("reservation_stage", f"published {CANARY_TEXT}"),
        ("transport", f"Streaming{CANARY_TEXT}"),
    ],
)
def test_a_canary_appended_to_a_legitimate_value_is_rejected(name, value):
    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("session", None, {name: value}, UTC)

    assert CANARY_TEXT not in str(caught.value)
    assert CANARY_TEXT not in repr(caught.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("phase", "Mirroring "),
        ("phase", "mirroring"),
        ("transport", "Streaming; rm -rf /"),
        ("display", "Owned\n"),
        ("audio", "Enabled\x00"),
        ("trust", "RevocationPending "),
        ("error_code", "cleanup-timeout"),
        ("command_name", "/usr/bin/kscreen-doctor"),
        ("command_name", "rm -rf /"),
        ("connector", "DP-9;rm"),
        ("connector", "DP"),
        ("output_id", "v1\n"),
        ("output_id", "-1"),
        ("resolution", "1920X1080"),
        ("resolution", "1920x"),
        ("reservation_stage", "Published"),
    ],
)
def test_text_outside_a_fields_shape_is_rejected(name, value):
    with pytest.raises(ValueError, match="value"):
        safe_activity("session", None, {name: value}, UTC)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("retry_seconds", -1),
        ("retry_seconds", 10**6),
        ("retry_seconds", True),
        ("retry_seconds", "4"),
        ("retry_seconds", 4.0),
        ("endpoint_generation", -1),
        ("endpoint_generation", 2**31),
        ("endpoint_generation", True),
        ("endpoint_generation", "3"),
        ("explicit_video_only", "true"),
        ("explicit_video_only", 1),
        ("explicit_video_only", None),
    ],
)
def test_numeric_fields_accept_only_bounded_numbers_of_their_own_type(name, value):
    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("session", None, {name: value}, UTC)

    assert CANARY_TEXT not in str(caught.value)


@pytest.mark.parametrize(("name", "value"), ENUMERATED_VALUES)
def test_legitimate_enumerated_values_are_accepted_unchanged(name, value):
    event = safe_activity("session", RECEIVER, {name: value}, UTC)

    assert event.fields == {name: value}
    assert type(event.fields[name]) is str


def test_domain_enum_members_are_normalized_to_plain_text():
    event = safe_activity("session", None, {"phase": SessionPhase.Mirroring}, UTC)

    assert event.fields == {"phase": "Mirroring"}
    assert type(event.fields["phase"]) is str


def test_a_str_subclass_cannot_render_one_value_and_carry_another():
    class Liar(str):
        def __str__(self) -> str:
            return "Mirroring"

    # The object's own data is backend text; only its (spoofed) rendering looks
    # legitimate. Data is the value, so this is rejected rather than stored.
    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("session", None, {"phase": Liar(CANARY_TEXT)}, UTC)

    assert CANARY_TEXT not in str(caught.value)
    assert CANARY_TEXT not in repr(caught.value)


def test_a_str_subclass_with_legitimate_data_is_rejected_not_copied():
    class Quiet(str):
        def __str__(self) -> str:
            return SMUGGLED

    # Exact types only: carrying legitimate data does not make a subclass a
    # diagnostics value, so nothing is copied, rendered, or stored from it.
    with pytest.raises(ValueError) as caught:
        safe_activity("session", None, {"phase": Quiet("Mirroring")}, UTC)

    assert_clean_boundary_error(caught.value, ValueError)
    assert SMUGGLED not in str(caught.value)
    assert SMUGGLED not in repr(caught.value)


#: The canary every subclass below hides; no hook on it may reach any output.
SMUGGLED: Final[str] = CANARY_TEXT


class SpoofText(str):
    """A ``str`` subclass that lies through *every* protocol it is asked for.

    ``__str__`` returns ``self`` (F-1's mechanism), so a caller that stores the
    rendering stores the source. Equality and hashing claim ``claim``, so
    container membership looks satisfied while the object carries the canary.
    Iteration, length, indexing and ``encode`` render the canary too, so a
    sanitizer that re-reads the value's characters learns the wrong text.
    """

    _claim: str

    def __new__(cls, claim: str, payload: str = SMUGGLED) -> SpoofText:
        instance = super().__new__(cls, payload)
        instance._claim = claim
        return instance

    def __str__(self) -> str:
        return self

    def __hash__(self) -> int:
        return hash(self._claim)

    def __eq__(self, other: object) -> bool:
        return str(other) == self._claim

    def __repr__(self) -> str:
        return repr(SMUGGLED)

    def __iter__(self) -> Iterator[str]:
        return iter(SMUGGLED)

    def __len__(self) -> int:
        return len(SMUGGLED)

    def __getitem__(self, index: int) -> str:
        return SMUGGLED[index]

    def encode(self, *args: object, **kwargs: object) -> bytes:
        return SMUGGLED.encode()


class RenderSpoof(str):
    """Carries legitimate text as its data, but renders the canary instead.

    This is the axis F-2 names: the wrapped text is a legitimate value, so any
    check against the object itself approves it, while every hook a sanitizer
    might call afterwards reports something else. Only the character *data* is
    the value; nothing here may be read as text.
    """

    _rendering: str

    def __new__(cls, value: str, rendering: str = SMUGGLED) -> RenderSpoof:
        instance = super().__new__(cls, value)
        instance._rendering = rendering
        return instance

    def __str__(self) -> str:
        return self._rendering

    def __repr__(self) -> str:
        return repr(self._rendering)

    def encode(self, *args: object, **kwargs: object) -> bytes:
        return self._rendering.encode()

    def __iter__(self) -> Iterator[str]:
        return iter(self._rendering)

    def __len__(self) -> int:
        return len(self._rendering)

    def __getitem__(self, index: int) -> str:
        return self._rendering[index]


#: The canary an ``ErrorCode`` impostor renders; it must never be echoed.
IMPOSTOR_CANARY: Final[str] = CANARY_TEXT


class CodeImpostor(str):
    """A ``str`` that claims to be an :class:`ErrorCode` and renders a canary.

    Its own data is a catalog key (or nothing at all), so a check that trusts
    the advertised class approves it while every rendering hook reports the
    payload. Only the enum type itself is the code.
    """

    @property
    def __class__(self) -> type[ErrorCode]:  # type: ignore[override]
        return ErrorCode

    def __str__(self) -> str:
        return IMPOSTOR_CANARY

    def __repr__(self) -> str:
        return repr(IMPOSTOR_CANARY)


class RaisingClassStr(str):
    """A string whose advertised class access raises with a secret-bearing error."""

    @property
    def __class__(self) -> type:
        raise RuntimeError(CANARY_TEXT)

    def __str__(self) -> str:
        return CANARY_TEXT

    def __repr__(self) -> str:
        return repr(CANARY_TEXT)


class RaisingClassObject:
    """A non-string whose advertised class access raises with a canary."""

    @property
    def __class__(self) -> type:
        raise RuntimeError(CANARY_TEXT)

    def __str__(self) -> str:
        return CANARY_TEXT

    def __repr__(self) -> str:
        return repr(CANARY_TEXT)


def assert_clean_boundary_error(error: Exception, expected_type: type[Exception]) -> None:
    assert type(error) is expected_type
    assert CANARY_TEXT not in str(error)
    assert CANARY_TEXT not in repr(error)
    assert all(CANARY_TEXT not in repr(argument) for argument in error.args)
    assert error.__cause__ is None
    assert error.__context__ is None


#: One value-taking field per shape, with the legitimate text it accepts.
SHAPED_SAMPLES: Final[tuple[tuple[str, str], ...]] = (
    ("phase", "Mirroring"),
    ("transport", "Streaming"),
    ("display", "Owned"),
    ("audio", "Enabled"),
    ("trust", "Trusted"),
    ("command_name", "kscreen-doctor"),
    ("reservation_stage", "published"),
    ("error_code", "cleanup_timeout"),
    ("connector", "DP-1"),
    ("output_id", "v1"),
    ("resolution", "1920x1080"),
)


def test_raising_class_string_subclasses_are_rejected_at_activity_boundaries():
    # The advertised class is never consulted. Each position an activity value
    # can occupy therefore rejects a subclass with fixed text, whether the
    # probe wraps the code, the identity, a field name, a field value or the
    # timestamp, and none of them is copied into the event.
    receiver = ReceiverId(RaisingClassStr("airplay-device:001122334455"))
    for invoke, expected_type in (
        (
            lambda: safe_activity(
                RaisingClassStr("session"), None, {"phase": "Mirroring"}, UTC
            ),
            ValueError,
        ),
        (lambda: safe_activity("session", receiver, {}, UTC), TypeError),
        (
            lambda: safe_activity(
                "session", None, {RaisingClassStr("phase"): "Mirroring"}, UTC
            ),
            ValueError,
        ),
        (
            lambda: safe_activity(
                "session", None, {"phase": RaisingClassStr("Mirroring")}, UTC
            ),
            ValueError,
        ),
        (
            lambda: safe_activity("session", None, {}, RaisingClassStr(UTC)),
            ValueError,
        ),
    ):
        with pytest.raises(expected_type) as caught:
            invoke()
        assert_clean_boundary_error(caught.value, expected_type)


def test_raising_class_hooks_are_rejected_with_fixed_errors_at_all_boundaries():
    bad = RaisingClassObject()

    class RaisingItems(dict):
        def items(self):
            raise RuntimeError(CANARY_TEXT)

    cases = (
        (lambda: safe_activity(bad, None, {}, UTC), ValueError),
        (lambda: safe_activity("session", bad, {}, UTC), TypeError),
        (lambda: safe_activity("session", None, {bad: "Mirroring"}, UTC), ValueError),
        (lambda: safe_activity("session", None, {"phase": bad}, UTC), ValueError),
        (lambda: safe_activity("session", None, RaisingItems(), UTC), TypeError),
        (lambda: safe_activity("session", None, {}, bad), ValueError),
        (lambda: safe_activity("session", None, bad, UTC), TypeError),
        (lambda: diagnostic(ErrorCode.transport_failed, bad), TypeError),
        (lambda: diagnostic(bad), TypeError),
        (lambda: diagnostic(ErrorCode.dependency_missing, command=bad), ValueError),
    )
    for invoke, expected_type in cases:
        with pytest.raises(expected_type) as caught:
            invoke()
        assert_clean_boundary_error(caught.value, expected_type)


def test_raising_class_hooks_cannot_bypass_numeric_or_boolean_field_shapes():
    for name, value in (
        ("retry_seconds", RaisingClassStr("4")),
        ("retry_seconds", RaisingClassObject()),
        ("explicit_video_only", RaisingClassStr("True")),
        ("explicit_video_only", RaisingClassObject()),
    ):
        with pytest.raises(ValueError) as caught:
            safe_activity("session", None, {name: value}, UTC)
        assert_clean_boundary_error(caught.value, ValueError)


def test_raising_class_hooks_on_outer_activity_types_are_rejected_cleanly():
    bad = RaisingClassObject()
    for invoke, expected_type in (
        (lambda: ActivityLog(capabilities=bad), TypeError),
        (lambda: ActivityLog().emit(bad), TypeError),
    ):
        with pytest.raises(expected_type) as caught:
            invoke()
        assert_clean_boundary_error(caught.value, expected_type)


@pytest.mark.parametrize(("name", "claim"), SHAPED_SAMPLES)
def test_a_fully_spoofed_value_cannot_reach_the_event_entries_or_export(name, claim):
    # F-1: the reproduction from the finding. A self-rendering subclass with
    # spoofed equality and hashing satisfies vocabulary membership while it
    # carries unrelated text. It must be rejected, or normalized to a plain
    # built-in string that cannot contain the canary -- never stored as itself.
    value = SpoofText(claim)

    try:
        event = safe_activity("session", RECEIVER, {name: value}, UTC)
    except ValueError as error:
        assert SMUGGLED not in str(error)
        assert SMUGGLED not in repr(error)
        return

    # A field's own character data is the only text that may be kept, and only
    # as an exact built-in string.
    assert type(event.fields[name]) is str
    assert event.fields[name] == claim
    log = ActivityLog()
    log.emit(event)
    for rendering in (
        repr(event),
        repr(dict(event.fields)),
        repr(log.entries()),
        log.export(),
    ):
        assert SMUGGLED not in rendering


def test_a_self_rendering_phase_value_is_rejected_not_stored():
    # The finding's exact probe: the data is the canary, every check claims
    # 'Mirroring', and ``__str__`` returns ``self``.
    value = SpoofText("Mirroring")
    assert value in _VOCABULARIES["phase"].permitted, "the probe must still satisfy membership"

    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("session", None, {"phase": value}, UTC)

    assert SMUGGLED not in str(caught.value)
    assert SMUGGLED not in repr(caught.value)


def test_a_spoofed_field_name_cannot_dress_as_an_allowlisted_name():
    # The same hook class on the name axis: a ``str`` key whose equality and
    # hash claim 'phase' while it carries and renders the canary.
    spoof = SpoofText("phase")
    assert spoof in ALLOWED_FIELDS, "the probe must still satisfy membership"

    with pytest.raises(ValueError, match="field") as caught:
        safe_activity("session", None, {spoof: "Mirroring"}, UTC)  # type: ignore[dict-item]

    assert SMUGGLED not in str(caught.value)
    assert SMUGGLED not in repr(caught.value)


@pytest.mark.parametrize(("name", "claim"), SHAPED_SAMPLES)
def test_a_differently_rendering_value_cannot_change_after_validation(name, claim):
    # The wrapped text is legitimate but the object renders something else, so
    # nothing may be re-read from the source after the check has run.
    try:
        event = safe_activity("session", RECEIVER, {name: RenderSpoof(claim)}, UTC)
    except ValueError as error:
        assert SMUGGLED not in str(error)
        assert SMUGGLED not in repr(error)
        return

    assert type(event.fields[name]) is str
    assert SMUGGLED not in repr(event)
    log = ActivityLog()
    log.emit(event)
    for rendering in (repr(log.entries()), log.export()):
        assert SMUGGLED not in rendering


def test_a_spoofed_receiver_identity_cannot_render_the_canary():
    # A canonical identity subclass is copied from its actual string data; its
    # rendering hooks cannot alter the retained receiver correlation value.
    spoof = ReceiverId(RenderSpoof("airplay-device:001122334455"))

    try:
        event = safe_activity("session", spoof, {}, UTC)
    except (TypeError, ValueError) as error:
        assert SMUGGLED not in str(error)
        assert SMUGGLED not in repr(error)
        return

    assert type(event.receiver_id) is str
    log = ActivityLog()
    log.emit(event)
    for rendering in (repr(event), repr(log.entries()), log.export()):
        assert SMUGGLED not in rendering


def test_a_utc_subclass_cannot_be_parsed_through_its_own_hooks():
    # ``datetime.fromisoformat`` accepts ``str`` subclasses and may consult their
    # hooks, so a subclass is never handed to it: the boundary requires the exact
    # ``str`` type and rejects the probe before any parsing happens.
    spoof = RenderSpoof("2026-10-17T02:00:00+02:00")

    with pytest.raises(ValueError) as caught:
        safe_activity("session", None, {}, spoof)

    assert_clean_boundary_error(caught.value, ValueError)
    assert SMUGGLED not in str(caught.value)


def test_a_capability_environment_subclass_cannot_render_the_canary():
    # The export sanitizes this text the same way as an activity value, so a
    # subclass is rejected rather than rendered into the document.
    log = ActivityLog(
        capabilities=CapabilityReport(
            environment=RenderSpoof("KDE Plasma 6 Wayland"),
            discovery=True,
            mirror=True,
            virtual_display=False,
            audio=True,
            remembered_pairing=False,
            additional_virtual_outputs=False,
            diagnostics=(),
        ),
    )

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert SMUGGLED not in str(caught.value)


@pytest.mark.parametrize(("name", "value"), FIELD_SAMPLES.items())
def test_allowlisted_fields_reach_the_event(name, value):
    event = safe_activity("session", RECEIVER, {name: value}, UTC)

    assert event.fields == {name: value}
    assert event.code == "session"
    assert event.receiver_id == RECEIVER
    assert event.utc == UTC
    assert event.message.strip()


def test_secret_field_cannot_enter_activity():
    with pytest.raises(ValueError, match="field"):
        safe_activity("pairing", None, {"password": "canary-8496"}, "2026-10-17T00:00:00Z")


@pytest.mark.parametrize("name", SECRET_FIELD_NAMES)
def test_every_secret_field_name_is_rejected(name):
    with pytest.raises(ValueError, match="field") as caught:
        safe_activity("session", None, {name: CANARY}, UTC)

    assert CANARY not in str(caught.value)


@pytest.mark.parametrize("value", SECRET_VALUES)
def test_transient_private_values_cannot_enter_activity(value):
    with pytest.raises(ValueError, match="private value") as caught:
        safe_activity("pairing", None, {"phase": value}, UTC)

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


@pytest.mark.parametrize("value", [None, 1.5, b"canary-8496", ["canary-8496"], {"canary-8496": 1}])
def test_raw_backend_payloads_cannot_enter_activity(value):
    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("process.failed", None, {"command_name": value}, UTC)

    assert CANARY not in str(caught.value)


class BaseExceptionItems(dict):
    """A mapping subclass whose overridden ``items`` leaks a BaseException."""

    def __init__(self) -> None:
        super().__init__()
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        raise BaseException(CANARY_TEXT)


class HostileItemsIterator:
    def __iter__(self):
        return self

    def __next__(self):
        raise BaseException(CANARY_TEXT)


class HostileIteratorItems(dict):
    """A mapping subclass returning an iterator controlled by the caller."""

    def __init__(self) -> None:
        super().__init__()
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        return HostileItemsIterator()


class MalformedFieldItem:
    """An item whose unpacking hook raises caller-controlled payload text."""

    def __iter__(self):
        raise BaseException(CANARY_TEXT)


class MalformedItems(dict):
    """A mapping subclass returning a malformed item object."""

    def __init__(self) -> None:
        super().__init__()
        self.items_calls = 0

    def items(self):
        self.items_calls += 1
        return [MalformedFieldItem()]


@pytest.mark.parametrize(
    "fields_type",
    [BaseExceptionItems, HostileIteratorItems, MalformedItems],
    ids=["overridden-items", "hostile-items-iterator", "malformed-item"],
)
def test_mapping_subclasses_are_rejected_before_caller_hooks_run(fields_type):
    fields = fields_type()

    with pytest.raises(TypeError) as caught:
        safe_activity("session", None, fields, UTC)  # type: ignore[arg-type]

    assert_clean_boundary_error(caught.value, TypeError)
    assert fields.items_calls == 0


def test_fields_must_be_a_mapping():
    with pytest.raises(TypeError, match="mapping"):
        safe_activity("session", None, [("phase", "Mirroring")], UTC)  # type: ignore[arg-type]


def test_control_characters_in_receiver_identity_are_rejected():
    hostile = ReceiverId("airplay-device:AirPlay\nTV\x00\x1b[31m")
    with pytest.raises(ValueError) as caught:
        safe_activity("display.published", hostile, {}, UTC)
    assert_clean_boundary_error(caught.value, ValueError)


def test_long_receiver_identity_is_rejected_instead_of_truncated():
    with pytest.raises(ValueError) as caught:
        safe_activity("display.published", ReceiverId("airplay-device:" + "a" * 5000), {}, UTC)
    assert_clean_boundary_error(caught.value, ValueError)


#: A complete escape this module can emit, so a leftover backslash is a fragment.
COMPLETE_ESCAPE = re.compile(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}")


@pytest.mark.parametrize("padding", [MAX_FIELD_TEXT - 6, MAX_FIELD_TEXT - 5, MAX_FIELD_TEXT - 4, MAX_FIELD_TEXT - 3])
def test_bounding_never_splits_an_escape_sequence(padding):
    value = "A" * padding + "\n" * 6

    rendered = _bounded(_escape(value))

    assert isinstance(rendered, str)
    assert len(rendered) <= MAX_FIELD_TEXT
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")
    # A truncated ``\u00`` fragment is a dangling backslash once the complete
    # escapes are removed; the source contains none of its own.
    assert "\\" not in COMPLETE_ESCAPE.sub("", rendered)


def test_bounding_keeps_a_whole_escape_at_the_boundary():
    value = "A" * (MAX_FIELD_TEXT - 7) + "\n\n"

    rendered = _bounded(_escape(value))

    assert isinstance(rendered, str)
    assert len(rendered) == MAX_FIELD_TEXT
    assert rendered.endswith("\\u000a\N{HORIZONTAL ELLIPSIS}")


@pytest.mark.parametrize("code", ["", "PAIRING", "pairing completed", "pairing\npassword=x", "p" * 80, 7])
def test_code_must_be_a_bounded_token(code):
    with pytest.raises(ValueError, match="code"):
        safe_activity(code, None, {}, UTC)  # type: ignore[arg-type]


@pytest.mark.parametrize("utc", ["yesterday", "2026-10-17T00:00:00", "2026-13-45T00:00:00Z", 12])
def test_utc_must_be_an_absolute_timestamp(utc):
    with pytest.raises(ValueError, match="utc"):
        safe_activity("session", None, {}, utc)  # type: ignore[arg-type]


def test_invalid_utc_rejection_does_not_retain_the_original_text():
    with pytest.raises(ValueError) as caught:
        safe_activity("session", None, {}, CANARY_TEXT)
    assert_clean_boundary_error(caught.value, ValueError)


def test_utc_normalization_overflow_uses_a_fixed_boundary_error():
    with pytest.raises(ValueError) as caught:
        safe_activity("session", None, {}, "0001-01-01T00:00:00+23:59")
    assert_clean_boundary_error(caught.value, ValueError)


def test_utc_is_normalized_to_canonical_utc():
    event = safe_activity("session", None, {}, "2026-10-17T02:00:00+02:00")

    assert event.utc == UTC


@pytest.mark.parametrize("receiver", [None, RECEIVER])
def test_receiver_identity_is_preserved(receiver):
    assert safe_activity("session", receiver, {}, UTC).receiver_id == receiver


def test_receiver_identity_must_be_canonical():
    bad = ReceiverId("airplay-device:" + CANARY_TEXT)
    log = ActivityLog()
    with pytest.raises(ValueError) as caught:
        log.emit(safe_activity("session", bad, {}, UTC))
    assert_clean_boundary_error(caught.value, ValueError)
    assert log.entries() == ()
    assert CANARY_TEXT not in repr(log.entries())
    assert CANARY_TEXT not in log.export()


def test_activity_receiver_id_is_preserved_exactly_when_canonical():
    canonical = ReceiverId("airplay-device:abcdef0123456789")
    event = safe_activity("session", canonical, {}, UTC)
    log = ActivityLog()
    log.emit(event)

    assert event.receiver_id == canonical
    assert type(event.receiver_id) is str
    assert event.receiver_id == "airplay-device:abcdef0123456789"
    assert CANARY_TEXT not in repr(event)
    assert log.entries()[0].receiver_id == canonical
    assert json.loads(log.export())["activity"][0]["receiver_id"] == canonical
    assert CANARY_TEXT not in repr(log.entries())
    assert CANARY_TEXT not in log.export()


def test_receiver_identity_must_be_a_identifier():
    with pytest.raises(TypeError, match="receiver"):
        safe_activity("session", 12345, {}, UTC)  # type: ignore[arg-type]


def test_receiver_identity_cannot_carry_controls_or_unbounded_text():
    hostile = ReceiverId("airplay-device:" + "\n\x1b[31m\x00" + "A" * 430)

    with pytest.raises(ValueError) as caught:
        safe_activity("session", hostile, {}, UTC)

    assert_clean_boundary_error(caught.value, ValueError)


def test_activity_messages_are_fixed_per_code():
    first = safe_activity("pairing.completed", None, {}, UTC)
    second = safe_activity("pairing.completed", None, {}, UTC)

    assert first.message == second.message
    assert first.message.strip()
    assert CANARY not in first.message


@pytest.mark.parametrize("code", ["future.event", CANARY_TEXT])
def test_unregistered_code_is_rejected_without_echoing_it(code):
    with pytest.raises(ValueError, match="code") as caught:
        safe_activity(code, None, {}, UTC)

    assert code not in str(caught.value)
    assert code not in repr(caught.value)


def test_unregistered_code_cannot_reach_the_log_or_export():
    log = ActivityLog()
    smuggled = ActivityEvent(
        utc=UTC, code=CANARY_TEXT, receiver_id=None, message="fixed copy", fields={},
    )

    with pytest.raises(ValueError, match="code") as caught:
        log.emit(smuggled)

    assert CANARY_TEXT not in str(caught.value)
    assert CANARY_TEXT not in repr(caught.value)
    assert log.entries() == ()
    assert CANARY_TEXT not in log.export()


def test_a_registered_code_that_renders_the_canary_cannot_leak_it():
    # F-2: the code's own data is the registered 'session', so every check that
    # re-reads the object from the outside approves it while ``__str__`` renders
    # the canary. Exact types only: the subclass is rejected before its data,
    # its rendering, or its hooks are consulted, and no failure carries it.
    with pytest.raises(ValueError, match="code") as caught:
        safe_activity(RenderSpoof("session"), None, {}, UTC)

    assert_clean_boundary_error(caught.value, ValueError)
    assert SMUGGLED not in str(caught.value)
    assert SMUGGLED not in repr(caught.value)


def test_a_code_with_spoofed_membership_cannot_reach_the_log_or_export():
    code = SpoofText("session")
    assert code in ACTIVITY_CODES, "the probe must still satisfy membership"

    with pytest.raises(ValueError, match="code") as caught:
        safe_activity(code, None, {}, UTC)

    assert SMUGGLED not in str(caught.value)
    assert SMUGGLED not in repr(caught.value)

    log = ActivityLog()
    smuggled = ActivityEvent(
        utc=UTC, code=code, receiver_id=None, message="fixed copy", fields={},
    )
    with pytest.raises(ValueError, match="code") as revalidated:
        log.emit(smuggled)

    assert SMUGGLED not in str(revalidated.value)
    assert SMUGGLED not in repr(revalidated.value)
    assert log.entries() == ()
    assert SMUGGLED not in log.export()


@pytest.mark.parametrize("code", sorted(ACTIVITY_CODES))
def test_every_registered_code_has_fixed_rendered_copy(code):
    message = safe_activity(code, None, {}, UTC).message

    assert message.strip()
    assert message == safe_activity(code, None, {}, UTC).message
    assert "{" not in message and "}" not in message
    assert CANARY not in message


def test_event_fields_are_immutable():
    event = safe_activity("session", None, {"phase": "Mirroring"}, UTC)

    with pytest.raises(TypeError):
        event.fields["phase"] = "Failed"  # type: ignore[index]


# ---------------------------------------------------------------- activity log


def test_log_keeps_arrival_order_within_capacity():
    log = ActivityLog(max_entries=3)
    for index in range(5):
        log.emit(safe_activity("session", None, {"retry_seconds": index}, UTC))

    assert [entry.fields["retry_seconds"] for entry in log.entries()] == [2, 3, 4]
    assert json.loads(log.export())["counts"]["entries"] == 3


@pytest.mark.parametrize("bad", [0, -1])
def test_log_rejects_an_empty_capacity(bad):
    with pytest.raises(ValueError, match="max_entries"):
        ActivityLog(max_entries=bad)


@pytest.mark.parametrize("bad", [True, 5.0, "10"])
def test_log_rejects_a_non_integer_capacity(bad):
    with pytest.raises(TypeError, match="max_entries"):
        ActivityLog(max_entries=bad)  # type: ignore[arg-type]


def test_log_revalidates_hand_built_events():
    log = ActivityLog()
    smuggled = ActivityEvent(
        utc=UTC, code="pairing", receiver_id=None, message=CANARY, fields={},
    )
    log.emit(smuggled)

    entry = log.entries()[0]
    assert entry.message != CANARY
    assert CANARY not in repr(entry)
    assert CANARY not in log.export()

    with pytest.raises(ValueError, match="field"):
        log.emit(
            ActivityEvent(
                utc=UTC, code="pairing", receiver_id=None,
                message="Pairing", fields={"password": CANARY},
            ),
        )
    assert len(log.entries()) == 1


def test_log_rejects_a_foreign_payload():
    log = ActivityLog()

    with pytest.raises(TypeError, match="ActivityEvent"):
        log.emit({"code": "pairing"})  # type: ignore[arg-type]


def test_subscribers_receive_events_until_removed():
    log = ActivityLog()
    seen: list[ActivityEvent] = []
    subscription = log.subscribe(seen.append)

    log.emit(safe_activity("pairing", None, {}, UTC))
    subscription.close()
    subscription.close()
    log.emit(safe_activity("pairing", None, {}, UTC))

    assert [event.code for event in seen] == ["pairing"]
    assert len(log.entries()) == 2


def test_failing_subscriber_does_not_block_the_log():
    log = ActivityLog()
    seen: list[ActivityEvent] = []

    def explode(event: ActivityEvent) -> None:
        raise RuntimeError(CANARY)

    log.subscribe(explode)
    log.subscribe(seen.append)
    log.emit(safe_activity("session", None, {}, UTC))

    assert len(seen) == 1
    assert len(log.entries()) == 1
    assert CANARY not in log.export()


def test_subscribe_requires_a_callable():
    with pytest.raises(TypeError, match="callback"):
        ActivityLog().subscribe("not-callable")  # type: ignore[arg-type]


# --------------------------------------------------------------------- export


class RaisingCapabilityItem:
    """A capability-diagnostic item whose ``code`` hook raises the canary.

    It is not a :class:`Diagnostic`, so no part of it may be inspected. The
    counter proves the export decides on the item's actual type before it reads
    any attribute, rather than trusting what the object offers to answer.
    """

    def __init__(self) -> None:
        self.code_reads = 0

    @property
    def code(self) -> ErrorCode:
        self.code_reads += 1
        raise RuntimeError(CANARY_TEXT)

    def __repr__(self) -> str:
        return repr(CANARY_TEXT)


class RaisingCodeDiagnostic(Diagnostic):
    """A ``Diagnostic`` subclass whose ``code`` access raises the canary.

    Only the exact domain type may pass the collection check, so a subclass
    cannot get its ``code`` read at all, however it behaves when asked.
    """

    def __getattribute__(self, name: str) -> object:
        if name == "code":
            raise RuntimeError(CANARY_TEXT)
        return object.__getattribute__(self, name)

    def __repr__(self) -> str:
        return "<raising-code diagnostic>"


class IteratingDiagnostics(tuple):
    """A ``tuple`` whose iteration is caller-controlled, so it is not a tuple."""

    def __iter__(self) -> Iterator[object]:
        yield RaisingCapabilityItem()


def capability_report() -> CapabilityReport:
    return CapabilityReport(
        environment="KDE Plasma 6 Wayland",
        discovery=True,
        mirror=True,
        virtual_display=False,
        audio=True,
        remembered_pairing=False,
        additional_virtual_outputs=False,
        diagnostics=(
            Diagnostic(
                code=ErrorCode.dependency_missing,
                message=f"backend said {CANARY}",
                recovery=(RecoveryAction.retry,),
            ),
        ),
    )


@pytest.mark.parametrize("environment", [CANARY_TEXT, "raising-subclass"])
def test_capability_environment_uses_a_fixed_unknown_fallback_for_unrecognized_text(environment):
    if environment == "raising-subclass":
        # A subclass is not an environment string at all, so the export rejects
        # it with fixed text rather than reading it through its own hooks.
        log = ActivityLog(
            capabilities=replace(
                capability_report(), environment=RaisingClassStr(CANARY_TEXT)
            ),
        )
        with pytest.raises(TypeError) as caught:
            log.export()
        assert_clean_boundary_error(caught.value, TypeError)
        return
    report = replace(capability_report(), environment=environment)
    log = ActivityLog(capabilities=report)

    exported = log.export()

    assert json.loads(exported)["capabilities"]["environment"] == "Unknown"
    assert CANARY_TEXT not in exported
    assert CANARY_TEXT not in repr(log.entries())


def test_capability_environment_rejects_non_string_data_cleanly():
    report = replace(capability_report(), environment=RaisingClassObject())
    log = ActivityLog(capabilities=report)

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


@pytest.mark.parametrize(
    "field",
    [
        "discovery",
        "mirror",
        "virtual_display",
        "audio",
        "remembered_pairing",
        "additional_virtual_outputs",
    ],
)
@pytest.mark.parametrize(
    "value",
    [CANARY_TEXT, 1, "raising-subclass", "raising-object"],
)
def test_capability_export_rejects_unvalidated_flag_values(field, value):
    if value == "raising-subclass":
        value = RaisingClassStr(CANARY_TEXT)
    elif value == "raising-object":
        value = RaisingClassObject()
    report = replace(capability_report(), **{field: value})
    log = ActivityLog(capabilities=report)

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


def test_capability_export_rejects_an_item_whose_code_hook_raises():
    hostile = RaisingCapabilityItem()
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=(hostile,)))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert hostile.code_reads == 0, "the item's type is decided before any attribute"
    assert log.entries() == ()


def test_capability_export_rejects_a_diagnostic_subclass_before_reading_it():
    # The item is shaped like a domain record, but its actual type is not the
    # frozen one, so even a well-formed construction may not reach the export.
    item = RaisingCodeDiagnostic(
        code=ErrorCode.dependency_missing,
        message=f"backend said {CANARY_TEXT}",
        recovery=(RecoveryAction.retry,),
    )
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=(item,)))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


@pytest.mark.parametrize(
    "code",
    [CANARY_TEXT, "dependency_missing", None, 42, ErrorCode.dependency_missing.value],
)
def test_capability_export_rejects_a_code_that_is_not_an_error_code(code):
    item = Diagnostic(code=code, message="fixed copy", recovery=())  # type: ignore[arg-type]
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=(item,)))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


def test_a_code_impostor_cannot_reach_the_capability_export():
    # ``isinstance`` honours the advertised class, so the impostor claims to be
    # an ``ErrorCode`` while holding a catalog-looking key as its own data.
    impostor = CodeImpostor("dependency_missing")
    assert isinstance(impostor, ErrorCode), "the probe must still fool isinstance"
    item = Diagnostic(code=impostor, message="fixed copy", recovery=())  # type: ignore[arg-type]
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=(item,)))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert IMPOSTOR_CANARY not in repr(caught.value)


def test_capability_export_rejects_a_mutable_diagnostic_collection():
    genuine = Diagnostic(
        code=ErrorCode.audio_denied,
        message="fixed copy",
        recovery=(RecoveryAction.video_only,),
    )
    log = ActivityLog(
        capabilities=replace(capability_report(), diagnostics=[genuine]),  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    # Deterministic: a second export fails the same way and renders no codes.
    with pytest.raises(TypeError) as again:
        log.export()
    assert str(again.value) == str(caught.value)


def test_capability_export_rejects_a_collection_that_controls_its_own_iteration():
    # The collection is not consulted at all: only the exact frozen tuple type
    # is a capability collection, so its ``__iter__`` cannot decide what runs.
    hostile = IteratingDiagnostics()
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=hostile))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


@pytest.mark.parametrize(
    "collection",
    [None, "diagnostics", frozenset(), {"diagnostics": ()}, 7],
)
def test_capability_export_rejects_a_non_tuple_diagnostic_collection(collection):
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=collection))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)


def test_genuine_diagnostics_still_export_their_fixed_codes():
    report = CapabilityReport(
        environment="KDE Plasma 6 Wayland",
        discovery=True,
        mirror=True,
        virtual_display=True,
        audio=True,
        remembered_pairing=True,
        additional_virtual_outputs=True,
        diagnostics=(
            Diagnostic(
                code=ErrorCode.dependency_missing,
                message=f"backend said {CANARY_TEXT}",
                recovery=(RecoveryAction.retry,),
            ),
            Diagnostic(
                code=ErrorCode.audio_unavailable,
                message=CANARY_TEXT,
                recovery=(RecoveryAction.video_only,),
            ),
            Diagnostic(
                code=ErrorCode.dependency_missing,
                message="a duplicate leaf",
                recovery=(),
            ),
        ),
    )

    exported = ActivityLog(capabilities=report).export()

    capabilities = json.loads(exported)["capabilities"]
    # The seven leaves are unchanged, and codes come from the frozen vocabulary.
    assert capabilities == {
        "environment": "KDE Plasma 6 Wayland",
        "discovery": True,
        "mirror": True,
        "virtual_display": True,
        "audio": True,
        "remembered_pairing": True,
        "additional_virtual_outputs": True,
        "diagnostic_codes": ["audio_unavailable", "dependency_missing"],
    }
    vocabulary = {member.value for member in ErrorCode}
    assert all(code in vocabulary for code in capabilities["diagnostic_codes"])
    assert CANARY_TEXT not in exported


def test_export_includes_counts_and_capabilities_only():
    log = ActivityLog(max_entries=8, capabilities=capability_report())
    log.emit(safe_activity("transport.ready", RECEIVER, {"transport": "Streaming"}, UTC))
    log.emit(safe_activity("pairing", RECEIVER, {"trust": "Trusted"}, UTC))

    document = json.loads(log.export())

    assert set(document) == {"schema", "counts", "capabilities", "activity"}
    assert document["counts"] == {
        "entries": 2,
        "capacity": 8,
        "by_code": {"pairing": 1, "transport.ready": 1},
    }
    assert document["capabilities"] == {
        "environment": "KDE Plasma 6 Wayland",
        "discovery": True,
        "mirror": True,
        "virtual_display": False,
        "audio": True,
        "remembered_pairing": False,
        "additional_virtual_outputs": False,
        "diagnostic_codes": ["dependency_missing"],
    }
    assert CANARY not in log.export()


def test_export_emits_no_runtime_records():
    log = ActivityLog(capabilities=capability_report())
    log.emit(safe_activity("display.published", RECEIVER, {"output_id": "v1"}, UTC))

    for entry in json.loads(log.export())["activity"]:
        assert set(entry) == {"utc", "code", "receiver_id", "message", "fields"}
        assert isinstance(entry["fields"], dict)


def test_export_is_deterministic_json():
    log = ActivityLog(max_entries=4)
    log.emit(safe_activity("session", None, {"phase": "Mirroring"}, UTC))
    log.emit(safe_activity("display.removed", None, {"display": "Owned"}, UTC))

    first = log.export()

    assert first == log.export()
    assert json.loads(first)["activity"][0]["utc"] == UTC
    assert "\x1b" not in first


def test_activity_log_feeds_the_frozen_sink():
    log = ActivityLog()
    sink = MemoryActivity()
    identifiers = SequenceIds(seed=1)
    receiver = ReceiverId(f"airplay-device:{identifiers.new()}")

    event = safe_activity("session.starting", receiver, {"phase": "Connecting"}, UTC)
    log.emit(event)
    sink.emit(event)

    assert sink.events == list(log.entries())


# ------------------------------------------- hook-free normalization boundary

#: The canary every hostile hook below raises or renders; it must never surface.
HOOK_CANARY: Final[str] = "hook-canary-3821"


class HookCounter:
    """The shared instrument: every hostile hook below records into one of these.

    The counter is owned by the test, not by any value it inspects, so a nonzero
    count proves the diagnostics package ran caller code inside its boundary.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def hit(self, name: str) -> None:
        self.calls.append(name)

    def __len__(self) -> int:
        return len(self.calls)


class HookedMeta(type):
    """A metaclass whose ``__mro__`` access is itself the leak."""

    @property
    def __mro__(cls) -> tuple[type, ...]:  # type: ignore[override]
        _LIVE_COUNTER.hit("metaclass.__mro__")
        raise BaseException(HOOK_CANARY)


class MetaclassTrap(str, metaclass=HookedMeta):
    """A ``str`` subclass whose own type cannot be described without that hook."""


class HookedItemsDict(dict):
    """A mapping subclass whose every container protocol raises and counts."""

    def __init__(self, counter: HookCounter) -> None:
        super().__init__({"phase": "Mirroring"})
        self._counter = counter

    def items(self):  # type: ignore[override]
        self._counter.hit("dict.items")
        raise BaseException(HOOK_CANARY)

    def keys(self):  # type: ignore[override]
        self._counter.hit("dict.keys")
        raise BaseException(HOOK_CANARY)

    def __iter__(self):  # type: ignore[override]
        self._counter.hit("dict.__iter__")
        raise BaseException(HOOK_CANARY)

    def __getitem__(self, key):  # type: ignore[override]
        self._counter.hit("dict.__getitem__")
        raise BaseException(HOOK_CANARY)

    def copy(self):  # type: ignore[override]
        self._counter.hit("dict.copy")
        raise BaseException(HOOK_CANARY)

    def __len__(self) -> int:
        self._counter.hit("dict.__len__")
        raise BaseException(HOOK_CANARY)


class HookedSelfStr(str):
    """A ``str`` subclass that returns itself, spoofs equality, and counts."""

    def __new__(cls, claim: str, counter: HookCounter) -> "HookedSelfStr":
        instance = super().__new__(cls, HOOK_CANARY)
        instance._claim = claim
        instance._counter = counter
        return instance

    def __str__(self) -> str:
        self._counter.hit("str.__str__")
        return self

    def __eq__(self, other: object) -> bool:
        self._counter.hit("str.__eq__")
        return True

    def __hash__(self) -> int:
        self._counter.hit("str.__hash__")
        return hash(self._claim)

    def __repr__(self) -> str:
        self._counter.hit("str.__repr__")
        return repr(HOOK_CANARY)

    def encode(self, *args: object, **kwargs: object) -> bytes:
        self._counter.hit("str.encode")
        return HOOK_CANARY.encode()


class HookedClassStr(str):
    """A ``str`` subclass whose advertised class access raises and counts."""

    def __new__(cls, counter: HookCounter) -> "HookedClassStr":
        instance = super().__new__(cls, "Mirroring")
        instance._counter = counter
        return instance

    @property
    def __class__(self) -> type:  # type: ignore[override]
        self._counter.hit("__class__")
        raise BaseException(HOOK_CANARY)

    def __str__(self) -> str:
        self._counter.hit("str.__str__")
        raise BaseException(HOOK_CANARY)

    def __repr__(self) -> str:
        self._counter.hit("str.__repr__")
        raise BaseException(HOOK_CANARY)


#: The counter the metaclass trap and shared helper report into. A probe always
#: rebinds it just before use, so a stale binding cannot hide a leak.
_LIVE_COUNTER: HookCounter = HookCounter()


def hook_counter() -> HookCounter:
    global _LIVE_COUNTER
    _LIVE_COUNTER = HookCounter()
    return _LIVE_COUNTER


def forged_error_code() -> object:
    """An exact-type ``ErrorCode`` nonmember, which compares like the member.

    ``str.__new__`` builds an instance whose type *is* the enum, so a type check,
    equality, hashing and dictionary lookup all accept it, while the enumeration
    never registered it and ``is`` never matches it.
    """
    forged = str.__new__(ErrorCode, ErrorCode.transport_failed.value)
    assert type(forged) is ErrorCode, "the probe must carry the exact enum type"
    assert forged == ErrorCode.transport_failed, "the probe must compare equal"
    assert hash(forged) == hash(ErrorCode.transport_failed), "the probe must hash equal"
    assert forged is not ErrorCode.transport_failed, "the probe must not be the member"
    return forged


def hostile_probes(counter: HookCounter) -> tuple[tuple[str, object], ...]:
    """One instance of every hostile shape the boundary must not inspect."""
    return (
        ("forged-enum", forged_error_code()),
        ("hooked-dict", HookedItemsDict(counter)),
        ("proxy-over-hooked-dict", MappingProxyType(HookedItemsDict(counter))),
        ("metaclass-mro", MetaclassTrap("Mirroring")),
        ("self-rendering-str", HookedSelfStr("Mirroring", counter)),
        ("raising-class-str", HookedClassStr(counter)),
    )


#: Every public entry point a hostile value can cross, with the argument slot it
#: occupies. The positive matrix below proves the same slots still accept the
#: legitimate values the design allows.
BOUNDARY_ENTRY_POINTS: Final[tuple[tuple[str, Callable[[object], object]], ...]] = (
    ("safe_activity.code", lambda probe: safe_activity(probe, None, {}, UTC)),
    ("safe_activity.receiver_id", lambda probe: safe_activity("session", probe, {}, UTC)),
    ("safe_activity.fields", lambda probe: safe_activity("session", None, probe, UTC)),
    ("safe_activity.utc", lambda probe: safe_activity("session", None, {}, probe)),
    ("diagnostic.code", lambda probe: diagnostic(probe)),
    ("diagnostic.receiver_id", lambda probe: diagnostic(ErrorCode.transport_failed, probe)),
    (
        "diagnostic.command",
        lambda probe: diagnostic(ErrorCode.dependency_missing, command=probe),
    ),
    ("ActivityLog.emit", lambda probe: ActivityLog().emit(probe)),
    ("ActivityLog.capabilities", lambda probe: ActivityLog(capabilities=probe)),
    (
        "ActivityLog.emit.fields",
        lambda probe: ActivityLog().emit(event_with_fields(probe)),
    ),
)


def event_with_fields(fields: object) -> ActivityEvent:
    """A hand-built event whose ``fields`` really holds ``fields``.

    ``ActivityEvent.__post_init__`` copies its mapping, and that copy is domain
    code this lane cannot change, so the probe is installed afterwards. The
    emission path is what is under test: it receives the hostile mapping exactly
    as a caller could hand it one.
    """
    event = ActivityEvent(
        utc=UTC, code="session", receiver_id=None, message="fixed copy", fields={},
    )
    object.__setattr__(event, "fields", fields)
    return event


def assert_scrubbed(failure: Exception, counter: HookCounter) -> None:
    """The rejection is fixed, unhooked, and carries nothing the probe supplied."""
    assert len(counter) == 0, f"caller hooks ran at the boundary: {counter.calls}"
    assert type(failure) is ValueError or type(failure) is TypeError
    assert HOOK_CANARY not in str(failure)
    assert HOOK_CANARY not in repr(failure)
    assert all(HOOK_CANARY not in repr(argument) for argument in failure.args)
    assert failure.__cause__ is None, "the rejection must not chain its cause"
    assert failure.__context__ is None, "the rejection must not chain its context"


@pytest.mark.parametrize(
    ("entry_point", "invoke"),
    BOUNDARY_ENTRY_POINTS,
    ids=[name for name, _ in BOUNDARY_ENTRY_POINTS],
)
@pytest.mark.parametrize(
    "probe_name",
    [name for name, _ in hostile_probes(HookCounter())],
)
def test_no_public_entry_point_runs_a_caller_hook(entry_point, invoke, probe_name):
    # The point of one shared boundary: a class of defect is closed for every
    # entry point at once instead of one instance at a time. The counter proves
    # no caller hook ran. Either outcome is a pass -- a fixed rejection, or
    # acceptance of a value the entry point never had to inspect -- but a
    # rejection may not render the probe and an accepted value may not carry it.
    counter = hook_counter()
    probes = dict(hostile_probes(counter))
    probe = probes[probe_name]

    try:
        result = invoke(probe)
    except (ValueError, TypeError) as failure:
        assert_scrubbed(failure, counter)
        return

    assert len(counter) == 0, f"caller hooks ran at {entry_point}: {counter.calls}"
    assert HOOK_CANARY not in repr(result)


def test_a_forged_exact_type_enum_nonmember_is_rejected_by_identity():
    # Type and equality both accept the forgery, so only ``is`` against a
    # captured member can reject it.
    forged = forged_error_code()

    with pytest.raises(TypeError) as caught:
        diagnostic(forged)  # type: ignore[arg-type]

    assert type(caught.value) is TypeError
    assert str(caught.value) == (
        "code must be an ErrorCode from the diagnostics catalog; "
        "arbitrary error text is not a diagnostic"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_forged_exact_type_enum_nonmember_cannot_reach_the_capability_export():
    # The same forgery on the export path: the enum's captured text is what is
    # exported, so a nonmember contributes no code at all.
    forged = forged_error_code()
    item = Diagnostic(code=forged, message="fixed copy", recovery=())  # type: ignore[arg-type]
    log = ActivityLog(capabilities=replace(capability_report(), diagnostics=(item,)))

    with pytest.raises(TypeError) as caught:
        log.export()

    assert_clean_boundary_error(caught.value, TypeError)
    assert log.entries() == ()


def test_a_mapping_proxy_over_an_exact_dict_is_accepted():
    # The positive half of the proxy rule: a proxy over an *exact* dict is a
    # legitimate immutable mapping and must keep working.
    fields = MappingProxyType({"phase": "Mirroring", "retry_seconds": 4})

    event = safe_activity("session", None, fields, UTC)

    assert event.fields == {"phase": "Mirroring", "retry_seconds": 4}
    assert type(event.fields["phase"]) is str
    assert type(event.fields["retry_seconds"]) is int


def test_a_mapping_proxy_over_a_dict_subclass_is_rejected_before_its_hooks_run():
    # A proxy hides the subclass from a plain type check, so the wrapped
    # object's exact type is decided before anything is read from it. The counter
    # proves the wrapped subclass's hooks never ran.
    counter = hook_counter()
    fields = MappingProxyType(HookedItemsDict(counter))

    with pytest.raises(TypeError) as caught:
        safe_activity("session", None, fields, UTC)  # type: ignore[arg-type]

    assert_scrubbed(caught.value, counter)
    assert str(caught.value) == "fields must be a mapping of allowlisted names to values"


def test_a_metaclass_mro_hook_is_never_consulted():
    # Reading ``__mro__`` through the type would run this metaclass property, so
    # the boundary must decide on ``type(value)`` alone.
    counter = hook_counter()
    probe = MetaclassTrap("Mirroring")

    for invoke in (
        lambda: safe_activity("session", None, {"phase": probe}, UTC),
        lambda: safe_activity(probe, None, {}, UTC),
        lambda: diagnostic(ErrorCode.dependency_missing, command=probe),
    ):
        with pytest.raises((TypeError, ValueError)) as caught:
            invoke()
        assert_scrubbed(caught.value, counter)


def test_ordinary_values_still_work_through_every_entry_point():
    # The positive matrix: the boundary tightens what is rejected without
    # narrowing what the design already allows.
    assert safe_activity("session", RECEIVER, {}, UTC).receiver_id == RECEIVER
    assert safe_activity("session", None, {"phase": "Mirroring"}, UTC).fields == {
        "phase": "Mirroring"
    }
    assert safe_activity("session", None, {}, "2026-10-17T02:00:00+02:00").utc == UTC
    assert diagnostic(ErrorCode.transport_failed, RECEIVER).receiver_id == RECEIVER
    rendered = diagnostic(ErrorCode.dependency_missing, command="doubletake").message
    assert "'doubletake'" in rendered

    log = ActivityLog()
    log.emit(safe_activity("session", None, {"retry_seconds": 4}, UTC))
    assert len(log.entries()) == 1
    assert json.loads(log.export())["counts"]["entries"] == 1


@pytest.mark.parametrize("member", list(ErrorCode))
def test_every_genuine_error_code_member_is_still_accepted_by_identity(member):
    result = diagnostic(member)

    assert result.code is member
    assert result.message == ENTRIES[member].message


def test_every_genuine_error_code_member_still_export_its_captured_text():
    report = replace(
        capability_report(),
        diagnostics=tuple(
            Diagnostic(code=member, message="fixed copy", recovery=()) for member in ErrorCode
        ),
    )

    exported = json.loads(ActivityLog(capabilities=report).export())

    assert exported["capabilities"]["diagnostic_codes"] == sorted(
        member.value for member in ErrorCode
    )


def test_a_base_exception_from_trusted_state_becomes_a_fixed_rejection():
    # The wrapper's own rule, on a path where the boundary does read a trusted
    # domain record: a hostile subclass of that record raises ``BaseException``
    # while its attributes are read, and the caller still receives a fresh
    # rejection with no chaining and no caller text.
    class ExplodingReport(CapabilityReport):
        def __getattribute__(self, name: str) -> object:
            if name == "diagnostics":
                raise BaseException(HOOK_CANARY)
            return object.__getattribute__(self, name)

    report = ExplodingReport(
        environment="KDE Plasma 6 Wayland",
        discovery=True,
        mirror=True,
        virtual_display=False,
        audio=True,
        remembered_pairing=False,
        additional_virtual_outputs=False,
        diagnostics=(),
    )
    log = ActivityLog(capabilities=capability_report())
    # A subclass is refused at construction, so the probe is installed afterwards
    # to reach the export path with the hostile record a caller would supply.
    object.__setattr__(log, "_capabilities", report)

    with pytest.raises(ValueError) as caught:
        log.export()

    assert type(caught.value) is ValueError
    assert str(caught.value) == (
        "the value could not be normalized at the diagnostics boundary"
    )
    assert HOOK_CANARY not in str(caught.value)
    assert HOOK_CANARY not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_caller_base_exception_never_escapes_the_boundary():
    # ``BaseException`` is not ``Exception``: an entry point that caught only
    # ``Exception`` would let this probe's payload escape as itself.
    class ExplodingFields(dict):
        def items(self):
            raise KeyboardInterrupt(HOOK_CANARY)

    with pytest.raises(TypeError) as caught:
        safe_activity("session", None, ExplodingFields(), UTC)  # type: ignore[arg-type]

    assert type(caught.value) is TypeError
    assert HOOK_CANARY not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


# -------------------------------------- rejection provenance (F-1 regressions)

#: The boundary's own text for a failure it did not issue itself. A failure the
#: boundary did issue keeps its own literal instead; what may never happen is a
#: failure *claiming* to be the boundary's and lending its caller's text.
FIXED_LITERAL: Final[str] = (
    "the value could not be normalized at the diagnostics boundary"
)

#: Attribute names a caller could dress its own exception up with, including the
#: private marker the boundary itself used to write. Provenance is not an
#: attribute: an exact built-in ``ValueError`` is something any caller can build
#: and fill in, so trusting one would make the caller's text the "fixed" text.
FORGED_MARKERS: Final[tuple[str, ...]] = (
    "_diagnostics_fixed_rejection",
    "_diagnostics_fixed_text",
    "_fixed_text",
    "message",
)


class HookedExceptionDict(dict):
    """A replacement instance dict whose every read raises and counts.

    ``object.__dict__`` of an exception can be replaced wholesale, so a probe
    that installs one of these on a caller-built exception reaches the boundary
    through a decorated public entry point carrying hook-instrumented metadata.
    """

    def __init__(self, counter: HookCounter) -> None:
        super().__init__()
        self._counter = counter

    def get(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
        self._counter.hit("exception.__dict__.get")
        raise BaseException(HOOK_CANARY)

    def __getitem__(self, key: object) -> object:  # type: ignore[override]
        self._counter.hit("exception.__dict__[key]")
        raise BaseException(HOOK_CANARY)

    def __contains__(self, key: object) -> bool:  # type: ignore[override]
        self._counter.hit("exception.__dict__.__contains__")
        raise BaseException(HOOK_CANARY)

    def items(self):  # type: ignore[override]
        self._counter.hit("exception.__dict__.items")
        raise BaseException(HOOK_CANARY)

    def keys(self):  # type: ignore[override]
        self._counter.hit("exception.__dict__.keys")
        raise BaseException(HOOK_CANARY)

    def values(self):  # type: ignore[override]
        self._counter.hit("exception.__dict__.values")
        raise BaseException(HOOK_CANARY)

    def copy(self):  # type: ignore[override]
        self._counter.hit("exception.__dict__.copy")
        raise BaseException(HOOK_CANARY)

    def __iter__(self):  # type: ignore[override]
        self._counter.hit("exception.__dict__.__iter__")
        raise BaseException(HOOK_CANARY)

    def __len__(self) -> int:  # type: ignore[override]
        self._counter.hit("exception.__dict__.__len__")
        raise BaseException(HOOK_CANARY)


def assert_literal_rejection(failure: Exception) -> None:
    """The replacement is the boundary's own literal and carries nothing else."""
    assert type(failure) is ValueError, (
        "a failure the boundary did not issue becomes a plain rejection"
    )
    assert str(failure) == FIXED_LITERAL
    assert failure.args == (FIXED_LITERAL,)
    assert HOOK_CANARY not in repr(failure)
    assert HOOK_CANARY not in repr(failure.args)
    assert HOOK_CANARY not in repr(failure.__dict__)
    assert failure.__cause__ is None, "the replacement must not chain its cause"
    assert failure.__context__ is None, "the replacement must not chain its context"


def forging_log(
    kind: type[Exception],
    *,
    marker: str | None = None,
    counter: HookCounter | None = None,
) -> ActivityLog:
    """An ``ActivityLog`` subclass whose trusted hook raises a caller failure.

    This is the reachable attack: ``export`` is a decorated public entry point
    that calls back into subclass-supplied code, so the failure is the caller's
    object exactly as a backend adapter could hand one over.
    """

    class ForgingLog(ActivityLog):
        def _capabilities_document(self) -> dict[str, object] | None:
            failure = kind(HOOK_CANARY)
            if marker is None:
                assert counter is not None, "a hostile dict probe needs a counter"
                failure.__dict__ = HookedExceptionDict(counter)
            else:
                failure.__dict__[marker] = HOOK_CANARY
            raise failure

    return ForgingLog(capabilities=capability_report())


@pytest.mark.parametrize("kind", [ValueError, TypeError])
@pytest.mark.parametrize("marker", FORGED_MARKERS)
def test_a_forged_rejection_marker_never_lends_caller_text(kind, marker):
    # Only the boundary can say a failure is the boundary's. A copied marker is
    # a string, and a string is not provenance, so the caller's canary must not
    # become the replacement's message.
    log = forging_log(kind, marker=marker)

    with pytest.raises(ValueError) as caught:
        log.export()

    assert_literal_rejection(caught.value)


@pytest.mark.parametrize("kind", [ValueError, TypeError])
def test_a_hostile_exception_instance_dict_is_never_read(kind):
    # The same attack with metadata that answers every read by running caller
    # code: provenancing on anything but identity would execute it.
    counter = hook_counter()
    log = forging_log(kind, marker=None, counter=counter)

    with pytest.raises(ValueError) as caught:
        log.export()

    assert_literal_rejection(caught.value)
    assert len(counter) == 0, f"caller exception hooks ran: {counter.calls}"


def test_a_source_written_rejection_is_recognized_without_touching_its_dict():
    # The positive half: the boundary's own rejection keeps its own text, and
    # needs no mutable marker on the exception to be recognized.
    with pytest.raises(ValueError) as caught:
        safe_activity("not-a-code", None, {}, UTC)

    assert type(caught.value) is ValueError
    assert str(caught.value) == "code must be one of the registered activity codes"
    assert dict(caught.value.__dict__) == {}, (
        "provenance must not be recorded in state the caller can edit"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_a_source_written_rejection_survives_a_nested_entry_point():
    # ``emit`` re-validates through ``safe_activity``, so a genuine rejection is
    # translated by two entry points. Provenance by identity has to survive the
    # nesting: degrading it to the generic literal would lose the only text that
    # tells the user what to fix.
    log = ActivityLog()
    smuggled = ActivityEvent(
        utc=UTC, code="pairing", receiver_id=None, message="fixed copy", fields={},
    )
    object.__setattr__(smuggled, "fields", MappingProxyType({"password": CANARY}))

    with pytest.raises(ValueError) as caught:
        log.emit(smuggled)

    assert type(caught.value) is ValueError
    assert str(caught.value) == (
        "field is not allowlisted; secrets and backend payloads cannot enter activity"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "rewrite",
    [
        lambda failure: setattr(failure, "args", (HOOK_CANARY,)),
        lambda failure: setattr(failure, "args", ()),
        lambda failure: failure.__dict__.update({"message": HOOK_CANARY}),
        lambda failure: setattr(failure, "__notes__", [HOOK_CANARY]),
    ],
    ids=["args-canary", "args-emptied", "dict-canary", "notes-canary"],
)
def test_a_genuine_rejection_lends_only_the_text_the_boundary_recorded(rewrite):
    # Provenance by identity says *which* failure is the boundary's, not what it
    # currently says. The text that propagates is the recorded one, so a caller
    # that caught a rejection and rewrote its message cannot lend its own text to
    # an enclosing entry point -- and cannot blank the message either.
    try:
        safe_activity("not-a-code", None, {}, UTC)
    except ValueError as failure:
        borrowed = failure
        rewrite(borrowed)

    class ReraisingLog(ActivityLog):
        def _capabilities_document(self) -> dict[str, object] | None:
            raise borrowed

    with pytest.raises(ValueError) as caught:
        ReraisingLog(capabilities=capability_report()).export()

    assert type(caught.value) is ValueError
    assert str(caught.value) == "code must be one of the registered activity codes"
    assert HOOK_CANARY not in str(caught.value)
    assert HOOK_CANARY not in repr(caught.value)
    assert HOOK_CANARY not in repr(caught.value.args)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
