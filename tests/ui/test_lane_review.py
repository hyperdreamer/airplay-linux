"""Integrated presenter/factory regression probes for the UI final-fix wave."""

import asyncio
from dataclasses import replace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton
import shiboken6

from test_presenter import (
    Action,
    Cancel,
    CapturingFactory,
    RECEIVER_A,
    Retry,
    SessionPhase,
    SubmitPairing,
    TransportState,
    TrustState,
    challenge,
    mirror_snapshot,
    physical,
    rig,
    started,
    virtual_snapshot,
)


async def real_pairing(rig, *, retained=False):
    factory = CapturingFactory(rig.window)
    rig.dialogs = factory
    rig.presenter._dialogs = factory
    rig.discover("a")
    await started(rig)
    view = challenge(RECEIVER_A)
    values = dict(
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        transport=TransportState.Pairing,
        challenge=view,
        session_generation=1,
    )
    if retained:
        virtual_snapshot(rig, **values)
    else:
        rig.snapshot("a", **values)
    await rig.settle()
    assert len(factory.created) == 1
    dialog = factory.created[0]
    assert dialog.isVisible()
    assert dialog.parent() is None
    return dialog


def visible_pairing_actions(dialog):
    return frozenset(
        action
        for action, name in (
            (Action.submit_pairing, "pairingSubmitButton"),
            (Action.cancel, "pairingCancelButton"),
            (Action.retry, "pairingRetryButton"),
        )
        if dialog.findChild(QPushButton, name) is not None
    )


async def test_real_factory_carries_retained_pairing_actions(rig):
    dialog = await real_pairing(rig, retained=True)
    projected = rig.offered() & {
        Action.submit_pairing,
        Action.cancel,
        Action.retry,
    }
    assert projected == {Action.submit_pairing, Action.cancel}
    assert visible_pairing_actions(dialog) == projected
    assert "Virtual-v1" in " ".join(
        label.text() for label in dialog.findChildren(QLabel)
    )


async def test_real_factory_honors_disagreeing_projection(rig, monkeypatch):
    from airplay_linux.ui import presenter

    real_project = presenter.project

    def restricted(snapshot, inputs):
        pane = real_project(snapshot, inputs)
        return replace(pane, actions=frozenset({Action.cancel})) if pane.challenge else pane

    monkeypatch.setattr(presenter, "project", restricted)
    dialog = await real_pairing(rig)
    assert rig.offered() == {Action.cancel}
    assert visible_pairing_actions(dialog) == rig.offered()


@pytest.mark.parametrize(
    "action,button,command",
    (
        (Action.cancel, "pairingCancelButton", Cancel),
        (Action.retry, "pairingRetryButton", Retry),
    ),
)
async def test_real_pairing_button_dispatches_captured_intent(
    rig, action, button, command
):
    dialog = await real_pairing(rig, retained=(action is Action.cancel))
    assert action in rig.offered()
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("synthetic-final-review-1729")
    dialog.findChild(QPushButton, button).click()
    assert field.text() == "" and not field.isUndoAvailable()
    assert not dialog.isVisible()
    await rig.settle()
    commands = rig.commands_of(command)
    assert len(commands) == 1
    assert commands[0].receiver_id == RECEIVER_A
    assert rig.commands_of(SubmitPairing) == []


async def test_authoritative_invalidation_wipes_open_prompt_via_subscription(rig):
    dialog = await real_pairing(rig)
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("synthetic-invalidated-2718")
    # An actual ManagerPort publication cancels the old challenge. No GUI click.
    rig.snapshot("a", session_generation=2, challenge=None)
    await rig.settle()
    assert rig.pane().challenge is None
    if shiboken6.isValid(dialog):
        assert field.text() == "" and not field.isUndoAvailable()
        assert not dialog.isVisible()
    assert rig.commands_of(SubmitPairing) == []


async def test_late_dispatch_exception_is_fenced_like_a_refusal(rig):
    rig.capture.add_physical(physical("DP-1"))
    rig.discover("a")
    await started(rig)
    rig.snapshot("a", session_generation=1)
    await rig.settle()
    entered, finish = asyncio.Event(), asyncio.Event()

    async def failing_dispatch(command):
        rig.manager.commands.append(command)
        entered.set()
        await finish.wait()
        raise RuntimeError("synthetic-stale-error")

    rig.manager.dispatch = failing_dispatch
    rig.click(Action.start)
    await rig.settle()
    assert entered.is_set()
    mirror_snapshot(rig, session_generation=2)
    await rig.settle()
    assert rig.error_text() == ""
    finish.set()
    await rig.settle()
    assert rig.error_text() == ""


async def test_stale_finished_after_adoption_is_identity_fenced(rig):
    retired = await real_pairing(rig)
    factory = rig.dialogs
    # Delay A's ordinary close completion until a published new challenge
    # opens B through the real presenter/factory and takes ownership.
    retired.close = lambda: True
    rig.snapshot(
        "a",
        phase=SessionPhase.Pairing,
        trust=TrustState.Pairing,
        transport=TransportState.Pairing,
        session_generation=1,
        challenge=challenge(RECEIVER_A, challenge_id=12),
    )
    await rig.settle()
    assert len(factory.created) == 2
    current = factory.created[-1]
    assert current is not retired and current.isVisible()
    field = current.findChild(QLineEdit, "secretField")
    field.insert("synthetic-current-3141")
    retired.reject()
    await rig.settle()
    assert rig.window._tracked_prompt is current
    observed = []
    rig.window.action_requested.connect(
        lambda action: observed.append(
            (action, field.text(), field.isUndoAvailable(), current.isVisible())
        )
    )
    button = rig.window.findChild(QPushButton, "retryButton")
    assert Action.retry in rig.offered() and button.isEnabled()
    button.click()
    assert observed == [("retry", "", False, False)]
    await rig.settle()
    assert len(rig.commands_of(Retry)) == 1
    retired.deleteLater()
    current.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
