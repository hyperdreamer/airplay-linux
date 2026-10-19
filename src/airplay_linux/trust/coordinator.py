"""The sole trust transaction authority, fenced before and after every acquisition."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Awaitable

from airplay_linux.backend.control import BackendControl, BackendOperation, PairingPayload
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.operations import (
    ChallengeId,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationContext,
    PairingChallenge,
    PairingResult,
    SensitiveText,
)
from airplay_linux.domain.ports import CancelHandle, Clock, CredentialStore, IdFactory, Scheduler
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.storage import JsonReceiverStore

from .fences import TrustFenceRegistry, fence_matches
from .keyring import KeyringPairing, KeyringPairingPort, _acknowledged

PAIRING_LIFETIME = 120.0
CONTROL_TIMEOUT = 5.0
MAX_INVALID_ATTEMPTS = 3


@dataclass(kw_only=True, repr=False)
class _Attempt:
    target: ReceiverTarget
    context: OperationContext
    created_at: float
    challenge: PairingChallenge | None = None
    expiry: CancelHandle | None = None
    invalid_submissions: int = 0
    submitting: bool = False
    retired: bool = False
    cancel_confirmed: bool = False
    needs_revoke: bool = False
    provisional: OpaqueProvisionalCredentialRef | None = None
    provisional_revoked: bool = False
    commit_started: bool = False
    credential: OpaqueCredentialRef | None = None


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _pairing(kind: str, code: ErrorCode) -> PairingResult:
    return PairingResult(kind=kind, credential_ref=None, diagnostic=diagnostic(code))


async def _safe(operation: Awaitable):
    """Rebuild every external failure from a closed code, never its text."""
    try:
        result = await operation
    except Exception:
        return _failure(ErrorCode.keyring_unavailable)
    if isinstance(result, Ok):
        return result
    if isinstance(result, Err) and type(result.error.code) is ErrorCode:
        return _failure(result.error.code)
    return _failure(ErrorCode.keyring_unavailable)


class PairingCoordinator:
    """No receiver lock is held across I/O.

    Invalidation is synchronous. An in-flight acquisition remains observed even
    after its waiter cancels or its deadline expires; the receiver barrier waits
    for that outcome before the final compensating revoke. Pending cleanup is
    never inferred complete from a cancelled coroutine.
    """

    def __init__(
        self, control: BackendControl, credentials: CredentialStore,
        receiver_store: JsonReceiverStore, fences: TrustFenceRegistry,
        clock: Clock, scheduler: Scheduler, ids: IdFactory,
    ) -> None:
        self._control = control
        self._credentials = credentials
        self._receiver_store = receiver_store
        self._fences = fences
        self._clock = clock
        self._scheduler = scheduler
        self._ids = ids
        self._attempts: dict[ReceiverId, list[_Attempt]] = {}
        self._challenges: dict[ChallengeId, _Attempt] = {}
        self._operations: dict[ReceiverId, set[asyncio.Task]] = {}
        self._cleanups: dict[ReceiverId, asyncio.Task] = {}
        self._cleanup_required: set[ReceiverId] = set()
        self._forgetting: set[ReceiverId] = set()
        self._invalidating: set[ReceiverId] = set()
        self._revoke_receivers: set[ReceiverId] = set()

    def __repr__(self) -> str:
        return "PairingCoordinator(transactions=<redacted>)"

    def pending_cleanup(self, receiver_id: ReceiverId) -> bool:
        return receiver_id in self._cleanup_required or any(
            attempt.retired or not self._current(attempt)
            for attempt in self._attempts.get(receiver_id, ())
        )

    def _target_current(self, target: ReceiverTarget, context: OperationContext) -> bool:
        fence = self._fences.get(target.receiver_id)
        return fence is not None and (
            fence.endpoint_fingerprint == target.endpoint_fingerprint
            and fence.endpoint_generation == target.endpoint_generation == context.endpoint_generation
            and fence.session_generation == context.session_generation
            and fence.trust_generation == (context.trust_generation or 0)
        )

    def _current(self, attempt: _Attempt) -> bool:
        if attempt.retired:
            return False
        if attempt.challenge is None:
            return self._target_current(attempt.target, attempt.context)
        fence = self._fences.get(attempt.target.receiver_id)
        return fence is not None and fence_matches(attempt.challenge, fence, self._clock.now())

    def _tracked(self, attempt: _Attempt) -> bool:
        return any(item is attempt for item in self._attempts.get(attempt.target.receiver_id, ()))

    def _retire(self, receiver_id: ReceiverId) -> None:
        attempts = self._attempts.get(receiver_id, ())
        invalidate = receiver_id not in self._cleanup_required or any(not item.retired for item in attempts)
        self._cleanup_required.add(receiver_id)
        for attempt in attempts:
            attempt.retired = True
            if attempt.expiry is not None:
                attempt.expiry.cancel()
        if invalidate:
            fence = self._fences.get(receiver_id)
            if fence is not None:
                self._fences.set(replace(fence, trust_generation=fence.trust_generation + 1, active_challenge_id=None))

    def _discard(self, attempt: _Attempt) -> None:
        receiver_id = attempt.target.receiver_id
        if attempt.expiry is not None:
            attempt.expiry.cancel()
        if attempt.challenge is not None:
            self._challenges.pop(attempt.challenge.challenge_id, None)
            self._fences.clear_challenge(receiver_id, attempt.challenge.challenge_id)
        attempts = self._attempts.get(receiver_id, [])
        if attempt in attempts:
            attempts.remove(attempt)
        if not attempts:
            self._attempts.pop(receiver_id, None)

    def _spawn(self, receiver_id: ReceiverId, operation: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(operation)
        self._operations.setdefault(receiver_id, set()).add(task)

        def finished(completed: asyncio.Task) -> None:
            self._operations[receiver_id].discard(completed)
            # Consume an unexpected failure even when the original waiter left.
            if not completed.cancelled():
                completed.exception()
            if self.pending_cleanup(receiver_id):
                self._ensure_cleanup(receiver_id)

        task.add_done_callback(finished)
        return task

    async def _wait(self, task: asyncio.Task, receiver_id: ReceiverId):
        """Bound the waiter while retaining ownership of an unfinished operation."""
        attempts = tuple(self._attempts.get(receiver_id, ()))
        loop = asyncio.get_running_loop()
        deadline = loop.create_future()

        def timed_out() -> None:
            if task.done() or deadline.done():
                return
            self._retire(receiver_id)
            self._ensure_cleanup(receiver_id)
            deadline.set_result(None)

        scheduled = self._scheduler.call_at(self._clock.now() + CONTROL_TIMEOUT, timed_out)
        realtime = loop.call_later(CONTROL_TIMEOUT, timed_out)
        try:
            done, _ = await asyncio.wait((task, deadline), return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            return None
        except asyncio.CancelledError:
            if not task.done() or any(self._tracked(attempt) for attempt in attempts):
                self._retire(receiver_id)
                self._ensure_cleanup(receiver_id)
            raise
        finally:
            scheduled.cancel()
            realtime.cancel()
            deadline.cancel()

    def _ensure_cleanup(self, receiver_id: ReceiverId, *, retry: bool = False) -> asyncio.Task:
        task = self._cleanups.get(receiver_id)
        if task is None or (task.done() and (retry or (not task.cancelled() and isinstance(task.result(), Ok)))):
            task = asyncio.create_task(self._cleanup(receiver_id))
            self._cleanups[receiver_id] = task
        return task

    async def _finish_cleanup(self, receiver_id: ReceiverId, *, retry: bool = False) -> Result[None]:
        task = self._ensure_cleanup(receiver_id, retry=retry)
        result = await self._wait(task, receiver_id)
        return result if isinstance(result, Ok) else _failure(ErrorCode.trust_cleanup_required)

    async def begin_pairing(self, target: ReceiverTarget, context: OperationContext) -> Result[PairingChallenge]:
        receiver_id = target.receiver_id
        if self.pending_cleanup(receiver_id):
            return _failure(ErrorCode.trust_cleanup_required)
        if not isinstance(self._credentials, KeyringPairingPort):
            return _failure(ErrorCode.keyring_unavailable)
        fence = self._fences.get(receiver_id)
        if not self._target_current(target, context) or fence.active_challenge_id is not None or self._attempts.get(receiver_id):
            return _failure(ErrorCode.stale_target)
        attempt = _Attempt(target=target, context=context, created_at=self._clock.now())
        self._attempts.setdefault(receiver_id, []).append(attempt)
        result = await self._wait(self._spawn(receiver_id, self._begin(attempt)), receiver_id)
        if not self._tracked(attempt):
            return _failure(ErrorCode.stale_event)
        if result is None:
            return _failure(ErrorCode.trust_cleanup_required)
        if isinstance(result, Ok) and self._current(attempt):
            return result
        if attempt.retired or attempt.needs_revoke or attempt.challenge is not None:
            self._retire(receiver_id)
            cleanup = await self._finish_cleanup(receiver_id)
            if isinstance(cleanup, Err):
                return cleanup
        else:
            self._discard(attempt)
        return result if isinstance(result, Err) else _failure(ErrorCode.stale_event)

    async def _begin(self, attempt: _Attempt) -> Result[PairingChallenge]:
        result = await _safe(self._credentials.begin_pairing_token(attempt.target, attempt.context))
        if isinstance(result, Err):
            if result.error.code is ErrorCode.trust_cleanup_required:
                attempt.needs_revoke = True
                self._retire(attempt.target.receiver_id)
            return result
        if not isinstance(result.value, KeyringPairing) or result.value.credential_kind not in ("pin", "password"):
            attempt.needs_revoke = True
            self._retire(attempt.target.receiver_id)
            return _failure(ErrorCode.keyring_unavailable)
        challenge = PairingChallenge(
            challenge_id=ChallengeId(self._ids.new()),
            receiver_id=attempt.target.receiver_id,
            endpoint_fingerprint=attempt.target.endpoint_fingerprint,
            endpoint_generation=attempt.target.endpoint_generation,
            operation_id=attempt.context.operation_id,
            session_generation=attempt.context.session_generation,
            trust_generation=attempt.context.trust_generation or 0,
            created_at=attempt.created_at,
            expires_at=attempt.created_at + PAIRING_LIFETIME,
            credential_kind=result.value.credential_kind,
            backend_token=result.value.backend_token,
        )
        attempt.challenge = challenge
        self._challenges[challenge.challenge_id] = attempt
        if attempt.retired or not self._target_current(attempt.target, attempt.context) or self._clock.now() >= challenge.expires_at:
            self._retire(challenge.receiver_id)
            return _failure(ErrorCode.stale_event)
        activated = self._fences.activate(challenge)
        if isinstance(activated, Err):
            self._retire(challenge.receiver_id)
            return activated

        def expired() -> None:
            if self._challenges.get(challenge.challenge_id) is attempt:
                self._retire(challenge.receiver_id)
                self._ensure_cleanup(challenge.receiver_id)

        attempt.expiry = self._scheduler.call_at(challenge.expires_at, expired)
        return Ok(value=challenge)

    async def submit_pairing(
        self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext,
    ) -> PairingResult:
        attempt = self._challenges.get(challenge_id)
        if attempt is None:
            return _pairing("Cancelled", ErrorCode.stale_event)
        challenge = attempt.challenge
        receiver_id = challenge.receiver_id
        if (
            context.endpoint_generation != challenge.endpoint_generation
            or context.session_generation != challenge.session_generation
            or (context.trust_generation or 0) != challenge.trust_generation
        ):
            return _pairing("Cancelled", ErrorCode.stale_event)
        if self._clock.now() >= challenge.expires_at or not self._current(attempt):
            code = ErrorCode.pairing_expired if self._clock.now() >= challenge.expires_at else ErrorCode.stale_event
            self._retire(receiver_id)
            cleanup = await self._finish_cleanup(receiver_id)
            return _pairing("Cancelled", code) if isinstance(cleanup, Ok) else _pairing("TrustCleanupRequired", ErrorCode.trust_cleanup_required)
        if attempt.submitting:
            return _pairing("InvalidInput", ErrorCode.invalid_input)
        text = None
        try:
            text = value.reveal_for_backend() if type(value) is SensitiveText else None
            valid = type(text) is str and bool(text)
            if challenge.credential_kind == "pin":
                valid = valid and len(text) == 4 and text.isascii() and text.isdigit()
        except Exception:
            valid = False
        # Only the transient input crosses the backend boundary; never retain it
        # on an attempt, in metadata, or in an error, including invalid input.
        del text
        if not valid:
            return await self._reject(attempt)
        attempt.submitting = True
        try:
            result = await self._wait(self._spawn(receiver_id, self._submit(attempt, value, context)), receiver_id)
        finally:
            attempt.submitting = False
        if not self._tracked(attempt):
            return _pairing("Cancelled", ErrorCode.stale_event)
        if result is None:
            return _pairing("TrustCleanupRequired", ErrorCode.trust_cleanup_required)
        if result.kind == "Trusted" and self._current(attempt):
            self._discard(attempt)
            return result
        if result.kind == "InvalidInput" and self._current(attempt):
            return await self._reject(attempt)
        self._retire(receiver_id)
        cleanup = await self._finish_cleanup(receiver_id)
        if isinstance(cleanup, Err):
            return _pairing("TrustCleanupRequired", ErrorCode.trust_cleanup_required)
        if result.kind == "Trusted":
            return _pairing("Cancelled", ErrorCode.stale_event)
        return result

    async def _reject(self, attempt: _Attempt) -> PairingResult:
        attempt.invalid_submissions += 1
        if attempt.invalid_submissions < MAX_INVALID_ATTEMPTS:
            return _pairing("InvalidInput", ErrorCode.invalid_input)
        self._retire(attempt.target.receiver_id)
        cleanup = await self._finish_cleanup(attempt.target.receiver_id)
        if isinstance(cleanup, Err):
            return _pairing("TrustCleanupRequired", ErrorCode.trust_cleanup_required)
        return _pairing("ReceiverRejected", ErrorCode.receiver_rejected)

    async def _submit(self, attempt: _Attempt, value: SensitiveText, context: OperationContext) -> PairingResult:
        challenge = attempt.challenge
        if not self._current(attempt):
            return _pairing("Cancelled", ErrorCode.stale_event)
        attempt.needs_revoke = True
        submitted = await _safe(self._control.request(
            BackendOperation.submit_pairing, attempt.target, PairingPayload(credential=value), context,
        ))
        del value
        if not self._current(attempt):
            return _pairing("Cancelled", ErrorCode.stale_event)
        if isinstance(submitted, Err):
            if submitted.error.code in (ErrorCode.receiver_rejected, ErrorCode.invalid_input):
                return _pairing("InvalidInput", ErrorCode.invalid_input)
            kind = "CredentialUnavailable" if submitted.error.code is ErrorCode.keyring_unavailable else "TransportFailure"
            return _pairing(kind, submitted.error.code)
        if not _acknowledged(submitted.value, BackendOperation.submit_pairing, attempt.target):
            return _pairing("CredentialUnavailable", ErrorCode.keyring_unavailable)
        prepared = await _safe(self._credentials.prepare_pairing(challenge, challenge.backend_token))
        if isinstance(prepared, Ok) and isinstance(prepared.value, OpaqueProvisionalCredentialRef):
            attempt.provisional = prepared.value
        if not self._current(attempt):
            return _pairing("Cancelled", ErrorCode.stale_event)
        if attempt.provisional is None or isinstance(prepared, Err):
            return _pairing("CredentialUnavailable", ErrorCode.keyring_unavailable)
        attempt.commit_started = True
        committed = await _safe(self._credentials.commit_pairing(attempt.provisional, challenge.trust_generation))
        if isinstance(committed, Ok) and isinstance(committed.value, OpaqueCredentialRef):
            attempt.credential = committed.value
        if not self._current(attempt):
            return _pairing("Cancelled", ErrorCode.stale_event)
        if attempt.credential is None or isinstance(committed, Err):
            return _pairing("CredentialUnavailable", ErrorCode.keyring_unavailable)
        return PairingResult(kind="Trusted", credential_ref=attempt.credential, diagnostic=None)

    async def cancel_pairing(
        self, receiver_id: ReceiverId, challenge_id: ChallengeId | None, context: OperationContext,
    ) -> Result[None]:
        if challenge_id is not None:
            attempt = self._challenges.get(challenge_id)
            if attempt is None or attempt.target.receiver_id != receiver_id:
                return _failure(ErrorCode.stale_event)
        if not self._attempts.get(receiver_id) and not self.pending_cleanup(receiver_id):
            return Ok(value=None)
        self._retire(receiver_id)
        return await self._finish_cleanup(receiver_id)

    async def forget_trust(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        # The transport authority carries the live lease until Stop confirms its
        # release. A caller cannot Forget while that evidence is still present.
        if context.transport_lease_id is not None:
            return _failure(ErrorCode.invalid_input)
        self._forgetting.add(receiver_id)
        self._revoke_receivers.add(receiver_id)
        self._retire(receiver_id)
        return await self._finish_cleanup(receiver_id)

    async def mark_invalid(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        self._invalidating.add(receiver_id)
        self._revoke_receivers.add(receiver_id)
        self._retire(receiver_id)
        return await self._finish_cleanup(receiver_id)

    async def retry_revocation(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        if not self.pending_cleanup(receiver_id):
            return Ok(value=None)
        self._retire(receiver_id)
        return await self._finish_cleanup(receiver_id, retry=True)

    async def _cancel_attempts(self, receiver_id: ReceiverId) -> bool:
        confirmed = True
        for attempt in tuple(self._attempts.get(receiver_id, ())):
            if attempt.challenge is not None and not attempt.cancel_confirmed:
                result = await _safe(self._credentials.cancel_pairing(attempt.challenge))
                attempt.cancel_confirmed = isinstance(result, Ok)
                confirmed = confirmed and attempt.cancel_confirmed
        return confirmed

    async def _cleanup(self, receiver_id: ReceiverId) -> Result[None]:
        if receiver_id in self._invalidating:
            invalidated = await _safe(self._credentials.mark_invalid(receiver_id))
            if isinstance(invalidated, Err):
                return _failure(ErrorCode.trust_cleanup_required)
        # Clear invalid local trust before a potentially slow cancellation, then
        # request cancellation before waiting: it may release a backend waiter.
        cancelled = await self._cancel_attempts(receiver_id)
        operations = tuple(self._operations.get(receiver_id, ()))
        if operations:
            await asyncio.gather(*(asyncio.shield(task) for task in operations))
        # A begin/prepare can finish after cancellation; cancel the resulting
        # challenge too, then revoke only after the entire acquisition barrier.
        cancelled = await self._cancel_attempts(receiver_id) and cancelled
        attempts = tuple(self._attempts.get(receiver_id, ()))
        needs_receiver_revoke = receiver_id in self._revoke_receivers
        for attempt in attempts:
            if attempt.provisional is not None and not attempt.provisional_revoked:
                revoked = await _safe(self._credentials.revoke_pairing(attempt.provisional))
                if isinstance(revoked, Err):
                    return _failure(ErrorCode.trust_cleanup_required)
                attempt.provisional_revoked = True
            needs_receiver_revoke = needs_receiver_revoke or (
                attempt.needs_revoke and (attempt.provisional is None or attempt.commit_started)
            )
        if needs_receiver_revoke or receiver_id in self._revoke_receivers:
            revoked = await _safe(self._credentials.revoke_pairing(receiver_id))
            if isinstance(revoked, Err):
                return _failure(ErrorCode.trust_cleanup_required)
            self._revoke_receivers.discard(receiver_id)
            for attempt in attempts:
                attempt.needs_revoke = False
            cancelled = True  # The completed barrier plus revoke compensates cancellation failure.
        if not cancelled:
            return _failure(ErrorCode.trust_cleanup_required)
        if receiver_id in self._invalidating and receiver_id not in self._forgetting:
            saved = await _safe(self._receiver_store.get(receiver_id))
            if isinstance(saved, Err):
                return _failure(ErrorCode.trust_cleanup_required)
            if saved.value is not None and saved.value.credential_ref is not None:
                cleared = await _safe(self._receiver_store.put(replace(saved.value, credential_ref=None)))
                if isinstance(cleared, Err):
                    return _failure(ErrorCode.trust_cleanup_required)
        # Forget can join this barrier during either metadata await. Its stronger
        # removal intent must still run before any waiter observes clean trust.
        if receiver_id in self._forgetting:
            removed = await _safe(self._receiver_store.remove_metadata(receiver_id))
            if isinstance(removed, Err):
                return _failure(ErrorCode.trust_cleanup_required)
        for attempt in attempts:
            self._discard(attempt)
        self._revoke_receivers.discard(receiver_id)
        self._forgetting.discard(receiver_id)
        self._invalidating.discard(receiver_id)
        self._cleanup_required.discard(receiver_id)
        return Ok(value=None)


__all__ = ["PairingCoordinator", "fence_matches"]
