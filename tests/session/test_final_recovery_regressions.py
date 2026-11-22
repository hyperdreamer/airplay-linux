import pytest

from airplay_linux.domain.messages import DismissCredentialRecovery, Stop, StopCredentialRecovery
from airplay_linux.domain.relay import NoReply, Submitted
from airplay_linux.testing.adapters import Deferred
from airplay_linux.domain.resources import Ok
from airplay_linux.session.recovery import build_recovery_handlers
from test_lifecycle_regressions import (
    credential_rig, credential_status, handoff_pending, settle_resources, submit_credential,
)


@pytest.mark.asyncio
async def test_unknown_outcome_runs_automatic_late_target_cleanup(tmp_path):
    h = await credential_rig(tmp_path, installed=True)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    calls = []

    async def stop_recovery_target(marker, target, context, fence):
        assert fence() == Ok(value=None)
        calls.append((marker.target, marker.marker_generation))
        return Ok(value=None)

    h.registry.stop_recovery_target = stop_recovery_target
    await submit_credential(h, Ok(value=NoReply(reason='command_timeout')))
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    marker = h.coordinator.markers[0]
    assert marker.state == 'release_pending'
    assert marker.automatic_status_checks is True
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    # Positive control proves that the composed recovery handler can reach this seam.
    await h.rig.manager.dispatch(StopCredentialRecovery(
        receiver_id=h.destination.receiver_id, target=marker.target,
        marker_generation=marker.marker_generation,
    ))
    await settle_resources(h.rig)
    assert calls == [(marker.target, marker.marker_generation)]
    calls.clear()
    for _ in range(10):
        h.rig.scheduler.advance(4.0)
        await settle_resources(h.rig)
    assert calls, 'automatic_status_checks=True must schedule recovery after ordinary Stop'


@pytest.mark.asyncio
@pytest.mark.parametrize('marker_state', ['watching', 'release_pending'])
async def test_restart_restores_periodic_checks_for_retained_marker(tmp_path, marker_state):
    import asyncio
    from dataclasses import replace
    from airplay_linux.process.ownership import DaemonRecoveryExemption
    from airplay_linux.session.engine import SessionManager
    from airplay_linux.session.recovery import CredentialRecoveryCoordinator

    h = await credential_rig(tmp_path, installed=True)
    if marker_state == 'watching':
        created = await h.coordinator.begin_submission_for(
            h.destination.receiver_id, h.destination, h.event.context.operation_id,
        )
        assert isinstance(created, Ok)
    else:
        await submit_credential(h, Ok(value=NoReply(reason='command_timeout')))
    marker = h.coordinator.markers[0]
    assert marker.state == marker_state
    calls = []

    async def authenticate(restored):
        assert restored == marker
        return Ok(value=DaemonRecoveryExemption(
            marker_identity=restored, marker_generation=restored.marker_generation,
            kind='watching_original', daemon_identity=restored.original_daemon,
        ))

    async def stop_recovery_target(restored, target, context, fence):
        assert restored == marker
        assert fence() == Ok(value=None)
        assert context.owner_daemon == marker.original_daemon
        assert context.marker_generation == marker.marker_generation
        assert target.receiver_id == marker.receiver_id
        assert target.endpoint_generation == marker.original_daemon.endpoint_generation
        calls.append(target.canonical_host)
        return Ok(value=None)

    h.registry.authenticate_recovery = authenticate
    h.registry.stop_recovery_target = stop_recovery_target
    restored = CredentialRecoveryCoordinator(h.store, h.registry, fences=h.rig.fences)
    deps = replace(h.rig.deps, credential_recovery=restored)
    manager = SessionManager(deps, build_recovery_handlers(deps))
    assert await restored.load_before_startup() == Ok(value=None)
    exemptions = await restored.recover_before_registry_reconciliation()
    assert isinstance(exemptions, Ok) and len(exemptions.value) == 1
    restored._startup_completed()
    try:
        for interval in range(1, 4):
            h.rig.scheduler.advance(4.0)
            for _ in range(20):
                await asyncio.sleep(0)
                await manager.drain()
            assert calls == [marker.target] * interval
            assert restored.markers == (marker,)
            assert await h.store.for_target(marker.target) == Ok(value=marker)

        dismissed = await restored.dismiss_for(marker, marker.marker_generation)
        assert isinstance(dismissed, Ok)
        assert dismissed.value.automatic_status_checks is False
        h.rig.scheduler.advance(12.0)
        for _ in range(20):
            await asyncio.sleep(0)
            await manager.drain()
        assert calls == [marker.target] * 3
        assert restored.markers == (dismissed.value,)
    finally:
        restored.stop_automatic_checks()


@pytest.mark.asyncio
@pytest.mark.parametrize('dismiss_before_completion', [False, True], ids=['enabled', 'dismissed'])
async def test_restored_checks_resume_after_release_advances_inflight_generation(
    tmp_path, dismiss_before_completion,
):
    import asyncio
    from dataclasses import replace
    from pathlib import Path

    from airplay_linux.diagnostics import diagnostic
    from airplay_linux.domain.messages import RestartReceiverToRelease
    from airplay_linux.domain.resources import Err, ErrorCode
    from airplay_linux.process.ownership import DaemonRecoveryExemption
    from airplay_linux.session.engine import SessionManager
    from airplay_linux.session.recovery import CredentialRecoveryCoordinator, StartupRecovery
    from airplay_linux.session.testing import SessionRig

    h = await credential_rig(tmp_path, installed=True)
    created = await h.coordinator.begin_submission_for(
        h.destination.receiver_id, h.destination, h.event.context.operation_id,
    )
    assert isinstance(created, Ok)
    pending = await h.coordinator.mark_release_pending_for(created.value[0], 1)
    assert isinstance(pending, Ok)
    marker = pending.value
    h.coordinator.stop_automatic_checks()

    async def authenticate(restored_marker):
        assert restored_marker == marker
        return Ok(value=DaemonRecoveryExemption(
            marker_identity=restored_marker,
            marker_generation=restored_marker.marker_generation,
            kind='watching_original',
            daemon_identity=restored_marker.original_daemon,
        ))

    entered, release = asyncio.Event(), asyncio.Event()
    calls, authorized, stale_fences, restart_attempts = [], [], [], []

    async def stop_recovery_target(checked, target, context, fence):
        assert target.canonical_host == marker.target
        assert context.owner_daemon == marker.original_daemon
        assert context.marker_generation == checked.marker_generation
        assert fence() == Ok(value=None)
        calls.append(checked.marker_generation)
        if len(calls) == 1:
            entered.set()
            await release.wait()
            stale_fences.append(fence())
        else:
            assert fence() == Ok(value=None)
            authorized.append((target.canonical_host, checked.marker_generation))
        return Ok(value=None)

    async def restart_with_new_endpoint(*args):
        restart_attempts.append(args)
        return Err(error=diagnostic(ErrorCode.ownership_mismatch))

    async def reconcile_previous(*, recovery_exemptions):
        assert len(recovery_exemptions) == 1
        assert recovery_exemptions[0].marker_identity == marker
        return Ok(value=None)

    h.registry.authenticate_recovery = authenticate
    h.registry.stop_recovery_target = stop_recovery_target
    h.registry._configured_runtime = Path(tmp_path) / 'replacement'
    h.registry.authorize_recovery_attempt = lambda current, fence: fence()
    h.registry.restart_with_new_endpoint = restart_with_new_endpoint
    h.registry.reconcile_previous = reconcile_previous
    rig = SessionRig()
    restored = CredentialRecoveryCoordinator(h.store, h.registry, fences=rig.fences)
    deps = replace(rig.deps, credential_recovery=restored)
    manager = SessionManager(deps, build_recovery_handlers(deps))
    assert await StartupRecovery(manager, deps, h.registry).run() == Ok(value=())
    assert marker.receiver_id not in restored._automatic_dispatches

    try:
        rig.scheduler.advance(4.0)
        await asyncio.wait_for(entered.wait(), timeout=2)
        receipt = await manager.dispatch(RestartReceiverToRelease(
            receiver_id=marker.receiver_id,
            marker_generation=marker.marker_generation,
            target=marker.target,
        ))
        assert receipt.accepted
        for _ in range(30):
            await asyncio.sleep(0)
            await manager.drain()
        assert len(restart_attempts) == 1
        assert marker.receiver_id in restored._automatic_dispatches
        newer = restored.markers[0]
        assert newer.marker_generation == marker.marker_generation + 1
        assert newer.state == 'release_pending' and newer.automatic_status_checks
        assert newer.original_daemon == marker.original_daemon
        assert newer.release_attempt is not None
        assert await h.store.for_target(marker.target) == Ok(value=newer)
        assert isinstance(restored.require_start_allowed(h.destination, marker.start_operation_id), Err)

        rig.scheduler.advance(4.0)
        for _ in range(10):
            await asyncio.sleep(0)
            await manager.drain()
        assert calls == [marker.marker_generation]

        if dismiss_before_completion:
            dismissed = await restored.dismiss_for(newer, newer.marker_generation)
            assert isinstance(dismissed, Ok)
            newer = dismissed.value
            assert not newer.automatic_status_checks
            assert await h.store.for_target(marker.target) == Ok(value=newer)

        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(stale_fences) == 1 and isinstance(stale_fences[0], Err)
        for _ in range(3):
            rig.scheduler.advance(4.0)
            for _ in range(20):
                await asyncio.sleep(0)
                await manager.drain()
        expected_generations = [] if dismiss_before_completion else [newer.marker_generation] * 3
        assert calls == [marker.marker_generation, *expected_generations]
        assert authorized == [(marker.target, generation) for generation in expected_generations]
        assert restored.markers == (newer,)
        assert await h.store.for_target(marker.target) == Ok(value=newer)
    finally:
        release.set()
        restored.stop_automatic_checks()


@pytest.mark.asyncio
async def test_automatic_recovery_shutdown_fences_inflight_disconnect(tmp_path):
    import asyncio

    h = await credential_rig(tmp_path, installed=True)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    entered, resume = asyncio.Event(), asyncio.Event()
    dispatches = []

    async def stop_recovery_target(marker, target, context, fence):
        entered.set()
        await resume.wait()
        if fence() == Ok(value=None):
            dispatches.append(target.canonical_host)
        return Ok(value=None)

    h.registry.stop_recovery_target = stop_recovery_target
    await submit_credential(h, Ok(value=NoReply(reason='command_timeout')))
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    marker = h.coordinator.markers[0]
    await h.rig.manager.dispatch(StopCredentialRecovery(
        receiver_id=marker.receiver_id, target=marker.target,
        marker_generation=marker.marker_generation + 1,
    ))
    for _ in range(10):
        h.rig.scheduler.advance(4.0)
        await asyncio.sleep(0)
        await settle_resources(h.rig)
        if entered.is_set():
            break
    assert entered.is_set(), (h.coordinator._automatic_handles, h.coordinator._automatic_tasks,
                              h.coordinator._automatic_generations, h.coordinator.markers)
    h.coordinator.stop_automatic_checks()
    resume.set()
    await settle_resources(h.rig)
    assert dispatches == []


@pytest.mark.asyncio
@pytest.mark.parametrize('dismiss', [False, True])
async def test_warning_dismissal_does_not_strand_live_settlement(tmp_path, dismiss):
    h = await credential_rig(tmp_path, installed=True)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    reply = Deferred()
    await submit_credential(h, reply)
    marker = h.coordinator.markers[0]
    assert marker.state == 'watching'
    ctx = h.rig.manager.context(h.destination.receiver_id)
    settlement, = ctx.credential_settlements.values()
    assert settlement.phase == 'awaiting_reply'
    if dismiss:
        await h.rig.manager.dispatch(DismissCredentialRecovery(
            receiver_id=h.destination.receiver_id, target=marker.target,
            marker_generation=marker.marker_generation,
        ))
        await settle_resources(h.rig)
        assert h.coordinator.markers[0].state == 'release_pending'
        assert h.coordinator.markers[0].marker_generation == marker.marker_generation + 1
    h.rig.transport.queue_result('read_credential_status', credential_status(h))
    reply.resolve(Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    await settle_resources(h.rig)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(40.0)
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending, (
        'dismissing recovery must preserve completion settlement and bounded Stop'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('installed', [False, True], ids=['pending', 'installed'])
@pytest.mark.parametrize('stage', ['command', 'status'])
async def test_warning_dismissal_preserves_settlement_across_stop(tmp_path, installed, stage):
    h = await credential_rig(tmp_path, installed=installed)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    if not installed:
        h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    reply = Deferred()
    status = Deferred()
    h.rig.transport.queue_result('read_credential_status', status)
    submitted = Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None))
    await submit_credential(h, reply if stage == 'command' else submitted)
    ctx = h.rig.manager.context(h.destination.receiver_id)
    settlement, = ctx.credential_settlements.values()
    assert settlement.phase == ('awaiting_reply' if stage == 'command' else 'observing')
    marker = h.coordinator.markers[0]
    h.rig.transport.queue_result('record_credential_marker_generation', Ok(value=None))
    await h.rig.manager.dispatch(DismissCredentialRecovery(
        receiver_id=marker.receiver_id, target=marker.target,
        marker_generation=marker.marker_generation,
    ))
    await settle_resources(h.rig)
    assert h.coordinator.markers[0].automatic_status_checks is False
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    if stage == 'command':
        reply.resolve(submitted)
        await settle_resources(h.rig)
    status.resolve(credential_status(h))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(40.0)
    await settle_resources(h.rig)
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    assert not state.cleanup_pending
    assert state.credential_prompt is None
    assert len(h.rig.transport.journal.calls('transport.stop_stream')) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_before_release', [False, True])
async def test_warning_dismissal_serializes_completion_while_rebinding(tmp_path, stop_before_release):
    h = await credential_rig(tmp_path, installed=True)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    reply = Deferred()
    await submit_credential(h, reply)
    marker = h.coordinator.markers[0]
    rebind = Deferred()
    h.rig.transport._scripted['record_credential_marker_generation'].clear()
    h.rig.transport.queue_result('record_credential_marker_generation', rebind)
    h.rig.transport.queue_result('read_credential_status', credential_status(h))
    await h.rig.manager.dispatch(DismissCredentialRecovery(
        receiver_id=marker.receiver_id, target=marker.target,
        marker_generation=marker.marker_generation,
    ))
    await settle_resources(h.rig)
    assert h.coordinator.markers[0].marker_generation == marker.marker_generation + 1
    assert len(h.rig.transport.journal.calls('transport.record_credential_marker_generation')) == 1
    assert not rebind.resolved
    reply.resolve(Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    await settle_resources(h.rig)
    if stop_before_release:
        await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
        await settle_resources(h.rig)
    rebind.resolve(Ok(value=None))
    await settle_resources(h.rig)
    if not stop_before_release:
        await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(40.0)
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_warning_dismissal_failed_transport_rebind_preserves_settlement(tmp_path):
    from airplay_linux.diagnostics import diagnostic
    from airplay_linux.domain.resources import Err, ErrorCode

    h = await credential_rig(tmp_path, installed=True)
    h.rig.manager.handlers.update(build_recovery_handlers(h.rig.deps))
    reply = Deferred()
    await submit_credential(h, reply)
    marker = h.coordinator.markers[0]
    h.rig.transport._scripted['record_credential_marker_generation'].clear()
    h.rig.transport.queue_result('record_credential_marker_generation', Err(
        error=diagnostic(ErrorCode.stale_event),
    ))
    await h.rig.manager.dispatch(DismissCredentialRecovery(
        receiver_id=marker.receiver_id, target=marker.target,
        marker_generation=marker.marker_generation,
    ))
    await settle_resources(h.rig)
    assert h.coordinator.markers[0].marker_generation == marker.marker_generation + 1
    h.rig.transport.queue_result('read_credential_status', credential_status(h))
    reply.resolve(Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    await settle_resources(h.rig)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(40.0)
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
@pytest.mark.parametrize('two_targets', [False, True])
async def test_recovery_command_identifies_exact_marker_across_address_changes(tmp_path, two_targets):
    from dataclasses import replace
    from uuid import UUID
    from airplay_linux.domain.operations import OperationId
    from airplay_linux.domain.records import CredentialRecoveryMarker, OriginalDaemonIdentity, SessionSnapshot
    h = await credential_rig(tmp_path, installed=True)
    identity = OriginalDaemonIdentity(
        launch_uuid=h.registry.receipt.launch_uuid,
        instance_uuid=h.registry.record.instance_uuid,
        endpoint_generation=h.registry.receipt.endpoint_generation,
        daemon_process=h.registry.record.daemon,
        runtime_directory=h.registry.record.runtime_directory,
        socket_path=h.registry.record.socket_path,
    )
    old = CredentialRecoveryMarker(
        schema_version=1, target=h.destination.canonical_host,
        receiver_id=h.destination.receiver_id, start_operation_id=h.event.context.operation_id,
        original_daemon=identity, marker_generation=1, state='watching',
        automatic_status_checks=True, release_attempt=None, retirement=None,
    )
    if two_targets:
        assert await h.coordinator.begin_submission(old) == Ok(value=old)
        assert isinstance(await h.coordinator.mark_release_pending_for(old, 1), Ok)
    new_target = replace(
        h.destination, canonical_host='192.0.2.20', endpoint_generation=2,
        endpoint_fingerprint='endpoint-a-v2',
    )
    new = replace(
        old, target=new_target.canonical_host, start_operation_id=OperationId(UUID(int=19991)),
        marker_generation=1, state='watching', automatic_status_checks=True,
        release_attempt=None, retirement=None,
    )
    assert await h.coordinator.begin_submission(new) == Ok(value=new)
    assert isinstance(await h.coordinator.mark_release_pending_for(new, 1), Ok)
    state = replace(SessionSnapshot.idle(h.destination.receiver_id), target=new_target, online=True)
    from airplay_linux.session.engine import SessionManager
    h.rig.manager = SessionManager(h.rig.deps, build_recovery_handlers(h.rig.deps))
    h.rig.manager.install(state)
    calls = []
    async def stop_recovery_target(marker, target, context, fence):
        from airplay_linux.diagnostics import diagnostic
        from airplay_linux.domain.resources import Err, ErrorCode
        calls.append(marker.target)
        assert fence() == Ok(value=None)
        if two_targets:
            assert isinstance(await h.coordinator.dismiss_for(marker, marker.marker_generation), Ok)
            return Err(error=diagnostic(ErrorCode.transport_failed))
        return Ok(value=None)
    h.registry.stop_recovery_target = stop_recovery_target
    await h.rig.manager.dispatch(StopCredentialRecovery(
        receiver_id=h.destination.receiver_id, target=old.target if two_targets else new.target,
        marker_generation=2,
    ))
    await settle_resources(h.rig)
    assert calls == [old.target if two_targets else new_target.canonical_host], (
        'target-keyed recovery must distinguish records sharing receiver ID and generation'
    )
    if two_targets:
        assert h.rig.manager.snapshot(h.destination.receiver_id).last_error is None
