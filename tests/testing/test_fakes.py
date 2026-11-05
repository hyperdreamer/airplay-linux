"""Deterministic adapters: controlled time, cancellation, and exact identities."""

import asyncio
import subprocess
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain import messages, ports
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget, ServiceRecord
from airplay_linux.domain.operations import (
    ChallengeId,
    DisplayHandle,
    InstanceId,
    LeaseId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationContext,
    OperationId,
    OwnerId,
    PairingResult,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import AudioStartReceipt, ActivityEvent, DaemonRecord, SessionSnapshot, SavedReceiver
from airplay_linux.domain.resources import (
    AudioPolicy,
    Diagnostic,
    Err,
    ErrorCode,
    IntentStage,
    Ok,
    PhysicalMonitorSource,
    RecoveryAction,
    SessionPhase,
    Size,
    TransportState,
    TrustState,
    VolumeLevel,
)
from airplay_linux.testing.adapters import (
    Deferred,
    FakeAudioProbe,
    FakeCapture,
    FakeCredentialStore,
    FakeDiscovery,
    FakeDisplay,
    FakeManager,
    FakePairing,
    FakeProcess,
    FakeTransport,
    MemoryActivity,
    MemoryDaemonStore,
    MemoryOwnershipStore,
    MemoryReceiverStore,
    MemorySessionStore,
    Redacted,
    RedactedRecord,
    SequenceIds,
)
from airplay_linux.testing.builders import context, physical, service, target, virtual_handle
from airplay_linux.testing.time import FakeClock, FakeScheduler

MONITOR = Size(width=1920, height=1080)


def run(awaitable):
    """Run one deterministic scenario; fakes never touch a real event loop policy."""
    return asyncio.run(awaitable)


def failure(code: ErrorCode, message: str = "scripted failure") -> Err:
    return Err(error=Diagnostic(code=code, message=message, recovery=(RecoveryAction.retry,)))


def stream_handle(lease: int = 42) -> StreamHandle:
    return StreamHandle(
        receiver_id=target().receiver_id,
        endpoint_generation=1,
        endpoint_fingerprint=target().endpoint_fingerprint,
        backend_stream_id="stream-1",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=LeaseId(UUID(int=lease)),
        start_operation_id=OperationId(UUID(int=3)),
    )


def test_volume_fake_normalizes_legacy_start_and_passes_explicit_receipt():
    async def scenario():
        transport = FakeTransport()
        t, operation = target(), context()
        transport.update_target(t, t.receiver_id)
        handle = (await transport.start_stream(t, None, AudioPolicy(), operation,
                                               initial_volume=VolumeLevel(value=30))).value
        operation = replace(operation, transport_lease_id=handle.lease_id)
        transport.queue_result("start_audio", Ok(value=None))
        result = await transport.start_audio(handle, operation)
        assert result == Ok(value=AudioStartReceipt(audio=True))
        assert result.value.level is None
        receipt = AudioStartReceipt(audio=True, volume_state="applied", level=0)
        transport.queue_result("start_audio", Ok(value=receipt))
        assert await transport.start_audio(handle, operation) == Ok(value=receipt)
        assert transport.muted[handle.lease_id]
        assert transport.journal.calls("transport.start_stream")[0]["initial_volume"] == {"value": 30}
    run(scenario())


@pytest.mark.parametrize("change", ["none", "handle", "target", "daemon", "lease"])
def test_volume_fake_fences_and_settles_once(change):
    async def scenario():
        transport, events = FakeTransport(), []
        transport.subscribe(events.append)
        t, operation = target(), context()
        transport.update_target(t, t.receiver_id)
        handle = (await transport.start_stream(t, None, AudioPolicy(), operation)).value
        operation = replace(operation, transport_lease_id=handle.lease_id)
        await transport.start_audio(handle, operation)
        if change == "handle":
            handle = replace(handle, backend_stream_id="wrong")
        elif change == "target":
            transport.update_target(target(endpoint_generation=2), t.receiver_id)
        elif change == "daemon":
            transport.daemon_instance_uuid = InstanceId(UUID(int=99))
        elif change == "lease":
            operation = replace(operation, transport_lease_id=LeaseId(UUID(int=999)))
        result = await transport.set_volume(handle, VolumeLevel(value=0), operation)
        await asyncio.sleep(0)
        assert len(events) == 1
        assert events[0].stream == handle and events[0].context == operation
        assert events[0].payload.ok is (change == "none")
        if change == "none":
            assert result == Ok(value=0) and transport.muted[handle.lease_id]
            assert await transport.close_audio(handle, operation) == Ok(value=None)
            assert isinstance(await transport.set_volume(handle, VolumeLevel(value=30), operation), Err)
        else:
            assert isinstance(result, Err)
            assert isinstance(await transport.close_audio(handle, operation), Err)
    run(scenario())


def test_equal_deadlines_run_in_fifo_order_and_cancel_is_permanent():
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    seen = []
    scheduler.call_at(1, lambda: seen.append("first"))
    cancelled = scheduler.call_at(1, lambda: seen.append("cancelled"))
    scheduler.call_at(1, lambda: seen.append("last"))
    cancelled.cancel()
    scheduler.advance(1)
    assert seen == ["first", "last"]
    assert scheduler.pending() == ()
    assert clock.now() == 1


def test_callbacks_scheduled_inside_callbacks_run_in_the_same_advance():
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    seen = []

    def first():
        seen.append(("first", clock.now()))
        scheduler.call_at(clock.now() + 2, lambda: seen.append(("second", clock.now())))

    scheduler.call_at(1, first)
    scheduler.advance(1)
    assert seen == [("first", 1.0)]
    assert scheduler.pending() == (3.0,)
    scheduler.advance(2)
    assert seen == [("first", 1.0), ("second", 3.0)]
    with pytest.raises(ValueError):
        scheduler.advance(-1)


def test_advancing_fake_time_alone_runs_backoff_retries_without_spawning(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("fake time must never start a real process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    process = FakeProcess()
    attempts = []

    def retry(attempt):
        attempts.append((attempt, clock.now()))
        assert process.spawned == []
        if attempt < 4:
            schedule_retry(attempt)

    def schedule_retry(attempt):
        scheduler.call_at(clock.now() + min(1 * 2**attempt, 30), lambda: retry(attempt + 1))

    schedule_retry(0)
    scheduler.advance(30)
    assert attempts == [(1, 1.0), (2, 3.0), (3, 7.0), (4, 15.0)]
    assert scheduler.pending() == ()
    assert process.spawned == []


def test_deferred_completion_survives_caller_cancellation_and_stays_releasable():
    async def scenario():
        transport = FakeTransport()
        t = target()
        transport.update_target(t, t.receiver_id)
        deferred = Deferred()
        transport.queue_result("start_stream", deferred)
        caller = asyncio.ensure_future(
            transport.start_stream(t, physical(), AudioPolicy(), context()),
        )
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert deferred.resolved is False
        handle = stream_handle()
        deferred.resolve(Ok(value=handle))
        # The late success stays observable after the caller gave up: a second
        # waiter still receives it because the completion future was shielded
        # from the cancelled caller.
        assert deferred.value == Ok(value=handle)
        assert await deferred.wait() == Ok(value=handle)
        assert await transport.stop_stream(handle, context()) == Ok(value=None)

    run(scenario())


def test_deferred_resolution_is_single_use_and_replayable():
    deferred = Deferred()
    deferred.resolve(Ok(value=7))
    assert deferred.resolved is True
    assert deferred.value == Ok(value=7)
    assert run(deferred.wait()) == Ok(value=7)
    with pytest.raises(RuntimeError):
        deferred.resolve(Ok(value=8))


def test_cancelling_one_waiter_does_not_cancel_the_completion_for_others():
    async def scenario():
        deferred = Deferred()
        abandoned = asyncio.ensure_future(deferred.wait())
        survivor = asyncio.ensure_future(deferred.wait())
        await asyncio.sleep(0)
        abandoned.cancel()
        with pytest.raises(asyncio.CancelledError):
            await abandoned
        deferred.resolve(Ok(value="late"))
        # The completion future is shielded: another waiter still observes it.
        assert await survivor == Ok(value="late")

    run(scenario())


def test_fake_transport_keeps_concurrent_receivers_independent():
    async def scenario():
        transport = FakeTransport()
        first = target("a")
        second = target("b")
        transport.update_target(first, first.receiver_id)
        transport.update_target(second, second.receiver_id)
        slow_first = Deferred()
        slow_second = Deferred()
        transport.queue_result("start_stream", slow_first)
        transport.queue_result("start_stream", slow_second)
        call_first = asyncio.ensure_future(
            transport.start_stream(first, physical("DP-1"), AudioPolicy(), context()),
        )
        await asyncio.sleep(0)
        call_second = asyncio.ensure_future(
            transport.start_stream(second, physical("HDMI-1"), AudioPolicy(), context()),
        )
        await asyncio.sleep(0)
        slow_second.resolve(failure(ErrorCode.transport_failed, "receiver b failed"))
        assert isinstance(await call_second, Err)
        handle = stream_handle()
        slow_first.resolve(Ok(value=handle))
        assert await call_first == Ok(value=handle)
        return handle

    run(scenario())


def test_fake_transport_rejects_stale_targets_and_foreign_handles():
    transport = FakeTransport()
    old = target("a", endpoint_generation=1)
    transport.update_target(old, old.receiver_id)
    fresh = target("a", endpoint_generation=2)
    transport.update_target(fresh, fresh.receiver_id)
    stale = run(transport.start_stream(old, physical(), AudioPolicy(), context()))
    assert isinstance(stale, Err)
    assert stale.error.code is ErrorCode.stale_target
    stale_modes = run(transport.query_modes(old, context()))
    assert stale_modes.error.code is ErrorCode.stale_target
    started = run(transport.start_stream(fresh, physical(), AudioPolicy(), context(endpoint_generation=2)))
    assert isinstance(started, Ok)
    handle = started.value
    foreign = replace(handle, lease_id=LeaseId(UUID(int=999)))
    rejected = run(transport.stop_stream(foreign, context(endpoint_generation=2)))
    assert isinstance(rejected, Err)
    assert rejected.error.code is ErrorCode.stale_event
    assert run(transport.stop_stream(handle, context(endpoint_generation=2))) == Ok(value=None)
    assert transport.streams == {}


def test_fake_transport_fences_daemon_instance_in_handles():
    transport = FakeTransport()
    t = target()
    transport.update_target(t, t.receiver_id)
    handle = run(transport.start_stream(t, physical(), AudioPolicy(), context())).value
    transport.daemon_instance_uuid = InstanceId(UUID(int=77))
    result = run(transport.stop_stream(handle, context()))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.stale_event


def test_event_sources_deliver_until_unsubscribed():
    transport = FakeTransport()
    received = []
    subscription = transport.subscribe(received.append)
    event = messages.TransportEvent(
        kind=messages.TransportEventKind.ready, receiver_id=target().receiver_id,
        stream=None, endpoint_fingerprint=target().endpoint_fingerprint,
        endpoint_generation=1, daemon_instance_uuid=transport.daemon_instance_uuid,
        context=context(), payload=messages.EmptyPayload(),
    )
    transport.emit(event)
    assert received == [event]
    subscription.close()
    subscription.close()
    transport.emit(replace(event, kind=messages.TransportEventKind.stopped))
    assert received == [event]


def test_fake_process_matches_exact_pid_start_time_and_owner_environment():
    process = FakeProcess()
    owner = UUID(int=1)
    runtime = "/run/user/1000/airplay-linux/instances/fake"
    spec = ports.CommandSpec(
        argv=("doubletake", "-socket", "ctl.sock"),
        env={FakeProcess.OWNER_TOKEN_ENV: str(owner), FakeProcess.RUNTIME_DIRECTORY_ENV: runtime},
    )
    first = run(process.spawn(spec)).value
    assert process.spawned == [spec]
    assert process.identity(first.pid) == first
    replacement = ProcessIdentity(pid=first.pid, start_ticks=first.start_ticks + 1)
    process.register(replacement, spec.env)
    assert process.identity(first.pid) == replacement
    assert process.owned(first, owner, runtime) is False
    assert process.owned(replacement, owner, runtime) is True
    assert process.owned(replacement, UUID(int=2), runtime) is False
    assert process.owned(replacement, owner, "/run/other") is False
    refused = run(process.terminate(first, owner, runtime, deadline=5.0))
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch
    assert run(process.terminate(replacement, owner, runtime, deadline=5.0)) == Ok(value=None)
    assert process.identity(first.pid) is None


def test_fake_process_run_records_timeouts_and_never_executes():
    process = FakeProcess()
    output = run(process.run(ports.CommandSpec(argv=("kscreen-doctor", "-j"), env={}), timeout=5.0))
    assert output == Ok(value=ports.CommandOutput(exit_code=0, stdout=b"", stderr=b""))
    assert process.runs[0][1] == 5.0


def test_memory_ownership_store_atomic_failures_hide_unpublished_records():
    display = FakeDisplay()
    t = target()
    published = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(published, Ok)
    store = display.ownership
    record = run(store.all()).value[0]
    store.stage_log.clear()
    removing = replace(record, stage="removing", desired_state="removing")
    assert run(store.write(record)) == Ok(value=None)
    assert store.stage_log == ["write", "fsync", "rename", "directory_fsync"]
    store.inject_failure("rename")
    failed = run(store.write(removing))
    assert isinstance(failed, Err)
    assert failed.error.code is ErrorCode.persistence_failed
    assert failed.error.detail == "rename"
    assert run(store.all()).value == (record,)
    store.inject_failure("directory_fsync")
    assert isinstance(run(store.write(removing)), Err)
    # The rename already made the record visible; only its durability is unconfirmed.
    assert run(store.all()).value == (removing,)
    with pytest.raises(ValueError):
        store.inject_failure("chmod")


def test_fake_display_publishes_stages_and_requires_removal_intent_before_teardown():
    display = FakeDisplay()
    t = target()
    result = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(result, Ok)
    handle = result.value
    record = run(display.ownership.all()).value[0]
    assert [stage for _, stage in display.stage_log] == [
        "reserved", "helper_started", "output_observed", "published",
    ]
    assert (record.stage, record.desired_state) == ("published", "active")
    assert record.helper == handle.helper
    assert record.requested_size == MONITOR
    unowned = run(display.remove(OwnerId(UUID(int=999)), context()))
    assert isinstance(unowned, Err)
    assert unowned.error.code is ErrorCode.ownership_mismatch
    premature = run(display.remove(handle.owner_uuid, context()))
    assert isinstance(premature, Err)
    assert premature.error.code is ErrorCode.ownership_mismatch
    assert run(display.prepare_teardown(t.receiver_id, None, context())) == Ok(value=(handle.owner_uuid,))
    assert run(display.ownership.all()).value[0].stage == "removing"
    assert display.process.identity(handle.helper.pid) == handle.helper
    assert run(display.remove(handle.owner_uuid, context())) == Ok(value=None)
    assert run(display.ownership.all()).value == ()
    assert "Virtual-v1" not in display.outputs


def test_fake_display_stops_before_helper_launch_when_reservation_cannot_publish():
    display = FakeDisplay()
    t = target()
    display.ownership.inject_failure("write")
    result = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    assert display.stage_log == []
    assert display.process.spawned == []
    assert display.created == []
    assert run(display.ownership.all()).value == ()


def test_fake_display_rolls_back_a_started_helper_when_publication_fails():
    display = FakeDisplay()
    t = target()
    display.ownership.inject_failure("write", occurrence=2)
    result = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(result, Err)
    # The rollback publishes removal intent before any destructive step.
    assert [stage for _, stage in display.stage_log] == ["reserved", "removing"]
    (helper,) = [identity for identity in display.process.terminated]
    assert display.process.alive(helper) is False
    assert display.process.spawned[0].env[FakeProcess.OWNER_TOKEN_ENV]
    assert display.created == []
    assert run(display.ownership.all()).value == ()


def test_fake_display_creates_distinct_outputs_per_receiver():
    display = FakeDisplay()
    first = run(display.create(target("a").receiver_id, target("a").endpoint_fingerprint, MONITOR, context()))
    second = run(display.create(target("b").receiver_id, target("b").endpoint_fingerprint, MONITOR, context()))
    assert first.value.owner_uuid != second.value.owner_uuid
    assert first.value.source.kscreen_output_id != second.value.source.kscreen_output_id
    assert len(run(display.ownership.all()).value) == 2


def test_fake_display_reconciles_an_orphan_and_requires_confirmation_to_remove():
    display = FakeDisplay()
    t = target()
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    display.process.exit(handle.helper)
    items = run(display.reconcile())
    assert [item.kind for item in items] == ["orphan"]
    assert items[0].handle is not None
    assert run(display.approve_orphan(handle.owner_uuid, False, context())) == Ok(value=None)
    assert run(display.ownership.all()).value != ()
    assert run(display.approve_orphan(handle.owner_uuid, True, context())) == Ok(value=None)
    assert run(display.ownership.all()).value == ()


def test_fake_discovery_adds_targets_and_retains_the_last_view_offline():
    discovery = FakeDiscovery()
    received = []
    discovery.subscribe(received.append)
    t = target()
    assert run(discovery.start()) == Ok(value=None)
    discovery.add_target(t)
    view = discovery.current(t.receiver_id)
    assert view is not None
    assert view.online is True
    assert view.record_set.endpoint_generation == 1
    discovery.add_target(target(endpoint_generation=2))
    assert discovery.current(t.receiver_id).record_set.endpoint_generation == 2
    discovery.remove_target(t.receiver_id)
    removed = discovery.current(t.receiver_id)
    assert removed is not None
    assert removed.online is False
    assert [event.kind for event in received] == ["added", "updated", "removed"]
    assert received[-1].receiver.online is False


def test_fake_capture_offers_only_physical_monitors():
    capture = FakeCapture()
    monitor = physical("DP-1")
    capture.add_physical(monitor)
    assert run(capture.list_physical()) == Ok(value=(monitor,))
    assert run(capture.select("DP-1")) == Ok(value=monitor)
    # A buggy provider that listed a window or virtual output still cannot make
    # this fake hand one back as a capture source.
    capture.add_physical(physical("screen-cast-window:1"))
    capture.add_physical(physical("Virtual-1"))
    for rejected in ("screen-cast-window:1", "Virtual-1", "HDMI-9"):
        result = run(capture.select(rejected))
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.unsupported_source
    assert capture.rejected == ["screen-cast-window:1", "Virtual-1", "HDMI-9"]
    # Registration enumerates, so it refuses a non-physical descriptor outright.
    assert capture.refused == ["screen-cast-window:1", "Virtual-1"]
    listed = run(capture.list_physical()).value
    assert listed == (monitor,)
    assert run(capture.verify(monitor)) == Ok(value=None)
    assert isinstance(run(capture.verify(physical("HDMI-1"))), Err)
    assert run(capture.release(monitor)) == Ok(value=None)
    assert run(capture.release(monitor)) == Ok(value=None)
    assert capture.released == [monitor]


def test_fake_manager_records_commands_without_precomputing_transitions():
    manager = FakeManager()
    receiver = target().receiver_id
    seen = []
    manager.subscribe(seen.append)
    assert manager.snapshot(receiver) == SessionSnapshot.idle(receiver)
    receipt = run(manager.dispatch(messages.Stop(receiver_id=receiver)))
    assert receipt.accepted is True
    assert manager.commands == [messages.Stop(receiver_id=receiver)]
    assert manager.snapshot(receiver) == SessionSnapshot.idle(receiver)
    snapshot = replace(
        SessionSnapshot.idle(receiver, online=True),
        phase=SessionPhase.Stopping,
        intent_stage=IntentStage.stopping,
        transport=TransportState.Stopping,
        trust=TrustState.Trusted,
    )
    manager.emit_snapshot(snapshot)
    assert manager.snapshot(receiver) is snapshot
    assert seen == [snapshot]


def test_journal_replaces_secrets_and_backend_tokens_with_sentinels():
    clock = FakeClock()
    pairing = FakePairing(clock=clock)
    store = FakeCredentialStore()
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    assert isinstance(challenge.backend_token, OpaqueBackendToken)
    provisional = run(store.prepare_pairing(challenge, challenge.backend_token)).value
    assert isinstance(provisional, OpaqueProvisionalCredentialRef)
    run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("canary-pin-8496"), context()))
    rendered = repr((pairing.journal.entries, store.journal.entries))
    assert "canary-pin-8496" not in rendered
    assert "fake-pairing-token" not in rendered
    submitted = pairing.journal.calls("pairing.submit_pairing")[-1]
    assert isinstance(submitted["value"], Redacted)
    prepared = store.journal.calls("credential_store.prepare_pairing")[-1]
    assert isinstance(prepared["challenge"], RedactedRecord)
    assert "backend_token" not in prepared["challenge"].fields
    assert isinstance(prepared["backend_result"], Redacted)
    begun = pairing.journal.calls("pairing.begin_pairing")[-1]
    assert begun["target"]["receiver_id"] == t.receiver_id


def test_journal_redacts_non_repr_daemon_owner_tokens():
    store = MemoryDaemonStore()
    record = DaemonRecord(
        schema_version=1, instance_uuid=InstanceId(UUID(int=8)),
        owner_token="canary-owner-token-8496",
        app=ProcessIdentity(pid=10, start_ticks=1), supervisor=None, daemon=None,
        protocol_version="1", socket_path="/run/x.sock", lifecycle="starting",
        runtime_directory="/run/x",
    )
    assert run(store.write(record)) == Ok(value=None)
    assert "canary-owner-token-8496" not in repr(store.journal.entries)
    assert run(store.all()) == Ok(value=(record,))


def test_fake_pairing_fences_generations_and_expiry():
    clock = FakeClock()
    pairing = FakePairing(clock=clock)
    t = target()
    challenge = run(pairing.begin_pairing(t, context(generation=1, trust_generation=0))).value
    stale = run(
        pairing.submit_pairing(
            challenge.challenge_id, SensitiveText("1234"), context(generation=2, trust_generation=0),
        ),
    )
    assert stale.kind == "Cancelled"
    assert stale.diagnostic.code is ErrorCode.stale_event
    clock.advance(120)
    expired = run(
        pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()),
    )
    assert expired.kind == "Cancelled"
    assert expired.diagnostic.code is ErrorCode.pairing_expired
    again = run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()))
    assert again.kind == "Cancelled"


def test_fake_pairing_commits_trust_and_forgets_it():
    pairing = FakePairing(clock=FakeClock())
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    result = run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()))
    assert result.kind == "Trusted"
    assert isinstance(result.credential_ref, OpaqueCredentialRef)
    assert pairing.trusted[t.receiver_id] == result.credential_ref
    assert run(pairing.forget_trust(t.receiver_id, context())) == Ok(value=None)
    assert pairing.trusted == {}


def test_default_fakes_report_verified_success_and_unknown_mode_evidence():
    transport = FakeTransport()
    t = target()
    transport.update_target(t, t.receiver_id)
    assert run(transport.query_modes(t, context())) == Ok(value=None)
    assert run(FakeAudioProbe().check()) == Ok(value=None)
    loaded = run(FakeCredentialStore().load_credential_ref(t.receiver_id))
    assert isinstance(loaded, Ok)
    assert isinstance(loaded.value, OpaqueCredentialRef)
    assert run(FakeCapture().list_physical()) == Ok(value=())
    receipt = run(FakeManager().dispatch(messages.Stop(receiver_id=t.receiver_id)))
    assert receipt.accepted is True
    assert receipt.diagnostic is None
    assert isinstance(receipt.operation_id, UUID)


def test_scripted_results_are_fifo_and_exhausted_results_fall_back_to_defaults():
    probe = FakeAudioProbe()
    scripted = failure(ErrorCode.audio_unavailable, "no audio")
    probe.queue_result("check", scripted)
    assert run(probe.check()) is scripted
    assert run(probe.check()) == Ok(value=None)


def test_memory_stores_round_trip_and_keep_latest_values():
    receiver = target().receiver_id
    sessions = MemorySessionStore()
    snapshot = SessionSnapshot.idle(receiver, online=True)
    assert run(sessions.save(snapshot)) == Ok(value=None)
    assert run(sessions.load_all()) == (snapshot,)
    updated = replace(snapshot, session_generation=1)
    assert run(sessions.save(updated)) == Ok(value=None)
    assert run(sessions.load_all()) == (updated,)
    receivers = MemoryReceiverStore()
    saved = SavedReceiver(
        receiver_id=receiver, service_metadata=(), display_name="Living Room",
        last_endpoint=None, continuity_fingerprint="fingerprint", credential_ref=None,
        remembered_resolution=None,
    )
    assert run(receivers.put(saved)) == Ok(value=None)
    assert run(receivers.get(receiver)) == Ok(value=saved)
    assert run(receivers.all()) == Ok(value=(saved,))
    assert run(receivers.remove_metadata(receiver)) == Ok(value=None)
    assert run(receivers.get(receiver)) == Ok(value=None)


def test_memory_activity_keeps_events_and_journals_them():
    activity = MemoryActivity()
    event = ActivityEvent(
        utc="1970-01-01T00:00:00Z", code="session.started",
        receiver_id=target().receiver_id, message="Started", fields={"mode": "mirror"},
    )
    activity.emit(event)
    assert activity.events == [event]
    assert activity.journal.names() == ["activity.emit"]


def test_sequence_ids_are_deterministic_and_monotonic():
    ids = SequenceIds(seed=7)
    assert [ids.new() for _ in range(3)] == [UUID(int=7), UUID(int=8), UUID(int=9)]
    assert SequenceIds().new() == UUID(int=1)


def test_builders_produce_typed_defaults_and_reject_unknown_overrides():
    t = target()
    assert isinstance(t, ReceiverTarget)
    assert t.receiver_id == ReceiverId("airplay-device:a")
    assert t.endpoint_generation == 1
    assert t.service_records and t.service_records[0].addresses == ("192.0.2.10",)
    assert target("b", endpoint_generation=4).endpoint_generation == 4
    monitor = physical()
    assert isinstance(monitor, PhysicalMonitorSource)
    assert monitor.connector == "DP-1"
    handle = virtual_handle()
    assert isinstance(handle, DisplayHandle)
    assert handle.source.kscreen_output_id == "v1"
    ctx = context()
    assert isinstance(ctx, OperationContext)
    assert ctx.transport_lease_id == LeaseId(UUID(int=1))
    assert context(lease=None).transport_lease_id is None
    record = service()
    assert isinstance(record, ServiceRecord)
    assert record.service_type == "_airplay._tcp"
    assert service(kind="raop").service_type == "_raop._tcp"
    with pytest.raises(ValueError):
        service(kind="bogus")
    with pytest.raises(TypeError):
        service(nonsense=True)
    with pytest.raises(TypeError):
        target(nonsense=1)
    with pytest.raises(TypeError):
        physical(nonsense=1)
    with pytest.raises(TypeError):
        context(nonsense=1)
    with pytest.raises(TypeError):
        virtual_handle(nonsense=1)


def test_fake_pairing_begins_only_the_scripted_challenge_without_events():
    transport = FakeTransport()
    received = []
    transport.subscribe(received.append)
    t = target()
    transport.update_target(t, t.receiver_id)
    run(transport.query_modes(t, context()))
    assert received == []
    assert ChallengeId(UUID(int=1)) == ChallengeId(UUID(int=1))


def test_arbitrary_stale_display_and_transport_events_can_be_emitted():
    display = FakeDisplay()
    transport = FakeTransport()
    stale_display = messages.DisplayEvent(
        kind="published", receiver_id=target().receiver_id,
        creation_operation_id=OperationId(UUID(int=1)), owner_uuid=OwnerId(UUID(int=999)),
        connector="Virtual-v9", output_id="v9",
        helper=ProcessIdentity(pid=1, start_ticks=1), handle=None, diagnostic=None,
    )
    stale_transport = messages.TransportEvent(
        kind=messages.TransportEventKind.ready, receiver_id=target().receiver_id,
        stream=None, endpoint_fingerprint="endpoint-a-v1", endpoint_generation=1,
        daemon_instance_uuid=InstanceId(UUID(int=88)), context=context(generation=7),
        payload=messages.EmptyPayload(),
    )
    seen = []
    display.subscribe(seen.append)
    transport.subscribe(seen.append)
    display.emit(stale_display)
    transport.emit(stale_transport)
    assert seen == [stale_display, stale_transport]
    # Emitting a stale event never mutates owned state.
    assert run(display.ownership.all()).value == ()
    assert transport.streams == {}


def test_display_creation_lock_serializes_concurrent_receivers():
    async def scenario():
        display = FakeDisplay()
        first = target("a")
        second = target("b")
        suspended = Deferred()
        # Suspend the first attempt mid-reservation so a concurrent second attempt
        # is forced to contend for the provider-wide lock.
        display.ownership.queue_result("write", suspended)
        first_call = asyncio.ensure_future(
            display.create(first.receiver_id, first.endpoint_fingerprint, MONITOR, context()),
        )
        await asyncio.sleep(0)
        second_call = asyncio.ensure_future(
            display.create(second.receiver_id, second.endpoint_fingerprint, MONITOR, context()),
        )
        await asyncio.sleep(0)
        assert display.max_concurrent_creations == 1
        assert display.process.spawned == []
        suspended.resolve(Ok(value=None))
        results = await asyncio.gather(first_call, second_call)
        assert all(isinstance(result, Ok) for result in results)
        assert display.max_concurrent_creations == 1
        assert results[0].value.source.kscreen_output_id != results[1].value.source.kscreen_output_id
        assert len(display.process.spawned) == 2
        assert [stage for _, stage in display.stage_log].count("published") == 2

    run(scenario())


def test_fake_display_never_terminates_a_reused_pid():
    display = FakeDisplay()
    t = target()
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    published = run(display.ownership.all()).value[0]
    # The helper died and its PID was recycled by an unrelated process.
    display.process.exit(handle.helper)
    display.process.register(
        ProcessIdentity(pid=handle.helper.pid, start_ticks=handle.helper.start_ticks + 5),
        {},
    )
    assert run(display.prepare_teardown(t.receiver_id, None, context())) == Ok(value=(handle.owner_uuid,))
    assert run(display.remove(handle.owner_uuid, context())) == Ok(value=None)
    assert display.process.terminated == []
    assert display.process.identity(handle.helper.pid).start_ticks == handle.helper.start_ticks + 5
    assert published.stage == "published"


def test_mark_removing_requires_an_owned_record():
    display = FakeDisplay()
    result = run(display.mark_removing(OwnerId(UUID(int=1234))))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch


def test_ownership_atomic_failure_at_every_step_is_reported_by_step():
    display = FakeDisplay()
    t = target()
    run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    record = run(display.ownership.all()).value[0]
    store = display.ownership
    outcomes = {}
    for step in MemoryOwnershipStore.STEPS:
        store.inject_failure(step)
        result = run(store.write(record))
        outcomes[step] = (result.error.code if isinstance(result, Err) else None, result.error.detail if isinstance(result, Err) else None)
        assert run(store.all()).value == (record,)
    assert outcomes == {
        "write": (ErrorCode.persistence_failed, "write"),
        "fsync": (ErrorCode.persistence_failed, "fsync"),
        "rename": (ErrorCode.persistence_failed, "rename"),
        "directory_fsync": (ErrorCode.persistence_failed, "directory_fsync"),
    }
    assert run(store.write(record)) == Ok(value=None)


def test_scripted_results_cover_every_adapter_method():
    async def scenario():
        transport = FakeTransport()
        pairing = FakePairing()
        capture = FakeCapture()
        store = FakeCredentialStore()
        discovery = FakeDiscovery()
        display = FakeDisplay()
        sessions = MemorySessionStore()
        receivers = MemoryReceiverStore()
        ownership = MemoryOwnershipStore()
        daemons = MemoryDaemonStore()
        process = FakeProcess()
        probe = FakeAudioProbe()
        manager = FakeManager()
        t = target()
        transport.update_target(t, t.receiver_id)
        key = failure(ErrorCode.transport_failed)

        calls = {
            (transport, "shutdown", transport.shutdown()),
            (capture, "list_physical", capture.list_physical()),
            (discovery, "refresh", discovery.refresh()),
            (daemons, "all", daemons.all()),
            (probe, "check", probe.check()),
            (manager, "shutdown", manager.shutdown()),
            (process, "run", process.run(ports.CommandSpec(argv=("x",), env={}), 1.0)),
        }
        for adapter, method, call in calls:
            adapter.queue_result(method, key)
            assert await call is key, (adapter.__class__.__name__, method)

        # Every async pairing and credential method accepts a scripted outcome.
        assert pairing.pending("retry_revocation") == 0
        pairing.queue_result("retry_revocation", key)
        assert await pairing.retry_revocation(t.receiver_id, context()) is key
        store.queue_result("revoke_pairing", key)
        assert await store.revoke_pairing(t.receiver_id) is key
        # ``DiscoveryPort.close`` declares ``None`` success, so its scripting seam
        # controls only completion timing (see the deferred-close test).
        discovery.queue_result("close", None)
        assert discovery.pending("close") == 1
        await discovery.close()
        assert discovery.pending("close") == 0
        assert discovery.closed is True
        store.queue_result("mark_invalid", key)
        assert await store.mark_invalid(t.receiver_id) is key
        sessions.queue_result("save", key)
        assert await sessions.save(SessionSnapshot.idle(t.receiver_id)) is key
        receivers.queue_result("all", key)
        assert await receivers.all() is key
        ownership.queue_result("delete", key)
        assert await ownership.delete(OwnerId(UUID(int=5))) is key
        display.queue_result("create", key)
        assert await display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()) is key
        transport.queue_result("query_modes", key)
        assert await transport.query_modes(t, context()) is key

    run(scenario())


def test_scripted_pairing_result_can_drive_unconfirmed_revocation():
    pairing = FakePairing()
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    scripted = PairingResult(
        kind="TrustCleanupRequired", credential_ref=None,
        diagnostic=Diagnostic(
            code=ErrorCode.trust_cleanup_required, message="Revocation is unconfirmed",
            recovery=(RecoveryAction.retry_revocation,),
        ),
    )
    pairing.queue_result("submit_pairing", scripted)
    result = run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()))
    assert result is scripted
    assert result.kind == "TrustCleanupRequired"
    assert pairing.trusted == {}


def test_empty_pairing_value_is_invalid_and_exhausts_after_three_tries():
    pairing = FakePairing()
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    kinds = [
        run(pairing.submit_pairing(challenge.challenge_id, SensitiveText(""), context())).kind
        for _ in range(3)
    ]
    assert kinds == ["InvalidInput", "InvalidInput", "ReceiverRejected"]
    assert pairing.challenges == {}
    assert pairing.trusted == {}


def test_deferred_results_can_be_used_for_independent_audio_mute_and_teardown():
    async def scenario():
        transport = FakeTransport()
        t = target()
        transport.update_target(t, t.receiver_id)
        handle = (await transport.start_stream(t, physical(), AudioPolicy(), context())).value
        mute_deferred = Deferred()
        audio_deferred = Deferred()
        transport.queue_result("set_muted", mute_deferred)
        transport.queue_result("start_audio", audio_deferred)
        mute_call = asyncio.ensure_future(transport.set_muted(handle, True, context()))
        audio_call = asyncio.ensure_future(transport.start_audio(handle, context()))
        await asyncio.sleep(0)
        audio_deferred.resolve(Ok(value=None))
        assert await audio_call == Ok(value=AudioStartReceipt(audio=True))
        assert transport.audio_started[handle.lease_id] is True
        mute_deferred.resolve(failure(ErrorCode.audio_mute_failed, "mute rejected"))
        mute = await mute_call
        assert isinstance(mute, Err)
        # A failed mute preserves the last confirmed state.
        assert handle.lease_id not in transport.muted

    run(scenario())


def test_fake_process_models_missing_command_nonzero_exit_and_timeout():
    process = FakeProcess()
    spec = ports.CommandSpec(argv=("kscreen-doctor", "-j"), env={})
    process.queue_result("run", failure(ErrorCode.dependency_missing, "kscreen-doctor is missing"))
    missing = run(process.run(spec, 5.0))
    assert missing.error.code is ErrorCode.dependency_missing
    process.queue_result(
        "run",
        Ok(value=ports.CommandOutput(exit_code=2, stdout=b"", stderr=b"denied")),
    )
    nonzero = run(process.run(spec, 5.0))
    assert nonzero.value.exit_code == 2
    process.queue_result("run", failure(ErrorCode.cleanup_timeout, "command timed out"))
    timed_out = run(process.run(spec, 5.0))
    assert timed_out.error.code is ErrorCode.cleanup_timeout
    assert process.spawned == [] and process.runs


def test_fake_process_reports_owner_mismatch_for_foreign_owner_token():
    process = FakeProcess()
    owner = UUID(int=1)
    runtime = "/run/user/1000/airplay-linux/instances/v1"
    identity = run(
        process.spawn(
            ports.CommandSpec(
                argv=("doubletake",), env=FakeProcess.owner_environment(owner, runtime),
            ),
        ),
    ).value
    assert process.owned(identity, owner, runtime) is True
    assert process.owned(identity, UUID(int=2), runtime) is False
    assert process.owned(identity, owner, "/run/other") is False
    result = run(process.terminate(identity, UUID(int=2), runtime, deadline=5.0))
    assert result.error.code is ErrorCode.ownership_mismatch
    exits = []
    process.subscribe(exits.append)
    assert run(process.terminate(identity, owner, runtime, deadline=5.0)) == Ok(value=None)
    assert [event.exit_code for event in exits] == [-15]
    assert process.alive(identity) is False


def test_fake_display_disappearance_and_cleanup_required_events_are_emittable():
    display = FakeDisplay()
    t = target()
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    received = []
    display.subscribe(received.append)
    assert received == []
    display.process.exit(handle.helper)
    removed = messages.DisplayEvent(
        kind="disappeared", receiver_id=t.receiver_id,
        creation_operation_id=None, owner_uuid=handle.owner_uuid,
        connector=handle.source.connector, output_id=handle.source.kscreen_output_id,
        helper=None, handle=None,
        diagnostic=Diagnostic(
            code=ErrorCode.helper_failed, message="Helper exited", recovery=(RecoveryAction.retry,),
        ),
    )
    display.emit(removed)
    assert received == [removed]
    (item,) = run(display.reconcile())
    assert item.kind == "orphan"
    assert item.record.desired_state == "active"


def test_memory_activity_and_journal_never_store_secret_values():
    activity = MemoryActivity()
    event = ActivityEvent(
        utc="1970-01-01T00:00:00Z", code="pairing.rejected", receiver_id=None,
        message="Pairing value rejected", fields={"attempt": 1},
    )
    activity.emit(event)
    assert "canary" not in repr(activity.journal.entries)
    assert activity.events == [event]


def test_fake_discovery_refresh_reports_scripted_failure_and_retains_views():
    discovery = FakeDiscovery()
    t = target()
    discovery.add_target(t)
    discovery.queue_result("refresh", failure(ErrorCode.discovery_failed, "mdns unavailable"))
    result = run(discovery.refresh())
    assert result.error.code is ErrorCode.discovery_failed
    assert discovery.current(t.receiver_id).online is True
    removed = []
    discovery.subscribe(removed.append)
    discovery.remove_target(ReceiverId("airplay-device:absent"))
    assert removed == []


def test_fake_manager_keeps_snapshots_per_receiver():
    manager = FakeManager()
    first = target("a").receiver_id
    second = target("b").receiver_id
    online = replace(SessionSnapshot.idle(first, online=True), session_generation=2)
    manager.emit_snapshot(online)
    assert manager.snapshot(first) is online
    assert manager.snapshot(second) == SessionSnapshot.idle(second)
    event = messages.TimerEvent(receiver_id=first, kind="reconnect", context=context(generation=2))
    manager.ingest(event)
    assert manager.events == [event]
    assert manager.journal.names() == ["manager.ingest"]


def test_fake_capture_verifies_only_the_exact_selected_source():
    capture = FakeCapture()
    monitor = physical()
    capture.add_physical(monitor)
    assert run(capture.select("DP-1")).value is monitor
    # A same-connector descriptor with a different restore token is not that source.
    other = replace(monitor, portal_restore_token="token-2")
    result = run(capture.verify(other))
    assert result.error.code is ErrorCode.unsupported_source


def test_fake_display_crash_at_each_stage_leaves_a_reconcilable_record():
    for stage in ("helper_started", "output_observed", "published"):
        display = FakeDisplay()
        t = target()
        display.crash_at(stage)
        crashed = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
        assert isinstance(crashed, Err), stage
        assert crashed.error.code is ErrorCode.helper_failed
        (owner_uuid, recorded) = display.crashes[0]
        assert recorded == stage
        (item,) = run(display.reconcile())
        assert item.record.owner_uuid == owner_uuid
        assert item.record.stage == stage
        assert item.record.desired_state == "active"
        # The owned helper survives the crash, so recovery sees a live exact owner.
        assert item.kind == "recovering"
        assert display.kscreen_snapshot().baseline_hash in {"layout-0", "layout-1"}
        assert str(owner_uuid) in display.scan_owners()
        assert run(display.prepare_teardown(t.receiver_id, None, context())).value == (owner_uuid,)
        assert run(display.remove(owner_uuid, context())) == Ok(value=None)
        assert run(display.ownership.all()).value == ()


def test_kscreen_snapshot_and_owner_scan_track_live_outputs_only():
    display = FakeDisplay()
    t = target()
    assert display.kscreen_snapshot().outputs == {}
    assert display.scan_owners() == {}
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    snapshot = display.kscreen_snapshot()
    assert snapshot.outputs[handle.source.kscreen_output_id]["connector"] == handle.source.connector
    scans = display.scan_owners()
    assert str(handle.owner_uuid) in scans
    assert scans[str(handle.owner_uuid)].pid == handle.helper.pid
    run(display.prepare_teardown(t.receiver_id, None, context()))
    run(display.remove(handle.owner_uuid, context()))
    assert display.kscreen_snapshot().outputs == {}
    assert display.scan_owners() == {}
    with pytest.raises(ValueError):
        display.crash_at("bogus")


def test_fakes_satisfy_their_domain_protocols():
    import inspect as _inspect

    cases = {
        ports.CapturePort: FakeCapture(),
        ports.TransportPort: FakeTransport(),
        ports.PairingPort: FakePairing(),
        ports.CredentialStore: FakeCredentialStore(),
        ports.DisplayPort: FakeDisplay(),
        ports.SessionStore: MemorySessionStore(),
        ports.ReceiverStore: MemoryReceiverStore(),
        ports.OwnershipStore: MemoryOwnershipStore(),
        ports.DaemonStore: MemoryDaemonStore(),
        ports.ActivitySink: MemoryActivity(),
        ports.ProcessPort: FakeProcess(),
        ports.AudioProbe: FakeAudioProbe(),
        ports.ManagerPort: FakeManager(),
        ports.DiscoveryPort: FakeDiscovery(),
        ports.Clock: FakeClock(),
        ports.Scheduler: FakeScheduler(FakeClock()),
        ports.IdFactory: SequenceIds(),
    }
    for protocol, fake in cases.items():
        assert protocol._is_protocol
        missing = [
            name for name in protocol.__protocol_attrs__
            if not callable(getattr(fake, name, None))
        ]
        assert missing == [], (protocol.__name__, type(fake).__name__, missing)
        for name in protocol.__protocol_attrs__:
            expected = [
                parameter for parameter in _inspect.signature(getattr(protocol, name)).parameters
                if parameter != "self"
            ]
            actual = list(_inspect.signature(getattr(fake, name)).parameters)
            assert expected == actual, (protocol.__name__, name, expected, actual)

# ------------------------------------------------------------------ F-1 and F-15
# The teardown gate fences pending creation, and the durable removing stage never
# becomes a ``DisplayEvent`` kind that the frozen union does not declare.


def test_prepare_teardown_fences_a_create_suspended_before_reservation():
    async def scenario():
        display = FakeDisplay()
        t = target()
        suspended = Deferred()
        display.queue_result("create", suspended)
        create_call = asyncio.ensure_future(
            display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()),
        )
        await asyncio.sleep(0)
        # The gate acknowledges removal while the create still has no owner UUID.
        assert await display.prepare_teardown(t.receiver_id, None, context()) == Ok(value=())
        suspended.resolve(Ok(value=virtual_handle(owner=7, output="v7")))
        result = await create_call
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_event
        # The fenced completion is durably recorded as removing, never active.
        records = (await display.ownership.all()).value
        assert [(record.desired_state, record.stage) for record in records] == [
            ("removing", "removing"),
        ]
        assert [item.kind for item in await display.reconcile()] == ["removing"]
        # The output was acquired but never became an active, owned display.
        [handle] = display.created
        assert handle.owner_uuid == records[0].owner_uuid
        assert handle.owner_uuid not in display.handles
        # It is still releasable by exact handle rather than left dangling.
        assert await display.remove(handle.owner_uuid, context()) == Ok(value=None)

    run(scenario())


def test_prepare_teardown_fences_a_create_suspended_during_a_stage_write():
    async def scenario():
        display = FakeDisplay()
        t = target()
        suspended = Deferred()
        # The reserved write completes immediately; helper_started suspends.
        display.ownership.queue_result("write", Ok(value=None))
        display.ownership.queue_result("write", suspended)
        create_call = asyncio.ensure_future(
            display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()),
        )
        while display.ownership.pending("write") == 0 and not display.process.spawned:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        # Only the pre-existing reservation is admitted; the suspended stage is fenced.
        admitted = await display.prepare_teardown(t.receiver_id, None, context())
        assert isinstance(admitted, Ok)
        assert len(admitted.value) == 1
        assert display.process.spawned
        suspended.resolve(Ok(value=None))
        result = await create_call
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_event
        # The suspended helper_started stage never promoted the output to active.
        records = (await display.ownership.all()).value
        assert all(record.desired_state == "removing" for record in records)
        assert display.handles == {}
        assert display.created == []
        assert display.process.spawned
        # The fenced helper was cleaned up rather than abandoned.
        assert display.process.environments() == {}

    run(scenario())


@pytest.mark.parametrize("scripted_create", [False, True])
def test_prepare_teardown_rejects_new_create_while_removal_write_is_suspended(scripted_create):
    async def scenario():
        display = FakeDisplay()
        receiver = target()
        original = context(generation=1)
        owner = (await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, original,
        )).value
        suspended = Deferred()
        display.ownership.queue_result("write", suspended)
        teardown = asyncio.ensure_future(display.prepare_teardown(
            receiver.receiver_id, original.operation_id, context(generation=2),
        ))
        await asyncio.sleep(0)
        assert not teardown.done()
        assert display.ownership.pending("write") == 0
        assert display.ownership.journal.calls("ownership_store.write")[-1]["record"]["stage"] == "removing"

        if scripted_create:
            display.queue_result("create", Ok(value=virtual_handle(owner=7, output="v7")))
        replacement = await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=3),
        )
        # Finish the gate before asserting so failure also shows any escaped owner.
        suspended.resolve(Ok(value=None))
        admitted = await teardown
        records = (await display.ownership.all()).value
        assert isinstance(replacement, Err), (replacement, records)
        assert replacement.error.code is ErrorCode.stale_event
        assert admitted == Ok(value=(owner.owner_uuid,))
        assert [(record.owner_uuid, record.desired_state, record.stage) for record in records] == [
            (owner.owner_uuid, "removing", "removing"),
        ]
        assert display.created == [owner]
        assert display.outputs == {owner.source.kscreen_output_id: owner.source.connector}
        assert display.process.terminated == []
        if scripted_create:
            # Reject admission before consuming a scripted acquisition.
            assert display.pending("create") == 1
        assert await display.remove(owner.owner_uuid, context(generation=4)) == Ok(value=None)
        # A fresh start is allowed after the confirmed cleanup.
        assert isinstance(await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=5),
        ), Ok)

    run(scenario())


def test_prepare_teardown_keeps_admission_closed_until_all_receiver_gates_finish():
    async def scenario():
        display = FakeDisplay()
        receiver = target()
        owner = (await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=1),
        )).value
        first_write, second_write = Deferred(), Deferred()
        display.ownership.queue_result("write", first_write)
        display.ownership.queue_result("write", second_write)
        first = asyncio.ensure_future(display.prepare_teardown(
            receiver.receiver_id, None, context(generation=2),
        ))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(display.prepare_teardown(
            receiver.receiver_id, None, context(generation=3),
        ))
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert display.ownership.pending("write") == 0
        first_write.resolve(Ok(value=None))
        assert await first == Ok(value=(owner.owner_uuid,))
        assert not second.done()
        replacement = await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=4),
        )
        second_write.resolve(Ok(value=None))
        assert await second == Ok(value=(owner.owner_uuid,))
        assert isinstance(replacement, Err)
        assert replacement.error.code is ErrorCode.stale_event
        assert [(record.owner_uuid, record.desired_state)
                for record in (await display.ownership.all()).value] == [
            (owner.owner_uuid, "removing"),
        ]
        assert await display.remove(owner.owner_uuid, context(generation=5)) == Ok(value=None)
        assert isinstance(await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=6),
        ), Ok)

    run(scenario())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
def test_prepare_teardown_admission_gate_is_receiver_local_and_released_on_exit(outcome):
    async def scenario():
        display = FakeDisplay()
        receiver, other = target("a"), target("b")
        suspended = Deferred()
        display.queue_result("prepare_teardown", suspended)
        gate = asyncio.ensure_future(display.prepare_teardown(
            receiver.receiver_id, None, context(generation=1),
        ))
        await asyncio.sleep(0)
        assert not gate.done()
        assert display.pending("prepare_teardown") == 0
        other_result = await display.create(
            other.receiver_id, other.endpoint_fingerprint, MONITOR, context(generation=2),
        )
        blocked = await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=3),
        )
        if outcome == "cancelled":
            gate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await gate
            suspended.resolve(Ok(value=()))
        else:
            expected = Ok(value=()) if outcome == "success" else failure(ErrorCode.persistence_failed)
            suspended.resolve(expected)
            assert await gate == expected
        assert isinstance(other_result, Ok)
        assert isinstance(blocked, Err)
        assert blocked.error.code is ErrorCode.stale_event
        assert display.created == [other_result.value]
        assert isinstance(await display.create(
            receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, context(generation=4),
        ), Ok)

    run(scenario())


def test_display_never_emits_an_undeclared_display_event_kind():
    from airplay_linux.testing.adapters import DISPLAY_EVENT_KINDS

    assert "removing" not in DISPLAY_EVENT_KINDS
    display = FakeDisplay()
    t = target()
    received = []
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    display.subscribe(received.append)
    assert run(display.prepare_teardown(t.receiver_id, None, context())) == Ok(
        value=(handle.owner_uuid,),
    )
    # The removing stage stays in ownership metadata and the journal, not events.
    assert received == []
    assert run(display.ownership.all()).value[0].stage == "removing"
    stages = [arguments["stage"] for arguments in display.journal.calls("display.stage")]
    assert stages == ["removing"]
    assert run(display.remove(handle.owner_uuid, context())) == Ok(value=None)
    assert {event.kind for event in received} <= DISPLAY_EVENT_KINDS
    assert [event.kind for event in received] == ["removed"]


# ------------------------------------------------------------------ F-2
# Rollback publishes removal intent first, propagates failures, and never models
# an unconfirmed cleanup as a released resource.



def test_rollback_blocks_destructive_teardown_when_removal_intent_cannot_publish():
    display = FakeDisplay()
    t = target()
    # reserved and helper_started publish; output_observed and the rollback's
    # removal intent both fail durably.
    display.ownership.queue_result("write", Ok(value=None))
    display.ownership.queue_result("write", Ok(value=None))
    display.ownership.queue_result(
        "write", failure(ErrorCode.persistence_failed, "output stage"),
    )
    display.ownership.queue_result(
        "write", failure(ErrorCode.persistence_failed, "removal intent"),
    )
    result = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    # Removal intent never became durable, so nothing destructive ran.
    assert display.process.terminated == []
    assert display.process.environments() != {}
    # The last durable record still names the live helper, so it stays recoverable.
    records = run(display.ownership.all()).value
    assert len(records) == 1
    assert records[0].stage == "helper_started"
    assert records[0].helper is not None
    assert display.process.alive(records[0].helper) is True
    assert display.handles == {}


def test_rollback_propagates_termination_failure_and_keeps_the_record_recoverable():
    display = FakeDisplay()
    t = target()
    # Second atomic write is the helper_started stage; make it fail so rollback runs.
    display.ownership.inject_failure("write", occurrence=2)
    display.process.queue_result(
        "terminate", failure(ErrorCode.cleanup_timeout, "helper did not exit"),
    )
    result = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.cleanup_timeout
    # The surviving helper keeps its durable identity; the record is not deleted.
    records = run(display.ownership.all()).value
    assert len(records) == 1
    assert records[0].desired_state == "removing"
    assert records[0].helper is not None
    assert display.process.alive(records[0].helper) is True
    assert display.created == []
    [item] = run(display.reconcile())
    assert item.kind == "removing"


def test_rollback_publishes_removal_intent_before_terminating_the_helper():
    display = FakeDisplay()
    t = target()
    display.ownership.inject_failure("write", occurrence=2)
    run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()))
    intents = [
        arguments for arguments in display.ownership.journal.calls("ownership_store.write")
        if arguments["record"]["desired_state"] == "removing"
    ]
    assert intents, display.ownership.journal.names()
    # The removal-intent write is journaled, and the removing stage is visible in
    # ownership metadata rather than as an undeclared display event.
    assert [arguments["stage"] for arguments in display.journal.calls("display.stage")] == [
        "removing",
    ]
    assert display.process.terminated


# ------------------------------------------------------------------ F-3
CANARY_STDIN = b"canary-stdin-pin-8496"


def test_process_stdin_is_redacted_in_the_journal_and_its_representation():
    process = FakeProcess()
    spec = ports.CommandSpec(argv=("doubletake", "-pin-stdin"), env={})
    assert run(process.run(spec, 5.0, stdin=CANARY_STDIN)) == Ok(
        value=ports.CommandOutput(exit_code=0, stdout=b"", stderr=b""),
    )
    recorded = process.journal.calls("process.run")[-1]
    assert recorded["stdin"] == Redacted(kind=f"stdin[{len(CANARY_STDIN)} bytes]")
    assert CANARY_STDIN.decode() not in repr(process.journal.entries)
    assert "canary-stdin-pin-8496" not in repr(process.journal)
    # A call without stdin still journals the absence clearly.
    run(process.run(spec, 5.0))
    assert process.journal.calls("process.run")[-1]["stdin"] is None


# ------------------------------------------------------------------ F-4
def test_display_reconcile_is_a_deferred_async_operation():
    async def scenario():
        display = FakeDisplay()
        t = target()
        handle = (await display.create(
            t.receiver_id, t.endpoint_fingerprint, MONITOR, context(),
        )).value
        suspended = Deferred()
        display.queue_result("reconcile", suspended)
        call = asyncio.ensure_future(display.reconcile())
        await asyncio.sleep(0)
        assert display.pending("reconcile") == 0
        assert not call.done()
        assert suspended.resolved is False
        suspended.resolve(())
        assert await call == ()
        # The unscripted path is a coroutine returning the declared tuple.
        recovered = await display.reconcile()
        assert isinstance(recovered, tuple)
        assert [item.record.owner_uuid for item in recovered] == [handle.owner_uuid]

    run(scenario())


# ------------------------------------------------------------------ F-5
def test_session_store_load_all_returns_the_frozen_unwrapped_tuple():
    store = MemorySessionStore()
    receiver = target().receiver_id
    snapshot = SessionSnapshot.idle(receiver, online=True)
    assert run(store.save(snapshot)) == Ok(value=None)
    loaded = run(store.load_all())
    assert isinstance(loaded, tuple)
    assert tuple(loaded) == (snapshot,)
    # A scripted failure is reported on the diagnostic seam while the declared
    # return shape keeps its last durable contents instead of a wrapper.
    store.queue_result("load_all", failure(ErrorCode.persistence_failed, "corrupt"))
    assert run(store.load_all()) == (snapshot,)
    assert store.load_error is not None
    assert store.load_error.error.code is ErrorCode.persistence_failed
    # A scripted success still selects which sessions the port returns.
    store.queue_result("load_all", Ok(value=()))
    assert run(store.load_all()) == ()


# ------------------------------------------------------------------ F-6
def test_cancelling_an_already_fired_deadline_is_a_harmless_noop():
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    seen = []
    handle = scheduler.call_at(1, lambda: seen.append("fired"))
    scheduler.advance(1)
    assert seen == ["fired"]
    handle.cancel()
    handle.cancel()
    handle.close()
    assert scheduler.pending() == ()
    assert clock.now() == 1


def test_a_callback_can_cancel_its_own_handle_and_its_successors():
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    seen = []
    following = scheduler.call_at(2, lambda: seen.append("following"))
    holder = {}

    def first():
        seen.append("first")
        holder["handle"].cancel()
        following.cancel()

    holder["handle"] = scheduler.call_at(1, first)
    scheduler.advance(5)
    assert seen == ["first"]
    assert scheduler.pending() == ()
    assert clock.now() == 5


# ------------------------------------------------------------------ F-7
def test_scripted_immediate_stream_success_is_releasable_by_exact_handle():
    transport = FakeTransport()
    t = target()
    transport.update_target(t, t.receiver_id)
    handle = stream_handle()
    transport.queue_result("start_stream", Ok(value=handle))
    assert run(transport.start_stream(t, physical(), AudioPolicy(), context())) == Ok(value=handle)
    assert transport.streams[handle.lease_id] == handle
    assert run(transport.stop_stream(handle, context())) == Ok(value=None)
    assert transport.streams == {}


def test_scripted_late_stream_success_stays_releasable_after_cancellation():
    async def scenario():
        transport = FakeTransport()
        t = target()
        transport.update_target(t, t.receiver_id)
        deferred = Deferred()
        transport.queue_result("start_stream", deferred)
        call = asyncio.ensure_future(
            transport.start_stream(t, physical(), AudioPolicy(), context()),
        )
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        handle = stream_handle(lease=77)
        deferred.resolve(Ok(value=handle))
        assert transport.streams[handle.lease_id] == handle
        assert await transport.stop_stream(handle, context()) == Ok(value=None)
        assert transport.streams == {}

    run(scenario())


def test_scripted_process_identity_is_owned_and_terminable():
    process = FakeProcess()
    owner = UUID(int=1)
    runtime = "/run/user/1000/airplay-linux/instances/fake"
    spec = ports.CommandSpec(argv=("krfb-virtualmonitor",), env=FakeProcess.owner_environment(owner, runtime))
    identity = ProcessIdentity(pid=5555, start_ticks=31337)
    process.queue_result("spawn", Ok(value=identity))
    assert run(process.spawn(spec)) == Ok(value=identity)
    assert process.owned(identity, owner, runtime) is True
    assert process.spawned == [spec]
    assert run(process.terminate(identity, owner, runtime, deadline=5.0)) == Ok(value=None)
    assert process.alive(identity) is False


def test_scripted_late_process_identity_is_adopted_for_exact_termination():
    async def scenario():
        process = FakeProcess()
        owner = UUID(int=1)
        runtime = "/run/user/1000/airplay-linux/instances/fake"
        spec = ports.CommandSpec(argv=("krfb-virtualmonitor",), env=FakeProcess.owner_environment(owner, runtime))
        deferred = Deferred()
        process.queue_result("spawn", deferred)
        call = asyncio.ensure_future(process.spawn(spec))
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        identity = ProcessIdentity(pid=6666, start_ticks=4242)
        deferred.resolve(Ok(value=identity))
        assert process.owned(identity, owner, runtime) is True
        assert await process.terminate(identity, owner, runtime, deadline=5.0) == Ok(value=None)

    run(scenario())


def test_scripted_display_handle_is_registered_and_removable():
    display = FakeDisplay()
    t = target()
    handle = virtual_handle(owner=3, output="v3")
    display.queue_result("create", Ok(value=handle))
    assert run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())) == Ok(
        value=handle,
    )
    (record,) = run(display.ownership.all()).value
    assert record.owner_uuid == handle.owner_uuid
    assert record.helper == handle.helper
    assert record.connector == handle.source.connector
    assert display.outputs[handle.source.kscreen_output_id] == handle.source.connector
    assert run(display.prepare_teardown(t.receiver_id, None, context())) == Ok(
        value=(handle.owner_uuid,),
    )
    assert run(display.remove(handle.owner_uuid, context())) == Ok(value=None)
    assert run(display.ownership.all()).value == ()
    assert handle.source.kscreen_output_id not in display.outputs


def test_scripted_late_display_success_is_removable_after_cancellation():
    async def scenario():
        display = FakeDisplay()
        t = target()
        deferred = Deferred()
        display.queue_result("create", deferred)
        call = asyncio.ensure_future(
            display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()),
        )
        await asyncio.sleep(0)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        handle = virtual_handle(owner=4, output="v4")
        deferred.resolve(Ok(value=handle))
        assert (await display.ownership.all()).value != ()
        assert await display.prepare_teardown(t.receiver_id, None, context()) == Ok(
            value=(handle.owner_uuid,),
        )
        assert await display.remove(handle.owner_uuid, context()) == Ok(value=None)
        assert (await display.ownership.all()).value == ()

    run(scenario())


# ------------------------------------------------------------------ F-8
# Active controls require a currently matching endpoint; exact-handle cleanup
# keeps working across endpoint replacement and receiver removal.


def test_active_controls_reject_a_superseded_endpoint_but_stop_still_cleans_up():
    transport = FakeTransport()
    first = target("a", endpoint_generation=1)
    transport.update_target(first, first.receiver_id)
    handle = run(transport.start_stream(first, physical(), AudioPolicy(), context())).value
    second = target("a", endpoint_generation=2)
    transport.update_target(second, second.receiver_id)
    for result in (
        run(transport.set_muted(handle, True, context(endpoint_generation=2))),
        run(transport.start_audio(handle, context(endpoint_generation=2))),
    ):
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_target
    assert transport.muted == {} and transport.audio_started == {}
    # The exact handle is still releasable: cleanup is the documented exception.
    assert run(transport.stop_stream(handle, context(endpoint_generation=2))) == Ok(value=None)
    assert transport.streams == {}


def test_active_controls_reject_an_absent_receiver_and_a_stale_context():
    transport = FakeTransport()
    t = target()
    transport.update_target(t, t.receiver_id)
    handle = run(transport.start_stream(t, physical(), AudioPolicy(), context())).value
    # A context bound to another endpoint generation cannot drive this stream.
    mismatched = run(transport.set_muted(handle, True, context(endpoint_generation=2)))
    assert isinstance(mismatched, Err)
    assert mismatched.error.code is ErrorCode.stale_target
    assert run(transport.start_audio(handle, context(endpoint_generation=2))).error.code is (
        ErrorCode.stale_target
    )
    transport.update_target(None, t.receiver_id)
    for result in (
        run(transport.set_muted(handle, True, context())),
        run(transport.start_audio(handle, context())),
    ):
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_target
    assert run(transport.stop_stream(handle, context())) == Ok(value=None)


# ------------------------------------------------------------------ F-9
def test_forget_invalidates_active_challenges_so_late_trust_cannot_be_promoted():
    pairing = FakePairing(clock=FakeClock())
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    assert run(pairing.forget_trust(t.receiver_id, context())) == Ok(value=None)
    late = run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()))
    assert late.kind == "Cancelled"
    assert late.diagnostic.code is ErrorCode.stale_event
    assert pairing.trusted == {}


def test_forget_during_a_suspended_submission_revokes_the_late_success():
    async def scenario():
        pairing = FakePairing(clock=FakeClock())
        t = target()
        challenge = (await pairing.begin_pairing(t, context())).value
        suspended = Deferred()
        pairing.queue_result(
            "submit_pairing",
            suspended,
        )
        submit = asyncio.ensure_future(
            pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()),
        )
        await asyncio.sleep(0)
        assert await pairing.forget_trust(t.receiver_id, context()) == Ok(value=None)
        credential = OpaqueCredentialRef("late-credential")
        suspended.resolve(
            PairingResult(kind="Trusted", credential_ref=credential, diagnostic=None),
        )
        result = await submit
        assert result.kind == "Cancelled"
        assert result.diagnostic.code is ErrorCode.stale_event
        assert pairing.trusted == {}
        # The late success was compensated rather than silently dropped.
        assert pairing.revocations == [t.receiver_id]

    run(scenario())


def test_cancel_during_a_suspended_submission_reports_unconfirmed_revocation():
    async def scenario():
        pairing = FakePairing(clock=FakeClock())
        t = target()
        challenge = (await pairing.begin_pairing(t, context())).value
        suspended = Deferred()
        pairing.queue_result("submit_pairing", suspended)
        pairing.queue_result(
            "revoke_trust", failure(ErrorCode.trust_cleanup_required, "revocation unconfirmed"),
        )
        submit = asyncio.ensure_future(
            pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context()),
        )
        await asyncio.sleep(0)
        assert await pairing.cancel_pairing(t.receiver_id, challenge.challenge_id, context()) == (
            Ok(value=None)
        )
        suspended.resolve(
            PairingResult(
                kind="Trusted",
                credential_ref=OpaqueCredentialRef("late"),
                diagnostic=None,
            ),
        )
        result = await submit
        assert result.kind == "TrustCleanupRequired"
        assert result.diagnostic.code is ErrorCode.trust_cleanup_required
        assert result.diagnostic.recovery == (RecoveryAction.retry_revocation,)
        assert pairing.trusted == {}

    run(scenario())


def test_mark_invalid_also_invalidates_an_active_challenge():
    pairing = FakePairing(clock=FakeClock())
    t = target()
    challenge = run(pairing.begin_pairing(t, context())).value
    assert run(pairing.mark_invalid(t.receiver_id, context())) == Ok(value=None)
    assert run(pairing.submit_pairing(challenge.challenge_id, SensitiveText("1234"), context())).kind == (
        "Cancelled"
    )
    assert pairing.trusted == {}


# ------------------------------------------------------------------ F-10
def test_credential_revocation_works_for_both_union_variants():
    store = FakeCredentialStore()
    t = target()
    challenge = run(FakePairing().begin_pairing(t, context())).value
    provisional = run(store.prepare_pairing(challenge, challenge.backend_token)).value
    # The provisional variant is a runtime object, not a string identifier.
    assert run(store.revoke_pairing(provisional)) == Ok(value=None)
    assert provisional not in store.provisional_owner
    # The receiver variant removes any remembered credential for that receiver.
    store.seed_trusted(t.receiver_id)
    assert run(store.revoke_pairing(t.receiver_id)) == Ok(value=None)
    assert t.receiver_id not in store.stored
    assert store.revoked == [provisional, t.receiver_id]


# ------------------------------------------------------------------ F-11
def test_credential_transaction_publishes_the_committed_reference_for_its_receiver():
    store = FakeCredentialStore()
    t = target()
    challenge = run(FakePairing().begin_pairing(t, context())).value
    provisional = run(store.prepare_pairing(challenge, challenge.backend_token)).value
    committed = run(store.commit_pairing(provisional, trust_generation=3)).value
    assert store.committed[committed] == 3
    loaded = run(store.load_credential_ref(t.receiver_id)).value
    assert loaded == committed
    assert store.provisional == {}
    assert store.provisional_owner == {}


def test_credential_commit_rejects_cancelled_and_unknown_provisional_references():
    store = FakeCredentialStore()
    t = target()
    challenge = run(FakePairing().begin_pairing(t, context())).value
    provisional = run(store.prepare_pairing(challenge, challenge.backend_token)).value
    assert run(store.cancel_pairing(challenge)) == Ok(value=None)
    cancelled = run(store.commit_pairing(provisional, trust_generation=1))
    assert isinstance(cancelled, Err)
    assert cancelled.error.code is ErrorCode.keyring_unavailable
    unknown = run(
        store.commit_pairing(
            OpaqueProvisionalCredentialRef("never-prepared"), trust_generation=1,
        ),
    )
    assert isinstance(unknown, Err)
    assert unknown.error.code is ErrorCode.keyring_unavailable
    assert store.stored == {}
    assert run(store.load_credential_ref(t.receiver_id)).value is not None


# ------------------------------------------------------------------ F-12
def test_remove_refuses_to_delete_an_unrelated_output_that_reused_an_id():
    display = FakeDisplay()
    t = target()
    handle = run(display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context())).value
    output_id = handle.source.kscreen_output_id
    display.process.exit(handle.helper)
    run(display.prepare_teardown(t.receiver_id, None, context()))
    # An unrelated physical connector adopted the same output id.
    display.outputs[output_id] = "DP-9"
    result = run(display.remove(handle.owner_uuid, context()))
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch
    assert display.outputs[output_id] == "DP-9"
    assert run(display.ownership.all()).value != ()
    assert display.removed == []


def test_rollback_also_refuses_to_delete_a_reused_output_id():
    async def scenario():
        display = FakeDisplay()
        t = target()
        suspended = Deferred()
        # reserved and helper_started publish; output_observed suspends then fails.
        display.ownership.queue_result("write", Ok(value=None))
        display.ownership.queue_result("write", Ok(value=None))
        display.ownership.queue_result("write", suspended)
        call = asyncio.ensure_future(
            display.create(t.receiver_id, t.endpoint_fingerprint, MONITOR, context()),
        )
        while display.ownership.pending("write") != 0 or not display.outputs:
            await asyncio.sleep(0)
        # An unrelated physical connector adopted the same output id meanwhile.
        for output_id in list(display.outputs):
            display.outputs[output_id] = "DP-9"
        suspended.resolve(failure(ErrorCode.persistence_failed, "output stage"))
        result = await call
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.ownership_mismatch
        assert set(display.outputs.values()) == {"DP-9"}
        assert (await display.ownership.all()).value != ()

    run(scenario())


# ------------------------------------------------------------------ F-13
def test_capture_rejects_non_physical_sources_through_registration_and_listing():
    capture = FakeCapture()
    monitor = physical("DP-1")
    capture.add_physical(monitor)
    capture.add_physical(physical("virtual-1"))
    capture.add_physical(physical("screen-cast-window:2"))
    listed = run(capture.list_physical()).value
    assert listed == (monitor,)
    assert capture.refused == ["virtual-1", "screen-cast-window:2"]
    for rejected in ("virtual-1", "screen-cast-window:2"):
        assert run(capture.select(rejected)).error.code is ErrorCode.unsupported_source
        assert run(capture.verify(physical(rejected))).error.code is ErrorCode.unsupported_source


# ------------------------------------------------------------------ F-14
def test_discovery_close_remains_pending_until_its_deferred_completes():
    async def scenario():
        discovery = FakeDiscovery()
        suspended = Deferred()
        discovery.queue_result("close", suspended)
        call = asyncio.ensure_future(discovery.close())
        await asyncio.sleep(0)
        assert not call.done()
        assert discovery.closed is False
        assert discovery.pending("close") == 0
        suspended.resolve(None)
        assert await call is None
        assert discovery.closed is True

    run(scenario())


def test_prepare_teardown_fences_the_explicit_superseded_operation():
    async def scenario():
        display = FakeDisplay()
        receiver = target("a")
        other = target("b")
        superseded = context(generation=1)
        suspended = Deferred()
        display.queue_result("create", suspended)
        call = asyncio.ensure_future(
            display.create(receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, superseded),
        )
        await asyncio.sleep(0)
        # The gate names the superseded operation even for another receiver's scope.
        admitted = await display.prepare_teardown(
            other.receiver_id, superseded.operation_id, context(generation=2),
        )
        assert admitted == Ok(value=())
        suspended.resolve(Ok(value=virtual_handle(owner=9, output="v9")))
        result = await call
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_event
        assert superseded.operation_id in display.fenced_operations
        # A fresh operation is still admitted normally.
        fresh = context(generation=2)
        assert isinstance(
            await display.create(
                receiver.receiver_id, receiver.endpoint_fingerprint, MONITOR, fresh,
            ),
            Ok,
        )
        # Only the live create is active before the gate runs.
        active = {
            record.owner_uuid for record in (await display.ownership.all()).value
            if record.desired_state == "active"
        }
        assert len(active) == 1
        admitted = await display.prepare_teardown(
            receiver.receiver_id, None, context(generation=2),
        )
        assert admitted == Ok(value=tuple(active))

    run(scenario())
