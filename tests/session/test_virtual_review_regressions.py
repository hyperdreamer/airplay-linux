"""Virtual contract regressions over deterministic ports and the real transport."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from uuid import UUID

import pytest

from airplay_linux.backend.contract import AckReply
from airplay_linux.backend.control import ApplicationContractProfile, BackendOperation, Handshake
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import Retry, RetryCleanup, Stop, TransportEventKind
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import DisplayState, Err, ErrorCode, Ok, SessionPhase, TransportState
from airplay_linux.testing.adapters import Deferred, FakeDisplay, SequenceIds
from airplay_linux.testing.builders import context, target
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.transport.doubletake import DoubleTakeTransport
from test_virtual_and_resolution import start_virtual
from virtual_scenarios import (
    ContextTransport,
    HD,
    SMALL,
    make_rig,
    production_display,
    settle,
    stream_event,
)

pytestmark = pytest.mark.asyncio


def _audit_foreign_output():
    # Even an exact owner-name match cannot replace the temporal fence. The
    # deterministic fixture reserves UUID(int=2500) for its first creation.
    return dict(
        id=19,
        name="Virtual-airplay-da1f0aef916590efb0292edbd28be5cd",
        type="Virtual",
        enabled=True,
        pos={"x": 0, "y": 0},
        scale=1,
        currentModeId="1",
        size={"width": HD.width, "height": HD.height},
        modes=[{
            "id": "1",
            "size": {"width": HD.width, "height": HD.height},
            "refreshRate": 60,
        }],
    )


async def test_preexisting_exact_size_virtual_output_still_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(
        tmp_path,
        verifier=True,
        initial_entries=[_audit_foreign_output()],
        create_output=False,
    )
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())

    assert isinstance(result, Err), result
    assert result.error.code is ErrorCode.ownership_mismatch
    assert not store.records
    assert not desktop.associations
    assert len(desktop.entries) == 1


async def test_launch_gap_foreign_output_is_refused_and_never_published(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=True, create_output=False)
    original_launch = provider.helper.launch

    async def launch_with_foreign_output(*args):
        desktop.entries.append(_audit_foreign_output())
        return await original_launch(*args)

    provider.helper.launch = launch_with_foreign_output
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())

    assert isinstance(result, Err), result
    assert desktop.associations == {}
    assert all(record.stage != "published" for record in store.records.values())


async def test_replayed_prelaunch_observation_cannot_prove_current_output(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=True, create_output=False)
    original_launch = provider.helper.launch
    cached = []

    async def launch_with_cached_observation(*args):
        desktop.entries.append(_audit_foreign_output())
        observation = await desktop.observer.observation()
        assert isinstance(observation, Ok)
        cached.append(observation)
        launched = await original_launch(*args)
        desktop.entries.clear()

        async def replay():
            return cached[0]

        desktop.observer.observation = replay
        return launched

    provider.helper.launch = launch_with_cached_observation
    outcomes = []
    original_verify = desktop.output_verifier._verify

    async def record_verify(*args):
        result = await original_verify(*args)
        outcomes.append(result)
        return result

    desktop.output_verifier._verify = record_verify
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())

    assert isinstance(result, Err), result
    assert cached and outcomes
    assert isinstance(outcomes[0], Err)
    assert all(record.stage != "published" for record in store.records.values())


async def test_unconditional_verifier_cannot_publish_prelaunch_foreign_output(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=True, create_output=False)
    original_launch = provider.helper.launch

    async def launch_with_foreign_output(*args):
        desktop.entries.append(_audit_foreign_output())
        return await original_launch(*args)

    async def unconditional_success(*args):
        return Ok(value=None)

    provider.helper.launch = launch_with_foreign_output
    desktop.output_verifier._verify = unconditional_success
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())

    assert isinstance(result, Err), result
    assert desktop.associations == {}
    assert all(record.stage != "published" for record in store.records.values())


@pytest.mark.parametrize("churn", ["offline", "endpoint"])
@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_pending_connection_churn_retains_verified_output(churn, outcome):
    # Break caught: routing a retired connection result into whole-session Stop.
    gate = Deferred()

    class HeldTransport(ContextTransport):
        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            if getattr(self, "returned", False):
                return await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            self.returned = True
            result = (await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
                      if outcome == "success" else Err(error=diagnostic(ErrorCode.transport_failed)))
            await gate.wait()
            return result

    rig = make_rig(transport=HeldTransport())
    before = await start_virtual(rig)
    assert before.display is DisplayState.Owned
    assert before.stream is None
    owner = rig.ownerships.records[before.ownership_uuid]
    replacement = target(endpoint_generation=2, address="192.0.2.33")
    if churn == "offline":
        rig.discovery.remove_target(target().receiver_id)
    else:
        rig.discovery.add_target(replacement)
    gate.resolve(Ok(value=None))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.display_handle == before.display_handle
    assert after.display is DisplayState.Owned
    assert after.intent_active
    assert after.session_generation == before.session_generation
    assert rig.ownerships.records[before.ownership_uuid] == owner
    assert "display.prepare_teardown" not in rig.journal.names()
    assert not rig.display.removed
    assert after.stream is None
    assert after.transport is TransportState.Reconnecting
    assert not rig.transport.streams
    assert not rig.manager.pending_resources(target().receiver_id)
    assert len(rig.transport._subscribers) == 1
    stops = rig.journal.calls("transport.stop_stream")
    assert len(stops) == (1 if outcome == "success" else 0)
    assert target().receiver_id not in rig.receivers.receivers
    if churn == "offline":
        rig.discovery.add_target(replacement)
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    recovered = rig.manager.snapshot(target().receiver_id)
    assert recovered.phase is SessionPhase.VirtualDisplay
    assert recovered.display_handle == before.display_handle
    assert recovered.session_generation == before.session_generation
    assert recovered.stream.endpoint_fingerprint == replacement.endpoint_fingerprint
    assert recovered.stream.endpoint_generation == 2
    assert recovered.stream.lease_id != before.transport_lease_id
    assert len(rig.display.created) == 1


@pytest.mark.parametrize("outcome", ["success", "failure"])
async def test_retired_connection_cannot_replace_a_new_connection(outcome):
    gate = Deferred()

    class FirstHeld(ContextTransport):
        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            if destination.endpoint_generation != 1:
                return await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            result = (await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
                      if outcome == "success" else Err(error=diagnostic(ErrorCode.transport_failed)))
            await gate.wait()
            return result

    rig = make_rig(transport=FirstHeld())
    await start_virtual(rig)
    rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.33"))
    await rig.manager.dispatch(Retry(receiver_id=target().receiver_id))
    await settle(rig)
    current = rig.manager.snapshot(target().receiver_id)
    assert current.phase is SessionPhase.VirtualDisplay
    gate.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == current
    assert tuple(rig.transport.streams.values()) == (current.stream,)
    assert not rig.manager.pending_resources(target().receiver_id)
    assert "display.prepare_teardown" not in rig.journal.names()


@pytest.mark.parametrize("finish", ["retry_cleanup", "stop"])
async def test_uncertain_retired_stream_keeps_output_until_exact_cleanup(finish):
    connect = Deferred()

    class Held(ContextTransport):
        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            result = await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            self.acquired = result.value
            await connect.wait()
            return result

    transport = Held()
    rig = make_rig(transport=transport)
    before = await start_virtual(rig)
    rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.33"))
    transport.queue_result("stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    connect.resolve(Ok(value=None))
    await settle(rig)
    uncertain = rig.manager.snapshot(target().receiver_id)
    assert uncertain.display_handle == before.display_handle
    assert uncertain.display is DisplayState.Owned
    assert uncertain.intent_active
    assert uncertain.cleanup_pending
    assert uncertain.last_error.code is ErrorCode.cleanup_timeout
    assert rig.manager.pending_resources(target().receiver_id) == (transport.acquired,)
    assert not rig.display.removed
    assert "display.prepare_teardown" not in rig.journal.names()
    command = RetryCleanup if finish == "retry_cleanup" else Stop
    await rig.manager.dispatch(command(receiver_id=target().receiver_id))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert not after.cleanup_pending
    assert not rig.manager.pending_resources(target().receiver_id)
    assert not transport.streams
    stops = rig.journal.calls("transport.stop_stream")
    assert len(stops) == 2
    assert all(call["handle"] == asdict(transport.acquired) for call in stops)
    if finish == "retry_cleanup":
        assert after.display_handle == before.display_handle
        assert after.intent_active
        assert after.session_generation == before.session_generation
        assert not rig.display.removed
        assert "display.prepare_teardown" not in rig.journal.names()
    else:
        assert after.phase is SessionPhase.Available
        assert not rig.display.outputs


async def test_stop_joins_inflight_retired_stream_cleanup_before_removing_output():
    connect, stop = Deferred(), Deferred()

    class Held(ContextTransport):
        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            result = await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            await connect.wait()
            return result

    rig = make_rig(transport=Held())
    await start_virtual(rig)
    rig.discovery.add_target(target(endpoint_generation=2, address="192.0.2.33"))
    rig.transport.queue_result("stop_stream", stop)
    connect.resolve(Ok(value=None))
    await settle(rig)
    assert len(rig.journal.calls("transport.stop_stream")) == 1
    await rig.manager.dispatch(Stop(receiver_id=target().receiver_id))
    await settle(rig)
    assert len(rig.journal.calls("transport.stop_stream")) == 1
    assert not rig.display.removed
    assert rig.manager.snapshot(target().receiver_id).cleanup_pending
    stop.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.Available
    assert not rig.manager.pending_resources(target().receiver_id)
    assert not rig.display.outputs
    assert not rig.transport.streams


@pytest.mark.parametrize("proof", ["records", "size", "helper"])
async def test_invalid_output_proof_cannot_start_transport(proof):
    # M03 drops record confirmation; M04 drops returned-handle proof checks.
    class UnprovenDisplay(FakeDisplay):
        async def create(self, receiver_id, endpoint_fingerprint, size, operation):
            result = await super().create(receiver_id, endpoint_fingerprint, size, operation)
            if proof == "records":
                self.ownership.records.clear()
            elif proof == "size":
                result = Ok(value=replace(result.value, source=replace(result.value.source, size=SMALL)))
                record = self.ownership.records[result.value.owner_uuid]
                self.ownership.records[record.owner_uuid] = replace(record, observed_size=SMALL)
            else:
                result = Ok(value=replace(result.value, helper=replace(result.value.helper, start_ticks=0)))
                record = self.ownership.records[result.value.owner_uuid]
                self.ownership.records[record.owner_uuid] = replace(record, helper=result.value.helper)
                self.queue_result("remove", Err(error=diagnostic(ErrorCode.ownership_mismatch)))
            return result

    rig = make_rig(display=UnprovenDisplay())
    states = []
    rig.manager.subscribe(states.append)
    state = await start_virtual(rig)
    assert "transport.start_stream" not in rig.journal.names()
    assert not rig.transport.streams
    assert state.stream is None
    assert all(snapshot.display is not DisplayState.Owned for snapshot in states)
    assert state.last_error.code is ErrorCode.ownership_mismatch
    if rig.display.outputs or rig.manager.pending_resources(target().receiver_id):
        assert state.cleanup_pending
        assert state.phase is SessionPhase.CleanupRequired
    else:
        assert state.display_handle is None
        assert state.ownership_uuid is None
        assert not rig.ownerships.records


class _Subscription:
    def close(self):
        pass


class TransportHarness:
    """Real DoubleTakeTransport; replace its process registry and typed control."""

    def __init__(self, *, connect_state="connecting", gate=None):
        self.record = DaemonRecord(
            schema_version=1, instance_uuid=InstanceId(UUID(int=90)), owner_token="test-owner",
            app=ProcessIdentity(pid=100, start_ticks=1),
            supervisor=ProcessIdentity(pid=200, start_ticks=2),
            daemon=ProcessIdentity(pid=300, start_ticks=3), protocol_version="v1",
            socket_path="/private/test/daemon.sock", lifecycle="running", runtime_directory="/private/test",
        )
        self.connect_state, self.gate = connect_state, gate
        self.result = None
        self.events = []
        self.requests = []

    def subscribe(self, callback):
        return _Subscription()

    async def ensure(self):
        return Ok(value=self.record)

    async def handshake(self, owner_token, instance_uuid):
        return Ok(value=Handshake(
            instance_uuid=instance_uuid, owner_token=owner_token,
            daemon=self.record.daemon, protocol_version="v1",
        ))

    async def request(self, operation, destination, payload, context, *, dispatch_fence=None):
        assert dispatch_fence is not None
        checked = dispatch_fence()
        if isinstance(checked, Err):
            return checked
        self.requests.append(operation)
        return Ok(value=AckReply(
            operation=operation.value, target=destination.canonical_host,
            state=self.connect_state if operation is BackendOperation.connect else None,
        ))

    def rig(self):
        rig = make_rig()
        harness = self

        class HeldTransport(DoubleTakeTransport):
            async def start_stream(self, *args, initial_volume=None):
                result = await super().start_stream(*args, initial_volume=initial_volume)
                harness.result = result
                if harness.gate is not None:
                    await harness.gate.wait()
                return result

        from airplay_linux.trust.keyring import BackendKeyringStore
        features = ApplicationContractProfile(socket_path=self.record.socket_path).features
        clock = FakeClock()
        transport = HeldTransport(
            lambda record: self, self, rig.capture, BackendKeyringStore(self, features),
            rig.receivers, rig.fences, features, clock, FakeScheduler(clock), SequenceIds(100),
        )
        transport.subscribe(self.events.append)
        for subscription in rig.manager._subscriptions:
            subscription.close()
        from airplay_linux.session.engine import SessionManager
        from virtual_scenarios import virtual_handlers
        rig.transport = transport
        rig.deps = replace(rig.deps, transport=transport)
        rig.manager = SessionManager(rig.deps, virtual_handlers(rig.deps))
        return rig


@pytest.mark.parametrize("observation", ["streaming", "remembering"])
async def test_virtual_waits_for_real_transport_readiness_before_streaming_and_remembering(observation):
    harness = TransportHarness()
    rig = harness.rig()
    state = await start_virtual(rig)
    assert state.display is DisplayState.Owned
    assert state.stream is not None
    assert not any(event.kind is TransportEventKind.ready for event in harness.events)
    assert BackendOperation.start_audio not in harness.requests
    if observation == "streaming":
        assert state.transport is TransportState.Connecting
        assert state.phase is SessionPhase.Connecting
    else:
        assert target().receiver_id not in rig.receivers.receivers
        assert "receiver_store.put" not in rig.journal.names()


@pytest.mark.parametrize("ready_order", ["before_result", "after_result"])
async def test_real_transport_ready_remembers_only_after_exact_handle_adoption(ready_order):
    gate = Deferred() if ready_order == "before_result" else None
    harness = TransportHarness(connect_state="streaming" if gate else "connecting", gate=gate)
    rig = harness.rig()
    before = await start_virtual(rig)
    assert before.transport is TransportState.Connecting
    assert target().receiver_id not in rig.receivers.receivers
    if gate:
        assert before.stream is None
        assert isinstance(harness.result, Ok)
        assert any(event.kind is TransportEventKind.ready for event in harness.events)
        gate.resolve(Ok(value=None))
    else:
        assert before.stream is not None
        assert not any(event.kind is TransportEventKind.ready for event in harness.events)
        rig.manager.ingest(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    after = rig.manager.snapshot(target().receiver_id)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.transport is TransportState.Streaming
    assert after.stream == harness.result.value
    assert after.display_handle == before.display_handle
    remembered = rig.receivers.receivers[target().receiver_id].remembered_resolution
    assert remembered.size == before.resolution.size
    assert remembered.evidence.origin == "verified"
    assert len(rig.journal.calls("receiver_store.put")) == 1
    rig.manager.ingest(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    assert len(rig.journal.calls("receiver_store.put")) == 1
    assert harness.requests.count(BackendOperation.start_audio) == 1


@pytest.mark.parametrize("field", ["lease_id", "backend_stream_id", "daemon_instance_uuid", "start_operation_id"])
async def test_early_ready_must_match_the_eventual_acquired_handle(field):
    from airplay_linux.domain.messages import EmptyPayload, TransportEvent

    class WrongReady(ContextTransport):
        def __init__(self):
            super().__init__(ready=False)

        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            result = await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            handle = result.value
            changed = "another-stream" if field == "backend_stream_id" else UUID(int=999999)
            wrong = replace(handle, **{field: changed})
            self.emit(TransportEvent(
                kind=TransportEventKind.ready, receiver_id=destination.receiver_id,
                stream=wrong, endpoint_fingerprint=destination.endpoint_fingerprint,
                endpoint_generation=destination.endpoint_generation,
                daemon_instance_uuid=wrong.daemon_instance_uuid, context=operation, payload=EmptyPayload(),
            ))
            return result

    rig = make_rig(transport=WrongReady())
    state = await start_virtual(rig)
    assert state.stream is not None
    assert state.transport is TransportState.Connecting
    assert target().receiver_id not in rig.receivers.receivers
    rig.transport.emit(stream_event(rig, TransportEventKind.ready))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.VirtualDisplay
    assert target().receiver_id in rig.receivers.receivers


async def test_ready_queued_between_transport_return_and_actor_adoption_is_not_lost():
    from airplay_linux.domain.messages import EmptyPayload, TransportEvent

    class QueuedReady(ContextTransport):
        def __init__(self):
            super().__init__(ready=False)

        async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
            result = await super().start_stream(destination, source, audio, operation, initial_volume=initial_volume)
            asyncio.get_running_loop().call_soon(self.emit, TransportEvent(
                kind=TransportEventKind.ready, receiver_id=destination.receiver_id,
                stream=result.value, endpoint_fingerprint=destination.endpoint_fingerprint,
                endpoint_generation=destination.endpoint_generation,
                daemon_instance_uuid=result.value.daemon_instance_uuid,
                context=operation, payload=EmptyPayload(),
            ))
            return result

    rig = make_rig(transport=QueuedReady())
    after = await start_virtual(rig)
    assert after.phase is SessionPhase.VirtualDisplay
    assert after.stream is not None
    assert target().receiver_id in rig.receivers.receivers
    # Only the manager's permanent subscriber remains after acquisition delivery.
    assert len(rig.transport._subscribers) == 1


async def test_ready_with_retired_trust_cannot_verify_accepted_stream():
    rig = make_rig(transport=ContextTransport(ready=False))
    before = await start_virtual(rig)
    ready = stream_event(rig, TransportEventKind.ready)
    rig.transport.emit(replace(ready, context=replace(
        ready.context, trust_generation=before.trust_generation - 1,
    )))
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id) == before
    assert target().receiver_id not in rig.receivers.receivers
    rig.transport.emit(ready)
    await settle(rig)
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.VirtualDisplay
