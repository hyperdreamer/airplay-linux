"""Identity fencing for asynchronous session resource events."""

from __future__ import annotations

from collections.abc import Iterable

from airplay_linux.domain.messages import (
    DisplayEvent,
    EffectEvent,
    ResourceEvent,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import ChallengeView, DisplayHandle, StreamHandle
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import PhysicalMonitorSource


def _same_receiver(state: SessionSnapshot, receiver_id) -> bool:
    return state.receiver_id == receiver_id


def _same_stream(left: StreamHandle | None, right: StreamHandle | None) -> bool:
    if left is None or right is None:
        return False
    return (
        left.receiver_id == right.receiver_id
        and left.endpoint_generation == right.endpoint_generation
        and left.endpoint_fingerprint == right.endpoint_fingerprint
        and left.daemon_instance_uuid == right.daemon_instance_uuid
        and left.lease_id == right.lease_id
        and left.backend_stream_id == right.backend_stream_id
        and left.start_operation_id == right.start_operation_id
    )


def _same_display(left: DisplayHandle | None, event: DisplayEvent) -> bool:
    if left is None or event.owner_uuid != left.owner_uuid:
        return False
    if event.handle is not None and (
        event.handle.owner_uuid != left.owner_uuid
        or event.handle.source != left.source
        or event.handle.helper != left.helper
    ):
        return False
    if event.handle is None and (
        event.connector != left.source.connector
        or event.output_id != left.source.kscreen_output_id
        or event.helper != left.helper
    ):
        return False
    return (
        (event.connector is None or event.connector == left.source.connector)
        and (event.output_id is None or event.output_id == left.source.kscreen_output_id)
        and (event.helper is None or event.helper == left.helper)
    )


def _display_progress_matches(left: DisplayHandle, event: DisplayEvent) -> bool:
    if event.owner_uuid != left.owner_uuid:
        return False
    if event.handle is None and event.connector is None and event.output_id is None and event.helper is None:
        return False
    if event.handle is not None and not _same_display(left, event):
        return False
    if event.connector is not None and event.connector != left.source.connector:
        return False
    if event.output_id is not None and event.output_id != left.source.kscreen_output_id:
        return False
    if event.helper is not None and event.helper != left.helper:
        return False
    return True


def accept_transport(state: SessionSnapshot, event: TransportEvent) -> bool:
    """Accept an event only when its stream lease and start identity match.

    A completion from an old generation can still be accepted for an exact
    stream during cleanup.  The resource identity is stronger than the current
    command or session generation, which lets the manager release late output
    safely without allowing it to replace current state.
    """
    if not _same_receiver(state, event.receiver_id):
        return False
    stream = event.stream
    if stream is None or not _same_stream(state.stream, stream):
        return False
    stopped = event.kind is TransportEventKind.stopped
    audio_result = event.kind in {
        TransportEventKind.audio_start_failed,
        TransportEventKind.audio_failed,
        TransportEventKind.audio_recovered,
        TransportEventKind.mute_result,
        TransportEventKind.volume_result,
    }
    if stopped:
        if state.transport_lease_id is not None and state.transport_lease_id != stream.lease_id:
            return False
    elif state.transport_lease_id != stream.lease_id:
        return False
    if event.context.transport_lease_id != stream.lease_id:
        return False
    if event.context.endpoint_generation != stream.endpoint_generation:
        return False
    if audio_result:
        if state.audio_operation_id is None:
            return False
        if event.context.audio_operation_id != state.audio_operation_id:
            return False
    elif not stopped and event.context.operation_id != state.stream.start_operation_id:
        return False
    if event.daemon_instance_uuid != stream.daemon_instance_uuid:
        return False
    if event.endpoint_generation != stream.endpoint_generation:
        return False
    if event.endpoint_fingerprint != stream.endpoint_fingerprint:
        return False
    if not stopped:
        target = state.target
        if (
            target is None
            or target.endpoint_generation != stream.endpoint_generation
            or target.endpoint_fingerprint != stream.endpoint_fingerprint
        ):
            return False
        if event.context.session_generation != state.session_generation:
            return False
    return True


def accept_display(state: SessionSnapshot, event: DisplayEvent) -> bool:
    """Accept display progress using the registered operation or exact owner."""
    if not _same_receiver(state, event.receiver_id):
        return False
    current = state.display_handle
    if current is None:
        if state.ownership_uuid is not None and event.owner_uuid != state.ownership_uuid:
            return False
        return (
            event.kind == "reserved"
            and event.creation_operation_id is not None
            and state.operation_id == event.creation_operation_id
        )
    if state.ownership_uuid is not None and event.owner_uuid != state.ownership_uuid:
        return False
    return _display_progress_matches(current, event)


def accept_cleanup(owned: Iterable[StreamHandle | DisplayHandle], event: ResourceEvent) -> bool:
    """Return whether an asynchronous release identifies an owned resource."""
    if isinstance(event, TransportEvent):
        stream = event.stream
        if (
            stream is None
            or event.receiver_id != stream.receiver_id
            or event.daemon_instance_uuid != stream.daemon_instance_uuid
            or event.endpoint_generation != stream.endpoint_generation
            or event.endpoint_fingerprint != stream.endpoint_fingerprint
        ):
            return False
        return any(_same_stream(item, stream) for item in owned if isinstance(item, StreamHandle))
    if isinstance(event, DisplayEvent):
        return any(_same_display(item, event) for item in owned if isinstance(item, DisplayHandle))
    if isinstance(event, EffectEvent) and isinstance(event.resource, PhysicalMonitorSource):
        return any(item == event.resource for item in owned if isinstance(item, PhysicalMonitorSource))
    return False


def accept_challenge(state: SessionSnapshot, challenge: ChallengeView) -> bool:
    """Accept a pairing challenge only while its complete trust fence is current."""
    current = state.challenge
    if current is None:
        return False
    target = state.target
    if (
        target is None
        or target.endpoint_fingerprint != challenge.endpoint_fingerprint
        or target.endpoint_generation != challenge.endpoint_generation
    ):
        return False
    return (
        current.challenge_id == challenge.challenge_id
        and current.receiver_id == challenge.receiver_id == state.receiver_id
        and current.endpoint_fingerprint == challenge.endpoint_fingerprint
        and current.endpoint_generation == challenge.endpoint_generation
        and current.operation_id == challenge.operation_id
        and current.session_generation == challenge.session_generation == state.session_generation
        and current.trust_generation == challenge.trust_generation == state.trust_generation
    )


__all__ = ["accept_challenge", "accept_cleanup", "accept_display", "accept_transport"]
