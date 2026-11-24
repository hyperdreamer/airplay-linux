from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QPushButton

from airplay_linux.domain.messages import DismissCredentialPrompt, SubmitCredential
from airplay_linux.domain.operations import SensitiveText
from airplay_linux.ui.dialogs import PairingDialog
from airplay_linux.ui.state import Action
from test_presenter import (
    CapturingFactory, RECEIVER_A, challenge, relay_prompt, rig, started,
)


@pytest.mark.parametrize('stage', ['editing', 'submitting'])
@pytest.mark.parametrize('close_style', ['cancel', 'window_close', 'escape'])
async def test_closing_submitted_relay_dialog_dismisses_reporting(rig, close_style, stage):
    factory = CapturingFactory(rig.window)
    rig.presenter._dialogs = factory
    await started(rig)
    prompt = relay_prompt()
    rig.snapshot(
        'a', operation_id=prompt.request.start_operation_id,
        session_generation=1, credential_prompt=prompt,
    )
    await rig.settle()
    dialog, = factory.created
    dialog.findChild(QLineEdit, 'secretField').insert('review-input-5968')
    if stage == 'submitting':
        dialog.findChild(QPushButton, 'pairingSubmitButton').click()
        await rig.settle()
        assert len(rig.commands_of(SubmitCredential)) == 1
    assert dialog.isVisible()
    if close_style == 'cancel':
        dialog.findChild(QPushButton, 'pairingCancelButton').click()
    elif close_style == 'window_close':
        dialog.close()
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    await rig.settle()
    assert not dialog.isVisible()
    assert len(rig.commands_of(DismissCredentialPrompt)) == 1
    assert prompt.challenge not in factory._pairing_prompts


def test_nonempty_whitespace_credential_is_accepted(qtbot):
    dialog = PairingDialog(
        challenge(RECEIVER_A), relay=True,
        actions=frozenset({Action.submit_credential, Action.cancel}),
    )
    qtbot.addWidget(dialog)
    field = dialog.findChild(QLineEdit, 'secretField')
    field.insert('  ')
    dialog.confirm_input()
    answer = dialog.take_result()
    assert answer is not None, 'the contract permits every nonempty representable credential'
    assert isinstance(answer.value, SensitiveText)
    assert answer.value.reveal_for_backend() == '  '
