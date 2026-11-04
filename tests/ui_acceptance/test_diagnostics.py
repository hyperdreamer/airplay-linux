"""Diagnostics acceptance: exact recovery copy in the UI, nothing raw in the export.

The dashboard may only ever show the catalog's fixed copy for a typed error, and
the activity export may only contain allowlisted, secret-free records.  These
tests drive the assembled application, make it fail the way a backend fails, and
then read both surfaces the operator would use: the selected pane and
``ActivityLog.export()``.

Secret canaries (a typed PIN, a portal restore token, a capture lease and backend
exception text) are injected through the real boundaries.  If any canary
survives into the pane, the activity strip, the retained entries or the JSON
export, the test fails with that canary in the assertion.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Callable
from uuid import UUID

import pytest
from PySide6.QtWidgets import QLineEdit, QPushButton

from airplay_linux.diagnostics import ACTIVITY_CODES, ENTRIES, diagnostic, safe_activity
from airplay_linux.diagnostics.activity import ALLOWED_FIELDS
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import CommandReceipt, Start
from airplay_linux.domain.operations import LeaseId, PairingResult
from airplay_linux.domain.resources import (
    Diagnostic,
    Err,
    ErrorCode,
    RecoveryAction,
)
from airplay_linux.testing.builders import physical
from airplay_linux.ui.state import Action

from test_matrix import (
    RECEIVER_A,
    UiApplication,
    build,  # noqa: F401 - re-exported fixture used by these tests
    capability_report,
    remembered,
    saved_receiver,
)

PIN_CANARY = "PIN-CANARY-9182"
SECOND_PIN_CANARY = "PIN-CANARY-4477"
PORTAL_CANARY = "PORTAL-RESTORE-CANARY-5511"
LEASE_CANARY = "LEASE-CANARY-6623"
RAW_BACKEND_CANARY = "RAW-BACKEND-EXCEPTION-CANARY-7711"
RAW_DETAIL_CANARY = "RAW-PROCESS-OUTPUT-CANARY-8822"

#: The concrete control the pane offers for each catalog recovery action that has
#: one.  ``back``/``choose_mode`` are the pane's mode selector (and the other
#: receivers), so they are asserted through that control instead of a button.
RECOVERY_CONTROL: dict[RecoveryAction, Action] = {
    RecoveryAction.retry: Action.retry,
    RecoveryAction.stop: Action.stop,
    RecoveryAction.back: Action.cancel,
    RecoveryAction.choose_mode: Action.change_resolution,
    RecoveryAction.video_only: Action.video_only,
    RecoveryAction.retry_audio: Action.retry_audio,
    RecoveryAction.retry_cleanup: Action.retry_cleanup,
    RecoveryAction.retry_revocation: Action.retry_revocation,
    RecoveryAction.keep_blocked: Action.keep_blocked,
    RecoveryAction.remove_metadata: Action.remove_metadata,
    RecoveryAction.recreate: Action.recreate,
    RecoveryAction.confirm_remove: Action.confirm_orphan,
    RecoveryAction.keep: Action.keep_cleanup,
}

#: The complete fixed activity copy.  The export may only ever contain one of
#: these exact sentences, so backend or exception text cannot be smuggled in.
ACTIVITY_COPY: dict[str, str] = {
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
    "screen.forget": "Remembered screen-sharing choices were cleared.",
    "screen.forget_failed": "Remembered screen-sharing choices could not be cleared.",
    "resolution.required": "A resolution must be chosen before this start continues.",
    "resolution.remembered": "The chosen resolution was remembered for this receiver.",
    "audio.decision_required": "An explicit audio decision is required before start.",
    "mode.deferred": "The requested mode is queued until cleanup is confirmed.",
    "process.failed": "A backend process reported a failure.",
    "session": "Session lifecycle event."
}


def alive(widget: Any) -> bool:
    import shiboken6

    return shiboken6.isValid(widget)


def export_of(app: UiApplication) -> dict[str, Any]:
    document = app.activity.export()
    assert isinstance(document, str)
    return json.loads(document)


def rendered_surfaces(app: UiApplication) -> str:
    return " | ".join(
        [
            app.title,
            app.phase,
            app.notice,
            app.error,
            app.activity_text,
            " ".join(app.badges.values()),
        ]
    )


def assert_no_canary(app: UiApplication, *canaries: str) -> None:
    """No canary may appear in any user-visible or exported diagnostic surface."""
    document = app.activity.export()
    pane = rendered_surfaces(app)
    for canary in canaries:
        assert canary not in document, f"{canary!r} leaked into the diagnostics export"
        assert canary not in pane, f"{canary!r} leaked into the rendered pane"
        for entry in app.activity.entries():
            assert canary not in repr(entry), f"{canary!r} leaked into a retained record"


def assert_export_matches_the_log(app: UiApplication) -> dict[str, Any]:
    document = export_of(app)
    entries = app.activity.entries()
    assert document["schema"] == "airplay-linux-diagnostics/1"
    assert document["counts"]["entries"] == len(entries)
    assert document["counts"]["capacity"] == 500
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.code] = counts.get(entry.code, 0) + 1
    assert document["counts"]["by_code"] == counts
    assert [item["code"] for item in document["activity"]] == [
        entry.code for entry in entries
    ]
    assert [item["message"] for item in document["activity"]] == [
        entry.message for entry in entries
    ]
    for item in document["activity"]:
        assert item["code"] in ACTIVITY_CODES
        assert item["code"] in ACTIVITY_COPY
        assert item["message"] == ACTIVITY_COPY[item["code"]]
    return document


# ------------------------------------------------------------------ capability


async def test_a_capability_failure_shows_the_catalog_copy_and_offers_its_recovery(
    build: Callable[..., Any],
) -> None:
    code = ErrorCode.dependency_missing
    report = capability_report(
        virtual_display=False,
        diagnostics=(
            diagnostic(code, ReceiverId(RECEIVER_A), command="krfb-virtualmonitor"),
        ),
    )
    app = await build(capabilities=report)
    app.discover("a")
    await app.settle()
    app.widget("modeVirtual", QPushButton).click()
    await app.settle()

    entry = ENTRIES[code]
    expected = entry.command_message.format(command="krfb-virtualmonitor")
    assert entry.message != expected
    assert app.error == expected
    assert entry.recovery == (RecoveryAction.retry, RecoveryAction.back)
    assert RECOVERY_CONTROL[RecoveryAction.retry] in app.offered()
    # "Back" is the still-usable mode selector: the operator can choose an
    # available mode instead, which the matrix row exercises end to end.
    assert app.mode_buttons()[2] is True
    assert Action.start not in app.offered()

    document = assert_export_matches_the_log(app)
    capabilities = document["capabilities"]
    assert capabilities["environment"] == "KDE Plasma 6 Wayland"
    assert capabilities["virtual_display"] is False
    assert capabilities["mirror"] is True
    assert capabilities["diagnostic_codes"] == ["dependency_missing"]
    assert "detail" not in json.dumps(document)


async def test_the_export_names_capability_codes_and_never_the_probe_text(
    build: Callable[..., Any],
) -> None:
    """Only validated codes cross the capability boundary into the export."""
    report = capability_report(
        virtual_display=False,
        diagnostics=(
            Diagnostic(
                code=ErrorCode.dependency_missing,
                message=RAW_BACKEND_CANARY,
                recovery=(RecoveryAction.retry,),
                receiver_id=ReceiverId(RECEIVER_A),
                detail=RAW_DETAIL_CANARY,
            ),
        ),
    )
    app = await build(capabilities=report)
    app.discover("a")
    await app.settle()

    document = export_of(app)
    assert document["capabilities"]["diagnostic_codes"] == ["dependency_missing"]
    encoded = json.dumps(document)
    assert RAW_BACKEND_CANARY not in encoded
    assert RAW_DETAIL_CANARY not in encoded
    for entry in app.activity.entries():
        assert RAW_BACKEND_CANARY not in repr(entry)
        assert RAW_DETAIL_CANARY not in repr(entry)


# ------------------------------------------------------------------- cleanup


async def test_cleanup_failure_shows_the_exact_catalog_copy_and_its_recovery_actions(
    build: Callable[..., Any],
) -> None:
    tokenized = replace(
        physical(),
        portal_restore_token=PORTAL_CANARY,
        capture_lease_id=LeaseId(UUID(int=0x6623)),
    )
    app = await build(sources=(tokenized,))
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    assert app.window._source.currentText() == "DP-1"
    assert app.window._source.currentData() == "DP-1"

    app.rig.transport.queue_result(
        "stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)),
    )
    app.click(Action.stop)
    await app.settle()

    entry = ENTRIES[ErrorCode.cleanup_timeout]
    assert app.error == entry.message
    promised = {RECOVERY_CONTROL[action] for action in entry.recovery}
    assert promised == {Action.retry_cleanup, Action.keep_cleanup, Action.stop}
    assert promised <= app.offered()
    assert Action.start not in app.offered()

    document = assert_export_matches_the_log(app)
    assert document["counts"]["by_code"]["audio.enabled"] >= 1
    assert_no_canary(app, PORTAL_CANARY, str(UUID(int=0x6623)), LEASE_CANARY)

    # The safe export carries no field that could ever hold an owner or portal
    # token, so no future emission can add one without changing this contract.
    assert not (
        set(ALLOWED_FIELDS)
        & {
            "owner_token",
            "portal_restore_token",
            "capture_lease_id",
            "backend_token",
            "credential_ref",
        }
    )


# ------------------------------------------------------------------- secrets


async def test_a_typed_pairing_secret_never_reaches_the_export_or_the_activity(
    build: Callable[..., Any],
) -> None:
    app = await build()
    app.discover("a")
    await app.settle()

    # First attempt: the receiver rejects the typed value.  The pane must repeat
    # the catalog's copy, and the value itself must not survive anywhere.
    app.rig.pairing.queue_result(
        "submit_pairing",
        PairingResult(
            kind="InvalidInput",
            credential_ref=None,
            diagnostic=diagnostic(ErrorCode.invalid_input, ReceiverId(RECEIVER_A)),
        ),
    )
    app.click(Action.pair)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert(PIN_CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await app.settle()
    assert app.error == ENTRIES[ErrorCode.invalid_input].message
    # The prompt is disposed after its one answer; while it exists the field must
    # already be wiped and un-undoable.  Deletion destroys the secret with it.
    if alive(field):
        assert field.text() == "" and field.isUndoAvailable() is False
    assert Action.submit_pairing in app.offered()
    assert_no_canary(app, PIN_CANARY)

    # Second attempt: the receiver accepts its own value and the challenge retires.
    app.click(Action.submit_pairing)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert(SECOND_PIN_CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await app.settle()
    assert app.badges["trust"] == "Paired"
    assert app.error == ""

    document = assert_export_matches_the_log(app)
    for item in document["activity"]:
        assert item["code"] in ACTIVITY_COPY
    assert_no_canary(app, PIN_CANARY, SECOND_PIN_CANARY)


# ------------------------------------------------------------------- receipts


async def test_a_receipt_carrying_backend_text_is_rendered_from_the_catalog_only(
    build: Callable[..., Any],
) -> None:
    """A port may hand back arbitrary Diagnostic text; the pane must not repeat it.

    The response is injected at the *port* boundary (the presenter's manager),
    which is exactly the seam that carries adapter text in production.  The
    production presenter body under test is untouched.
    """
    report = capability_report()
    app = await build(capabilities=report)
    app.discover("a")
    await app.settle()
    assert Action.start in app.offered()

    real = app.manager.dispatch

    async def poisoned(command: Any) -> CommandReceipt:
        if isinstance(command, Start):
            return CommandReceipt(
                operation_id=app.manager.context(
                    ReceiverId(RECEIVER_A)
                ).operation().operation_id,
                accepted=False,
                diagnostic=Diagnostic(
                    code=ErrorCode.dependency_missing,
                    message=RAW_BACKEND_CANARY,
                    recovery=(RecoveryAction.retry,),
                    receiver_id=ReceiverId(RECEIVER_A),
                    detail=RAW_DETAIL_CANARY,
                ),
            )
        return await real(command)

    app.manager.dispatch = poisoned
    try:
        app.click(Action.start)
        await app.settle()
    finally:
        app.manager.dispatch = real

    assert app.error == diagnostic(
        ErrorCode.dependency_missing, ReceiverId(RECEIVER_A)
    ).message
    assert app.error not in {RAW_BACKEND_CANARY, RAW_DETAIL_CANARY}
    assert_no_canary(app, RAW_BACKEND_CANARY, RAW_DETAIL_CANARY)


# ------------------------------------------------------------------ structure


async def test_the_activity_strip_is_the_safe_log_and_the_export_is_deterministic(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"

    entries = app.activity.entries()
    assert entries
    assert app.activity_text == "\n".join(
        f"{event.utc}  {event.message}" for event in entries
    )
    for event in entries:
        assert event.code in ACTIVITY_CODES
        assert event.message == ACTIVITY_COPY[event.code]
        assert set(event.fields) <= set(ALLOWED_FIELDS)

    first = app.activity.export()
    second = app.activity.export()
    assert first == second
    document = json.loads(first)
    assert document["counts"]["entries"] == len(entries)
    assert document["capabilities"]["environment"] == "KDE Plasma 6 Wayland"
    assert document["capabilities"]["virtual_display"] is True
    assert document["capabilities"]["diagnostic_codes"] == []

    # A raw, unregistered activity token is refused at the safe boundary instead
    # of entering the log the strip and the export read.
    with pytest.raises((TypeError, ValueError)):
        safe_activity(
            "process.failed",
            ReceiverId(RECEIVER_A),
            {"command_name": RAW_DETAIL_CANARY},
            "2026-10-17T00:00:00Z",
        )
    assert app.activity.export() == first
