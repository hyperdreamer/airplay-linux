"""Startup uses durable intent and the display provider's ownership proof."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import ConfirmOrphanRemoval, KeepCleanup, Retry, Start
from airplay_linux.domain.operations import InstanceId, LeaseId, OperationId, StreamHandle
from airplay_linux.domain.records import RecoveryItem, SessionSnapshot
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok,
    SessionPhase, StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import Route, SessionManager
from airplay_linux.session.reconnect import build_reconnect_handlers, compose_reconnect_handlers
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import context, target
from virtual_scenarios import decision, make_rig, production_display, settle, virtual_handlers


class Registry:
    def __init__(self, journal):
        self.journal = journal
        self.failure = None
        self.shutdowns = 0

    async def reconcile_previous(self):
        self.journal.append("registry.reconcile_previous")
        return self.failure or Ok(value=None)

    async def shutdown(self):
        self.shutdowns += 1
        self.journal.append("registry.shutdown")
        return Ok(value=None)


def install_handlers(rig):
    from airplay_linux.session.recovery import build_recovery_handlers

    for subscription in rig.manager._subscriptions:
        subscription.close()
    handlers = compose_reconnect_handlers(virtual_handlers(rig.deps), build_reconnect_handlers(rig.deps))
    rig.manager = SessionManager(rig.deps, {**handlers, **build_recovery_handlers(rig.deps)})
    return rig


async def prepared(tmp_path, *, online=False, stage="published", desired="active"):
    # The real display recovery proves ownership over a wholly fake compositor.
    display, process, store = production_display(tmp_path, verifier=True)
    rig = make_rig(display=display, ownerships=store)
    destination = target()
    created = await display.create(destination.receiver_id, destination.endpoint_fingerprint,
                                   decision().size, context())
    assert isinstance(created, Ok)
    handle = created.value
    original = store.records[handle.owner_uuid]
    store.records[handle.owner_uuid] = replace(original, stage=stage, desired_state=desired)
    stale = StreamHandle(
        receiver_id=destination.receiver_id, endpoint_generation=1,
        endpoint_fingerprint=destination.endpoint_fingerprint, backend_stream_id="old-stream",
        daemon_instance_uuid=InstanceId(UUID(int=99)), lease_id=LeaseId(UUID(int=98)),
        start_operation_id=OperationId(UUID(int=97)),
    )
    rig.sessions.saved[destination.receiver_id] = SessionSnapshot(
        receiver_id=destination.receiver_id, target=destination, online=True, mode=Mode.VIRTUAL,
        intent_active=True, intent_stage=IntentStage.active, transport=TransportState.Streaming,
        display=DisplayState.Owned, display_handle=handle, ownership_uuid=handle.owner_uuid,
        stream=stale, transport_lease_id=stale.lease_id, audio=AudioState.Muted, muted=True,
        trust=TrustState.Trusted, resolution=decision(), audio_policy=AudioPolicy(enabled=False),
        session_generation=7,
    )
    install_handlers(rig)
    if online:
        rig.discovery.add_target(destination)
    return rig, process, store, handle


async def recover(rig, registry=None):
    from airplay_linux.session.recovery import StartupRecovery

    registry = registry or Registry([])
    result = await StartupRecovery(rig.manager, rig.deps, registry).run()
    await settle(rig)
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["helper_started", "output_observed", "published"])
async def test_exact_output_is_reconstructed_by_stage_aware_provider_before_reconnect(tmp_path, stage):
    # Break: trusting the stored phase/stream or bypassing provider reconciliation.
    rig, process, store, handle = await prepared(tmp_path, stage=stage)
    result = await recover(rig)
    assert isinstance(result, Ok)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.display is DisplayState.Owned
    assert state.display_handle == handle
    assert state.intent_active
    assert state.phase is SessionPhase.Recovering
    assert not state.online
    assert state.target is None
    assert state.stream is None and state.transport_lease_id is None
    assert state.transport is not TransportState.Streaming
    assert state.audio is AudioState.Disabled and not state.muted
    assert state.trust is TrustState.Unknown
    assert store.records[handle.owner_uuid].stage == "published"
    assert process.identity(handle.helper.pid) == handle.helper
    assert not rig.journal.calls("transport.start_stream")


@pytest.mark.asyncio
async def test_reappearance_reconnects_same_intent_without_creating_an_output(tmp_path):
    rig, process, store, handle = await prepared(tmp_path)
    await recover(rig)
    generation = rig.manager.snapshot(rig.receiver_id).session_generation
    rig.discovery.add_target(target(endpoint_generation=2))
    await settle(rig)
    waiting = rig.manager.snapshot(rig.receiver_id)
    rig.scheduler.advance(waiting.next_retry_at - rig.clock.now())
    await settle(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.transport is TransportState.Streaming
    assert state.display_handle == handle
    assert state.session_generation == generation
    assert state.stream.backend_stream_id != "old-stream"
    assert len(store.records) == 1
    assert len(process.associations) == 1


@pytest.mark.asyncio
async def test_explicit_retry_uses_recovered_output_and_does_not_invent_pairing(tmp_path):
    rig, _, _, handle = await prepared(tmp_path, online=True)
    await recover(rig)
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    await settle(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.transport is TransportState.Streaming
    assert state.display_handle == handle
    assert not rig.journal.calls("pairing.begin_pairing")


@pytest.mark.asyncio
async def test_removing_record_wins_over_stored_streaming_intent(tmp_path):
    rig, process, store, handle = await prepared(tmp_path, desired="removing")
    result = await recover(rig)
    assert isinstance(result, Ok)
    state = rig.manager.snapshot(rig.receiver_id)
    assert not state.intent_active
    assert state.stream is None and state.display_handle is None
    assert state.display is DisplayState.None_
    assert not store.records
    assert process.identity(handle.helper.pid) is None
    assert not rig.journal.calls("transport.start_stream")


@pytest.mark.asyncio
async def test_uncertain_output_blocks_start_and_remains_durable(tmp_path):
    rig, process, store, handle = await prepared(tmp_path, online=True)
    process.associations.clear()  # no proof that this helper owns that connector
    await recover(rig)
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    await rig.manager.dispatch(Start(receiver_id=rig.receiver_id, request=StartRequest(mode=Mode.VIRTUAL)))
    await settle(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.cleanup_pending and state.display is DisplayState.CleanupRequired
    assert handle.owner_uuid in store.records
    assert process.identity(handle.helper.pid) == handle.helper
    assert not rig.journal.calls("transport.start_stream")


@pytest.mark.asyncio
async def test_orphan_keep_and_confirmation_preserve_evidence_when_provider_cannot_remove(tmp_path):
    rig, process, store, handle = await prepared(tmp_path)
    process._identities.pop(handle.helper.pid)
    process._environments.pop(handle.helper.pid)
    result = await recover(rig)
    assert result.value[0].kind == "orphan"
    await rig.manager.dispatch(KeepCleanup(receiver_id=rig.receiver_id))
    await rig.manager.dispatch(ConfirmOrphanRemoval(receiver_id=rig.receiver_id,
                               owner_uuid=handle.owner_uuid, confirmed=False))
    await settle(rig)
    assert handle.owner_uuid in store.records
    assert process.entries
    await rig.manager.dispatch(ConfirmOrphanRemoval(receiver_id=rig.receiver_id,
                               owner_uuid=handle.owner_uuid, confirmed=True))
    await settle(rig)
    assert handle.owner_uuid in store.records
    assert store.records[handle.owner_uuid].desired_state == "removing"
    assert process.entries
    assert rig.manager.snapshot(rig.receiver_id).cleanup_pending
    assert rig.manager.snapshot(rig.receiver_id).display is DisplayState.CleanupRequired


@pytest.mark.asyncio
async def test_prior_daemon_failure_blocks_display_reconstruction_and_replacement(tmp_path):
    rig, process, store, handle = await prepared(tmp_path)
    registry = Registry([])
    registry.failure = Err(error=diagnostic(ErrorCode.ownership_mismatch))
    result = await recover(rig, registry)
    assert isinstance(result, Err)
    assert handle.owner_uuid in store.records
    assert not rig.journal.calls("transport.start_stream")
    assert rig.manager.snapshot(rig.receiver_id).display is not DisplayState.Owned


@pytest.mark.asyncio
async def test_daemon_reconciliation_precedes_display_scan(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    order = []
    reconcile = rig.display.reconcile

    async def display_reconcile():
        order.append("display.reconcile")
        return await reconcile()

    rig.display.reconcile = display_reconcile
    await recover(rig, Registry(order))
    assert order[:2] == ["registry.reconcile_previous", "display.reconcile"]


@pytest.mark.asyncio
async def test_trust_tombstone_survives_restart_and_retries_revocation_without_challenge(tmp_path):
    rig, _, store, handle = await prepared(tmp_path, online=True)
    rig.sessions.saved[rig.receiver_id] = replace(rig.sessions.saved[rig.receiver_id], trust=TrustState.CleanupRequired)
    pending = Deferred()
    rig.pairing.queue_result("retry_revocation", pending)
    await recover(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.trust in (TrustState.CleanupRequired, TrustState.RevocationPending)
    assert state.challenge is None
    assert state.display_handle == handle
    assert rig.journal.calls("pairing.retry_revocation")[0]["receiver_id"] == rig.receiver_id
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    await settle(rig)
    assert not rig.journal.calls("transport.start_stream")
    pending.resolve(Err(error=diagnostic(ErrorCode.trust_cleanup_required)))
    await settle(rig)
    assert handle.owner_uuid in store.records


@pytest.mark.asyncio
async def test_unproven_persisted_handle_is_never_owned_without_provider_evidence(tmp_path):
    rig, process, store, handle = await prepared(tmp_path)
    process.associations.clear()
    await recover(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.display_handle is None
    assert state.display is DisplayState.CleanupRequired
    assert state.ownership_uuid == handle.owner_uuid
    assert handle.owner_uuid in store.records


@pytest.mark.asyncio
async def test_nonpersistable_identity_inherits_no_resolution_or_trust(tmp_path):
    rig, _, _, handle = await prepared(tmp_path, online=True)
    view = rig.discovery.views[rig.receiver_id]
    rig.discovery.views[rig.receiver_id] = replace(view, record_set=replace(
        view.record_set, persistable=False, ambiguous=True,
    ))
    await recover(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.display_handle == handle
    assert state.resolution is None
    assert state.trust is TrustState.Unknown
    assert state.challenge is None
    assert not rig.journal.calls("pairing.begin_pairing")


@pytest.mark.asyncio
async def test_unresolved_removing_record_resumes_ordered_cleanup(tmp_path):
    rig, _, store, handle = await prepared(tmp_path, desired="removing")
    remove = rig.display.remover.remove
    failures = []

    async def fail_first(owner, operation):
        if not failures:
            failures.append(owner)
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))
        return await remove(owner, operation)

    rig.display.remover.remove = fail_first
    await recover(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert failures == [handle.owner_uuid]
    assert not state.intent_active
    assert not state.cleanup_pending
    assert not store.records
    assert not rig.journal.calls("transport.start_stream")
