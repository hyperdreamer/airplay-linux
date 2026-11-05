"""Receiver-local audio decisions, video-preserving loss, and session mute.

Audio is an independent resource authority, not a shorthand for "video works".
This module owns the ``Route.audio`` handler and, through
:func:`compose_audio_handlers`, the audio-shaped messages that the Mirror,
Virtual, and Reconnect owners would otherwise apply directly:

* a capability probe result becomes exactly one explicit start decision; an
  enabled policy with unproven audio holds ``AudioDecisionRequired`` and never
  promotes the session to ``Mirroring``/``VirtualDisplay`` on its own;
* a pre-ready backend audio failure also holds that explicit decision, even
  when a partial transport stream already exists;
* a failure after video readiness keeps ``TransportState.Streaming`` and the
  exact stream/display handles untouched, records ``AudioState.Unavailable``,
  and disables mute because no state admits a mute command;
* ``RetryAudio`` mints a fresh audio operation id while preserving the session
  generation, transport lease, and display owner, then either calls
  ``start_audio`` for the exact current handle or reprobes an intent whose
  stream never started;
* mute is receiver-local and session-only: only a confirmed success changes
  ``muted``/``AudioState``; a failed or stale result leaves the last confirmed
  value alone.

Every mute gets its own operation id, so two overlapping mutes cannot apply an
older confirmation over a newer one, while the stream keeps its established
audio-operation fence. Nothing here persists a mute preference, tears down
video to retry audio, or invents a backend operation. When the transport cannot
retry audio independently the retry reports ``unsupported_operation`` and keeps
video and ``Stop`` available.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from airplay_linux.diagnostics import diagnostic, safe_activity
from airplay_linux.domain.messages import (
    Cancel,
    ConfirmSwitch,
    DiscoveryEvent,
    EffectEvent,
    FailurePayload,
    MutePayload,
    Retry,
    RetryAudio,
    SetMuted,
    SetVolume,
    Start,
    StartVideoOnly,
    Stop,
    TransportEvent,
    TransportEventKind,
    VolumePayload,
)
from airplay_linux.domain.operations import (
    LeaseId,
    OperationContext,
    OperationId,
    StreamHandle,
)
from airplay_linux.domain.records import AudioStartReceipt, SessionSnapshot
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    Diagnostic,
    Err,
    ErrorCode,
    IntentStage,
    Ok,
    SessionPhase,
    TransportState,
    TrustState,
    VolumeLevel,
)
from airplay_linux.policy.audio import audio_start_policy

from .engine import Handler, ManagerDeps, Route, SessionContext

#: Effect kinds whose result belongs to the audio authority.  The launching
#: owner tags the route (mirror/virtual/reconnect/audio); the suffix decides.
_AUDIO_EFFECTS: frozenset[str] = frozenset(
    {"audio_probe", "audio_start", "audio_retry", "mute", "audio_partial_stop",
     "volume", "audio_close", "remember_volume"},
)

#: Typed transport events that describe audio rather than the video stream.
_AUDIO_EVENTS: frozenset[TransportEventKind] = frozenset(
    {
        TransportEventKind.audio_start_failed,
        TransportEventKind.audio_failed,
        TransportEventKind.audio_recovered,
        TransportEventKind.mute_result,
        TransportEventKind.volume_result,
    },
)

#: Routes whose handlers launch audio work or observe audio transport events.
_INTERCEPT_ROUTES: tuple[Route, ...] = (
    Route.mirror,
    Route.virtual,
    Route.reconnect,
    Route.cleanup,
    Route.trust,
    Route.resolution,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class AudioRuntime:
    """Actor-local audio bookkeeping; never a second persisted authority.

    ``pending_mute`` holds the operation id of the one mute command whose
    confirmation has not arrived, together with the requested value, so an
    older confirmation cannot be applied to a newer request. ``muted_lease``
    names the transport lease on which mute was last confirmed: a replacement
    stream starts unmuted, because only a confirmed command may claim a mute.
    """

    pending_mute: tuple[OperationId, bool] | None = None
    muted_lease: LeaseId | None = None
    start_volume: VolumeLevel | None = None
    volume_inflight: tuple[OperationId, LeaseId, int, str] | None = None
    volume_desired: tuple[OperationId, LeaseId, int, str] | None = None
    volume_reported: bool = False
    volume_context: OperationContext | None = None
    volume_stream: StreamHandle | None = None
    volume_absence_reported: bool = False
    retry_requested: bool = False
    close_operation: OperationId | None = None
    close_context: OperationContext | None = None
    close_stream: StreamHandle | None = None
    preference_operation: OperationId | None = None
    preference_context: OperationContext | None = None
    preference_stream: StreamHandle | None = None
    availability: tuple[LeaseId | None, str] | None = None


def audio_runtime(ctx: SessionContext) -> AudioRuntime:
    """Return the audio bookkeeping for this actor context only."""
    runtime = getattr(ctx, "_session_audio_runtime", None)
    if runtime is None:
        runtime = AudioRuntime()
        setattr(ctx, "_session_audio_runtime", runtime)
    return runtime


def _failure(result: object, fallback: ErrorCode) -> Diagnostic:
    if isinstance(result, Err):
        return result.error
    return diagnostic(fallback)


def _event_error(
    event: TransportEvent, fallback: ErrorCode = ErrorCode.audio_unavailable,
) -> Diagnostic:
    if isinstance(event.payload, FailurePayload):
        return event.payload.diagnostic
    return diagnostic(fallback)


def _decision_error(error: Diagnostic, receiver_id) -> Diagnostic:
    """The explicit decision diagnostic offers the three required choices.

    A backend failure may carry a recovery list that describes a transport
    retry rather than the audio decision.  The decision is still about audio, so
    it is published from the sanitized catalog vocabulary (Retry audio / Start
    video only / Stop) while keeping the exact audio error code when the catalog
    knows it.
    """
    code = (
        error.code
        if error.code in (ErrorCode.audio_denied, ErrorCode.audio_unavailable, ErrorCode.volume_unavailable)
        else ErrorCode.audio_unavailable
    )
    return diagnostic(code, receiver_id)


def _stream_current(state: SessionSnapshot, stream: StreamHandle | None) -> bool:
    """Whether ``stream`` is the exact current video stream bound to this target."""
    if not isinstance(stream, StreamHandle):
        return False
    current = state.stream
    target = state.target
    return (
        current is not None
        and current == stream
        and target is not None
        and target.endpoint_fingerprint == stream.endpoint_fingerprint
        and target.endpoint_generation == stream.endpoint_generation
    )


class AudioLifecycle:
    """State handler for the audio route and the audio-shaped lifecycle messages."""

    def __init__(self, deps: ManagerDeps) -> None:
        self.deps = deps
        self._start: Callable[[SessionContext], None] | None = None

    # -- composition seams --------------------------------------------------

    def bind_start(self, start: Callable[[SessionContext], None]) -> None:
        """Bind the lifecycle owner's video-start continuation.

        The audio authority resolves a pending decision by asking the owner to
        continue its own start state machine; it never duplicates stream
        acquisition, lease creation, or pairing gates here.
        """
        if not callable(start):
            raise TypeError("an audio start continuation must be callable")
        self._start = start

    def claim(self, ctx: SessionContext, message: object) -> bool | None:
        """Handle audio messages; ``None`` means an unrelated owner decides."""
        runtime = audio_runtime(ctx)
        if (ctx.state.intent_active and not self.deps.capabilities().volume
                and not runtime.volume_absence_reported):
            runtime.volume_absence_reported = True
            self._emit(ctx, "audio.volume_unavailable", {"reason": "backend_unsupported"})
        if isinstance(message, Start):
            self._fresh_intent(ctx)
            return None
        if isinstance(message, ConfirmSwitch):
            if message.confirmed:
                # A confirmed switch ends this session; the queued start after
                # cleanup begins with no inherited mute.
                self._fresh_intent(ctx, force=True)
            return None
        if isinstance(message, RetryAudio):
            return self._retry(ctx)
        if (isinstance(message, Retry) and self.deps.capabilities().volume
                and ctx.state.audio_close_pending):
            return False
        if isinstance(message, SetVolume):
            return self._set_volume_command(ctx, message.level)
        if isinstance(message, SetMuted):
            return self._set_muted(ctx, message.muted)
        if isinstance(message, (Stop, Cancel)):
            self._retire_volume(ctx)
            return None
        if isinstance(message, StartVideoOnly):
            return self._explicit_video_only(ctx)
        if isinstance(message, EffectEvent):
            kind = message.kind.rsplit(":", 1)[-1]
            if kind in _AUDIO_EFFECTS:
                return self._effect(ctx, message, kind)
            return None
        if isinstance(message, DiscoveryEvent):
            view = message.receiver
            if view is not None and view.record_set.receiver_id == ctx.state.receiver_id:
                target = ctx._manager._target_from_view(view) if view.online else None
                if (target != ctx.state.target or view.online != ctx.state.online
                        or view.record_set.ambiguous or view.record_set.replacement_of is not None
                        or not view.record_set.persistable):
                    self._retire_volume(ctx)
            return None
        if isinstance(message, TransportEvent):
            if message.kind is TransportEventKind.daemon_exit:
                self._retire_volume(ctx)
                return None
            if message.kind in _AUDIO_EVENTS:
                return self._transport_event(ctx, message)
            if (
                message.kind is TransportEventKind.ready
                and ctx.state.intent_stage is IntentStage.audio_decision
            ):
                return self._ready_while_deciding(ctx, message)
            return None
        return None

    def after_transition(self, ctx: SessionContext, before: SessionSnapshot) -> None:
        after = ctx.state
        if (before.transport_lease_id != after.transport_lease_id
                or before.session_generation != after.session_generation
                or before.trust_generation != after.trust_generation
                or before.target != after.target
                or (before.intent_active and not after.intent_active)):
            self._retire_volume(ctx)
            audio_runtime(ctx).muted_lease = None
        if (self.deps.capabilities().volume and before.audio is not AudioState.Unavailable
                and after.audio is AudioState.Unavailable):
            audio_runtime(ctx).muted_lease = None

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        before = ctx.state
        result = bool(self.claim(ctx, message))
        self.after_transition(ctx, before)
        return result

    # -- helpers ------------------------------------------------------------

    def _context(
        self, ctx: SessionContext, *, audio: OperationId | None = None,
    ) -> OperationContext:
        """Build the fence for one audio-owned effect on the current intent."""
        state = ctx.state
        operation_id = state.operation_id
        if operation_id is None:
            operation_id = ctx.operation().operation_id
        target = state.target
        return OperationContext(
            operation_id=operation_id,
            session_generation=state.session_generation,
            endpoint_generation=target.endpoint_generation if target is not None else 0,
            transport_lease_id=state.transport_lease_id,
            trust_generation=state.trust_generation,
            audio_operation_id=state.audio_operation_id if audio is None else audio,
        )

    def _authority_current(self, ctx: SessionContext, stream: StreamHandle) -> bool:
        """Revalidate the receiver endpoint immediately before audio control."""
        view = self.deps.discovery.current(ctx.state.receiver_id)
        if view is None or not view.online:
            return False
        try:
            target = ctx._manager._target_from_view(view)
        except Exception:  # noqa: BLE001 - unreadable discovery is stale authority
            return False
        state = ctx.state
        return (
            state.online
            and state.target is not None
            and target == state.target
            and target.endpoint_fingerprint == stream.endpoint_fingerprint
            and target.endpoint_generation == stream.endpoint_generation
        )

    def _emit(
        self, ctx: SessionContext, code: str,
        fields: Mapping[str, str | int | bool],
    ) -> None:
        sink = getattr(self.deps, "activity", None)
        if sink is None:
            return
        try:
            event = safe_activity(code, ctx.state.receiver_id, fields, _utc_now())
            sink.emit(event)
        except Exception:  # noqa: BLE001 - activity is never authoritative for audio
            return

    def _retire_volume(self, ctx: SessionContext, *, close: bool = True) -> None:
        runtime = audio_runtime(ctx)
        runtime.volume_inflight = None
        runtime.volume_desired = None
        runtime.volume_reported = False
        runtime.volume_context = None
        runtime.volume_stream = None
        runtime.retry_requested = False
        runtime.preference_operation = None
        runtime.preference_context = None
        runtime.preference_stream = None
        if close:
            runtime.close_operation = None
            runtime.close_context = None
            runtime.close_stream = None

    def _fresh_intent(self, ctx: SessionContext, *, force: bool = False) -> None:
        """Session-only mute never survives into a fresh start or mode switch."""
        state = ctx.state
        if not force and (
            state.intent_active or state.cleanup_pending or state.stream is not None
        ):
            return
        runtime = audio_runtime(ctx)
        runtime.pending_mute = None
        runtime.muted_lease = None
        self._retire_volume(ctx)
        runtime.start_volume = None
        runtime.volume_absence_reported = False
        runtime.availability = None
        if state.muted:
            ctx.apply(muted=False)

    def _begin(self, ctx: SessionContext) -> None:
        """Continue the owner's start after audio is no longer undecided."""
        state = ctx.state
        if state.stream is not None or state.cleanup_pending:
            return
        start = self._start
        if start is None:
            ctx.apply(
                last_error=diagnostic(ErrorCode.unsupported_operation, state.receiver_id),
            )
            return
        start(ctx)

    # -- failure states -----------------------------------------------------

    def _hold_decision(self, ctx: SessionContext, error: Diagnostic) -> None:
        """Publish the explicit pre-ready decision; never start video implicitly."""
        ctx.cancel_audio()
        audio_runtime(ctx).pending_mute = None
        if self.deps.capabilities().volume:
            self._retire_volume(ctx)
            audio_runtime(ctx).muted_lease = None
            ctx.apply(muted=False)
        decision = _decision_error(error, ctx.state.receiver_id)
        ctx.apply(
            audio=AudioState.Unavailable,
            intent_stage=IntentStage.audio_decision,
            last_error=decision,
        )
        self._emit(
            ctx,
            "audio.decision_required",
            {"audio": AudioState.Unavailable, "error_code": decision.code},
        )

    def _unavailable(self, ctx: SessionContext, error: Diagnostic) -> None:
        """Record runtime audio loss while leaving video and its fences intact.

        The failed audio operation stays authoritative so a later
        ``audio_recovered`` event for this exact stream can still be accepted;
        a replacement start or Stop retires it through the usual fences.
        """
        if self.deps.capabilities().volume:
            self._retire_volume(ctx)
            audio_runtime(ctx).muted_lease = None
            ctx.apply(muted=False)
        audio_runtime(ctx).pending_mute = None
        ctx.apply(audio=AudioState.Unavailable, last_error=error)
        self._emit(
            ctx,
            "audio.unavailable",
            {"audio": AudioState.Unavailable, "error_code": error.code},
        )

    def _confirm(self, ctx: SessionContext, *, recovered: bool = False) -> None:
        """Apply a confirmed audio success, respecting the confirmed mute state.

        A mute is confirmed for one exact transport lease.  A replacement
        stream starts unmuted, so an old session mute can never be reported as
        active on a stream that never received the command.
        """
        state = ctx.state
        runtime = audio_runtime(ctx)
        muted = (
            state.muted
            and runtime.muted_lease is not None
            and runtime.muted_lease == state.transport_lease_id
        )
        desired = AudioState.Muted if muted else AudioState.Enabled
        stage = state.intent_stage
        if stage is IntentStage.audio_decision:
            stage = (
                IntentStage.active
                if state.transport is TransportState.Streaming
                else IntentStage.starting
            )
        ctx.apply(audio=desired, muted=muted, intent_stage=stage, last_error=None)
        if recovered:
            code = "audio.recovered"
        else:
            code = "audio.muted" if desired is AudioState.Muted else "audio.enabled"
        self._emit(ctx, code, {"audio": desired})

    # -- start-path results -------------------------------------------------

    def _effect(self, ctx: SessionContext, message: EffectEvent, kind: str) -> bool:
        if kind == "audio_probe":
            return self._probe_result(ctx, message)
        if kind in ("audio_start", "audio_retry"):
            return self._start_result(ctx, message)
        if kind == "mute":
            return self._mute_result_effect(ctx, message)
        if kind == "volume":
            return self._volume_effect(ctx, message)
        if kind == "audio_close":
            return self._audio_close_effect(ctx, message)
        if kind == "remember_volume":
            return self._remember_volume_effect(ctx, message)
        if kind == "audio_partial_stop":
            return self._partial_stopped(ctx, message)
        return False

    def _probe_result(self, ctx: SessionContext, message: EffectEvent) -> bool:
        probe = message.result if isinstance(message.result, (Ok, Err)) else None
        decision = audio_start_policy(ctx.state.audio_policy, probe, False)
        if decision == "decision_required":
            error = (
                probe.error if isinstance(probe, Err)
                else diagnostic(ErrorCode.audio_unavailable)
            )
            self._hold_decision(ctx, error)
            return True
        if decision == "video_only":
            # An explicitly disabled policy is a recorded session choice, not an
            # inferred capability: it is not audited as a fallback decision.
            ctx.cancel_audio()
            ctx.apply(audio=AudioState.Disabled, last_error=None)
            self._begin(ctx)
            return True
        audio_id = ctx.operation().operation_id
        ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id, last_error=None)
        self._begin(ctx)
        return True

    def _start_result(self, ctx: SessionContext, message: EffectEvent) -> bool:
        if isinstance(message.result, Ok):
            receipt = message.result.value
            capable = self.deps.capabilities().volume
            if capable and not self._receipt_current(ctx, message):
                return True
            if capable and isinstance(receipt, AudioStartReceipt):
                return self._volume_start_result(ctx, receipt)
            self._confirm(ctx)
            return True
        error = _failure(message.result, ErrorCode.audio_unavailable)
        state = ctx.state
        if self.deps.capabilities().volume and not self._receipt_current(ctx, message):
            return True
        if (
            state.intent_stage is IntentStage.audio_decision
            or state.transport is not TransportState.Streaming
        ):
            self._hold_decision(ctx, error)
        else:
            self._unavailable(ctx, error)
        return True

    def _receipt_current(self, ctx: SessionContext, message: EffectEvent) -> bool:
        effect = ctx._manager._completed_effects.get((
            message.receiver_id, message.operation_id, message.kind,
        ))
        return (
            effect is not None and self._publication_current(ctx, effect.context, ctx.state.stream)
            and ctx.state.audio_policy.enabled and not ctx.state.audio_close_pending
        )

    def _volume_start_result(self, ctx: SessionContext, receipt: AudioStartReceipt) -> bool:
        state = ctx.state
        runtime = audio_runtime(ctx)
        if receipt.volume_state == "unrequested" and receipt.audio:
            self._confirm(ctx)
            return True
        if receipt.volume_state == "applied" and receipt.audio and receipt.level is not None:
            muted = receipt.level == 0
            runtime.muted_lease = state.transport_lease_id if muted else None
            stage = (IntentStage.active if state.transport is TransportState.Streaming
                     else IntentStage.starting)
            changes = {
                "audio": AudioState.Muted if muted else AudioState.Enabled,
                "muted": muted,
                "effective_volume": receipt.level,
                "last_audible_volume": (receipt.level if receipt.level > 0
                                         else state.last_audible_volume),
                "volume_state": "applied",
                "intent_stage": stage,
                "last_error": None,
            }
            changed = (state.audio not in (AudioState.Enabled, AudioState.Muted)
                       or state.volume_state != "applied")
            ctx.apply(**changes)
            runtime.availability = None
            if changed:
                self._emit(ctx, "audio.recovered", {"audio": changes["audio"]})
            return True
        if not receipt.audio and receipt.volume_state in ("applied", "unrequested"):
            error = replace(
                diagnostic(ErrorCode.audio_unavailable, state.receiver_id), detail="no_audio",
            )
            reason = "no_audio"
        else:
            reason = {
                "refused": "startup_refused",
                "pending": "receipt_pending",
            }.get(receipt.volume_state, "receipt_missing")
            error = replace(
                diagnostic(ErrorCode.volume_unavailable, state.receiver_id), detail=reason,
            )
        self._volume_decision(ctx, error, reason, receipt.volume_state)
        return True

    def _volume_decision(self, ctx: SessionContext, error: Diagnostic, reason: str,
                         volume_state: str | None) -> None:
        runtime = audio_runtime(ctx)
        runtime.pending_mute = None
        runtime.muted_lease = None
        self._retire_volume(ctx)
        ctx.cancel_audio()
        ctx.apply(
            audio=AudioState.Unavailable,
            muted=False,
            volume_state=volume_state,
            intent_stage=IntentStage.audio_decision,
            last_error=error,
        )
        availability = (ctx.state.transport_lease_id, reason)
        if runtime.availability != availability:
            runtime.availability = availability
            self._emit(ctx, "audio.volume_unavailable", {
                "reason": reason, "error_code": error.code,
            })

    def _ready_while_deciding(self, ctx: SessionContext, event: TransportEvent) -> bool:
        """Record that video connected while the audio decision remains open.

        The lifecycle owner's ready handler would promote the session to
        ``Mirroring``/``VirtualDisplay``.  Here the decision is still pending, so
        only the transport fact is applied; the derived phase stays
        ``AudioDecisionRequired`` until the user chooses.
        """
        if not _stream_current(ctx.state, event.stream):
            return False
        ctx.apply(transport=TransportState.Streaming)
        return True

    # -- pre-ready decision and retry --------------------------------------

    def _retry(self, ctx: SessionContext) -> bool:
        state = ctx.state
        if (
            not state.intent_active
            or state.cleanup_pending
            or state.audio_policy.enabled is not True
            or state.audio is not AudioState.Unavailable
        ):
            return False
        if (self.deps.capabilities().volume
                and (state.audio_close_pending
                     or (state.stream is not None and not self._control_current(ctx, state.stream))
                     or state.trust in (TrustState.Pairing, TrustState.RevocationPending,
                                        TrustState.CleanupRequired))):
            return False
        if (self.deps.capabilities().volume and state.stream is not None
                and state.volume_state == "refused"):
            runtime = audio_runtime(ctx)
            if runtime.volume_inflight is not None:
                runtime.retry_requested = True
                return True
            return self._retry_refused(ctx)
        if state.intent_stage is IntentStage.audio_decision:
            if state.stream is not None:
                # A partial backend stream already exists; retry audio on that
                # exact handle rather than acquiring a second transport stream.
                return self._start_audio(ctx, state.stream)
            return self._reprobe(ctx)
        if state.transport is not TransportState.Streaming or state.stream is None:
            return False
        return self._start_audio(ctx, state.stream)

    def _retry_refused(self, ctx: SessionContext) -> bool:
        if not self._volume_allowed(ctx, retry=True):
            return False
        runtime = audio_runtime(ctx)
        level = runtime.start_volume.value if runtime.start_volume is not None else None
        if level is None:
            return self._start_audio(ctx, ctx.state.stream)
        audio_id = ctx.operation().operation_id
        ctx.apply(audio_operation_id=audio_id, audio=AudioState.Unavailable,
                  intent_stage=IntentStage.audio_decision)
        return self._queue_volume(ctx, level, "retry")

    def _reprobe(self, ctx: SessionContext) -> bool:
        """Retry capability evidence for an intent whose stream never started."""
        audio_id = ctx.operation().operation_id
        ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id, last_error=None)
        context = self._context(ctx, audio=audio_id)
        ctx.launch(Route.audio, "audio_probe", context, self.deps.audio_probe.check)
        return True

    def _start_audio(self, ctx: SessionContext, stream: StreamHandle) -> bool:
        """Retry audio on the exact established (or partial) transport handle."""
        state = ctx.state
        if not _stream_current(state, stream):
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return True
        starter = getattr(self.deps.transport, "start_audio", None)
        if not callable(starter):
            self._unsupported_retry(ctx)
            return True
        if not self._authority_current(ctx, stream):
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return True
        audio_id = ctx.operation().operation_id
        ctx.apply(audio=AudioState.Starting, audio_operation_id=audio_id, last_error=None)
        context = self._context(ctx, audio=audio_id)
        ctx.launch(
            Route.audio,
            "audio_retry",
            context,
            lambda: starter(stream, context),
        )
        return True

    def _unsupported_retry(self, ctx: SessionContext) -> None:
        """Tear down nothing when the backend cannot retry audio independently."""
        ctx.apply(
            audio=AudioState.Unavailable,
            last_error=diagnostic(ErrorCode.unsupported_operation, ctx.state.receiver_id),
        )

    # -- explicit video-only choice ----------------------------------------

    def _explicit_video_only(self, ctx: SessionContext) -> bool:
        if self.deps.capabilities().volume:
            return self._capable_video_only(ctx)
        state = ctx.state
        if (
            state.intent_stage is not IntentStage.audio_decision
            or not state.intent_active
            or state.cleanup_pending
        ):
            return False
        # Stop listening to any capability probe still in flight, so a late
        # result cannot resume the start behind the user's back.
        ctx.cancel_audio()
        audio_runtime(ctx).pending_mute = None
        ctx.apply(
            audio_policy=AudioPolicy(enabled=False),
            audio=AudioState.Disabled,
            intent_stage=IntentStage.starting,
            last_error=None,
        )
        self._emit(
            ctx,
            "session.starting",
            {
                "explicit_video_only": True,
                "audio": AudioState.Disabled,
                "phase": SessionPhase.Connecting,
            },
        )
        stream = state.stream
        if stream is not None and state.transport is not TransportState.Streaming:
            # Confirm the unready partial stream is released before a new
            # transport lease starts.  The released handle is never reused.
            context = self._context(ctx)
            ctx.launch(
                Route.audio,
                "audio_partial_stop",
                context,
                lambda: self.deps.transport.stop_stream(stream, context),
                resource=stream,
            )
            return True
        if stream is not None:
            # Video is already streaming; the choice changes only audio.
            ctx.apply(intent_stage=IntentStage.active)
            return True
        self._begin(ctx)
        return True

    def _capable_video_only(self, ctx: SessionContext) -> bool:
        state = ctx.state
        if (not state.intent_active or state.cleanup_pending
                or (state.intent_stage is not IntentStage.audio_decision
                    and not state.audio_close_pending)):
            return False
        stream = state.stream
        if stream is not None and not self._control_current(ctx, stream):
            return False
        ctx.apply(audio_policy=AudioPolicy(enabled=False))
        self._retire_volume(ctx)
        ctx.cancel_audio()
        runtime = audio_runtime(ctx)
        runtime.pending_mute = None
        if stream is None:
            runtime.muted_lease = None
            ctx.apply(audio=AudioState.Disabled, muted=False, intent_stage=IntentStage.starting,
                      last_error=None)
            self._begin(ctx)
            return True
        audio_id = ctx.operation().operation_id
        ctx.apply(audio_operation_id=audio_id, audio_close_pending=True)
        operation = replace(self._context(ctx), operation_id=ctx.operation().operation_id)
        runtime.close_operation = operation.operation_id
        runtime.close_context = operation
        runtime.close_stream = stream
        ctx.launch(Route.audio, "audio_close", operation,
                   lambda: self.deps.transport.close_audio(stream, operation))
        return True

    def _partial_stopped(self, ctx: SessionContext, message: EffectEvent) -> bool:
        if isinstance(message.result, Err):
            ctx.apply(
                cleanup_pending=True,
                intent_stage=IntentStage.cleanup,
                last_error=message.result.error,
            )
            return True
        stream = ctx.state.stream
        if stream is not None and message.resource in (None, stream):
            ctx.apply(stream=None, transport_lease_id=None)
        if ctx.state.intent_active and not ctx.state.cleanup_pending:
            self._begin(ctx)
        return True

    # -- typed transport events --------------------------------------------

    def _transport_event(self, ctx: SessionContext, event: TransportEvent) -> bool:
        if event.kind is TransportEventKind.volume_result:
            return self._volume_result_event(ctx, event)
        if self.deps.capabilities().volume and (
            not self._control_current(ctx, event.stream)
            or event.context.trust_generation != ctx.state.trust_generation
            or not ctx.state.audio_policy.enabled or ctx.state.audio_close_pending
        ):
            return False
        if event.kind in (
            TransportEventKind.audio_start_failed,
            TransportEventKind.audio_failed,
        ):
            error = _event_error(event)
            state = ctx.state
            if (
                state.intent_stage is IntentStage.audio_decision
                or state.transport is not TransportState.Streaming
            ):
                self._hold_decision(ctx, error)
            else:
                self._unavailable(ctx, error)
            return True
        if event.kind is TransportEventKind.audio_recovered:
            state = ctx.state
            if (self.deps.capabilities().volume
                    and (state.intent_stage is IntentStage.audio_decision
                         or state.volume_state in ("refused", "pending")
                         or not state.audio_policy.enabled or state.audio_close_pending)):
                return False
            if (
                not state.intent_active
                or state.cleanup_pending
                or state.transport is not TransportState.Streaming
                or state.stream is None
                or state.audio not in (AudioState.Starting, AudioState.Unavailable)
            ):
                return False
            self._confirm(ctx, recovered=True)
            return True
        if event.kind is TransportEventKind.mute_result:
            return self._mute_result_event(ctx, event)
        return False

    # -- volume worker ownership -------------------------------------------

    def _control_current(self, ctx: SessionContext, stream: StreamHandle | None) -> bool:
        state = ctx.state
        view = self.deps.discovery.current(state.receiver_id)
        identity_current = (
            view is not None and view.record_set.persistable and not view.record_set.ambiguous
            and view.record_set.replacement_of is None
            and (view.record_set.identity_kind == "device" or view.record_set.continuity_verified)
        )
        return (
            identity_current
            and isinstance(stream, StreamHandle) and _stream_current(state, stream)
            and state.intent_active and not state.cleanup_pending
            and state.phase not in (SessionPhase.Stopping, SessionPhase.CleanupRequired,
                                    SessionPhase.TrustCleanupRequired)
            and state.trust not in (TrustState.Pairing, TrustState.RevocationPending,
                                    TrustState.CleanupRequired)
            and self._authority_current(ctx, stream)
        )

    def _publication_current(
        self, ctx: SessionContext, operation: OperationContext | None,
        stream: StreamHandle | None,
    ) -> bool:
        state = ctx.state
        return (
            operation is not None and self._control_current(ctx, stream)
            and operation.session_generation == state.session_generation
            and operation.trust_generation == state.trust_generation
            and operation.endpoint_generation == state.target.endpoint_generation
            and operation.transport_lease_id == state.transport_lease_id == stream.lease_id
            and operation.audio_operation_id == state.audio_operation_id
        )

    def _volume_allowed(self, ctx: SessionContext, *, retry: bool = False) -> bool:
        state = ctx.state
        return (
            self.deps.capabilities().volume and self._control_current(ctx, state.stream)
            and state.audio_policy.enabled and not state.audio_close_pending
            and (state.transport is TransportState.Streaming or retry)
            and (retry or (state.audio in (AudioState.Enabled, AudioState.Muted)
                           and state.intent_stage is not IntentStage.audio_decision))
        )

    def _set_volume_command(self, ctx: SessionContext, level: VolumeLevel) -> bool:
        if type(level) is not VolumeLevel or not self._volume_allowed(ctx):
            return False
        return self._queue_volume(ctx, level.value, "slider")

    def _queue_volume(self, ctx: SessionContext, level: int, origin: str) -> bool:
        runtime = audio_runtime(ctx)
        stream = ctx.state.stream
        if stream is None:
            return False
        request = (ctx.operation().operation_id, stream.lease_id, level, origin)
        if runtime.volume_inflight is not None:
            runtime.volume_desired = request
            return True
        self._launch_volume(ctx, request)
        return True

    def _launch_volume(self, ctx: SessionContext, request) -> None:
        runtime = audio_runtime(ctx)
        stream = ctx.state.stream
        operation = replace(self._context(ctx), operation_id=request[0])
        runtime.volume_inflight = request
        runtime.volume_context = operation
        runtime.volume_stream = stream
        runtime.volume_reported = False
        level = VolumeLevel(value=request[2])
        ctx.launch(
            Route.audio, "volume", operation,
            lambda: self.deps.transport.set_volume(stream, level, operation),
        )

    def _volume_failed(self, ctx: SessionContext, request) -> None:
        runtime = audio_runtime(ctx)
        if runtime.volume_reported:
            return
        runtime.volume_reported = True
        error = diagnostic(ErrorCode.audio_volume_failed, ctx.state.receiver_id)
        changes = {"last_error": error}
        if request[3] == "retry":
            changes.update(audio=AudioState.Unavailable, intent_stage=IntentStage.audio_decision)
        ctx.apply(**changes)
        self._emit(ctx, "audio.volume_failed", {"level": request[2], "error_code": error.code})

    def _volume_effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        runtime = audio_runtime(ctx)
        request = runtime.volume_inflight
        if request is None or request[0] != message.operation_id:
            return True
        if not self._publication_current(ctx, runtime.volume_context, runtime.volume_stream):
            self._retire_volume(ctx)
            return True
        if isinstance(message.result, Err):
            self._volume_failed(ctx, request)
        return True

    def _volume_result_event(self, ctx: SessionContext, event: TransportEvent) -> bool:
        runtime = audio_runtime(ctx)
        request = runtime.volume_inflight
        if (request is None or event.context.operation_id != request[0]
                or event.stream is None or event.stream.lease_id != request[1]
                or not isinstance(event.payload, VolumePayload)):
            return True
        if (not self._publication_current(ctx, event.context, runtime.volume_stream)
                or event.context != runtime.volume_context
                or not ctx.state.audio_policy.enabled or ctx.state.audio_close_pending):
            self._retire_volume(ctx)
            return True
        desired = runtime.volume_desired
        retry_requested = runtime.retry_requested
        if event.payload.ok and not runtime.volume_reported:
            self._acknowledge_volume(ctx, event.payload.level, request[3])
        else:
            self._volume_failed(ctx, request)
        runtime.volume_inflight = None
        runtime.volume_desired = None
        runtime.volume_context = None
        runtime.volume_stream = None
        runtime.retry_requested = False
        if retry_requested and request[3] == "retry" and ctx.state.audio is AudioState.Unavailable:
            self._retry_refused(ctx)
        elif desired is not None and self._volume_allowed(ctx):
            self._launch_volume(ctx, desired)
        return True

    def _acknowledge_volume(self, ctx: SessionContext, level: int, origin: str) -> None:
        state = ctx.state
        runtime = audio_runtime(ctx)
        muted = level == 0
        runtime.muted_lease = state.transport_lease_id if muted else None
        ctx.apply(
            effective_volume=level,
            last_audible_volume=level if level > 0 else state.last_audible_volume,
            muted=muted, audio=AudioState.Muted if muted else AudioState.Enabled,
            intent_stage=IntentStage.active if state.transport is TransportState.Streaming else IntentStage.starting,
            volume_state="applied", last_error=None,
        )
        self._emit(ctx, "audio.volume_applied", {"level": level})
        if origin == "retry":
            self._emit(ctx, "audio.recovered", {"audio": ctx.state.audio})
        elif origin == "slider" and level > 0:
            self._save_volume(ctx, level)

    def _save_volume(self, ctx: SessionContext, level: int) -> None:
        runtime = audio_runtime(ctx)
        request, operation, stream = runtime.volume_inflight, runtime.volume_context, runtime.volume_stream
        view = self.deps.discovery.current(ctx.state.receiver_id)
        if request is None or operation is None or stream is None or view is None:
            return
        runtime.preference_operation = request[0]
        runtime.preference_context = operation
        runtime.preference_stream = stream
        record_set = view.record_set

        def current():
            return (
                runtime.preference_operation == request[0]
                and self._publication_current(ctx, operation, stream)
                and ctx.state.audio_policy.enabled and not ctx.state.audio_close_pending
            )

        ctx.launch(
            Route.audio, "remember_volume", operation,
            lambda: self.deps.preferences.update(
                record_set, remembered_volume=VolumeLevel(value=level), still_current=current,
            ),
        )

    def _remember_volume_effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        runtime = audio_runtime(ctx)
        if (runtime.preference_operation != message.operation_id
                or not self._publication_current(ctx, runtime.preference_context, runtime.preference_stream)
                or not ctx.state.audio_policy.enabled or ctx.state.audio_close_pending):
            return True
        if isinstance(message.result, Ok):
            saved = message.result.value
            ctx.apply(remembered_volume=saved.remembered_volume, volume_save_failed=False, last_error=None)
        else:
            ctx.apply(volume_save_failed=True,
                      last_error=diagnostic(ErrorCode.volume_preference_failed, ctx.state.receiver_id))
        return True

    def _audio_close_effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        runtime = audio_runtime(ctx)
        if (runtime.close_operation != message.operation_id
                or not self._publication_current(ctx, runtime.close_context, runtime.close_stream)):
            return True
        runtime.close_operation = None
        if isinstance(message.result, Ok) and message.result.value is None:
            runtime.muted_lease = None
            ctx.apply(audio=AudioState.Disabled, muted=False, audio_close_pending=False,
                      intent_stage=IntentStage.active if ctx.state.transport is TransportState.Streaming
                      else IntentStage.starting, last_error=None)
        else:
            error = replace(diagnostic(ErrorCode.audio_volume_failed, ctx.state.receiver_id),
                            detail="close_unconfirmed")
            ctx.apply(last_error=error)
            self._emit(ctx, "audio.volume_failed", {
                "error_code": error.code, "reason": "close_unconfirmed",
            })
        return True

    # -- mute --------------------------------------------------------------

    def _set_muted(self, ctx: SessionContext, muted: bool) -> bool:
        state = ctx.state
        if self.deps.capabilities().volume:
            if type(muted) is not bool or not self._volume_allowed(ctx):
                return False
            level = 0 if muted else (state.last_audible_volume or state.remembered_volume or 100)
            return self._queue_volume(ctx, level, "mute" if muted else "unmute")
        stream = state.stream
        if (
            type(muted) is not bool
            or state.cleanup_pending
            or state.transport is not TransportState.Streaming
            or stream is None
            or state.audio not in (AudioState.Enabled, AudioState.Muted)
            or not _stream_current(state, stream)
        ):
            return False
        if not self._authority_current(ctx, stream):
            ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
            return True
        runtime = audio_runtime(ctx)
        # The mute gets a distinct operation id while the established audio
        # fence stays on the stream, so a stale confirmation is recognizable and
        # a runtime audio event for the playing stream is still accepted.
        mute_id = ctx.operation().operation_id
        runtime.pending_mute = (mute_id, muted)
        context = replace(self._context(ctx), operation_id=mute_id)
        ctx.launch(
            Route.audio,
            "mute",
            context,
            lambda: self.deps.transport.set_muted(stream, muted, context),
        )
        return True

    def _mute_result_effect(self, ctx: SessionContext, message: EffectEvent) -> bool:
        runtime = audio_runtime(ctx)
        pending = runtime.pending_mute
        if pending is None or message.operation_id != pending[0]:
            # A duplicate or stale confirmation for an operation the user has
            # already replaced cannot change the confirmed value.
            return True
        runtime.pending_mute = None
        if isinstance(message.result, Err):
            ctx.apply(last_error=message.result.error)
            return True
        if ctx.state.audio not in (AudioState.Enabled, AudioState.Muted):
            # Audio was lost while the mute was outstanding; a mute confirmation
            # cannot resurrect the audio resource.
            return True
        self._confirm_mute(ctx, pending[1])
        return True

    def _mute_result_event(self, ctx: SessionContext, event: TransportEvent) -> bool:
        pending = audio_runtime(ctx).pending_mute
        if pending is None or event.context.operation_id != pending[0]:
            return True
        payload = event.payload
        if not isinstance(payload, MutePayload):
            return True
        audio_runtime(ctx).pending_mute = None
        if not payload.ok:
            ctx.apply(
                last_error=diagnostic(ErrorCode.audio_mute_failed, ctx.state.receiver_id),
            )
            return True
        self._confirm_mute(ctx, payload.muted)
        return True

    def _confirm_mute(self, ctx: SessionContext, muted: bool) -> None:
        desired = AudioState.Muted if muted else AudioState.Enabled
        audio_runtime(ctx).muted_lease = ctx.state.transport_lease_id if muted else None
        ctx.apply(muted=muted, audio=desired, last_error=None)
        self._emit(ctx, "audio.muted" if muted else "audio.unmuted", {"audio": desired})


def build_audio_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    """Return the disjoint audio route; compose it with the lifecycle owner.

    The mapping assigns only ``Route.audio``.  The audio-shaped effects and
    transport events that a Mirror/Virtual/Reconnect owner launches are
    interposed by :func:`compose_audio_handlers`, which also binds the owner's
    start continuation.
    """
    return {Route.audio: AudioLifecycle(deps)}


def _start_continuation(
    lifecycle: Mapping[Route, Handler],
) -> Callable[[SessionContext], None] | None:
    # Prefer the most specific lifecycle owner: Virtual and Reconnect subclass
    # Mirror, and only their overrides can continue a Virtual start.
    seen: set[int] = set()
    for route in (Route.virtual, Route.reconnect, Route.mirror):
        handler = lifecycle.get(route)
        owner = getattr(handler, "__self__", None)
        if owner is None or id(owner) in seen:
            continue
        seen.add(id(owner))
        start = getattr(owner, "_begin_stream", None)
        if callable(start):
            return start
    return None


def _interpose(
    original: Handler, audio: AudioLifecycle,
) -> Callable[[SessionContext, object], bool]:
    def routed(ctx: SessionContext, message: object) -> bool:
        before = ctx.state
        claimed = audio.claim(ctx, message)
        result = claimed if claimed is not None else original(ctx, message)
        audio.after_transition(ctx, before)
        return result

    return routed


def compose_audio_handlers(
    lifecycle: Mapping[Route, Handler],
    audio: Mapping[Route, Handler],
    *,
    start: Callable[[SessionContext], None] | None = None,
) -> Mapping[Route, Handler]:
    """Bind the audio authority over the routes that currently launch audio work.

    Compose this after the Mirror/Virtual/Reconnect factories: those factories
    rebind ``Route.audio`` to their own lifecycle owner, and this composition
    must be the last one to own the audio-shaped messages.  The returned mapping
    keeps every unrelated route handler and interposes only the audio messages;
    the first bound ``_begin_stream`` supplies the video-start continuation.
    """
    if audio.keys() - {Route.audio}:
        raise ValueError(f"unexpected audio routes: {audio.keys() - {Route.audio}}")
    handler = audio.get(Route.audio)
    if not isinstance(handler, AudioLifecycle):
        raise TypeError("audio routes must come from build_audio_handlers")
    continuation = start if start is not None else _start_continuation(lifecycle)
    if not callable(continuation):
        raise TypeError(
            "the session lifecycle must provide a video-start continuation "
            "(a bound _begin_stream) for the audio decision",
        )
    handler.bind_start(continuation)
    composed: dict[Route, Handler] = dict(lifecycle)
    for route in _INTERCEPT_ROUTES:
        original = composed.get(route)
        if original is not None:
            composed[route] = _interpose(original, handler)
    composed[Route.audio] = handler
    return composed


__all__ = [
    "AudioLifecycle",
    "AudioRuntime",
    "audio_runtime",
    "build_audio_handlers",
    "compose_audio_handlers",
]
