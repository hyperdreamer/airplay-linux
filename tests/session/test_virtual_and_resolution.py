from __future__ import annotations

from dataclasses import asdict, replace

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel, ChangeResolution, ChooseResolution, ConfirmSwitch, DisplayEvent,
    EmptyPayload, ModePayload, Pair, RecreateAccepted, Retry, RetryCleanup,
    RetryRevocation, Start, StartVideoOnly, Stop, SubmitPairing, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import SensitiveText
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import (
    AudioState, DisplayState, Err, ErrorCode, Mode, Ok, ReceiverCapabilities, SessionPhase,
    StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import Route
from airplay_linux.session.testing import SessionRig
from airplay_linux.session.virtual import build_virtual_handlers, compose_virtual_handlers
from airplay_linux.testing.adapters import Deferred, FakeDisplay
from airplay_linux.testing.builders import physical, target, virtual_handle
from virtual_scenarios import (
    ContextTransport, HD, SMALL, decision, make_rig, production_display, settle,
    stream_event, virtual_handlers,
)


async def start_virtual(rig, destination=None, size=HD):
    destination = destination or target()
    rig.discovery.add_target(destination)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.VIRTUAL, resolution=decision(destination, size)),
    ))
    await settle(rig)
    return rig.manager.snapshot(destination.receiver_id)


def display_event(owned, kind="helper_exit", destination=None, **changes):
    destination = destination or target()
    return replace(DisplayEvent(
        kind=kind, receiver_id=destination.receiver_id, creation_operation_id=None,
        owner_uuid=owned.owner_uuid, connector=owned.source.connector,
        output_id=owned.source.kscreen_output_id, helper=owned.helper,
        handle=owned, diagnostic=None,
    ), **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_before_save", [False, True])
async def test_resolution_uses_shared_preferences_and_publishes_success(monkeypatch, stop_before_save):
    rig = make_rig()
    destination = target()
    original = SavedReceiver(
        receiver_id=destination.receiver_id, service_metadata=destination.service_records,
        display_name="Saved name", last_endpoint=destination,
        continuity_fingerprint=destination.endpoint_fingerprint, credential_ref=None,
        remembered_resolution=None, remembered_volume=30,
    )
    rig.receivers.receivers[destination.receiver_id] = original
    writer = rig.deps.preferences
    update = writer.update
    gate, entered = Deferred(), []
    async def held(record_set, **kwargs):
        entered.append((record_set, kwargs))
        await gate.wait()
        return await update(record_set, **kwargs)
    monkeypatch.setattr(writer, "update", held)
    publications = []
    subscription = rig.manager.subscribe(publications.append)
    state = await start_virtual(rig, size=SMALL)
    assert len(entered) == 1
    assert entered[0][1]["remembered_resolution"].size == SMALL
    assert entered[0][1]["still_current"]()
    if stop_before_save:
        await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
        await settle(rig)
    publications.clear()
    gate.resolve(None)
    await settle(rig)
    subscription.close()
    stored = rig.receivers.receivers[destination.receiver_id]
    if stop_before_save:
        assert stored == original
    else:
        assert stored.remembered_volume == 30
        assert stored.remembered_resolution.size == SMALL
        assert stored.display_name == original.display_name
        assert publications, "successful preference effects must refresh snapshot subscribers"
        assert rig.manager.snapshot(destination.receiver_id) == state


@pytest.mark.asyncio
async def test_unknown_mode_precedes_any_output():
    rig = SessionRig(virtual_handlers)
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(
        receiver_id=target().receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.ResolutionRequired
    assert "display.create" not in rig.display.journal.names()


@pytest.mark.asyncio
async def test_explicit_picker_creates_and_verifies_tv_size_before_start():
    rig = make_rig()
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(
        receiver_id=target().receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await settle(rig)
    await rig.manager.dispatch(ChooseResolution(receiver_id=target().receiver_id, decision=decision(size=SMALL)))
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert state.phase is SessionPhase.VirtualDisplay
    assert state.display is DisplayState.Owned
    assert state.observed_resolution == SMALL
    assert state.display_handle.source.size == SMALL
    names = rig.journal.names()
    assert names.index("display.create") < names.index("transport.start_stream")
    start = rig.journal.calls("transport.start_stream")[0]
    assert start["source"] == asdict(state.display_handle.source)
    assert "capture.select" not in names
    assert rig.receivers.receivers[target().receiver_id].remembered_resolution.size == SMALL


@pytest.mark.asyncio
@pytest.mark.parametrize("reliable,modes,expected", [
    (True, (SMALL,), SessionPhase.VirtualDisplay),
    (False, (SMALL,), SessionPhase.ResolutionRequired),
    (True, (SMALL, HD), SessionPhase.ResolutionRequired),
])
async def test_query_requires_one_reliable_mode(reliable, modes, expected):
    rig = make_rig()
    rig.discovery.add_target(target())
    rig.transport.queue_result("query_modes", Ok(value=ReceiverCapabilities(
        modes=modes, reliable=reliable, evidence="receiver query",
    )))
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(mode=Mode.VIRTUAL)))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is expected
    assert bool(rig.display.created) is (expected is SessionPhase.VirtualDisplay)


@pytest.mark.asyncio
@pytest.mark.parametrize("continuous", [True, False])
async def test_remembered_resolution_requires_identity_continuity(continuous):
    rig = make_rig()
    destination = target()
    rig.discovery.add_target(destination)
    view = rig.discovery.current(destination.receiver_id)
    rig.discovery.views[destination.receiver_id] = replace(view, record_set=replace(
        view.record_set, identity_kind="fingerprint", continuity_verified=continuous,
    ))
    rig.receivers.receivers[destination.receiver_id] = SavedReceiver(
        receiver_id=destination.receiver_id, service_metadata=destination.service_records,
        display_name="TV", last_endpoint=destination,
        continuity_fingerprint=destination.endpoint_fingerprint,
        credential_ref=None, remembered_resolution=decision(destination, SMALL),
    )
    await rig.manager.dispatch(Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL)))
    await settle(rig)
    state = rig.manager.snapshot(destination.receiver_id)
    assert bool(rig.display.created) is continuous
    assert state.phase is (SessionPhase.VirtualDisplay if continuous else SessionPhase.ResolutionRequired)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [HD, SMALL])
async def test_provider_refusal_without_association_verifier_never_starts(size):
    rig = make_rig()
    rig.display.queue_result("create", Err(error=diagnostic(ErrorCode.unsupported_operation)))
    state = await start_virtual(rig, size=size)
    assert state.stream is None
    assert state.display_handle is None
    assert "transport.start_stream" not in rig.journal.names()
    assert state.last_error.code is ErrorCode.unsupported_operation


@pytest.mark.asyncio
async def test_published_event_does_not_authorize_start_before_verified_result():
    rig = make_rig()
    gate = Deferred()
    rig.display.queue_result("create", gate)
    await start_virtual(rig)
    state = rig.manager.snapshot(target().receiver_id)
    handle = virtual_handle()
    rig.display.emit(display_event(handle, "reserved", creation_operation_id=state.operation_id,
                                   connector=None, output_id=None, helper=None, handle=None))
    rig.display.emit(display_event(handle, "published", creation_operation_id=state.operation_id))
    await settle(rig)
    pending = rig.manager.snapshot(target().receiver_id)
    assert pending.ownership_uuid == handle.owner_uuid
    assert pending.display_handle is None
    assert "transport.start_stream" not in rig.journal.names()
    gate.resolve(Ok(value=handle))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.VirtualDisplay


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["helper_started", "output_observed", "published"])
async def test_provider_failure_cleans_exact_reservation(stage):
    rig = make_rig()
    rig.display.crash_at(stage)
    state = await start_virtual(rig)
    assert state.stream is None
    assert "transport.start_stream" not in rig.journal.names()
    assert state.display is DisplayState.None_
    assert not rig.display.outputs
    assert not rig.ownerships.records
    assert state.last_error.code is ErrorCode.helper_failed


@pytest.mark.asyncio
async def test_two_receivers_have_distinct_outputs_and_failure_isolation():
    rig = make_rig()
    a = await start_virtual(rig, target("a"))
    b = await start_virtual(rig, target("b"), SMALL)
    assert a.phase is b.phase is SessionPhase.VirtualDisplay
    assert a.display_handle.owner_uuid != b.display_handle.owner_uuid
    assert a.display_handle.source.kscreen_output_id != b.display_handle.source.kscreen_output_id
    rig.display.emit(display_event(a.display_handle))
    await settle(rig)
    assert a.display_handle.owner_uuid in rig.display.removed
    assert rig.manager.snapshot(target("b").receiver_id) == b
    assert b.display_handle.owner_uuid in rig.display.handles


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "absent", "failed", "daemon"])
async def test_transport_only_failure_retains_verified_output(failure):
    rig = make_rig()
    before = await start_virtual(rig)
    if failure == "absent":
        rig.discovery.remove_target(target().receiver_id)
    else:
        kind = {"network": TransportEventKind.network_lost, "failed": TransportEventKind.failed,
                "daemon": TransportEventKind.daemon_exit}[failure]
        rig.transport.emit(stream_event(rig, kind))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.display is DisplayState.Owned
    assert after.display_handle == before.display_handle
    assert after.intent_active
    assert after.transport in (TransportState.Reconnecting, TransportState.Failed, TransportState.Connecting)
    assert not rig.display.removed
    assert "display.prepare_teardown" not in rig.journal.names()


@pytest.mark.asyncio
async def test_retry_reuses_output_with_new_lease_and_same_generation():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.failed))
    await settle(rig)
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle == before.display_handle
    assert after.session_generation == before.session_generation
    assert after.stream.lease_id != before.stream.lease_id
    assert after.stream.start_operation_id != before.stream.start_operation_id
    assert len(rig.display.created) == 1
    assert before.stream.lease_id not in rig.transport.streams


@pytest.mark.asyncio
async def test_helper_failure_marks_before_transport_stop_then_removes_output():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.display.emit(display_event(before.display_handle))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.display is DisplayState.None_
    assert after.stream is None
    names = rig.journal.names()
    assert names.index("display.prepare_teardown") < names.index("transport.stop_stream") < names.index("display.remove")
    assert before.display_handle.owner_uuid in rig.display.removed


@pytest.mark.asyncio
async def test_stale_helper_identity_cannot_remove_current_output():
    rig = make_rig()
    before = await start_virtual(rig)
    stale = replace(before.display_handle, helper=replace(before.display_handle.helper, start_ticks=99999))
    rig.display.emit(display_event(stale))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == before
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_backend_pairing_before_output_resumes_creation_only_after_trust():
    class PairBeforeModes(ContextTransport):
        async def query_modes(self, destination, operation):
            if not getattr(self, "asked", False):
                self.asked = True
                self.emit(TransportEvent(
                    kind=TransportEventKind.pairing_required, receiver_id=destination.receiver_id,
                    stream=None, endpoint_fingerprint=destination.endpoint_fingerprint,
                    endpoint_generation=destination.endpoint_generation,
                    daemon_instance_uuid=self.daemon_instance_uuid, context=operation,
                    payload=EmptyPayload(),
                ))
            return Ok(value=ReceiverCapabilities(modes=(HD,), reliable=True, evidence="query"))

    rig = make_rig(transport=PairBeforeModes())
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(mode=Mode.VIRTUAL)))
    await settle(rig)
    before = rig.manager.snapshot(target().receiver_id)
    assert before.phase is SessionPhase.Pairing
    assert before.challenge is not None
    assert not rig.display.created
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=target().receiver_id, challenge_id=before.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.trust is TrustState.Trusted
    assert after.session_generation == before.session_generation


@pytest.mark.asyncio
async def test_post_output_pairing_retains_output_and_continues_same_intent():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.pairing_required))
    await settle(rig)
    pairing = rig.manager.snapshot(target().receiver_id)
    assert pairing.phase is SessionPhase.Pairing
    assert pairing.display_handle == before.display_handle
    assert pairing.challenge is not None
    assert not rig.display.removed
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=target().receiver_id, challenge_id=pairing.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle == before.display_handle
    assert after.session_generation == before.session_generation


@pytest.mark.asyncio
@pytest.mark.parametrize("expire", [False, True])
async def test_cancel_or_expiry_of_retained_output_pairing_runs_ordered_cleanup(expire):
    rig = make_rig()
    before = await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.pairing_required))
    await settle(rig)
    challenge = rig.manager.snapshot(target().receiver_id).challenge
    assert challenge is not None
    if expire:
        rig.scheduler.advance(121)
    else:
        await rig.manager.dispatch(Cancel(receiver_id=target().receiver_id))
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert state.display_handle is None
    assert state.stream is None
    assert before.display_handle.owner_uuid in rig.display.removed
    assert state.trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_uncertain_pairing_cancel_blocks_replacement_until_revocation():
    rig = make_rig()
    await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.pairing_required))
    await settle(rig)
    rig.pairing.queue_result("cancel_pairing", Err(error=diagnostic(ErrorCode.trust_cleanup_required)))
    await rig.manager.dispatch(Cancel(receiver_id=target().receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).trust is TrustState.CleanupRequired
    begins = rig.journal.names().count("pairing.begin_pairing")
    await rig.manager.dispatch(Pair(receiver_id=target().receiver_id))
    await start_virtual(rig)
    assert rig.journal.names().count("pairing.begin_pairing") == begins
    assert rig.manager.snapshot(target().receiver_id).trust is TrustState.CleanupRequired
    await rig.manager.dispatch(RetryRevocation(receiver_id=target().receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_resolution_change_removes_old_before_reserving_new_and_supersedes_once():
    rig = make_rig()
    before = await start_virtual(rig)
    gate = Deferred()
    rig.transport.queue_result("stop_stream", gate)
    await rig.manager.dispatch(ChangeResolution(receiver_id=target().receiver_id, decision=decision(size=SMALL)))
    await settle(rig)
    during = rig.manager.snapshot(target().receiver_id)
    assert during.phase is SessionPhase.Reconfiguring
    assert during.session_generation == before.session_generation + 1
    assert len(rig.display.created) == 1
    record = rig.ownerships.records[before.display_handle.owner_uuid]
    assert record.desired_state == record.stage == "removing"
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.session_generation == before.session_generation + 1
    assert after.display_handle.source.size == SMALL
    assert after.display_handle.owner_uuid != before.display_handle.owner_uuid
    assert after.stream.lease_id != before.stream.lease_id
    names = rig.journal.names()
    assert names.index("transport.stop_stream") < names.index("display.remove")
    creates = [i for i, name in enumerate(names) if name == "display.create"]
    assert names.index("display.remove") < creates[1]
    assert len(rig.display.outputs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prepare_teardown", "stop_stream", "remove", "create", "start_stream"])
async def test_cancel_at_each_recreation_await_cannot_resurrect_output(method):
    rig = make_rig()
    await start_virtual(rig)
    gate = Deferred()
    adapter = rig.transport if method in ("stop_stream", "start_stream") else rig.display
    adapter.queue_result(method, gate)
    await rig.manager.dispatch(ChangeResolution(receiver_id=target().receiver_id, decision=decision(size=SMALL)))
    await settle(rig)
    await rig.manager.dispatch(Cancel(receiver_id=target().receiver_id))
    await settle(rig)
    if method == "prepare_teardown":
        # The fake's scripted result replaces its default publication. A retry
        # performs that publication after this deliberately suspended attempt.
        gate.resolve(Err(error=diagnostic(ErrorCode.persistence_failed)))
    elif method in ("create", "start_stream"):
        gate.resolve(Err(error=diagnostic(ErrorCode.stale_event)))
    elif method == "remove":
        gate.resolve(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    else:
        gate.resolve(Ok(value=None))
    await settle(rig)
    await rig.manager.dispatch(RetryCleanup(receiver_id=target().receiver_id))
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert not state.intent_active
    assert state.stream is None
    assert state.display_handle is None
    assert not state.cleanup_pending
    assert not rig.display.outputs
    assert not rig.transport.streams


@pytest.mark.asyncio
async def test_failed_removal_intent_blocks_recreation_and_destructive_calls():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.display.queue_result("prepare_teardown", Err(error=diagnostic(ErrorCode.persistence_failed)))
    await rig.manager.dispatch(ChangeResolution(receiver_id=target().receiver_id, decision=decision(size=SMALL)))
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert state.phase is SessionPhase.CleanupRequired
    assert state.stream == before.stream
    assert state.display_handle == before.display_handle
    assert "transport.stop_stream" not in rig.journal.names()
    assert not rig.display.removed
    assert len(rig.display.created) == 1


@pytest.mark.asyncio
async def test_reliable_accepted_mismatch_after_success_stops_only_transport_then_explicit_recreate():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.accepted_mode, payload=ModePayload(size=SMALL, reliable=True)))
    await settle(rig)
    mismatch = rig.manager.snapshot(target().receiver_id)
    assert mismatch.last_error.code is ErrorCode.resolution_mismatch
    assert mismatch.accepted_resolution == SMALL
    assert mismatch.transport is TransportState.Idle
    assert mismatch.stream is None
    assert mismatch.display_handle == before.display_handle
    assert mismatch.display is DisplayState.Owned
    assert not rig.display.removed
    assert "display.prepare_teardown" not in rig.journal.names()
    await rig.manager.dispatch(RecreateAccepted(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle.source.size == SMALL
    assert after.session_generation == before.session_generation + 1
    assert after.display_handle.owner_uuid != before.display_handle.owner_uuid


@pytest.mark.asyncio
async def test_unreliable_accepted_mode_does_not_stop_or_reconfigure():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.transport.emit(stream_event(rig, TransportEventKind.accepted_mode, payload=ModePayload(size=SMALL, reliable=False)))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == before
    assert "transport.stop_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_confirmed_switch_from_mirror_waits_for_release_then_starts_virtual():
    rig = make_rig()
    rig.discovery.add_target(target())
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id, request=StartRequest(mode=Mode.MIRROR, source=physical())))
    await settle(rig)
    before = rig.manager.snapshot(target().receiver_id)
    assert before.phase is SessionPhase.Mirroring
    await rig.manager.dispatch(ConfirmSwitch(
        receiver_id=target().receiver_id, confirmed=True,
        request=StartRequest(mode=Mode.VIRTUAL, resolution=decision()),
    ))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.physical_source is None
    assert after.session_generation == before.session_generation + 1
    names = rig.journal.names()
    assert names.index("capture.release") < names.index("display.create")


def test_composition_rejects_duplicate_routes():
    rig = make_rig()
    virtual = build_virtual_handlers(rig.deps)
    assert set(virtual) == {Route.virtual, Route.resolution}
    with pytest.raises(ValueError, match="duplicate"):
        compose_virtual_handlers(virtual, virtual)


@pytest.mark.asyncio
async def test_helper_loss_before_create_result_retires_creation_and_cleans_late_handle():
    gate = Deferred()

    class LostHelper(FakeDisplay):
        async def create(self, receiver_id, endpoint_fingerprint, size, operation):
            result = await super().create(receiver_id, endpoint_fingerprint, size, operation)
            self.emit(display_event(result.value, "helper_exit"))
            await gate.wait()
            return result

    display = LostHelper()
    rig = make_rig(display=display)
    before = await start_virtual(rig)
    assert before.cleanup_pending
    assert before.stream is None
    assert before.last_error.code is ErrorCode.helper_failed
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.display is DisplayState.None_
    assert not display.outputs
    assert not rig.ownerships.records
    assert not rig.manager.pending_resources(target().receiver_id)
    assert "transport.start_stream" not in rig.journal.names()


@pytest.mark.asyncio
async def test_output_confirmation_survives_endpoint_churn_without_recreating():
    rig = make_rig()
    gate = Deferred()
    rig.ownerships.queue_result("all", gate)
    before = await start_virtual(rig)
    assert before.display_handle is not None
    assert before.stream is None
    replacement = target(endpoint_generation=2, address="192.0.2.12")
    rig.discovery.add_target(replacement)
    gate.resolve(Ok(value=tuple(rig.ownerships.records.values())))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.session_generation == before.session_generation
    assert after.display_handle == before.display_handle
    assert after.stream.endpoint_fingerprint == replacement.endpoint_fingerprint
    assert len(rig.display.created) == 1
    assert not rig.display.removed


@pytest.mark.asyncio
async def test_completed_output_is_retained_when_receiver_vanishes_during_create():
    gate = Deferred()

    class HeldDisplay(FakeDisplay):
        async def create(self, receiver_id, endpoint_fingerprint, size, operation):
            result = await super().create(receiver_id, endpoint_fingerprint, size, operation)
            await gate.wait()
            return result

    display = HeldDisplay()
    rig = make_rig(display=display)
    before = await start_virtual(rig)
    rig.discovery.remove_target(target().receiver_id)
    gate.resolve(Ok(value=None))
    await settle(rig)
    absent = rig.manager.snapshot(target().receiver_id)
    assert absent.display is DisplayState.Owned
    assert absent.stream is None
    assert absent.transport is TransportState.Reconnecting
    assert absent.session_generation == before.session_generation
    assert absent.intent_active
    assert not display.removed
    rig.discovery.add_target(target(endpoint_generation=2))
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.VirtualDisplay
    assert len(display.created) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["create", "stream"])
async def test_cancel_releases_late_successes_before_reporting_available(stage):
    gate = Deferred()

    class HeldDisplay(FakeDisplay):
        async def create(self, receiver_id, endpoint_fingerprint, size, operation):
            result = await super().create(receiver_id, endpoint_fingerprint, size, operation)
            if stage == "create":
                await gate.wait()
            return result

    class HeldTransport(ContextTransport):
        async def start_stream(self, destination, source, audio, operation):
            result = await super().start_stream(destination, source, audio, operation)
            if stage == "stream":
                await gate.wait()
            return result

    rig = make_rig(display=HeldDisplay(), transport=HeldTransport())
    await start_virtual(rig)
    await rig.manager.dispatch(Cancel(receiver_id=target().receiver_id))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).cleanup_pending
    assert "display.remove" not in rig.journal.names()
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.Available
    assert not rig.display.outputs
    assert not rig.transport.streams
    assert not rig.ownerships.records
    assert not rig.manager.pending_resources(target().receiver_id)


@pytest.mark.asyncio
async def test_pairing_while_connecting_keeps_output_and_retries_after_original_call_finishes():
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

    rig = make_rig(transport=ConnectingPair())
    pairing = await start_virtual(rig)
    assert pairing.phase is SessionPhase.Pairing
    assert pairing.display is DisplayState.Owned
    assert pairing.challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=target().receiver_id, challenge_id=pairing.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle(rig)
    assert len(rig.journal.calls("transport.start_stream")) == 1
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle == pairing.display_handle
    assert after.session_generation == pairing.session_generation
    assert len(rig.display.created) == 1
    assert len(rig.transport.streams) == 1


@pytest.mark.asyncio
async def test_stop_during_retained_pause_adopts_exact_stop_without_double_dispatch():
    rig = make_rig()
    await start_virtual(rig)
    gate = Deferred()
    rig.transport.queue_result("stop_stream", gate)
    rig.transport.emit(stream_event(rig, TransportEventKind.pairing_required))
    await settle(rig)
    await rig.manager.dispatch(Stop(receiver_id=target().receiver_id))
    await settle(rig)
    assert len(rig.journal.calls("transport.stop_stream")) == 1
    assert not rig.display.removed
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.Available
    assert not rig.transport.streams
    assert not rig.display.outputs


@pytest.mark.asyncio
async def test_mismatch_cannot_be_retried_or_cleared_by_delayed_ready():
    rig = make_rig()
    before = await start_virtual(rig)
    ready = stream_event(rig, TransportEventKind.ready)
    rig.transport.emit(stream_event(rig, TransportEventKind.accepted_mode, payload=ModePayload(size=SMALL, reliable=True)))
    await settle(rig)
    rig.transport.emit(ready)
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.accepted_resolution == SMALL
    assert after.last_error.code is ErrorCode.resolution_mismatch
    assert after.stream is None
    assert after.display_handle == before.display_handle
    assert len(rig.journal.calls("transport.start_stream")) == 1


@pytest.mark.asyncio
async def test_virtual_audio_failure_requires_explicit_video_only_and_keeps_output():
    rig = make_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_unavailable)))
    before = await start_virtual(rig)
    assert before.phase is SessionPhase.AudioDecisionRequired
    assert before.display is DisplayState.Owned
    assert before.stream is None
    await rig.manager.dispatch(StartVideoOnly(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.display_handle == before.display_handle
    assert after.audio is AudioState.Disabled


@pytest.mark.asyncio
async def test_remembered_mode_contradicted_by_reliable_query_requires_picker():
    rig = make_rig()
    destination = target()
    rig.discovery.add_target(destination)
    rig.receivers.receivers[destination.receiver_id] = SavedReceiver(
        receiver_id=destination.receiver_id, service_metadata=destination.service_records,
        display_name="TV", last_endpoint=destination,
        continuity_fingerprint=destination.endpoint_fingerprint,
        credential_ref=None, remembered_resolution=decision(destination, HD),
    )
    rig.transport.queue_result("query_modes", Ok(value=ReceiverCapabilities(
        modes=(SMALL,), reliable=True, evidence="query",
    )))
    await rig.manager.dispatch(Start(receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL)))
    await settle(rig)
    assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.ResolutionRequired
    assert not rig.display.created


async def test_production_association_verifier_rejects_kscreen_mismatch_after_selection(tmp_path, monkeypatch):
    pytest.importorskip("airplay_linux.display.kde", reason="display lane is not present in this standalone checkout")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=True)
    rig = make_rig(display=provider, ownerships=store)
    observer = desktop.observer
    original_observation = observer.observation
    reads = 0

    async def observation_with_contradiction():
        nonlocal reads
        reads += 1
        if reads == 5:
            desktop.entries[-1]["name"] = "MYSTERY-9"
            desktop.entries[-1]["type"] = "Unknown"
        return await original_observation()

    observer.observation = observation_with_contradiction
    state = await start_virtual(rig)

    assert reads >= 5
    assert state.stream is None
    assert state.display is not DisplayState.Owned
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.ownership_mismatch
    assert all(record.stage != "published" for record in store.records.values())


@pytest.mark.asyncio
async def test_preexisting_exact_size_output_is_not_adopted_without_helper_output(tmp_path, monkeypatch):
    pytest.importorskip("airplay_linux.display.kde", reason="display lane is not present in this standalone checkout")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    existing = dict(
        id=3, name="DP-1", type="Physical", enabled=True, pos={"x": 0, "y": 0},
        scale=1, currentModeId="1", size={"width": HD.width, "height": HD.height},
        modes=[{"id": "1", "size": {"width": HD.width, "height": HD.height}, "refreshRate": 60}],
    )
    provider, desktop, store = production_display(
        tmp_path, verifier=True, initial_entries=[existing], create_output=False,
    )
    rig = make_rig(display=provider, ownerships=store)

    state = await start_virtual(rig)

    assert state.stream is None
    assert state.display is not DisplayState.Owned
    assert all(record.stage != "published" for record in store.records.values())
    assert [entry["name"] for entry in desktop.entries] == ["DP-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier", [True, False])
async def test_production_display_provider_requires_association_verifier(tmp_path, monkeypatch, verifier):
    pytest.importorskip("airplay_linux.display.kde", reason="display lane is not present in this standalone checkout")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=verifier)
    events = []
    provider.subscribe(events.append)
    rig = make_rig(display=provider, ownerships=store)
    state = await start_virtual(rig)
    if verifier:
        assert state.phase is SessionPhase.VirtualDisplay
        assert state.display is DisplayState.Owned
        record = store.records[state.display_handle.owner_uuid]
        assert record.stage == "published"
        assert record.observed_size == state.resolution.size
        await rig.manager.dispatch(Stop(receiver_id=target().receiver_id))
        await settle(rig)
        assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.Available
        assert not desktop.entries
        assert not store.records
    else:
        assert state.stream is None
        assert state.display is not DisplayState.Owned
        assert "transport.start_stream" not in rig.journal.names()
        assert all(event.kind != "published" for event in events)
        assert state.last_error.code in (ErrorCode.unsupported_operation, ErrorCode.ownership_mismatch)


@pytest.mark.asyncio
async def test_virtual_to_mirror_switch_waits_for_confirmed_output_removal():
    rig = make_rig()
    before = await start_virtual(rig)
    rig.capture.add_physical(physical())
    gate = Deferred()
    rig.display.queue_result("remove", gate)
    await rig.manager.dispatch(ConfirmSwitch(
        receiver_id=target().receiver_id, confirmed=True,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await settle(rig)
    assert "capture.select" not in rig.journal.names()
    gate.resolve(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.CleanupRequired
    await rig.manager.dispatch(RetryCleanup(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.Mirroring
    assert after.session_generation == before.session_generation + 1
    assert after.display_handle is None
    assert not rig.display.outputs


@pytest.mark.asyncio
@pytest.mark.parametrize("late", ["completion", "event"])
async def test_audio_cannot_clear_mismatch_diagnostic_while_stop_is_pending(late):
    rig = make_rig()
    audio = Deferred()
    if late == "completion":
        rig.transport.queue_result("start_audio", audio)
    await start_virtual(rig)
    recovered = stream_event(rig, TransportEventKind.audio_recovered)
    stop = Deferred()
    rig.transport.queue_result("stop_stream", stop)
    rig.transport.emit(stream_event(rig, TransportEventKind.accepted_mode, payload=ModePayload(size=SMALL, reliable=True)))
    if late == "completion":
        audio.resolve(Ok(value=None))
    else:
        rig.transport.emit(recovered)
    await settle(rig)
    state = rig.manager.snapshot(target().receiver_id)
    assert state.accepted_resolution == SMALL
    assert state.last_error.code is ErrorCode.resolution_mismatch
    stop.resolve(Ok(value=None))
    await settle(rig)
