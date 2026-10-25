"""Pure, Qt-free dashboard projection.

Import the projection from :mod:`airplay_linux.ui.state`; the PySide6 widgets and
the presenter are separate modules so this layer stays deterministic and
importable without a display server.
"""

from __future__ import annotations

from .state import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_OK,
    SEVERITY_WARNING,
    Action,
    Badge,
    PaneState,
    ReceiverRow,
    ViewInputs,
    project,
    project_row,
)

__all__ = [
    "Action",
    "Badge",
    "PaneState",
    "ReceiverRow",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_OK",
    "SEVERITY_WARNING",
    "ViewInputs",
    "project",
    "project_row",
]
