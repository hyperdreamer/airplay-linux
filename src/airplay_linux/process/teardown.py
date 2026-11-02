"""Durable cleanup outcome publication with no teardown authority."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.records import DaemonCleanupIntent
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result

if TYPE_CHECKING:
    from airplay_linux.domain.ports import DaemonCleanupIntentStore


class CleanupOutcomeWriter:
    """Publish one cleanup intent and at most one immutable completion.

    The writer only talks to the frozen persistence port. It cannot signal,
    spawn, unlink, or turn a failed cleanup into authorization for a later
    destructive operation. Repeating an identical write is an idempotent
    durability recheck; changing any already published field is refused.
    """

    def __init__(self, store: DaemonCleanupIntentStore) -> None:
        self._store = store

    async def record(self, intent: DaemonCleanupIntent) -> Result[None]:
        if not _valid_initial(intent):
            return _refused()
        current = await self._current(intent.launch_uuid)
        if isinstance(current, Err):
            return current
        if current.value is not None and current.value != intent:
            return _refused()
        return await self._store.write(intent)

    async def complete(
        self,
        intent: DaemonCleanupIntent,
        *,
        completed_at: float,
        outcome: Literal["stopped"] | ErrorCode,
    ) -> Result[None]:
        if (
            not _valid_initial(intent)
            or type(completed_at) not in (int, float)
            or isinstance(completed_at, bool)
            or not math.isfinite(completed_at)
            or completed_at < intent.intent_at
            or not _valid_outcome(outcome)
        ):
            return _refused()
        current = await self._current(intent.launch_uuid)
        if isinstance(current, Err):
            return current
        if current.value is None:
            return _refused()
        completion = replace(
            intent, completed_at=float(completed_at), outcome=outcome,
        )
        if current.value == completion:
            return await self._store.write(completion)
        if current.value != intent:
            # This includes an already completed record with different
            # completion data and every changed initial field.
            return _refused()
        return await self._store.write(completion)

    async def _current(
        self, launch_uuid,
    ) -> Result[DaemonCleanupIntent | None]:
        records = await self._store.all()
        if isinstance(records, Err):
            return records
        matches = tuple(item for item in records.value if item.launch_uuid == launch_uuid)
        if len(matches) > 1:
            return _refused()
        return Ok(value=matches[0] if matches else None)


def _valid_initial(intent: object) -> bool:
    return (
        isinstance(intent, DaemonCleanupIntent)
        and isinstance(intent.launch_uuid, UUID)
        and intent.completed_at is None
        and intent.outcome is None
        and isinstance(intent.reason, ErrorCode)
        and type(intent.intent_at) in (int, float)
        and not isinstance(intent.intent_at, bool)
        and math.isfinite(intent.intent_at)
    )


def _valid_outcome(outcome: object) -> bool:
    return outcome == "stopped" or isinstance(outcome, ErrorCode)


def _refused() -> Err:
    return Err(error=diagnostic(ErrorCode.persistence_failed))
