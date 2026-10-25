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
"""

from __future__ import annotations

from PySide6.QtCore import Qt
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

_MODE_LABELS: dict[Mode, str] = {
    Mode.MIRROR: "Mirror",
    Mode.VIRTUAL: "Virtual display",
}


class _DecisionDialog(QDialog):
    """Modal prompt scaffold: explanatory text plus explicit text buttons."""

    def __init__(self, title: str, object_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setWindowTitle(title)
        self.setModal(True)
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
    """Transient PIN or password entry for one receiver's pairing challenge."""

    def __init__(
        self,
        challenge: ChallengeView,
        receiver_label: str | None = None,
        retained_output: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        if not isinstance(challenge, ChallengeView):
            raise TypeError("PairingDialog requires a ChallengeView")
        super().__init__("Pair with receiver", "pairingDialog", parent)
        self._challenge = challenge
        self._input: SensitiveText | None = None
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

        self._submit = self._button_row(
            confirm_text=f"Submit {noun}",
            confirm_name="pairingSubmitButton",
            confirm_slot=self.confirm_input,
            cancel_text="Cancel",
            cancel_name="pairingCancelButton",
        )
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

    def reject(self) -> None:  # noqa: D102 - Qt override
        self._input = None
        self._clear_field()
        super().reject()

    # ---------------------------------------------------------------- internals

    def _sync_submit(self, text: str) -> None:
        self._submit.setEnabled(bool(text.strip()))

    def _clear_field(self) -> None:
        # ``setReadOnly`` also drops the widget's undo/redo history, so Ctrl+Z in a
        # stale prompt can never bring a typed secret back onto the screen.
        self._field.blockSignals(True)
        self._field.setReadOnly(True)
        self._field.clear()
        self._field.blockSignals(False)
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
