"""Startup uses durable intent and the display provider's ownership proof."""
from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    ConfirmOrphanRemoval, KeepCleanup, Retry, Start, TransportEventKind,
)
from airplay_linux.domain.operations import (
    ChallengeId, InstanceId, LeaseId, OpaqueCredentialRef, OperationId, StreamHandle,
)
from airplay_linux.domain.records import RecoveryItem, SessionSnapshot
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok,
    SessionPhase, StartRequest, TransportState, TrustState, VolumeLevel,
)
from airplay_linux.session.engine import Route, SessionManager
from airplay_linux.session.reconnect import build_reconnect_handlers, compose_reconnect_handlers
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import context, target
from virtual_scenarios import (
    decision, make_rig, production_display, settle, stream_event, virtual_handlers,
)


def test_volume_transient_authority_is_stripped_before_custom_store():
    from airplay_linux.session.persistence import _safe_snapshot
    from airplay_linux.storage.codecs import encode_session, decode_session
    state = replace(SessionSnapshot.idle(target().receiver_id), audio=AudioState.Muted, muted=True,
                    remembered_volume=70, effective_volume=0, last_audible_volume=30,
                    volume_state="applied", audio_close_pending=True, volume_save_failed=True,
                    queued_start=StartRequest(mode=Mode.MIRROR, volume=VolumeLevel(value=30)))
    clean = _safe_snapshot(state)
    payload = encode_session(clean)
    decoded = decode_session(payload)
    assert clean.audio is AudioState.Disabled and not clean.muted
    assert clean.effective_volume is None and clean.last_audible_volume is None
    assert clean.remembered_volume is None and clean.volume_state is None
    assert not clean.audio_close_pending and not clean.volume_save_failed
    for field in (b"volume", b"audio_close_pending", b"last_audible_volume", b"volume_save_failed"):
        assert field not in payload
    assert decoded.queued_start.volume is None


@pytest.mark.asyncio
@pytest.mark.parametrize("version,continuous,invalid", [(1, True, False), (2, True, False),
                                                       (2, False, False), (2, True, True)])
async def test_volume_recovery_reloads_only_continuity_approved_receiver(tmp_path, version, continuous, invalid):
    import json
    from airplay_linux.storage import AtomicFiles, JsonReceiverStore, JsonSessionStore
    from airplay_linux.domain.records import SavedReceiver
    from airplay_linux.storage.codecs import encode_receiver
    from airplay_linux.session.recovery import StartupRecovery
    rig, _, _, _ = await prepared(tmp_path, online=True)
    receiver = target()
    saved = SavedReceiver(receiver_id=receiver.receiver_id, service_metadata=receiver.service_records,
                          display_name="TV", last_endpoint=receiver, continuity_fingerprint=receiver.endpoint_fingerprint,
                          credential_ref=None, remembered_resolution=None, remembered_volume=30)
    document = json.loads(encode_receiver(saved))
    document["version"] = version
    if version == 1:
        document.pop("remembered_volume")
    elif invalid:
        document["remembered_volume"] = 0
    files, warnings = AtomicFiles(tmp_path / "volume-records"), []
    receivers = JsonReceiverStore(files, on_diagnostic=warnings.append)
    await receivers.put(saved)
    name = files.names("receivers")[0]
    payload = json.dumps(document).encode()
    files.write("receivers/" + name, payload)
    old = rig.sessions.saved[rig.receiver_id]
    dirty_session = replace(old, remembered_volume=99, effective_volume=0,
                            last_audible_volume=80, volume_state="applied",
                            audio_close_pending=True, volume_save_failed=True,
                            queued_start=StartRequest(mode=Mode.MIRROR, volume=VolumeLevel(value=30)))
    session_files = AtomicFiles(tmp_path / "volume-sessions")
    sessions = JsonSessionStore(session_files)
    assert isinstance(await sessions.save(dirty_session), Ok)
    session_name = session_files.names("sessions")[0]
    session_payload = session_files.read("sessions/" + session_name)
    assert session_payload is not None
    assert b"audio_close_pending" not in session_payload and b"last_audible_volume" not in session_payload
    assert b"volume_save_failed" not in session_payload and b'"volume"' not in session_payload
    rig.sessions = sessions
    rig.deps = replace(rig.deps, sessions=sessions, receivers=receivers)
    if not continuous:
        view = rig.discovery.views[rig.receiver_id]
        rig.discovery.views[rig.receiver_id] = replace(view, record_set=replace(view.record_set, replacement_of=receiver.receiver_id))
    install_handlers(rig)
    result = await StartupRecovery(rig.manager, rig.deps, Registry([])).run()
    await settle(rig)
    assert isinstance(result, Ok)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.remembered_volume == (30 if version == 2 and continuous and not invalid else None)
    assert state.effective_volume is None and state.last_audible_volume is None and state.volume_state is None
    assert not state.audio_close_pending and not state.volume_save_failed and not state.muted
    assert state.audio_policy.enabled is False
    assert state.queued_start is None
    assert bool(warnings) is invalid
    assert session_files.read("sessions/" + session_name) == session_payload
    assert files.read("receivers/" + name) == payload


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
async def test_startup_recovery_restores_trust_only_from_credential_authority(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    from airplay_linux.session.recovery import StartupRecovery

    calls = []

    async def load_credential_ref(receiver_id):
        calls.append(receiver_id)
        return Ok(value=OpaqueCredentialRef("backend-owned-reference"))

    result = await StartupRecovery(
        rig.manager, rig.deps, Registry([]),
        credential_loader=load_credential_ref,
    ).run()
    await settle(rig)

    state = rig.manager.snapshot(rig.receiver_id)
    fence = rig.fences.get(rig.receiver_id)
    assert isinstance(result, Ok)
    assert calls == [rig.receiver_id]
    assert state.trust is TrustState.Trusted
    assert fence is not None
    assert fence.session_generation == state.session_generation
    assert fence.trust_generation == state.trust_generation
    assert fence.active_challenge_id is None
    assert fence.endpoint_fingerprint is None


@pytest.mark.asyncio
async def test_startup_recovery_does_not_synthesize_trust_without_credential_authority(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    from airplay_linux.session.recovery import StartupRecovery

    result = await StartupRecovery(rig.manager, rig.deps, Registry([])).run()
    await settle(rig)

    state = rig.manager.snapshot(rig.receiver_id)
    assert isinstance(result, Ok)
    assert state.trust is TrustState.Unknown
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.keyring_unavailable


@pytest.mark.asyncio
async def test_startup_recovery_keeps_trust_unknown_when_authority_has_no_reference(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    from airplay_linux.session.recovery import StartupRecovery

    calls = []

    async def load_credential_ref(receiver_id):
        calls.append(receiver_id)
        return Ok(value=None)

    result = await StartupRecovery(
        rig.manager, rig.deps, Registry([]), credential_loader=load_credential_ref,
    ).run()
    await settle(rig)

    state = rig.manager.snapshot(rig.receiver_id)
    assert isinstance(result, Ok)
    assert calls == [rig.receiver_id]
    assert state.trust is TrustState.Unknown
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.keyring_unavailable


@pytest.mark.asyncio
async def test_startup_trust_restore_discards_reference_after_fence_changes(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    from airplay_linux.session.recovery import StartupRecovery
    from airplay_linux.trust.fences import TrustFence

    async def stale_loader(receiver_id):
        rig.fences.set(TrustFence(
            receiver_id=receiver_id, endpoint_fingerprint="replacement-endpoint",
            endpoint_generation=99, session_generation=7, trust_generation=99,
            active_challenge_id=None,
        ))
        return Ok(value=OpaqueCredentialRef("backend-owned-reference"))

    result = await StartupRecovery(
        rig.manager, rig.deps, Registry([]), credential_loader=stale_loader,
    ).run()
    await settle(rig)

    state = rig.manager.snapshot(rig.receiver_id)
    assert isinstance(result, Ok)
    assert state.trust is TrustState.Unknown
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.stale_target


@pytest.mark.asyncio
async def test_startup_trust_restore_discards_reference_after_challenge_fence_changes(tmp_path):
    rig, _, _, _ = await prepared(tmp_path)
    from airplay_linux.session.recovery import StartupRecovery

    async def stale_loader(receiver_id):
        fence = rig.fences.get(receiver_id)
        assert fence is not None
        rig.fences.set(replace(fence, active_challenge_id=ChallengeId(UUID(int=31337))))
        return Ok(value=OpaqueCredentialRef("backend-owned-reference"))

    result = await StartupRecovery(
        rig.manager, rig.deps, Registry([]), credential_loader=stale_loader,
    ).run()
    await settle(rig)

    state = rig.manager.snapshot(rig.receiver_id)
    assert isinstance(result, Ok)
    assert state.trust is TrustState.Unknown
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.stale_target


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
@pytest.mark.parametrize("event_kind, expected_trust", [
    (TransportEventKind.credential_invalid, TrustState.Untrusted),
    (TransportEventKind.pairing_required, TrustState.Pairing),
])
async def test_recovered_output_survives_backend_trust_events_without_inventing_challenges(
    tmp_path, event_kind, expected_trust,
):
    rig, _, ownerships, handle = await prepared(tmp_path, online=True)
    await recover(rig)
    assert rig.manager.snapshot(rig.receiver_id).trust is TrustState.Unknown
    assert not rig.journal.calls("pairing.begin_pairing")
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    await settle(rig)
    rig.transport.emit(stream_event(rig, event_kind))
    await settle(rig)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.display_handle == handle
    assert state.display is DisplayState.Owned
    assert state.trust is expected_trust
    challenged = event_kind is TransportEventKind.pairing_required
    assert (state.challenge is not None) == challenged
    assert len(rig.journal.calls("pairing.begin_pairing")) == int(challenged)
    assert handle.owner_uuid in ownerships.records


@pytest.mark.asyncio
async def test_quarantined_session_metadata_blocks_replacement_without_discarding_ownership(tmp_path):
    from airplay_linux.storage import AtomicFiles, JsonSessionStore

    rig, _, ownerships, handle = await prepared(tmp_path, online=True)
    files = AtomicFiles(tmp_path / "metadata")
    files.write("sessions/unreadable.json", b"{truncated")
    rig.sessions = JsonSessionStore(files)
    rig.deps = replace(rig.deps, sessions=rig.sessions)
    install_handlers(rig)
    result = await recover(rig)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    assert rig.sessions.quarantined()
    await rig.manager.dispatch(Start(receiver_id=rig.receiver_id, request=StartRequest(
        mode=Mode.VIRTUAL, resolution=decision(), audio=AudioPolicy(enabled=False),
    )))
    await settle(rig)
    assert handle.owner_uuid in ownerships.records
    assert files.read("sessions/unreadable.json") == b"{truncated"
    assert not rig.journal.calls("transport.start_stream")


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
    process._environments.pop(handle.helper.pid)  # owner evidence no longer matches
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
    assert rig.manager.snapshot(rig.receiver_id).trust is TrustState.CleanupRequired
    assert handle.owner_uuid in store.records


@pytest.mark.asyncio
async def test_unproven_persisted_handle_is_never_owned_without_provider_evidence(tmp_path):
    rig, process, store, handle = await prepared(tmp_path)
    process._environments.pop(handle.helper.pid)
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


@pytest.mark.asyncio
async def test_persistence_serializes_same_receiver_while_other_receivers_progress():
    import asyncio

    from airplay_linux.session.persistence import SessionPersistence
    from airplay_linux.testing.adapters import FakeManager, MemorySessionStore

    manager, store = FakeManager(), MemorySessionStore()
    first, second = target().receiver_id, target("b").receiver_id
    entered, release = asyncio.Event(), asyncio.Event()
    writes, active, overlaps = [], set(), []
    save = store.save

    async def gated(snapshot):
        if snapshot.receiver_id in active:
            overlaps.append((snapshot.receiver_id, snapshot.session_generation))
        assert snapshot.receiver_id not in active
        active.add(snapshot.receiver_id)
        writes.append((snapshot.receiver_id, snapshot.session_generation))
        if snapshot.receiver_id == first and snapshot.session_generation == 1:
            entered.set()
            await release.wait()
        result = await save(snapshot)
        active.remove(snapshot.receiver_id)
        return result

    store.save = gated
    persistence = SessionPersistence(manager, store)
    persistence.start()
    manager.emit_snapshot(replace(SessionSnapshot.idle(first), session_generation=1))
    await entered.wait()
    manager.emit_snapshot(replace(SessionSnapshot.idle(first), session_generation=2))
    manager.emit_snapshot(replace(SessionSnapshot.idle(second), session_generation=3))
    for _ in range(5):
        await asyncio.sleep(0)
    assert second in store.saved
    # The writer catches adapter exceptions and flush retries failures. Observe
    # overlap separately so those recovery paths cannot mask a broken queue.
    assert not overlaps
    assert (first, 2) not in writes
    release.set()
    assert isinstance(await persistence.flush(), Ok)
    assert store.saved[first].session_generation == 2
    assert writes.index((first, 1)) < writes.index((first, 2))
    assert isinstance(await persistence.close(), Ok)


@pytest.mark.asyncio
async def test_persistence_uses_storage_allowlist_before_any_store_observes_snapshot():
    from airplay_linux.domain.operations import SensitiveText
    from airplay_linux.session.persistence import SessionPersistence
    from airplay_linux.testing.adapters import FakeManager
    from airplay_linux.testing.builders import physical

    # Even a custom store receives no challenge, PIN or portal token. The disk
    # codec is a second boundary, not the only place transient data is stripped.
    manager = FakeManager()
    received = []

    class Store:
        async def save(self, snapshot):
            received.append(snapshot)
            return Ok(value=None)

    persistence = SessionPersistence(manager, Store())
    persistence.start()
    state = replace(SessionSnapshot.idle(target().receiver_id),
                    trust=TrustState.Pairing, challenge=SensitiveText("PIN-DO-NOT-PERSIST"),
                    physical_source=replace(physical(), portal_restore_token="PORTAL-DO-NOT-PERSIST"),
                    muted=True)
    manager.emit_snapshot(state)
    assert isinstance(await persistence.close(), Ok)
    assert received[0].challenge is None
    assert received[0].physical_source.portal_restore_token is None
    assert received[0].trust is TrustState.CleanupRequired
    assert not received[0].muted
    assert "DO-NOT-PERSIST" not in repr(received)


@pytest.mark.asyncio
async def test_failed_persistence_reports_error_without_releasing_owned_resource(tmp_path):
    from airplay_linux.session.persistence import SessionPersistence

    rig, _, store, handle = await prepared(tmp_path)
    await recover(rig)
    rig.sessions.queue_result("save", Err(error=diagnostic(ErrorCode.persistence_failed)))
    persistence = SessionPersistence(rig.manager, rig.sessions)
    persistence.start()
    result = await persistence.flush()
    await settle(rig)
    assert isinstance(result, Err)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.last_error.code is ErrorCode.persistence_failed
    assert state.display_handle == handle
    assert state.display is DisplayState.Owned
    assert handle.owner_uuid in store.records
    assert len(rig.journal.calls("session_store.save")) <= 2  # no diagnostic feedback loop
    assert isinstance(await persistence.flush(), Ok)  # explicit retry persists latest state
    assert rig.sessions.saved[rig.receiver_id].display_handle == handle
    assert isinstance(await persistence.close(), Ok)


@pytest.mark.asyncio
async def test_disk_persistence_roundtrip_keeps_intent_and_trust_tombstone(tmp_path):
    from airplay_linux.session.persistence import SessionPersistence
    from airplay_linux.storage import AtomicFiles, JsonSessionStore
    from airplay_linux.testing.adapters import FakeManager

    manager = FakeManager()
    store = JsonSessionStore(AtomicFiles(tmp_path / "state"))
    persistence = SessionPersistence(manager, store)
    persistence.start()
    manager.emit_snapshot(replace(SessionSnapshot.idle(target().receiver_id),
                                  intent_active=True, mode=Mode.VIRTUAL,
                                  trust=TrustState.RevocationPending, session_generation=4))
    assert isinstance(await persistence.close(), Ok)
    (state,) = await store.load_all()
    assert state.intent_active and state.mode is Mode.VIRTUAL
    assert state.trust is TrustState.RevocationPending
    assert state.session_generation == 4
    assert state.challenge is None
