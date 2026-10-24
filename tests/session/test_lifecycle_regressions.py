"""Resource-backed lifecycle regressions; every external boundary is deterministic."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    ConfirmSwitch, EffectEvent, Pair, RetryCleanup, Start, Stop, SubmitPairing,
)
from airplay_linux.domain.operations import (
    InstanceId, LeaseId, SensitiveText, StreamHandle,
)
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode,
    Ok, SessionPhase, StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import Route
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import context, physical, target, virtual_handle


async def online_rig(factory=build_lifecycle_handlers):
    rig = SessionRig(factory)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.daemon_instance_uuid = InstanceId(UUID(int=9))
    for adapter in (rig.capture, rig.transport, rig.display, rig.audio):
        adapter.journal = rig.journal
    await rig.settle()
    return rig, destination


def stream_for(destination, state, *, number=1):
    return StreamHandle(
        receiver_id=destination.receiver_id,
        endpoint_generation=destination.endpoint_generation,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        backend_stream_id=f"regression-stream-{number}",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=state.transport_lease_id,
        start_operation_id=state.operation_id,
    )


async def owned_session(rig, destination, *, streaming=True, delivered=True):
    operation = context(lease=1 if streaming else None)
    created = await rig.display.create(
        destination.receiver_id, destination.endpoint_fingerprint,
        virtual_handle().source.size, operation,
    )
    assert isinstance(created, Ok)
    handle = created.value
    state = replace(
        rig.manager.snapshot(destination.receiver_id),
        operation_id=operation.operation_id,
        session_generation=operation.session_generation,
        transport_lease_id=operation.transport_lease_id,
        mode=Mode.VIRTUAL, intent_active=True,
        intent_stage=IntentStage.active if streaming else IntentStage.starting,
        transport=TransportState.Streaming if streaming else TransportState.Connecting,
        audio=AudioState.Enabled if streaming else AudioState.Disabled,
        display=DisplayState.Owned if delivered else DisplayState.Creating,
        display_handle=handle if delivered else None,
        ownership_uuid=handle.owner_uuid if delivered else None,
    )
    if streaming:
        state = replace(state, stream=stream_for(destination, state))
        rig.transport.streams[state.stream.lease_id] = state.stream
    rig.manager.install(state)
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("connector", ["window:123", "Virtual-1"])
async def test_provider_verification_blocks_an_unverified_capture(connector):
    # Bypassing capture.verify must admit video here, rather than fail earlier
    # on a substituted connector or the selection double's own admission.
    rig, destination = await online_rig()
    source = physical(connector)
    rig.capture.queue_result("select", Ok(value=source))
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=source),
    ))
    await rig.settle()
    assert "transport.start_stream" not in rig.journal.names()
    assert rig.manager.snapshot(destination.receiver_id).stream is None
    assert rig.capture.released == [source]
    assert rig.manager.snapshot(destination.receiver_id).last_error.code is ErrorCode.unsupported_source


@pytest.mark.asyncio
async def test_stale_trust_generation_cannot_promote_a_trusted_completion():
    def factory(deps):
        routes = dict(build_lifecycle_handlers(deps))
        original = routes[Route.trust]

        def retire_trust(ctx, message):
            if isinstance(message, EffectEvent) and message.kind == "trust:pairing_submit":
                ctx.apply(trust_generation=ctx.state.trust_generation + 1)
            return original(ctx, message)

        routes[Route.trust] = retire_trust
        return routes

    rig, destination = await online_rig(factory)
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    challenge = rig.manager.snapshot(destination.receiver_id).challenge
    assert challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=destination.receiver_id, challenge_id=challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.CleanupRequired
    assert state.challenge is None


@pytest.mark.asyncio
async def test_publication_failure_blocks_all_resource_teardown_until_retry():
    rig, destination = await online_rig()
    handle = await owned_session(rig, destination)

    async def stop_control(stream, operation):
        rig.journal.record("audio.stop_control")
        return Ok(value=None)

    rig.audio.stop_control = stop_control
    rig.display.queue_result("prepare_teardown", Err(error=diagnostic(ErrorCode.persistence_failed)))
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    for name in ("transport.stop_stream", "audio.stop_control", "capture.release", "display.remove"):
        assert name not in rig.journal.names()
    assert handle.owner_uuid in rig.display.handles
    assert rig.manager.snapshot(destination.receiver_id).cleanup_pending
    await rig.manager.dispatch(RetryCleanup(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_owned_output_and_audio_wait_for_exact_transport_and_audio_acknowledgements():
    rig, destination = await online_rig()
    handle = await owned_session(rig, destination)
    original = rig.manager.snapshot(destination.receiver_id).stream
    stop_ack, audio_ack = Deferred(), Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    audio_released = []

    async def stop_control(stream, operation):
        rig.journal.record("audio.stop_control")
        result = await audio_ack.wait()
        if isinstance(result, Ok):
            audio_released.append(stream)
        return result

    rig.audio.stop_control = stop_control
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "display.prepare_teardown" in rig.journal.names()
    assert "transport.stop_stream" in rig.journal.names()
    assert "audio.stop_control" not in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.ownership.records[handle.owner_uuid].desired_state == "removing"
    assert rig.display.process.alive(handle.helper)
    stop_ack.resolve(Ok(value=None))
    await rig.settle()
    assert original.lease_id not in rig.transport.streams
    assert "audio.stop_control" in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    audio_ack.resolve(Ok(value=None))
    await rig.settle()
    assert audio_released == [original]
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.display.process.alive(handle.helper)
    assert handle.source.kscreen_output_id not in rig.display.outputs
    assert handle.owner_uuid not in rig.display.ownership.records
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True], ids=["stop", "switch"])
async def test_failed_late_capture_completes_stop_and_wakes_queued_start(queued):
    rig, destination = await online_rig()
    selected = Deferred()
    rig.capture.queue_result("select", selected)
    request = StartRequest(mode=Mode.MIRROR, source=physical(), audio=AudioPolicy(enabled=False))
    await rig.manager.dispatch(Start(receiver_id=destination.receiver_id, request=request))
    await rig.settle()
    assert rig.manager.context(destination.receiver_id).outstanding_acquisitions() == 1
    command = (ConfirmSwitch(receiver_id=destination.receiver_id, request=request, confirmed=True)
               if queued else Stop(receiver_id=destination.receiver_id))
    await rig.manager.dispatch(command)
    await rig.settle()
    before = rig.manager.snapshot(destination.receiver_id)
    assert before.cleanup_pending, "an unresolved capture can still acquire a resource"
    assert rig.journal.names().count("capture.select") == 1
    # Hold a possible queued restart so it cannot finish/fail for an unrelated
    # lease mismatch in FakeTransport's default handle builder.
    next_stream = Deferred()
    rig.transport.queue_result("start_stream", next_stream)
    selected.resolve(Err(error=diagnostic(ErrorCode.portal_cancelled)))
    await rig.settle()
    after = rig.manager.snapshot(destination.receiver_id)
    if queued:
        assert rig.journal.names().count("capture.select") == 2
        assert after.intent_active
        assert after.session_generation == before.session_generation
        next_stream.resolve(Ok(value=stream_for(destination, after)))
        await rig.settle()
        assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.Mirroring
        await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
        await rig.settle()
    else:
        assert not after.cleanup_pending
        assert after.phase is SessionPhase.Available
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_failed_late_release_retains_exact_source_until_retry():
    rig, destination = await online_rig()
    selected = Deferred()
    rig.capture.queue_result("select", selected)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await rig.settle()
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    source = replace(physical(), capture_lease_id=LeaseId(UUID(int=72)))
    rig.capture.queue_result("release", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    selected.resolve(Ok(value=source))
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == (source,)
    assert rig.manager.snapshot(destination.receiver_id).cleanup_pending
    await rig.manager.dispatch(RetryCleanup(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending
    assert rig.capture.released == [source]


def deferred_acquisition(acquired, name):
    """Exercise cleanup for a producer route without depending on later tasks."""
    def factory(deps):
        routes = dict(build_lifecycle_handlers(deps))

        def virtual(ctx, message):
            if not isinstance(message, Start):
                return False
            operation = replace(ctx.operation(), operation_id=ctx.state.operation_id)
            if name == "transport_start":
                operation = ctx.new_transport_lease(operation)

            async def acquire():
                result = await acquired.wait()
                if isinstance(result, Ok) and isinstance(result.value, StreamHandle):
                    deps.transport.streams[result.value.lease_id] = result.value
                return result

            ctx.launch(Route.virtual, name, operation, acquire, acquisition=True)
            return True

        routes[Route.virtual] = virtual
        return routes
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False], ids=["stream", "no-resource"])
async def test_owned_display_waits_for_retired_transport_acquisition_and_release(succeeds):
    acquired = Deferred()
    rig, destination = await online_rig(deferred_acquisition(acquired, "transport_start"))
    handle = await owned_session(rig, destination, streaming=False)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await rig.settle()
    late = stream_for(destination, rig.manager.snapshot(destination.receiver_id))
    stop_ack = Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    if succeeds:
        acquired.resolve(Ok(value=late))
        await rig.settle()
        assert "transport.stop_stream" in rig.journal.names()
        assert "display.remove" not in rig.journal.names()
        assert rig.display.process.alive(handle.helper)
        stop_ack.resolve(Ok(value=None))
    else:
        acquired.resolve(Err(error=diagnostic(ErrorCode.transport_failed)))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.display.process.alive(handle.helper)
    assert not rig.transport.streams
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_late_display_waits_for_held_transport_stop_and_durable_publication():
    acquired = Deferred()
    rig, destination = await online_rig(deferred_acquisition(acquired, "display_create"))
    # The creator owns the exact helper but has not delivered its handle yet.
    handle = await owned_session(rig, destination, delivered=False)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await rig.settle()
    stop_ack = Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "transport.stop_stream" in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    acquired.resolve(Ok(value=handle))
    await rig.settle()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    stop_ack.resolve(Ok(value=None))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending
