"""Cleanup outcome persistence cannot authorize destructive teardown."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

from airplay_linux.domain.records import DaemonCleanupIntent
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.teardown import CleanupOutcomeWriter


class RecordingCleanupStore:
    def __init__(self) -> None:
        self.records: list[DaemonCleanupIntent] = []
        self.writes: list[DaemonCleanupIntent] = []
        self.failed_receipt_bytes = b'{"state":"failed","immutable":true}'

    async def all(self):
        return Ok(value=tuple(self.records))

    async def write(self, record: DaemonCleanupIntent):
        self.writes.append(record)
        previous = next(
            (item for item in self.records if item.launch_uuid == record.launch_uuid),
            None,
        )
        if previous is None:
            if record.completed_at is not None or record.outcome is not None:
                return Err(error=_persistence_error())
        elif previous == record:
            return Ok(value=None)
        elif (
            previous.launch_uuid != record.launch_uuid
            or previous.reason != record.reason
            or previous.intent_at != record.intent_at
        ):
            return Err(error=_persistence_error())
        elif previous.completed_at is not None:
            return Err(error=_persistence_error())
        self.records = [
            item for item in self.records if item.launch_uuid != record.launch_uuid
        ] + [record]
        return Ok(value=None)


def _persistence_error():
    from airplay_linux.diagnostics import diagnostic

    return diagnostic(ErrorCode.persistence_failed)


def test_cleanup_outcome_writer_cannot_authorize_signal_or_spawn():
    store = RecordingCleanupStore()
    writer = CleanupOutcomeWriter(store)
    intent = DaemonCleanupIntent(
        launch_uuid=UUID("11111111-1111-1111-1111-111111111111"),
        reason=ErrorCode.daemon_failed,
        intent_at=10.0,
        completed_at=None,
        outcome=None,
    )

    assert asyncio.run(writer.record(intent)) == Ok(value=None)
    assert asyncio.run(writer.record(intent)) == Ok(value=None)
    assert asyncio.run(
        writer.complete(intent, completed_at=11.0, outcome="stopped")
    ) == Ok(value=None)
    assert asyncio.run(
        writer.complete(intent, completed_at=11.0, outcome="stopped")
    ) == Ok(value=None)

    changed_reason = replace(intent, reason=ErrorCode.cleanup_timeout)
    assert isinstance(asyncio.run(writer.record(changed_reason)), Err)
    assert isinstance(
        asyncio.run(
            writer.complete(intent, completed_at=12.0, outcome=ErrorCode.cleanup_timeout)
        ),
        Err,
    )

    assert len(store.records) == 1
    assert store.records[0].completed_at == 11.0
    assert store.records[0].outcome == "stopped"
    assert store.failed_receipt_bytes == b'{"state":"failed","immutable":true}'
    # The writer has no process or filesystem mutation capability beyond this
    # store's two immutable lifecycle writes.
    assert not hasattr(writer, "signal")
    assert not hasattr(writer, "spawn")
    assert not hasattr(writer, "unlink")
    assert len(store.writes) == 4
