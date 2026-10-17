"""Diagnostics must stay actionable for the user and silent about private input.

The activity log is an output channel: every value that reaches it, its bounded
export, an emitted entry's repr, or a rejection message is tested for the canary
strings a secret would leave behind.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Final

import pytest

from airplay_linux.diagnostics import (
    ACTIVITY_CODES,
    ALLOWED_COMMANDS,
    ActivityLog,
    diagnostic,
    safe_activity,
)
from airplay_linux.diagnostics.activity import (
    ALLOWED_FIELDS,
    FIELD_SHAPES,
    MAX_FIELD_TEXT,
    _VOCABULARIES,
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


def test_catalog_messages_are_distinct_and_fully_rendered():
    messages = [diagnostic(code).message for code in ErrorCode]

    assert len(set(messages)) == len(messages), "each code needs its own message"
    for message in messages:
        assert "{" not in message and "}" not in message
        assert len(message) <= 240


def test_diagnostic_keeps_the_receiver_identity():
    assert diagnostic(ErrorCode.transport_failed, RECEIVER).receiver_id == RECEIVER


def test_diagnostic_rejects_arbitrary_error_text():
    with pytest.raises(TypeError) as caught:
        diagnostic(CANARY)  # type: ignore[arg-type]

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


def test_diagnostic_rejects_a_command_outside_the_allowlist():
    with pytest.raises(ValueError, match="command") as caught:
        diagnostic(ErrorCode.dependency_missing, command=CANARY)

    assert CANARY not in str(caught.value)


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


# ------------------------------------------------- design action matrix

#: The design's normative "Allowed primary actions" text, quoted from the UI state
#: matrix rows that name an :class:`ErrorCode` (design:110-128). Rows that no code
#: represents (Available, Offline, Pairing, Resolution required, Creating output,
#: Connecting, Mirroring, Virtual display, Reconfiguring) are absent on purpose.
#: ``unsupported_source`` is absent too: design:64/534 require surfacing the
#: unsupported descriptor so the user picks another source, which is the ``back``
#: action of its own catalog entry, not a matrix row.
DESIGN_ROWS: tuple[tuple[str, tuple[ErrorCode, ...], tuple[str, ...]], ...] = (
    (
        "design:121 resolution mismatch",
        (ErrorCode.resolution_mismatch,),
        ("Recreate at accepted mode", "Stop"),
    ),
    (
        "design:124 dependency or capability failure",
        (
            ErrorCode.unsupported_operation,
            ErrorCode.dependency_missing,
            ErrorCode.dependency_incompatible,
        ),
        ("Retry after fix", "Back"),
    ),
    (
        "design:125 orphan candidate",
        (ErrorCode.ownership_mismatch,),
        ("Remove after confirmation", "Keep"),
    ),
    (
        "design:126 cleanup required",
        (ErrorCode.cleanup_timeout,),
        ("Retry cleanup", "Keep for manual cleanup", "Stop"),
    ),
    (
        "design:127 trust cleanup required",
        (ErrorCode.trust_cleanup_required,),
        ("Retry revocation", "Keep blocked", "Remove saved metadata"),
    ),
    (
        # One code reports the audio failure in both phases, so its action set is
        # the union of the two rows: retry before video readiness and after it.
        "design:122-123 audio decision required or audio unavailable",
        (ErrorCode.audio_denied, ErrorCode.audio_unavailable),
        ("Retry audio", "explicit video-only start", "Cancel", "Stop"),
    ),
    (
        # design:111 (Recovering), design:119 (Reconnecting) and design:128
        # (Failed) share one action set, so a retryable failure pins the same tuple.
        "design:111-128 recovering, reconnecting, or failed",
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
        ("Retry", "Stop"),
    ),
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


def test_design_matrix_names_only_known_actions():
    for row, _, actions in DESIGN_ROWS:
        for label in actions:
            assert label in DESIGN_ACTIONS, f"{row} names an unmapped action {label!r}"


def test_design_matrix_rows_do_not_contradict_each_other():
    pinned = [code for _, codes, _ in DESIGN_ROWS for code in codes]

    assert len(pinned) == len(set(pinned)), "a code may be pinned to one row only"


@pytest.mark.parametrize(("row", "codes", "actions"), DESIGN_ROWS, ids=[row for row, _, _ in DESIGN_ROWS])
def test_catalog_recovery_matches_the_design_action_matrix(row, codes, actions):
    expected = frozenset(DESIGN_ACTIONS[label] for label in actions)

    for code in codes:
        recovery = diagnostic(code).recovery
        assert len(set(recovery)) == len(recovery), f"{code} repeats a recovery action"
        assert set(recovery) == expected, f"{code} must match {row}: {sorted(expected)}"


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


def test_a_str_subclass_with_legitimate_data_is_stored_as_plain_text():
    class Quiet(str):
        def __str__(self) -> str:
            return SMUGGLED

    event = safe_activity("session", None, {"phase": Quiet("Mirroring")}, UTC)

    # The stored value is the exact built-in string whose data was validated.
    assert event.fields == {"phase": "Mirroring"}
    assert type(event.fields["phase"]) is str
    assert SMUGGLED not in repr(event)
    assert SMUGGLED not in repr(dict(event.fields))


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
    assert value in _VOCABULARIES["phase"], "the probe must still satisfy membership"

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
    # ``receiver_id`` is escaped and bounded by reading its characters, so a
    # subclass whose iteration protocol yields other text must not survive.
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
    # ``datetime.fromisoformat`` accepts ``str`` subclasses, so the timestamp is
    # re-read from the value's data before parsing, never through its hooks.
    spoof = RenderSpoof("2026-10-17T02:00:00+02:00")

    event = safe_activity("session", None, {}, spoof)

    assert event.utc == UTC
    assert type(event.utc) is str
    assert SMUGGLED not in repr(event)


def test_a_capability_environment_subclass_cannot_render_the_canary():
    # The export sanitizes this text the same way as an activity value.
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

    assert SMUGGLED not in log.export()


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


def test_fields_must_be_a_mapping():
    with pytest.raises(TypeError, match="mapping"):
        safe_activity("session", None, [("phase", "Mirroring")], UTC)  # type: ignore[arg-type]


def test_control_characters_are_escaped():
    hostile = ReceiverId("airplay-device:AirPlay\nTV\x00\x1b[31m")
    event = safe_activity("display.published", hostile, {}, UTC)
    rendered = event.receiver_id

    assert isinstance(rendered, str)
    assert "\n" not in rendered
    assert "\x00" not in rendered
    assert "\x1b" not in rendered
    assert "\\u000a" in rendered
    assert "\\u001b" in rendered
    assert "AirPlay" in rendered


def test_long_identity_text_is_bounded():
    event = safe_activity("display.published", ReceiverId("x" * 5000), {}, UTC)
    rendered = event.receiver_id

    assert isinstance(rendered, str)
    assert len(rendered) <= MAX_FIELD_TEXT
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")


#: A complete escape this module can emit, so a leftover backslash is a fragment.
COMPLETE_ESCAPE = re.compile(r"\\u[0-9a-f]{4}|\\U[0-9a-f]{8}")


@pytest.mark.parametrize("padding", [MAX_FIELD_TEXT - 6, MAX_FIELD_TEXT - 5, MAX_FIELD_TEXT - 4, MAX_FIELD_TEXT - 3])
def test_bounding_never_splits_an_escape_sequence(padding):
    value = ReceiverId("A" * padding + "\n" * 6)

    rendered = safe_activity("display.published", value, {}, UTC).receiver_id

    assert isinstance(rendered, str)
    assert len(rendered) <= MAX_FIELD_TEXT
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")
    # A truncated ``\u00`` fragment is a dangling backslash once the complete
    # escapes are removed; the source contains none of its own.
    assert "\\" not in COMPLETE_ESCAPE.sub("", rendered)


def test_bounding_keeps_a_whole_escape_at_the_boundary():
    value = ReceiverId("A" * (MAX_FIELD_TEXT - 7) + "\n\n")

    rendered = safe_activity("display.published", value, {}, UTC).receiver_id

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


def test_utc_is_normalized_to_canonical_utc():
    event = safe_activity("session", None, {}, "2026-10-17T02:00:00+02:00")

    assert event.utc == UTC


@pytest.mark.parametrize("receiver", [None, RECEIVER])
def test_receiver_identity_is_preserved(receiver):
    assert safe_activity("session", receiver, {}, UTC).receiver_id == receiver


def test_receiver_identity_must_be_a_identifier():
    with pytest.raises(TypeError, match="receiver"):
        safe_activity("session", 12345, {}, UTC)  # type: ignore[arg-type]


def test_receiver_identity_cannot_carry_controls_or_unbounded_text():
    hostile = ReceiverId("airplay-device:" + "\n\x1b[31m\x00" + "A" * 430)

    event = safe_activity("session", hostile, {}, UTC)
    rendered = event.receiver_id

    assert rendered is not None
    assert len(rendered) <= MAX_FIELD_TEXT
    assert "airplay-device:" in rendered
    assert "\\u000a" in rendered
    for control in ("\n", "\x1b", "\x00"):
        assert control not in rendered

    log = ActivityLog()
    log.emit(event)
    entry = log.entries()[0]
    assert entry.receiver_id is not None
    for control in ("\n", "\x1b", "\x00"):
        assert control not in repr(entry)
        assert control not in entry.receiver_id
    exported = json.loads(log.export())["activity"][0]["receiver_id"]
    assert exported == rendered
    assert "\x1b" not in log.export() and "\x00" not in log.export()


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
    # F-2: the code's own text is the registered 'session', so every check on
    # the original object passes while ``__str__`` renders the canary, and the
    # old lookup raised KeyError('raw-backend-canary-8496'). The code is now
    # read as data and stored as that same primitive, so the fixed copy is used
    # and no failure ever carries the rejected text.
    event = safe_activity(RenderSpoof("session"), None, {}, UTC)

    assert event.code == "session"
    assert type(event.code) is str
    assert event.message == safe_activity("session", None, {}, UTC).message
    assert SMUGGLED not in repr(event)

    log = ActivityLog()
    log.emit(event)
    for rendering in (repr(log.entries()), log.export()):
        assert SMUGGLED not in rendering


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
