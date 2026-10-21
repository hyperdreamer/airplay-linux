import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.messages import RetryRevocation, Start, Stop
from airplay_linux.domain.operations import OperationId
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    AudioState,
    DisplayState,
    IntentStage,
    Mode,
    Ok,
    SessionPhase,
    StartRequest,
    TransportState,
    TrustState,
)
from airplay_linux.session.engine import Route
from airplay_linux.session.phase import derive_phase
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import physical, target


@pytest.mark.asyncio
async def test_stop_is_dispatched_while_an_effect_is_still_blocked() -> None:
    blocked: Deferred[object] = Deferred()
    seen: list[str] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                seen.append("start")
                ctx.apply(
                    intent_active=True,
                    intent_stage=IntentStage.active,
                    transport=TransportState.Connecting,
                )
                ctx.launch(
                    Route.mirror,
                    "blocked",
                    ctx.operation(),
                    blocked.wait,
                )
                return True
            return False

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                seen.append("stop")
                ctx.supersede("stop")
                ctx.apply(intent_active=False, intent_stage=IntentStage.stopping)
                return True
            return False

        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()

    start_receipt = await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(mode=Mode.MIRROR, source=physical()),
        )
    )
    stop_receipt = await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()

    assert start_receipt.accepted
    assert stop_receipt.accepted
    assert seen == ["start", "stop"]
    assert rig.manager.snapshot(destination.receiver_id).session_generation == 1
    assert start_receipt.operation_id != stop_receipt.operation_id
    assert not blocked.resolved

    blocked.resolve(Ok(value=None))
    await rig.settle()


@pytest.mark.asyncio
async def test_same_intent_command_does_not_increment_session_generation() -> None:
    seen_generations: list[int] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                seen_generations.append(ctx.state.session_generation)
                ctx.apply(intent_active=True, intent_stage=IntentStage.active)
                return True
            return False

        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()

    first = await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    second = await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    await rig.settle()

    assert first.accepted and second.accepted
    assert seen_generations == [0, 0]
    assert rig.manager.snapshot(destination.receiver_id).session_generation == 0


@pytest.mark.asyncio
async def test_commands_for_two_receivers_are_independent() -> None:
    seen: list[str] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                seen.append(str(message.receiver_id))
                ctx.apply(intent_active=True, intent_stage=IntentStage.active)
                return True
            return False

        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    first_target = target("a")
    second_target = target("b", address="192.0.2.11")
    rig.discovery.add_target(first_target)
    rig.discovery.add_target(second_target)
    await rig.settle()

    first = await rig.manager.dispatch(
        Start(receiver_id=first_target.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    second = await rig.manager.dispatch(
        Start(receiver_id=second_target.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical("HDMI-1")))
    )
    await rig.settle()

    assert first.accepted and second.accepted
    assert first.operation_id != second.operation_id
    assert seen == [str(first_target.receiver_id), str(second_target.receiver_id)]
    assert rig.manager.snapshot(first_target.receiver_id).intent_active
    assert rig.manager.snapshot(second_target.receiver_id).intent_active


def test_phase_is_derived_and_manual_phase_cannot_contradict_resources() -> None:
    rig = SessionRig()
    destination = target()
    rig.discovery.add_target(destination)
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.phase is SessionPhase.Available

    context = rig.manager.context(destination.receiver_id)
    state = context.apply(
        transport=TransportState.Connecting,
        phase=SessionPhase.Available,
    )
    assert state.phase is SessionPhase.Connecting




@pytest.mark.asyncio
async def test_discovery_updates_online_target_without_a_reconnect_handler() -> None:
    rig = SessionRig()
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()

    online = rig.manager.snapshot(destination.receiver_id)
    assert online.online
    assert online.target is not None
    assert online.target.endpoint_fingerprint == destination.endpoint_fingerprint

    rig.discovery.remove_target(destination.receiver_id)
    await rig.settle()
    offline = rig.manager.snapshot(destination.receiver_id)
    assert not offline.online
    assert offline.target is not None
    assert offline.target.endpoint_fingerprint == destination.endpoint_fingerprint

    refreshed = target(endpoint_generation=2)
    rig.discovery.add_target(refreshed)
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).target.endpoint_generation == 2


def test_recovery_snapshot_is_installable_before_the_first_command() -> None:
    rig = SessionRig()
    destination = target()
    recovered = replace(
        SessionSnapshot.idle(destination.receiver_id, online=True),
        target=destination,
        transport=TransportState.Connecting,
    )
    rig.manager.install(recovered)
    assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.Connecting

    with pytest.raises(RuntimeError):
        rig.manager.install(recovered)


def test_manager_bootstraps_an_existing_offline_discovery_view_as_offline() -> None:
    from airplay_linux.session.engine import SessionManager

    rig = SessionRig()
    destination = target()
    rig.discovery.add_target(destination)
    rig.discovery.remove_target(destination.receiver_id)
    manager = SessionManager(rig.deps, {})
    state = manager.snapshot(destination.receiver_id)
    assert not state.online
    assert state.target is not None
    assert state.target.endpoint_fingerprint == destination.endpoint_fingerprint


@pytest.mark.asyncio
async def test_completed_stale_acquisition_is_routed_to_cleanup_and_remains_pending() -> None:
    blocked: Deferred[object] = Deferred()
    seen_routes: list[Route] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "capture", ctx.operation(), blocked.wait, acquisition=True)
                return True
            return False

        def cleanup(ctx, message):
            seen_routes.append(Route.cleanup)
            if isinstance(message, Stop):
                ctx.supersede("stop")
            return True

        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()
    await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()

    acquired = physical("DP-2")
    blocked.resolve(Ok(value=acquired))
    await rig.settle()

    assert seen_routes
    assert rig.manager.pending_resources(destination.receiver_id) == (acquired,)
    assert rig.manager.context(destination.receiver_id).outstanding_acquisitions() == 0


@pytest.mark.asyncio
async def test_accepted_acquisition_is_removed_from_pending_ledger_by_handler() -> None:
    blocked: Deferred[object] = Deferred()

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "capture", ctx.operation(), blocked.wait, acquisition=True)
            elif hasattr(message, "resource") and message.resource is not None:
                ctx.apply(physical_source=message.resource)
            return True

        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()
    await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    acquired = physical("DP-3")
    blocked.resolve(Ok(value=acquired))
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == ()


@pytest.mark.asyncio
async def test_non_acquisition_effects_do_not_count_as_outstanding_acquisitions() -> None:
    blocked: Deferred[object] = Deferred()

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "control", ctx.operation(), blocked.wait)
            return True

        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    context = rig.manager.context(destination.receiver_id)
    context.launch(Route.mirror, "control", context.operation(), blocked.wait)
    assert context.outstanding_acquisitions() == 0
    blocked.resolve(Ok(value=None))
    await rig.settle()


def test_context_audio_token_is_carried_until_cancel_or_supersede() -> None:
    rig = SessionRig()
    destination = target()
    context = rig.manager.context(destination.receiver_id)
    audio_id = OperationId(UUID(int=77))
    context.apply(audio_operation_id=audio_id)
    assert context.operation().audio_operation_id == audio_id

    context.cancel_audio()
    assert context.operation().audio_operation_id is None
    context.apply(audio_operation_id=audio_id)
    context.supersede("reconfigure")
    assert context.operation().audio_operation_id is None


@pytest.mark.asyncio
async def test_failed_acquisition_does_not_enter_pending_resource_ledger() -> None:
    source = physical("DP-failed")

    async def fail() -> object:
        raise RuntimeError("capture failed")

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(
                    Route.mirror,
                    "capture",
                    ctx.operation(),
                    fail,
                    acquisition=True,
                    resource=source,
                )
            return True

        return {Route.mirror: mirror}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()
    await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=source))
    )
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == ()


def test_context_allocates_operations_without_advancing_generation_until_supersede() -> None:
    rig = SessionRig()
    destination = target()
    rig.discovery.add_target(destination)
    context = rig.manager.context(destination.receiver_id)
    first = context.operation()
    second = context.operation()
    assert first.operation_id != second.operation_id
    assert first.session_generation == second.session_generation == 0

    leased = context.new_transport_lease(first)
    assert leased.transport_lease_id is not None
    superseding = context.supersede("reconfigure")
    assert superseding.session_generation == 1
    assert superseding.transport_lease_id is None
    assert superseding.audio_operation_id is None
    assert context.state.session_generation == 1
    assert context.state.transport_lease_id is None
    assert context.operation().session_generation == 1


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"online": False}, SessionPhase.Offline),
        ({"online": True}, SessionPhase.Available),
        ({"online": True, "transport": TransportState.Failed}, SessionPhase.Failed),
        ({"online": True, "transport": TransportState.Streaming, "mode": Mode.MIRROR}, SessionPhase.Mirroring),
        ({"online": True, "transport": TransportState.Streaming, "mode": Mode.VIRTUAL}, SessionPhase.VirtualDisplay),
        ({"online": True, "transport": TransportState.Connecting}, SessionPhase.Connecting),
        ({"online": True, "transport": TransportState.Reconnecting}, SessionPhase.Reconnecting),
        ({"online": True, "display": DisplayState.Creating}, SessionPhase.CreatingDisplay),
        ({"online": True, "trust": TrustState.Pairing}, SessionPhase.Pairing),
        ({"online": True, "intent_stage": IntentStage.recovering}, SessionPhase.Recovering),
        ({"online": True, "intent_stage": IntentStage.audio_decision}, SessionPhase.AudioDecisionRequired),
        ({"online": True, "intent_stage": IntentStage.resolution}, SessionPhase.ResolutionRequired),
        ({"online": True, "trust": TrustState.CleanupRequired, "transport": TransportState.Streaming}, SessionPhase.TrustCleanupRequired),
        ({"online": True, "intent_stage": IntentStage.reconfiguring}, SessionPhase.Reconfiguring),
        ({"online": True, "intent_stage": IntentStage.stopping}, SessionPhase.Stopping),
        ({"online": True, "cleanup_pending": True, "intent_stage": IntentStage.stopping}, SessionPhase.CleanupRequired),
    ],
)
def test_phase_precedence_keeps_unresolved_resources_visible(changes, expected) -> None:
    destination = target()
    state = replace(SessionSnapshot.idle(destination.receiver_id), target=destination, **changes)
    assert derive_phase(state) is expected


@pytest.mark.asyncio
async def test_retry_revocation_has_a_trust_route() -> None:
    seen: list[object] = []

    def handlers(_deps):
        def trust(_ctx, message):
            seen.append(message)
            return True

        return {Route.trust: trust}

    rig = SessionRig(handlers)
    destination = target()
    receipt = await rig.manager.dispatch(RetryRevocation(receiver_id=destination.receiver_id))
    await rig.settle()
    assert receipt.accepted
    assert len(seen) == 1


def test_rejected_command_does_not_block_recovery_installation() -> None:
    rig = SessionRig()
    destination = target()
    receipt = asyncio.run(rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    )))
    assert not receipt.accepted
    recovered = replace(SessionSnapshot.idle(destination.receiver_id, online=True), target=destination)
    rig.manager.install(recovered)


@pytest.mark.asyncio
async def test_shutdown_runs_hook_and_keeps_late_acquisition_releasable() -> None:
    blocked: Deferred[object] = Deferred()
    seen: list[str] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "capture", ctx.operation(), blocked.wait, acquisition=True)
            return True

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                seen.append("stop")
                ctx.supersede("shutdown")
            else:
                seen.append("late-completion")
            return True

        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await rig.settle()
    async def hook():
        seen.append("hook")
        return Ok(value=None)

    rig.manager.set_shutdown_hook(hook)
    result = await rig.manager.shutdown()
    assert isinstance(result, Ok)
    assert rig.transport.shutdown_called
    assert seen[:2] == ["stop", "hook"]

    acquired = physical("DP-late")
    blocked.resolve(Ok(value=acquired))
    await rig.settle()
    assert seen[-1] == "late-completion"
    assert rig.manager.pending_resources(destination.receiver_id) == (acquired,)


@pytest.mark.asyncio
async def test_shutdown_removes_client_subscribers_before_late_effect_delivery() -> None:
    blocked: Deferred[object] = Deferred()
    updates: list[SessionSnapshot] = []

    def handlers(_deps):
        def mirror(ctx, message):
            if isinstance(message, Start):
                ctx.launch(Route.mirror, "capture", ctx.operation(), blocked.wait, acquisition=True)
            return True

        def cleanup(ctx, message):
            if isinstance(message, Stop):
                ctx.supersede("shutdown")
            return True

        return {Route.mirror: mirror, Route.cleanup: cleanup}

    rig = SessionRig(handlers)
    destination = target()
    rig.discovery.add_target(destination)
    await rig.settle()
    rig.manager.subscribe(updates.append)
    await rig.manager.dispatch(
        Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical()))
    )
    await rig.settle()
    await rig.manager.shutdown()
    count_after_shutdown = len(updates)
    blocked.resolve(Ok(value=physical("DP-late-subscription")))
    await rig.settle()
    assert len(updates) == count_after_shutdown


@pytest.mark.asyncio
async def test_shutdown_closes_subscriptions_and_rejects_new_commands() -> None:
    rig = SessionRig()
    destination = target()
    updates: list[object] = []
    subscription = rig.manager.subscribe(updates.append)
    result = await rig.manager.shutdown()
    assert isinstance(result, Ok)
    before = len(updates)
    rig.discovery.add_target(destination)
    await rig.settle()
    assert len(updates) == before
    receipt = await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    assert not receipt.accepted
    subscription.close()
