"""Actor-facing pairing flow.

The session layer never creates credentials or interprets backend authentication.
It delegates every trust operation to the one injected ``PairingPort`` and keeps
only the public ``ChallengeView`` in the snapshot.  The coordinator owns opaque
correlation tokens, provisional credentials, compensation, and the actual
full-generation trust fence.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    EffectEvent,
    Forget,
    Pair,
    RetryRevocation,
    SubmitPairing,
    TimerEvent,
)
from airplay_linux.domain.operations import (
    ChallengeView,
    OperationContext,
    PairingChallenge,
    PairingResult,
    SensitiveText,
)
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    IntentStage,
    Ok,
    Result,
    SessionPhase,
    TransportState,
    TrustState,
)

from .cleanup import runtime_for
from .engine import Handler, Route, SessionContext


class PairingFlow:
    """Synchronous actor handlers plus effect launchers for trust operations."""

    def __init__(
        self,
        deps: object,
        *,
        start_after_trust: Callable[[SessionContext], bool] | None = None,
    ) -> None:
        self.deps = deps
        self.start_after_trust = start_after_trust

    # -- public actor entry points -----------------------------------------

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, Pair):
            self._pair(ctx)
            return True
        if isinstance(message, SubmitPairing):
            self._submit(ctx, message)
            return True
        if isinstance(message, Forget):
            self._forget(ctx)
            return True
        if isinstance(message, RetryRevocation):
            self._retry_revocation(ctx)
            return True
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return False

    def discovery_changed(self, ctx: SessionContext) -> bool:
        """Cancel retired authority after discovery synchronously fences it."""
        fence = self.deps.fences.get(ctx.state.receiver_id)
        if fence is None or fence.trust_generation == ctx.state.trust_generation:
            return True
        challenge = ctx.state.challenge
        was_pairing = challenge is not None or ctx.state.trust is TrustState.Pairing
        ctx.invalidate_trust()
        ctx.cancel_timers()
        if was_pairing:
            ctx.apply(trust=TrustState.RevocationPending)
            self.cancel_from_cleanup(
                ctx, challenge.challenge_id if challenge is not None else None,
                ctx.operation(),
            )
        elif ctx.state.trust is TrustState.Trusted:
            ctx.apply(trust=TrustState.Untrusted)
        return True

    def begin_from_backend(self, ctx: SessionContext) -> bool:
        """Begin only after the current backend has requested pairing."""
        if ctx.state.challenge is not None:
            return True
        if ctx.state.cleanup_pending or ctx.state.intent_stage in (
            IntentStage.stopping,
            IntentStage.cleanup,
        ):
            return False
        if ctx.state.trust in (TrustState.CleanupRequired, TrustState.RevocationPending):
            return False
        ctx.invalidate_trust()
        ctx.apply(
            trust=TrustState.Pairing,
            transport=TransportState.Pairing
            if ctx.state.stream is None
            else ctx.state.transport,
        )
        self._begin(ctx)
        return True

    def cancel_from_cleanup(
        self, ctx: SessionContext, challenge_id: object | None, context: OperationContext,
    ) -> None:
        """Launch cancellation after Stop has synchronously invalidated trust."""
        receiver_id = ctx.state.receiver_id
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "pairing_cancel",
            context,
            lambda: self.deps.pairing.cancel_pairing(
                receiver_id, challenge_id, context,
            ),
        )

    def mark_invalid_from_backend(self, ctx: SessionContext) -> bool:
        """Fence the old trust before asking the coordinator to invalidate it."""
        if ctx.state.trust is TrustState.CleanupRequired:
            return True
        ctx.invalidate_trust()
        ctx.apply(trust=TrustState.CleanupRequired)
        context = ctx.operation()
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "mark_invalid",
            context,
            lambda: self.deps.pairing.mark_invalid(ctx.state.receiver_id, context),
        )
        return True

    def expire(self, ctx: SessionContext) -> bool:
        challenge = ctx.state.challenge
        if challenge is None:
            return False
        challenge_id = challenge.challenge_id
        ctx.invalidate_trust()
        ctx.cancel_timers()
        ctx.apply(
            trust=TrustState.RevocationPending,
            last_error=diagnostic(ErrorCode.pairing_expired),
        )
        context = ctx.operation()
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "pairing_cancel",
            context,
            lambda: self.deps.pairing.cancel_pairing(
                ctx.state.receiver_id, challenge_id, context,
            ),
        )
        return True

    # -- begin/submit ------------------------------------------------------

    def _pair(self, ctx: SessionContext) -> None:
        state = ctx.state
        if state.target is None or not state.online:
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return
        if state.stream is not None or state.intent_active:
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.cleanup_pending or state.intent_stage in (
            IntentStage.stopping,
            IntentStage.cleanup,
        ):
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.trust in (TrustState.CleanupRequired, TrustState.RevocationPending):
            ctx.apply(last_error=diagnostic(ErrorCode.trust_cleanup_required, state.receiver_id))
            return
        if state.challenge is not None or state.trust is TrustState.Pairing:
            return
        ctx.invalidate_trust()
        ctx.apply(trust=TrustState.Pairing, last_error=None)
        self._begin(ctx)

    def _begin(self, ctx: SessionContext) -> None:
        target = ctx.state.target
        if target is None:
            ctx.apply(
                trust=TrustState.Untrusted,
                last_error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id),
            )
            return
        try:
            self.deps.transport.update_target(target, ctx.state.receiver_id)
        except Exception:  # noqa: BLE001 - no authority for an unregistered target
            ctx.apply(
                trust=TrustState.Untrusted,
                last_error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id),
            )
            return
        context = ctx.operation()
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "pairing_begin",
            context,
            lambda: self.deps.pairing.begin_pairing(target, context),
        )

    def _submit(self, ctx: SessionContext, message: SubmitPairing) -> None:
        challenge = ctx.state.challenge
        if challenge is None:
            return
        # Manager admission already checked all challenge fields.  Rebuild the
        # context from the public view so a submit cannot accidentally use the
        # newer trust generation allocated by an unrelated command.
        current = ctx.operation()
        context = replace(
            current,
            operation_id=challenge.operation_id,
            session_generation=challenge.session_generation,
            endpoint_generation=challenge.endpoint_generation,
            trust_generation=challenge.trust_generation,
        )
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "pairing_submit",
            context,
            lambda: self._submit_result(message, context),
        )

    async def _submit_result(
        self, message: SubmitPairing, context: OperationContext,
    ) -> Result[object]:
        result = await self.deps.pairing.submit_pairing(
            message.challenge_id, message.value, context,
        )
        if isinstance(result, PairingResult):
            return Ok(value=result)
        return Err(error=diagnostic(ErrorCode.keyring_unavailable))

    # -- trust invalidation -------------------------------------------------

    def _forget(self, ctx: SessionContext) -> None:
        state = ctx.state
        if state.stream is not None:
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.intent_active or state.cleanup_pending:
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        ctx.invalidate_trust()
        ctx.cancel_timers()
        ctx.apply(trust=TrustState.RevocationPending, last_error=None)
        context = ctx.operation()
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "forget_trust",
            context,
            lambda: self.deps.pairing.forget_trust(ctx.state.receiver_id, context),
        )

    def _retry_revocation(self, ctx: SessionContext) -> None:
        if ctx.state.trust not in (TrustState.CleanupRequired, TrustState.RevocationPending):
            return
        ctx.apply(trust=TrustState.RevocationPending)
        context = ctx.operation()
        setattr(ctx, "_task2_pairing_operation", context.operation_id)
        ctx.launch(
            Route.trust,
            "retry_revocation",
            context,
            lambda: self.deps.pairing.retry_revocation(ctx.state.receiver_id, context),
        )

    # -- effect delivery ---------------------------------------------------

    @staticmethod
    def _view(challenge: PairingChallenge) -> ChallengeView:
        return ChallengeView(
            challenge_id=challenge.challenge_id,
            receiver_id=challenge.receiver_id,
            endpoint_fingerprint=challenge.endpoint_fingerprint,
            endpoint_generation=challenge.endpoint_generation,
            operation_id=challenge.operation_id,
            session_generation=challenge.session_generation,
            trust_generation=challenge.trust_generation,
            created_at=challenge.created_at,
            expires_at=challenge.expires_at,
            credential_kind=challenge.credential_kind,
        )

    def _effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        kind = message.kind.rsplit(":", 1)[-1]
        if kind == "pairing_begin":
            expected_operation = getattr(ctx, "_task2_pairing_operation", None)
            if (
                expected_operation is not None
                and message.operation_id != expected_operation
            ):
                return True
            if isinstance(message.result, Err):
                ctx.apply(
                    trust=TrustState.CleanupRequired
                    if message.result.error.code is ErrorCode.trust_cleanup_required
                    else TrustState.Untrusted,
                    last_error=message.result.error,
                    challenge=None,
                )
                return True
            value = message.result.value if isinstance(message.result, Ok) else None
            if not isinstance(value, PairingChallenge):
                ctx.apply(
                    trust=TrustState.CleanupRequired,
                    last_error=diagnostic(ErrorCode.keyring_unavailable),
                )
                return True
            target = ctx.state.target
            if (
                target is None
                or message.session_generation != ctx.state.session_generation
                or value.receiver_id != ctx.state.receiver_id
                or value.endpoint_fingerprint != target.endpoint_fingerprint
                or value.endpoint_generation != target.endpoint_generation
                or value.session_generation != ctx.state.session_generation
                or value.trust_generation != ctx.state.trust_generation
            ):
                # The coordinator is the compensation owner.  Expose no stale
                # challenge to the UI even when a malformed adapter result is
                # returned.
                ctx.apply(
                    trust=TrustState.CleanupRequired,
                    challenge=None,
                    last_error=diagnostic(ErrorCode.stale_event),
                )
                return True
            view = self._view(value)
            ctx.apply(challenge=view, trust=TrustState.Pairing)
            ctx.schedule("pairing_expiry", view.expires_at, message_context(ctx, message))
            return True

        if kind == "pairing_submit":
            return self._submit_effect(ctx, message)

        if kind in {"pairing_cancel", "mark_invalid", "forget_trust", "retry_revocation"}:
            expected_operation = getattr(ctx, "_task2_pairing_operation", None)
            if expected_operation is not None and message.operation_id != expected_operation:
                return True
            if isinstance(message.result, Ok):
                ctx.cancel_timers()
                if kind == "pairing_cancel":
                    ctx.apply(
                        challenge=None,
                        trust=TrustState.Untrusted
                        if ctx.state.trust in (
                            TrustState.Pairing,
                            TrustState.RevocationPending,
                        )
                        else ctx.state.trust,
                    )
                else:
                    ctx.apply(
                        challenge=None,
                        trust=TrustState.Untrusted,
                        last_error=None,
                    )
                if kind == "pairing_cancel":
                    runtime = runtime_for(ctx)
                    runtime.trust_pending = False
                    continuation = getattr(ctx, "_task2_cleanup_continue", None)
                    if callable(continuation):
                        continuation(ctx)
                elif kind == "retry_revocation":
                    continuation = getattr(ctx, "_task2_cleanup_continue", None)
                    if callable(continuation):
                        continuation(ctx)
                return True
            ctx.cancel_timers()
            ctx.apply(
                trust=TrustState.CleanupRequired,
                challenge=None,
                last_error=_error_from_result(message.result),
            )
            if kind == "pairing_cancel":
                runtime_for(ctx).trust_pending = False
                continuation = getattr(ctx, "_task2_cleanup_continue", None)
                if callable(continuation):
                    continuation(ctx)
            return True
        return False

    def _submit_effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        challenge = ctx.state.challenge
        value = message.result.value if isinstance(message.result, Ok) else None
        target = ctx.state.target
        if challenge is None or message.operation_id != challenge.operation_id:
            # Stop, Forget, expiry, or replacement may already have retired the
            # public challenge.  The coordinator owns compensation for the old
            # submission; ordinary late answers must not disturb the cancellation
            # transaction.  A coordinator-reported compensation failure is the
            # exception: trust is still uncertain and must remain blocked.
            retired = value if isinstance(value, PairingResult) else None
            if (
                isinstance(message.result, Err)
                or (
                    retired is not None
                    and retired.kind == "TrustCleanupRequired"
                )
            ):
                error = (
                    retired.diagnostic
                    if retired is not None and retired.diagnostic is not None
                    else _error_from_result(message.result)
                )
                ctx.apply(
                    trust=TrustState.CleanupRequired,
                    challenge=None,
                    last_error=error,
                )
            return True
        if (
            message.session_generation != challenge.session_generation
            or ctx.state.session_generation != challenge.session_generation
            or target is None
            or challenge.receiver_id != ctx.state.receiver_id
            or challenge.endpoint_fingerprint != target.endpoint_fingerprint
            or challenge.endpoint_generation != target.endpoint_generation
            or ctx.state.trust_generation != challenge.trust_generation
        ):
            # A stale or malformed completion must not publish trust.  The
            # coordinator owns backend compensation; the actor exposes the
            # conservative blocked state until that authority confirms cleanup.
            ctx.invalidate_trust()
            ctx.cancel_timers()
            ctx.apply(
                trust=TrustState.CleanupRequired,
                challenge=None,
                last_error=diagnostic(ErrorCode.stale_event),
            )
            return True
        if isinstance(message.result, Err) or not isinstance(value, PairingResult):
            ctx.cancel_timers()
            ctx.apply(
                trust=TrustState.CleanupRequired,
                challenge=None,
                last_error=_error_from_result(message.result),
            )
            return True
        result = value
        if result.kind == "Trusted":
            ctx.cancel_timers()
            ctx.apply(
                challenge=None,
                trust=TrustState.Trusted,
                last_error=None,
            )
            if self.start_after_trust is not None and ctx.state.intent_active:
                self.start_after_trust(ctx)
            return True
        if result.kind == "InvalidInput":
            ctx.apply(last_error=result.diagnostic)
            return True
        if result.kind == "ReceiverRejected":
            ctx.cancel_timers()
            ctx.apply(
                challenge=None,
                trust=TrustState.Untrusted,
                last_error=result.diagnostic,
            )
            return True
        if result.kind == "TrustCleanupRequired":
            ctx.cancel_timers()
            ctx.apply(
                challenge=None,
                trust=TrustState.CleanupRequired,
                last_error=result.diagnostic
                or diagnostic(ErrorCode.trust_cleanup_required),
            )
            return True
        # Cancelled, CredentialUnavailable, and TransportFailure all retire the
        # active challenge.  Only an explicit cleanup-required result blocks new
        # starts; an ordinary cancelled challenge is safe to begin again.
        ctx.cancel_timers()
        ctx.apply(
            challenge=None,
            trust=TrustState.Untrusted,
            last_error=result.diagnostic,
        )
        return True


def _error_from_result(result: object):
    if isinstance(result, Err):
        return result.error
    return diagnostic(ErrorCode.trust_cleanup_required)


def message_context(ctx: SessionContext, message: EffectEvent) -> OperationContext:
    """Recover a timer fence from the effect that created the challenge."""
    # EffectEvent intentionally does not expose the context.  The challenge's
    # operation/session/trust fields are the public fence; build a fresh context
    # with the exact current target and those values for TimerEvent admission.
    current = ctx.operation()
    challenge = ctx.state.challenge
    if challenge is None:
        return current
    return replace(
        current,
        operation_id=challenge.operation_id,
        session_generation=challenge.session_generation,
        endpoint_generation=challenge.endpoint_generation,
        trust_generation=challenge.trust_generation,
    )


def build_pairing_handler(
    deps: object,
    *,
    start_after_trust: Callable[[SessionContext], bool] | None = None,
) -> PairingFlow:
    return PairingFlow(deps, start_after_trust=start_after_trust)


__all__ = ["PairingFlow", "build_pairing_handler"]
