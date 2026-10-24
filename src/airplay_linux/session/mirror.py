"""Mirror-mode lifecycle composition.

Mirror is intentionally the narrow path: it accepts a ``PhysicalMonitorSource``,
verifies that exact source through ``CapturePort``, and never calls a display
creation API.  Pairing and ordered teardown are delegated to the sibling actor
handlers so their fences and ownership rules remain shared by every start.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    DiscoveryEvent,
    EffectEvent,
    Retry,
    RetryAudio,
    SetMuted,
    Start,
    StartVideoOnly,
    TimerEvent,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import OperationContext, OperationId, StreamHandle
from airplay_linux.domain.ports import ManagerPort
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    PhysicalMonitorSource,
    Result,
    StartRequest,
    TransportState,
    TrustState,
)

from .cleanup import OrderedCleanup, runtime_for
from .engine import Handler, ManagerDeps, Route, SessionContext
from .pairing import PairingFlow


def _current_context(ctx: SessionContext, *, audio: OperationId | None = None) -> OperationContext:
    """Build an effect fence that belongs to the current actor operation."""
    context = ctx.operation()
    if ctx.state.operation_id is not None:
        context = replace(context, operation_id=ctx.state.operation_id)
    if audio is not None:
        context = replace(context, audio_operation_id=audio)
    return context


def _audio_decision(policy: AudioPolicy, probe: Result[None] | None, explicit_video_only: bool) -> str:
    """Local fallback for the policy lane when running this lane alone."""
    try:
        from airplay_linux.policy.audio import audio_start_policy
    except ImportError:
        if explicit_video_only or not policy.enabled:
            return "video_only"
        return "start_audio" if isinstance(probe, Ok) else "decision_required"
    return audio_start_policy(policy, probe, explicit_video_only)


def _start_gate(deps: ManagerDeps, mode: Mode, trust: TrustState) -> Result[None]:
    """Use policy's gate when integrated, with the frozen fallback in isolation."""
    try:
        from airplay_linux.policy.capabilities import can_start
    except ImportError:
        report = deps.capabilities()
        if trust in (TrustState.CleanupRequired, TrustState.RevocationPending):
            return Err(error=diagnostic(ErrorCode.trust_cleanup_required))
        supported = report.mirror if mode is Mode.MIRROR else report.virtual_display
        if not supported:
            return Err(error=diagnostic(ErrorCode.unsupported_operation))
        return Ok(value=None)
    return can_start(deps.capabilities(), mode, trust)


class MirrorLifecycle:
    """State-machine handlers for mirror, reconnect, and local audio commands."""

    def __init__(self, deps: ManagerDeps) -> None:
        self.deps = deps
        self.pairing = PairingFlow(deps, start_after_trust=self._start_after_trust)
        self.cleanup = OrderedCleanup(
            deps,
            start_callback=self.start_request,
            cancel_pairing_callback=self.pairing.cancel_from_cleanup,
        )

    # -- route handlers -----------------------------------------------------

    def mirror(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, Start):
            self._start(ctx, message)
            return True
        if isinstance(message, EffectEvent):
            if message.kind.startswith("cleanup:"):
                return self.cleanup(ctx, message)
            return self._effect(ctx, message)
        return False

    def reconnect(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, TransportEvent):
            return self._transport_event(ctx, message)
        if isinstance(message, TimerEvent):
            if message.kind == "pairing_expiry":
                return self.pairing.expire(ctx)
            return False
        if isinstance(message, StartVideoOnly):
            if ctx.state.intent_stage is not IntentStage.audio_decision:
                return False
            ctx.apply(
                audio_policy=AudioPolicy(enabled=False),
                audio=AudioState.Disabled,
                intent_stage=IntentStage.starting,
                last_error=None,
            )
            self._begin_stream(ctx)
            return True
        if isinstance(message, Retry):
            if ctx.state.physical_source is None or not ctx.state.intent_active:
                return False
            if ctx.state.transport in (
                TransportState.Failed,
                TransportState.Reconnecting,
                TransportState.Connecting,
            ):
                self._begin_stream(ctx)
                return True
            return False
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return False

    def audio(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, RetryAudio):
            if (
                ctx.state.intent_stage is IntentStage.audio_decision
                and ctx.state.intent_active
                and ctx.state.physical_source is not None
                and ctx.state.stream is None
            ):
                self._retry_pre_ready_audio(ctx)
                return True
            if ctx.state.stream is None or ctx.state.audio is not AudioState.Unavailable:
                return False
            self._retry_audio(ctx)
            return True
        if isinstance(message, SetMuted):
            self._set_muted(ctx, message.muted)
            return True
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return False

    # -- start -------------------------------------------------------------

    def start_request(self, ctx: SessionContext, request: StartRequest) -> bool:
        """Start a queued request after cleanup has confirmed every resource."""
        self._start(
            ctx,
            Start(receiver_id=ctx.state.receiver_id, request=request),
            supersede=False,
        )
        return True

    def _start(
        self, ctx: SessionContext, message: Start, *, supersede: bool = True,
    ) -> None:
        request = message.request
        state = ctx.state
        if request.mode is not Mode.MIRROR:
            return
        if not isinstance(request.source, PhysicalMonitorSource):
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_source, state.receiver_id))
            return
        if not request.source.connector or type(request.source.connector) is not str:
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_source, state.receiver_id))
            return
        if state.target is None or not state.online:
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return
        if state.cleanup_pending or state.intent_active or state.stream is not None:
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.display_handle is not None or state.ownership_uuid is not None:
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.trust in (TrustState.CleanupRequired, TrustState.RevocationPending):
            ctx.apply(last_error=diagnostic(ErrorCode.trust_cleanup_required, state.receiver_id))
            return
        try:
            gate = _start_gate(self.deps, Mode.MIRROR, state.trust)
        except Exception:  # noqa: BLE001 - capability evidence is fail-closed
            gate = Err(error=diagnostic(ErrorCode.unsupported_operation, state.receiver_id))
        if isinstance(gate, Err):
            ctx.apply(last_error=gate.error)
            return
        try:
            self.deps.transport.update_target(state.target, state.receiver_id)
        except Exception:  # noqa: BLE001 - do not expose adapter details
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return

        context = ctx.supersede("start") if supersede else _current_context(ctx)
        ctx.apply(
            mode=Mode.MIRROR,
            intent_active=True,
            intent_stage=IntentStage.starting,
            transport=TransportState.Connecting,
            audio=AudioState.Disabled,
            audio_policy=request.audio,
            physical_source=None,
            last_error=None,
            accepted_resolution=None,
            observed_resolution=None,
        )
        source = request.source
        setattr(ctx, "_task2_requested_source", source)
        ctx.launch(
            Route.mirror,
            "capture_select",
            context,
            lambda source=source: self.deps.capture.select(source.connector),
            acquisition=True,
        )

    def _start_after_trust(self, ctx: SessionContext) -> bool:
        if (
            not ctx.state.intent_active
            or ctx.state.physical_source is None
            or ctx.state.stream is not None
        ):
            return False
        self._begin_stream(ctx)
        return True

    def _begin_stream(self, ctx: SessionContext) -> None:
        target = ctx.state.target
        source = ctx.state.physical_source
        if target is None or source is None:
            return
        if ctx.state.cleanup_pending:
            return
        context = _current_context(ctx)
        context = ctx.new_transport_lease(context)
        ctx.apply(
            transport=TransportState.Connecting,
            intent_stage=IntentStage.starting,
            intent_active=True,
        )
        policy = ctx.state.audio_policy
        ctx.launch(
            Route.mirror,
            "transport_start",
            context,
            lambda target=target, source=source, policy=policy, context=context: self.deps.transport.start_stream(
                target, source, policy, context,
            ),
            acquisition=True,
        )

    # -- effect delivery ---------------------------------------------------

    def _effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        kind = message.kind.rsplit(":", 1)[-1]
        if kind == "capture_select":
            if isinstance(message.result, Err):
                self._failed_start(ctx, message.result.error)
                return True
            source = message.result.value if isinstance(message.result, Ok) else None
            if not isinstance(source, PhysicalMonitorSource):
                self._failed_start(ctx, diagnostic(ErrorCode.unsupported_source, ctx.state.receiver_id))
                return True
            requested = getattr(ctx, "_task2_requested_source", None)
            # The provider owns the portal lease/token in the returned
            # descriptor, so equality of the full dataclass would reject a
            # legitimate acquisition of the requested connector.  Connector
            # identity is the stable user selection; a different connector is
            # a substituted physical source and must be released before fail.
            if requested.connector != source.connector:
                self._failed_start(
                    ctx,
                    diagnostic(ErrorCode.unsupported_source, ctx.state.receiver_id),
                    resource=source,
                )
                return True
            # The source is made authoritative immediately after selection so a
            # later verification failure or cancellation has an exact release
            # target.  The acquisition ledger is removed only after this handler
            # returns; verification itself is a separate effect.
            ctx.apply(physical_source=source)
            context = _current_context(ctx)
            ctx.launch(
                Route.mirror,
                "capture_verify",
                context,
                lambda source=source: self.deps.capture.verify(source),
            )
            return True

        if kind == "capture_verify":
            if isinstance(message.result, Err):
                self._failed_start(ctx, message.result.error)
                return True
            if not ctx.state.audio_policy.enabled:
                ctx.apply(audio=AudioState.Disabled, last_error=None)
                self._begin_stream(ctx)
                return True
            context = _current_context(ctx)
            ctx.launch(
                Route.mirror,
                "audio_probe",
                context,
                self.deps.audio_probe.check,
            )
            return True

        if kind == "audio_probe":
            probe = message.result if isinstance(message.result, (Ok, Err)) else None
            decision = _audio_decision(ctx.state.audio_policy, probe, False)
            if decision == "decision_required":
                ctx.apply(
                    audio=AudioState.Unavailable,
                    intent_stage=IntentStage.audio_decision,
                    last_error=(probe.error if isinstance(probe, Err) else diagnostic(ErrorCode.audio_unavailable)),
                )
                return True
            if decision == "video_only":
                ctx.apply(audio=AudioState.Disabled, last_error=None)
            else:
                ctx.apply(audio=AudioState.Starting, last_error=None)
            self._begin_stream(ctx)
            return True

        if kind == "transport_start":
            if isinstance(message.result, Err):
                self._failed_start(ctx, message.result.error)
                return True
            stream = message.result.value if isinstance(message.result, Ok) else None
            if not isinstance(stream, StreamHandle):
                self._failed_start(ctx, diagnostic(ErrorCode.transport_failed, ctx.state.receiver_id))
                return True
            ctx.apply(
                stream=stream,
                transport=TransportState.Streaming,
                intent_stage=IntentStage.active,
                accepted_resolution=None,
            )
            if not ctx.state.audio_policy.enabled:
                ctx.apply(audio=AudioState.Disabled)
                return True
            audio_id = ctx.operation().operation_id
            ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id)
            audio_context = _current_context(ctx, audio=audio_id)
            ctx.launch(
                Route.mirror,
                "audio_start",
                audio_context,
                lambda stream=stream, audio_context=audio_context: self.deps.transport.start_audio(
                    stream, audio_context,
                ),
            )
            return True

        if kind == "audio_start":
            if isinstance(message.result, Ok):
                ctx.apply(audio=AudioState.Enabled, last_error=None)
            else:
                error = message.result.error if isinstance(message.result, Err) else diagnostic(ErrorCode.audio_unavailable)
                ctx.cancel_audio()
                ctx.apply(audio=AudioState.Unavailable, last_error=error)
            return True

        if kind == "audio_retry":
            if isinstance(message.result, Ok):
                ctx.apply(audio=AudioState.Enabled, last_error=None)
            else:
                error = message.result.error if isinstance(message.result, Err) else diagnostic(ErrorCode.audio_unavailable)
                ctx.cancel_audio()
                ctx.apply(audio=AudioState.Unavailable, last_error=error)
            return True

        if kind == "mute":
            muted = getattr(ctx, "_task2_requested_mute", None)
            if isinstance(message.result, Ok):
                if isinstance(muted, bool):
                    ctx.apply(muted=muted, audio=AudioState.Muted if muted else AudioState.Enabled)
                if hasattr(ctx, "_task2_requested_mute"):
                    delattr(ctx, "_task2_requested_mute")
            else:
                ctx.apply(last_error=message.result.error if isinstance(message.result, Err) else diagnostic(ErrorCode.audio_mute_failed))
            return True
        return False

    def _failed_start(
        self,
        ctx: SessionContext,
        error: object,
        *,
        resource: PhysicalMonitorSource | None = None,
    ) -> None:
        if resource is not None:
            runtime = runtime_for(ctx)
            if resource not in runtime.late_resources:
                runtime.late_resources.append(resource)
        diagnostic_value = error if hasattr(error, "code") else diagnostic(ErrorCode.transport_failed)
        ctx.apply(
            intent_active=False,
            intent_stage=IntentStage.stopping,
            transport=TransportState.Failed,
            last_error=diagnostic_value,
        )
        # Selected portal sessions are resources even when video never became
        # ready.  Reuse the same ordered transaction rather than releasing them
        # in an ad-hoc error branch.
        self.cleanup._stop(ctx, None)

    # -- transport/reconnect ----------------------------------------------

    def _transport_event(self, ctx: SessionContext, event: TransportEvent) -> bool:
        # Some transports report a confirmed stop as a resource event rather
        # than completing the stop-stream awaitable.  Once ordered cleanup owns
        # the transaction, route that acknowledgement through the cleanup
        # state machine even if the engine admitted it on the reconnect route.
        if event.kind is TransportEventKind.stopped and runtime_for(ctx).active:
            return self.cleanup(ctx, event)
        if event.kind is TransportEventKind.pairing_required:
            return self.pairing.begin_from_backend(ctx)
        if event.kind is TransportEventKind.credential_invalid:
            return self.pairing.mark_invalid_from_backend(ctx)
        if event.kind is TransportEventKind.network_lost:
            ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.recovering)
            return True
        if event.kind is TransportEventKind.audio_start_failed or event.kind is TransportEventKind.audio_failed:
            ctx.cancel_audio()
            ctx.apply(audio=AudioState.Unavailable, last_error=diagnostic(ErrorCode.audio_unavailable))
            return True
        if event.kind is TransportEventKind.audio_recovered:
            ctx.apply(audio=AudioState.Enabled, last_error=None)
            return True
        if event.kind is TransportEventKind.failed:
            ctx.apply(transport=TransportState.Failed, last_error=diagnostic(ErrorCode.transport_failed))
            return True
        if event.kind is TransportEventKind.stopped:
            # The cleanup route normally receives exact stop acknowledgements;
            # this branch covers a provider that reports the same acknowledgement
            # as an event instead of a Result completion.
            ctx.apply(stream=None, transport=TransportState.Idle, transport_lease_id=None)
            return True
        return True

    # -- local audio -------------------------------------------------------

    def _retry_pre_ready_audio(self, ctx: SessionContext) -> None:
        context = _current_context(ctx)
        ctx.launch(
            Route.audio,
            "audio_probe",
            context,
            self.deps.audio_probe.check,
        )

    def _retry_audio(self, ctx: SessionContext) -> None:
        stream = ctx.state.stream
        if stream is None:
            return
        audio_id = ctx.operation().operation_id
        ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id, last_error=None)
        context = _current_context(ctx, audio=audio_id)
        ctx.launch(
            Route.audio,
            "audio_retry",
            context,
            lambda stream=stream, context=context: self.deps.transport.start_audio(
                stream, context,
            ),
        )

    def _set_muted(self, ctx: SessionContext, muted: bool) -> None:
        if (
            type(muted) is not bool
            or ctx.state.stream is None
            or ctx.state.audio not in (AudioState.Enabled, AudioState.Muted)
        ):
            return
        # EffectEvent is frozen and has no payload slot for a requested mute
        # value.  Keep the intended value on the actor context, not in a public
        # snapshot, until the adapter confirms it.  Store it before launching:
        # an already-completed adapter may enqueue its acknowledgement during
        # the same actor turn.
        setattr(ctx, "_task2_requested_mute", muted)
        stream = ctx.state.stream
        context = _current_context(ctx)
        ctx.launch(
            Route.audio,
            "mute",
            context,
            lambda stream=stream, muted=muted, context=context: self.deps.transport.set_muted(
                stream, muted, context,
            ),
        )


# The public mapping is intentionally a factory: each manager gets no shared
# mutable session state, and every receiver's transient state stays in its actor.
def build_lifecycle_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    lifecycle = MirrorLifecycle(deps)
    # Let cleanup wake itself when a pairing cancellation finishes.  This is an
    # actor-local callback, not a global coordinator or a background mutation.
    original_continue = lifecycle.cleanup._continue

    def continue_cleanup(ctx: SessionContext) -> None:
        original_continue(ctx)

    # OrderedCleanup installs the callback lazily when the first stop is handled;
    # setting it here is safe because the context is only read during dispatch.
    def cleanup_with_hook(ctx: SessionContext, message: object) -> bool:
        setattr(ctx, "_task2_cleanup_continue", continue_cleanup)
        return lifecycle.cleanup(ctx, message)

    return {
        Route.mirror: lifecycle.mirror,
        Route.trust: lifecycle.pairing,
        Route.cleanup: cleanup_with_hook,
        Route.reconnect: lifecycle.reconnect,
        Route.audio: lifecycle.audio,
    }


__all__ = ["build_lifecycle_handlers"]
