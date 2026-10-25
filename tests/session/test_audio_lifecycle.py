"""Audio lifecycle: explicit decisions, preserved video, and session-local mute.

Every external boundary is a deterministic fake.  The handler tests deliver real
typed audio events and assert the recorded adapter calls, so the direct
snapshot projection assertion below is a supplement rather than the only
evidence.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel,
    EmptyPayload,
    FailurePayload,
    MutePayload,
    RetryAudio,
    SetMuted,
    Start,
    StartVideoOnly,
    Stop,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    InstanceId,
    OperationContext,
    OperationId,
    StreamHandle,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    RecoveryAction,
    SessionPhase,
    StartRequest,
    TransportState,
)
from airplay_linux.session.audio import (
    audio_runtime,
    build_audio_handlers,
    compose_audio_handlers,
)
from airplay_linux.session.engine import Route, SessionManager
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.reconnect import (
    build_reconnect_handlers,
    compose_reconnect_handlers,
)
from airplay_linux.session.testing import SessionRig
from airplay_linux.session.virtual import build_virtual_handlers, compose_virtual_handlers
from airplay_linux.testing.adapters import Deferred, FakeTransport
from airplay_linux.testing.builders import context, physical, target, virtual_handle
from virtual_scenarios import ContextTransport, decision, make_rig, settle, stream_event


# --------------------------------------------------------------------------- rigs


class AudioTransport(FakeTransport):
    """Accept the dialog's lease so an exact stream handle stays releasable."""

    def _new_handle(self, destination, operation):
        handle = super()._new_handle(destination, operation)
        self.streams.pop(handle.lease_id, None)
        handle = replace(handle, lease_id=operation.transport_lease_id)
        self.streams[handle.lease_id] = handle
        return handle


def audio_handlers(deps):
    return compose_audio_handlers(
        build_lifecycle_handlers(deps), build_audio_handlers(deps),
    )


def virtual_audio_handlers(deps):
    return compose_audio_handlers(
        compose_virtual_handlers(
            build_lifecycle_handlers(deps), build_virtual_handlers(deps),
        ),
        build_audio_handlers(deps),
    )


def reconnecting_audio_handlers(deps):
    return compose_audio_handlers(
        compose_reconnect_handlers(
            build_lifecycle_handlers(deps), build_reconnect_handlers(deps),
        ),
        build_audio_handlers(deps),
    )


def mirror_rig(*, transport=None, compose=audio_handlers):
    rig = SessionRig()
    for subscription in rig.manager._subscriptions:
        subscription.close()
    rig.transport = transport if transport is not None else AudioTransport()
    rig.deps = replace(rig.deps, transport=rig.transport)
    for adapter in (rig.capture, rig.transport, rig.audio, rig.receivers, rig.activity):
        adapter.journal = rig.journal
    rig.manager = SessionManager(rig.deps, compose(rig.deps))
    return rig


def virtual_rig(*, ready: bool):
    rig = make_rig(transport=ContextTransport(ready=ready))
    for subscription in rig.manager._subscriptions:
        subscription.close()
    rig.manager = SessionManager(rig.deps, virtual_audio_handlers(rig.deps))
    return rig


async def online_mirror_rig(*, transport=None):
    rig = mirror_rig(transport=transport)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.settle()
    return rig, destination


async def start_mirror(rig, destination, *, audio=None):
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(
            mode=Mode.MIRROR, source=physical(),
            audio=audio if audio is not None else AudioPolicy(),
        ),
    ))
    await settle(rig)
    return rig.manager.snapshot(destination.receiver_id)


async def start_virtual(rig, destination, *, audio=True):
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(
            mode=Mode.VIRTUAL, resolution=decision(destination),
            audio=AudioPolicy(enabled=audio),
        ),
    ))
    await settle(rig)
    return rig.manager.snapshot(destination.receiver_id)


async def dispatch(rig, command):
    receipt = await rig.manager.dispatch(command)
    await settle(rig)
    return receipt


def audio_event(
    rig,
    kind,
    *,
    audio_id=None,
    operation_id=None,
    payload=None,
    destination=None,
):
    """A typed audio event bound to the receiver's exact current stream."""
    destination = destination or target()
    state = rig.manager.snapshot(destination.receiver_id)
    handle = state.stream
    assert isinstance(handle, StreamHandle)
    operation = OperationContext(
        operation_id=handle.start_operation_id if operation_id is None else operation_id,
        session_generation=state.session_generation,
        endpoint_generation=handle.endpoint_generation,
        transport_lease_id=handle.lease_id,
        trust_generation=state.trust_generation,
        audio_operation_id=state.audio_operation_id if audio_id is None else audio_id,
    )
    return TransportEvent(
        kind=kind,
        receiver_id=destination.receiver_id,
        stream=handle,
        endpoint_fingerprint=handle.endpoint_fingerprint,
        endpoint_generation=handle.endpoint_generation,
        daemon_instance_uuid=handle.daemon_instance_uuid,
        context=operation,
        payload=payload if payload is not None else EmptyPayload(),
    )


def activity_codes(rig):
    return [event.code for event in rig.activity.events]


def journal_handle(retrieved):
    """The redacted journal projection of one exact stream handle."""
    assert isinstance(retrieved, dict)
    return retrieved


def _streaming_snapshot():
    operation = context(generation=1, lease=7)
    display = virtual_handle()
    handle = StreamHandle(
        receiver_id=target().receiver_id,
        endpoint_generation=1,
        endpoint_fingerprint=target().endpoint_fingerprint,
        backend_stream_id="stream-projection",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=operation.transport_lease_id,
        start_operation_id=operation.operation_id,
    )
    return replace(
        SessionSnapshot.idle(handle.receiver_id, online=True),
        target=target(), stream=handle, session_generation=1,
        operation_id=operation.operation_id, transport_lease_id=handle.lease_id,
        transport=TransportState.Streaming, display_handle=display,
        ownership_uuid=display.owner_uuid, display=DisplayState.Owned,
        audio=AudioState.Enabled, intent_active=True,
        intent_stage=IntentStage.active,
    )


@pytest.fixture
def streaming_snapshot():
    return _streaming_snapshot()


def test_audio_unavailable_projection_retains_stream(streaming_snapshot):
    updated = replace(streaming_snapshot, audio=AudioState.Unavailable)
    assert updated.transport is TransportState.Streaming
    assert updated.stream == streaming_snapshot.stream
    assert updated.display_handle == streaming_snapshot.display_handle


def test_build_audio_handlers_assigns_only_the_audio_route():
    rig = SessionRig()
    routes = build_audio_handlers(rig.deps)
    assert set(routes) == {Route.audio}
    with pytest.raises(TypeError, match="continuation"):
        compose_audio_handlers({}, routes)


# --------------------------------------------------------------- enabled default


def test_audio_policy_defaults_to_enabled_and_snapshot_audio_starts_disabled():
    request = StartRequest(mode=Mode.MIRROR)
    assert request.audio.enabled is True
    idle = SessionSnapshot.idle(target().receiver_id, online=True)
    assert idle.audio_policy.enabled is True
    # ``Disabled`` is the resource state before a start chooses audio; it is not
    # the default *policy* and no start infers it from capability evidence.
    assert idle.audio is AudioState.Disabled


@pytest.mark.asyncio
async def test_default_start_probes_and_confirms_enabled_audio_without_touching_video():
    rig, destination = await online_mirror_rig()
    state = await start_mirror(rig, destination)

    assert state.audio is AudioState.Enabled
    assert state.muted is False
    assert state.intent_stage is IntentStage.active
    assert state.phase is SessionPhase.Mirroring
    assert state.transport is TransportState.Streaming
    assert state.stream is not None
    assert state.audio_operation_id is not None
    assert rig.journal.names().count("audio_probe.check") == 1
    starts = rig.journal.calls("transport.start_audio")
    assert len(starts) == 1
    assert journal_handle(starts[0]["context"])["audio_operation_id"] == (
        state.audio_operation_id
    )
    assert "audio.enabled" in activity_codes(rig)
    assert "transport.stop_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_explicitly_disabled_policy_starts_video_without_probing_or_audio():
    rig, destination = await online_mirror_rig()
    state = await start_mirror(rig, destination, audio=AudioPolicy(enabled=False))

    assert state.audio_policy.enabled is False
    assert state.audio is AudioState.Disabled
    assert state.transport is TransportState.Streaming
    assert state.stream is not None
    assert state.audio_operation_id is None
    assert "audio_probe.check" not in rig.journal.names()
    assert "transport.start_audio" not in rig.journal.names()
    assert "audio.decision_required" not in activity_codes(rig)


# ------------------------------------------------------- pre-ready audio decision


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [ErrorCode.audio_denied, ErrorCode.audio_unavailable])
async def test_denied_or_unavailable_probe_requires_an_explicit_decision(code):
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(code)))
    state = await start_mirror(rig, destination)

    assert state.audio is AudioState.Unavailable
    assert state.intent_stage is IntentStage.audio_decision
    assert state.phase is SessionPhase.AudioDecisionRequired
    assert state.last_error is not None and state.last_error.code is code
    assert state.intent_active is True
    assert state.stream is None
    assert state.physical_source == physical()
    assert "transport.start_stream" not in rig.journal.names()
    assert "transport.start_audio" not in rig.journal.names()
    assert "audio.decision_required" in activity_codes(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (ErrorCode.audio_denied, ErrorCode.audio_denied),
        (ErrorCode.transport_failed, ErrorCode.audio_unavailable),
    ],
)
async def test_audio_decision_publishes_the_three_explicit_choices(backend, expected):
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(backend)))
    state = await start_mirror(rig, destination)

    assert state.phase is SessionPhase.AudioDecisionRequired
    assert state.last_error is not None
    assert state.last_error.code is expected
    assert state.last_error.recovery == (
        RecoveryAction.retry_audio, RecoveryAction.video_only, RecoveryAction.stop,
    )
    assert state.stream is None
    assert "transport.start_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_retry_audio_from_the_decision_reprobes_and_starts_enabled():
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
    before = await start_mirror(rig, destination)
    assert before.phase is SessionPhase.AudioDecisionRequired

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Enabled
    assert after.phase is SessionPhase.Mirroring
    assert after.session_generation == before.session_generation
    assert after.operation_id == before.operation_id
    assert after.physical_source == before.physical_source
    assert rig.journal.names().count("audio_probe.check") == 2
    assert len(rig.journal.calls("transport.start_stream")) == 1
    assert len(rig.journal.calls("transport.start_audio")) == 1
    assert "capture.release" not in rig.journal.names()


@pytest.mark.asyncio
async def test_retry_audio_that_still_fails_keeps_the_explicit_decision():
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
    await start_mirror(rig, destination)
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_unavailable)))

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    state = rig.manager.snapshot(destination.receiver_id)

    assert state.audio is AudioState.Unavailable
    assert state.intent_stage is IntentStage.audio_decision
    assert state.phase is SessionPhase.AudioDecisionRequired
    assert state.last_error.code is ErrorCode.audio_unavailable
    assert "transport.start_stream" not in rig.journal.names()
    assert activity_codes(rig).count("audio.decision_required") == 2


@pytest.mark.asyncio
async def test_explicit_start_video_only_records_the_choice_and_starts_video():
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
    before = await start_mirror(rig, destination)

    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio_policy.enabled is False
    assert after.audio is AudioState.Disabled
    assert after.phase is SessionPhase.Mirroring
    assert after.transport is TransportState.Streaming
    assert after.stream is not None
    assert after.session_generation == before.session_generation
    assert after.operation_id == before.operation_id
    assert len(rig.journal.calls("transport.start_stream")) == 1
    assert "transport.start_audio" not in rig.journal.names()
    decisions = [
        event for event in rig.activity.events
        if event.fields.get("explicit_video_only") is True
    ]
    assert len(decisions) == 1
    assert decisions[0].fields["audio"] == AudioState.Disabled.value


@pytest.mark.asyncio
async def test_cancel_from_the_decision_releases_capture_without_implicit_video():
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
    await start_mirror(rig, destination)

    await dispatch(rig, Cancel(receiver_id=destination.receiver_id))
    state = rig.manager.snapshot(destination.receiver_id)

    assert state.audio is AudioState.Disabled
    assert not state.intent_active
    assert state.stream is None
    assert state.physical_source is None
    assert state.cleanup_pending is False
    assert "transport.start_stream" not in rig.journal.names()
    assert rig.capture.released == [physical()]
    assert "audio.partial" not in " ".join(rig.journal.names())


# ------------------------------------------------------- runtime loss preserves video


@pytest.mark.asyncio
async def test_runtime_audio_failure_preserves_video_and_disables_mute():
    rig = virtual_rig(ready=True)
    destination = target()
    before = await start_virtual(rig, destination)
    assert before.audio is AudioState.Enabled
    handle = before.display_handle
    assert handle is not None

    rig.transport.emit(audio_event(
        rig, TransportEventKind.audio_failed,
        payload=FailurePayload(diagnostic=diagnostic(ErrorCode.audio_unavailable)),
    ))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Unavailable
    assert after.transport is TransportState.Streaming
    assert after.stream == before.stream
    assert after.display is DisplayState.Owned
    assert after.display_handle == handle
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.last_error.code is ErrorCode.audio_unavailable
    assert "transport.stop_stream" not in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert "audio.unavailable" in activity_codes(rig)

    # Mute is disabled while audio is unavailable: no backend command is sent.
    rig.journal.clear()
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    assert "transport.set_muted" not in rig.journal.names()
    assert rig.manager.snapshot(destination.receiver_id).muted is False


@pytest.mark.asyncio
async def test_retry_after_runtime_loss_keeps_transport_and_display_leases():
    rig = virtual_rig(ready=True)
    destination = target()
    before = await start_virtual(rig, destination)
    handle = before.display_handle
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    lost = rig.manager.snapshot(destination.receiver_id)
    old_audio = lost.audio_operation_id
    rig.journal.clear()

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Enabled
    assert after.audio_operation_id is not None
    assert after.audio_operation_id != old_audio
    assert after.session_generation == before.session_generation
    assert after.operation_id == lost.operation_id
    assert after.transport_lease_id == before.transport_lease_id
    assert after.stream == before.stream
    assert after.display_handle == handle
    assert after.display is DisplayState.Owned
    starts = rig.journal.calls("transport.start_audio")
    assert len(starts) == 1
    exact = journal_handle(starts[0]["handle"])
    assert exact["lease_id"] == before.stream.lease_id
    assert exact["backend_stream_id"] == before.stream.backend_stream_id
    audio_context = journal_handle(starts[0]["context"])
    assert audio_context["audio_operation_id"] == after.audio_operation_id
    assert "transport.stop_stream" not in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert "transport.start_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_failed_audio_retry_keeps_video_and_unavailable_state():
    rig = virtual_rig(ready=True)
    destination = target()
    before = await start_virtual(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    rig.transport.queue_result(
        "start_audio", Err(error=diagnostic(ErrorCode.audio_denied)),
    )

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Unavailable
    assert after.last_error.code is ErrorCode.audio_denied
    assert after.transport is TransportState.Streaming
    assert after.stream == before.stream
    assert after.display_handle == before.display_handle
    assert "transport.stop_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_retry_reports_unsupported_operation_without_tearing_down_video():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    assert before.audio is AudioState.Enabled
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    rig.transport.start_audio = None
    rig.journal.clear()

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Unavailable
    assert after.last_error.code is ErrorCode.unsupported_operation
    assert after.transport is TransportState.Streaming
    assert after.stream == before.stream
    assert "transport.stop_stream" not in rig.journal.names()
    assert "capture.release" not in rig.journal.names()


# ------------------------------------------------------------- stale audio results


@pytest.mark.asyncio
async def test_stale_audio_retry_completion_cannot_overwrite_a_newer_retry():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)

    first = Deferred()
    rig.transport.queue_result("start_audio", first)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    first_state = rig.manager.snapshot(destination.receiver_id)
    assert first_state.audio is AudioState.Starting
    first_id = first_state.audio_operation_id

    # The first retry fails before it completes; a second retry supersedes it.
    rig.transport.emit(audio_event(
        rig, TransportEventKind.audio_failed, audio_id=first_id,
    ))
    await settle(rig)
    second = Deferred()
    rig.transport.queue_result("start_audio", second)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    second_state = rig.manager.snapshot(destination.receiver_id)
    second_id = second_state.audio_operation_id
    assert second_state.audio is AudioState.Starting
    assert second_id != first_id

    first.resolve(Ok(value=None))
    await settle(rig)
    still = rig.manager.snapshot(destination.receiver_id)
    assert still.audio is AudioState.Starting
    assert still.audio_operation_id == second_id
    assert still.audio is not AudioState.Enabled

    second.resolve(Ok(value=None))
    await settle(rig)
    final = rig.manager.snapshot(destination.receiver_id)
    assert final.audio is AudioState.Enabled
    assert final.audio_operation_id == second_id
    assert final.stream == first_state.stream


@pytest.mark.asyncio
async def test_stale_audio_result_after_stop_cannot_revive_audio():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    held = Deferred()
    rig.transport.queue_result("start_audio", held)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))

    await dispatch(rig, Stop(receiver_id=destination.receiver_id))
    stopped = rig.manager.snapshot(destination.receiver_id)
    assert stopped.stream is None
    assert stopped.transport is TransportState.Idle
    assert stopped.audio is AudioState.Disabled
    assert stopped.audio_operation_id is None
    settled_activity = len(rig.activity.events)

    held.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Disabled
    assert after.transport is TransportState.Idle
    assert after.stream is None
    assert after.audio_operation_id is None
    assert len(rig.activity.events) == settled_activity


@pytest.mark.asyncio
async def test_audio_recovered_event_requires_the_current_stream_and_operation():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    failed = rig.manager.snapshot(destination.receiver_id)
    assert failed.audio is AudioState.Unavailable

    stale_id = OperationId(UUID(int=999))
    rig.transport.emit(audio_event(
        rig, TransportEventKind.audio_recovered, audio_id=stale_id,
    ))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Unavailable

    rig.transport.emit(audio_event(rig, TransportEventKind.audio_recovered))
    await settle(rig)
    recovered = rig.manager.snapshot(destination.receiver_id)
    assert recovered.audio is AudioState.Enabled
    assert recovered.stream == failed.stream
    assert "audio.recovered" in activity_codes(rig)


# --------------------------------------------------------------------- mute


@pytest.mark.asyncio
async def test_only_a_confirmed_mute_changes_the_confirmed_state():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    assert before.audio is AudioState.Enabled and before.muted is False
    held = Deferred()
    rig.transport.queue_result("set_muted", held)

    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.audio is AudioState.Enabled
    assert pending.muted is False
    assert pending.audio_operation_id == before.audio_operation_id

    held.resolve(Ok(value=None))
    await settle(rig)
    confirmed = rig.manager.snapshot(destination.receiver_id)
    assert confirmed.audio is AudioState.Muted
    assert confirmed.muted is True
    assert confirmed.audio_operation_id == before.audio_operation_id
    assert "audio.muted" in activity_codes(rig)


@pytest.mark.asyncio
async def test_failed_mute_preserves_the_last_confirmed_state():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    rig.transport.queue_result(
        "set_muted", Err(error=diagnostic(ErrorCode.audio_mute_failed)),
    )

    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    failed = rig.manager.snapshot(destination.receiver_id)
    assert failed.audio is AudioState.Enabled
    assert failed.muted is False
    assert failed.audio_operation_id == before.audio_operation_id
    assert failed.last_error.code is ErrorCode.audio_mute_failed

    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    muted = rig.manager.snapshot(destination.receiver_id)
    assert muted.audio is AudioState.Muted and muted.muted is True

    rig.transport.queue_result(
        "set_muted", Err(error=diagnostic(ErrorCode.audio_mute_failed)),
    )
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    still_muted = rig.manager.snapshot(destination.receiver_id)
    assert still_muted.audio is AudioState.Muted
    assert still_muted.muted is True
    assert still_muted.last_error.code is ErrorCode.audio_mute_failed


@pytest.mark.asyncio
async def test_failed_mute_result_event_preserves_the_last_confirmed_state():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    held = Deferred()
    rig.transport.queue_result("set_muted", held)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))

    context = rig.manager.context(destination.receiver_id)
    pending = audio_runtime(context).pending_mute
    assert pending is not None
    mute_operation, requested = pending
    assert requested is True
    rig.transport.emit(audio_event(
        rig, TransportEventKind.mute_result,
        operation_id=mute_operation,
        payload=MutePayload(muted=True, ok=False),
    ))
    await settle(rig)
    failed = rig.manager.snapshot(destination.receiver_id)
    assert failed.audio is AudioState.Enabled
    assert failed.muted is False
    assert failed.audio_operation_id == before.audio_operation_id
    assert failed.last_error.code is ErrorCode.audio_mute_failed

    # A late success for the superseded operation cannot flip the state.
    held.resolve(Ok(value=None))
    await settle(rig)
    late = rig.manager.snapshot(destination.receiver_id)
    assert late.audio is AudioState.Enabled
    assert late.muted is False


@pytest.mark.asyncio
async def test_mute_result_event_confirms_the_pending_request():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    held = Deferred()
    rig.transport.queue_result("set_muted", held)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))

    context = rig.manager.context(destination.receiver_id)
    pending = audio_runtime(context).pending_mute
    assert pending is not None
    rig.transport.emit(audio_event(
        rig, TransportEventKind.mute_result, operation_id=pending[0],
        payload=MutePayload(muted=True, ok=True),
    ))
    await settle(rig)
    confirmed = rig.manager.snapshot(destination.receiver_id)
    assert confirmed.audio is AudioState.Muted
    assert confirmed.muted is True
    assert confirmed.audio_operation_id == before.audio_operation_id

    # The duplicate command result cannot change the confirmed value.
    held.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Muted
    assert after.muted is True


@pytest.mark.asyncio
async def test_mute_confirmation_after_audio_loss_does_not_resurrect_audio():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    held = Deferred()
    rig.transport.queue_result("set_muted", held)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))

    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Unavailable

    held.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Unavailable
    assert after.muted is False
    assert after.audio_operation_id == before.audio_operation_id
    assert after.transport is TransportState.Streaming
    assert after.stream == before.stream


@pytest.mark.asyncio
async def test_stale_mute_confirmation_cannot_apply_to_a_newer_request():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    confirmed = rig.manager.snapshot(destination.receiver_id)
    assert confirmed.audio is AudioState.Muted

    older, newer = Deferred(), Deferred()
    rig.transport.queue_result("set_muted", older)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    rig.transport.queue_result("set_muted", newer)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))

    # The older request resolved first. It may not stand in for the newer
    # request, whose unmute is still unconfirmed, so the last confirmed mute
    # must survive until that request is confirmed.
    older.resolve(Ok(value=None))
    await settle(rig)
    stale = rig.manager.snapshot(destination.receiver_id)
    assert stale.audio is AudioState.Muted
    assert stale.muted is True
    assert stale.last_error is None

    newer.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Enabled
    assert rig.manager.snapshot(destination.receiver_id).muted is False


@pytest.mark.asyncio
async def test_retry_restores_the_desired_muted_state():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    lost = rig.manager.snapshot(destination.receiver_id)
    assert lost.audio is AudioState.Unavailable
    assert lost.muted is True

    rig.transport.queue_result("start_audio", Ok(value=None))
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Muted
    assert after.muted is True
    assert after.stream == lost.stream


@pytest.mark.asyncio
async def test_a_replacement_stream_is_not_reported_as_muted():
    rig = mirror_rig(compose=reconnecting_audio_handlers)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.settle()
    before = await start_mirror(rig, destination)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Muted

    rig.transport.emit(audio_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    reconnecting = rig.manager.snapshot(destination.receiver_id)
    assert reconnecting.next_retry_at is not None
    rig.scheduler.advance(reconnecting.next_retry_at - rig.clock.now())
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.transport is TransportState.Streaming
    assert after.stream is not None and after.stream != before.stream
    assert after.stream.lease_id != before.stream.lease_id
    # The replacement stream never received the mute command, so only a
    # confirmed ``Enabled`` may be reported and the stale mute is retired.
    assert after.audio is AudioState.Enabled
    assert after.muted is False
    assert len(rig.journal.calls("transport.set_muted")) == 1


@pytest.mark.asyncio
async def test_mute_and_retry_require_current_discovery_authority():
    rig, destination = await online_mirror_rig()
    before = await start_mirror(rig, destination)
    view = rig.discovery.current(destination.receiver_id)
    assert view is not None
    rig.discovery.views[destination.receiver_id] = replace(view, online=False)

    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    assert "transport.set_muted" not in rig.journal.names()
    stale_mute = rig.manager.snapshot(destination.receiver_id)
    assert stale_mute.last_error.code is ErrorCode.stale_target
    assert stale_mute.audio is AudioState.Enabled

    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Unavailable
    assert after.last_error.code is ErrorCode.stale_target
    assert after.stream == before.stream
    assert after.transport is TransportState.Streaming


@pytest.mark.asyncio
async def test_explicit_video_only_supersedes_an_inflight_audio_retry():
    rig, destination = await online_mirror_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
    await start_mirror(rig, destination)
    probe = Deferred()
    rig.audio.queue_result("check", probe)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Starting

    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    probe.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio_policy.enabled is False
    assert after.audio is AudioState.Disabled
    assert after.phase is SessionPhase.Mirroring
    assert len(rig.journal.calls("transport.start_stream")) == 1
    assert "transport.start_audio" not in rig.journal.names()


@pytest.mark.asyncio
async def test_mute_is_receiver_local_and_never_persisted():
    rig = mirror_rig()
    first, second = target("a"), target("b", address="192.0.2.11")
    rig.discovery.add_target(first)
    rig.discovery.add_target(second)
    rig.capture.add_physical(physical())
    await rig.settle()
    await start_mirror(rig, first)
    await start_mirror(rig, second)
    rig.journal.clear()

    await dispatch(rig, SetMuted(receiver_id=first.receiver_id, muted=True))
    muted = rig.manager.snapshot(first.receiver_id)
    other = rig.manager.snapshot(second.receiver_id)

    assert muted.audio is AudioState.Muted and muted.muted is True
    assert other.audio is AudioState.Enabled and other.muted is False
    calls = rig.journal.calls("transport.set_muted")
    assert len(calls) == 1
    assert journal_handle(calls[0]["handle"])["receiver_id"] == first.receiver_id
    assert "receiver_store.put" not in rig.journal.names()
    assert rig.receivers.receivers == {}

    # One receiver's runtime audio loss cannot change the other session.
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed, destination=first))
    await settle(rig)
    failed = rig.manager.snapshot(first.receiver_id)
    untouched = rig.manager.snapshot(second.receiver_id)
    assert failed.audio is AudioState.Unavailable
    assert failed.transport is TransportState.Streaming
    assert untouched.audio is AudioState.Enabled
    assert untouched.muted is False
    assert untouched.stream == other.stream
    assert untouched.transport is TransportState.Streaming


@pytest.mark.asyncio
async def test_mute_does_not_survive_a_fresh_start():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Muted

    await dispatch(rig, Stop(receiver_id=destination.receiver_id))
    connecting = Deferred()
    rig.transport.queue_result("start_stream", connecting)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await settle(rig)
    pending = rig.manager.snapshot(destination.receiver_id)
    # The fresh intent stops reporting a mute before the new stream is even
    # confirmed; no state ever claims the new receiver stream is muted.
    assert pending.transport is TransportState.Connecting
    assert pending.muted is False
    assert pending.audio is not AudioState.Muted

    connecting.resolve(Ok(value=StreamHandle(
        receiver_id=destination.receiver_id,
        endpoint_generation=destination.endpoint_generation,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        backend_stream_id="gated-fresh-start",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=pending.transport_lease_id,
        start_operation_id=pending.operation_id,
    )))
    await settle(rig)
    restarted = rig.manager.snapshot(destination.receiver_id)
    assert restarted.muted is False
    assert restarted.audio is AudioState.Enabled
    assert restarted.audio_policy.enabled is True
    assert len(rig.journal.calls("transport.set_muted")) == 1
    assert "receiver_store.put" not in rig.journal.names()


# ------------------------------------------------- pre-ready partial stream (Virtual)


@pytest.mark.asyncio
async def test_pre_ready_backend_audio_failure_holds_decision_with_partial_stream():
    rig = virtual_rig(ready=False)
    destination = target()
    before = await start_virtual(rig, destination)
    assert before.stream is not None
    assert before.transport is TransportState.Connecting
    assert before.audio is AudioState.Starting
    handle = before.stream
    assert handle is not None

    rig.transport.emit(audio_event(
        rig, TransportEventKind.audio_start_failed,
        payload=FailurePayload(diagnostic=diagnostic(ErrorCode.audio_denied)),
    ))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio is AudioState.Unavailable
    assert after.intent_stage is IntentStage.audio_decision
    assert after.phase is SessionPhase.AudioDecisionRequired
    assert after.last_error.code is ErrorCode.audio_denied
    # The partial stream is retained for an explicit release; nothing is torn
    # down implicitly and video is not promoted to a ready display.
    assert after.stream == handle
    assert after.transport is TransportState.Connecting
    assert after.display is DisplayState.Owned
    assert "transport.stop_stream" not in rig.journal.names()
    assert "display.remove" not in rig.journal.names()


@pytest.mark.asyncio
async def test_ready_while_audio_decides_does_not_promote_the_session():
    rig = virtual_rig(ready=False)
    destination = target()
    await start_virtual(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.AudioDecisionRequired
    rig.journal.clear()

    rig.transport.emit(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    deciding = rig.manager.snapshot(destination.receiver_id)
    assert deciding.transport is TransportState.Streaming
    assert deciding.phase is SessionPhase.AudioDecisionRequired
    assert deciding.audio is AudioState.Unavailable

    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Enabled
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle == deciding.display_handle
    starts = rig.journal.calls("transport.start_audio")
    assert journal_handle(starts[-1]["handle"])["lease_id"] == deciding.stream.lease_id
    assert "transport.start_stream" not in rig.journal.names()
    assert "transport.stop_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_start_video_only_after_ready_keeps_the_established_video():
    rig = virtual_rig(ready=False)
    destination = target()
    before = await start_virtual(rig, destination)
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    ready = rig.manager.snapshot(destination.receiver_id)
    assert ready.intent_stage is IntentStage.audio_decision
    assert ready.transport is TransportState.Streaming
    rig.journal.clear()

    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio_policy.enabled is False
    assert after.audio is AudioState.Disabled
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.transport is TransportState.Streaming
    assert after.stream == before.stream
    assert after.display_handle == before.display_handle
    assert "transport.stop_stream" not in rig.journal.names()
    assert "transport.start_stream" not in rig.journal.names()
    assert "transport.start_audio" not in rig.journal.names()


@pytest.mark.asyncio
async def test_failed_partial_stream_retry_keeps_the_decision_and_drops_late_recovery():
    rig = virtual_rig(ready=False)
    destination = target()
    before = await start_virtual(rig, destination)
    partial = before.stream
    assert partial is not None
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_start_failed))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.AudioDecisionRequired

    rig.transport.queue_result(
        "start_audio", Err(error=diagnostic(ErrorCode.audio_denied)),
    )
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    starts = rig.journal.calls("transport.start_audio")
    retry_audio_id = journal_handle(starts[-1]["context"])["audio_operation_id"]
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.phase is SessionPhase.AudioDecisionRequired
    assert after.audio is AudioState.Unavailable
    assert after.last_error.code is ErrorCode.audio_denied
    assert after.stream == partial
    assert "transport.stop_stream" not in rig.journal.names()

    # Video becoming ready does not promote, and a recovery notice for the
    # failed retry cannot clear the explicit decision behind the user's back.
    rig.transport.emit(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).intent_stage is IntentStage.audio_decision
    rig.transport.emit(audio_event(
        rig, TransportEventKind.audio_recovered, audio_id=retry_audio_id,
    ))
    await settle(rig)
    still = rig.manager.snapshot(destination.receiver_id)
    assert still.intent_stage is IntentStage.audio_decision
    assert still.audio is AudioState.Unavailable
    assert still.phase is SessionPhase.AudioDecisionRequired
    assert still.stream == partial


@pytest.mark.asyncio
async def test_start_video_only_releases_the_unready_partial_stream_before_a_new_lease():
    rig = virtual_rig(ready=False)
    destination = target()
    before = await start_virtual(rig, destination)
    partial = before.stream
    assert partial is not None
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_start_failed))
    await settle(rig)
    rig.journal.clear()

    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    assert after.audio_policy.enabled is False
    assert after.audio is AudioState.Disabled
    assert after.session_generation == before.session_generation
    assert after.display_handle == before.display_handle
    assert after.display is DisplayState.Owned
    names = rig.journal.names()
    assert "transport.stop_stream" in names
    assert names.index("transport.stop_stream") < names.index("transport.start_stream")
    assert partial.lease_id not in rig.transport.streams
    assert after.stream is not None
    assert after.stream != partial
    assert after.stream.lease_id != partial.lease_id
    assert "transport.start_audio" not in names
    assert "display.remove" not in names


@pytest.mark.asyncio
async def test_stop_runs_audio_control_after_transport_release_and_before_display_removal():
    calls = []

    async def stop_control(stream, operation):
        rig.journal.record("audio.stop_control", handle=stream, context=operation)
        calls.append("audio.stop_control")
        return Ok(value=None)

    rig = virtual_rig(ready=True)
    rig.audio.stop_control = stop_control
    destination = target()
    before = await start_virtual(rig, destination)
    assert before.audio is AudioState.Enabled
    rig.journal.clear()

    await dispatch(rig, Stop(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)

    names = rig.journal.names()
    assert before.audio_operation_id is not None
    assert after.audio is AudioState.Disabled
    assert after.audio_operation_id is None
    assert after.stream is None
    assert after.display_handle is None
    assert names.index("transport.stop_stream") < names.index("audio.stop_control")
    assert names.index("audio.stop_control") < names.index("display.remove")
    assert calls == ["audio.stop_control"]
    assert "display.remove" in names
