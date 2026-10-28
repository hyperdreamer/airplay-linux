"""Two-receiver acceptance: every result belongs to the subject it was issued for.

The recurring defect this module exists to catch is a result applied to the wrong
subject: a pairing commit after the endpoint moved, a stale connection or
reconnect error painting the receiver the user has since selected, a prompt
tracked on the wrong window, or a second manager trying to own a resource the
first one acquired.

Every test drives the assembled application (production presenter, dialog factory
and window over the real session manager) with two receivers, so the assertions
name the receiver and the exact retained resource each time.  A bare "Ok" would
never satisfy them.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit

from airplay_linux import app as app_module
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import (
    ConfirmSwitch,
    SubmitPairing,
    TransportEventKind,
)
from airplay_linux.domain.operations import SensitiveText
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Mode,
    Ok,
    TransportState,
    TrustState,
)
from airplay_linux.runtime import RuntimeConfig
from airplay_linux.session import Route
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import target
from airplay_linux.ui.state import Action

from test_matrix import (
    RECEIVER_A,
    RECEIVER_B,
    AcceptanceTransport,
    build,  # noqa: F401 - re-exported fixture used by these tests
    field_of,
    remembered,
    saved_receiver,
)


def a() -> ReceiverId:
    return ReceiverId(RECEIVER_A)


def b() -> ReceiverId:
    return ReceiverId(RECEIVER_B)


def shiboken_alive(widget: Any) -> bool:
    import shiboken6

    return shiboken6.isValid(widget)


# ------------------------------------------------------------------ two sessions


async def test_two_receivers_render_their_own_rows_and_receive_their_own_commands(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("a")
    app.discover("b", address="192.0.2.20")
    await app.settle()

    assert [row.receiver_id for row in app.rows()] == [a(), b()]
    assert app.summary == "2 TVs available  0 sessions"

    # A: physical mirror.  Selecting a receiver is a real table-row selection.
    app.select("a")
    await app.settle()
    assert app.title == "Living Room 192.0.2.10"
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    a_stream = app.state("a").stream
    assert a_stream is not None

    # B: virtual display at the remembered explicit size, concurrently.
    app.select("b")
    await app.settle()
    assert app.phase == "Available"
    assert app.title == "Living Room 192.0.2.20"
    await app.start_virtual()
    assert app.phase == "Virtual display"
    b_stream = app.state("b").stream
    b_display = app.state("b").display_handle
    assert b_stream is not None and b_stream != a_stream
    assert b_display is not None

    assert app.summary == "2 TVs available  2 sessions"
    assert app.row("a").mode_label == "Mirror"
    assert app.row("b").mode_label == "Virtual display"
    assert app.row("b").size_label == "1920x1080"
    assert app.row("a").size_label == ""

    started = app.calls("transport.start_stream")
    assert [field_of(call["target"], "receiver_id") for call in started] == [a(), b()]
    created = app.calls("display.create")
    assert [call["receiver_id"] for call in created] == [b()]
    assert app.rig.display.created == [b_display]

    # Stopping B releases exactly B's stream and leaves A's untouched.
    app.click(Action.stop)
    await app.settle()
    assert app.phase == "Available"
    assert app.state("b").stream is None
    assert app.state("a").stream == a_stream
    assert set(app.rig.transport.streams) == {a_stream.lease_id}
    assert app.rig.display.removed == [b_display.owner_uuid]

    stopped = app.calls("transport.stop_stream")
    assert [field_of(call["handle"], "lease_id") for call in stopped] == [
        b_stream.lease_id
    ]
    assert app.row("a").mode_label == "Mirror"
    assert app.row("b").mode_label == ""


async def test_audio_failure_and_mute_stay_receiver_local(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    app.select("b")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    b_stream = app.state("b").stream
    assert b_stream is not None

    # A loses audio; B keeps both its stream and its mute control.
    app.select("a")
    await app.settle()
    a_stream = app.state("a").stream
    assert a_stream is not None
    app.rig.transport.emit(app.transport_event(TransportEventKind.audio_failed, "a"))
    await app.settle()
    assert app.offered() == {Action.retry_audio, Action.stop}
    assert app.button(Action.mute).isEnabled() is False
    app.select("b")
    await app.settle()
    assert app.phase == "Virtual display"
    assert app.button(Action.mute).isEnabled()
    app.button(Action.mute).click()
    await app.settle()
    assert app.rig.transport.muted == {b_stream.lease_id: True}
    assert app.state("a").stream == a_stream

    # Retrying A's audio re-targets A's exact stream and never B's.
    app.select("a")
    await app.settle()
    app.click(Action.retry_audio)
    await app.settle()
    audio = [
        field_of(call["handle"], "lease_id") for call in app.calls("transport.start_audio")
    ]
    # A's start, B's start, then A's retry: each audio control names its own lease.
    assert audio == [a_stream.lease_id, b_stream.lease_id, a_stream.lease_id]
    assert app.badges["audio"] == "Audio on"
    app.select("b")
    await app.settle()
    assert app.badges["audio"] == "Muted"


async def test_a_confirmed_switch_stops_before_it_starts_the_new_mode(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    first = app.state("a").stream
    assert first is not None

    # The pane's control is locked during a session: it can never present two
    # active modes.  The presenter's documented confirmation path is what a
    # request (keyboard/API) reaches, and it must stop before starting.
    assert app.mode_buttons() == (True, False, False)
    app.window.mode_requested.emit(Mode.VIRTUAL.value)
    await app.settle()
    dialog = await app.prompt("confirmStopDialog")
    assert app.phase == "Mirroring", "a pending confirmation must not stop anything"
    assert app.mode_buttons() == (True, False, False)
    assert any("Virtual display" in text for text in app.prompt_texts(dialog))
    app.prompt_buttons(dialog)["keepSessionButton"].click()
    await app.settle()
    assert [type(command).__name__ for command in app.dispatched] == ["Start"]
    assert app.phase == "Mirroring"
    assert app.state("a").stream == first

    app.window.mode_requested.emit(Mode.VIRTUAL.value)
    await app.settle()
    dialog = await app.prompt("confirmStopDialog")
    app.prompt_buttons(dialog)["confirmStopButton"].click()
    await app.settle()

    assert [type(command).__name__ for command in app.dispatched] == [
        "Start", "ConfirmSwitch",
    ]
    switch = app.dispatched[-1]
    assert isinstance(switch, ConfirmSwitch)
    assert switch.receiver_id == a()
    assert switch.confirmed is True
    assert switch.request.mode is Mode.VIRTUAL
    assert app.phase == "Virtual display"
    after = app.state("a").stream
    assert after is not None and after != first
    assert app.state("a").display_handle is not None
    assert set(app.rig.transport.streams) == {after.lease_id}

    names = app.journal.names()
    stop_at = names.index("transport.stop_stream")
    create_at = names.index("display.create")
    start_at = names.index("transport.start_stream", stop_at)
    assert stop_at < create_at < start_at
    stopped = app.calls("transport.stop_stream")
    assert [field_of(call["handle"], "lease_id") for call in stopped][-1] == first.lease_id


# --------------------------------------------------------------------- prompts


async def test_a_pairing_prompt_is_tracked_for_the_receiver_that_earned_it(
    build: Callable[..., Any],
) -> None:
    app = await build()
    app.discover("a")
    app.discover("b")
    await app.settle()

    app.select("a")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    a_dialog = await app.prompt("pairingDialog")
    a_field = a_dialog.findChild(QLineEdit, "secretField")
    a_field.insert("1111")
    a_challenge = app.state("a").challenge
    assert a_challenge is not None

    # Opening B's prompt adopts it and dismisses (wiping) A's open prompt.
    app.select("b")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    b_dialog = await app.prompt("pairingDialog")
    assert b_dialog is not a_dialog
    # The superseded prompt was closed before the new one took over; Qt may have
    # destroyed it already, which also destroys any typed secret.  If it is still
    # alive, the field must already be folded, wiped and un-undoable.
    if shiboken_alive(a_dialog):
        assert a_dialog.isVisible() is False
        assert a_field.text() == "" and a_field.isUndoAvailable() is False
    assert app.window._tracked_prompt is b_dialog
    b_challenge = app.state("b").challenge
    assert b_challenge is not None and b_challenge != a_challenge

    b_field = b_dialog.findChild(QLineEdit, "secretField")
    b_field.insert("2222")
    app.prompt_buttons(b_dialog)["pairingSubmitButton"].click()
    await app.settle()

    submits = app.calls("pairing.submit_pairing")
    assert [call["challenge_id"] for call in submits] == [b_challenge.challenge_id]
    assert app.state("b").trust is TrustState.Trusted
    # A was not submitted by B's prompt: its challenge is still live and its
    # snapshot still reports the pairing attempt, not trust.
    assert app.state("a").challenge == a_challenge
    assert app.state("a").trust is TrustState.Pairing
    assert "1111" not in app.activity_text and "2222" not in app.activity_text

    # A's challenge survived B's prompt; its own prompt still works afterwards.
    app.select("a")
    await app.settle()
    assert app.phase == "Pairing"
    assert Action.submit_pairing in app.offered()
    app.click(Action.submit_pairing)
    await app.settle()
    fresh = await app.prompt("pairingDialog")
    field = fresh.findChild(QLineEdit, "secretField")
    field.insert("3333")
    app.prompt_buttons(fresh)["pairingSubmitButton"].click()
    await app.settle()
    assert app.badges["trust"] == "Paired"
    assert a() in app.rig.pairing.trusted
    assert len(app.calls("pairing.submit_pairing")) == 2
    assert [call["challenge_id"] for call in app.calls("pairing.submit_pairing")] == [
        b_challenge.challenge_id, a_challenge.challenge_id,
    ]


async def test_a_pairing_submit_after_the_endpoint_moved_is_never_a_trust_commit(
    build: Callable[..., Any],
) -> None:
    app = await build()
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("b")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("pinned-pin")
    challenge = app.state("b").challenge
    assert challenge is not None
    assert challenge.endpoint_generation == 1
    trust_generation = app.state("b").trust_generation

    # The receiver answers from a replaced endpoint: the challenge is retired and
    # the open prompt is invalidated (closed and wiped) before any secret leaves.
    app.rig.discovery.add_target(target("b", 2))
    await app.settle()

    assert app.state("b").challenge is None
    assert app.state("b").trust is not TrustState.Trusted
    assert app.state("b").trust_generation == trust_generation + 1
    if shiboken_alive(dialog):
        assert field.text() == "" and field.isUndoAvailable() is False
        assert dialog.isVisible() is False
        # A user can still press the stale prompt's submit button.  The field is
        # wiped and the control disabled, so no stale secret can be submitted.
        buttons = app.prompt_buttons(dialog)
        if "pairingSubmitButton" in buttons:
            buttons["pairingSubmitButton"].click()
            await app.settle()
    assert app.calls("pairing.submit_pairing") == []
    assert b() not in app.rig.pairing.trusted

    # A late submit for the retired challenge is refused by the pairing authority
    # itself; it never commits trust for B, and it never touches A.
    await app.dispatchem(
        SubmitPairing(
            receiver_id=b(),
            challenge_id=challenge.challenge_id,
            value=SensitiveText("late-pin"),
        )
    )
    assert b() not in app.rig.pairing.trusted
    assert app.rig.pairing.journal.calls("pairing.submit_pairing") == []
    assert app.state("a").trust is TrustState.Unknown
    assert app.state("a").challenge is None
    assert app.state("b").transport is not TransportState.Streaming


# ------------------------------------------------------------- late completions


async def test_a_late_reconnect_failure_only_diagnoses_its_own_receiver(
    build: Callable[..., Any],
) -> None:
    app = await build(
        transport=AcceptanceTransport(),
        saved=(saved_receiver("b", resolution=remembered("b")),),
    )
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("b")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    retained = app.state("b").display_handle
    assert retained is not None

    app.rig.transport.emit(app.transport_event(TransportEventKind.network_lost, "b"))
    await app.settle()
    assert app.phase == "Reconnecting"

    # Hold B's retry, select A, and complete A's own start while B is suspended.
    deferred = Deferred()
    app.rig.transport.queue_result("start_stream", deferred)
    app.click(Action.retry)
    await app.settle()
    # The retry is in flight and fenced to B; nothing about A has changed yet.
    assert app.state("b").transport is TransportState.Connecting
    app.select("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    assert app.error == ""

    deferred.resolve(Err(error=diagnostic(ErrorCode.stale_target)))
    await app.settle()

    # A keeps streaming and shows no error; the failure belongs to B alone.
    assert app.state("a").transport is TransportState.Streaming
    assert app.error == ""
    assert app.state("b").display_handle == retained
    app.select("b")
    await app.settle()
    assert app.state("b").transport is TransportState.Reconnecting
    assert app.error == diagnostic(ErrorCode.stale_target, b()).message
    assert app.state("b").display_handle == retained
    assert app.rig.display.removed == []


# ------------------------------------------------------------------- ownership


async def test_the_presenter_commands_the_one_composed_manager(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    await app.settle()

    assert isinstance(app.rig, SessionRig)
    assert app.presenter._manager is app.runtime.manager
    assert app.runtime.manager is not app.rig.manager
    assert set(app.runtime.manager.handlers) == set(Route)

    await app.start_virtual()
    assert len(app.calls("display.create")) == 1
    assert len(app.calls("transport.start_stream")) == 1
    # The disabled rig's manager owns nothing at all.
    assert app.rig.manager.snapshot(a()).display_handle is None
    assert app.rig.manager.snapshot(a()).stream is None

    app.click(Action.stop)
    await app.settle()
    assert len(app.calls("transport.stop_stream")) == 1
    assert len(app.calls("display.remove")) == 1
    assert app.rig.display.outputs == {}


async def test_a_released_output_never_leaves_phantom_cleanup_actions(
    build: Callable[..., Any],
) -> None:
    """A confirmed release must read as released on every later render.

    The presenter refreshes durable ownership evidence when the selection moves
    (a normal two-receiver action).  When the session then *releases* its output,
    the cached evidence is no longer the store's answer, and the pane must not
    keep describing a resource that no longer exists as "Ownership unconfirmed"
    with Remove/Retry controls while the phase says the receiver is Available.
    """
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("a")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    owned = app.state("a").display_handle
    assert owned is not None

    # A two-receiver user action: look at B, then come back to A.
    app.select("b")
    await app.settle()
    app.select("a")
    await app.settle()
    assert app.presenter._records.get(owned.owner_uuid) is not None

    app.click(Action.stop)
    await app.settle()
    assert app.state("a").display_handle is None
    assert app.rig.ownerships.records == {}

    assert app.phase == "Available"
    assert app.badges["display"] == "No output"
    assert app.error == ""
    assert Action.start in app.offered()
    assert not (
        app.offered()
        & {
            Action.retry_cleanup,
            Action.keep_cleanup,
            Action.confirm_orphan,
            Action.keep_orphan,
        }
    )


# ----------------------------------------------------------------- app lifetime


async def test_closing_the_window_during_creation_ends_in_a_truthful_cleanup(
    build: Callable[..., Any],
) -> None:
    """Closing during an in-flight creation never lies about the cleanup.

    Two things must hold.  While the reservation is genuinely unconfirmed the
    dashboard stays open with bounded progress instead of disappearing.  And the
    final result must be truthful: either cleanup is confirmed (``Ok``, window
    gone, nothing owned) or it is reported as unconfirmed (``Err``) with a
    resource that really is still unconfirmed.  A false "could not be confirmed"
    for resources that were in fact released is a shutdown defect.
    """
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("b")
    await app.settle()
    publish = Deferred()
    store = app.rig.ownerships
    write = store.write

    async def held(record):
        if record.stage == "published":
            await publish.wait()
        return await write(record)

    store.write = held
    try:
        await app.start_virtual()
        assert app.phase == "Creating display"
        assert app.window.close() is False
        await app.settle()
        assert app.window.isVisible(), "cleanup is still unconfirmed"
        assert "closing" in app.window.windowTitle().lower()
        assert app.presenter._shutdown_task is not None
        assert not app.presenter._shutdown_task.done()

        # The pending publication may now finish; cleanup owns its release.
        publish.resolve(None)
        outcome = await asyncio.wait_for(app.runtime.shutdown(), 8)
    finally:
        if not publish.resolved:
            publish.resolve(None)
        await app.settle(40)
        shutdown_task = app.presenter._shutdown_task
        if shutdown_task is not None and not shutdown_task.done():
            await asyncio.wait_for(shutdown_task, 8)

    if isinstance(outcome, Err):
        # A cleanup failure must name a resource that is genuinely unresolved.
        remaining = (
            dict(app.rig.ownerships.records)
            or dict(app.rig.transport.streams)
            or dict(app.rig.display.outputs)
            or list(app.rig.display.process.environments())
        )
        assert remaining, (
            "shutdown reported unconfirmed cleanup although every owned resource "
            f"was released: {outcome.error.code}"
        )
        assert app.window.isVisible()
        return

    assert isinstance(outcome, Ok)
    assert app.window.isVisible() is False
    assert app.runtime._closed
    assert app.rig.transport.streams == {}
    assert app.rig.display.outputs == {}
    assert app.rig.ownerships.records == {}
    assert app.rig.display.removed, "the late creation was released, not leaked"
    assert not app.manager.pending_resources(b())


async def test_a_second_instance_is_rejected_before_a_second_window_exists(
    build: Callable[..., Any], tmp_path: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    from uuid import uuid4

    from airplay_linux.domain.operations import InstanceId
    from airplay_linux.process.instance import InstanceGuard
    from airplay_linux.process.linux import LinuxProcessRunner

    app = await build()
    window_class = app.window.__class__
    windows = [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.isVisible() and isinstance(widget, window_class)
    ]
    assert len(windows) == 1

    # The operator's entry point owns the single-instance lock for its lifetime.
    guard = InstanceGuard(app.runtime.config.runtime_root, LinuxProcessRunner())
    assert isinstance(guard.acquire(InstanceId(uuid4())), Ok)
    try:
        second = app_module._run_guarded(
            RuntimeConfig(
                state_root=tmp_path / "second" / "state",
                runtime_root=app.runtime.config.runtime_root,
                live=True,
            )
        )
    finally:
        guard.release()

    assert second == 1
    assert "another instance already owns this runtime root" in capsys.readouterr().err
    still = [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.isVisible() and isinstance(widget, window_class)
    ]
    assert still == windows
