"""Virtual outputs as actor-owned transactions over the frozen display port.

A successful DisplayPort.create is the provider's association-verification
boundary. Progress is ownership evidence, never permission to stream. The
provider must refuse publication when assembly supplies no association verifier.
Pairing and destructive teardown reuse the Mirror lifecycle's shared owners.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverRecordSet
from airplay_linux.domain.messages import (
    Cancel, ChangeResolution, ChooseResolution, DisplayEvent, DiscoveryEvent,
    EffectEvent, ModePayload, Pair, RecreateAccepted, Retry, RetryAudio, RetryCleanup,
    Start, Stop, TimerEvent, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import (
    DisplayHandle, OperationContext, OperationId, OwnerId, ProcessIdentity, StreamHandle,
)
from airplay_linux.domain.ports import Subscription
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import (
    AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok,
    ReceiverCapabilities, ResolutionDecision, ResolutionEvidence, Size,
    StartRequest, TransportState, TrustState, VirtualOutputSource, VolumeLevel,
)
from airplay_linux.storage import PreferenceAccess, is_ambiguous_identity

from .cleanup import OrderedCleanup, runtime_for
from .engine import Handler, ManagerDeps, Route, SessionContext
from .fencing import accept_transport
from .mirror import MirrorLifecycle, _current_context, _start_gate


@dataclass(frozen=True)
class _ResolutionRequired:
    choices: tuple[Size, ...] = ()


def _resolve(record_set, saved, queried, user=None):
    """Use the policy lane; retain conservative behavior in a standalone lane."""
    try:
        from airplay_linux.policy.resolution import resolve_resolution
    except ModuleNotFoundError as error:
        if error.name not in ("airplay_linux.policy", "airplay_linux.policy.resolution"):
            raise
    else:
        return resolve_resolution(record_set, saved, None, queried, user)

    evidence = ResolutionEvidence(
        origin="picker" if user is not None else "query", reliable=True,
        receiver_id=record_set.receiver_id,
        endpoint_fingerprint=record_set.endpoint_fingerprint,
    )
    if user is not None:
        return ResolutionDecision(size=user, source="user", evidence=evidence)
    authorized = PreferenceAccess.for_record_set(record_set, saved) if saved is not None else None
    remembered = authorized.remembered_resolution if authorized is not None else None
    if remembered is not None and (
        not remembered.evidence.reliable
        or remembered.evidence.receiver_id != record_set.receiver_id
    ):
        remembered = None
    modes = tuple(dict.fromkeys(queried.modes)) if queried is not None and queried.reliable else ()
    if remembered is not None and (not modes or remembered.size in modes):
        return replace(remembered, source="stored", evidence=replace(evidence, origin="remembered"))
    if remembered is None and len(modes) == 1:
        return ResolutionDecision(size=modes[0], source="discovery", evidence=evidence)
    return _ResolutionRequired(modes)


def _rememberable(record_set: ReceiverRecordSet, choice: ResolutionDecision, observed: Size):
    try:
        from airplay_linux.policy.resolution import remember_after_success
    except ModuleNotFoundError as error:
        if error.name not in ("airplay_linux.policy", "airplay_linux.policy.resolution"):
            raise
    else:
        return remember_after_success(record_set, choice, observed)
    if (
        observed != choice.size or not choice.evidence.reliable
        or choice.evidence.receiver_id != record_set.receiver_id
        or is_ambiguous_identity(record_set.receiver_id) or record_set.ambiguous
        or record_set.identity_kind == "ambiguous" or not record_set.persistable
        or record_set.replacement_of is not None
        or (record_set.identity_kind != "device" and not record_set.continuity_verified)
    ):
        return None
    return replace(choice, evidence=replace(
        choice.evidence, origin="verified", endpoint_fingerprint=record_set.endpoint_fingerprint,
    ))


def _mismatch(choice, accepted, reliable):
    try:
        from airplay_linux.policy.resolution import accepted_mode_mismatch
    except ModuleNotFoundError as error:
        if error.name not in ("airplay_linux.policy", "airplay_linux.policy.resolution"):
            raise
    else:
        return accepted_mode_mismatch(choice, accepted, reliable)
    return reliable and choice.size != accepted


async def _confirmed():
    return Ok(value=None)


@dataclass
class _VirtualRuntime:
    request: StartRequest | None = None
    saved: SavedReceiver | None = None
    record_set: ReceiverRecordSet | None = None
    queried: ReceiverCapabilities | None = None
    query_complete: bool = False
    pairing_requested: bool = False
    resume_after_start: bool = False
    creating: bool = False
    creation_context: OperationContext | None = None
    owner: OwnerId | None = None
    helper: ProcessIdentity | None = None
    connector: str | None = None
    output_id: str | None = None
    stage: int = -1
    recreating: bool = False
    pause_inflight: bool = False
    pause_reason: str | None = None
    pending_ready: TransportEvent | None = None
    ready_stream: StreamHandle | None = None
    start_bridges: dict[OperationId, Subscription] = field(default_factory=dict)


def _virtual_runtime(ctx: SessionContext) -> _VirtualRuntime:
    runtime = getattr(ctx, "_virtual_runtime", None)
    if runtime is None:
        runtime = _VirtualRuntime()
        setattr(ctx, "_virtual_runtime", runtime)
    return runtime


class _VirtualCleanup(OrderedCleanup):
    def _launch_transport_stop(self, ctx):
        # A retained-output pause already owns this exact stop. Adopt its
        # acknowledgement instead of launching a second concurrent stop call.
        if _virtual_runtime(ctx).pause_inflight:
            runtime = runtime_for(ctx)
            runtime.transport_inflight = True
            runtime.stopped_stream = ctx.state.stream
            return
        super()._launch_transport_stop(ctx)


class VirtualLifecycle(MirrorLifecycle):
    """Virtual-specific continuations of the shared pairing/cleanup lifecycle."""

    def __init__(self, deps: ManagerDeps):
        super().__init__(deps)
        self.cleanup = _VirtualCleanup(
            deps, start_callback=self.start_request,
            cancel_pairing_callback=self.pairing.cancel_from_cleanup,
        )

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, Start):
            self._start(ctx, message)
            return True
        if isinstance(message, DisplayEvent):
            return self._display_event(ctx, message)
        if isinstance(message, EffectEvent):
            return self._effect(ctx, message)
        return False

    def start_request(self, ctx, request):
        self._start(ctx, Start(receiver_id=ctx.state.receiver_id, request=request), supersede=False)
        return True

    def cleanup_route(self, ctx, message):
        setattr(ctx, "_task2_cleanup_continue", self.cleanup._continue)
        runtime = _virtual_runtime(ctx)
        if isinstance(message, (Cancel, Stop)):
            runtime.recreating = False
            runtime.pause_reason = None
        if isinstance(message, EffectEvent) and message.kind == "cleanup:transport_start":
            self._close_start_bridge(ctx, message.operation_id)
        if isinstance(message, EffectEvent) and message.kind == "cleanup:virtual_stale_stop":
            return self._stale_stream_stopped(ctx, message)
        if (
            ctx.state.mode is Mode.VIRTUAL and ctx.state.intent_active
            and ctx.state.display is DisplayState.Owned and ctx.state.display_handle is not None
            and not runtime_for(ctx).active
        ):
            if isinstance(message, EffectEvent) and message.kind == "cleanup:transport_start":
                return self._stale_transport(ctx, message)
            if isinstance(message, RetryCleanup) and runtime_for(ctx).late_resources:
                for resource in tuple(runtime_for(ctx).late_resources):
                    if isinstance(resource, StreamHandle):
                        self._release_stale_stream(ctx, resource)
                return True
        if isinstance(message, EffectEvent) and message.kind == "cleanup:virtual_retained_stop":
            return self._paused(ctx, message)
        if isinstance(message, EffectEvent) and message.kind == "cleanup:virtual_display_confirm":
            if runtime_for(ctx).active or not ctx.state.intent_active:
                return True
            return self._effect(ctx, replace(message, kind="virtual:display_confirm"))
        handled = self.cleanup(ctx, message)
        # A healthy recreation is an active transaction, not cleanup uncertainty.
        # OrderedCleanup retains all its stage flags and exact resource ledger;
        # only a failed stage sets cleanup_pending and wins the UI projection.
        if runtime.recreating and runtime_for(ctx).active and ctx.state.intent_stage is IntentStage.stopping:
            ctx.apply(intent_stage=IntentStage.reconfiguring, cleanup_pending=False)
        return handled

    def _stale_transport(self, ctx, message):
        # A retired connect owns only its exact stream, never the healthy output
        # of the active Virtual intent. Err carries no acquisition to release.
        runtime = _virtual_runtime(ctx)
        if ctx.state.stream is None and message.operation_id == ctx.state.operation_id:
            runtime.pending_ready = None
            ctx.cancel_audio()
            ctx.apply(transport=TransportState.Reconnecting, transport_lease_id=None,
                      intent_stage=IntentStage.active, audio=AudioState.Disabled)
        resource = message.resource
        if isinstance(message.result, Ok) and isinstance(resource, StreamHandle):
            cleanup = runtime_for(ctx)
            if resource not in cleanup.late_resources:
                cleanup.late_resources.append(resource)
            self._release_stale_stream(ctx, resource)
            # Retain the acquisition in the manager ledger until exact release.
            return False
        return True

    def _release_stale_stream(self, ctx, stream):
        runtime = runtime_for(ctx)
        if stream in runtime.late_inflight:
            return
        runtime.late_inflight.add(stream)
        operation = _current_context(ctx)
        ctx.launch(Route.cleanup, "virtual_stale_stop", operation,
                   lambda: self.deps.transport.stop_stream(stream, operation), resource=stream)

    def _stale_stream_stopped(self, ctx, message):
        runtime = runtime_for(ctx)
        runtime.late_inflight.discard(message.resource)
        if isinstance(message.result, Err):
            ctx.apply(cleanup_pending=True, intent_stage=IntentStage.cleanup, last_error=message.result.error)
            return True
        if message.resource in runtime.late_resources:
            runtime.late_resources.remove(message.resource)
        if runtime.active:
            # Stop may have joined this release. Its next actor turn sees the
            # manager's pending-ledger removal and can advance ordered teardown.
            self.cleanup._enqueue_recheck(ctx)
        elif not runtime.late_resources and ctx.state.cleanup_pending:
            ctx.apply(cleanup_pending=False, intent_stage=IntentStage.active, last_error=None)
        return True

    def trust_route(self, ctx, message):
        if (
            ctx.state.mode is Mode.VIRTUAL and isinstance(message, Pair)
            and ctx.state.intent_active and _virtual_runtime(ctx).pairing_requested
        ):
            return self.pairing.begin_from_backend(ctx)
        return self.pairing(ctx, message)

    def reconnect(self, ctx, message):
        if ctx.state.mode is not Mode.VIRTUAL:
            return super().reconnect(ctx, message)
        if isinstance(message, DiscoveryEvent):
            old_challenge = ctx.state.challenge
            self.pairing.discovery_changed(ctx)
            if old_challenge is not None and ctx.state.challenge is None:
                # Authority retirement never releases the independent display.
                _virtual_runtime(ctx).pairing_requested = True
            if ctx.state.intent_active and not ctx.state.cleanup_pending:
                if not ctx.state.online:
                    ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.active)
                elif ctx.state.stream is not None and ctx.state.target is not None and (
                    ctx.state.stream.endpoint_fingerprint != ctx.state.target.endpoint_fingerprint
                    or ctx.state.stream.endpoint_generation != ctx.state.target.endpoint_generation
                ):
                    ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.active)
            return True
        if isinstance(message, TimerEvent) and message.kind == "pairing_expiry":
            expired = self.pairing.expire(ctx)
            if expired:
                self.cleanup_route(ctx, Cancel(receiver_id=ctx.state.receiver_id))
            return expired
        if isinstance(message, Retry):
            if (
                not ctx.state.intent_active or ctx.state.cleanup_pending
                or runtime_for(ctx).active or not ctx.state.online
                or ctx.state.trust in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)
                or ctx.state.accepted_resolution is not None
            ):
                return False
            self._new_operation(ctx)
            if ctx.state.display is DisplayState.Owned:
                if ctx.state.stream is not None:
                    self._pause(ctx, "retry")
                else:
                    self._prepare_audio(ctx)
                return True
            if ctx.state.display_handle is None and not _virtual_runtime(ctx).creating:
                self._load_resolution(ctx)
                return True
            return False
        return super().reconnect(ctx, message)

    def audio(self, ctx, message):
        if (
            ctx.state.mode is Mode.VIRTUAL and isinstance(message, RetryAudio)
            and ctx.state.intent_stage is IntentStage.audio_decision
            and ctx.state.intent_active and ctx.state.display is DisplayState.Owned
            and ctx.state.stream is None
        ):
            self._retry_pre_ready_audio(ctx)
            return True
        return super().audio(ctx, message)

    def resolution(self, ctx, message):
        if ctx.state.mode is not Mode.VIRTUAL:
            return False
        if isinstance(message, ChooseResolution):
            if (
                not ctx.state.intent_active or ctx.state.intent_stage is not IntentStage.resolution
                or ctx.state.ownership_uuid is not None or ctx.state.display_handle is not None
                or ctx.state.cleanup_pending or runtime_for(ctx).active
            ):
                return False
            if not self._valid_decision(ctx, message.decision):
                ctx.apply(last_error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id))
                return True
            self._new_operation(ctx)
            ctx.apply(resolution=message.decision, last_error=None)
            self._create(ctx)
            return True
        if isinstance(message, (ChangeResolution, RecreateAccepted)):
            if (
                not ctx.state.intent_active or ctx.state.display is not DisplayState.Owned
                or ctx.state.display_handle is None or ctx.state.cleanup_pending
                or runtime_for(ctx).active
            ):
                return False
            choice = message.decision if isinstance(message, ChangeResolution) else None
            if isinstance(message, RecreateAccepted):
                if ctx.state.accepted_resolution is None or ctx.state.target is None:
                    return False
                choice = ResolutionDecision(
                    size=ctx.state.accepted_resolution, source="user",
                    evidence=ResolutionEvidence(
                        origin="picker", reliable=True, receiver_id=ctx.state.receiver_id,
                        endpoint_fingerprint=ctx.state.target.endpoint_fingerprint,
                    ),
                )
            if not self._valid_decision(ctx, choice):
                ctx.apply(last_error=diagnostic(ErrorCode.stale_target, ctx.state.receiver_id))
                return True
            runtime = _virtual_runtime(ctx)
            runtime.recreating = True
            request = StartRequest(mode=Mode.VIRTUAL, resolution=choice, audio=ctx.state.audio_policy,
                                   volume=VolumeLevel(value=ctx.state.remembered_volume)
                                   if ctx.state.remembered_volume is not None else None)
            self.cleanup._stop(ctx, request)
            setattr(ctx, "_task2_cleanup_continue", self.cleanup._continue)
            ctx.apply(intent_stage=IntentStage.reconfiguring, cleanup_pending=False)
            return True
        return False

    def _valid_decision(self, ctx, choice):
        return (
            isinstance(choice, ResolutionDecision) and isinstance(choice.size, Size)
            and choice.evidence.reliable and choice.evidence.receiver_id == ctx.state.receiver_id
            and ctx.state.target is not None
            and choice.evidence.endpoint_fingerprint == ctx.state.target.endpoint_fingerprint
            and ctx.state.online
        )

    def _start(self, ctx, message, *, supersede=True):
        if message.request.mode is not Mode.VIRTUAL:
            return super()._start(ctx, message, supersede=supersede)
        state = ctx.state
        if (
            state.cleanup_pending or runtime_for(ctx).active or state.intent_active
            or state.stream is not None or state.display_handle is not None
            or state.ownership_uuid is not None or state.challenge is not None
        ):
            ctx.apply(last_error=diagnostic(ErrorCode.invalid_input, state.receiver_id))
            return
        if state.trust in (TrustState.CleanupRequired, TrustState.RevocationPending):
            ctx.apply(last_error=diagnostic(ErrorCode.trust_cleanup_required, state.receiver_id))
            return
        if state.target is None or not state.online:
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return
        if message.request.source is not None:
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_source, state.receiver_id))
            return
        if message.request.resolution is not None and not self._valid_decision(ctx, message.request.resolution):
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return
        try:
            gate = _start_gate(self.deps, Mode.VIRTUAL, state.trust)
            if isinstance(gate, Err):
                ctx.apply(last_error=gate.error)
                return
            self.deps.transport.update_target(state.target, state.receiver_id)
        except Exception:
            ctx.apply(last_error=diagnostic(ErrorCode.unsupported_operation, state.receiver_id))
            return
        operation = ctx.supersede("start") if supersede else ctx.operation()
        from .audio import audio_runtime
        audio_runtime(ctx).start_volume = message.request.volume
        remembered = message.request.volume.value if (self.deps.capabilities().volume
                      and message.request.volume is not None and message.request.volume.value > 0) else None
        setattr(ctx, "_virtual_runtime", _VirtualRuntime(request=message.request))
        ctx.apply(
            remembered_volume=remembered,
            operation_id=operation.operation_id, mode=Mode.VIRTUAL, intent_active=True,
            intent_stage=IntentStage.starting, display=DisplayState.Resolving,
            transport=TransportState.Idle, resolution=None, observed_resolution=None,
            accepted_resolution=None, audio=AudioState.Disabled, audio_policy=message.request.audio,
            physical_source=None, last_error=None, muted=False,
        )
        self._load_resolution(ctx)

    def _new_operation(self, ctx):
        operation = ctx.operation()
        ctx.apply(operation_id=operation.operation_id)
        return operation

    def _load_resolution(self, ctx):
        ctx.launch(Route.virtual, "resolution_load", _current_context(ctx),
                   lambda: self.deps.receivers.get(ctx.state.receiver_id))

    def _record_set(self, ctx):
        view = self.deps.discovery.current(ctx.state.receiver_id)
        target = ctx.state.target
        if view is None or target is None or not view.online:
            return None
        record_set = view.record_set
        if (
            record_set.receiver_id != ctx.state.receiver_id
            or record_set.endpoint_fingerprint != target.endpoint_fingerprint
            or record_set.endpoint_generation != target.endpoint_generation
        ):
            return None
        return record_set

    def _query(self, ctx):
        target = ctx.state.target
        if target is None or not ctx.state.online:
            return
        operation = _current_context(ctx)
        ctx.launch(Route.virtual, "resolution_query", operation,
                   lambda: self._with_backend_events(ctx, operation, lambda: self.deps.transport.query_modes(target, operation)))

    def _close_start_bridge(self, ctx, operation_id):
        subscription = _virtual_runtime(ctx).start_bridges.pop(operation_id, None)
        if subscription is not None:
            subscription.close()

    async def _with_backend_events(self, ctx, operation, work, *, acquisition=False):
        # Before a stream exists, engine admission intentionally rejects raw
        # stream-less events. This scoped bridge delivers messages for the
        # particular query/start; the actor checks target, operation, session,
        # and lease fields. A start bridge stays alive through actor adoption,
        # covering notifications queued between task return and result delivery.
        def receive(event):
            if event.receiver_id != ctx.state.receiver_id:
                return
            if event.kind not in (
                TransportEventKind.pairing_required, TransportEventKind.credential_invalid,
                TransportEventKind.ready,
            ):
                return
            ctx.enqueue(EffectEvent(
                receiver_id=event.receiver_id, operation_id=operation.operation_id,
                session_generation=operation.session_generation,
                kind="virtual:backend_request", result=Ok(value=event), resource=None,
            ))
        subscription = self.deps.transport.subscribe(receive)
        if acquisition:
            _virtual_runtime(ctx).start_bridges[operation.operation_id] = subscription
        try:
            return await work()
        except BaseException:
            # A cancelled call has no acquisition delivery to close its bridge.
            if acquisition:
                self._close_start_bridge(ctx, operation.operation_id)
            raise
        finally:
            if not acquisition:
                subscription.close()

    def _resolved(self, ctx):
        runtime = _virtual_runtime(ctx)
        record_set = self._record_set(ctx)
        if record_set is None:
            ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.active, display=DisplayState.None_)
            return
        runtime.record_set = record_set
        request = runtime.request
        user = request.resolution.size if request is not None and request.resolution is not None and request.resolution.source == "user" else None
        choice = _resolve(record_set, runtime.saved, runtime.queried, user)
        if not isinstance(choice, ResolutionDecision):
            ctx.apply(display=DisplayState.Resolving, intent_stage=IntentStage.resolution, transport=TransportState.Idle)
            return
        ctx.apply(resolution=choice, display=DisplayState.None_, intent_stage=IntentStage.starting)
        if ctx.state.trust is TrustState.Pairing or runtime.pairing_requested:
            return
        self._create(ctx)

    def _create(self, ctx):
        runtime = _virtual_runtime(ctx)
        target, choice = ctx.state.target, ctx.state.resolution
        if (
            target is None or choice is None or not ctx.state.online
            or ctx.state.cleanup_pending or runtime_for(ctx).active or runtime.creating
            or ctx.state.trust in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)
        ):
            return
        operation = _current_context(ctx)
        runtime.creating = True
        runtime.creation_context = operation
        ctx.apply(display=DisplayState.Creating, intent_stage=IntentStage.starting, transport=TransportState.Idle)

        async def create():
            def progress(event):
                if (
                    event.receiver_id == ctx.state.receiver_id
                    and event.creation_operation_id in (None, operation.operation_id)
                ):
                    ctx.enqueue(EffectEvent(
                        receiver_id=event.receiver_id, operation_id=operation.operation_id,
                        session_generation=operation.session_generation, kind="virtual:creation_progress",
                        result=Ok(value=event), resource=None,
                    ))
            subscription = self.deps.display.subscribe(progress)
            try:
                return await self.deps.display.create(ctx.state.receiver_id, target.endpoint_fingerprint, choice.size, operation)
            finally:
                subscription.close()

        ctx.launch(Route.virtual, "display_create", operation, create, acquisition=True)

    def _display_event(self, ctx, event):
        runtime = _virtual_runtime(ctx)
        if event.kind in ("reserved", "helper_started", "output_observed", "published"):
            if not runtime.creating or runtime.creation_context is None:
                return False
            if event.creation_operation_id != runtime.creation_context.operation_id:
                return False
            if runtime.owner not in (None, event.owner_uuid):
                return False
            if event.kind != "reserved" and runtime.owner is None:
                return False
            if (
                (runtime.helper is not None and event.helper not in (None, runtime.helper))
                or (runtime.connector is not None and event.connector not in (None, runtime.connector))
                or (runtime.output_id is not None and event.output_id not in (None, runtime.output_id))
            ):
                return False
            stages = ("reserved", "helper_started", "output_observed", "published")
            stage = stages.index(event.kind)
            if stage < runtime.stage:
                return False
            runtime.stage, runtime.owner = stage, event.owner_uuid
            runtime.helper = event.helper or runtime.helper
            runtime.connector = event.connector or runtime.connector
            runtime.output_id = event.output_id or runtime.output_id
            ctx.apply(ownership_uuid=event.owner_uuid)
            return True
        if event.kind in ("helper_exit", "disappeared", "cleanup_required"):
            if ctx.state.display_handle is None:
                if runtime.owner != event.owner_uuid:
                    return False
                if runtime.helper is None or event.helper != runtime.helper:
                    return False
                if runtime.connector is not None and event.connector != runtime.connector:
                    return False
                if runtime.output_id is not None and event.output_id != runtime.output_id:
                    return False
            if runtime_for(ctx).active:
                return True
            ctx.apply(display=DisplayState.Failed, last_error=event.diagnostic or diagnostic(ErrorCode.helper_failed, ctx.state.receiver_id))
            self.cleanup_route(ctx, Stop(receiver_id=ctx.state.receiver_id))
            return True
        return event.kind == "removed"

    def _confirmed_handle(self, ctx, handle):
        runtime = _virtual_runtime(ctx)
        return (
            isinstance(handle, DisplayHandle) and isinstance(handle.source, VirtualOutputSource)
            and ctx.state.resolution is not None and handle.source.size == ctx.state.resolution.size
            and bool(handle.source.connector) and bool(handle.source.kscreen_output_id)
            and handle.helper.pid > 0 and handle.helper.start_ticks > 0
            and runtime.owner in (None, handle.owner_uuid)
            and runtime.helper in (None, handle.helper)
            and runtime.connector in (None, handle.source.connector)
            and runtime.output_id in (None, handle.source.kscreen_output_id)
        )

    def _prepare_audio(self, ctx):
        if not ctx.state.audio_policy.enabled:
            ctx.apply(audio=AudioState.Disabled)
            self._begin_stream(ctx)
        else:
            ctx.launch(Route.virtual, "audio_probe", _current_context(ctx), self.deps.audio_probe.check)

    def _start_after_trust(self, ctx):
        if ctx.state.mode is not Mode.VIRTUAL:
            return super()._start_after_trust(ctx)
        runtime = _virtual_runtime(ctx)
        runtime.pairing_requested = False
        if not ctx.state.intent_active or ctx.state.cleanup_pending or runtime_for(ctx).active:
            return False
        if runtime.pause_inflight:
            runtime.pause_reason = "retry"
            return True
        if ctx.outstanding_acquisitions(names=frozenset({"transport_start"})):
            # Trust may finish before the connect that requested it. Keep that
            # acquisition fenced and owned until it delivers its exact result.
            runtime.resume_after_start = True
            return True
        self._new_operation(ctx)
        if ctx.state.display is DisplayState.Owned:
            if ctx.state.stream is not None:
                self._pause(ctx, "retry")
            else:
                self._prepare_audio(ctx)
        elif ctx.state.resolution is not None:
            self._create(ctx)
        elif runtime.query_complete:
            self._resolved(ctx)
        else:
            self._query(ctx)
        return True

    def _begin_stream(self, ctx):
        if ctx.state.mode is not Mode.VIRTUAL:
            return super()._begin_stream(ctx)
        target, handle = ctx.state.target, ctx.state.display_handle
        if (
            target is None or handle is None or ctx.state.display is not DisplayState.Owned
            or not ctx.state.online or not ctx.state.intent_active or ctx.state.cleanup_pending
            or runtime_for(ctx).active or _virtual_runtime(ctx).pause_inflight
            or ctx.state.stream is not None or ctx.state.accepted_resolution is not None
            or ctx.state.trust in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)
        ):
            return
        runtime = _virtual_runtime(ctx)
        runtime.pending_ready = None
        runtime.ready_stream = None
        from .audio import audio_runtime
        policy = ctx.state.audio_policy
        startup_request = runtime.request is not None
        initial = (audio_runtime(ctx).start_volume
                   if startup_request else
                   VolumeLevel(value=ctx.state.remembered_volume)
                   if ctx.state.remembered_volume is not None else None)
        if not self.deps.capabilities().volume or not policy.enabled:
            initial = None
        runtime.request = None
        operation = ctx.new_transport_lease(_current_context(ctx))
        ctx.apply(transport=TransportState.Connecting, intent_stage=IntentStage.starting)
        ctx.launch(
            Route.virtual, "transport_start", operation,
            lambda policy=policy, initial=initial: self._with_backend_events(ctx, operation, lambda: self.deps.transport.start_stream(target, handle.source, policy, operation, initial_volume=initial), acquisition=True),
            acquisition=True,
        )

    def _failed_display(self, ctx, error):
        _virtual_runtime(ctx).creating = False
        ctx.apply(display=DisplayState.Failed, last_error=error)
        self.cleanup_route(ctx, Stop(receiver_id=ctx.state.receiver_id))

    def _effect(self, ctx, message):
        kind = message.kind.rsplit(":", 1)[-1]
        runtime = _virtual_runtime(ctx)
        if kind == "resolution_load":
            if isinstance(message.result, Err):
                ctx.apply(transport=TransportState.Failed, display=DisplayState.None_, last_error=message.result.error)
                return True
            runtime.saved = message.result.value
            request = runtime.request
            if request is not None and request.resolution is not None and request.resolution.source == "user":
                self._resolved(ctx)
            else:
                self._query(ctx)
            return True
        if kind == "resolution_query":
            runtime.query_complete = True
            runtime.queried = message.result.value if isinstance(message.result, Ok) and isinstance(message.result.value, ReceiverCapabilities) else None
            if ctx.state.trust is TrustState.Pairing or runtime.pairing_requested:
                ctx.apply(display=DisplayState.None_, intent_stage=IntentStage.starting)
            else:
                self._resolved(ctx)
            return True
        if kind == "backend_request":
            event = message.result.value if isinstance(message.result, Ok) else None
            target = ctx.state.target
            if not isinstance(event, TransportEvent) or target is None:
                return True
            if (
                event.receiver_id != ctx.state.receiver_id or not ctx.state.online
                or event.endpoint_fingerprint != target.endpoint_fingerprint
                or event.endpoint_generation != target.endpoint_generation
                or event.context.endpoint_generation != target.endpoint_generation
                or event.context.session_generation != ctx.state.session_generation
                or event.context.operation_id != ctx.state.operation_id
                or event.context.transport_lease_id != ctx.state.transport_lease_id
                or event.context.trust_generation != ctx.state.trust_generation
            ):
                return True
            if event.kind is TransportEventKind.ready:
                handle = event.stream
                if (
                    not isinstance(handle, StreamHandle)
                    or handle.start_operation_id != ctx.state.operation_id
                    or not accept_transport(replace(ctx.state, stream=handle), event)
                ):
                    return True
                if ctx.state.stream is None:
                    # A real transport can emit ready inside start_stream. Hold
                    # the proof until the acquisition returns the exact handle.
                    runtime.pending_ready = event
                elif accept_transport(ctx.state, event):
                    self._transport_event(ctx, event)
                return True
            self._transport_event(ctx, event)
            return True
        if kind == "creation_progress":
            if isinstance(message.result, Ok) and isinstance(message.result.value, DisplayEvent):
                self._display_event(ctx, message.result.value)
            return True
        if kind == "display_create":
            runtime.creating = False
            if isinstance(message.result, Err):
                self._failed_display(ctx, message.result.error)
                return True
            handle = message.result.value
            if not self._confirmed_handle(ctx, handle):
                if isinstance(handle, DisplayHandle):
                    runtime_for(ctx).late_resources.append(handle)
                self._failed_display(ctx, diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
                return True
            ctx.apply(display_handle=handle, ownership_uuid=handle.owner_uuid)
            ctx.launch(Route.cleanup, "virtual_display_confirm", _current_context(ctx),
                       self.deps.ownerships.all, resource=handle)
            return True
        if kind == "display_confirm":
            handle = ctx.state.display_handle
            records = message.result.value if isinstance(message.result, Ok) else ()
            matched = next((record for record in records if handle is not None and record.owner_uuid == handle.owner_uuid), None)
            if (
                handle is None or matched is None or matched.receiver_id != ctx.state.receiver_id
                or matched.stage != "published" or matched.desired_state != "active"
                or matched.helper != handle.helper or matched.connector != handle.source.connector
                or matched.output_id != handle.source.kscreen_output_id
                or matched.observed_size != handle.source.size
                or matched.requested_size != ctx.state.resolution.size
            ):
                self._failed_display(ctx, message.result.error if isinstance(message.result, Err) else diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
                return True
            ctx.apply(display=DisplayState.Owned, observed_resolution=handle.source.size, last_error=None)
            self._prepare_audio(ctx)
            return True
        if kind == "transport_start" and ctx.state.mode is Mode.VIRTUAL:
            self._close_start_bridge(ctx, message.operation_id)
            if not ctx.state.online:
                return self._stale_transport(ctx, message)
            if isinstance(message.result, Err):
                runtime.pending_ready = None
                if runtime.resume_after_start:
                    runtime.resume_after_start = False
                    self._new_operation(ctx)
                    ctx.launch(Route.virtual, "resume_after_pairing", _current_context(ctx), _confirmed)
                elif ctx.state.trust is not TrustState.Pairing:
                    ctx.apply(transport=TransportState.Failed, intent_stage=IntentStage.active, last_error=message.result.error)
                return True
            handle = message.result.value
            if not isinstance(handle, StreamHandle):
                ctx.apply(transport=TransportState.Failed, last_error=diagnostic(ErrorCode.transport_failed, ctx.state.receiver_id))
                return True
            ctx.apply(stream=handle, transport=TransportState.Connecting, intent_stage=IntentStage.starting)
            if runtime.resume_after_start:
                runtime.resume_after_start = False
                self._pause(ctx, "retry")
                return True
            if ctx.state.trust in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending):
                self._pause(ctx, "pairing")
                return True
            pending, runtime.pending_ready = runtime.pending_ready, None
            if pending is not None and accept_transport(ctx.state, pending):
                self._transport_event(ctx, pending)
            return True
        if kind == "resume_after_pairing":
            self._prepare_audio(ctx)
            return True
        if kind == "remember_resolution":
            if isinstance(message.result, Err):
                if message.result.error.code is not ErrorCode.stale_target:
                    ctx.apply(last_error=message.result.error)
            else:
                ctx.apply()  # Preference caches subscribe to fresh publications.
            return True
        return super()._effect(ctx, message)

    def _stream_ready(self, ctx):
        runtime = _virtual_runtime(ctx)
        handle = ctx.state.stream
        if (
            handle is None or not ctx.state.online or not ctx.state.intent_active
            or ctx.state.cleanup_pending or runtime_for(ctx).active or runtime.pause_inflight
            or ctx.state.display is not DisplayState.Owned or ctx.state.accepted_resolution is not None
            or ctx.state.trust in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)
        ):
            return
        ctx.apply(transport=TransportState.Streaming, intent_stage=IntentStage.active)
        if runtime.ready_stream == handle:
            return
        runtime.ready_stream = handle
        if ctx.state.audio_policy.enabled:
            audio_id = ctx.operation().operation_id
            ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id)
            audio_context = _current_context(ctx, audio=audio_id)
            ctx.launch(Route.audio, "audio_start", audio_context,
                       lambda: self.deps.transport.start_audio(handle, audio_context))
        else:
            ctx.apply(audio=AudioState.Disabled)
        self._remember(ctx)

    def _remember(self, ctx: SessionContext) -> None:
        record_set = self._record_set(ctx)
        choice, observed = ctx.state.resolution, ctx.state.observed_resolution
        if record_set is None or choice is None or observed is None:
            return
        remembered = _rememberable(record_set, choice, observed)
        if remembered is None:
            return
        admitted = ctx.state
        operation = _current_context(ctx)

        def still_current():
            state = ctx.state
            return (
                state.receiver_id == admitted.receiver_id and state.target == admitted.target
                and state.session_generation == admitted.session_generation
                and state.operation_id == admitted.operation_id
                and state.transport_lease_id == admitted.transport_lease_id
                and state.stream == admitted.stream
                and state.trust_generation == admitted.trust_generation
                and state.online and state.intent_active and not state.cleanup_pending
                and state.trust not in (TrustState.Pairing, TrustState.CleanupRequired, TrustState.RevocationPending)
                and state.resolution == choice and state.observed_resolution == observed
                and state.display_handle == admitted.display_handle
                and state.accepted_resolution is None
            )

        async def save():
            return await self.deps.preferences.update(
                record_set, remembered_resolution=remembered, still_current=still_current,
            )
        ctx.launch(Route.virtual, "remember_resolution", operation, save)

    def _transport_event(self, ctx, event):
        if ctx.state.mode is not Mode.VIRTUAL:
            return super()._transport_event(ctx, event)
        runtime = _virtual_runtime(ctx)
        if event.kind is TransportEventKind.pairing_required:
            if runtime_for(ctx).active:
                return False
            runtime.pairing_requested = True
            if ctx.state.display is DisplayState.Resolving:
                ctx.apply(display=DisplayState.None_, intent_stage=IntentStage.starting)
            began = self.pairing.begin_from_backend(ctx)
            if began:
                ctx.apply(transport=TransportState.Pairing, intent_stage=IntentStage.starting)
                if ctx.state.stream is not None:
                    self._pause(ctx, "pairing")
            return began
        if event.kind is TransportEventKind.credential_invalid:
            runtime.pairing_requested = True
            self.pairing.mark_invalid_from_backend(ctx)
            ctx.apply(transport=TransportState.Pairing)
            if ctx.state.stream is not None:
                self._pause(ctx, "pairing")
            return True
        if event.kind is TransportEventKind.accepted_mode:
            payload = event.payload
            choice = ctx.state.resolution
            if isinstance(payload, ModePayload) and choice is not None and _mismatch(choice, payload.size, payload.reliable):
                ctx.apply(accepted_resolution=payload.size, last_error=diagnostic(ErrorCode.resolution_mismatch, ctx.state.receiver_id))
                self._pause(ctx, "mismatch")
            return True
        if event.kind is TransportEventKind.daemon_exit:
            ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.active, last_error=diagnostic(ErrorCode.daemon_failed, ctx.state.receiver_id))
            return True
        if event.kind is TransportEventKind.network_lost:
            ctx.apply(transport=TransportState.Reconnecting, intent_stage=IntentStage.active)
            return True
        if event.kind is TransportEventKind.ready:
            if (
                event.context.trust_generation == ctx.state.trust_generation
                and accept_transport(ctx.state, event)
            ):
                self._stream_ready(ctx)
            return True
        return super()._transport_event(ctx, event)

    def _pause(self, ctx, reason):
        runtime = _virtual_runtime(ctx)
        runtime.pause_reason = reason
        runtime.pending_ready = None
        if runtime.pause_inflight:
            return
        stream = ctx.state.stream
        if stream is None:
            return
        runtime.pause_inflight = True
        # The exact stream stays owned until stop is acknowledged, but its audio
        # authority ends at pause admission. Late audio cannot clear a mismatch
        # or alter the pending pairing/retry transaction.
        ctx.cancel_audio()
        operation = _current_context(ctx)
        if reason != "pairing":
            ctx.apply(transport=TransportState.Stopping)
        ctx.launch(Route.cleanup, "virtual_retained_stop", operation,
                   lambda: self.deps.transport.stop_stream(stream, operation), resource=stream)

    def _paused(self, ctx, message):
        runtime = _virtual_runtime(ctx)
        runtime.pause_inflight = False
        if runtime_for(ctx).active:
            return self.cleanup(ctx, replace(message, kind="cleanup:transport_stop"))
        if isinstance(message.result, Err):
            ctx.apply(transport=TransportState.Stopping, last_error=message.result.error)
            return True
        if ctx.state.stream != message.resource:
            return True
        ctx.cancel_audio()
        ctx.apply(
            stream=None, transport_lease_id=None, audio=AudioState.Disabled,
            transport=TransportState.Pairing if ctx.state.trust is TrustState.Pairing else TransportState.Idle,
        )
        reason, runtime.pause_reason = runtime.pause_reason, None
        if reason == "retry" and ctx.state.intent_active and ctx.state.accepted_resolution is None:
            self._new_operation(ctx)
            self._prepare_audio(ctx)
        return True


def build_virtual_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    """Return the two disjoint Virtual routes; compose shared continuations below."""
    virtual = VirtualLifecycle(deps)
    return {Route.virtual: virtual, Route.resolution: virtual.resolution}


def compose_virtual_handlers(
    lifecycle: Mapping[Route, Handler], virtual: Mapping[Route, Handler],
) -> Mapping[Route, Handler]:
    """Merge disjoint routes and bind the shared lifecycle's Virtual continuations.

    PairingFlow and OrderedCleanup own their callback continuations at factory
    construction time. A plain dict union cannot teach a Mirror-only callback to
    resume Virtual creation. This explicit composition is also used by assembly.
    """
    duplicates = lifecycle.keys() & virtual.keys()
    if duplicates:
        raise ValueError(f"duplicate session routes: {duplicates}")
    owner = virtual.get(Route.virtual)
    if not isinstance(owner, VirtualLifecycle):
        raise TypeError("virtual routes must come from build_virtual_handlers")
    return {
        **lifecycle, **virtual,
        Route.cleanup: owner.cleanup_route,
        Route.trust: owner.trust_route,
        Route.reconnect: owner.reconnect,
        Route.audio: owner.audio,
    }


__all__ = ["build_virtual_handlers", "compose_virtual_handlers"]
