"""Receiver table model for the dense operations dashboard.

The model is a *view of a decision that has already been made*.  Every cell is
copied verbatim from a :class:`~airplay_linux.ui.state.ReceiverRow`, which the
pure projection produced from the session's independent resource facts.  The
model therefore never derives a phase, an action set, an output size or a trust
label of its own: if the projection says ``Sideways``, the table says
``Sideways``.

Accessibility is part of the contract rather than an afterthought.  Text that is
blank for layout reasons (a Mirror session has no output size) still carries a
spoken :data:`Qt.ItemDataRole.AccessibleTextRole` value such as ``"No output
size"``, so an empty cell is never a mystery to a screen reader.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.ui.state import ReceiverRow

COLUMNS: Final[tuple[str, ...]] = ("Device", "Mode", "State", "Size")

_DEVICE: Final[int] = 0
_MODE: Final[int] = 1
_STATE: Final[int] = 2
_SIZE: Final[int] = 3

_NO_MODE: Final[str] = "No active mode"
_NO_STATE: Final[str] = "No state reported"
_NO_SIZE: Final[str] = "No output size"


class ReceiverListModel(QAbstractTableModel):
    """One dense row per receiver: identity, mode, phase and output size.

    ``replace`` publishes a whole snapshot as a single atomic reset, so the view
    can never observe a half-updated list (for example new names with old
    phases).  Callers pass the projection's rows; nothing else is accepted.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[ReceiverRow, ...] = ()

    # ------------------------------------------------------------------ contents

    @property
    def rows(self) -> tuple[ReceiverRow, ...]:
        """The currently rendered rows, unchanged from the projection."""
        return self._rows

    def replace(self, rows: tuple[ReceiverRow, ...]) -> None:
        """Render exactly ``rows``, replacing the previous list atomically.

        Values are validated before the reset begins: a rejected list leaves the
        previous rows - and the selection built on them - intact.
        """
        if isinstance(rows, (str, bytes, bytearray)) or not isinstance(rows, Iterable):
            raise TypeError("rows must be a sequence of ReceiverRow values")
        normalized = tuple(rows)
        for row in normalized:
            if not isinstance(row, ReceiverRow):
                raise TypeError("rows must contain only ReceiverRow values")
        self.beginResetModel()
        self._rows = normalized
        self.endResetModel()

    def receiver_id(self, index: QModelIndex) -> ReceiverId | None:
        """The projection's receiver identity for ``index``, or ``None``."""
        if index.model() is not self or not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._rows):
            return None
        return self._rows[row].receiver_id

    def index_of(self, receiver_id: ReceiverId) -> QModelIndex:
        """The first column's index for ``receiver_id``, or an invalid index."""
        for row, candidate in enumerate(self._rows):
            if candidate.receiver_id == receiver_id:
                return self.index(row, _DEVICE)
        return QModelIndex()

    # --------------------------------------------------------------- Qt overrides

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        row = index.row()
        if not 0 <= row < len(self._rows):
            return None
        entry = self._rows[row]
        column = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(entry, column)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return self._spoken(entry, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(entry)
        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(COLUMNS):
                return COLUMNS[section]
            return None
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ------------------------------------------------------------------- rendering

    @staticmethod
    def _display(row: ReceiverRow, column: int) -> str:
        """The literal projection value for one column; empty means "not known"."""
        if column == _DEVICE:
            return row.name
        if column == _MODE:
            return row.mode_label
        if column == _STATE:
            return row.phase_label
        if column == _SIZE:
            return row.size_label
        return ""

    @staticmethod
    def _spoken(row: ReceiverRow, column: int) -> str:
        """A never-blank description for assistive technology."""
        if column == _DEVICE:
            return f"{row.name}, {'online' if row.online else 'offline'}"
        if column == _MODE:
            return row.mode_label or _NO_MODE
        if column == _STATE:
            return row.phase_label or _NO_STATE
        if column == _SIZE:
            return row.size_label or _NO_SIZE
        return ""

    @staticmethod
    def _tooltip(row: ReceiverRow) -> str:
        """One sentence describing the whole row, including online/offline."""
        parts = [
            row.name,
            "online" if row.online else "offline",
            row.mode_label or _NO_MODE,
            row.phase_label or _NO_STATE,
            row.size_label or _NO_SIZE,
        ]
        return " · ".join(parts)


__all__ = ["COLUMNS", "ReceiverListModel"]
