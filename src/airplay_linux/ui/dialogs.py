"""Accessible, non-secret-leaking prompts for the operations dashboard.

Every prompt here is a *decision surface*, not a decision maker: the presenter
opens it with typed evidence (a pairing challenge, a set of sizes, a recovery
record) and reads back a single answer.  The dialogs never contact the backend,
never touch the keyring and never decide what to do next.

Secrets get special care.  A PIN or password lives only in the Qt line edit while
the prompt is open; :meth:`PairingDialog.take_input` hands back one
:class:`~airplay_linux.domain.operations.SensitiveText` and clears the widget in
the same call, so no item data, tooltip, label, window title or activity record
ever contains it.  The value is masked on screen and marked as sensitive input so
input methods do not keep a prediction history of it.

The safe answer is the default one everywhere it matters: ``Keep`` keeps the
session, ``Keep`` keeps the orphan output, and the resolution prompt commits
nothing until the user explicitly confirms it.

Only decisions that need a *typed input* or a *destructive confirmation* are
modal.  The design's state matrix presents the audio decision (``Retry audio`` /
``Start video only`` / ``Cancel``) and the cleanup recovery (``Retry cleanup`` /
``Keep for manual cleanup`` / ``Stop``) as explicit labelled choices inside the
selected pane, so those live in :mod:`airplay_linux.ui.window` next to the notice
that explains them instead of hiding behind another dialog.
"""

from __future__ import annotations

from typing import Final

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QHideEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from airplay_linux.domain.operations import ChallengeView, SensitiveText
from airplay_linux.domain.records import RecoveryItem
from airplay_linux.domain.resources import Mode, Size
from airplay_linux.ui.state import Action

_MODE_LABELS: dict[Mode, str] = {
    Mode.MIRROR: "Mirror",
    Mode.VIRTUAL: "Virtual display",
}

#: The only choices a pairing prompt may render.  The projection decides whether
#: each one is offered; the dialog never invents an extra command.
_PAIRING_ACTIONS: Final[frozenset[Action]] = frozenset(
    {Action.submit_pairing, Action.cancel, Action.retry}
)


class _DecisionDialog(QDialog):
    """Modal prompt scaffold: explanatory text plus explicit text buttons."""

    def __init__(self, title: str, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWindowTitle(title)
        self.setModal(True)
        # Prompts carry several explanatory sentences; keep them readable.
        self.setMinimumWidth(420)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 12, 16, 12)
        self._layout.setSpacing(8)
        self._layout.addWidget(self._heading(title))

    def _heading(self, text: str) -> QLabel:
        heading = QLabel(text, self)
        heading.setObjectName("promptHeading")
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        heading.setWordWrap(True)
        return heading

    def _body(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._layout.addWidget(label)
        return label

    def _button_row(
        self,
        *,
        confirm_text: str,
        confirm_name: str,
        confirm_slot: object,
        cancel_text: str,
        cancel_name: str,
        confirm_is_default: bool = True,
    ) -> QPushButton:
        row = QHBoxLayout()
        row.addStretch(1)
        confirm = QPushButton(confirm_text, self)
        confirm.setObjectName(confirm_name)
        confirm.setAccessibleName(confirm_text)
        confirm.setDefault(confirm_is_default)
        confirm.setAutoDefault(confirm_is_default)
        confirm.clicked.connect(confirm_slot)  # type: ignore[arg-type]
        cancel = QPushButton(cancel_text, self)
        cancel.setObjectName(cancel_name)
        cancel.setAccessibleName(cancel_text)
        cancel.setDefault(not confirm_is_default)
        cancel.setAutoDefault(not confirm_is_default)
        cancel.clicked.connect(self.reject)
        row.addWidget(confirm)
        row.addWidget(cancel)
        self._layout.addLayout(row)
        # The safe answer holds the initial focus when it is not the confirm one.
        (confirm if confirm_is_default else cancel).setFocus(Qt.FocusReason.OtherFocusReason)
        return confirm


class PairingDialog(_DecisionDialog):
    """Transient PIN or password entry for one receiver's pairing challenge.

    The prompt renders exactly the pairing choices its projection offered.  A
    pairing that has not retained an output may be retried blindly; one that
    already owns an output may only submit the challenge or cancel (which
    removes the output), so Retry is absent there.  Callers that hold the
    projection may pass ``actions`` explicitly instead of relying on that
    derivation.

    Every way of leaving this prompt - accept, ``done``, reject, Escape, the
    window close button, or a programmatic close/hide - wipes the line edit and
    its undo history.  Only an explicit submit hands the secret to
    :meth:`take_input`, exactly once.
    """

    retry_requested = Signal()

    def __init__(
        self,
        challenge: ChallengeView,
        receiver_label: str | None = None,
        retained_output: str | None = None,
        actions: frozenset[Action] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(challenge, ChallengeView):
            raise TypeError("PairingDialog requires a ChallengeView")
        super().__init__("Pair with receiver", "pairingDialog", parent)
        self._challenge = challenge
        self._input: SensitiveText | None = None
        self._actions = self._pairing_actions(actions, retained_output)
        self._submit: QPushButton | None = None
        self._pin = challenge.credential_kind == "pin"
        noun = "PIN" if self._pin else "password"

        identity = receiver_label or str(challenge.receiver_id)
        self._body(f"Pairing with {identity}.")
        self._body(
            f"Enter the {noun} shown by the receiver. The {noun} is used once and "
            "is never stored, displayed back, or written to the activity log."
        )
        self._body(self._expiry_text(challenge))
        if retained_output:
            self._body(
                f"The output {retained_output} is still owned by this session. "
                "Cancelling this prompt stops the attempt, and the retained output is "
                "then removed."
            )

        self._field = QLineEdit(self)
        self._field.setObjectName("secretField")
        self._field.setEchoMode(QLineEdit.EchoMode.Password)
        self._field.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText
        )
        self._field.setPlaceholderText(noun)
        self._field.setAccessibleName(f"Pairing {noun}")
        self._field.setToolTip(
            f"Type the {noun} the receiver is showing; it is used once and not saved"
        )
        self._field.textChanged.connect(self._sync_submit)
        self._layout.addWidget(self._field)

        self._build_actions(noun)
        self._sync_submit(self._field.text())

    # ------------------------------------------------------------------ public API

    def take_input(self) -> SensitiveText | None:
        """Hand the secret to the caller exactly once, then forget it."""
        value, self._input = self._input, None
        return value

    def confirm_input(self) -> None:
        """Accept the typed secret if there is one; blank input is not submitted."""
        text = self._field.text()
        if not text.strip():
            return
        self._input = SensitiveText(text)
        self._clear_field()
        self.accept()

    # ---------------------------------------------------- completion lifecycle

    def accept(self) -> None:  # noqa: D102 - Qt override
        # A direct accept completes the prompt without a submitted secret, so the
        # typed text must not survive the close.
        self._clear_field()
        super().accept()

    def reject(self) -> None:  # noqa: D102 - Qt override
        self._input = None
        self._clear_field()
        super().reject()

    def done(self, result: int) -> None:  # noqa: D102 - Qt override
        # Inherited completion paths (``done(Accepted)``/``done(Rejected)``) bypass
        # accept/reject entirely; clear there too so no exit retains the secret.
        # Only an explicit submit may leave its one-shot value pending.
        if int(result) != int(QDialog.DialogCode.Accepted):
            self._input = None
        self._clear_field()
        super().done(result)

    def close(self) -> bool:  # noqa: D102 - Qt override
        # ``QWidget.close`` on a dialog that has never been shown does not deliver
        # a close event, so clear before delegating rather than only in closeEvent.
        # Closing also abandons any submit that was never taken.
        self._input = None
        self._clear_field()
        return super().close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._input = None
        self._clear_field()
        super().closeEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt override
        # A presenter may hide the prompt instead of closing it.  The one-shot
        # handoff from an explicit submit survives, because accepting the dialog
        # hides it before the presenter calls take_input.
        self._clear_field()
        super().hideEvent(event)

    # ---------------------------------------------------------------- internals

    def _build_actions(self, noun: str) -> None:
        """Render exactly the offered pairing choices, in reading order."""
        row = QHBoxLayout()
        row.addStretch(1)
        first: QPushButton | None = None
        if Action.submit_pairing in self._actions:
            submit = QPushButton(f"Submit {noun}", self)
            submit.setObjectName("pairingSubmitButton")
            submit.setAccessibleName(f"Submit {noun}")
            submit.setDefault(True)
            submit.setAutoDefault(True)
            submit.clicked.connect(self.confirm_input)
            row.addWidget(submit)
            self._submit = submit
            first = submit
        if Action.cancel in self._actions:
            cancel = QPushButton("Cancel", self)
            cancel.setObjectName("pairingCancelButton")
            cancel.setAccessibleName("Cancel")
            cancel.setDefault(False)
            cancel.setAutoDefault(False)
            cancel.clicked.connect(self.reject)
            row.addWidget(cancel)
            if first is None:
                first = cancel
        if Action.retry in self._actions:
            retry = QPushButton("Retry", self)
            retry.setObjectName("pairingRetryButton")
            retry.setAccessibleName("Retry pairing")
            retry.setToolTip("Retry pairing with a fresh PIN or password")
            retry.setDefault(False)
            retry.setAutoDefault(False)
            retry.clicked.connect(self._retry)
            row.addWidget(retry)
            if first is None:
                first = retry
        self._layout.addLayout(row)
        if first is not None:
            first.setFocus(Qt.FocusReason.OtherFocusReason)

    def _retry(self) -> None:
        """Abandon this attempt and ask the presenter for a fresh challenge."""
        self.reject()
        self.retry_requested.emit()

    @staticmethod
    def _pairing_actions(
        actions: frozenset[Action] | None, retained_output: str | None
    ) -> frozenset[Action]:
        if actions is None:
            # Mirrors the projection's pairing branch: a blind retry is not the
            # design's action while an output is still owned.
            if retained_output is None:
                return frozenset(
                    {Action.submit_pairing, Action.cancel, Action.retry}
                )
            return frozenset({Action.submit_pairing, Action.cancel})
        resolved = frozenset(actions)
        if not resolved:
            raise ValueError("PairingDialog needs at least one action")
        unknown = resolved - _PAIRING_ACTIONS
        if unknown:
            names = ", ".join(sorted(action.value for action in unknown))
            raise ValueError(f"PairingDialog cannot offer {names}")
        return resolved

    def _sync_submit(self, text: str) -> None:
        if self._submit is not None:
            self._submit.setEnabled(bool(text.strip()))

    def _clear_field(self) -> None:
        # ``setReadOnly`` also drops the widget's undo/redo history, so Ctrl+Z in a
        # stale prompt can never bring a typed secret back onto the screen.
        self._field.blockSignals(True)
        self._field.setReadOnly(True)
        self._field.clear()
        self._field.blockSignals(False)
        if self._submit is not None:
            self._submit.setEnabled(False)

    @staticmethod
    def _expiry_text(challenge: ChallengeView) -> str:
        budget = max(0, int(round(challenge.expires_at - challenge.created_at)))
        return (
            f"This entry expires {budget} seconds after the receiver requested it. "
            "Retry with a new PIN or password if it has expired."
        )


class ConfirmStopDialog(_DecisionDialog):
    """Stop-before-switch confirmation; nothing is stopped without an explicit yes."""

    def __init__(self, next_mode: Mode, parent: QWidget | None = None) -> None:
        if not isinstance(next_mode, Mode):
            raise TypeError("ConfirmStopDialog requires a Mode")
        super().__init__("Stop and switch mode", "confirmStopDialog", parent)
        self._confirmed = False
        label = _MODE_LABELS[next_mode]
        self._body(
            f"Switching this receiver to {label} requires stopping the current session "
            "first."
        )
        self._body(
            "Stopping removes everything this session owns: the stream, audio control "
            "and any virtual output. The new mode starts only after cleanup is "
            "confirmed, so Mirror and Virtual display never run at the same time for "
            "one receiver."
        )
        self._button_row(
            confirm_text="Stop and switch",
            confirm_name="confirmStopButton",
            confirm_slot=self._confirm,
            cancel_text="Keep current session",
            cancel_name="keepSessionButton",
            confirm_is_default=False,
        )

    def confirmed(self) -> bool:
        """Whether the user chose Stop; the default answer keeps the session."""
        return self._confirmed

    def _confirm(self) -> None:
        self._confirmed = True
        self.accept()

    def reject(self) -> None:  # noqa: D102 - Qt override
        self._confirmed = False
        super().reject()


class ResolutionDialog(_DecisionDialog):
    """Explicit output-size decision; the choice is never inferred from a guess."""

    def __init__(self, choices: tuple[Size, ...], parent: QWidget | None = None) -> None:
        super().__init__("Choose output size", "resolutionDialog", parent)
        for choice in choices:
            if not isinstance(choice, Size):
                raise TypeError("ResolutionDialog choices must be Size values")
        self._decision: Size | None = None
        self._body(
            "The receiver did not provide a reliable output size, so this decision is "
            "yours. Choose the size to create for this receiver."
        )
        self._body(
            "The desktop layout may change temporarily while the virtual output is "
            "created, and it is restored only when that is safe."
        )

        self._combo = QComboBox(self)
        self._combo.setObjectName("resolutionChoice")
        self._combo.setAccessibleName("Output size")
        self._combo.setToolTip("Choose the size of the virtual output for this receiver")
        for choice in choices:
            self._combo.addItem(f"{choice.width}x{choice.height}", choice)
        if len(choices) > 1:
            self._combo.setCurrentIndex(0)
        self._layout.addWidget(self._combo)

        confirm = self._button_row(
            confirm_text="Use this size",
            confirm_name="confirmResolutionButton",
            confirm_slot=self._confirm,
            cancel_text="Cancel",
            cancel_name="cancelResolutionButton",
        )
        confirm.setEnabled(bool(choices))

    def decision(self) -> Size | None:
        """The explicitly confirmed size, or ``None`` while undecided."""
        return self._decision

    def _confirm(self) -> None:
        chosen = self._combo.currentData()
        if not isinstance(chosen, Size):
            return
        self._decision = chosen
        self.accept()

    def reject(self) -> None:  # noqa: D102 - Qt override
        self._decision = None
        super().reject()


class OrphanDialog(_DecisionDialog):
    """Ownership evidence for a recovered output, with Keep as the safe default."""

    def __init__(self, item: RecoveryItem, parent: QWidget | None = None) -> None:
        if not isinstance(item, RecoveryItem):
            raise TypeError("OrphanDialog requires a RecoveryItem")
        super().__init__("Orphan output found", "orphanDialog", parent)
        self._item = item
        self._confirmed = False
        record = item.record

        self._body(
            "This output is not confirmed as owned by this session and is never "
            "removed automatically. Check the evidence below, then remove it or keep "
            "it; removing it is your explicit decision."
        )

        evidence = [
            f"Connector: {record.connector or 'unknown connector'}",
            f"Output ID: {record.output_id or 'unknown output'}",
            f"Owner: {record.owner_uuid}",
            f"Receiver: {record.receiver_id}",
            f"Requested size: {record.requested_size.width}x{record.requested_size.height}",
            f"Observed size: "
            + (
                f"{record.observed_size.width}x{record.observed_size.height}"
                if record.observed_size is not None
                else "not observed"
            ),
            _helper_text(item),
            f"Reservation stage: {record.stage}",
        ]
        for line in evidence:
            entry = self._body(line)
            entry.setObjectName("orphanEvidence")

        self._button_row(
            confirm_text="Remove output",
            confirm_name="confirmOrphanButton",
            confirm_slot=self._confirm,
            cancel_text="Keep output",
            cancel_name="keepOrphanButton",
            confirm_is_default=False,
        )

    def confirmed(self) -> bool:
        """Whether the user explicitly confirmed removing the orphan output."""
        return self._confirmed

    def _confirm(self) -> None:
        self._confirmed = True
        self.accept()

    def reject(self) -> None:  # noqa: D102 - Qt override
        self._confirmed = False
        super().reject()


def _helper_text(item: RecoveryItem) -> str:
    helper = item.record.helper
    if helper is None:
        return "Helper: no recorded helper process; nothing to stop for this output"
    handle = item.handle
    if handle is not None and handle.helper == helper:
        return (
            f"Helper: pid {helper.pid}, start ticks {helper.start_ticks} "
            "(exact process identity recorded)"
        )
    return f"Helper: pid {helper.pid}, start ticks {helper.start_ticks}"


__all__ = [
    "ConfirmStopDialog",
    "OrphanDialog",
    "PairingDialog",
    "ResolutionDialog",
]
