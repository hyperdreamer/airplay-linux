"""Regression probes for review findings F-1 through F-7."""

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.messages import (
    DisplayEvent, EffectEvent, EmptyPayload, Retry, RetryAudio, SetMuted, Start,
    Stop, SubmitPairing, TimerEvent, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import (
    ChallengeId, ChallengeView, InstanceId, OpaqueCredentialRef, OperationId,
    SensitiveText, StreamHandle,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    DisplayState, IntentStage, Mode, Ok, StartRequest, TransportState,
)
from airplay_linux.session.engine import Route
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import context, physical, target, virtual_handle
from airplay_linux.trust.fences import fence_matches


def start(receiver_id):
    return Start(receiver_id=receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))


def stream_for(operation, name="stream"):
    destination = target(endpoint_generation=operation.endpoint_generation)
    return StreamHandle(
        receiver_id=destination.receiver_id,
        endpoint_generation=destination.endpoint_generation,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        backend_stream_id=name,
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=operation.transport_lease_id,
        start_operation_id=operation.operation_id,
    )


def streaming_state():
    operation = context(generation=1, lease=7)
    handle = stream_for(operation)
    display = virtual_handle()
    return replace(
        SessionSnapshot.idle(handle.receiver_id, online=True), target=target(),
        stream=handle, session_generation=1, operation_id=operation.operation_id,
        transport_lease_id=handle.lease_id, transport=TransportState.Connecting,
        display_handle=display, ownership_uuid=display.owner_uuid, display=DisplayState.Owned,
    )


def ready_for(state):
    handle = state.stream
    return TransportEvent(
        kind=TransportEventKind.ready, receiver_id=state.receiver_id, stream=handle,
        endpoint_fingerprint=handle.endpoint_fingerprint,
        endpoint_generation=handle.endpoint_generation,
        daemon_instance_uuid=handle.daemon_instance_uuid,
        context=context(
            generation=state.session_generation, endpoint_generation=handle.endpoint_generation,
            lease=handle.lease_id.int, operation_id=handle.start_operation_id,
        ),
        payload=EmptyPayload(),
    )


@pytest.mark.asyncio
async def test_effect_cannot_mutate_snapshot_after_stop_outside_dispatch():
    completion = Deferred()
    rejected = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                operation = ctx.supersede("start")
                ctx.apply(intent_active=True, transport=TransportState.Connecting)

                async def work():
                    result = await completion.wait()
                    try:
                        ctx.apply(intent_active=True, transport=TransportState.Streaming)
                    except RuntimeError:
                        rejected.append("apply")
                    try:
                        ctx.state = replace(ctx.state, intent_active=True)
                    except AttributeError:
                        rejected.append("state")
                    return result

                ctx.launch(Route.mirror, "illegal-write", operation, work)
            return True

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("stop")
                ctx.apply(intent_active=False, transport=TransportState.Idle)
            return True

        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    rig.discovery.add_target(target())
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    stopped = rig.manager.snapshot(rig.receiver_id)
    completion.resolve(Ok(value=None))
    await rig.settle()
    assert rejected == ["apply", "state"]
    assert rig.manager.snapshot(rig.receiver_id) == stopped


@pytest.mark.parametrize("mutation", [
    "apply", "supersede", "new_transport_lease", "invalidate_trust", "cancel_timers",
    "cancel_audio", "launch", "schedule",
])
@pytest.mark.asyncio
async def test_mutating_context_operations_require_actor_dispatch(mutation):
    rig = SessionRig()
    ctx = rig.manager.context(rig.receiver_id)
    original = ctx.state
    operation = context(generation=0, lease=None)
    mutations = {
        "apply": lambda: ctx.apply(audio_operation_id=OperationId(UUID(int=71))),
        "supersede": lambda: ctx.supersede("stop"),
        "new_transport_lease": lambda: ctx.new_transport_lease(operation),
        "invalidate_trust": ctx.invalidate_trust,
        "cancel_timers": ctx.cancel_timers,
        "cancel_audio": ctx.cancel_audio,
        "launch": lambda: ctx.launch(Route.mirror, "setup", operation, Deferred().wait),
        "schedule": lambda: ctx.schedule("reconnect", 2, operation),
    }
    with pytest.raises(RuntimeError):
        mutations[mutation]()
    assert ctx.state == original
    assert rig.manager.snapshot(rig.receiver_id) == original
    assert rig.fences.get(rig.receiver_id) is None


@pytest.mark.asyncio
async def test_effect_returns_its_result_for_actor_delivery():
    completion = Deferred()
    delivered = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "setup", ctx.operation(), completion.wait)
            elif isinstance(message, EffectEvent):
                delivered.append(message.result)
                ctx.apply(intent_active=True)
            return True
        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    await rig.manager.dispatch(start(rig.receiver_id))
    result = Ok(value="completed")
    completion.resolve(result)
    await rig.settle()
    assert delivered == [result]
    assert rig.manager.snapshot(rig.receiver_id).intent_active


@pytest.mark.asyncio
async def test_ingested_stale_transport_event_is_fenced_at_actor_delivery():
    received = []

    def handlers(_deps):
        def reconnect(ctx, message):
            if isinstance(message, TransportEvent):
                received.append(message)
                ctx.apply(transport=TransportState.Streaming)
            return True

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("stop")
                ctx.apply(transport=TransportState.Idle, intent_active=False)
            return True
        return {Route.reconnect: reconnect, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    installed = streaming_state()
    rig.manager.install(installed)
    ready = ready_for(installed)
    rig.manager.ingest(ready)
    assert received == [ready]
    received.clear()
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    rig.manager.ingest(ready)
    await rig.settle()
    assert received == []
    assert rig.manager.snapshot(rig.receiver_id).transport is TransportState.Idle


@pytest.mark.asyncio
async def test_stale_nonacquisition_effect_cannot_revive_stopped_intent():
    completion = Deferred()
    received = []
    callback_errors = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "setup", ctx.supersede("start"), completion.wait)
            elif isinstance(message, EffectEvent):
                received.append(message)
                ctx.apply(intent_active=True, intent_stage=IntentStage.active,
                          transport=TransportState.Streaming)
            return True

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("stop")
                ctx.apply(intent_active=False, transport=TransportState.Idle)
            return True
        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    rig.discovery.add_target(target())
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    stopped = rig.manager.snapshot(rig.receiver_id)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, error: callback_errors.append(error))
    try:
        completion.resolve(Ok(value=None))
        await rig.settle()
    finally:
        loop.set_exception_handler(previous)
    assert callback_errors == []
    assert received == []
    assert rig.manager.snapshot(rig.receiver_id) == stopped


@pytest.mark.asyncio
async def test_same_intent_retry_cleans_a_late_acquisition_from_old_lease():
    old_completion, new_completion = Deferred(), Deferred()
    operations, cleanup_events = [], []

    def handlers(_deps):
        def acquire(ctx, message):
            if isinstance(message, (Start, Retry)):
                operation = ctx.new_transport_lease(
                    ctx.supersede("start") if isinstance(message, Start) else ctx.operation()
                )
                operations.append(operation)
                completion = old_completion if isinstance(message, Start) else new_completion
                route = Route.mirror if isinstance(message, Start) else Route.reconnect
                ctx.launch(route, "stream", operation, completion.wait, acquisition=True)
            elif isinstance(message, EffectEvent):
                ctx.apply(stream=message.result.value, transport=TransportState.Streaming)
            return True

        def cleanup(_ctx, message):
            cleanup_events.append(message)
            return True
        return {Route.mirror: acquire, Route.reconnect: acquire, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    rig.discovery.add_target(target())
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    assert operations[0].session_generation == operations[1].session_generation == 1
    assert operations[0].transport_lease_id != operations[1].transport_lease_id
    old_stream, new_stream = stream_for(operations[0], "old"), stream_for(operations[1], "new")
    new_completion.resolve(Ok(value=new_stream))
    await rig.settle()
    assert rig.manager.snapshot(rig.receiver_id).stream == new_stream
    assert rig.manager.pending_resources(rig.receiver_id) == ()
    old_completion.resolve(Ok(value=old_stream))
    await rig.settle()
    assert rig.manager.snapshot(rig.receiver_id).stream == new_stream
    assert len(cleanup_events) == 1
    late = cleanup_events[0]
    assert late.kind == "cleanup:stream"
    assert late.operation_id == operations[0].operation_id
    assert late.session_generation == 1
    assert late.resource == old_stream
    assert rig.manager.pending_resources(rig.receiver_id) == (old_stream,)


@pytest.mark.parametrize("reason", ["start", "cancel", "stop", "switch", "reconfigure", "shutdown"])
@pytest.mark.asyncio
async def test_supersede_returns_the_trust_generation_it_published(reason):
    observed = []

    def handlers(deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                operation = ctx.supersede(reason)
                observed.append((operation, ctx.state, deps.fences.get(ctx.state.receiver_id)))
            return True
        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    rig.discovery.add_target(target())
    await rig.manager.dispatch(start(rig.receiver_id))
    operation, state, fence = observed[0]
    assert operation.operation_id == state.operation_id
    assert operation.session_generation == state.session_generation == fence.session_generation == 1
    assert operation.trust_generation == state.trust_generation == fence.trust_generation == 1
    challenge = rig.pairing.script_challenge(state.target, operation)
    assert isinstance(rig.fences.activate(challenge), Ok)
    assert fence_matches(challenge, rig.fences.get(rig.receiver_id), rig.clock.now())


@pytest.mark.asyncio
async def test_retry_audio_challenge_and_mute_commands_preserve_resource_generations():
    observed = []

    def handlers(_deps):
        def command(ctx, message):
            if isinstance(message, (Retry, RetryAudio, SubmitPairing, SetMuted)):
                operation = ctx.operation()
                ctx.apply(operation_id=operation.operation_id)
                observed.append((operation, ctx.state))
            return True
        return {Route.reconnect: command, Route.audio: command, Route.trust: command}

    rig = SessionRig(handlers)
    installed = streaming_state()
    challenge = ChallengeView(
        challenge_id=ChallengeId(UUID(int=91)), receiver_id=rig.receiver_id,
        endpoint_fingerprint=installed.target.endpoint_fingerprint, endpoint_generation=1,
        operation_id=installed.operation_id, session_generation=1, trust_generation=0,
        created_at=0, expires_at=120, credential_kind="pin",
    )
    installed = replace(installed, challenge=challenge)
    rig.manager.install(installed)
    receipts = []
    for command in (
        Retry(receiver_id=rig.receiver_id), RetryAudio(receiver_id=rig.receiver_id),
        SubmitPairing(receiver_id=rig.receiver_id, challenge_id=challenge.challenge_id,
                      value=SensitiveText("test-input")),
        SetMuted(receiver_id=rig.receiver_id, muted=True),
    ):
        receipt = await rig.manager.dispatch(command)
        assert receipt.accepted
        receipts.append(receipt.operation_id)
        assert observed[-1][0].session_generation == 1
        assert rig.manager.snapshot(rig.receiver_id).session_generation == 1
    assert len(observed) == 4
    assert len(set(receipts)) == 4
    assert len({operation.operation_id for operation, _state in observed}) == 4
    for operation, state in observed:
        assert operation.session_generation == state.session_generation == 1
        assert operation.trust_generation == state.trust_generation == 0
        assert operation.transport_lease_id == state.transport_lease_id == installed.transport_lease_id
        assert state.stream == installed.stream
        assert state.display_handle == installed.display_handle
        assert state.ownership_uuid == installed.ownership_uuid
        assert state.target == installed.target
        assert state.challenge == challenge


@pytest.mark.asyncio
async def test_stale_trust_completion_still_reaches_compensation_handler():
    completion = Deferred()
    received = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.trust, "pair", ctx.supersede("start"), completion.wait)
            return True
        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("stop")
            return True
        def trust(_ctx, message):
            received.append(message)
            return True
        return {Route.mirror: mirror, Route.cleanup: cleanup, Route.trust: trust}

    rig = SessionRig(handlers)
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    result = Ok(value=None)
    completion.resolve(result)
    await rig.settle()
    assert len(received) == 1
    assert received[0].kind == "trust:pair"
    assert received[0].session_generation == 1
    assert received[0].result == result


@pytest.mark.asyncio
async def test_display_delivery_uses_ownership_independently_of_endpoint():
    received = []

    def handlers(_deps):
        def virtual(ctx, message):
            if isinstance(message, DisplayEvent):
                received.append(message)
                ctx.apply(display=DisplayState.Failed)
            return True
        return {Route.virtual: virtual}

    rig = SessionRig(handlers)
    state = replace(streaming_state(), target=target(endpoint_generation=2), session_generation=5)
    rig.manager.install(state)
    handle = state.display_handle
    event = DisplayEvent(
        kind="helper_exit", receiver_id=rig.receiver_id, creation_operation_id=None,
        owner_uuid=handle.owner_uuid, connector=handle.source.connector,
        output_id=handle.source.kscreen_output_id, helper=handle.helper,
        handle=handle, diagnostic=None,
    )
    rig.manager.ingest(replace(event, owner_uuid=virtual_handle(owner=2).owner_uuid))
    assert received == []
    rig.manager.ingest(event)
    await rig.settle()
    assert received == [event]
    assert rig.manager.snapshot(rig.receiver_id).display is DisplayState.Failed


@pytest.mark.asyncio
async def test_exact_old_stream_release_is_admitted_after_stop():
    received = []

    def handlers(_deps):
        def reconnect(ctx, message):
            if isinstance(message, TransportEvent):
                received.append(message)
                ctx.apply(stream=None, transport=TransportState.Idle)
            return True
        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("stop")
            return True
        return {Route.reconnect: reconnect, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    state = streaming_state()
    rig.manager.install(state)
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    stopped = replace(
        ready_for(state), kind=TransportEventKind.stopped,
        context=replace(ready_for(state).context, operation_id=OperationId(UUID(int=98))),
    )
    rig.manager.ingest(stopped)
    await rig.settle()
    assert received == [stopped]
    assert rig.manager.snapshot(rig.receiver_id).stream is None


@pytest.mark.asyncio
async def test_timer_delivery_rejects_old_operation_and_generation():
    received = []
    rig = SessionRig(lambda _deps: {Route.reconnect: lambda _ctx, event: received.append(event) or True})
    state = streaming_state()
    rig.manager.install(state)
    event = TimerEvent(receiver_id=rig.receiver_id, kind="reconnect", context=ready_for(state).context)
    rig.manager.ingest(replace(event, context=replace(event.context, session_generation=0)))
    rig.manager.ingest(replace(event, context=replace(event.context, operation_id=OperationId(UUID(int=99)))))
    assert received == []
    rig.manager.ingest(event)
    assert received == [event]


@pytest.mark.parametrize("changed", ["operation", "endpoint", "lease", "audio"])
@pytest.mark.asyncio
async def test_ordinary_effect_delivery_rechecks_applicable_tokens(changed):
    completion = Deferred()
    received = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                operation = ctx.operation()
                ctx.apply(operation_id=operation.operation_id)
                ctx.launch(Route.audio if changed == "audio" else Route.mirror,
                           "control", operation, completion.wait)
            elif isinstance(message, EffectEvent):
                received.append(message)
                ctx.apply(muted=True)
            return True
        def reconnect(ctx, message):
            if isinstance(message, Retry):
                changes = {
                    "operation": {"operation_id": OperationId(UUID(int=101))},
                    "endpoint": {"target": target(endpoint_generation=2)},
                    "lease": {"transport_lease_id": context(lease=101).transport_lease_id},
                    "audio": {"audio_operation_id": OperationId(UUID(int=101))},
                }
                ctx.apply(**changes[changed])
            return True
        return {Route.mirror: mirror, Route.reconnect: reconnect, Route.audio: mirror}

    rig = SessionRig(handlers)
    rig.manager.install(replace(streaming_state(), audio_operation_id=OperationId(UUID(int=100))))
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    current = rig.manager.snapshot(rig.receiver_id)
    completion.resolve(Ok(value=None))
    await rig.settle()
    assert received == []
    assert rig.manager.snapshot(rig.receiver_id) == current


@pytest.mark.parametrize("route", [Route.mirror, Route.audio])
@pytest.mark.asyncio
async def test_effect_admission_uses_only_its_own_operation_tokens(route):
    completion = Deferred()
    received = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                operation = ctx.operation()
                ctx.apply(operation_id=operation.operation_id)
                ctx.launch(route, "control", operation, completion.wait)
            elif isinstance(message, EffectEvent):
                received.append(message)
            return True
        def reconnect(ctx, message):
            if isinstance(message, Retry):
                if route is Route.mirror:
                    ctx.apply(audio_operation_id=OperationId(UUID(int=103)))
                else:
                    ctx.apply(operation_id=OperationId(UUID(int=103)))
            return True
        return {Route.mirror: mirror, Route.audio: mirror, Route.reconnect: reconnect}

    rig = SessionRig(handlers)
    rig.manager.install(replace(streaming_state(), audio_operation_id=OperationId(UUID(int=100))))
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    completion.resolve(Ok(value=None))
    await rig.settle()
    assert len(received) == 1
    assert received[0].result == Ok(value=None)


@pytest.mark.parametrize("changed", ["endpoint", "daemon"])
@pytest.mark.asyncio
async def test_acquisition_rechecks_endpoint_and_daemon_binding(changed):
    completion = Deferred()
    received, cleanup = [], []
    acquired = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                operation = ctx.operation()
                acquired.append(replace(ctx.state.stream, start_operation_id=operation.operation_id))
                ctx.launch(Route.mirror, "stream", operation, completion.wait, acquisition=True)
            elif isinstance(message, EffectEvent):
                received.append(message)
                ctx.apply(stream=message.resource)
            return True
        def reconnect(ctx, message):
            if isinstance(message, Retry):
                if changed == "endpoint":
                    ctx.apply(target=target(endpoint_generation=2))
                else:
                    ctx.apply(stream=replace(ctx.state.stream, daemon_instance_uuid=InstanceId(UUID(int=88))))
            return True
        return {Route.mirror: mirror, Route.reconnect: reconnect,
                Route.cleanup: lambda _ctx, event: cleanup.append(event) or True}

    rig = SessionRig(handlers)
    rig.manager.install(streaming_state())
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    current = rig.manager.snapshot(rig.receiver_id)
    completion.resolve(Ok(value=acquired[0]))
    await rig.settle()
    assert received == []
    assert rig.manager.snapshot(rig.receiver_id) == current
    assert len(cleanup) == 1 and cleanup[0].resource == acquired[0]
    assert cleanup[0].kind == "cleanup:stream"
    assert rig.manager.pending_resources(rig.receiver_id) == (acquired[0],)


@pytest.mark.parametrize("replace_owner", [False, True])
@pytest.mark.asyncio
async def test_display_acquisition_uses_owner_independently_of_endpoint(replace_owner):
    completion = Deferred()
    received, cleanup = [], []
    handle = virtual_handle()

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.virtual, "display", ctx.operation(), completion.wait, acquisition=True)
            return True
        def reconnect(ctx, message):
            if isinstance(message, Retry):
                ctx.apply(target=target(endpoint_generation=2))
                if replace_owner:
                    other = virtual_handle(owner=2)
                    ctx.apply(display_handle=other, ownership_uuid=other.owner_uuid)
            return True
        def virtual(ctx, message):
            received.append(message)
            ctx.apply(display_handle=message.resource, display=DisplayState.Owned)
            return True
        return {Route.mirror: mirror, Route.reconnect: reconnect, Route.virtual: virtual,
                Route.cleanup: lambda _ctx, event: cleanup.append(event) or True}

    rig = SessionRig(handlers)
    rig.manager.install(streaming_state())
    await rig.manager.dispatch(start(rig.receiver_id))
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    completion.resolve(Ok(value=handle))
    await rig.settle()
    if replace_owner:
        assert received == []
        assert len(cleanup) == 1 and cleanup[0].resource == handle
        assert rig.manager.pending_resources(rig.receiver_id) == (handle,)
        assert rig.manager.snapshot(rig.receiver_id).display_handle == virtual_handle(owner=2)
    else:
        assert len(received) == 1 and received[0].resource == handle
        assert cleanup == []
        assert rig.manager.pending_resources(rig.receiver_id) == ()


@pytest.mark.asyncio
async def test_pairing_expiry_uses_current_challenge_trust_tokens_not_latest_command():
    received = []
    rig = SessionRig(lambda _deps: {Route.reconnect: lambda _ctx, event: received.append(event) or True})
    state = streaming_state()
    challenge = ChallengeView(
        challenge_id=ChallengeId(UUID(int=91)), receiver_id=rig.receiver_id,
        endpoint_fingerprint=state.target.endpoint_fingerprint, endpoint_generation=1,
        operation_id=state.operation_id, session_generation=1, trust_generation=2,
        created_at=0, expires_at=120, credential_kind="pin",
    )
    rig.manager.install(replace(state, challenge=challenge, trust_generation=2,
                                operation_id=OperationId(UUID(int=102))))
    event = TimerEvent(
        receiver_id=rig.receiver_id, kind="pairing_expiry",
        context=replace(ready_for(state).context, trust_generation=2),
    )
    rig.manager.ingest(replace(event, context=replace(event.context, trust_generation=1)))
    assert received == []
    rig.manager.ingest(event)
    assert received == [event]


@pytest.mark.asyncio
async def test_exact_release_effect_survives_stop_and_endpoint_replacement():
    completion = Deferred()
    received = []

    def handlers(_deps):
        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.launch(Route.cleanup, "release", ctx.operation(), completion.wait,
                           resource=ctx.state.stream)
                ctx.supersede("stop")
                ctx.apply(target=target(endpoint_generation=2))
            elif isinstance(message, EffectEvent):
                received.append(message)
                ctx.apply(stream=None, transport=TransportState.Idle)
            return True
        return {Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    state = streaming_state()
    rig.manager.install(state)
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    completion.resolve(Ok(value=None))
    await rig.settle()
    assert len(received) == 1
    assert received[0].kind == "cleanup:release"
    assert received[0].session_generation == 1
    assert received[0].resource == state.stream
    assert rig.manager.snapshot(rig.receiver_id).stream is None


@pytest.mark.asyncio
async def test_session_rig_defaults_to_trusted_available_audio_and_unknown_modes():
    rig = SessionRig()
    assert isinstance(rig.pairing.trusted.get(rig.receiver_id), OpaqueCredentialRef)
    capabilities = rig.deps.capabilities()
    assert all((capabilities.discovery, capabilities.mirror, capabilities.virtual_display,
                capabilities.audio, capabilities.remembered_pairing,
                capabilities.additional_virtual_outputs))
    assert isinstance(await rig.audio.check(), Ok)
    rig.transport.update_target(target(), rig.receiver_id)
    modes = await rig.transport.query_modes(target(), context())
    assert isinstance(modes, Ok)
    assert modes.value is None
