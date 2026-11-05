"""Ordered metadata writes with a safe snapshot boundary before custom stores."""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import replace
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import EffectEvent
from airplay_linux.domain.operations import OperationId
from airplay_linux.domain.ports import ManagerPort, SessionStore
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import AudioState, Err, ErrorCode, Ok, Result, TrustState
from airplay_linux.storage.codecs import decode_session, encode_session, is_ambiguous_identity

from .engine import SessionManager


def _safe_snapshot(state: SessionSnapshot) -> SessionSnapshot:
    # Never retain a secret-bearing ChallengeView substitute, portal token or
    # old stream authority, even when an injected store has no JSON codec. A
    # pending challenge becomes the durable receiver-scoped revocation barrier.
    clean = replace(
        state, challenge=None, stream=None, transport_lease_id=None,
        audio_operation_id=None, audio=AudioState.Disabled, muted=False, next_retry_at=None,
        remembered_volume=None, effective_volume=None, last_audible_volume=None,
        volume_state=None, audio_close_pending=False, volume_save_failed=False,
        trust=TrustState.CleanupRequired if state.trust is TrustState.Pairing else state.trust,
    )
    # Reuse storage's explicit allowlist for nested sources, errors and requests;
    # this also validates values and detaches all mutable record projections.
    return decode_session(encode_session(clean))


class SessionPersistence:
    def __init__(self, manager: ManagerPort, store: SessionStore) -> None:
        self.manager, self.store = manager, store
        self._subscription = None
        self._closed = False
        self._reporting = False
        self._queues = defaultdict(deque)
        self._workers: dict[object, asyncio.Task] = {}
        self._latest: dict[object, SessionSnapshot] = {}
        self._failures: dict[object, Err] = {}

    def start(self) -> None:
        if self._subscription is not None or self._closed:
            return
        self._subscription = self.manager.subscribe(self.record)
        if isinstance(self.manager, SessionManager):
            for state in tuple(self.manager._states.values()):
                self.record(state)

    def record(self, snapshot: SessionSnapshot) -> None:
        """Queue one actor publication, also usable for the final shutdown view."""
        if self._closed or self._reporting or is_ambiguous_identity(snapshot.receiver_id):
            return
        try:
            state = _safe_snapshot(snapshot)
        except Exception:
            self._failed(snapshot)
            return
        self._latest[state.receiver_id] = state
        self._queues[state.receiver_id].append(state)
        self._start_worker(state.receiver_id)

    def _start_worker(self, receiver_id) -> None:
        if receiver_id not in self._workers:
            self._workers[receiver_id] = asyncio.create_task(self._write(receiver_id))

    async def _write(self, receiver_id) -> None:
        try:
            queue = self._queues[receiver_id]
            while queue:
                state = queue.popleft()
                try:
                    result = await self.store.save(state)
                except Exception:  # fixed error, never backend text
                    result = Err(error=diagnostic(ErrorCode.persistence_failed, receiver_id))
                if not isinstance(result, Ok):
                    self._failed(state)
                else:
                    self._failures.pop(receiver_id, None)
        finally:
            self._workers.pop(receiver_id, None)

    def _failed(self, state: SessionSnapshot) -> None:
        failure = Err(error=diagnostic(ErrorCode.persistence_failed, state.receiver_id))
        self._failures[state.receiver_id] = failure
        current = self.manager.snapshot(state.receiver_id)
        self._reporting = True
        try:
            self.manager.ingest(EffectEvent(
                receiver_id=state.receiver_id,
                operation_id=current.operation_id or OperationId(uuid4()),
                session_generation=current.session_generation,
                kind="recovery:persistence_failed", result=failure, resource=None,
            ))
        finally:
            self._reporting = False

    async def flush(self) -> Result[None]:
        # A failed write is retried only at an explicit flush (or a later actor
        # publication). Reporting the diagnostic never recursively writes it.
        for receiver_id in tuple(self._failures):
            state = self._latest.get(receiver_id)
            if state is not None and receiver_id not in self._workers:
                self._queues[receiver_id].append(state)
                self._start_worker(receiver_id)
        while self._workers:
            await asyncio.gather(*(asyncio.shield(task) for task in tuple(self._workers.values())))
        if self._failures:
            return next(iter(self._failures.values()))
        return Ok(value=None)

    async def close(self) -> Result[None]:
        if self._subscription is not None:
            self._subscription.close()
            self._subscription = None
        # Queued writes remain live when a deadline cancels the closer. Atomic
        # store publication must not be replaced by a claim that it finished.
        self._closed = True
        return await self.flush()


__all__ = ["SessionPersistence"]
