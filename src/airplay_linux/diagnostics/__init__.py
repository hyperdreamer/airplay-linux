"""Safe diagnostics and user recovery messages.

Import the catalog builder for user-facing errors and the activity channel for
timestamped, secret-free lifecycle records.
"""

from __future__ import annotations

from .activity import ALLOWED_FIELDS, MAX_FIELD_TEXT, ActivityLog, safe_activity
from .catalog import ALLOWED_COMMANDS, ENTRIES, CatalogEntry, diagnostic

__all__ = [
    "ALLOWED_COMMANDS",
    "ALLOWED_FIELDS",
    "ActivityLog",
    "CatalogEntry",
    "ENTRIES",
    "MAX_FIELD_TEXT",
    "diagnostic",
    "safe_activity",
]
