"""Ordered, idempotent session teardown.

This module deliberately treats cleanup as a transaction rather than as a single
``stop`` call.  The actor first publishes display removal intent, then releases
transport and audio control, then releases the exact capture/display resources.
The small runtime object is attached to the per-receiver ``SessionContext``; it
is only touched while that actor is dispatching a message.  The durable resource
facts remain in :class:`SessionSnapshot` and in the provider's ownership store.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel,
    ConfirmSwitch,
    EffectEvent,
    KeepCleanup,
    RetryCleanup,
    Start,
    Stop,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    DisplayHandle,
    OperationContext,
    OperationId,
    OwnerId,
    StreamHandle,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    AudioState,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Ok,
    PhysicalMonitorSource,
    Result,
    StartRequest,
    TransportState,
    TrustState,
)

from .engine import Handler, Route, SessionContext


_CLEANUP_ACQUISITION_NAMES = frozenset(
    {
        "capture_select",
        "transport_start",
        "display_create",
        "virtual_create",
        "create",
    }
)


@dataclass
class CleanupRuntime:
    """Ephemeral transaction bookkeeping for one actor context.

    Nothing in this object is used as authoritative ownership evidence.  It only
    prevents duplicate effects while a command is being retried and remembers
    the exact owners returned by ``prepare_teardown`` until their acknowledgements
    arrive.
    """

    active: bool = False
    context: OperationContext | None = None
    superseded_operation_id: OperationId | None = None
    prepare_inflight: bool = False
    prepare_again: bool = False
    prepared: bool = False
    transport_inflight: bool = False
    transport_done: bool = False
    stopped_stream: StreamHandle | None = None
    audio_inflight: bool = False
    audio_done: bool = False
    capture_inflight: bool = False
    capture_done: bool = False
    owners: tuple[OwnerId, ...] = ()
    removed: set[OwnerId] = field(default_factory=set)
    display_inflight: set[OwnerId] = field(default_factory=set)
    late_resources: list[StreamHandle | DisplayHandle | PhysicalMonitorSource] = field(
        default_factory=list,
    )
    late_inflight: set[StreamHandle | DisplayHandle | PhysicalMonitorSource] = field(
        default_factory=set,
    )
    trust_pending: bool = False
    queued_start: StartRequest | None = None
    finishing: bool = False


def runtime_for(ctx: SessionContext) -> CleanupRuntime:
    """Return actor-local cleanup state without creating global mutable state."""
    runtime = getattr(ctx, "_task2_cleanup_runtime", None)
    if runtime is None:
        runtime = CleanupRuntime()
        setattr(ctx, "_task2_cleanup_runtime", runtime)
    return runtime


def cleanup_complete(
    state: SessionSnapshot,
    outstanding_acquisitions: int,
    pending_handles: tuple[
        StreamHandle | DisplayHandle | PhysicalMonitorSource, ...
    ],
) -> bool:
    """Whether every resource has *confirmed* release.

    A failed state is not evidence of release.  Trust is intentionally absent
    from this predicate: transport/display cleanup may complete while trust
    revocation remains independently blocked, in which case the derived phase
    is ``TrustCleanupRequired`` and new starts are still refused.
    """
    if type(outstanding_acquisitions) is not int or outstanding_acquisitions < 0:
        raise TypeError("outstanding_acquisitions must be a nonnegative integer")
    if type(pending_handles) is not tuple:
        raise TypeError("pending_handles must be a tuple")
    if outstanding_acquisitions != 0 or pending_handles:
        return False
    return (
        state.stream is None
        and state.transport is TransportState.Idle
        and state.physical_source is None
        and state.audio is AudioState.Disabled
        and state.display_handle is None
        and state.ownership_uuid is None
        and state.display is DisplayState.None_
        and not state.cleanup_pending
    )


def _result_error(result: object, default: ErrorCode = ErrorCode.cleanup_timeout):
    if isinstance(result, Err):
        return result.error
    return diagnostic(default)


def _owned_display_owners(
    state: SessionSnapshot, owners: tuple[OwnerId, ...],
) -> tuple[OwnerId, ...]:
    values = list(owners)
    if state.ownership_uuid is not None and state.ownership_uuid not in values:
        values.insert(0, state.ownership_uuid)
    if state.display_handle is not None:
        owner = state.display_handle.owner_uuid
        if owner not in values:
            values.insert(0, owner)
    return tuple(values)


async def _optional_audio_stop(
    deps: object, handle: StreamHandle | None, context: OperationContext,
) -> Result[None]:
    """Invoke an assembly-provided audio-control stop when one exists.

    Task 1 intentionally keeps the audio probe small and does not invent a
    transport ``stop_audio`` operation.  Assemblies that own an audio control
    lease may provide either ``audio_probe.stop_control(handle, context)`` or
    ``transport.stop_audio(handle, context)``.  With no such lease there is
    nothing to call; synchronously clearing the actor's audio token is the
    confirmed local release.
    """
    method = getattr(getattr(deps, "audio_probe", None), "stop_control", None)
    if not callable(method):
        method = getattr(getattr(deps, "transport", None), "stop_audio", None)
    if not callable(method):
        return Ok(value=None)
    try:
        value = method(handle, context)
        if inspect.isawaitable(value):
            value = await value
    except TypeError:
        # A small number of assembly adapters expose only ``stop_control(ctx)``.
        # This fallback is limited to the optional seam; adapter exceptions are
        # converted to a closed diagnostic below.
        try:
            value = method(context)
            if inspect.isawaitable(value):
                value = await value
        except Exception:  # noqa: BLE001 - never copy adapter failures
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))
    except Exception:  # noqa: BLE001 - never copy adapter failures
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))
    if isinstance(value, (Ok, Err)):
        return value
    return Err(error=diagnostic(ErrorCode.cleanup_timeout))


class OrderedCleanup:
    """Route handler implementing the universal teardown transaction."""

    def __init__(
        self,
        deps: object,
        *,
        start_callback: Callable[[SessionContext, StartRequest], bool] | None = None,
        cancel_pairing_callback: Callable[
            [SessionContext, object | None, OperationContext], None
        ]
        | None = None,
    ) -> None:
        self.deps = deps
        self.start_callback = start_callback
        self.cancel_pairing_callback = cancel_pairing_callback

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, (Stop, Cancel)):
            self._stop(ctx, None)
            return True
        if isinstance(message, ConfirmSwitch):
            if not message.confirmed:
                return True
            self._stop(ctx, message.request)
            return True
        if isinstance(message, RetryCleanup):
            self._retry(ctx)
            return True
        if isinstance(message, KeepCleanup):
            # Keeping an unresolved resource is an explicit choice.  It must
            # remain visible and must never be reported as released.
            if ctx.state.cleanup_pending:
                ctx.apply(
                    intent_active=False,
                    intent_stage=IntentStage.cleanup,
                    last_error=diagnostic(ErrorCode.cleanup_timeout),
                )
            return True
        if isinstance(message, TransportEvent):
            if message.kind is not TransportEventKind.stopped:
                return False
            runtime = runtime_for(ctx)
            if message.stream is not None and ctx.state.stream == message.stream:
                ctx.apply(
                    stream=None,
                    transport=TransportState.Idle,
                    transport_lease_id=None,
                )
            runtime.transport_inflight = False
            runtime.transport_done = False
            self._continue(ctx)
            return True
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return False

    # -- transaction admission --------------------------------------------

    def _stop(self, ctx: SessionContext, queued: StartRequest | None) -> None:
        runtime = runtime_for(ctx)
        if runtime.active:
            if queued is not None:
                runtime.queued_start = queued
                ctx.apply(queued_start=queued)
            else:
                # A plain Stop/Cancel supersedes a previously confirmed switch
                # that is still waiting for cleanup; it must not resurrect that
                # request after the transaction completes.
                runtime.queued_start = None
                ctx.apply(queued_start=None)
            self._continue(ctx)
            return

        state = ctx.state
        # A repeated Stop after a confirmed, resource-free stop is a no-op.  In
        # particular it does not consume another session generation.
        if (
            not state.intent_active
            and state.intent_stage is IntentStage.idle
            and not state.cleanup_pending
            and state.transport is TransportState.Idle
            and state.audio is AudioState.Disabled
            and state.display is DisplayState.None_
            and state.challenge is None
            and state.trust is not TrustState.Pairing
            and state.stream is None
            and state.physical_source is None
            and state.display_handle is None
            and state.ownership_uuid is None
            and not runtime.late_resources
            and not ctx.outstanding_acquisitions()
        ):
            return

        old_operation = state.operation_id
        old_challenge = state.challenge
        # A context owns one ephemeral cleanup transaction at a time.  A later
        # Stop after a completed start/stop cycle must not inherit the prior
        # stage completion flags or old display-owner acknowledgements.  Keep
        # late_resources: those are exact provider handles that still need the
        # new transaction to release them if a previous superseded acquisition
        # was remembered outside the public snapshot.
        runtime.prepare_inflight = False
        runtime.prepare_again = False
        runtime.prepared = False
        runtime.transport_inflight = False
        runtime.transport_done = False
        runtime.stopped_stream = None
        runtime.audio_inflight = False
        runtime.audio_done = False
        runtime.capture_inflight = False
        runtime.capture_done = False
        runtime.owners = ()
        runtime.removed.clear()
        runtime.display_inflight.clear()
        runtime.finishing = False
        context = ctx.supersede("stop")
        runtime.active = True
        runtime.context = context
        runtime.superseded_operation_id = old_operation
        runtime.queued_start = queued
        runtime.trust_pending = old_challenge is not None or state.trust is TrustState.Pairing
        ctx.apply(
            intent_active=False,
            intent_stage=IntentStage.stopping,
            cleanup_pending=True,
            queued_start=queued,
            transport=TransportState.Stopping
            if state.stream is not None
            else state.transport,
            display=DisplayState.Removing
            if (state.display_handle is not None or state.ownership_uuid is not None)
            else state.display,
            trust=TrustState.RevocationPending
            if runtime.trust_pending
            else state.trust,
            last_error=state.last_error,
        )

        # Keep the old challenge id only in this transient closure.  The public
        # snapshot was synchronously cleared by ``supersede``.
        if runtime.trust_pending and self.cancel_pairing_callback is not None:
            self.cancel_pairing_callback(
                ctx,
                old_challenge.challenge_id if old_challenge is not None else None,
                context,
            )

        self._launch_prepare(ctx)

    def _launch_prepare(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        if runtime.prepared or runtime.prepare_inflight or runtime.context is None:
            return
        runtime.prepare_inflight = True
        runtime.prepare_again = False
        context = runtime.context
        ctx.launch(
            Route.cleanup,
            "prepare_teardown",
            context,
            lambda: self.deps.display.prepare_teardown(
                ctx.state.receiver_id, runtime.superseded_operation_id, context,
            ),
        )

    def _retry(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        if not runtime.active:
            # A caller may retry a durable cleanup snapshot restored without the
            # ephemeral runtime.  Starting a new transaction is safe and does
            # not need another generation when the snapshot already says stop.
            if ctx.state.cleanup_pending or ctx.state.intent_stage is IntentStage.stopping:
                self._stop(ctx, runtime.queued_start or ctx.state.queued_start)
            return
        self._continue(ctx)

    def _continue(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        if not runtime.active:
            # Resource cleanup can finish before a failed trust cancellation or
            # revocation is retried.  Once the coordinator confirms trust is
            # safe, wake a queued mode switch without consuming another Stop
            # generation or starting before the barrier is complete.
            queued = runtime.queued_start
            if (
                queued is not None
                and not ctx.state.cleanup_pending
                and ctx.state.trust not in (
                    TrustState.CleanupRequired,
                    TrustState.RevocationPending,
                )
            ):
                runtime.queued_start = None
                ctx.apply(queued_start=None)
                if self.start_callback is not None:
                    self.start_callback(ctx, queued)
            return
        if runtime.context is None:
            return
        if not runtime.prepared:
            if not runtime.prepare_inflight:
                self._launch_prepare(ctx)
            return
        if not runtime.transport_done:
            if not runtime.transport_inflight:
                self._launch_transport_stop(ctx)
            return
        if not runtime.audio_done:
            if not runtime.audio_inflight:
                self._launch_audio_stop(ctx)
            return
        if not runtime.capture_done:
            if not runtime.capture_inflight:
                self._launch_capture_release(ctx)
            return
        # A creator may still return an exact handle for one of the published
        # owners. Wait for delivery so its release can acknowledge that handle.
        if ctx.outstanding_acquisitions(names=frozenset({"display_create", "virtual_create", "create"})):
            return
        self._launch_display_removals(ctx)
        self._finish_if_possible(ctx)

    # -- ordered stages -----------------------------------------------------

    def _launch_transport_stop(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None:
            return
        stream = ctx.state.stream
        if stream is None:
            late_streams = tuple(
                resource for resource in runtime.late_resources
                if isinstance(resource, StreamHandle)
            )
            for resource in late_streams:
                self._release_late(ctx, resource)
            if (
                late_streams
                or any(isinstance(resource, StreamHandle) for resource in ctx._manager.pending_resources(ctx.state.receiver_id))
                or ctx.outstanding_acquisitions(names=frozenset({"transport_start"}))
            ):
                return
            runtime.transport_done = True
            ctx.apply(transport=TransportState.Idle, transport_lease_id=None)
            self._continue(ctx)
            return
        runtime.transport_inflight = True
        runtime.stopped_stream = stream
        ctx.apply(transport=TransportState.Stopping)
        ctx.launch(
            Route.cleanup,
            "transport_stop",
            context,
            lambda stream=stream, context=context: self.deps.transport.stop_stream(
                stream, context,
            ),
            resource=stream,
        )

    def _launch_audio_stop(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None:
            return
        if ctx.state.audio is AudioState.Disabled:
            runtime.audio_done = True
            ctx.cancel_audio()
            self._continue(ctx)
            return
        runtime.audio_inflight = True
        # The audio operation token is invalidated only after transport release
        # is confirmed.  A late audio event therefore cannot mutate a new lease.
        ctx.launch(
            Route.cleanup,
            "audio_stop_control",
            context,
            lambda: _optional_audio_stop(self.deps, runtime.stopped_stream, context),
        )

    def _launch_capture_release(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None:
            return
        source = ctx.state.physical_source
        if source is None:
            late_sources = tuple(
                resource for resource in runtime.late_resources
                if isinstance(resource, PhysicalMonitorSource)
            )
            for resource in late_sources:
                self._release_late(ctx, resource)
            if late_sources:
                return
            runtime.capture_done = True
            self._continue(ctx)
            return
        runtime.capture_inflight = True
        ctx.launch(
            Route.cleanup,
            "capture_release",
            context,
            lambda source=source: self.deps.capture.release(source),
            resource=source,
        )

    def _launch_display_removals(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None:
            return
        owners = _owned_display_owners(ctx.state, runtime.owners)
        runtime.owners = owners
        # Late display owners follow the same ordered stage as snapshot owners.
        for owner in owners:
            if owner in runtime.removed or owner in runtime.display_inflight:
                continue
            runtime.display_inflight.add(owner)
            handle = (
                ctx.state.display_handle
                if ctx.state.display_handle is not None
                and ctx.state.display_handle.owner_uuid == owner
                else next(
                    (
                        resource
                        for resource in runtime.late_resources
                        if isinstance(resource, DisplayHandle)
                        and resource.owner_uuid == owner
                    ),
                    None,
                )
            )
            ctx.launch(
                Route.cleanup,
                f"display_remove_{owner}",
                context,
                lambda owner=owner, context=context: self.deps.display.remove(
                    owner, context,
                ),
                resource=handle,
            )
        if not runtime.owners or runtime.removed.issuperset(runtime.owners):
            runtime.capture_done = True
            # No owned output was admitted.  This is a confirmed empty display
            # stage, not an inferred release of a handle.
            if not runtime.display_inflight:
                ctx.apply(
                    display=DisplayState.None_,
                    display_handle=None,
                    ownership_uuid=None,
                )

    # -- completion delivery ----------------------------------------------

    def _effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        kind = message.kind.rsplit(":", 1)[-1]
        if message.kind.startswith("cleanup:"):
            if kind in _CLEANUP_ACQUISITION_NAMES:
                return self._late_acquisition(ctx, message)
            if kind.startswith("late_"):
                # The original acquisition stays in the manager's pending ledger
                # until this exact release acknowledgement is handled.  The
                # manager removes it after this handler returns, so enqueue a
                # recheck for the following actor turn.  A failed release must
                # leave the resource pending but clear the in-flight guard so
                # RetryCleanup can issue the same exact release again.
                runtime = runtime_for(ctx)
                if message.resource is not None:
                    runtime.late_inflight.discard(message.resource)
                if isinstance(message.result, Ok) and message.resource is not None:
                    if message.resource in runtime.late_resources:
                        runtime.late_resources.remove(message.resource)
                    self._enqueue_recheck(ctx)
                    return True
                if isinstance(message.result, Err):
                    ctx.apply(
                        cleanup_pending=True,
                        intent_stage=IntentStage.cleanup,
                        last_error=message.result.error,
                    )
                    return True
                return False

        runtime = runtime_for(ctx)
        if kind == "recheck":
            self._continue(ctx)
            self._finish_if_possible(ctx)
            return True

        if kind == "prepare_teardown":
            runtime.prepare_inflight = False
            if isinstance(message.result, Err):
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    display=DisplayState.CleanupRequired,
                    last_error=message.result.error,
                )
                return True
            owners = message.result.value if isinstance(message.result, Ok) else ()
            if type(owners) is not tuple:
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    display=DisplayState.CleanupRequired,
                    last_error=diagnostic(ErrorCode.cleanup_timeout),
                )
                return True
            runtime.owners = tuple(dict.fromkeys(
                (*runtime.owners, *(owner for owner in owners if owner is not None)),
            ))
            # If a new owner arrived while publication was suspended, publish
            # again before advancing. The provider's earlier snapshot may not
            # have included that reservation.
            runtime.prepared = not runtime.prepare_again
            runtime.owners = _owned_display_owners(ctx.state, runtime.owners)
            if runtime.owners:
                ctx.apply(display=DisplayState.Removing)
            self._continue(ctx)
            return True

        if kind == "transport_stop":
            runtime.transport_inflight = False
            if isinstance(message.result, Err):
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    transport=TransportState.Stopping,
                    last_error=message.result.error,
                )
                return True
            runtime.transport_done = False
            stream = runtime.stopped_stream
            if stream is None or ctx.state.stream == stream:
                ctx.apply(
                    stream=None,
                    transport=TransportState.Idle,
                    transport_lease_id=None,
                )
            self._continue(ctx)
            return True

        if kind == "audio_stop_control":
            runtime.audio_inflight = False
            if isinstance(message.result, Err):
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    last_error=message.result.error,
                )
                return True
            runtime.audio_done = True
            ctx.cancel_audio()
            ctx.apply(audio=AudioState.Disabled)
            self._continue(ctx)
            return True

        if kind == "capture_release":
            runtime.capture_inflight = False
            if isinstance(message.result, Err):
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    last_error=message.result.error,
                )
                return True
            runtime.capture_done = False
            source = ctx.state.physical_source
            if source is None or message.resource in (None, source):
                ctx.apply(physical_source=None)
            self._continue(ctx)
            return True

        if kind.startswith("display_remove_"):
            owner_text = kind.removeprefix("display_remove_")
            owner = next((candidate for candidate in runtime.owners if str(candidate) == owner_text), None)
            if owner is None:
                # ``OwnerId`` is a UUID NewType in production.  Do not parse
                # untrusted event text into an ownership identity.
                return True
            runtime.display_inflight.discard(owner)
            if isinstance(message.result, Err):
                ctx.apply(
                    cleanup_pending=True,
                    intent_stage=IntentStage.cleanup,
                    display=DisplayState.CleanupRequired,
                    last_error=message.result.error,
                )
                return True
            runtime.removed.add(owner)
            for resource in tuple(runtime.late_resources):
                if isinstance(resource, DisplayHandle) and resource.owner_uuid == owner:
                    runtime.late_inflight.discard(resource)
                    runtime.late_resources.remove(resource)
            if ctx.state.display_handle is not None and ctx.state.display_handle.owner_uuid == owner:
                ctx.apply(display_handle=None, ownership_uuid=None)
            if runtime.removed.issuperset(runtime.owners) and not runtime.display_inflight:
                ctx.apply(display=DisplayState.None_, display_handle=None, ownership_uuid=None)
            # If this was a late display acquisition, the manager removes its
            # pending-ledger entry only after this handler returns.  Recheck on
            # the next actor turn just like late capture/transport releases.
            self._enqueue_recheck(ctx)
            self._finish_if_possible(ctx)
            return True

        # Resource releases for which the provider did not have a matching
        # snapshot handle are still acknowledged through their typed resource.
        if kind.startswith("late_"):
            return isinstance(message.result, Ok)
        return False

    def _late_acquisition(self, ctx: SessionContext, message: EffectEvent) -> bool:
        resource = message.resource
        runtime = runtime_for(ctx)
        if resource is not None and resource not in runtime.late_resources:
            runtime.late_resources.append(resource)
            if isinstance(resource, StreamHandle):
                runtime.transport_done = False
            elif isinstance(resource, PhysicalMonitorSource):
                runtime.capture_done = False
            elif resource.owner_uuid not in runtime.owners:
                runtime.owners = runtime.owners + (resource.owner_uuid,)
                runtime.prepared = False
                runtime.prepare_again = runtime.prepare_inflight
        # An acquisition can be rerouted here because endpoint churn or another
        # superseding command made it stale before an explicit Stop arrived.
        # Start the same ordered transaction in that case; merely remembering a
        # late handle would otherwise strand it when the runtime was idle.
        if not runtime.active:
            self._stop(ctx, None)
        # Recheck after this delivery removes acquisition bookkeeping, including
        # Err/no-resource outcomes. Successful handles remain in the separate
        # pending ledger until their exact release acknowledgement is delivered.
        self._enqueue_recheck(ctx)
        return resource is None

    def _release_late(
        self, ctx: SessionContext,
        resource: StreamHandle | DisplayHandle | PhysicalMonitorSource,
    ) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None or resource in runtime.late_inflight:
            return
        runtime.late_inflight.add(resource)
        name = f"late_{type(resource).__name__.lower()}"
        if isinstance(resource, PhysicalMonitorSource):
            ctx.launch(
                Route.cleanup,
                name,
                context,
                lambda resource=resource: self.deps.capture.release(resource),
                resource=resource,
            )
            return
        if isinstance(resource, StreamHandle):
            ctx.launch(
                Route.cleanup,
                name,
                context,
                lambda resource=resource: self.deps.transport.stop_stream(
                    resource, context,
                ),
                resource=resource,
            )
            return
        raise TypeError("late display handles must use the ordered display stage")

    def _enqueue_recheck(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        context = runtime.context
        if context is None:
            return
        ctx.enqueue(
            EffectEvent(
                receiver_id=ctx.state.receiver_id,
                operation_id=context.operation_id,
                session_generation=ctx.state.session_generation,
                kind="cleanup:recheck",
                result=Ok(value=None),
                resource=None,
            )
        )

    def _finish_if_possible(self, ctx: SessionContext) -> None:
        runtime = runtime_for(ctx)
        if (
            not runtime.active or runtime.finishing or not runtime.prepared
            or not runtime.transport_done or not runtime.audio_done or not runtime.capture_done
        ):
            return
        if (
            runtime.transport_inflight
            or runtime.audio_inflight
            or runtime.capture_inflight
            or runtime.display_inflight
            or runtime.late_inflight
        ):
            return
        pending = tuple(ctx._manager.pending_resources(ctx.state.receiver_id))
        if not cleanup_complete(ctx.state, ctx.outstanding_acquisitions(), pending):
            # ``cleanup_complete`` expects the actor's public snapshot to say
            # cleanup is no longer pending.  The transaction owns that final
            # transition, so test the same resource conditions before clearing it.
            if (
                ctx.state.stream is not None
                or ctx.state.transport is not TransportState.Idle
                or ctx.state.physical_source is not None
                or ctx.state.audio is not AudioState.Disabled
                or ctx.state.display_handle is not None
                or ctx.state.ownership_uuid is not None
                or ctx.state.display is not DisplayState.None_
                or pending
                or ctx.outstanding_acquisitions()
            ):
                return
        runtime.finishing = True
        ctx.apply(
            cleanup_pending=False,
            intent_active=False,
            intent_stage=IntentStage.idle,
            transport=TransportState.Idle,
            display=DisplayState.None_,
            audio=AudioState.Disabled,
            stream=None,
            physical_source=None,
            display_handle=None,
            ownership_uuid=None,
            transport_lease_id=None,
            audio_operation_id=None,
            last_error=None if ctx.state.last_error and ctx.state.last_error.code is ErrorCode.cleanup_timeout else ctx.state.last_error,
        )
        runtime.active = False
        runtime.finishing = False
        queued = runtime.queued_start
        if queued is None:
            ctx.apply(queued_start=None)
            return
        # A queued mode switch must wait for trust cancellation too.  Resource
        # cleanup itself remains complete even when trust is independently blocked.
        if runtime.trust_pending or ctx.state.trust in (
            TrustState.CleanupRequired,
            TrustState.RevocationPending,
        ):
            ctx.apply(queued_start=queued, intent_stage=IntentStage.cleanup)
            return
        ctx.apply(queued_start=None)
        runtime.queued_start = None
        if self.start_callback is not None:
            self.start_callback(ctx, queued)


# Public factory for callers that want only the cleanup route.  The lifecycle
# composition root normally supplies the two callbacks from mirror/pairing.
def build_cleanup_handler(
    deps: object,
    *,
    start_callback: Callable[[SessionContext, StartRequest], bool] | None = None,
    cancel_pairing_callback: Callable[
        [SessionContext, object | None, OperationContext], None
    ]
    | None = None,
) -> Handler:
    return OrderedCleanup(
        deps,
        start_callback=start_callback,
        cancel_pairing_callback=cancel_pairing_callback,
    )


__all__ = [
    "CleanupRuntime",
    "OrderedCleanup",
    "build_cleanup_handler",
    "cleanup_complete",
    "runtime_for",
]
