"""Two-receiver isolation, stale-result fences and bounded assembled shutdown."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.backend.contract import AckReply
from airplay_linux.backend.control import ApplicationContractProfile, BackendOperation, Handshake
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    FailurePayload, MutePayload, RetryAudio, SetMuted, Start, Stop, TransportEventKind,
)
from airplay_linux.domain.operations import InstanceId, LeaseId, OperationId
from airplay_linux.domain.resources import (
    AudioState, DisplayState, Err, ErrorCode, Mode, Ok, SessionPhase, TransportState,
)
from airplay_linux.process.daemon_registry import DaemonNotice
from airplay_linux.testing.adapters import Deferred, FakeCredentialStore
from airplay_linux.testing.builders import target
from airplay_linux.transport.doubletake import DoubleTakeTransport

from test_lifecycle_matrix import (
    app_factory, daemon_record, display_event, event, request,
)

B = target("b", address="192.0.2.11")


class Subscription:
    def __init__(self, callbacks, callback):
        self.callbacks, self.callback = callbacks, callback

    def close(self):
        if self.callback in self.callbacks:
            self.callbacks.remove(self.callback)


class Registry:
    """Process admission boundary; the real transport retains daemon fences."""
    def __init__(self):
        self.record = daemon_record(9)
        self.callbacks = []
        self.restarts = 0
        self.shutdowns = 0

    def subscribe(self, callback):
        self.callbacks.append(callback)
        return Subscription(self.callbacks, callback)

    async def ensure(self):
        return Ok(value=self.record)

    async def restart(self):
        self.restarts += 1
        self.record = daemon_record(9 + self.restarts)
        return Ok(value=self.record)

    async def shutdown(self):
        self.shutdowns += 1
        return Ok(value=None)

    def exit(self, instance):
        for callback in tuple(self.callbacks):
            callback(DaemonNotice(kind="exit", instance_uuid=instance))


class Control:
    """Backend replies and suspension seams; production code owns all routing."""
    def __init__(self, record, journal):
        self.record, self.journal = record, journal
        self.gates = {}
        self.calls = []
        self.connected, self.audio, self.muted = set(), set(), {}
        self.closed = False

    def subscribe(self, callback):
        return Subscription([], callback)

    async def handshake(self, owner_token, instance_uuid):
        return Ok(value=Handshake(instance_uuid=instance_uuid, owner_token=owner_token,
                                  daemon=self.record.daemon, protocol_version=self.record.protocol_version))

    async def request(self, operation, destination, payload, context, *, dispatch_fence=None):
        if dispatch_fence is not None:
            checked = dispatch_fence()
            if isinstance(checked, Err):
                return checked
        self.calls.append((operation, destination, context))
        self.journal.record("backend.request", operation=operation, target=destination, context=context)
        gate = self.gates.pop((operation, destination.receiver_id), None)
        if gate is not None:
            result = await gate.wait()
            if result is not None:
                return result
        if operation is BackendOperation.connect:
            self.connected.add(destination.receiver_id)
        if operation is BackendOperation.disconnect:
            self.connected.discard(destination.receiver_id)
            self.audio.discard(destination.receiver_id)
        if operation is BackendOperation.start_audio:
            self.audio.add(destination.receiver_id)
        muted = operation is BackendOperation.mute
        if operation in (BackendOperation.mute, BackendOperation.unmute):
            self.muted[destination.receiver_id] = muted
        return Ok(value=AckReply(operation=operation.value, target=destination.canonical_host,
                                 state="streaming" if operation is BackendOperation.connect else None,
                                 muted=muted if operation in (BackendOperation.mute, BackendOperation.unmute) else None))

    async def close(self):
        self.closed = True


def real_transport(holder):
    def configure(rig):
        registry, controls = Registry(), {}
        def factory(record):
            return controls.setdefault(record.instance_uuid, Control(record, rig.journal))
        features = ApplicationContractProfile(socket_path="/fake/private/socket").features
        transport = DoubleTakeTransport(factory, registry, rig.capture, FakeCredentialStore(),
                                         rig.receivers, rig.fences, features, rig.clock, rig.scheduler, rig.ids)
        rig.transport = transport
        rig.deps = replace(rig.deps, transport=transport, restart_daemon=registry.restart)
        holder.update(registry=registry, controls=controls)
    return configure


@pytest.mark.parametrize("mode_a", [Mode.MIRROR, Mode.VIRTUAL], ids=["A-mirror-B-virtual", "A-virtual-B-virtual"])
async def test_two_receivers_keep_mode_resources_audio_and_failure_independent(app_factory, mode_a):
    holder = {}
    app = await app_factory(configure=real_transport(holder))
    a = await app.start(mode_a, audio=True)
    b = await app.start(Mode.VIRTUAL, destination=B, audio=True)
    assert a.transport is b.transport is TransportState.Streaming
    assert a.audio is b.audio is AudioState.Enabled
    assert a.transport_lease_id != b.transport_lease_id
    assert a.physical_source is not None if mode_a is Mode.MIRROR else a.physical_source is None
    assert a.display_handle is None if mode_a is Mode.MIRROR else a.display_handle is not None
    assert b.physical_source is None and b.display_handle is not None
    if mode_a is Mode.VIRTUAL:
        assert a.display_handle.owner_uuid != b.display_handle.owner_uuid
        assert a.display_handle.helper != b.display_handle.helper
        assert a.display_handle.source != b.display_handle.source
        assert app.display.max_concurrent_creations == 1
    # One receiver's mute uses the real transport's exact daemon/lease admission.
    await app.send(SetMuted(receiver_id=a.receiver_id, muted=True))
    assert app.state().audio is AudioState.Muted and app.state().muted
    assert app.state(B) == b
    control = holder["controls"][a.stream.daemon_instance_uuid]
    mute, = [call for call in control.calls if call[0] is BackendOperation.mute]
    assert mute[1].receiver_id == a.receiver_id and mute[2].transport_lease_id == a.transport_lease_id
    app.transport._emit(app.transport._streams[b.transport_lease_id], TransportEventKind.audio_failed,
                        FailurePayload(diagnostic=diagnostic(ErrorCode.audio_unavailable)),
                        context=event(app, TransportEventKind.audio_failed, B).context)
    await app.settle()
    assert app.state(B).audio is AudioState.Unavailable
    assert app.state().audio is AudioState.Muted and app.state().stream == a.stream
    await app.send(RetryAudio(receiver_id=b.receiver_id))
    assert app.state(B).audio is AudioState.Enabled and app.state(B).stream == b.stream
    assert app.state().audio is AudioState.Muted
    app.transport._emit(app.transport._streams[b.transport_lease_id], TransportEventKind.network_lost)
    await app.settle()
    assert app.state(B).transport is TransportState.Reconnecting
    assert app.state(B).display_handle == b.display_handle
    assert app.state().transport is TransportState.Streaming and app.state().stream == a.stream
    await app.send(Stop(receiver_id=b.receiver_id))
    assert app.state(B).display is DisplayState.None_ and app.state(B).stream is None
    assert app.state().stream == a.stream
    assert a.receiver_id in control.connected and b.receiver_id not in control.connected
    assert len(app.display.outputs) == (mode_a is Mode.VIRTUAL)


async def test_provider_serializes_simultaneous_creations_and_cancel_is_receiver_local(app_factory, tmp_path):
    from test_crash_recovery import World

    world = World(tmp_path)
    app = await app_factory(configure=world.configure, directory=tmp_path)
    await app.online()
    await app.online(B)
    spawned = Deferred()
    world.process.queue_result("spawn", spawned)
    try:
        await app.send(Start(receiver_id=target().receiver_id, request=request()))
        assert app.state().display is DisplayState.Creating
        await app.send(Start(receiver_id=B.receiver_id, request=request(destination=B)))
        assert app.state(B).display is DisplayState.Creating
        # The production CreationLock must hold B before its helper launch.
        assert len(app.journal.calls("process.spawn")) == 1
        await app.send(Stop(receiver_id=target().receiver_id))
        from airplay_linux.domain.operations import ProcessIdentity
        late_a = ProcessIdentity(pid=6000, start_ticks=7000)
        spawned.resolve(Ok(value=late_a))
        await app.settle()
        assert app.state().stream is None and not app.state().intent_active
        assert app.state(B).transport is TransportState.Streaming
        assert len(app.journal.calls("process.spawn")) == 2
        assert len(world.process.entries) == 2 and len(app.transport.streams) == 1
        record, = await world.records()
        assert record.receiver_id == B.receiver_id and record.owner_uuid == app.state(B).ownership_uuid
        assert record.helper != late_a and world.process.alive(record.helper)
        assert world.process.terminated == [late_a]
    finally:
        if not spawned.resolved:
            from airplay_linux.domain.operations import ProcessIdentity
            spawned.resolve(Ok(value=ProcessIdentity(pid=6000, start_ticks=7000)))
        await app.settle()


@pytest.mark.parametrize("stale", ["session", "lease", "endpoint_generation", "endpoint_fingerprint",
                                   "daemon", "audio", "display"])
async def test_old_resource_events_cannot_revive_or_destroy_new_session(app_factory, stale):
    app = await app_factory()
    old = await app.start(audio=True)
    old_event = event(app, TransportEventKind.network_lost)
    old_display = display_event(old, "helper_exit")
    await app.send(Stop(receiver_id=old.receiver_id))
    current = await app.start(audio=True)
    app.journal.clear()
    notice = event(app, TransportEventKind.network_lost)
    if stale == "session":
        notice = replace(notice, context=replace(notice.context, session_generation=old.session_generation))
    elif stale == "lease":
        notice = replace(notice, context=replace(notice.context, transport_lease_id=old.transport_lease_id))
    elif stale == "endpoint_generation":
        notice = replace(notice, endpoint_generation=0)
    elif stale == "endpoint_fingerprint":
        notice = replace(notice, endpoint_fingerprint="replaced-endpoint")
    elif stale == "daemon":
        notice = replace(notice, daemon_instance_uuid=InstanceId(UUID(int=999)))
    elif stale == "audio":
        notice = replace(event(app, TransportEventKind.audio_failed),
                         context=replace(event(app, TransportEventKind.audio_failed).context,
                                         audio_operation_id=old.audio_operation_id))
    if stale == "display":
        app.display.emit(old_display)
    else:
        app.transport.emit(notice)
    app.transport.emit(old_event)
    await app.settle()
    assert app.state() == current
    assert len(app.display.outputs) == len(app.transport.streams) == 1
    assert not app.journal.calls("transport.stop_stream") and not app.journal.calls("display.remove")


@pytest.mark.parametrize("envelope", ["genuine-A-stream", "B-stream-under-A-envelope"])
async def test_cross_receiver_transport_delivery_cannot_mutate_either_session(
    app_factory, monkeypatch, envelope,
):
    """Wrong-actor delivery must be fenced independently of stream identity.

    Removing accept_transport's receiver check kills the B-stream-under-A-envelope
    case. The genuine delayed A event also pins the complete cross-subject fence.
    """
    app = await app_factory()
    a = await app.start(audio=True)
    delayed_a = event(app, TransportEventKind.network_lost)
    b = await app.start(destination=B, audio=True)
    assert delayed_a.stream == a.stream and delayed_a.receiver_id == a.receiver_id
    assert a.stream != b.stream
    notice = delayed_a
    if envelope == "B-stream-under-A-envelope":
        # A corrupt envelope can carry B's otherwise completely current resource
        # identity. This leaves receiver_id as the ONLY mismatching fence.
        notice = replace(event(app, TransportEventKind.network_lost, B), receiver_id=a.receiver_id)
    streams = dict(app.transport.streams)
    outputs = dict(app.display.outputs)
    ownerships = dict(app.ownerships.records)
    pending_a = app.manager.pending_resources(a.receiver_id)
    pending_b = app.manager.pending_resources(b.receiver_id)
    app.journal.clear()
    route = app.manager._receiver_of
    # Normal ingest routes by receiver_id and would hide the explicit guard.
    # Inject a wrong broker route for only this message; keep real admission,
    # actors, handlers and effects. No receiver snapshot is fabricated.
    with monkeypatch.context() as routing:
        routing.setattr(app.manager, "_receiver_of",
                        lambda message: b.receiver_id if message is notice else route(message))
        app.transport.emit(notice)
    await app.settle()
    assert app.state(B) == b
    assert app.state() == a
    assert app.transport.streams == streams
    assert app.display.outputs == outputs and app.ownerships.records == ownerships
    assert app.manager.pending_resources(a.receiver_id) == pending_a
    assert app.manager.pending_resources(b.receiver_id) == pending_b
    assert not app.journal.calls("transport.stop_stream") and not app.journal.calls("display.remove")
    # Positive control: the held A event is genuinely current, not an event
    # rejected everywhere. Restored normal routing changes only A.
    app.transport.emit(delayed_a)
    await app.settle()
    assert app.state() != a and app.state(B) == b


async def test_stale_reconnect_error_cannot_make_later_network_loss_terminal(app_factory):
    app = await app_factory()
    before = await app.start()
    app.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.transport_failed)))
    app.transport.emit(event(app, TransportEventKind.network_lost))
    await app.settle()
    await app.retry_tick()
    assert app.state().last_error is not None
    assert app.state().next_retry_at == app.clock.now() + 2
    await app.retry_tick()
    assert app.state().transport is TransportState.Streaming
    assert app.state().last_error is None
    app.transport.emit(event(app, TransportEventKind.network_lost))
    await app.settle()
    assert app.state().transport is TransportState.Reconnecting
    assert app.state().next_retry_at == app.clock.now() + 1
    assert app.state().display_handle == before.display_handle
    await app.retry_tick()
    assert app.state().transport is TransportState.Streaming
    assert len(app.display.created) == 1


async def test_late_connect_after_endpoint_replacement_preserves_retained_output(app_factory):
    app = await app_factory()
    held = Deferred()
    app.transport.queue_result("start_stream", held)
    try:
        before = await app.start()
        assert before.transport is TransportState.Connecting and before.display is DisplayState.Owned
        operation = replace(app.manager.context(before.receiver_id).operation(),
                            operation_id=before.operation_id,
                            transport_lease_id=before.transport_lease_id)
        late = app.transport._new_handle(target(), operation)
        await app.online(target(endpoint_generation=2, address="192.0.2.99"))
        held.resolve(Ok(value=late))
        await app.settle()
        assert app.state().display_handle == before.display_handle
        assert before.display_handle.owner_uuid in app.ownerships.records
        assert before.display_handle.owner_uuid not in app.display.removed
        assert late.lease_id not in app.transport.streams
        await app.retry_tick()
        assert app.state().transport is TransportState.Streaming
        assert app.state().stream.endpoint_generation == 2
        assert app.state().display_handle == before.display_handle
        assert len(app.display.created) == 1
    finally:
        if not held.resolved:
            held.resolve(Err(error=diagnostic(ErrorCode.stale_target)))
        await app.settle()


async def test_old_daemon_operation_completion_and_cleanup_cannot_reach_replacement_daemon(app_factory):
    holder = {}
    app = await app_factory(configure=real_transport(holder))
    before = await app.start(Mode.MIRROR, audio=True)
    registry = holder["registry"]
    old_control = holder["controls"][before.stream.daemon_instance_uuid]
    gate = Deferred()
    old_control.gates[(BackendOperation.mute, before.receiver_id)] = gate
    try:
        await app.send(SetMuted(receiver_id=before.receiver_id, muted=True))
        assert old_control.calls[-1][0] is BackendOperation.mute
        registry.exit(before.stream.daemon_instance_uuid)
        await app.settle()
        current = app.state()
        assert current.transport is TransportState.Streaming
        assert current.stream.daemon_instance_uuid != before.stream.daemon_instance_uuid
        assert registry.restarts == 1
        new_control = holder["controls"][current.stream.daemon_instance_uuid]
        new_calls = tuple(new_control.calls)
        gate.resolve(None)
        await app.settle()
        assert app.state() == current
        old_context = replace(app.manager.context(before.receiver_id).operation(),
                              transport_lease_id=before.transport_lease_id,
                              endpoint_generation=before.stream.endpoint_generation)
        stale_mute = await app.transport.set_muted(before.stream, True, old_context)
        stale_audio = await app.transport.start_audio(before.stream, old_context)
        stale_stop = await app.transport.stop_stream(before.stream, old_context)
        assert all(isinstance(result, Err) for result in (stale_mute, stale_audio, stale_stop))
        assert tuple(new_control.calls) == new_calls
        assert before.receiver_id in new_control.connected
        assert not any(call[0] in (BackendOperation.disconnect, BackendOperation.mute) for call in new_control.calls)
    finally:
        if not gate.resolved:
            gate.resolve(None)
        await app.settle()


async def test_shutdown_deadline_keeps_uncertain_ownership_and_is_one_shared_transaction(app_factory):
    app = await app_factory()
    a = await app.start(audio=True)
    b = await app.start(destination=B)
    held = Deferred()
    app.transport.queue_result("stop_stream", held)
    first = asyncio.create_task(app.runtime.shutdown())
    second = asyncio.create_task(app.runtime.shutdown())
    try:
        await app.settle()
        assert not first.done()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        # Both callers share the real coordinator; advancing its scheduler
        # completes the policy budget without a wall-clock sleep.
        app.scheduler.advance(15)
        await app.settle()
        result = await asyncio.wait_for(second, 1)
        assert isinstance(result, Err) and result.error.code is ErrorCode.cleanup_timeout
        uncertain = app.state()
        assert not uncertain.intent_active and uncertain.cleanup_pending
        assert uncertain.phase is SessionPhase.CleanupRequired
        assert a.ownership_uuid in app.ownerships.records
        assert app.ownerships.records[a.ownership_uuid].desired_state == "removing"
        assert app.display.process.alive(a.display_handle.helper)
        assert not app.state(B).cleanup_pending and b.ownership_uuid not in app.ownerships.records
        stops = app.journal.calls("transport.stop_stream")
        assert [call["handle"]["lease_id"] for call in stops].count(a.transport_lease_id) == 1
        assert [call["handle"]["lease_id"] for call in stops].count(b.transport_lease_id) == 1
        stored = {state.receiver_id: state for state in await app.sessions.load_all()}
        assert stored[a.receiver_id].cleanup_pending and not stored[a.receiver_id].intent_active
        assert not stored[b.receiver_id].cleanup_pending
    finally:
        if not held.resolved:
            held.resolve(Ok(value=None))
        await app.settle()
        if not second.done():
            await asyncio.wait_for(second, 1)
