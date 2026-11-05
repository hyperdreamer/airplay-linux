"""Audio lifecycle: explicit decisions, preserved video, and session-local mute.

Every external boundary is a deterministic fake.  The handler tests deliver real
typed audio events and assert the recorded adapter calls, so the direct
snapshot projection assertion below is a supplement rather than the only
evidence.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel,
    ChooseResolution,
    EmptyPayload,
    FailurePayload,
    MutePayload,
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
    InstanceId,
    OperationContext,
    OperationId,
    StreamHandle,
)
from airplay_linux.domain.records import AudioStartReceipt, SessionSnapshot
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
    ReceiverCapabilities,
    SessionPhase,
    StartRequest,
    TransportState,
    TrustState,
    VolumeLevel,
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


def mirror_rig(*, transport=None, compose=audio_handlers, volume=False):
    rig = SessionRig()
    for subscription in rig.manager._subscriptions:
        subscription.close()
    rig.transport = transport if transport is not None else AudioTransport()
    report = rig.deps.capabilities()
    rig.deps = replace(rig.deps, transport=rig.transport,
                       capabilities=lambda: replace(report, volume=volume))
    for adapter in (rig.capture, rig.transport, rig.audio, rig.receivers, rig.activity):
        adapter.journal = rig.journal
    rig.manager = SessionManager(rig.deps, compose(rig.deps))
    return rig


def virtual_rig(*, ready: bool, volume=False):
    rig = make_rig(transport=ContextTransport(ready=ready))
    report = rig.deps.capabilities()
    rig.deps = replace(rig.deps, capabilities=lambda: replace(report, volume=volume))
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


async def start_mirror(rig, destination, *, audio=None, volume: VolumeLevel | None = None):
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(
            mode=Mode.MIRROR, source=physical(),
            audio=audio if audio is not None else AudioPolicy(), volume=volume,
        ),
    ))
    await settle(rig)
    return rig.manager.snapshot(destination.receiver_id)


async def start_virtual(rig, destination, *, audio=True, volume: VolumeLevel | None = None):
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(
            mode=Mode.VIRTUAL, resolution=decision(destination),
            audio=AudioPolicy(enabled=audio), volume=volume,
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


async def capable_session(*, level=30, receipt=None, mode=Mode.MIRROR, transport=None):
    rig = (mirror_rig(volume=True, transport=transport) if mode is Mode.MIRROR
           else virtual_rig(ready=True, volume=True))
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    if receipt is None:
        receipt = AudioStartReceipt(audio=True, volume_state="applied", level=level)
    rig.transport.queue_result("start_audio", Ok(value=receipt))
    if mode is Mode.MIRROR:
        state = await start_mirror(rig, destination, volume=VolumeLevel(value=level))
    else:
        state = await start_virtual(rig, destination, volume=VolumeLevel(value=level))
    return rig, destination, state


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [0, 33])
async def test_volume_refused_retry_reissues_original_level_on_same_stream(level):
    rig, destination, before = await capable_session(
        level=level, receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    assert before.audio is AudioState.Unavailable
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    calls = rig.journal.calls("transport.set_volume")
    assert len(calls) == 1 and calls[0]["level"] == {"value": level}
    assert after.stream == before.stream and after.transport_lease_id == before.transport_lease_id
    assert after.effective_volume == level
    assert after.audio is (AudioState.Muted if level == 0 else AudioState.Enabled)
    assert len(rig.journal.calls("transport.start_audio")) == 1
    assert not rig.journal.calls("receiver_store.put")


@pytest.mark.asyncio
@pytest.mark.parametrize("volume_state", ["pending", None])
async def test_volume_pending_retry_resamples_one_receipt_without_timer(volume_state):
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state=volume_state),
    )
    timers = rig.scheduler.pending()
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=True, volume_state="applied", level=30,
    )))
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Enabled and after.effective_volume == 30
    assert len(rig.journal.calls("transport.start_audio")) == 2
    assert not rig.journal.calls("transport.set_volume")
    assert after.stream == before.stream and rig.scheduler.pending() == timers


@pytest.mark.asyncio
async def test_volume_drag_public_timeout_does_not_release_worker_or_publish_late_ack():
    timeout = asyncio.Event()
    tasks = []
    class PublicTimeout(AudioTransport):
        async def set_volume(self, handle, level, operation):
            if tasks:
                return await super().set_volume(handle, level, operation)
            task = asyncio.create_task(super().set_volume(handle, level, operation))
            tasks.append(task)
            await timeout.wait()
            self._volume_tasks[handle.lease_id].expired = True
            return Err(error=diagnostic(ErrorCode.audio_volume_failed))
    transport = PublicTimeout()
    rig, destination, before = await capable_session(transport=transport)
    first, second = Deferred(), Deferred()
    transport.queue_result("set_volume", first)
    transport.queue_result("set_volume", second)
    for level in (40, 50, 60):
        await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=level)))
    timeout.set()
    await settle(rig)
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.effective_volume == 30 and pending.remembered_volume == 30
    assert len(rig.journal.calls("transport.set_volume")) == 1
    assert audio_runtime(rig.manager.context(destination.receiver_id)).volume_inflight is not None
    assert activity_codes(rig).count("audio.volume_failed") == 1
    first.resolve(Ok(value=40))
    await tasks[0]
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).effective_volume == 30
    assert len(rig.journal.calls("transport.set_volume")) == 2
    assert rig.journal.calls("transport.set_volume")[-1]["level"] == {"value": 60}
    second.resolve(Ok(value=60))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).effective_volume == 60
    assert rig.manager.snapshot(destination.receiver_id).remembered_volume == 60
    assert activity_codes(rig).count("audio.volume_failed") == 1
    assert [event.fields["level"] for event in rig.activity.events if event.code == "audio.volume_applied"] == [60]
    await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_volume_occupied_refused_retry_does_not_rotate_audio_fence():
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    gate = Deferred()
    rig.transport.queue_result("set_volume", gate)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    pending = rig.manager.snapshot(destination.receiver_id)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    assert rig.manager.snapshot(destination.receiver_id).audio_operation_id == pending.audio_operation_id
    assert len(rig.journal.calls("transport.set_volume")) == 1
    gate.resolve(Err(error=diagnostic(ErrorCode.audio_volume_failed)))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert len(rig.journal.calls("transport.set_volume")) == 2
    assert after.audio_operation_id != pending.audio_operation_id
    assert after.effective_volume == 30 and after.audio is AudioState.Enabled
    assert after.stream == before.stream


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", [
    AudioStartReceipt(audio=True, volume_state="pending", level=0),
    AudioStartReceipt(audio=False, volume_state="refused"),
    AudioStartReceipt(audio=True),
])
async def test_volume_non_enabling_retry_keeps_same_lease_historical_levels(receipt):
    rig, destination, before = await capable_session()
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    rig.transport.queue_result("start_audio", Ok(value=receipt))
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Unavailable and not after.muted
    assert after.effective_volume == 0 and after.last_audible_volume == 30
    assert after.stream == before.stream
    assert after.last_error.code is ErrorCode.volume_unavailable
    assert after.last_error.detail in ("receipt_pending", "startup_refused", "receipt_missing")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["trust_generation", "trust_block", "cleanup", "endpoint", "stop", "loss"])
async def test_volume_stale_settlement_is_silent_and_never_drains_or_saves(change):
    rig, destination, before = await capable_session()
    first = Deferred()
    rig.transport.queue_result("set_volume", first)
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=40)))
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=70)))
    if change in ("trust_generation", "trust_block", "cleanup"):
        old = rig.manager.handlers[Route.audio]
        def controlled(ctx, message):
            if isinstance(message, SetMuted):
                kwargs = ({"trust_generation": ctx.state.trust_generation + 1} if change == "trust_generation"
                          else {"trust": TrustState.CleanupRequired} if change == "trust_block"
                          else {"cleanup_pending": True})
                ctx.apply(**kwargs)
                return True
            return old(ctx, message)
        rig.manager.handlers[Route.audio] = controlled
        await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    elif change == "endpoint":
        new = target(endpoint_generation=2, address="192.0.2.44")
        rig.discovery.views[destination.receiver_id] = rig.discovery.build_view(new, True)
    elif change == "stop":
        await dispatch(rig, Stop(receiver_id=destination.receiver_id))
    else:
        rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
        await settle(rig)
    checkpoint = rig.manager.snapshot(destination.receiver_id)
    activities = list(rig.activity.events)
    first.resolve(Ok(value=40))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id) == checkpoint
    assert rig.activity.events == activities
    assert len(rig.journal.calls("transport.set_volume")) == 1
    assert not rig.journal.calls("receiver_store.put")


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", [False, True])
async def test_volume_close_during_retry_retires_level_and_requires_fresh_close(closed):
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    level, close = Deferred(), Deferred()
    rig.transport.queue_result("set_volume", level)
    rig.transport.queue_result("close_audio", close)
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    await rig.manager.dispatch(StartVideoOnly(receiver_id=destination.receiver_id))
    pending = rig.manager.snapshot(destination.receiver_id)
    assert not pending.audio_policy.enabled and pending.audio_close_pending
    await settle(rig)
    for command in (RetryAudio(receiver_id=destination.receiver_id),
                    SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=70)),
                    SetMuted(receiver_id=destination.receiver_id, muted=False)):
        await dispatch(rig, command)
    level.resolve(Ok(value=30))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Unavailable
    close.resolve(Ok(value=None) if closed else Err(error=diagnostic(ErrorCode.audio_volume_failed)))
    await settle(rig)
    result = rig.manager.snapshot(destination.receiver_id)
    if not closed:
        assert result.audio_close_pending and result.audio is AudioState.Unavailable
        assert result.last_error.detail == "close_unconfirmed"
        await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
        result = rig.manager.snapshot(destination.receiver_id)
    assert result.audio is AudioState.Disabled and not result.audio_close_pending
    assert result.stream == before.stream and result.transport is TransportState.Streaming
    assert result.effective_volume is None
    assert len(rig.journal.calls("transport.set_volume")) == 1
    assert not rig.journal.calls("receiver_store.put")


@pytest.mark.asyncio
async def test_volume_capability_absence_is_reported_once_per_session():
    rig, destination = await online_mirror_rig()
    await start_mirror(rig, destination)
    for level in (30, 40):
        await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=level)))
    events = [event for event in rig.activity.events if event.code == "audio.volume_unavailable"]
    assert len(events) == 1 and events[0].fields["reason"] == "backend_unsupported"
    assert not rig.journal.calls("transport.set_volume")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["trust_generation", "trust_block", "cleanup", "endpoint"])
async def test_volume_receipt_rechecks_full_publication_authority(change):
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    held = Deferred()
    rig.transport.queue_result("start_audio", held)
    await start_mirror(rig, destination, volume=VolumeLevel(value=30))
    old = rig.manager.handlers[Route.audio]
    def controlled(ctx, message):
        if isinstance(message, SetMuted):
            changes = ({"trust_generation": ctx.state.trust_generation + 1} if change == "trust_generation"
                       else {"trust": TrustState.Pairing} if change == "trust_block"
                       else {"cleanup_pending": True})
            ctx.apply(**changes)
            return True
        return old(ctx, message)
    if change == "endpoint":
        replacement = target(endpoint_generation=2, address="192.0.2.55")
        rig.discovery.views[destination.receiver_id] = rig.discovery.build_view(replacement, True)
    else:
        rig.manager.handlers[Route.audio] = controlled
        await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    before = rig.manager.snapshot(destination.receiver_id)
    events = list(rig.activity.events)
    held.resolve(Ok(value=AudioStartReceipt(audio=True, volume_state="applied", level=30)))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id) == before
    assert rig.activity.events == events


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["pairing", "cleanup", "closed", "starting", "decision", "identity"])
async def test_volume_blocked_controls_do_no_mutation_io(change):
    rig, destination, before = await capable_session()
    if change == "identity":
        view = rig.discovery.views[destination.receiver_id]
        rig.discovery.views[destination.receiver_id] = replace(
            view, record_set=replace(view.record_set, replacement_of=destination.receiver_id),
        )
    else:
        original = rig.manager.handlers[Route.audio]
        changes = {"pairing": {"trust": TrustState.Pairing}, "cleanup": {"cleanup_pending": True},
                   "closed": {"audio_policy": AudioPolicy(enabled=False)},
                   "starting": {"audio": AudioState.Starting},
                   "decision": {"intent_stage": IntentStage.audio_decision}}[change]
        def controlled(ctx, message):
            if isinstance(message, RetryAudio):
                ctx.apply(**changes)
                return True
            return original(ctx, message)
        rig.manager.handlers[Route.audio] = controlled
        await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    rig.journal.clear()
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=51)))
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    assert not rig.journal.calls("transport.set_volume")
    assert not rig.journal.calls("transport.set_muted")
    assert not rig.journal.calls("receiver_store.put")


@pytest.mark.asyncio
async def test_volume_delayed_old_save_cannot_overwrite_newer_remembered_publication(monkeypatch):
    rig, destination, before = await capable_session()
    update = rig.deps.preferences.update
    gate = Deferred()
    async def held(record_set, **kwargs):
        result = await update(record_set, **kwargs)
        if kwargs["remembered_volume"].value == 40:
            await gate.wait()
        return result
    monkeypatch.setattr(rig.deps.preferences, "update", held)
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=40)))
    assert rig.manager.snapshot(destination.receiver_id).remembered_volume == 30
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=70)))
    assert rig.manager.snapshot(destination.receiver_id).remembered_volume == 70
    assert rig.receivers.receivers[destination.receiver_id].remembered_volume == 70
    events = list(rig.activity.events)
    gate.resolve(None)
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).remembered_volume == 70
    assert rig.activity.events == events


@pytest.mark.asyncio
async def test_volume_close_pending_ignores_audio_loss_until_its_own_ack():
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    close = Deferred()
    rig.transport.queue_result("close_audio", close)
    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    close.resolve(Ok(value=None))
    await settle(rig)
    result = rig.manager.snapshot(destination.receiver_id)
    assert result.audio is AudioState.Disabled and not result.audio_close_pending


@pytest.mark.asyncio
async def test_volume_close_timeout_late_ack_cannot_confirm_without_fresh_request():
    expire = asyncio.Event()
    tasks = []
    class CloseTimeout(AudioTransport):
        async def close_audio(self, handle, operation):
            if tasks:
                return await super().close_audio(handle, operation)
            task = asyncio.create_task(super().close_audio(handle, operation))
            tasks.append(task)
            await expire.wait()
            return Err(error=diagnostic(ErrorCode.audio_volume_failed))
    rig, destination, before = await capable_session(
        transport=CloseTimeout(), receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    gate = Deferred()
    rig.transport.queue_result("close_audio", gate)
    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    expire.set()
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).audio_close_pending
    events = list(rig.activity.events)
    gate.resolve(Ok(value=None))
    await tasks[0]
    await settle(rig)
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.audio is AudioState.Unavailable and state.audio_close_pending
    assert rig.activity.events == events
    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Disabled
    assert len(rig.journal.calls("transport.close_audio")) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["pairing", "close_pending"])
async def test_volume_receipt_retry_is_blocked_before_transport_mutation(block):
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state="pending"),
    )
    original = rig.manager.handlers[Route.audio]
    def controlled(ctx, message):
        if isinstance(message, SetMuted):
            ctx.apply(**({"trust": TrustState.Pairing} if block == "pairing"
                         else {"audio_close_pending": True}))
            return True
        return original(ctx, message)
    rig.manager.handlers[Route.audio] = controlled
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    rig.journal.clear()
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    assert not rig.journal.calls("transport.start_audio")
    assert not rig.journal.calls("transport.set_volume")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_volume_pending_close_reconnect_uses_off_policy_on_fresh_lease(mode):
    rig = mirror_rig(volume=True, compose=reconnecting_audio_handlers)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(audio=False, volume_state="refused")))
    if mode is Mode.MIRROR:
        before = await start_mirror(rig, destination, volume=VolumeLevel(value=30))
    else:
        before = await start_virtual(rig, destination, volume=VolumeLevel(value=30))
        rig.transport.emit(stream_event(rig, TransportEventKind.ready))
        await settle(rig)
    close = Deferred()
    rig.transport.queue_result("close_audio", close)
    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.next_retry_at is not None
    rig.scheduler.advance(state.next_retry_at - rig.clock.now())
    await settle(rig)
    call = rig.journal.calls("transport.start_stream")[-1]
    assert call["audio_policy"] == {"enabled": False} and call["initial_volume"] is None
    replacement = rig.manager.snapshot(destination.receiver_id)
    assert replacement.transport_lease_id != before.transport_lease_id
    assert replacement.effective_volume is None and replacement.last_audible_volume is None
    close.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id) == replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("remembered,expected", [(45, 45), (None, 100)])
async def test_volume_unmute_without_last_audible_uses_remembered_else_default(remembered, expected):
    rig, destination, before = await capable_session(level=0)
    original = rig.manager.handlers[Route.audio]
    def controlled(ctx, message):
        if isinstance(message, RetryAudio):
            ctx.apply(remembered_volume=remembered)
            return True
        return original(ctx, message)
    rig.manager.handlers[Route.audio] = controlled
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    assert rig.manager.snapshot(destination.receiver_id).audio is AudioState.Muted
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.effective_volume == expected and state.last_audible_volume == expected
    assert state.remembered_volume == remembered
    assert not rig.journal.calls("receiver_store.put")


@pytest.mark.asyncio
async def test_volume_positive_slider_save_is_independent_of_consumed_worker(monkeypatch):
    rig, destination, before = await capable_session()
    writer = rig.deps.preferences
    update = writer.update
    entered, gate = [], Deferred()
    async def held(record_set, **kwargs):
        entered.append(kwargs)
        await gate.wait()
        return await update(record_set, **kwargs)
    monkeypatch.setattr(writer, "update", held)
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=66)))
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.effective_volume == 66 and pending.last_audible_volume == 66
    assert pending.remembered_volume == 30
    assert len(entered) == 1 and entered[0]["still_current"]()
    assert audio_runtime(rig.manager.context(destination.receiver_id)).volume_inflight is None
    gate.resolve(None)
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.remembered_volume == 66 and not after.volume_save_failed
    assert rig.receivers.receivers[destination.receiver_id].remembered_volume == 66


@pytest.mark.asyncio
async def test_volume_save_failure_keeps_applied_level_and_recovers_on_next_save():
    rig, destination, before = await capable_session()
    rig.receivers.queue_result("put", Err(error=diagnostic(ErrorCode.persistence_failed)))
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=66)))
    failed = rig.manager.snapshot(destination.receiver_id)
    assert failed.effective_volume == 66 and failed.last_audible_volume == 66
    assert failed.remembered_volume == 30 and failed.volume_save_failed
    assert failed.last_error.code is ErrorCode.volume_preference_failed
    assert "audio.volume_failed" not in activity_codes(rig)
    publications = []
    subscription = rig.manager.subscribe(publications.append)
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=66)))
    subscription.close()
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.remembered_volume == 66 and not after.volume_save_failed
    assert after.last_error is None and publications


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_volume_zero_and_speaker_share_one_knob_without_saving_zero(mode):
    rig, destination, before = await capable_session(mode=mode)
    rig.journal.clear()
    await dispatch(rig, SetVolume(receiver_id=destination.receiver_id, level=VolumeLevel(value=0)))
    muted = rig.manager.snapshot(destination.receiver_id)
    assert muted.effective_volume == 0 and muted.last_audible_volume == 30
    assert muted.audio is AudioState.Muted and muted.muted
    assert audio_runtime(rig.manager.context(destination.receiver_id)).muted_lease == muted.transport_lease_id
    assert rig.transport.muted[muted.transport_lease_id]
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=False))
    restored = rig.manager.snapshot(destination.receiver_id)
    assert restored.effective_volume == 30 and restored.audio is AudioState.Enabled
    assert not restored.muted and not rig.transport.muted[restored.transport_lease_id]
    assert restored.remembered_volume == 30
    assert not rig.journal.calls("transport.set_muted") and not rig.journal.calls("receiver_store.put")
    assert [call["level"]["value"] for call in rig.journal.calls("transport.set_volume")] == [0, 30]


@pytest.mark.asyncio
async def test_volume_loss_preserves_history_clears_mute_and_zero_receipt_restores_it():
    rig, destination, before = await capable_session()
    await dispatch(rig, SetMuted(receiver_id=destination.receiver_id, muted=True))
    rig.transport.emit(audio_event(rig, TransportEventKind.audio_failed))
    await settle(rig)
    lost = rig.manager.snapshot(destination.receiver_id)
    assert lost.effective_volume == 0 and lost.last_audible_volume == 30
    assert not lost.muted and lost.audio is AudioState.Unavailable
    assert audio_runtime(rig.manager.context(destination.receiver_id)).muted_lease is None
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=True, volume_state="applied", level=0,
    )))
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    restored = rig.manager.snapshot(destination.receiver_id)
    assert restored.audio is AudioState.Muted and restored.muted
    assert restored.effective_volume == 0 and restored.last_audible_volume == 30


@pytest.mark.asyncio
async def test_volume_refused_decision_ignores_unowned_recovery_and_deduplicates_availability():
    rig, destination, before = await capable_session(
        receipt=AudioStartReceipt(audio=False, volume_state="refused"),
    )
    for _ in range(2):
        rig.transport.emit(audio_event(rig, TransportEventKind.audio_recovered))
        await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.audio is AudioState.Unavailable and after.intent_stage is IntentStage.audio_decision
    assert activity_codes(rig).count("audio.volume_unavailable") == 1
    assert "audio.recovered" not in activity_codes(rig)


@pytest.mark.asyncio
async def test_volume_result_is_not_projected_before_actual_worker_settlement():
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=True, volume_state="applied", level=30,
    )))
    await start_mirror(
        rig, destination, audio=AudioPolicy(enabled=True), volume=VolumeLevel(value=30),
    )
    held = Deferred()
    rig.transport.queue_result("set_volume", held)
    receipt = await rig.manager.dispatch(SetVolume(
        receiver_id=destination.receiver_id, level=VolumeLevel(value=64),
    ))
    await settle(rig)
    assert receipt.accepted
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.effective_volume == 30 and pending.last_audible_volume == 30
    assert pending.audio is AudioState.Enabled and not pending.muted
    held.resolve(Ok(value=64))
    await settle(rig)
    applied = rig.manager.snapshot(destination.receiver_id)
    assert applied.effective_volume == 64 and applied.last_audible_volume == 64
    assert applied.audio is AudioState.Enabled and not applied.muted


@pytest.mark.asyncio
async def test_volume_drag_keeps_one_worker_and_applies_intermediate_then_latest_ack():
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=True, volume_state="applied", level=30,
    )))
    await start_mirror(
        rig, destination, audio=AudioPolicy(enabled=True), volume=VolumeLevel(value=30),
    )
    first, second = Deferred(), Deferred()
    rig.transport.queue_result("set_volume", first)
    rig.transport.queue_result("set_volume", second)
    for level in (41, 52, 73):
        await rig.manager.dispatch(SetVolume(
            receiver_id=destination.receiver_id, level=VolumeLevel(value=level),
        ))
        await settle(rig)
    assert len(rig.journal.calls("transport.set_volume")) == 1
    first.resolve(Ok(value=41))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).effective_volume == 41
    assert len(rig.journal.calls("transport.set_volume")) == 2
    assert rig.journal.calls("transport.set_volume")[-1]["level"] == {"value": 73}
    second.resolve(Ok(value=73))
    await settle(rig)
    result = rig.manager.snapshot(destination.receiver_id)
    assert result.effective_volume == 73 and result.last_audible_volume == 73


@pytest.mark.asyncio
async def test_capable_video_only_waits_for_current_close_acknowledgement():
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=False, volume_state="refused",
    )))
    await start_mirror(
        rig, destination, audio=AudioPolicy(enabled=True), volume=VolumeLevel(value=30),
    )
    held = Deferred()
    rig.transport.queue_result("close_audio", held)
    await dispatch(rig, StartVideoOnly(receiver_id=destination.receiver_id))
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.audio_policy.enabled is False
    assert pending.audio_close_pending is True
    assert pending.audio is AudioState.Unavailable
    assert len(rig.journal.calls("transport.close_audio")) == 1
    held.resolve(Ok(value=None))
    await settle(rig)
    closed = rig.manager.snapshot(destination.receiver_id)
    assert closed.audio is AudioState.Disabled and not closed.muted
    assert not closed.audio_close_pending
    assert closed.transport is TransportState.Streaming and closed.stream is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt,state,expected_error", [
    (AudioStartReceipt(audio=True, volume_state="applied", level=37), AudioState.Enabled, None),
    (AudioStartReceipt(audio=True, volume_state="pending"), AudioState.Unavailable, ErrorCode.volume_unavailable),
    (AudioStartReceipt(audio=True, volume_state="refused"), AudioState.Unavailable, ErrorCode.volume_unavailable),
    (AudioStartReceipt(audio=True), AudioState.Unavailable, ErrorCode.volume_unavailable),
    (AudioStartReceipt(audio=False, volume_state="applied", level=37), AudioState.Unavailable, ErrorCode.audio_unavailable),
])
async def test_capable_startup_receipt_controls_audio_and_volume_state(receipt, state, expected_error):
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=receipt))
    result = await start_mirror(
        rig, destination, audio=AudioPolicy(enabled=True), volume=VolumeLevel(value=37),
    )
    assert result.audio is state
    assert result.effective_volume == (37 if expected_error is None else None)
    assert result.last_audible_volume == (37 if expected_error is None else None)
    if expected_error is None:
        assert result.volume_state == "applied"
        assert result.intent_stage is IntentStage.active
        assert activity_codes(rig).count("audio.recovered") == 1
    else:
        assert result.intent_stage is IntentStage.audio_decision
        assert result.last_error.code is expected_error
        assert "audio.recovered" not in activity_codes(rig)


@pytest.mark.asyncio
async def test_set_volume_command_routes_through_audio_and_applies_only_acknowledged_level():
    rig = mirror_rig(volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.queue_result("start_audio", Ok(value=AudioStartReceipt(
        audio=True, volume_state="applied", level=30,
    )))
    await start_mirror(
        rig, destination, audio=AudioPolicy(enabled=True), volume=VolumeLevel(value=30),
    )
    receipt = await rig.manager.dispatch(SetVolume(
        receiver_id=destination.receiver_id, level=VolumeLevel(value=64),
    ))
    await settle(rig)
    assert receipt.accepted
    assert len(rig.journal.calls("transport.set_volume")) == 1
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.effective_volume == 64
    assert state.last_audible_volume == 64
    assert state.audio is AudioState.Enabled and not state.muted


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
@pytest.mark.parametrize("capable,enabled,value,want", [
    (True, True, 30, {"value": 30}), (True, True, 0, {"value": 0}),
    (True, True, None, None), (True, False, 30, None), (False, True, 30, None),
])
async def test_volume_startup_input_reaches_actual_transport(mode, capable, enabled, value, want):
    rig = mirror_rig(volume=capable) if mode is Mode.MIRROR else virtual_rig(ready=True, volume=capable)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    volume = None if value is None else VolumeLevel(value=value)
    if mode is Mode.MIRROR:
        state = await start_mirror(rig, destination, audio=AudioPolicy(enabled=enabled), volume=volume)
    else:
        state = await start_virtual(rig, destination, audio=enabled, volume=volume)
    calls = rig.journal.calls("transport.start_stream")
    assert len(calls) == 1
    assert calls[0]["initial_volume"] == want
    assert calls[0]["audio_policy"] == {"enabled": enabled}
    assert state.effective_volume is None and state.last_audible_volume is None
    assert state.remembered_volume == (value if value and capable else None)


async def test_volume_initial_value_survives_resolution_choice():
    rig = virtual_rig(ready=True, volume=True)
    destination = target()
    rig.discovery.add_target(destination)
    rig.receivers.queue_result("query_modes", Ok(value=ReceiverCapabilities(
        modes=(decision(destination).size,), reliable=True, evidence="receiver query",
    )))
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.VIRTUAL, volume=VolumeLevel(value=0)),
    ))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.ResolutionRequired
    await rig.manager.dispatch(ChooseResolution(
        receiver_id=destination.receiver_id, decision=decision(destination),
    ))
    await settle(rig)
    calls = rig.journal.calls("transport.start_stream")
    assert len(calls) == 1
    assert calls[0]["initial_volume"] == {"value": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_volume_initial_value_survives_audio_decision_continuation(mode):
    rig = mirror_rig(volume=True) if mode is Mode.MIRROR else virtual_rig(ready=True, volume=True)
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_unavailable)))
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    if mode is Mode.MIRROR:
        await start_mirror(rig, destination, volume=VolumeLevel(value=0))
    else:
        await start_virtual(rig, destination, volume=VolumeLevel(value=0))
    assert not rig.journal.calls("transport.start_stream")
    await dispatch(rig, RetryAudio(receiver_id=destination.receiver_id))
    assert rig.journal.calls("transport.start_stream")[0]["initial_volume"] == {"value": 0}


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
