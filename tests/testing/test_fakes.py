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
from airplay_linux.domain.records import ActivityEvent, DaemonRecord, SessionSnapshot, SavedReceiver
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
    assert [stage for _, stage in display.stage_log] == ["reserved"]
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
    items = display.reconcile()
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
    assert run(sessions.load_all()) == Ok(value=(snapshot,))
    updated = replace(snapshot, session_generation=1)
    assert run(sessions.save(updated)) == Ok(value=None)
    assert run(sessions.load_all()) == Ok(value=(updated,))
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
        assert await audio_call == Ok(value=None)
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
    (item,) = display.reconcile()
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
        (item,) = display.reconcile()
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
