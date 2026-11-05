from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import (
    DisplayEvent,
    EffectEvent,
    EmptyPayload,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    ChallengeId, ChallengeView, InstanceId, LeaseId, OperationId, ProcessIdentity, StreamHandle,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import AudioState, DisplayState, Mode, Ok
from airplay_linux.session.fencing import accept_challenge, accept_cleanup, accept_display, accept_transport
from airplay_linux.testing.builders import context, physical, target, virtual_handle


def stream(*, receiver: str = "a", generation: int = 1, lease: int = 10, operation: int = 11) -> StreamHandle:
    destination = target(receiver=receiver, endpoint_generation=generation)
    return StreamHandle(
        receiver_id=destination.receiver_id,
        endpoint_generation=generation,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        backend_stream_id=f"stream-{lease}",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=LeaseId(UUID(int=lease)),
        start_operation_id=OperationId(UUID(int=operation)),
    )


def transport_ready(state: SessionSnapshot, handle: StreamHandle) -> TransportEvent:
    return TransportEvent(
        kind=TransportEventKind.ready,
        receiver_id=handle.receiver_id,
        stream=handle,
        endpoint_fingerprint=handle.endpoint_fingerprint,
        endpoint_generation=handle.endpoint_generation,
        daemon_instance_uuid=handle.daemon_instance_uuid,
        context=context(
            generation=state.session_generation,
            endpoint_generation=handle.endpoint_generation,
            lease=handle.lease_id.int,
            operation_id=handle.start_operation_id,
        ),
        payload=EmptyPayload(),
    )


def test_stale_transport_lease_cannot_update_current_state() -> None:
    destination = target()
    handle = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        stream=handle,
        transport_lease_id=handle.lease_id,
        session_generation=1,
    )
    ready = transport_ready(state, handle)
    stale = replace(
        ready,
        context=context(
            generation=state.session_generation,
            endpoint_generation=destination.endpoint_generation,
            lease=999,
            operation_id=handle.start_operation_id,
        ),
    )
    assert not accept_transport(state, stale)
    assert accept_transport(state, ready)


def test_transport_ready_uses_stream_start_operation_not_latest_command() -> None:
    destination = target()
    handle = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        stream=handle,
        operation_id=OperationId(UUID(int=999)),
        transport_lease_id=handle.lease_id,
        session_generation=1,
    )
    event = transport_ready(state, handle)
    assert accept_transport(state, event)
    latest_operation = replace(
        event,
        context=replace(event.context, operation_id=OperationId(UUID(int=999))),
    )
    assert not accept_transport(state, latest_operation)


def test_transport_start_operation_is_compared_with_owned_handle() -> None:
    destination = target()
    owned = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True), target=destination,
        stream=owned, transport_lease_id=owned.lease_id, session_generation=1,
    )
    incoming = replace(owned, start_operation_id=OperationId(UUID(int=333)))
    # Both the incoming handle and context agree with each other, but not ownership.
    assert not accept_transport(state, transport_ready(state, incoming))


def test_stopped_event_can_finish_an_old_exact_stream_after_generation_change() -> None:
    destination = target()
    handle = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        stream=handle,
        transport_lease_id=handle.lease_id,
        session_generation=2,
    )
    event = replace(
        transport_ready(replace(state, session_generation=1), handle),
        kind=TransportEventKind.stopped,
        context=replace(transport_ready(replace(state, session_generation=1), handle).context,
                        session_generation=1),
    )
    assert accept_transport(state, event)


def test_stopped_exact_stream_does_not_require_the_start_operation() -> None:
    destination = target()
    handle = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        stream=handle,
        transport_lease_id=handle.lease_id,
        session_generation=2,
    )
    event = replace(
        transport_ready(state, handle),
        kind=TransportEventKind.stopped,
        context=replace(
            transport_ready(state, handle).context,
            operation_id=OperationId(UUID(int=999)),
        ),
    )
    assert accept_transport(state, event)


def test_cleanup_accepts_only_exact_owned_resource() -> None:
    destination = target()
    owned = stream()
    other = stream(lease=12, operation=13)
    state = replace(SessionSnapshot.idle(destination.receiver_id, online=True), target=destination)
    event = replace(transport_ready(replace(state, session_generation=1), owned), kind=TransportEventKind.stopped)
    assert accept_cleanup((owned,), event)
    assert not accept_cleanup((other,), event)


def test_stopped_exact_stream_remains_releasable_after_current_lease_is_cleared() -> None:
    destination = target()
    handle = stream()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        stream=handle,
        transport_lease_id=None,
        session_generation=2,
    )
    event = replace(
        transport_ready(replace(state, session_generation=1), handle),
        kind=TransportEventKind.stopped,
        context=replace(
            transport_ready(replace(state, session_generation=1), handle).context,
            session_generation=1,
        ),
    )
    assert accept_transport(state, event)


def test_display_fence_uses_current_owner_and_exact_helper_identity() -> None:
    destination = target()
    handle = virtual_handle()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        mode=Mode.VIRTUAL,
        display=DisplayState.Owned,
        display_handle=handle,
        ownership_uuid=handle.owner_uuid,
    )
    event = DisplayEvent(
        kind="published",
        receiver_id=destination.receiver_id,
        creation_operation_id=None,
        owner_uuid=handle.owner_uuid,
        connector=handle.source.connector,
        output_id=handle.source.kscreen_output_id,
        helper=handle.helper,
        handle=handle,
        diagnostic=None,
    )
    assert accept_display(state, event)
    assert not accept_display(state, replace(event, owner_uuid=virtual_handle(owner=2).owner_uuid))
    assert not accept_display(state, replace(event, helper=type(handle.helper)(pid=handle.helper.pid, start_ticks=0)))
    assert not accept_display(
        state,
        replace(event, connector=None, output_id=None, helper=None, handle=None),
    )


def test_display_creation_event_requires_the_registered_operation() -> None:
    destination = target()
    handle = virtual_handle()
    operation = OperationId(UUID(int=55))
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        mode=Mode.VIRTUAL,
        display=DisplayState.Creating,
        operation_id=operation,
    )
    event = DisplayEvent(
        kind="reserved",
        receiver_id=destination.receiver_id,
        creation_operation_id=operation,
        owner_uuid=handle.owner_uuid,
        connector=handle.source.connector,
        output_id=handle.source.kscreen_output_id,
        helper=None,
        handle=None,
        diagnostic=None,
    )
    assert accept_display(state, event)
    assert not accept_display(state, replace(event, creation_operation_id=OperationId(UUID(int=56))))


def test_challenge_fence_rejects_a_challenge_after_target_replacement() -> None:
    destination = target()
    challenge = ChallengeView(
        challenge_id=ChallengeId(UUID(int=20)),
        receiver_id=destination.receiver_id,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        endpoint_generation=destination.endpoint_generation,
        operation_id=OperationId(UUID(int=21)),
        session_generation=3,
        trust_generation=2,
        created_at=0,
        expires_at=120,
        credential_kind="pin",
    )
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=target(endpoint_generation=2),
        challenge=challenge,
        session_generation=3,
        trust_generation=2,
    )
    assert not accept_challenge(state, challenge)


@pytest.mark.parametrize(
    "changes",
    [
        {"endpoint_fingerprint": "wrong-endpoint"},
        {"endpoint_generation": 2},
        {"session_generation": 2},
        {"trust_generation": 3},
        {"challenge_id": ChallengeId(UUID(int=99))},
        {"operation_id": OperationId(UUID(int=99))},
    ],
)
def test_challenge_requires_every_current_trust_token(changes) -> None:
    destination = target()
    challenge = ChallengeView(
        challenge_id=ChallengeId(UUID(int=20)), receiver_id=destination.receiver_id,
        endpoint_fingerprint=destination.endpoint_fingerprint, endpoint_generation=1,
        operation_id=OperationId(UUID(int=21)), session_generation=3, trust_generation=2,
        created_at=0, expires_at=120, credential_kind="pin",
    )
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True), target=destination,
        challenge=challenge, session_generation=3, trust_generation=2,
    )
    assert accept_challenge(state, challenge)
    assert not accept_challenge(state, replace(challenge, **changes))


@pytest.mark.parametrize("kind", [
    TransportEventKind.audio_recovered, TransportEventKind.mute_result,
    TransportEventKind.volume_result,
])
def test_audio_results_use_their_pending_operation_without_staling_stream_notices(kind) -> None:
    destination = target()
    handle = stream()
    audio_id = OperationId(UUID(int=60))
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True), target=destination,
        stream=handle, transport_lease_id=handle.lease_id, session_generation=1,
        audio=AudioState.Enabled, audio_operation_id=audio_id,
        operation_id=OperationId(UUID(int=61)),
    )
    ready = transport_ready(state, handle)
    assert accept_transport(state, ready)
    audio = replace(
        ready, kind=kind,
        context=replace(ready.context, operation_id=audio_id, audio_operation_id=audio_id),
    )
    assert accept_transport(state, audio)
    assert not accept_transport(
        state,
        replace(audio, context=replace(audio.context, session_generation=0)),
    )
    assert not accept_transport(
        state,
        replace(audio, context=replace(audio.context, audio_operation_id=OperationId(UUID(int=59)))),
    )
    assert not accept_transport(replace(state, audio_operation_id=None), audio)


def test_cleanup_accepts_an_exact_physical_capture_completion() -> None:
    destination = target()
    source = physical("DP-cleanup")
    event = EffectEvent(
        receiver_id=destination.receiver_id,
        operation_id=OperationId(UUID(int=70)),
        session_generation=1,
        kind="cleanup:capture",
        result=Ok(value=source),
        resource=source,
    )
    assert accept_cleanup((source,), event)
    assert not accept_cleanup((physical("DP-other"),), event)


def test_stream_ready_is_stale_after_endpoint_replacement_even_with_old_handle_retained() -> None:
    handle = stream()
    state = replace(
        SessionSnapshot.idle(handle.receiver_id, online=True), target=target(endpoint_generation=2),
        stream=handle, transport_lease_id=handle.lease_id, session_generation=1,
    )
    assert not accept_transport(state, transport_ready(state, handle))


def test_cleanup_rejects_resource_metadata_that_disagrees_with_the_exact_handle() -> None:
    handle = stream()
    state = replace(SessionSnapshot.idle(handle.receiver_id, online=True), session_generation=1)
    stopped = replace(transport_ready(state, handle), kind=TransportEventKind.stopped)
    assert not accept_cleanup((handle,), replace(stopped, daemon_instance_uuid=InstanceId(UUID(int=88))))
    assert not accept_cleanup((handle,), replace(stopped, receiver_id=target("b").receiver_id))


def test_owned_display_events_survive_endpoint_and_latest_operation_changes() -> None:
    destination = target(endpoint_generation=2)
    handle = virtual_handle()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True), target=destination,
        display=DisplayState.Owned, display_handle=handle, ownership_uuid=handle.owner_uuid,
        operation_id=OperationId(UUID(int=999)), session_generation=5,
    )
    event = DisplayEvent(
        kind="helper_exit", receiver_id=destination.receiver_id, creation_operation_id=OperationId(UUID(int=10)),
        owner_uuid=handle.owner_uuid, connector=handle.source.connector,
        output_id=handle.source.kscreen_output_id, helper=handle.helper, handle=None, diagnostic=None,
    )
    assert accept_display(state, event)
    assert accept_cleanup((handle,), event)
    assert not accept_display(state, replace(event, output_id="foreign-output"))
    assert not accept_display(state, replace(event, connector="foreign-connector"))
    assert not accept_cleanup((handle,), replace(event, output_id="foreign-output"))
    assert not accept_cleanup((handle,), replace(event, connector="foreign-connector"))
    assert not accept_cleanup((handle,), replace(event, helper=ProcessIdentity(pid=handle.helper.pid, start_ticks=1)))
