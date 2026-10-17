"""Diagnostics must stay actionable for the user and silent about private input.

The activity log is an output channel: every value that reaches it, its bounded
export, an emitted entry's repr, or a rejection message is tested for the canary
strings a secret would leave behind.
"""

from __future__ import annotations

import json

import pytest

from airplay_linux.diagnostics import ActivityLog, diagnostic, safe_activity
from airplay_linux.diagnostics.activity import ALLOWED_FIELDS, MAX_FIELD_TEXT
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    SensitiveText,
)
from airplay_linux.domain.records import ActivityEvent
from airplay_linux.domain.resources import (
    CapabilityReport,
    Diagnostic,
    ErrorCode,
    RecoveryAction,
)
from airplay_linux.testing import MemoryActivity, SequenceIds

RECEIVER = ReceiverId("airplay-device:001122334455")
UTC = "2026-10-17T00:00:00Z"
CANARY = "canary-8496"

#: The frozen permit list from the task: nothing else may carry backend text.
FIELD_SAMPLES: dict[str, str | int | bool] = {
    "receiver_name": "Office TV",
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


# ------------------------------------------------------------------ activity


def test_allowlist_matches_the_frozen_field_permit():
    assert set(FIELD_SAMPLES) == ALLOWED_FIELDS
    assert MAX_FIELD_TEXT <= 200


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
        safe_activity("pairing", None, {"receiver_name": value}, UTC)

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)


@pytest.mark.parametrize("value", [None, 1.5, b"canary-8496", ["canary-8496"], {"canary-8496": 1}])
def test_raw_backend_payloads_cannot_enter_activity(value):
    with pytest.raises(ValueError, match="value") as caught:
        safe_activity("process", None, {"command_name": value}, UTC)

    assert CANARY not in str(caught.value)


def test_fields_must_be_a_mapping():
    with pytest.raises(TypeError, match="mapping"):
        safe_activity("session", None, [("receiver_name", "Office TV")], UTC)  # type: ignore[arg-type]


def test_control_characters_are_escaped():
    event = safe_activity("display", None, {"receiver_name": "AirPlay\nTV\x00\x1b[31m"}, UTC)
    rendered = event.fields["receiver_name"]

    assert isinstance(rendered, str)
    assert "\n" not in rendered
    assert "\x00" not in rendered
    assert "\x1b" not in rendered
    assert "\\u000a" in rendered
    assert "\\u001b" in rendered
    assert "AirPlay" in rendered


def test_long_field_text_is_bounded():
    event = safe_activity("display", None, {"receiver_name": "x" * 5000}, UTC)
    rendered = event.fields["receiver_name"]

    assert isinstance(rendered, str)
    assert len(rendered) <= MAX_FIELD_TEXT
    assert rendered.endswith("\N{HORIZONTAL ELLIPSIS}")


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


def test_activity_messages_are_fixed_per_code():
    first = safe_activity("pairing.completed", None, {}, UTC)
    second = safe_activity("pairing.completed", None, {}, UTC)
    unknown = safe_activity("future.event", None, {}, UTC)

    assert first.message == second.message
    assert first.message.strip()
    assert unknown.message.strip()
    assert CANARY not in first.message


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
    log.emit(safe_activity("display", None, {"display": "Owned"}, UTC))

    first = log.export()

    assert first == log.export()
    assert json.loads(first)["activity"][0]["utc"] == UTC
    assert "\x1b" not in first


def test_activity_log_feeds_the_frozen_sink():
    log = ActivityLog()
    sink = MemoryActivity()
    identifiers = SequenceIds(seed=1)
    receiver = ReceiverId(f"airplay-device:{identifiers.new()}")

    event = safe_activity("session.started", receiver, {"phase": "Connecting"}, UTC)
    log.emit(event)
    sink.emit(event)

    assert sink.events == list(log.entries())
