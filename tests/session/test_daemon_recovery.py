"""Shared daemon recovery must spend one manager budget, never one per TV."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    DiscoveryEvent, FailurePayload, Retry, Start, Stop, TransportEventKind,
)
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import (
    AudioPolicy, DisplayState, Err, ErrorCode, Mode, Ok, SessionPhase, StartRequest, TransportState,
)
from airplay_linux.session.reconnect import DaemonRecovery, RestartBudget
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import target
from airplay_linux.testing.time import FakeClock
from test_reconnect import advance_retry, recovery_rig, start_session, starts
from virtual_scenarios import decision, settle, stream_event


def record(number=100):
    return DaemonRecord(
        schema_version=1, instance_uuid=InstanceId(UUID(int=number)), owner_token="fake-runtime-owner",
        app=ProcessIdentity(pid=101, start_ticks=102),
        supervisor=ProcessIdentity(pid=103, start_ticks=104),
        daemon=ProcessIdentity(pid=105, start_ticks=106), protocol_version="1",
        socket_path="/fake/private/socket", lifecycle="running", runtime_directory="/fake/private",
    )


class Restart:
    def __init__(self, *, outcomes=(), gate=None):
        self.calls = 0
        self.outcomes = list(outcomes)
        self.gate = gate
        self.rig = None

    async def __call__(self):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        result = self.outcomes.pop(0) if self.outcomes else Ok(value=record(100 + self.calls))
        if isinstance(result, Ok) and self.rig is not None:
            self.rig.transport.streams.clear()
            self.rig.transport.daemon_instance_uuid = result.value.instance_uuid
        return result


def test_restart_budget_uses_sliding_monotonic_window_and_counts_only_claims():
    clock = FakeClock()
    budget = RestartBudget(clock)
    assert [budget.claim() for _ in range(4)] == [True, True, True, False]
    clock.advance(59.999)
    assert not budget.claim()
    clock.advance(0.001)
    assert budget.claim()
    assert budget.claim()
    assert budget.claim()
    assert not budget.claim()
    clock.advance(20)
    assert not budget.claim()
    clock.advance(40)
    assert budget.claim()


@pytest.mark.asyncio
async def test_simultaneous_recovery_waiters_share_one_launch_and_one_budget_claim():
    gate = Deferred()
    restart = Restart(gate=gate)
    rig = SessionRig()
    recovery = DaemonRecovery(replace(rig.deps, restart_daemon=restart))
    waiting = [asyncio.create_task(recovery.request_restart()) for _ in range(8)]
    for _ in range(4):
        await asyncio.sleep(0)
    assert restart.calls == 1
    gate.resolve(Ok(value=None))
    results = await asyncio.gather(*waiting)
    assert all(result == Ok(value=record(101)) for result in results)
    assert isinstance(await recovery.request_restart(), Ok)
    assert isinstance(await recovery.request_restart(), Ok)
    assert isinstance(await recovery.request_restart(), Err)
    assert restart.calls == 3


@pytest.mark.asyncio
async def test_cancelled_receiver_waiter_does_not_cancel_shared_recovery():
    gate = Deferred()
    restart = Restart(gate=gate)
    rig = SessionRig()
    recovery = DaemonRecovery(replace(rig.deps, restart_daemon=restart))
    first = asyncio.create_task(recovery.request_restart())
    second = asyncio.create_task(recovery.request_restart())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.resolve(Ok(value=None))
    assert await second == Ok(value=record(101))
    assert restart.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("second_notice_late", [False, True])
async def test_one_daemon_restart_serves_two_receivers_without_duplicate_sessions(second_notice_late):
    gate = Deferred()
    restart = Restart(gate=gate)
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    a = await start_session(rig)
    b = await start_session(rig, target("b", address="192.0.2.11"))
    notice_a = stream_event(rig, TransportEventKind.daemon_exit)
    notice_b = stream_event(rig, TransportEventKind.daemon_exit, target("b"))
    rig.transport.emit(notice_a)
    if not second_notice_late:
        rig.transport.emit(notice_b)
    await settle(rig)
    assert restart.calls == 1
    assert rig.manager.snapshot(a.receiver_id).display_handle == a.display_handle
    gate.resolve(Ok(value=None))
    await settle(rig)
    if second_notice_late:
        rig.transport.emit(notice_b)
        await settle(rig)
    for before in (a, b):
        current = rig.manager.snapshot(before.receiver_id)
        assert current.transport is TransportState.Streaming
        assert current.stream.daemon_instance_uuid == record(101).instance_uuid
        assert current.session_generation == before.session_generation
        assert current.display_handle == before.display_handle
        assert current.transport_lease_id != before.transport_lease_id
    assert restart.calls == 1
    assert len(rig.transport.streams) == 2
    assert len(starts(rig)) == 4
    assert len(rig.display.created) == 2
    assert not rig.display.removed
    rig.transport.emit(notice_a)
    await settle(rig)
    assert restart.calls == 1
    assert len(starts(rig)) == 4


@pytest.mark.asyncio
async def test_daemon_exit_does_not_touch_a_stream_from_another_instance():
    restart = Restart()
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    a = await start_session(rig)
    rig.transport.daemon_instance_uuid = InstanceId(UUID(int=55))
    b = await start_session(rig, target("b", address="192.0.2.11"))
    # A wrong-instance event with an otherwise exact stream is rejected at admission.
    event = stream_event(rig, TransportEventKind.daemon_exit, target("b"))
    rig.transport.emit(replace(event, daemon_instance_uuid=a.stream.daemon_instance_uuid))
    await settle(rig)
    assert rig.manager.snapshot(b.receiver_id) == b
    assert restart.calls == 0


@pytest.mark.asyncio
async def test_local_restart_exhaustion_retains_output_and_explicit_retry_uses_same_intent():
    failures = [Err(error=diagnostic(ErrorCode.daemon_failed)) for _ in range(3)]
    restart = Restart(outcomes=failures)
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.daemon_exit))
    await settle(rig)
    # Local failure consumes the shared 3-in-60 budget; no network counter is involved.
    failed = rig.manager.snapshot(before.receiver_id)
    assert restart.calls == 3
    assert failed.phase is SessionPhase.Failed
    assert failed.transport is TransportState.Failed
    assert failed.intent_active
    assert failed.display is DisplayState.Owned
    assert failed.display_handle == before.display_handle
    assert failed.reconnect_attempt == 0
    assert not rig.scheduler.pending()
    await rig.manager.dispatch(Retry(receiver_id=before.receiver_id))
    await settle(rig)
    assert restart.calls == 3
    rig.scheduler.advance(60)
    assert rig.manager.snapshot(before.receiver_id).phase is SessionPhase.Failed
    await rig.manager.dispatch(Retry(receiver_id=before.receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert restart.calls == 4
    assert after.transport is TransportState.Streaming
    assert after.session_generation == before.session_generation
    assert after.display_handle == before.display_handle
    assert len(rig.display.created) == 1
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_ownership_mismatch_blocks_repeated_launches_with_cleanup_required():
    restart = Restart(outcomes=[Err(error=diagnostic(ErrorCode.ownership_mismatch))])
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.daemon_exit))
    await settle(rig)
    blocked = rig.manager.snapshot(before.receiver_id)
    assert blocked.phase is SessionPhase.CleanupRequired
    assert blocked.cleanup_pending
    assert blocked.last_error.code is ErrorCode.ownership_mismatch
    assert blocked.display is DisplayState.Owned
    assert blocked.display_handle == before.display_handle
    for _ in range(3):
        await rig.manager.dispatch(Retry(receiver_id=before.receiver_id))
        await settle(rig)
    rig.scheduler.advance(120)
    assert restart.calls == 1
    assert len(starts(rig)) == 1
    assert not rig.display.removed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["endpoint", "offline", "stop"])
async def test_restart_completion_rechecks_current_intent_and_target(change):
    gate = Deferred()
    restart = Restart(gate=gate)
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.daemon_exit))
    await settle(rig)
    if change == "endpoint":
        rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.26"))
    elif change == "offline":
        rig.discovery.remove_target(before.receiver_id)
    else:
        await rig.manager.dispatch(Stop(receiver_id=before.receiver_id))
    await settle(rig)
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert restart.calls == 1
    if change == "endpoint":
        assert after.transport is TransportState.Streaming
        assert after.stream.endpoint_generation == 2
        assert starts(rig)[-1]["target"]["canonical_host"] == "192.0.2.26"
    else:
        assert after.stream is None
        assert len(starts(rig)) == 1
    if change == "stop":
        assert not after.intent_active
        assert after.display is DisplayState.None_
    else:
        assert after.intent_active
        assert after.session_generation == before.session_generation
        assert after.display_handle == before.display_handle


@pytest.mark.asyncio
async def test_after_shared_restart_one_receiver_rejection_does_not_fail_the_other():
    restart = Restart()
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    a = await start_session(rig)
    b = await start_session(rig, target("b", address="192.0.2.11"))
    notices = [stream_event(rig, TransportEventKind.daemon_exit),
               stream_event(rig, TransportEventKind.daemon_exit, target("b"))]
    rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.receiver_rejected)))
    for notice in notices:
        rig.transport.emit(notice)
    await settle(rig)
    assert restart.calls == 1
    assert rig.manager.snapshot(a.receiver_id).transport is TransportState.Failed
    assert rig.manager.snapshot(b.receiver_id).transport is TransportState.Streaming
    assert rig.manager.snapshot(a.receiver_id).display_handle == a.display_handle
    assert rig.manager.snapshot(b.receiver_id).display_handle == b.display_handle
    assert len(rig.transport.streams) == 1
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_repeated_successful_restarts_spend_budget_again_on_a_new_failure():
    restart = Restart()
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    # The transport cannot initially acquire a local daemon.
    rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.daemon_failed)))
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(
        mode=Mode.VIRTUAL, resolution=decision(), audio=AudioPolicy(enabled=False),
    )))
    await settle(rig)
    assert restart.calls == 1
    before = rig.manager.snapshot(target().receiver_id)
    assert before.transport is TransportState.Streaming
    for expected_calls in (2, 3):
        rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
        await settle(rig)
        rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.daemon_failed)))
        await advance_retry(rig)
        assert restart.calls == expected_calls
        assert rig.manager.snapshot(before.receiver_id).transport is TransportState.Streaming
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.daemon_failed)))
    await advance_retry(rig)
    failed = rig.manager.snapshot(before.receiver_id)
    assert restart.calls == 3
    assert failed.transport is TransportState.Failed
    assert failed.display_handle == before.display_handle
    assert failed.intent_active


@pytest.mark.asyncio
async def test_refresh_notice_during_local_failure_does_not_silently_retry():
    restart = Restart(outcomes=[Err(error=diagnostic(ErrorCode.daemon_failed)) for _ in range(3)])
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.daemon_exit))
    await settle(rig)
    failed = rig.manager.snapshot(before.receiver_id)
    assert failed.transport is TransportState.Failed
    view = rig.discovery.current(before.receiver_id)
    rig.discovery.emit(DiscoveryEvent(kind="updated", receiver=view, diagnostic=None))
    await settle(rig)
    assert rig.manager.snapshot(before.receiver_id) == failed
    assert restart.calls == 3


@pytest.mark.asyncio
async def test_unconfirmed_daemon_death_keeps_stream_owned_until_reconciliation():
    gate = Deferred()
    restart = Restart(gate=gate)
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.daemon_exit, payload=FailurePayload(
        diagnostic=diagnostic(ErrorCode.cleanup_timeout),
    )))
    await settle(rig)
    reconciling = rig.manager.snapshot(before.receiver_id)
    assert reconciling.stream == before.stream
    assert reconciling.transport_lease_id is None
    assert reconciling.display_handle == before.display_handle
    assert restart.calls == 1
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.transport is TransportState.Streaming
    assert after.display_handle == before.display_handle


@pytest.mark.asyncio
async def test_failed_first_connect_after_each_successful_restart_still_consumes_budget():
    restart = Restart()
    rig = recovery_rig(restart=restart)
    restart.rig = rig
    for _ in range(6):
        rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.daemon_failed)))
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(
        mode=Mode.VIRTUAL, resolution=decision(), audio=AudioPolicy(enabled=False),
    )))
    # Three recover/reconnect transactions each cross several shielded actor
    # continuations; let the observable terminal state settle deterministically.
    for _ in range(4):
        await settle(rig)
        state = rig.manager.snapshot(target().receiver_id)
        if state.transport is TransportState.Failed:
            break
    assert restart.calls == 3
    assert len(starts(rig)) == 4
    assert state.transport is TransportState.Failed
    assert state.display is DisplayState.Owned
    assert state.intent_active
    assert not rig.scheduler.pending()
