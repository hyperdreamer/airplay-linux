"""Manager-owned network retries and registry-bounded daemon recovery.

Compose these handlers with ``compose_reconnect_handlers`` after the Mirror and
Virtual factories. As with Virtual composition, the shared continuations must
belong to the same lifecycle owner: a retry result may acquire a stream, but it
never acquires permission to tear down a retained source.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, replace

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverTarget
from airplay_linux.domain.messages import (
    Cancel, ConfirmSwitch, DiscoveryEvent, EffectEvent, FailurePayload, Pair,
    Retry, RetryCleanup, SetMuted, Start, Stop, TimerEvent, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import InstanceId, StreamHandle
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import (
    AudioState, Diagnostic, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok,
    PhysicalMonitorSource, Result, TransportState, TrustState,
)

from .cleanup import runtime_for
from .engine import Handler, ManagerDeps, Route, SessionContext
from .fencing import accept_transport
from .mirror import _current_context
from .virtual import VirtualLifecycle, _virtual_runtime


def network_delay(attempt: int) -> float:
    """The v1 network schedule has no jitter and no terminal attempt."""
    if type(attempt) is not int:
        raise TypeError("attempt must be an integer")
    if attempt < 0:
        raise ValueError("attempt must be nonnegative")
    return float(2 ** attempt) if attempt < 5 else 30.0


class DaemonRecovery:
    """One recovery authority per manager, shared by all receiver actors.

    ``request_restart`` coalesces concurrent callers and delegates restart
    admission to the daemon registry. Incident tasks also remember completed
    recovery for an exact dead instance, so a delayed notice cannot restart its
    replacement. Cancellation of a receiver's waiter does not cancel the shared
    transaction.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self.deps = deps
        self._inflight: asyncio.Task | None = None
        self._incidents: dict[InstanceId | None, asyncio.Task] = {}
        self._blocked: Err | None = None

    async def request_restart(self) -> Result[DaemonRecord]:
        if self._blocked is not None:
            return self._blocked
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.create_task(self._restart())
        return await asyncio.shield(self._inflight)

    async def _restart(self) -> Result[DaemonRecord]:
        try:
            result = await self.deps.restart_daemon()
        except Exception:  # never expose adapter exception text
            result = Err(error=diagnostic(ErrorCode.daemon_failed))
        if isinstance(result, Err):
            if result.error.code is ErrorCode.ownership_mismatch:
                self._blocked = result
            return result
        if not isinstance(result, Ok) or not isinstance(result.value, DaemonRecord) or result.value.lifecycle != "running":
            return Err(error=diagnostic(ErrorCode.daemon_failed))
        return result

    async def recover(self, instance: InstanceId | None) -> Result[DaemonRecord]:
        task = self._incidents.get(instance)
        if task is None:
            task = asyncio.create_task(self._recover())
            self._incidents[instance] = task
        return await asyncio.shield(task)

    async def _recover(self) -> Result[DaemonRecord]:
        # The registry owns restart admission and its rolling budget. A failed
        # registry transaction is terminal for this incident; an explicit Retry
        # retires the incident cache and asks the registry again.
        return await self.request_restart()

    def retry(self, instance: InstanceId | None) -> None:
        task = self._incidents.get(instance)
        if task is not None and task.done() and not task.cancelled() and isinstance(task.result(), Err):
            del self._incidents[instance]


@dataclass
class _ReconnectRuntime:
    known: bool = False
    target: ReceiverTarget | None = None
    require_pairing: bool = False
    identity_blocked: bool = False
    failure_after_stop: Diagnostic | None = None
    local_required: bool = False
    dead_instance: InstanceId | None = None
    last_instance: InstanceId | None = None
    immediate: bool = False
    release_failed: bool = False


def _runtime(ctx: SessionContext) -> _ReconnectRuntime:
    runtime = getattr(ctx, "_reconnect_runtime", None)
    if runtime is None:
        runtime = _ReconnectRuntime()
        setattr(ctx, "_reconnect_runtime", runtime)
    return runtime


def _endpoint(target: ReceiverTarget | None):
    if target is None:
        return None
    return (target.receiver_id, target.endpoint_fingerprint, target.endpoint_generation,
            target.canonical_host, target.service_port, target.protocol)


_TERMINAL = frozenset({
    ErrorCode.receiver_rejected, ErrorCode.unsupported_source, ErrorCode.unsupported_mode,
    ErrorCode.unsupported_operation, ErrorCode.dependency_missing,
    ErrorCode.dependency_incompatible, ErrorCode.invalid_input,
})
_BLOCKED_TRUST = (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)


class ReconnectLifecycle(VirtualLifecycle):
    """Extend the shared lifecycle, retaining its pairing and cleanup owners."""

    def __init__(self, deps: ManagerDeps) -> None:
        super().__init__(deps)
        self.recovery = DaemonRecovery(deps)

    def _sync_target(self, ctx, event=None):
        runtime = _runtime(ctx)
        view = self.deps.discovery.current(ctx.state.receiver_id)
        online = view is not None and view.online
        if event is not None and event.kind == "removed":
            online = False
        target = ctx._manager._target_from_view(view) if online else None
        old = runtime.target if runtime.known else ctx.state.target
        changed = _endpoint(old) != _endpoint(target) or ctx.state.online != online
        runtime.known, runtime.target = True, target
        challenge = ctx.state.challenge
        was_pairing = challenge is not None or ctx.state.trust is TrustState.Pairing
        continuous = target is None or (
            not view.record_set.ambiguous and view.record_set.replacement_of is None
            and view.record_set.persistable
            and (view.record_set.identity_kind == "device" or view.record_set.continuity_verified)
        )
        lost_identity = target is not None and not continuous
        retire_identity = lost_identity and (
            ctx.state.intent_active or ctx.state.trust is TrustState.Trusted or was_pairing
        )
        changed = changed or (retire_identity and not runtime.identity_blocked)
        runtime.identity_blocked = lost_identity
        try:
            self.deps.transport.update_target(target, ctx.state.receiver_id)
        except Exception:
            target, online = None, False
            runtime.target = None
            changed = True
        ctx.apply(target=target, online=online)
        fence = self.deps.fences.get(ctx.state.receiver_id)
        retired = fence is not None and fence.trust_generation != ctx.state.trust_generation
        if changed or retired:
            ctx.cancel_timers()
            ctx.cancel_audio()
            operation = ctx.operation()
            ctx.apply(operation_id=operation.operation_id, transport_lease_id=None,
                      next_retry_at=None, audio=AudioState.Disabled)
            ctx.invalidate_trust()
            if was_pairing:
                ctx.apply(trust=TrustState.RevocationPending)
                self.pairing.cancel_from_cleanup(
                    ctx, challenge.challenge_id if challenge is not None else None, ctx.operation(),
                )
            elif retire_identity and not runtime.require_pairing:
                self.pairing.mark_invalid_from_backend(ctx)
            if retire_identity:
                runtime.require_pairing = True
                _virtual_runtime(ctx).pairing_requested = True
                ctx.apply(resolution=None, accepted_resolution=None)
        return changed

    def _refresh_discovery(self, ctx):
        ctx.launch(Route.reconnect, "discovery_refresh", _current_context(ctx), self.deps.discovery.refresh)

    def _retire_terminal_error(self, ctx):
        _runtime(ctx).failure_after_stop = None
        ctx.apply(last_error=None)

    def _start(self, ctx, message, *, supersede=True):
        self._sync_target(ctx)
        if ctx.state.target is None or not ctx.state.online:
            self._refresh_discovery(ctx)
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id))
            return
        self._retire_terminal_error(ctx)
        super()._start(ctx, message, supersede=supersede)

    def reconnect(self, ctx, message):
        if isinstance(message, DiscoveryEvent):
            changed = self._sync_target(ctx, message)
            if ctx.state.intent_active and not ctx.state.cleanup_pending and not runtime_for(ctx).active:
                runtime = _runtime(ctx)
                if runtime.local_required and changed and ctx.state.transport is not TransportState.Failed:
                    self._request_local(ctx, runtime.dead_instance)
                elif changed and not runtime.local_required:
                    self._network_lost(ctx)
            return True
        if isinstance(message, TimerEvent) and message.kind == "reconnect":
            ctx.apply(next_retry_at=None)
            self._attempt(ctx)
            return True
        if isinstance(message, Retry):
            if not ctx.state.intent_active or ctx.state.cleanup_pending or runtime_for(ctx).active:
                return False
            self._sync_target(ctx)
            runtime = _runtime(ctx)
            if runtime.local_required:
                self.recovery.retry(runtime.dead_instance)
                self._request_local(ctx, runtime.dead_instance)
                return True
            if ctx.state.trust in _BLOCKED_TRUST or runtime.require_pairing or ctx.state.accepted_resolution is not None:
                return False
            ctx.cancel_timers()
            ctx.apply(next_retry_at=None)
            if ctx.state.stream is not None:
                runtime.immediate = True
                self._network_lost(ctx)
            else:
                self._attempt(ctx)
            return True
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return super().reconnect(ctx, message)

    def _has_source(self, ctx):
        if ctx.state.mode is Mode.VIRTUAL:
            return ctx.state.display is DisplayState.Owned and ctx.state.display_handle is not None
        # Mirror may carry no local source at all: the backend owns the ScreenCast
        # session, so there is nothing to acquire before it can start or reconnect.
        return ctx.state.mode is Mode.MIRROR

    def _allowed(self, ctx):
        return (ctx.state.intent_active and not ctx.state.cleanup_pending
                and not runtime_for(ctx).active and self._has_source(ctx)
                and ctx.state.trust not in _BLOCKED_TRUST
                and not _runtime(ctx).require_pairing and ctx.state.accepted_resolution is None)

    def _network_lost(self, ctx, error=None):
        if not ctx.state.intent_active or runtime_for(ctx).active or ctx.state.cleanup_pending:
            return
        ctx.cancel_timers()
        ctx.cancel_audio()
        operation = ctx.operation()
        ctx.apply(operation_id=operation.operation_id, transport_lease_id=None,
                  next_retry_at=None, transport=TransportState.Reconnecting,
                  intent_stage=IntentStage.active, audio=AudioState.Disabled,
                  last_error=error or ctx.state.last_error)
        if ctx.state.stream is not None:
            self._pause(ctx, "network")
            ctx.apply(transport=TransportState.Reconnecting)
        else:
            self._schedule(ctx)

    def _schedule(self, ctx):
        if not self._allowed(ctx) or _runtime(ctx).local_required or ctx.state.next_retry_at is not None:
            return
        operation = ctx.operation()
        when = self.deps.clock.now() + network_delay(ctx.state.reconnect_attempt)
        ctx.apply(operation_id=operation.operation_id, transport=TransportState.Reconnecting,
                  intent_stage=IntentStage.active, next_retry_at=when,
                  reconnect_attempt=ctx.state.reconnect_attempt + 1)
        ctx.schedule("reconnect", when, _current_context(ctx))

    def _attempt(self, ctx):
        self._sync_target(ctx)
        if not ctx.state.intent_active or ctx.state.cleanup_pending or runtime_for(ctx).active:
            return
        ctx.cancel_timers()
        operation = ctx.operation()
        ctx.apply(operation_id=operation.operation_id, next_retry_at=None)
        if _runtime(ctx).local_required:
            self._request_local(ctx, _runtime(ctx).dead_instance)
        elif ctx.state.target is None:
            self._refresh_discovery(ctx)
            self._schedule(ctx)
        elif self._allowed(ctx):
            if ctx.state.stream is not None:
                _runtime(ctx).immediate = True
                self._network_lost(ctx)
            else:
                self._prepare_audio(ctx)
        elif not self._has_source(ctx):
            super().reconnect(ctx, Retry(receiver_id=ctx.state.receiver_id))

    def _begin_stream(self, ctx):
        self._sync_target(ctx)
        if not self._allowed(ctx) or _runtime(ctx).local_required:
            return
        if ctx.state.target is None or ctx.state.stream is not None or _virtual_runtime(ctx).pause_inflight:
            self._schedule(ctx)
            return
        target = ctx.state.target
        source = ctx.state.display_handle.source if ctx.state.mode is Mode.VIRTUAL else ctx.state.physical_source
        runtime = _virtual_runtime(ctx)
        runtime.pending_ready = runtime.ready_stream = None
        operation = ctx.new_transport_lease(_current_context(ctx))
        policy = ctx.state.audio_policy
        ctx.apply(transport=TransportState.Connecting, intent_stage=IntentStage.starting)

        async def start():
            # Selection/probe may have crossed an await. Check the discovery
            # authority again before handing any command to the transport.
            view = self.deps.discovery.current(ctx.state.receiver_id)
            current = ctx._manager._target_from_view(view) if view is not None and view.online else None
            if _endpoint(current) != _endpoint(target):
                return Err(error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id))
            self.deps.transport.update_target(target, ctx.state.receiver_id)
            return await self._with_backend_events(
                ctx, operation,
                lambda: self.deps.transport.start_stream(target, source, policy, operation),
                acquisition=True,
            )

        ctx.launch(Route.reconnect, "transport_start", operation, start, acquisition=True)

    def _effect(self, ctx, message):
        kind = message.kind.rsplit(":", 1)[-1]
        if kind == "discovery_refresh":
            return True
        if kind == "daemon_restart":
            if isinstance(message.result, Err):
                ctx.cancel_timers()
                ctx.apply(transport=TransportState.Failed, intent_stage=IntentStage.active,
                          next_retry_at=None, last_error=message.result.error,
                          cleanup_pending=message.result.error.code is ErrorCode.ownership_mismatch)
            else:
                runtime = _runtime(ctx)
                runtime.local_required = False
                runtime.dead_instance = None
                runtime.last_instance = message.result.value.instance_uuid
                ctx.apply(stream=None, transport=TransportState.Reconnecting,
                          intent_stage=IntentStage.active, last_error=None)
                self._attempt(ctx)
            return True
        if kind == "transport_start":
            self._close_start_bridge(ctx, message.operation_id)
            runtime = _virtual_runtime(ctx)
            if self._sync_target(ctx):
                runtime.pending_ready = None
                self._network_lost(ctx)
                return self._retired_start(ctx, message)
            if isinstance(message.result, Err):
                runtime.pending_ready = None
                if runtime.resume_after_start:
                    runtime.resume_after_start = False
                    self._attempt(ctx)
                elif ctx.state.trust not in _BLOCKED_TRUST:
                    self._connection_failed(ctx, message.result.error)
                return True
            handle = message.result.value
            if not isinstance(handle, StreamHandle):
                self._connection_failed(ctx, diagnostic(ErrorCode.transport_failed, ctx.state.receiver_id))
                return True
            _runtime(ctx).last_instance = handle.daemon_instance_uuid
            ctx.apply(stream=handle, transport=TransportState.Connecting, intent_stage=IntentStage.starting)
            if runtime.resume_after_start:
                runtime.resume_after_start = False
                self._pause(ctx, "retry")
                return True
            if ctx.state.trust in _BLOCKED_TRUST:
                self._pause(ctx, "pairing")
                return True
            pending, runtime.pending_ready = runtime.pending_ready, None
            if pending is not None and accept_transport(ctx.state, pending):
                self._transport_event(ctx, pending)
            if ctx.state.mode is Mode.MIRROR:
                # Preserve MirrorLifecycle's accepted-stream contract. Virtual
                # setup still waits for a fenced ready event before verification.
                self._stream_ready(ctx)
            return True
        return super()._effect(ctx, message)

    def _connection_failed(self, ctx, error):
        ctx.cancel_audio()
        ctx.apply(transport_lease_id=None, audio=AudioState.Disabled, last_error=error,
                  intent_stage=IntentStage.active)
        if error.code is ErrorCode.daemon_failed:
            self._request_local(ctx, _runtime(ctx).last_instance)
        elif error.code in _TERMINAL:
            ctx.cancel_timers()
            ctx.apply(transport=TransportState.Failed, next_retry_at=None)
        elif error.code in (ErrorCode.ownership_mismatch, ErrorCode.trust_cleanup_required):
            ctx.apply(transport=TransportState.Failed, cleanup_pending=True, next_retry_at=None)
        else:
            ctx.apply(transport=TransportState.Reconnecting)
            self._schedule(ctx)

    def _transport_event(self, ctx, event):
        if self._sync_target(ctx):
            self._network_lost(ctx)
            return True
        if event.kind is TransportEventKind.daemon_exit:
            self._request_local(ctx, event.daemon_instance_uuid)
            return True
        if event.kind is TransportEventKind.network_lost:
            error = event.payload.diagnostic if isinstance(event.payload, FailurePayload) else None
            self._network_lost(ctx, error)
            return True
        if event.kind is TransportEventKind.failed:
            error = event.payload.diagnostic if isinstance(event.payload, FailurePayload) else diagnostic(ErrorCode.transport_failed)
            if error.code is ErrorCode.daemon_failed:
                self._request_local(ctx, event.daemon_instance_uuid)
                return True
            if error.code in _TERMINAL:
                _runtime(ctx).failure_after_stop = error
            self._network_lost(ctx, error)
            return True
        if event.kind is TransportEventKind.ready:
            if event.context.trust_generation == ctx.state.trust_generation:
                self._stream_ready(ctx)
            return True
        return super()._transport_event(ctx, event)

    def _stream_ready(self, ctx):
        if not self._allowed(ctx) or _virtual_runtime(ctx).pause_inflight:
            return
        if ctx.state.mode is Mode.VIRTUAL:
            super()._stream_ready(ctx)
        else:
            handle = ctx.state.stream
            if handle is None:
                return
            if ctx.state.physical_source is None and handle.source_connector is not None:
                # A desktop capture has no local lease, so record the connector
                # the backend actually captured for the pane to report.
                ctx.apply(physical_source=PhysicalMonitorSource(
                    connector=handle.source_connector, portal_restore_token=None,
                ))
            runtime = _virtual_runtime(ctx)
            ctx.apply(transport=TransportState.Streaming, intent_stage=IntentStage.active)
            if runtime.ready_stream != handle:
                runtime.ready_stream = handle
                if ctx.state.audio_policy.enabled:
                    audio_id = ctx.operation().operation_id
                    ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id)
                    operation = _current_context(ctx, audio=audio_id)
                    ctx.launch(Route.audio, "audio_start", operation,
                               lambda: self.deps.transport.start_audio(handle, operation))
        if ctx.state.transport is TransportState.Streaming:
            ctx.cancel_timers()
            ctx.apply(reconnect_attempt=0, next_retry_at=None, last_error=None)

    def _request_local(self, ctx, instance):
        if not ctx.state.intent_active or runtime_for(ctx).active or ctx.state.cleanup_pending:
            return
        runtime = _runtime(ctx)
        runtime.local_required, runtime.dead_instance = True, instance
        ctx.cancel_timers()
        ctx.cancel_audio()
        operation = ctx.operation()
        # Reconciliation, rather than an arbitrary daemon notification, owns
        # proof that the old process has released its streams. Until the shared
        # transaction confirms success, keep the exact handle independently
        # owned with its command lease fenced out.
        ctx.apply(operation_id=operation.operation_id, transport_lease_id=None,
                  audio=AudioState.Disabled, transport=TransportState.Reconnecting,
                  intent_stage=IntentStage.recovering, next_retry_at=None,
                  last_error=diagnostic(ErrorCode.daemon_failed, ctx.state.receiver_id))
        ctx.launch(Route.reconnect, "daemon_restart", _current_context(ctx),
                   lambda: self.recovery.recover(instance))

    def _paused(self, ctx, message):
        reason = _virtual_runtime(ctx).pause_reason
        result = super()._paused(ctx, message)
        if reason != "network" or runtime_for(ctx).active or not ctx.state.intent_active:
            return result
        runtime = _runtime(ctx)
        if isinstance(message.result, Err):
            runtime.release_failed = True
            ctx.apply(cleanup_pending=True, intent_stage=IntentStage.cleanup)
        else:
            runtime.release_failed = False
            if runtime.failure_after_stop is not None:
                error, runtime.failure_after_stop = runtime.failure_after_stop, None
                ctx.apply(transport=TransportState.Failed, next_retry_at=None, last_error=error)
            elif runtime.immediate:
                runtime.immediate = False
                self._attempt(ctx)
            else:
                self._schedule(ctx)
        return result

    def cleanup_route(self, ctx, message):
        if isinstance(message, (Stop, Cancel)) or isinstance(message, ConfirmSwitch) and message.confirmed:
            self._retire_terminal_error(ctx)
            ctx.cancel_timers()
            _runtime(ctx).local_required = False
            ctx.apply(next_retry_at=None, reconnect_attempt=0)
        if isinstance(message, RetryCleanup) and _runtime(ctx).release_failed and not runtime_for(ctx).active:
            ctx.apply(cleanup_pending=False, intent_stage=IntentStage.active)
            self._pause(ctx, "network")
            return True
        if isinstance(message, EffectEvent) and message.kind == "cleanup:transport_start":
            self._close_start_bridge(ctx, message.operation_id)
            if ctx.state.intent_active and self._has_source(ctx) and not runtime_for(ctx).active:
                # A retired attempt owns only its exact late stream. Resource-
                # free failures cannot mutate the current intent or source.
                return self._retired_start(ctx, message)
        if isinstance(message, RetryCleanup) and runtime_for(ctx).late_resources and not runtime_for(ctx).active:
            for resource in tuple(runtime_for(ctx).late_resources):
                if isinstance(resource, StreamHandle):
                    self._release_stale_stream(ctx, resource)
            return True
        return super().cleanup_route(ctx, message)

    def _retired_start(self, ctx, message):
        if isinstance(message.result, Ok) and isinstance(message.resource, StreamHandle):
            runtime = runtime_for(ctx)
            if message.resource not in runtime.late_resources:
                runtime.late_resources.append(message.resource)
            self._release_stale_stream(ctx, message.resource)
            # The manager retains this acquisition until its exact stop ack.
            return False
        return True

    def trust_route(self, ctx, message):
        if isinstance(message, Pair):
            if self._sync_target(ctx) and ctx.state.intent_active:
                self._network_lost(ctx)
        if isinstance(message, Pair) and _runtime(ctx).require_pairing and ctx.state.intent_active:
            if ctx.state.target is None or not ctx.state.online:
                return False
            return self.pairing.begin_from_backend(ctx)
        result = super().trust_route(ctx, message)
        if ctx.state.trust is TrustState.Trusted:
            _runtime(ctx).require_pairing = False
        return result

    def audio(self, ctx, message):
        if isinstance(message, SetMuted):
            if self._sync_target(ctx) and ctx.state.intent_active:
                self._network_lost(ctx)
            if ctx.state.target is None or not ctx.state.online:
                return False
        return super().audio(ctx, message)

    def _start_after_trust(self, ctx):
        _runtime(ctx).require_pairing = False
        runtime = _virtual_runtime(ctx)
        runtime.pairing_requested = False
        if self._has_source(ctx):
            # Loss of fallback identity also invalidates the old resolution
            # decision. Retain the output until the user chooses reconfiguration.
            if ctx.state.mode is Mode.VIRTUAL and ctx.state.resolution is None:
                return False
            if runtime.pause_inflight:
                runtime.pause_reason = "retry"
            elif ctx.outstanding_acquisitions(names=frozenset({"transport_start"})):
                runtime.resume_after_start = True
            else:
                self._attempt(ctx)
            return True
        return super()._start_after_trust(ctx)


def build_reconnect_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    """Return the reconnect route and one shared recovery authority."""
    lifecycle = ReconnectLifecycle(deps)
    return {Route.reconnect: lifecycle.reconnect}


def compose_reconnect_handlers(
    lifecycle: Mapping[Route, Handler], reconnect: Mapping[Route, Handler],
) -> Mapping[Route, Handler]:
    """Bind shared continuations, as ``compose_virtual_handlers`` does.

    The factory remains disjoint (only reconnect); this explicit composition
    replaces earlier lifecycle continuations so initial acquisitions, retries,
    pairing, late cleanup, and queued starts use the same owner and fences.
    """
    handler = reconnect.get(Route.reconnect)
    owner = getattr(handler, "__self__", None)
    if not isinstance(owner, ReconnectLifecycle):
        raise TypeError("reconnect routes must come from build_reconnect_handlers")
    if reconnect.keys() - {Route.reconnect}:
        raise ValueError("unexpected reconnect routes")
    return {
        **lifecycle, **reconnect, Route.mirror: owner.mirror, Route.virtual: owner,
        Route.resolution: owner.resolution, Route.cleanup: owner.cleanup_route,
        Route.trust: owner.trust_route, Route.audio: owner.audio,
    }


__all__ = [
    "DaemonRecovery", "build_reconnect_handlers",
    "compose_reconnect_handlers", "network_delay",
]
