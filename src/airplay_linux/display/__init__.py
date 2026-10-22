"""The KDE display adapters: KScreen observation and owned virtual helpers.

Import :class:`KScreenObserver` for read-only layout evidence and verified
configuration, and :class:`VirtualHelper` for a version-checked, owner-evidenced
``krfb-virtualmonitor`` process per virtual-display reservation.
"""

from __future__ import annotations

from .helper import (
    DOCUMENTED_HELPER_FLAGS,
    HELPER_COMMAND,
    HELPER_TIMEOUT,
    REQUIRED_HELPER_FLAGS,
    HelperFeatures,
    VirtualHelper,
)
from .kscreen import (
    CONFIGURABLE_SETTINGS,
    DEFAULT_POLL_INTERVAL,
    KSCREEN_COMMAND,
    KSCREEN_TIMEOUT,
    KScreenMode,
    KScreenObserver,
    KScreenParseError,
    OutputInfo,
    layout_hash,
    parse_outputs,
    select_mode,
)

__all__ = [
    "CONFIGURABLE_SETTINGS",
    "DEFAULT_POLL_INTERVAL",
    "DOCUMENTED_HELPER_FLAGS",
    "HELPER_COMMAND",
    "HELPER_TIMEOUT",
    "HelperFeatures",
    "KSCREEN_COMMAND",
    "KSCREEN_TIMEOUT",
    "KScreenMode",
    "KScreenObserver",
    "KScreenParseError",
    "OutputInfo",
    "REQUIRED_HELPER_FLAGS",
    "VirtualHelper",
    "layout_hash",
    "parse_outputs",
    "select_mode",
]
