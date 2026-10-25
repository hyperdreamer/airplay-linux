"""Reconnect contracts over the real actor and deterministic adapter boundaries."""
from __future__ import annotations

from dataclasses import replace

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    DiscoveryEvent, EmptyPayload, FailurePayload, Pair, Retry, RetryCleanup, SetMuted,
    Start, Stop, SubmitPairing, TimerEvent, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import SensitiveText
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, Mode, Ok, SessionPhase,
    StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import SessionManager
from airplay_linux.session.reconnect import (
    build_reconnect_handlers, compose_reconnect_handlers, network_delay,
)
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import physical, target
from virtual_scenarios import (
    ContextTransport, decision, make_rig, settle, stream_event, virtual_handlers,
)


def recovery_rig(*, transport=None, restart=None):
    rig = make_rig(transport=transport)
    for subscription in rig.manager._subscriptions:
        subscription.close()
    if restart is not None:
        rig.deps = replace(rig.deps, restart_daemon=restart)
    handlers = compose_reconnect_handlers(
        virtual_handlers(rig.deps), build_reconnect_handlers(rig.deps),
    )
    rig.manager = SessionManager(rig.deps, handlers)
    return rig


async def start_session(rig, destination=None, *, mode=Mode.VIRTUAL, audio=False):
    destination = destination or target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    request = StartRequest(
        mode=mode, source=physical() if mode is Mode.MIRROR else None,
        resolution=decision(destination) if mode is Mode.VIRTUAL else None,
        audio=AudioPolicy(enabled=audio),
    )
    await rig.manager.dispatch(Start(receiver_id=destination.receiver_id, request=request))
    await settle(rig)
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.stream is not None
    return state


def starts(rig):
    return rig.journal.calls("transport.start_stream")


async def advance_retry(rig):
    state = rig.manager.snapshot(target().receiver_id)
    assert state.next_retry_at is not None
    rig.scheduler.advance(state.next_retry_at - rig.clock.now())
    await settle(rig)


def test_network_backoff_is_capped_without_becoming_terminal():
    assert [network_delay(i) for i in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
    assert network_delay(100_000) == 30


@pytest.mark.parametrize("attempt,exception", [(-1, ValueError), (True, TypeError), (1.5, TypeError)])
def test_network_backoff_rejects_invalid_attempts(attempt, exception):
    with pytest.raises(exception):
        network_delay(attempt)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_network_loss_retries_with_fresh_operation_and_lease_without_replacing_source(mode):
    # Break caught: no scheduled reconnect, duplicate stream, or superseding the intent.
    rig = recovery_rig()
    before = await start_session(rig, mode=mode)
    old_ready = stream_event(rig, TransportEventKind.ready)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    waiting = rig.manager.snapshot(target().receiver_id)
    assert waiting.transport is TransportState.Reconnecting
    assert waiting.next_retry_at == 1
    assert waiting.stream is None
    assert waiting.audio is AudioState.Disabled
    assert rig.transport.streams == {}
    rig.scheduler.advance(0.99)
    await settle(rig)
    assert len(starts(rig)) == 1
    rig.scheduler.advance(0.01)
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.transport is TransportState.Streaming
    assert after.session_generation == before.session_generation
    assert after.operation_id != before.operation_id
    assert after.transport_lease_id != before.transport_lease_id
    assert after.display_handle == before.display_handle
    assert after.physical_source == before.physical_source
    assert after.reconnect_attempt == 0
    assert after.next_retry_at is None
    assert len(rig.transport.streams) == 1
    rig.transport.emit(old_ready)
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == after
    assert not rig.display.removed
    assert not rig.capture.released
    assert "display.prepare_teardown" not in rig.journal.names()


@pytest.mark.asyncio
async def test_offline_intent_retries_indefinitely_at_capped_delays_without_stale_sends():
    rig = recovery_rig()
    before = await start_session(rig)
    rig.discovery.remove_target(target().receiver_id)
    await settle(rig)
    delays = []
    for _ in range(12):
        state = rig.manager.snapshot(target().receiver_id)
        assert state.target is None
        assert not state.online
        assert state.transport is TransportState.Reconnecting
        assert state.phase is SessionPhase.Reconnecting
        assert state.intent_active
        assert state.session_generation == before.session_generation
        assert state.display_handle == before.display_handle
        delays.append(state.next_retry_at - rig.clock.now())
        await advance_retry(rig)
    assert delays == [1, 2, 4, 8, 16, 30, 30, 30, 30, 30, 30, 30]
    assert len(starts(rig)) == 1
    assert target().receiver_id not in rig.transport.targets
    assert len(rig.discovery.journal.calls("discovery.refresh")) >= 12
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_each_network_attempt_refreshes_the_current_endpoint_even_without_a_notice():
    rig = recovery_rig()
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    current = target(endpoint_generation=2, address="192.0.2.22")
    # The snapshot is deliberately older than DiscoveryPort.current().
    rig.discovery.views[current.receiver_id] = rig.discovery.build_view(current, True)
    await advance_retry(rig)
    after = rig.manager.snapshot(current.receiver_id)
    assert after.stream.endpoint_generation == 2
    assert after.stream.endpoint_fingerprint == "endpoint-a-v2"
    assert after.target.canonical_host == "192.0.2.22"
    assert starts(rig)[-1]["target"]["canonical_host"] == "192.0.2.22"
    assert after.session_generation == before.session_generation


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_failed_network_attempts_keep_intent_and_source_until_a_later_success(mode):
    rig = recovery_rig()
    before = await start_session(rig, mode=mode)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    for delay in [1, 2, 4, 8, 16, 30, 30, 30]:
        state = rig.manager.snapshot(target().receiver_id)
        assert state.next_retry_at - rig.clock.now() == delay
        rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.transport_failed)))
        await advance_retry(rig)
        state = rig.manager.snapshot(target().receiver_id)
        assert state.transport is TransportState.Reconnecting
        assert state.intent_active
        assert state.display_handle == before.display_handle
        assert state.physical_source == before.physical_source
        assert state.session_generation == before.session_generation
    await advance_retry(rig)
    assert rig.manager.snapshot(target().receiver_id).transport is TransportState.Streaming
    assert not rig.display.removed
    assert not rig.capture.released


@pytest.mark.asyncio
async def test_timer_attempt_refreshes_offline_authority_before_starting():
    rig = recovery_rig()
    await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    current = target(endpoint_generation=2, address="192.0.2.23")
    # Discovery has changed, but its notice is deliberately withheld. The
    # scheduled attempt must refresh authority before choosing a start path.
    rig.discovery.views[current.receiver_id] = rig.discovery.build_view(current, False)
    await advance_retry(rig)
    after = rig.manager.snapshot(current.receiver_id)
    assert after.target is None
    assert not after.online
    assert after.transport is TransportState.Reconnecting
    assert after.next_retry_at == rig.clock.now() + 2
    assert len(starts(rig)) == 1


@pytest.mark.asyncio
async def test_timer_refreshes_authority_before_inactive_attempt_guard():
    rig = recovery_rig()
    destination = target()
    await start_session(rig, destination)
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await settle(rig)
    rig.discovery.views[destination.receiver_id] = rig.discovery.build_view(destination, False)
    context = rig.manager._contexts[destination.receiver_id]._context(
        rig.manager._contexts[destination.receiver_id].state.operation_id,
    )
    rig.manager.ingest(TimerEvent(
        receiver_id=destination.receiver_id, kind="reconnect", context=context,
    ))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.target is None
    assert not after.online
    assert not after.intent_active


@pytest.mark.asyncio
async def test_stop_retires_terminal_error_before_a_later_intent_reconnects():
    gate = Deferred()
    rig = recovery_rig()
    first = await start_session(rig)
    rig.transport.queue_result("stop_stream", gate)
    rig.transport.emit(stream_event(rig, TransportEventKind.failed, payload=FailurePayload(
        diagnostic=diagnostic(ErrorCode.receiver_rejected),
    )))
    await settle(rig)
    await rig.manager.dispatch(Stop(receiver_id=first.receiver_id))
    await settle(rig)
    gate.resolve(Ok(value=None))
    await settle(rig)
    stopped = rig.manager.snapshot(first.receiver_id)
    assert not stopped.intent_active
    assert stopped.stream is None
    assert stopped.display is DisplayState.None_
    fresh = await start_session(rig)
    assert fresh.session_generation > first.session_generation
    assert fresh.transport is TransportState.Streaming
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    after = rig.manager.snapshot(first.receiver_id)
    assert after.transport is TransportState.Reconnecting
    assert after.next_retry_at == rig.clock.now() + 1
    assert after.last_error is None


@pytest.mark.asyncio
async def test_stop_cancels_network_timer_and_late_timer_cannot_restart():
    rig = recovery_rig()
    await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    assert rig.scheduler.pending() == (1,)
    await rig.manager.dispatch(Stop(receiver_id=target().receiver_id))
    await settle(rig)
    count = len(starts(rig))
    assert not rig.scheduler.pending()
    rig.scheduler.advance(300)
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert not after.intent_active
    assert after.stream is None
    assert after.display is DisplayState.None_
    assert after.next_retry_at is None
    assert len(starts(rig)) == count


@pytest.mark.asyncio
async def test_idle_offline_start_only_refreshes_discovery_and_reappearance_creates_no_intent():
    rig = recovery_rig()
    destination = target()
    rig.discovery.add_target(destination)
    rig.discovery.remove_target(destination.receiver_id)
    await settle(rig)
    before = rig.manager.snapshot(destination.receiver_id)
    assert before.target is None
    assert before.phase is SessionPhase.Offline
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await settle(rig)
    assert rig.discovery.journal.calls("discovery.refresh")
    assert not starts(rig)
    assert not rig.pairing.journal.calls("pairing.begin_pairing")
    assert not rig.manager.snapshot(destination.receiver_id).intent_active
    rig.discovery.add_target(target(endpoint_generation=2))
    await settle(rig)
    after = rig.manager.snapshot(destination.receiver_id)
    assert after.phase is SessionPhase.Available
    assert not after.intent_active
    assert not starts(rig)
    assert not rig.scheduler.pending()


@pytest.mark.asyncio
async def test_retry_pair_and_mute_while_absent_never_use_the_last_known_endpoint():
    rig = recovery_rig()
    before = await start_session(rig, audio=True)
    rig.discovery.remove_target(target().receiver_id)
    await settle(rig)
    rig.journal.clear()
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await rig.manager.dispatch(Pair(receiver_id=target().receiver_id))
    await rig.manager.dispatch(SetMuted(receiver_id=target().receiver_id, muted=True))
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert state.target is None
    assert state.intent_active
    assert state.display_handle == before.display_handle
    assert not starts(rig)
    assert not rig.journal.calls("pairing.begin_pairing")
    assert not rig.journal.calls("transport.set_muted")
    assert rig.discovery.journal.calls("discovery.refresh")


@pytest.mark.asyncio
async def test_stable_identity_endpoint_change_preserves_trust_but_retires_live_challenge():
    rig = recovery_rig()
    destination = target()
    rig.discovery.add_target(destination)
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await settle(rig)
    challenge = rig.manager.snapshot(destination.receiver_id).challenge
    assert challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=destination.receiver_id, challenge_id=challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Trusted
    rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.22"))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Trusted
    assert not rig.pairing.journal.calls("pairing.mark_invalid")

    # An outstanding challenge still belongs to an exact endpoint, even for a stable ID.
    other = target("b")
    rig.discovery.add_target(other)
    await rig.manager.dispatch(Pair(receiver_id=other.receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(other.receiver_id).challenge is not None
    rig.discovery.add_target(target("b", endpoint_generation=2, address="192.0.2.23"))
    await settle(rig)
    state = rig.manager.snapshot(other.receiver_id)
    assert state.challenge is None
    assert state.trust is TrustState.Untrusted
    assert rig.pairing.journal.calls("pairing.cancel_pairing")


@pytest.mark.asyncio
async def test_lost_fallback_continuity_discards_trust_and_resolution_without_removing_output():
    rig = recovery_rig()
    before = await start_session(rig)
    destination = target(endpoint_generation=2, address="192.0.2.24")
    view = rig.discovery.build_view(destination, True)
    view = replace(view, record_set=replace(
        view.record_set, identity_kind="fingerprint", continuity_verified=False,
    ))
    rig.discovery.views[destination.receiver_id] = view
    rig.discovery.emit(DiscoveryEvent(kind="updated", receiver=view, diagnostic=None))
    await settle(rig)
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.Untrusted
    assert state.resolution is None
    assert state.display_handle == before.display_handle
    assert state.intent_active
    assert rig.pairing.journal.calls("pairing.mark_invalid")
    await rig.manager.dispatch(Retry(receiver_id=destination.receiver_id))
    rig.scheduler.advance(60)
    await settle(rig)
    assert len(starts(rig)) == 1
    assert not rig.display.removed
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).challenge is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_stale_reconnect_result_never_revives_or_tears_down_a_new_target(mode, outcome):
    gate = Deferred()

    class HeldReconnect(ContextTransport):
        async def start_stream(self, destination, source, audio, operation):
            if len(self.journal.calls("transport.start_stream")) == 1 and not getattr(self, "held", False):
                self.held = True
                result = (await super().start_stream(destination, source, audio, operation)
                          if outcome == "success" else Err(error=diagnostic(ErrorCode.transport_failed)))
                await gate.wait()
                return result
            return await super().start_stream(destination, source, audio, operation)

    rig = recovery_rig(transport=HeldReconnect())
    before = await start_session(rig, mode=mode)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    await advance_retry(rig)
    rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.25"))
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    current = rig.manager.snapshot(target().receiver_id)
    assert current.transport is TransportState.Streaming
    assert current.stream.endpoint_generation == 2
    gate.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == current
    assert tuple(rig.transport.streams.values()) == (current.stream,)
    assert not rig.manager.pending_resources(target().receiver_id)
    assert current.display_handle == before.display_handle
    assert not rig.display.removed
    assert not rig.capture.released
    assert "display.prepare_teardown" not in rig.journal.names()


@pytest.mark.asyncio
async def test_unconfirmed_network_stream_release_retains_output_and_blocks_duplicates_until_retry_cleanup():
    rig = recovery_rig()
    before = await start_session(rig)
    rig.transport.queue_result("stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    uncertain = rig.manager.snapshot(target().receiver_id)
    assert uncertain.cleanup_pending
    assert uncertain.stream == before.stream
    assert uncertain.display_handle == before.display_handle
    assert uncertain.display is DisplayState.Owned
    assert not rig.scheduler.pending()
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    assert len(starts(rig)) == 1
    await rig.manager.dispatch(RetryCleanup(receiver_id=target().receiver_id))
    await settle(rig)
    await advance_retry(rig)
    assert rig.manager.snapshot(target().receiver_id).transport is TransportState.Streaming
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_receiver_rejection_is_target_local_and_retains_its_virtual_output():
    rig = recovery_rig()
    a = await start_session(rig)
    b = await start_session(rig, target("b", address="192.0.2.11"))
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    rig.transport.queue_result("start_stream", Err(error=diagnostic(ErrorCode.receiver_rejected)))
    await advance_retry(rig)
    failed = rig.manager.snapshot(target().receiver_id)
    assert failed.transport is TransportState.Failed
    assert failed.intent_active
    assert failed.display_handle == a.display_handle
    assert failed.last_error.code is ErrorCode.receiver_rejected
    assert failed.next_retry_at is None
    assert rig.manager.snapshot(b.receiver_id) == b
    assert not rig.display.removed
    await rig.manager.dispatch(Retry(receiver_id=a.receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(a.receiver_id).transport is TransportState.Streaming


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_pairing_completion_waits_for_original_connection_before_reconnecting(mode):
    gate = Deferred()

    class ConnectingPair(ContextTransport):
        async def start_stream(self, destination, source, audio, operation):
            result = await super().start_stream(destination, source, audio, operation)
            if not getattr(self, "asked", False):
                self.asked = True
                self.emit(TransportEvent(
                    kind=TransportEventKind.pairing_required, receiver_id=destination.receiver_id,
                    stream=result.value, endpoint_fingerprint=destination.endpoint_fingerprint,
                    endpoint_generation=destination.endpoint_generation,
                    daemon_instance_uuid=result.value.daemon_instance_uuid,
                    context=operation, payload=EmptyPayload(),
                ))
                await gate.wait()
            return result

    rig = recovery_rig(transport=ConnectingPair())
    rig.discovery.add_target(target())
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(
        mode=mode, source=physical() if mode is Mode.MIRROR else None,
        resolution=decision() if mode is Mode.VIRTUAL else None, audio=AudioPolicy(enabled=False),
    )))
    await settle(rig)
    pairing = rig.manager.snapshot(target().receiver_id)
    assert pairing.challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=pairing.receiver_id, challenge_id=pairing.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle(rig)
    assert len(starts(rig)) == 1
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(pairing.receiver_id)
    assert after.transport is TransportState.Streaming
    assert after.trust is TrustState.Trusted
    assert after.session_generation == pairing.session_generation
    assert after.display_handle == pairing.display_handle
    assert len(rig.transport.streams) == 1
    assert not rig.manager.pending_resources(pairing.receiver_id)
    assert len(starts(rig)) == 2
    assert not rig.display.removed
    assert not rig.capture.released


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_ready_with_retired_trust_never_verifies_the_current_stream(mode):
    rig = recovery_rig(transport=ContextTransport(ready=False))
    before = await start_session(rig, mode=mode)
    event = stream_event(rig, TransportEventKind.ready)
    rig.transport.emit(replace(event, context=replace(
        event.context, trust_generation=before.trust_generation - 1,
    )))
    await settle(rig)
    assert rig.manager.snapshot(before.receiver_id) == before
    rig.transport.emit(event)
    await settle(rig)
    assert rig.manager.snapshot(before.receiver_id).transport is TransportState.Streaming


@pytest.mark.asyncio
async def test_receiver_rejection_notice_is_terminal_after_exact_stream_stop():
    rig = recovery_rig()
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.failed, payload=FailurePayload(
        diagnostic=diagnostic(ErrorCode.receiver_rejected),
    )))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.transport is TransportState.Failed
    assert after.last_error.code is ErrorCode.receiver_rejected
    assert after.stream is None
    assert after.next_retry_at is None
    assert not rig.scheduler.pending()
    assert after.display_handle == before.display_handle
    assert after.intent_active
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_fallback_continuity_loss_at_the_same_endpoint_still_invalidates_trust():
    rig = recovery_rig()
    before = await start_session(rig)
    view = rig.discovery.current(before.receiver_id)
    view = replace(view, record_set=replace(
        view.record_set, identity_kind="fingerprint", continuity_verified=False,
    ))
    rig.discovery.views[before.receiver_id] = view
    rig.discovery.emit(DiscoveryEvent(kind="updated", receiver=view, diagnostic=None))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.trust is TrustState.Untrusted
    assert after.resolution is None
    assert after.stream is None
    assert rig.pairing.journal.calls("pairing.mark_invalid")
    assert after.display_handle == before.display_handle


@pytest.mark.asyncio
async def test_pair_and_mute_refresh_authority_when_discovery_notice_has_not_arrived():
    rig = recovery_rig()
    before = await start_session(rig, audio=True)
    offline = replace(rig.discovery.current(before.receiver_id), online=False)
    rig.discovery.views[before.receiver_id] = offline
    rig.journal.clear()
    await rig.manager.dispatch(SetMuted(receiver_id=before.receiver_id, muted=True))
    await rig.manager.dispatch(Pair(receiver_id=before.receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.target is None
    assert after.display_handle == before.display_handle
    assert not rig.journal.calls("transport.set_muted")
    assert not rig.journal.calls("pairing.begin_pairing")


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize("churn", ["endpoint", "offline"])
async def test_reconnect_completion_rechecks_discovery_even_before_its_notice(outcome, churn):
    gate = Deferred()

    class Held(ContextTransport):
        async def start_stream(self, destination, source, audio, operation):
            if not getattr(self, "held", False) and len(self.journal.calls("transport.start_stream")) == 1:
                self.held = True
                result = (await super().start_stream(destination, source, audio, operation)
                          if outcome == "success" else Err(error=diagnostic(ErrorCode.transport_failed)))
                await gate.wait()
                return result
            return await super().start_stream(destination, source, audio, operation)

    rig = recovery_rig(transport=Held())
    before = await start_session(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    await advance_retry(rig)
    destination = target(endpoint_generation=2, address="192.0.2.27")
    rig.discovery.views[before.receiver_id] = rig.discovery.build_view(destination, churn != "offline")
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.transport is TransportState.Reconnecting
    assert after.stream is None
    assert after.target == (destination if churn == "endpoint" else None)
    assert after.display_handle == before.display_handle
    assert after.intent_active
    assert not rig.transport.streams
    assert not rig.manager.pending_resources(before.receiver_id)
    assert not rig.display.removed
    assert len(rig.transport._subscribers) == 1
    if churn == "endpoint":
        await advance_retry(rig)
        assert rig.manager.snapshot(before.receiver_id).stream.endpoint_generation == 2


@pytest.mark.asyncio
async def test_mirror_keeps_its_existing_stream_acceptance_and_audio_contract_on_retry():
    rig = recovery_rig(transport=ContextTransport(ready=False))
    before = await start_session(rig, mode=Mode.MIRROR, audio=True)
    assert before.transport is TransportState.Streaming
    assert before.audio is AudioState.Enabled
    await rig.manager.dispatch(SetMuted(receiver_id=before.receiver_id, muted=True))
    await settle(rig)
    assert rig.manager.snapshot(before.receiver_id).audio is AudioState.Muted
    rig.transport.emit(stream_event(rig, TransportEventKind.network_lost))
    await settle(rig)
    await advance_retry(rig)
    after = rig.manager.snapshot(before.receiver_id)
    assert after.transport is TransportState.Streaming
    assert after.audio is AudioState.Enabled
    assert after.session_generation == before.session_generation
    assert after.physical_source == before.physical_source
    assert after.stream != before.stream
    assert not rig.display.created
