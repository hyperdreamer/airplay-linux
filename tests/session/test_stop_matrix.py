from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.messages import ConfirmSwitch, Start, Stop
from airplay_linux.domain.operations import DisplayHandle, InstanceId, OwnerId, ProcessIdentity, StreamHandle
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.testing.adapters import Deferred
from airplay_linux.domain.resources import (
    AudioState,
    DisplayState,
    IntentStage,
    Mode,
    Ok,
    StartRequest,
    TransportState,
    TrustState,
    VirtualOutputSource,
    Size,
)
from airplay_linux.session.cleanup import cleanup_complete
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.builders import physical, target


async def _start_mirror(rig: SessionRig, destination: object) -> None:
    completion: Deferred[object] = Deferred()
    rig.transport.queue_result("start_stream", completion)
    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(mode=Mode.MIRROR, source=physical()),
        )
    )
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    completion.resolve(
        Ok(
            value=StreamHandle(
                receiver_id=destination.receiver_id,
                endpoint_generation=destination.endpoint_generation,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                backend_stream_id="stream-test",
                daemon_instance_uuid=InstanceId(UUID(int=9)),
                lease_id=state.transport_lease_id,
                start_operation_id=state.operation_id,
            )
        )
    )
    await rig.settle()


@pytest.mark.asyncio
async def test_stop_has_ordered_transport_and_capture_cleanup_and_is_idempotent() -> None:
    rig = SessionRig(build_lifecycle_handlers)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.settle()
    await _start_mirror(rig, destination)

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()

    assert rig.display.journal.names().count("display.prepare_teardown") == 1
    assert rig.transport.journal.names().count("transport.stop_stream") == 1
    assert rig.capture.journal.names().count("capture.release") == 1
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.stream is None
    assert state.physical_source is None
    assert state.transport is TransportState.Idle
    assert state.audio is AudioState.Disabled
    assert not state.cleanup_pending


@pytest.mark.parametrize(
    ("transport", "display", "trust", "pending", "expected"),
    [
        (TransportState.Idle, DisplayState.None_, TrustState.Unknown, (), True),
        (TransportState.Failed, DisplayState.None_, TrustState.Unknown, (), False),
        (TransportState.Idle, DisplayState.CleanupRequired, TrustState.Unknown, (), False),
        (TransportState.Idle, DisplayState.None_, TrustState.CleanupRequired, (), True),
    ],
)
def test_cleanup_completion_requires_confirmed_resource_release(
    transport, display, trust, pending, expected,
) -> None:
    destination = target()
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        transport=transport,
        display=display,
        audio=AudioState.Disabled,
        trust=trust,
        intent_stage=IntentStage.stopping,
        cleanup_pending=not expected and display is DisplayState.CleanupRequired,
    )
    assert cleanup_complete(state, 0, pending) is expected


def test_cleanup_completion_rejects_an_unconfirmed_owned_handle() -> None:
    destination = target()
    handle = DisplayHandle(
        owner_uuid=OwnerId(1),
        source=VirtualOutputSource(
            connector="Virtual-v1", kscreen_output_id="v1", size=Size(width=1920, height=1080),
        ),
        helper=ProcessIdentity(pid=4100, start_ticks=9),
    )
    state = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        transport=TransportState.Idle,
        display=DisplayState.Removing,
        audio=AudioState.Disabled,
        display_handle=handle,
        ownership_uuid=handle.owner_uuid,
    )
    assert not cleanup_complete(state, 0, ())


@pytest.mark.asyncio
async def test_confirmed_switch_waits_for_cleanup_before_queued_mirror_start() -> None:
    rig = SessionRig(build_lifecycle_handlers)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.settle()
    await _start_mirror(rig, destination)
    before = rig.manager.snapshot(destination.receiver_id)

    request = StartRequest(mode=Mode.MIRROR, source=physical())
    await rig.manager.dispatch(
        ConfirmSwitch(
            receiver_id=destination.receiver_id,
            request=request,
            confirmed=False,
        )
    )
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id) == before

    completion: Deferred[object] = Deferred()
    rig.transport.queue_result("start_stream", completion)
    await rig.manager.dispatch(
        ConfirmSwitch(
            receiver_id=destination.receiver_id,
            request=request,
            confirmed=True,
        )
    )
    await rig.settle()
    restarting = rig.manager.snapshot(destination.receiver_id)
    assert restarting.stream is None
    assert restarting.intent_active
    assert restarting.transport is TransportState.Connecting
    assert rig.transport.journal.names().count("transport.stop_stream") == 1

    assert restarting.operation_id is not None
    assert restarting.transport_lease_id is not None
    completion.resolve(
        Ok(
            value=StreamHandle(
                receiver_id=destination.receiver_id,
                endpoint_generation=destination.endpoint_generation,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                backend_stream_id="queued-stream",
                daemon_instance_uuid=InstanceId(UUID(int=10)),
                lease_id=restarting.transport_lease_id,
                start_operation_id=restarting.operation_id,
            )
        )
    )
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).phase.value == "Mirroring"
    assert "display.create" not in rig.display.journal.names()
