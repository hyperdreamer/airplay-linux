"""Rebuild intent only after the daemon and display owners prove resources.

Session metadata is never liveness evidence. The display provider owns its
stage machine, creation lock, exact removal and layout restoration. This module
only projects its reconciliation results into actors and rejoins ordered cleanup.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel, ConfirmOrphanRemoval, ConfirmSwitch, EffectEvent, KeepCleanup,
    Pair, Retry, RetryCleanup, RetryRevocation, Start, Stop,
)
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.operations import OpaqueCredentialRef, OperationId
from airplay_linux.domain.records import RecoveryItem, SessionSnapshot
from airplay_linux.domain.resources import (
    DisplayState, Err, ErrorCode, IntentStage, Mode, Ok, Result, TrustState,
)
from airplay_linux.storage.codecs import is_ambiguous_identity
from airplay_linux.storage.repositories import PreferenceAccess
from airplay_linux.trust.fences import TrustFence

from .engine import Handler, ManagerDeps, Route, SessionContext, SessionManager
from .reconnect import _runtime as reconnect_runtime


@dataclass(frozen=True)
class _CredentialRestore:
    expected_fence: TrustFence
    target: ReceiverTarget | None
    result: Result[OpaqueCredentialRef | None]


class RecoveryHandlers:
    def __init__(self, deps: ManagerDeps) -> None:
        self.deps = deps
        self.items: dict[object, tuple[RecoveryItem, ...]] = {}
        self.approving: set[object] = set()

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        if not isinstance(message, EffectEvent):
            return False
        kind = message.kind.split(":", 1)[-1]
        if kind == "startup":
            runtime = reconnect_runtime(ctx)
            runtime.known, runtime.target = True, ctx.state.target
            ctx.invalidate_trust()
            if ctx.state.trust is TrustState.CleanupRequired:
                handler = ctx._manager.handlers.get(Route.trust)
                if handler is not None:
                    handler(ctx, RetryRevocation(receiver_id=ctx.state.receiver_id))
            if ctx.state.intent_stage is IntentStage.stopping:
                handler = ctx._manager.handlers.get(Route.cleanup)
                if handler is not None:
                    handler(ctx, Stop(receiver_id=ctx.state.receiver_id))
            return True
        if kind == "trust_restore":
            if not isinstance(message.result, Ok) or type(message.result.value) is not _CredentialRestore:
                return True
            restore = message.result.value
            state = ctx.state
            target = state.target if state.online else None
            if (
                self.deps.fences.get(state.receiver_id) != restore.expected_fence
                or message.session_generation != state.session_generation
                or state.session_generation != restore.expected_fence.session_generation
                or state.trust_generation != restore.expected_fence.trust_generation
                or state.challenge is not None
                or state.trust is not TrustState.Unknown
                or target != restore.target
                or restore.expected_fence.receiver_id != state.receiver_id
            ):
                if state.trust is TrustState.Unknown and state.challenge is None:
                    ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
                return True
            if isinstance(restore.result, Ok) and type(restore.result.value) is OpaqueCredentialRef:
                ctx.apply(trust=TrustState.Trusted, last_error=None)
            else:
                error = (
                    restore.result.error if isinstance(restore.result, Err)
                    else diagnostic(ErrorCode.keyring_unavailable, state.receiver_id)
                )
                ctx.apply(last_error=error)
            return True
        if kind == "shutdown_pending":
            ctx.cancel_timers()
            trust = ctx.state.trust
            if trust in (TrustState.Pairing, TrustState.RevocationPending):
                trust = TrustState.CleanupRequired
            has_display = (ctx.state.display_handle is not None or ctx.state.ownership_uuid is not None
                           or ctx.state.display is not DisplayState.None_)
            from .cleanup import cleanup_complete
            pending = not cleanup_complete(ctx.state, ctx.outstanding_acquisitions(),
                                           ctx._manager.pending_resources(ctx.state.receiver_id))
            ctx.apply(intent_active=False, queued_start=None, next_retry_at=None,
                      challenge=None, trust=trust, cleanup_pending=pending,
                      intent_stage=IntentStage.cleanup if pending else IntentStage.idle,
                      display=DisplayState.CleanupRequired if has_display else ctx.state.display,
                      last_error=diagnostic(ErrorCode.cleanup_timeout, ctx.state.receiver_id))
            return True
        if kind == "persistence_failed":
            # Failure is a separate diagnostic; preserve every independent
            # resource fact (especially a still-owned stream or display).
            ctx.apply(last_error=diagnostic(ErrorCode.persistence_failed, ctx.state.receiver_id))
            return True
        if kind.startswith("orphan_"):
            owner = next((item.record.owner_uuid for item in self.items.get(ctx.state.receiver_id, ())
                          if f"orphan_{item.record.owner_uuid}" == kind), None)
            self.approving.discard(owner)
            if isinstance(message.result, Err):
                ctx.apply(last_error=message.result.error, cleanup_pending=True)
            else:
                remaining = tuple(item for item in self.items.get(ctx.state.receiver_id, ())
                                  if item.record.owner_uuid != owner)
                self.items[ctx.state.receiver_id] = remaining
                if not remaining:
                    ctx.apply(display=DisplayState.None_, display_handle=None, ownership_uuid=None,
                              intent_active=False, intent_stage=IntentStage.idle,
                              cleanup_pending=False, last_error=None)
            return True
        return False

    def cleanup(self, previous: Handler, ctx: SessionContext, message: object) -> bool:
        items = self.items.get(ctx.state.receiver_id, ())
        blocked = any(item.kind in {"orphan", "cleanup_required"}
                      and item.record.desired_state != "removing" and item.record.stage != "removing"
                      for item in items)
        if isinstance(message, ConfirmOrphanRemoval):
            item = next((item for item in items if item.kind == "orphan"
                         and item.record.owner_uuid == message.owner_uuid), None)
            if item is None or not message.confirmed or message.owner_uuid in self.approving:
                return True
            self.approving.add(message.owner_uuid)
            operation = ctx.operation()
            ctx.apply(operation_id=operation.operation_id)
            ctx.launch(Route.recovery, f"orphan_{message.owner_uuid}", operation,
                       lambda: self.deps.display.approve_orphan(message.owner_uuid, True, operation))
            return True
        if blocked and isinstance(message, (Stop, Cancel, RetryCleanup, KeepCleanup, ConfirmSwitch)):
            # A UUID is journal evidence, not proof of a live output. In
            # particular shutdown must never convert an orphan into auto-remove.
            if isinstance(message, (Stop, Cancel)):
                if ctx.state.intent_active:
                    ctx.supersede("stop")
                ctx.cancel_timers()
            ctx.apply(intent_active=False, cleanup_pending=True, intent_stage=IntentStage.cleanup,
                      display=DisplayState.CleanupRequired,
                      last_error=diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
            return True
        return previous(ctx, message)


def build_recovery_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    return {Route.recovery: RecoveryHandlers(deps)}


class StartupRecovery:
    """Run once before UI commands; install all actors before any continuation.

    The cleanup route needs one small adapter for orphan commands, since the
    frozen manager routes those commands to cleanup rather than recovery.
    """
    def __init__(
        self,
        manager: SessionManager,
        deps: ManagerDeps,
        registry,
        *,
        credential_loader: Callable[[ReceiverId], Awaitable[Result[OpaqueCredentialRef | None]]] | None = None,
    ) -> None:
        self.manager, self.deps, self.registry = manager, deps, registry
        self._credential_loader = credential_loader
        handler = manager.handlers.get(Route.recovery)
        if handler is None:
            handler = RecoveryHandlers(deps)
            manager.handlers[Route.recovery] = handler
        if not isinstance(handler, RecoveryHandlers):
            raise TypeError("recovery route must come from build_recovery_handlers")
        self.handler = handler
        self._result = None
        self._blocked = True
        for route, previous in tuple(manager.handlers.items()):
            if route is Route.recovery:
                continue

            def guarded(ctx, message, previous=previous, route=route):
                if self._blocked and isinstance(message, (Start, Retry, Pair, ConfirmSwitch)):
                    ctx.apply(last_error=diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
                    return True
                if route is Route.cleanup:
                    return self.handler.cleanup(previous, ctx, message)
                return previous(ctx, message)

            manager.handlers[route] = guarded

    async def run(self) -> Result[tuple[RecoveryItem, ...]]:
        if self._result is not None:
            return self._result
        try:
            self._result = await self._restore()
        except Exception:  # never retain adapter exception strings
            self._result = Err(error=diagnostic(ErrorCode.persistence_failed))
        return self._result

    async def _restore(self):
        reconciled = await self.registry.reconcile_previous()
        if not isinstance(reconciled, Ok):
            return reconciled if isinstance(reconciled, Err) else Err(error=diagnostic(ErrorCode.ownership_mismatch))
        snapshots = await self.deps.sessions.load_all()
        load_error = getattr(self.deps.sessions, "load_error", None)
        if isinstance(load_error, Err):
            return load_error
        quarantined = getattr(self.deps.sessions, "quarantined", None)
        failures = quarantined() if callable(quarantined) else ()
        if failures:
            # The tuple-only SessionStore contract uses this side channel for
            # unreadable metadata. Missing trust/intent is unknown, not idle.
            return Err(error=failures[0])
        preferences = await self.deps.receivers.all()
        if isinstance(preferences, Err):
            return preferences
        items = await self.deps.display.reconcile()
        failure = getattr(self.deps.display, "last_diagnostic", None)
        if failure is not None:
            return Err(error=failure)
        ownerships = await self.deps.ownerships.all()
        if isinstance(ownerships, Err):
            return ownerships
        durable = {record.owner_uuid: record for record in ownerships.value}
        grouped = defaultdict(list)
        for item in items:
            # Removed/discarded records are no longer resources, even when the
            # provider returns their historical reconciliation action.
            if item.record.owner_uuid in durable:
                grouped[item.record.receiver_id].append(item)
        # An incomplete provider response cannot make a durable reservation
        # disappear. Surface it conservatively without manufacturing a handle.
        seen = {item.record.owner_uuid for item in items}
        for record in durable.values():
            if record.owner_uuid not in seen:
                grouped[record.receiver_id].append(RecoveryItem(
                    kind="cleanup_required", record=record, handle=None,
                    diagnostic=diagnostic(ErrorCode.ownership_mismatch, record.receiver_id),
                ))
        removing_receivers = {item.record.receiver_id for item in items
                              if item.record.desired_state == "removing" or item.record.stage == "removing"}
        saved = {record.receiver_id: record for record in preferences.value}
        sessions = {state.receiver_id: state for state in snapshots}
        receivers = set(sessions) | set(saved) | set(grouped) | removing_receivers
        for receiver_id in receivers:
            old = sessions.get(receiver_id, SessionSnapshot.idle(receiver_id))
            candidates = tuple(grouped[receiver_id])
            self.handler.items[receiver_id] = candidates
            view = self.deps.discovery.current(receiver_id)
            online = view is not None and view.online
            destination = self.manager._target_from_view(view) if online else None
            preference = PreferenceAccess.for_record_set(view.record_set, saved.get(receiver_id)) if view else None
            continuous = not is_ambiguous_identity(receiver_id) and (
                view is None or (view.record_set.persistable and not view.record_set.ambiguous
                                 and view.record_set.replacement_of is None
                                 and (view.record_set.identity_kind == "device" or view.record_set.continuity_verified))
            )
            tombstone = old.trust in (
                TrustState.Pairing, TrustState.RevocationPending, TrustState.CleanupRequired,
            )
            trust_intent_safe = (
                not old.cleanup_pending
                and old.intent_stage not in (IntentStage.stopping, IntentStage.cleanup)
            )
            should_restore_trust = (
                not tombstone
                and receiver_id not in removing_receivers
                and trust_intent_safe
                and not candidates
            )
            trust_error = diagnostic(ErrorCode.stale_target, receiver_id) if not continuous and should_restore_trust else None
            # Construct from the idle defaults: no stale capture/stream/audio
            # token, challenge, mute, backoff or queued mode switch survives.
            state = replace(
                SessionSnapshot.idle(receiver_id, online=online), target=destination, mode=old.mode,
                intent_active=old.intent_active,
                intent_stage=IntentStage.recovering if old.intent_active else IntentStage.idle,
                audio_policy=old.audio_policy,
                session_generation=old.session_generation, trust_generation=old.trust_generation + 1,
                trust=TrustState.CleanupRequired if tombstone else TrustState.Unknown,
                last_error=trust_error,
                resolution=(preference.remembered_resolution if preference else old.resolution) if continuous else None,
            )
            if len(candidates) == 1:
                item = candidates[0]
                record = durable[item.record.owner_uuid]
                removing = record.desired_state == "removing" or record.stage == "removing"
                exact = (item.kind == "recovering" and record.stage == "published"
                         and not removing and item.handle is not None
                         and item.handle.owner_uuid == record.owner_uuid
                         and item.handle.helper == record.helper
                         and item.handle.source.connector == record.connector
                         and item.handle.source.kscreen_output_id == record.output_id
                         and item.handle.source.size == record.observed_size == record.requested_size)
                should_restore_trust = (
                    should_restore_trust or (exact and not tombstone and trust_intent_safe)
                )
                state = replace(state, mode=Mode.VIRTUAL, ownership_uuid=record.owner_uuid,
                                display_handle=item.handle if exact else None,
                                display=DisplayState.Owned if exact else DisplayState.CleanupRequired,
                                intent_active=old.intent_active and not removing,
                                intent_stage=IntentStage.stopping if removing else (
                                    IntentStage.recovering if exact else IntentStage.cleanup),
                                cleanup_pending=not exact,
                                observed_resolution=record.observed_size,
                                last_error=item.diagnostic if not exact else trust_error)
            elif candidates:
                state = replace(state, mode=Mode.VIRTUAL, intent_stage=IntentStage.cleanup,
                                display=DisplayState.CleanupRequired, cleanup_pending=True,
                                last_error=diagnostic(ErrorCode.ownership_mismatch, receiver_id))
            elif receiver_id in removing_receivers or old.cleanup_pending or old.intent_stage in (IntentStage.stopping, IntentStage.cleanup):
                # Prior daemon release and the provider's empty durable set are
                # both confirmed, so stale stream/display references can clear.
                state = replace(state, intent_active=False, intent_stage=IntentStage.idle)
            if trust_error is not None and state.last_error is None:
                state = replace(state, last_error=trust_error)
            self.manager.install(state)
            self.manager._fence_receiver(state)
            self.deps.transport.update_target(destination, receiver_id)
            if should_restore_trust and continuous:
                credential_loader = self._credential_loader
                if credential_loader is None:
                    credential_loader = getattr(self.deps.pairing, "load_credential_ref", None)
                expected_fence = self.deps.fences.get(receiver_id)
                if expected_fence is None:
                    expected_fence = TrustFence(
                        receiver_id=receiver_id,
                        endpoint_fingerprint=destination.endpoint_fingerprint if destination else None,
                        endpoint_generation=destination.endpoint_generation if destination else 0,
                        session_generation=state.session_generation,
                        trust_generation=state.trust_generation,
                        active_challenge_id=None,
                    )
                    loaded = Err(error=diagnostic(ErrorCode.stale_target, receiver_id))
                elif not callable(credential_loader):
                    loaded = Err(error=diagnostic(ErrorCode.keyring_unavailable, receiver_id))
                else:
                    try:
                        loaded = await credential_loader(receiver_id)
                    except Exception:
                        loaded = Err(error=diagnostic(ErrorCode.keyring_unavailable, receiver_id))
                self.manager.ingest(EffectEvent(
                    receiver_id=receiver_id,
                    operation_id=OperationId(self.deps.ids.new()),
                    session_generation=state.session_generation,
                    kind="recovery:trust_restore",
                    result=Ok(value=_CredentialRestore(
                        expected_fence=expected_fence,
                        target=destination,
                        result=loaded,
                    )),
                    resource=None,
                ))
        self._blocked = False
        for receiver_id in receivers:
            state = self.manager.snapshot(receiver_id)
            self.manager.ingest(EffectEvent(
                receiver_id=receiver_id, operation_id=OperationId(self.deps.ids.new()),
                session_generation=state.session_generation, kind="recovery:startup",
                result=Ok(value=None), resource=None,
            ))
        await self.manager.drain()
        return Ok(value=items)


__all__ = ["build_recovery_handlers", "StartupRecovery"]
