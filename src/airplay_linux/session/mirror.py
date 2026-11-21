"""Mirror-mode lifecycle composition.

Mirror is intentionally the narrow path: it accepts a ``PhysicalMonitorSource``,
verifies that exact source through ``CapturePort``, and never calls a display
creation API.  Pairing and ordered teardown are delegated to the sibling actor
handlers so their fences and ownership rules remain shared by every start.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Mapping

from airplay_linux.backend.contract import StatusReply
from airplay_linux.backend.control import DispatchFence

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    CredentialRequestPayload,
    CredentialStatusCompletion,
    CredentialSubmissionCompletion,
    DismissCredentialPrompt,
    DiscoveryEvent,
    EffectEvent,
    Retry,
    RetryAudio,
    SetMuted,
    Start,
    StartVideoOnly,
    SubmitCredential,
    TimerEvent,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import ChallengeId, ChallengeView, OperationContext, OperationId, StreamHandle
from airplay_linux.domain.records import (
    CredentialRecoveryMarker, NoSpawnRetirement, OriginalDaemonIdentity, TerminalReplyRetirement,
)
from airplay_linux.domain.relay import (
    CredentialEffectOwner, CredentialPromptState, CredentialSubmissionSettlement, NoReply, Submitted,
)
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

from airplay_linux.transport.doubletake import CONNECT_STREAMING_TIMEOUT, CONTROL_TIMEOUT, SOURCE_POLL_INTERVAL
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


class _CredentialCallNotStarted(TimeoutError):
    """The deadline expired before the bounded work was invoked."""


@dataclass(frozen=True)
class _PreparedCredential:
    marker: Result[tuple[CredentialRecoveryMarker, OriginalDaemonIdentity]]
    submit: Callable[[OperationContext, DispatchFence], Awaitable[Result]] = field(repr=False)
    command_deadline: float


@dataclass
class _CredentialRun:
    marker: CredentialRecoveryMarker
    context: OperationContext
    command_deadline: float
    reply: Submitted | None = None
    last_status: StatusReply | None = None
    terminal: TerminalReplyRetirement | NoSpawnRetirement | None = None
    transition_pending: bool = False
    release_pending_required: bool = False
    retirement_failed: bool = False
    retired: bool = False
    transport_generation: int = 0
    transport_submitted: bool = False


def _credential_runs(ctx: SessionContext) -> dict[OperationId, _CredentialRun]:
    if not hasattr(ctx, "_credential_runs"):
        ctx._credential_runs = {}
    return ctx._credential_runs


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
        if isinstance(message, SubmitCredential):
            self._submit_credential(ctx, message)
            return True
        if isinstance(message, DismissCredentialPrompt):
            self._dismiss_credential(ctx)
            return True
        if isinstance(message, EffectEvent):
            if message.kind.startswith("cleanup:"):
                return self.cleanup(ctx, message)
            return self._effect(ctx, message)
        return False

    def reconnect(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, DiscoveryEvent):
            return self.pairing.discovery_changed(ctx)
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
            if not ctx.state.intent_active:
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
        if request.source is not None and not isinstance(request.source, PhysicalMonitorSource):
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_source, state.receiver_id))
            return
        if isinstance(request.source, PhysicalMonitorSource) and (
            not request.source.connector or type(request.source.connector) is not str
        ):
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
        from .audio import audio_runtime
        audio_runtime(ctx).start_volume = request.volume
        remembered = request.volume.value if (self.deps.capabilities().volume and request.volume is not None
                                              and request.volume.value > 0) else None
        ctx.apply(
            remembered_volume=remembered,
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
        if source is None:
            # Desktop capture: the backend opens the ScreenCast session, so there
            # is no local acquisition to make or verify before connecting.
            self._after_capture(ctx)
            return
        setattr(ctx, "_task2_requested_source", source)
        ctx.launch(
            Route.mirror,
            "capture_select",
            context,
            lambda source=source: self.deps.capture.select(source.connector),
            acquisition=True,
        )

    def _after_capture(self, ctx: SessionContext) -> None:
        """Continue once the capture source is acquired or delegated."""
        if not ctx.state.audio_policy.enabled:
            ctx.apply(audio=AudioState.Disabled, last_error=None)
            self._begin_stream(ctx)
            return
        ctx.launch(
            Route.mirror,
            "audio_probe",
            _current_context(ctx),
            self.deps.audio_probe.check,
        )

    def _start_after_trust(self, ctx: SessionContext) -> bool:
        if not ctx.state.intent_active or ctx.state.stream is not None:
            return False
        self._begin_stream(ctx)
        return True

    def _begin_stream(self, ctx: SessionContext) -> None:
        target = ctx.state.target
        # ``None`` is a desktop capture: the transport asks the backend, which
        # owns the ScreenCast session, for whatever the portal grants.
        source = ctx.state.physical_source
        if target is None:
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
        from .audio import audio_runtime
        initial = audio_runtime(ctx).start_volume
        if not self.deps.capabilities().volume or not policy.enabled:
            initial = None
        ctx.launch(
            Route.mirror,
            "transport_start",
            context,
            lambda target=target, source=source, policy=policy, context=context, initial=initial: self.deps.transport.start_stream(
                target, source, policy, context, initial_volume=initial,
            ),
            acquisition=True,
        )

    # -- effect delivery ---------------------------------------------------

    def _effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        kind = message.kind.rsplit(":", 1)[-1]
        if kind.startswith("credential_"):
            return self._credential_effect(ctx, message, kind)
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
            self._after_capture(ctx)
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
            if ctx.state.physical_source is None and stream.source_connector is not None:
                # Desktop capture has no local lease: record the connector the
                # backend actually captured so the pane can report it.
                ctx.apply(physical_source=PhysicalMonitorSource(
                    connector=stream.source_connector, portal_restore_token=None,
                ))
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
        if event.kind is TransportEventKind.credential_request:
            return self._credential_request(ctx, event)
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

    # -- credential relay -------------------------------------------------

    def _credential_request(self, ctx, event):
        payload = event.payload
        if not isinstance(payload, CredentialRequestPayload):
            return True
        request = payload.request
        previous = ctx.credential_request
        if payload.lifecycle == "ended":
            if previous == request:
                ctx.credential_request_ended = True
                ctx.dismissed_credential_prompt_id = None
                prompt = ctx.state.credential_prompt
                if prompt is not None and prompt.request == request:
                    if prompt.submission_id in ctx.credential_settlements:
                        settlement = ctx.credential_settlements[prompt.submission_id]
                        ctx.credential_settlements[prompt.submission_id] = replace(settlement, report_suppressed=True)
                    ctx.apply(credential_prompt=None)
                self.deps.transport.dismiss_credential_prompt(request.target, event.context)
            return True
        if previous == request:
            return True
        for submission_id, settlement in tuple(ctx.credential_settlements.items()):
            if settlement.owner.start_operation_id == request.start_operation_id:
                ctx.credential_settlements[submission_id] = replace(settlement, report_suppressed=True)
        ctx.credential_request = request
        ctx.credential_request_ended = False
        ctx.dismissed_credential_prompt_id = None
        challenge = ChallengeView(
            challenge_id=ChallengeId(self.deps.ids.new()), receiver_id=request.receiver_id,
            endpoint_fingerprint=request.target.endpoint_fingerprint,
            endpoint_generation=request.endpoint_generation, operation_id=request.start_operation_id,
            session_generation=request.session_generation, trust_generation=ctx.state.trust_generation,
            created_at=request.observed_at, expires_at=float("inf"), credential_kind=request.credential_kind,
        )
        ctx._credential_cleanup_settled = self._cleanup_credentials
        ctx._credential_retry_transitions = self._retry_credential_transitions
        ctx.apply(credential_prompt=CredentialPromptState(
            request=request, challenge=challenge, state="editing", submission_id=None, report=None,
        ))
        return True

    def _dismiss_credential(self, ctx):
        prompt = ctx.state.credential_prompt
        if prompt is None:
            return
        ctx.dismissed_credential_prompt_id = prompt.challenge.challenge_id
        if prompt.submission_id in ctx.credential_settlements:
            settlement = ctx.credential_settlements[prompt.submission_id]
            ctx.credential_settlements[prompt.submission_id] = replace(settlement, report_suppressed=True)
        request = prompt.request
        context = OperationContext(
            operation_id=request.start_operation_id, session_generation=request.session_generation,
            endpoint_generation=request.endpoint_generation, transport_lease_id=request.owner_lease,
            trust_generation=prompt.challenge.trust_generation,
        )
        self.deps.transport.dismiss_credential_prompt(request.target, context)
        ctx.apply(credential_prompt=None)

    def _submit_credential(self, ctx, message):
        prompt = ctx.state.credential_prompt
        coordinator = self.deps.credential_recovery
        if prompt is None or coordinator is None:
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_operation, ctx.state.receiver_id))
            return
        request, challenge = prompt.request, prompt.challenge
        operation = ctx.operation()
        owner = CredentialEffectOwner(
            challenge_id=challenge.challenge_id, receiver_id=request.receiver_id, target=request.target,
            start_operation_id=request.start_operation_id,
            endpoint_fingerprint=request.target.endpoint_fingerprint,
            endpoint_generation=request.endpoint_generation, session_generation=request.session_generation,
            trust_generation=challenge.trust_generation, owner_lease=request.owner_lease,
            daemon_instance_uuid=request.daemon_instance_uuid, relay_generation=request.generation,
            marker_generation=0,
        )
        command_deadline = self.deps.clock.now() + CONTROL_TIMEOUT
        ctx.apply(credential_prompt=replace(prompt, state="submitting", submission_id=operation.operation_id))
        self.deps.transport.dismiss_credential_prompt(
            request.target, replace(operation, operation_id=request.start_operation_id),
        )
        value = message.value

        async def prepare():
            try:
                marker = await self._credential_call_before(
                    command_deadline,
                    lambda: coordinator.begin_submission_for(
                        request.receiver_id, request.target, request.start_operation_id,
                    ),
                )
            except TimeoutError:
                marker = Err(error=diagnostic(ErrorCode.cleanup_timeout, owner.receiver_id))

            async def submit(context, dispatch_fence):
                return await self.deps.transport.submit_credential(
                    request.target, value, context, dispatch_fence=dispatch_fence,
                )

            return Ok(value=_PreparedCredential(marker=marker, submit=submit, command_deadline=command_deadline))

        ctx.launch(Route.mirror, "credential_prepare", operation, prepare, credential_owner=owner)

    async def _credential_call_before(self, deadline, work):
        """Use the operator-submit deadline through each awaited command stage."""
        remaining = deadline - self.deps.clock.now()
        if remaining <= 0:
            raise _CredentialCallNotStarted
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        expired = False

        def expire():
            nonlocal expired
            if not expired:
                expired = True
                task.cancel()

        timer = self.deps.scheduler.call_at(deadline, expire)
        realtime = loop.call_later(remaining, expire)
        try:
            result = await work()
        except asyncio.CancelledError:
            if expired:
                raise TimeoutError from None
            raise
        finally:
            timer.cancel()
            realtime.cancel()
            if expired:
                task.uncancel()
        if isinstance(result, Ok):
            outcome = result.value
            if (
                (expired and isinstance(outcome, NoReply) and outcome.reason == "dispatch_cancelled")
                or (isinstance(outcome, Submitted) and outcome.reply_at > deadline)
            ):
                return Ok(value=NoReply(reason="command_timeout"))
        return result

    def _credential_effect(self, ctx, event, kind):
        effect = ctx._manager._completed_effects.get((event.receiver_id, event.operation_id, event.kind))
        if effect is None or effect.credential_owner is None:
            return True
        owner = effect.credential_owner
        runs = _credential_runs(ctx)
        if kind == "credential_prepare":
            prepared = event.result.value if isinstance(event.result, Ok) else None
            if not isinstance(prepared, _PreparedCredential) or isinstance(prepared.marker, Err):
                error = prepared.marker.error if isinstance(prepared, _PreparedCredential) else diagnostic(ErrorCode.persistence_failed)
                prompt = ctx.state.credential_prompt
                if prompt is not None and prompt.challenge.challenge_id == owner.challenge_id:
                    ctx.apply(last_error=error, credential_prompt=None)
                elif effect.credential_stop_operation_id is not None:
                    ctx.apply(last_error=error)
                self.cleanup._enqueue_recheck(ctx)
                return True
            marker, identity = prepared.marker.value
            owner = replace(owner, marker_generation=marker.marker_generation)
            context = replace(effect.context, marker_generation=marker.marker_generation, owner_daemon=identity)
            dispatch_current = ctx._manager._credential_owner_current(ctx.state, owner, None, for_dispatch=True)
            suppressed = (
                not dispatch_current
                or effect.credential_stop_operation_id is not None
                or ctx.dismissed_credential_prompt_id == owner.challenge_id
            )
            settlement = CredentialSubmissionSettlement(
                submission_id=event.operation_id, read_id=None, owner=owner,
                marker_generation=owner.marker_generation,
                stop_operation_id=effect.credential_stop_operation_id,
                report_suppressed=suppressed, phase="awaiting_reply",
            )
            ctx.credential_settlements[event.operation_id] = settlement
            run = _CredentialRun(
                marker=marker, context=context,
                command_deadline=prepared.command_deadline,
                transport_generation=marker.marker_generation,
            )
            runs[event.operation_id] = run
            expired = self.deps.clock.now() >= run.command_deadline
            if expired or not dispatch_current or identity.instance_uuid != owner.daemon_instance_uuid:
                run.terminal = NoSpawnRetirement(
                    failure=ErrorCode.cleanup_timeout if expired else ErrorCode.stale_event,
                    stop_cleanup_settled=False,
                    observed_at=self.deps.clock.now(),
                )
                ctx.credential_settlements[event.operation_id] = replace(settlement, phase="cleanup")
                self._credential_error(ctx, settlement, run.terminal.failure)
                self._credential_finished(ctx, event.operation_id)
                return True
            marker_fence = self.deps.credential_recovery.credential_dispatch_fence(marker, marker.marker_generation)

            def dispatch_fence():
                if self.deps.clock.now() >= run.command_deadline:
                    return Err(error=diagnostic(ErrorCode.cleanup_timeout, owner.receiver_id))
                if not ctx._manager._credential_owner_current(ctx.state, owner, None, for_dispatch=True):
                    return Err(error=diagnostic(ErrorCode.stale_event, owner.receiver_id))
                return marker_fence()

            async def invoke_transport():
                # On admission the transport records its submission identity
                # before yielding. A proven no-spawn result clears that identity
                # before it returns, so keep the handoff evidence in step.
                run.transport_submitted = True
                result = await prepared.submit(context, dispatch_fence)
                if isinstance(result, Err):
                    run.transport_submitted = False
                return result

            async def submit():
                try:
                    result = dispatch_fence()
                    if isinstance(result, Ok):
                        result = await self._credential_call_before(
                            run.command_deadline, invoke_transport,
                        )
                    if isinstance(result, Err):
                        outcome = Err(error=diagnostic(result.error.code, owner.receiver_id))
                    elif isinstance(result, Ok) and isinstance(result.value, (Submitted, NoReply)):
                        outcome = result.value
                    else:
                        outcome = NoReply(reason="incomplete_reply")
                except _CredentialCallNotStarted:
                    outcome = Err(error=diagnostic(ErrorCode.cleanup_timeout, owner.receiver_id))
                except TimeoutError:
                    outcome = NoReply(reason="command_timeout")
                except asyncio.CancelledError:
                    outcome = NoReply(reason="dispatch_cancelled")
                except Exception:
                    outcome = NoReply(reason="incomplete_reply")
                return Ok(value=CredentialSubmissionCompletion(
                    submission_id=event.operation_id, **vars(owner), outcome=outcome,
                ))

            ctx.launch(Route.mirror, "credential_submit", context, submit, credential_owner=owner)
            return True

        settlement = next((item for item in ctx.credential_settlements.values() if (
            item.owner.challenge_id == owner.challenge_id
            and item.marker_generation == owner.marker_generation
        )), None)
        if settlement is None:
            return True
        submission_id = settlement.submission_id
        run = runs[submission_id]
        if kind == "credential_submit":
            outcome = event.result.value.outcome
            if isinstance(outcome, Submitted):
                run.reply = outcome
                if outcome.post_dispatch_error is not None:
                    ctx.credential_settlements[submission_id] = replace(settlement, phase="cleanup")
                    self._credential_error(ctx, settlement, outcome.post_dispatch_error)
                    self._credential_finished(ctx, submission_id)
                else:
                    ctx.credential_settlements[submission_id] = replace(settlement, phase="observing")
                    self._credential_report(ctx, settlement, state="observing")
                    self._read_credential_status(ctx, submission_id)
            elif isinstance(outcome, NoReply):
                ctx.credential_settlements[submission_id] = replace(settlement, phase="cleanup")
                run.release_pending_required = True
                self._mark_credential_release_pending(ctx, submission_id)
            else:
                run.terminal = NoSpawnRetirement(
                    failure=outcome.error.code, stop_cleanup_settled=False,
                    observed_at=self.deps.clock.now(),
                )
                ctx.credential_settlements[submission_id] = replace(settlement, phase="cleanup")
                self._credential_error(ctx, settlement, outcome.error.code)
                self._credential_finished(ctx, submission_id)
            return True
        if kind == "credential_status":
            result = event.result.value.result
            ctx.credential_settlements[submission_id] = replace(settlement, read_id=None)
            deadline = run.reply.reply_at + CONNECT_STREAMING_TIMEOUT
            if isinstance(result, Err):
                if result.error.code is ErrorCode.cleanup_timeout and self.deps.clock.now() >= deadline and run.last_status is not None:
                    self._credential_terminal(ctx, submission_id, "not_streaming_at_deadline")
                else:
                    ctx.credential_settlements[submission_id] = replace(settlement, read_id=None, phase="cleanup")
                    self._credential_error(ctx, settlement, result.error.code)
                    self._credential_finished(ctx, submission_id)
                return True
            status = result.value
            if type(status) is not StatusReply:
                ctx.credential_settlements[submission_id] = replace(settlement, read_id=None, phase="cleanup")
                self._credential_error(ctx, settlement, ErrorCode.transport_failed)
                self._credential_finished(ctx, submission_id)
                return True
            rows = tuple(row for row in status.streams if row.target == owner.target.canonical_host)
            if len(rows) > 1:
                ctx.credential_settlements[submission_id] = replace(settlement, read_id=None, phase="cleanup")
                self._credential_error(ctx, settlement, ErrorCode.transport_failed)
                self._credential_finished(ctx, submission_id)
            elif self.deps.clock.now() > deadline:
                self._credential_terminal(ctx, submission_id, "not_streaming_at_deadline")
            elif not rows:
                self._credential_terminal(ctx, submission_id, "absent")
            elif rows[0].state == "streaming":
                self._credential_terminal(ctx, submission_id, "streaming")
            elif self.deps.clock.now() >= deadline:
                self._credential_terminal(ctx, submission_id, "not_streaming_at_deadline")
            else:
                run.last_status = status
                self._read_credential_status(ctx, submission_id, delay=SOURCE_POLL_INTERVAL)
            return True
        if kind in {"credential_transition", "credential_retire", "credential_generation"}:
            run.transition_pending = False
            if isinstance(event.result, Err):
                if kind != "credential_transition":
                    run.retirement_failed = True
                ctx.apply(last_error=event.result.error)
            else:
                if kind == "credential_transition":
                    run.release_pending_required = False
                if kind != "credential_generation":
                    marker = event.result.value
                    run.marker = marker
                    run.context = replace(run.context, marker_generation=marker.marker_generation)
                    settlement = replace(settlement, marker_generation=marker.marker_generation)
                    ctx.credential_settlements[submission_id] = settlement
                    run.retired = marker.state == "retired"
                    if settlement.stop_operation_id is None and not isinstance(run.terminal, NoSpawnRetirement):
                        run.transition_pending = True
                        old_context = replace(run.context, marker_generation=run.transport_generation)

                        async def advance_generation():
                            # Stop may capture the earlier transport generation
                            # before this effect starts. Its handoff owns it then.
                            pending_owner = runtime_for(ctx).pending_start_owner
                            if (
                                pending_owner is not None and pending_owner.marker_generation is not None
                                and pending_owner.marker_generation != settlement.marker_generation
                            ):
                                return Ok(value=None)
                            return await self.deps.transport.record_credential_marker_generation(
                                owner.target, old_context, submission_id=submission_id,
                                previous_generation=run.transport_generation,
                                expected_generation=settlement.marker_generation,
                            )

                        ctx.launch(
                            Route.mirror, "credential_generation", run.context, advance_generation,
                            credential_owner=replace(settlement.owner, marker_generation=settlement.marker_generation),
                        )
                        return True
                run.transport_generation = settlement.marker_generation
                if run.marker.state == "release_pending":
                    self._credential_report(ctx, settlement, report="outcome_unknown")
                elif isinstance(run.terminal, TerminalReplyRetirement):
                    self._credential_report(
                        ctx, settlement, report="connected" if run.terminal.target_observation == "streaming" else "not_observed_streaming",
                    )
            self.cleanup._enqueue_recheck(ctx)
            return True
        return True

    def _mark_credential_release_pending(self, ctx, submission_id):
        settlement = ctx.credential_settlements[submission_id]
        run = _credential_runs(ctx)[submission_id]
        if not run.release_pending_required or run.transition_pending:
            return
        run.transition_pending = True
        ctx.launch(
            Route.mirror, "credential_transition", run.context,
            lambda: self.deps.credential_recovery.mark_release_pending_for(run.marker, settlement.marker_generation),
            credential_owner=replace(settlement.owner, marker_generation=settlement.marker_generation),
        )

    def _retry_credential_transitions(self, ctx):
        runtime = runtime_for(ctx)
        for submission_id, settlement in tuple(ctx.credential_settlements.items()):
            if runtime.context is not None and settlement.stop_operation_id == runtime.context.operation_id:
                self._mark_credential_release_pending(ctx, submission_id)

    def _credential_report(self, ctx, settlement, *, state=None, report=None):
        prompt = ctx.state.credential_prompt
        if settlement.report_suppressed or prompt is None or prompt.challenge.challenge_id != settlement.owner.challenge_id:
            return
        changes = {}
        if state is not None:
            changes["state"] = state
        if report is not None:
            changes["report"] = report
        ctx.apply(credential_prompt=replace(prompt, **changes))

    def _credential_error(self, ctx, settlement, code):
        prompt = ctx.state.credential_prompt
        if (
            not settlement.report_suppressed and prompt is not None
            and prompt.challenge.challenge_id == settlement.owner.challenge_id
        ):
            ctx.apply(last_error=diagnostic(code, ctx.state.receiver_id), credential_prompt=None)

    def _credential_terminal(self, ctx, submission_id, observation):
        settlement = ctx.credential_settlements[submission_id]
        run = _credential_runs(ctx)[submission_id]
        run.terminal = TerminalReplyRetirement(
            reply_at=run.reply.reply_at, accepted=run.reply.accepted,
            target_observation=observation, observation_at=self.deps.clock.now(),
            stop_cleanup_settled=False,
        )
        ctx.credential_settlements[submission_id] = replace(settlement, phase="cleanup", read_id=None)
        self._credential_finished(ctx, submission_id)

    def _credential_finished(self, ctx, submission_id):
        settlement = ctx.credential_settlements[submission_id]
        if settlement.stop_operation_id is None:
            self._retire_credential(ctx, submission_id)
        self.cleanup._enqueue_recheck(ctx)

    def _retire_credential(self, ctx, submission_id):
        settlement = ctx.credential_settlements[submission_id]
        run = _credential_runs(ctx)[submission_id]
        if run.terminal is None or run.transition_pending or run.retired or run.retirement_failed:
            return
        evidence = replace(run.terminal, stop_cleanup_settled=True)
        run.transition_pending = True
        method = (self.deps.credential_recovery.retire_no_spawn_for if isinstance(evidence, NoSpawnRetirement)
                  else self.deps.credential_recovery.retire_after_reply_for)
        ctx.launch(
            Route.mirror, "credential_retire", run.context,
            lambda: method(run.marker, settlement.marker_generation, evidence),
            credential_owner=replace(settlement.owner, marker_generation=settlement.marker_generation),
        )

    def _cleanup_credentials(self, ctx):
        runtime = runtime_for(ctx)
        waiting = False
        for submission_id, settlement in tuple(ctx.credential_settlements.items()):
            if runtime.context is None or settlement.stop_operation_id != runtime.context.operation_id:
                continue
            run = _credential_runs(ctx)[submission_id]
            if settlement.phase != "cleanup" or run.transition_pending or run.release_pending_required:
                waiting = True
                continue
            self._retire_credential(ctx, submission_id)
            waiting = waiting or run.transition_pending
        return not waiting

    def _read_credential_status(self, ctx, submission_id, *, delay=0.0):
        settlement = ctx.credential_settlements[submission_id]
        run = _credential_runs(ctx)[submission_id]
        read_id = ctx.operation().operation_id
        owner = replace(settlement.owner, marker_generation=settlement.marker_generation)
        context = replace(run.context, operation_id=read_id)
        ctx.credential_settlements[submission_id] = replace(settlement, read_id=read_id)
        deadline = run.reply.reply_at + CONNECT_STREAMING_TIMEOUT

        async def read():
            loop = asyncio.get_running_loop()
            expired = loop.create_future()
            remaining = max(0.0, deadline - self.deps.clock.now())

            def expire():
                if not expired.done():
                    expired.set_result(None)

            timer = self.deps.scheduler.call_at(deadline, expire)
            realtime = loop.call_later(remaining, expire)
            delayed = None
            task = None
            try:
                if delay:
                    ready = loop.create_future()
                    delayed = self.deps.scheduler.call_at(
                        min(deadline, self.deps.clock.now() + delay),
                        lambda: ready.set_result(None) if not ready.done() else None,
                    )
                    await asyncio.wait((ready, expired), return_when=asyncio.FIRST_COMPLETED)
                if expired.done() or self.deps.clock.now() >= deadline:
                    result = Err(error=diagnostic(ErrorCode.cleanup_timeout, owner.receiver_id))
                else:
                    task = asyncio.create_task(self.deps.transport.read_credential_status(owner.target, context))
                    done, _ = await asyncio.wait((task, expired), return_when=asyncio.FIRST_COMPLETED)
                    if task in done and self.deps.clock.now() <= deadline:
                        result = task.result()
                        if isinstance(result, Err):
                            result = Err(error=diagnostic(result.error.code, owner.receiver_id))
                    else:
                        result = Err(error=diagnostic(ErrorCode.cleanup_timeout, owner.receiver_id))
            except Exception:
                result = Err(error=diagnostic(ErrorCode.transport_failed, owner.receiver_id))
            finally:
                timer.cancel()
                realtime.cancel()
                if delayed is not None:
                    delayed.cancel()
                if task is not None and not task.done():
                    task.cancel()
            return Ok(value=CredentialStatusCompletion(read_id=read_id, **vars(owner), result=result))

        ctx.launch(Route.mirror, "credential_status", context, read, credential_owner=owner)

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
