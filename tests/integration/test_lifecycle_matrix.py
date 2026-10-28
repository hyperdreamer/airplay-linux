"""Requirement-keyed acceptance through the public runtime composition root.

Only hardware/process ports are substituted. Commands, effects, persistence,
startup recovery and shutdown all use the manager returned by build_runtime.
Shared fixtures live here because the lane owns exactly four test modules.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio

from airplay_linux.diagnostics import diagnostic
from airplay_linux.diagnostics.activity import ActivityLog
from airplay_linux.domain.messages import (
    Cancel, ChangeResolution, ChooseResolution, ConfirmSwitch, DisplayEvent,
    EmptyPayload, KeepCleanup, ModePayload, Pair, RecreateAccepted, Retry,
    RetryCleanup, Start, Stop, TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import InstanceId, StreamHandle
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, Mode, Ok,
    ResolutionDecision, ResolutionEvidence, SessionPhase, Size, StartRequest,
    TransportState, TrustState,
)
from airplay_linux.runtime import RuntimeConfig, build_runtime
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred, FakeTransport
from airplay_linux.testing.builders import physical, target, virtual_handle

HD = Size(width=1920, height=1080)
SMALL = Size(width=1280, height=720)


def decision(destination=None, size=HD):
    destination = destination or target()
    return ResolutionDecision(size=size, source="user", evidence=ResolutionEvidence(
        origin="picker", reliable=True, receiver_id=destination.receiver_id,
        endpoint_fingerprint=destination.endpoint_fingerprint,
    ))


def request(mode=Mode.VIRTUAL, *, destination=None, audio=False, resolution=True):
    return StartRequest(mode=mode, source=physical() if mode is Mode.MIRROR else None,
                        resolution=decision(destination) if mode is Mode.VIRTUAL and resolution else None,
                        audio=AudioPolicy(enabled=audio))


class AcceptanceTransport(FakeTransport):
    """Echo the caller's lease and supply the backend's explicit ready event."""
    def __init__(self, *, ready=True):
        super().__init__()
        self.ready = ready

    def _new_handle(self, destination, operation):
        handle = super()._new_handle(destination, operation)
        self.streams.pop(handle.lease_id)
        handle = replace(handle, lease_id=operation.transport_lease_id)
        self.streams[handle.lease_id] = handle
        return handle

    async def start_stream(self, destination, source, audio, operation):
        result = await super().start_stream(destination, source, audio, operation)
        if isinstance(result, Ok) and self.ready:
            self.emit(TransportEvent(
                kind=TransportEventKind.ready, receiver_id=destination.receiver_id,
                stream=result.value, endpoint_fingerprint=destination.endpoint_fingerprint,
                endpoint_generation=destination.endpoint_generation,
                daemon_instance_uuid=result.value.daemon_instance_uuid,
                context=operation, payload=EmptyPayload(),
            ))
        return result

    async def stop_audio(self, handle, operation):
        self._record("stop_audio", handle=handle, context=operation)
        self.audio_started.pop(handle.lease_id, None)
        return Ok(value=None)


@dataclass
class Application:
    runtime: object
    rig: SessionRig

    @property
    def manager(self):
        return self.runtime.manager

    def __getattr__(self, name):
        return getattr(self.rig, name)

    async def settle(self):
        # Yield even when the actor has no effects yet: runtime/coordinator
        # tasks and the real display adapter have their own callback queues.
        # No policy clock advances here and deliberately held ports stay held.
        for _ in range(64):
            await asyncio.sleep(0)
            await self.manager.drain()

    async def send(self, command):
        receipt = await self.manager.dispatch(command)
        await self.settle()
        return receipt

    def state(self, destination=None):
        return self.manager.snapshot((destination or target()).receiver_id)

    async def online(self, destination=None):
        self.discovery.add_target(destination or target())
        await self.settle()

    async def start(self, mode=Mode.VIRTUAL, *, destination=None, audio=False, resolution=True):
        destination = destination or target()
        await self.online(destination)
        await self.send(Start(receiver_id=destination.receiver_id, request=request(
            mode, destination=destination, audio=audio, resolution=resolution,
        )))
        return self.state(destination)

    async def retry_tick(self, destination=None):
        state = self.state(destination)
        assert state.next_retry_at is not None
        self.scheduler.advance(state.next_retry_at - self.clock.now())
        await self.settle()


def event(app, kind, destination=None, payload=None):
    state = app.state(destination)
    handle = state.stream
    assert isinstance(handle, StreamHandle)
    operation = replace(app.manager.context(state.receiver_id).operation(),
                        operation_id=handle.start_operation_id,
                        transport_lease_id=handle.lease_id,
                        endpoint_generation=handle.endpoint_generation,
                        session_generation=state.session_generation,
                        audio_operation_id=state.audio_operation_id)
    return TransportEvent(kind=kind, receiver_id=state.receiver_id, stream=handle,
                          endpoint_fingerprint=handle.endpoint_fingerprint,
                          endpoint_generation=handle.endpoint_generation,
                          daemon_instance_uuid=handle.daemon_instance_uuid,
                          context=operation, payload=payload or EmptyPayload())


def display_event(state, kind):
    handle = state.display_handle
    assert handle is not None
    return DisplayEvent(kind=kind, receiver_id=state.receiver_id,
                        creation_operation_id=None, owner_uuid=handle.owner_uuid,
                        connector=handle.source.connector, output_id=handle.source.kscreen_output_id,
                        helper=handle.helper, handle=handle, diagnostic=None)


def resources(state):
    return (state.transport, state.display, state.audio, state.trust, state.intent_active)


def ordered(journal, *names):
    actual = journal.names()
    position = -1
    for name in names:
        assert name in actual[position + 1:], (names, actual)
        position = actual.index(name, position + 1)


def daemon_record(number):
    from airplay_linux.domain.operations import ProcessIdentity
    return DaemonRecord(schema_version=1, instance_uuid=InstanceId(UUID(int=number)),
                        owner_token="acceptance-owner", app=ProcessIdentity(pid=101, start_ticks=102),
                        supervisor=ProcessIdentity(pid=103, start_ticks=104),
                        daemon=ProcessIdentity(pid=105, start_ticks=106), protocol_version="1",
                        socket_path="/fake/private/socket", lifecycle="running",
                        runtime_directory="/fake/private")


@pytest_asyncio.fixture
async def app_factory(tmp_path):
    applications = []

    async def build(*, transport=None, configure=None, start=True, directory=None):
        rig = SessionRig()
        # SessionRig supplies ports, never a second active authority.
        for subscription in rig.manager._subscriptions:
            subscription.close()
        rig.transport = transport or AcceptanceTransport()
        rig.capture.add_physical(physical())
        rig.deps = replace(rig.deps, transport=rig.transport, activity=ActivityLog())
        if configure is not None:
            configure(rig)
        for adapter in (rig.transport, rig.pairing, rig.capture, rig.audio, rig.discovery,
                        rig.display, getattr(rig.display, "process", None), rig.sessions,
                        rig.receivers, rig.ownerships):
            if adapter is not None and hasattr(adapter, "journal"):
                adapter.journal = rig.journal
        root = directory or tmp_path / str(len(applications))
        built = await build_runtime(RuntimeConfig(state_root=root / "state",
                                                  runtime_root=root / "runtime", live=False),
                                    {"deps": rig.deps})
        assert isinstance(built, Ok), built
        app = Application(built.value, rig)
        applications.append(app)
        if start:
            assert isinstance(await app.runtime.start(), Ok)
            await app.settle()
        return app

    yield build
    for app in reversed(applications):
        # Tests resolve every intentional gate. A real-time guard catches a hung
        # test fixture; all lifecycle deadlines/retries use FakeScheduler.
        await asyncio.wait_for(app.runtime.shutdown(), 2)


# Frozen requirement inventory, independent of the dispatcher's grouping. Every
# key must be a collected test ID, including a matrix-only pytest invocation.
REQUIRED_CASES = (
    "idle_mirror", "idle_virtual", "offline_reappearance", "offline_start",
    "choose_resolution", "cancel_resolution", "submit_pair_none", "cancel_pair_none",
    "submit_pair_owned", "cancel_pair_owned", "publish_output", "forget_no_stream",
    "cancel_partial_create", "stream_ready", "pairing_during_connect", "network_during_connect",
    "startup_audio_decision", "active_network_loss", "runtime_audio_loss", "stop_or_confirm_switch",
    "reconnect_success", "reconnect_stop", "local_budget_exhausted", "daemon_exit",
    "helper_or_output_loss", "change_resolution", "cancel_reconfigure", "recreate_accepted",
    "stop_mismatch", "adopt_recovering", "recovery_offline", "shutdown_any",
    "stopped_all_released", "stop_uncertain", "retry_cleanup", "keep_cleanup",
    "retry_revocation", "keep_trust_blocked",
)

# Separate literal parameter list: deleting a case is caught by the inventory.
MATRIX_CASES = (
    "idle_mirror", "idle_virtual", "offline_reappearance", "offline_start",
    "choose_resolution", "cancel_resolution", "submit_pair_none", "cancel_pair_none",
    "submit_pair_owned", "cancel_pair_owned", "publish_output", "forget_no_stream",
    "cancel_partial_create", "stream_ready", "pairing_during_connect", "network_during_connect",
    "startup_audio_decision", "active_network_loss", "runtime_audio_loss", "stop_or_confirm_switch",
    "reconnect_success", "reconnect_stop", "local_budget_exhausted", "daemon_exit",
    "helper_or_output_loss", "change_resolution", "cancel_reconfigure", "recreate_accepted",
    "stop_mismatch", "adopt_recovering", "recovery_offline", "shutdown_any",
    "stopped_all_released", "stop_uncertain", "retry_cleanup", "keep_cleanup",
    "retry_revocation", "keep_trust_blocked",
)


def test_requirement_inventory_maps_to_collected_parameter_ids(request):
    collected = [item.callspec.params["case"] for item in request.session.items
                 if item.originalname == "test_lifecycle_transition"
                 and item.path == Path(__file__)]
    assert len(collected) == len(set(collected))
    assert set(collected) == set(REQUIRED_CASES)


@pytest.mark.parametrize("case", MATRIX_CASES, ids=MATRIX_CASES)
async def test_lifecycle_transition(case, app_factory):
    """Each branch states real initial resources, delivers a command/event and
    checks the resulting independent resource tuple and ordered port effects.
    """
    if case == "cancel_partial_create":
        from test_crash_recovery import partial_creation_transition
        await partial_creation_transition(app_factory)
        return
    if case == "publish_output":
        from test_crash_recovery import output_publication_transition
        await output_publication_transition(app_factory)
        return
    if case in {"adopt_recovering", "recovery_offline"}:
        from test_crash_recovery import recovery_transition
        await recovery_transition(case, app_factory)
        return
    if case in {"submit_pair_none", "cancel_pair_none", "submit_pair_owned", "cancel_pair_owned",
                "forget_no_stream", "pairing_during_connect", "startup_audio_decision",
                "runtime_audio_loss", "retry_revocation", "keep_trust_blocked"}:
        from test_trust_and_audio import trust_audio_transition
        await trust_audio_transition(case, app_factory)
        return
    app = await app_factory(transport=AcceptanceTransport(ready=case != "stream_ready"))
    receiver = target().receiver_id
    if case in {"idle_mirror", "idle_virtual", "stream_ready"}:
        await app.online()
        assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                          TrustState.Unknown, False)
        mode = Mode.MIRROR if case == "idle_mirror" else Mode.VIRTUAL
        state = await app.start(mode)
        if case == "stream_ready":
            assert resources(state) == (TransportState.Connecting, DisplayState.Owned, AudioState.Disabled,
                                         TrustState.Unknown, True)
            app.transport.emit(event(app, TransportEventKind.ready))
            await app.settle()
            state = app.state()
        assert resources(state) == (TransportState.Streaming,
                                    DisplayState.None_ if mode is Mode.MIRROR else DisplayState.Owned,
                                    AudioState.Disabled, TrustState.Unknown, True)
        if mode is Mode.VIRTUAL:
            assert [stage for _, stage in app.display.stage_log] == [
                "reserved", "helper_started", "output_observed", "published"]
            ordered(app.journal, "display.create", "process.spawn", "transport.start_stream")
        else:
            ordered(app.journal, "capture.select", "capture.verify", "transport.start_stream")
        return
    if case in {"offline_start", "offline_reappearance"}:
        assert app.state().phase is SessionPhase.Offline
        if case == "offline_start":
            await app.send(Start(receiver_id=receiver, request=request()))
            assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                              TrustState.Unknown, False)
            assert not app.journal.calls("display.create")
            assert not app.journal.calls("transport.start_stream")
            assert app.journal.calls("discovery.refresh")
        else:
            await app.online()
            assert app.state().phase is SessionPhase.Available
            assert not app.journal.calls("transport.start_stream")
        return
    if case in {"choose_resolution", "cancel_resolution"}:
        state = await app.start(resolution=False)
        assert state.phase is SessionPhase.ResolutionRequired
        assert state.display is DisplayState.Resolving and state.stream is None
        app.journal.clear()
        if case == "choose_resolution":
            await app.send(ChooseResolution(receiver_id=receiver, decision=decision()))
            assert resources(app.state()) == (TransportState.Streaming, DisplayState.Owned,
                                              AudioState.Disabled, TrustState.Unknown, True)
            ordered(app.journal, "display.create", "transport.start_stream")
        else:
            await app.send(Cancel(receiver_id=receiver))
            assert resources(app.state()) == (TransportState.Idle, DisplayState.None_,
                                              AudioState.Disabled, TrustState.Unknown, False)
            assert not app.display.created and not app.transport.streams
        return
    if case in {"network_during_connect", "active_network_loss", "reconnect_success", "reconnect_stop"}:
        if case == "network_during_connect":
            app.transport.ready = False
        before = await app.start()
        assert before.display is DisplayState.Owned
        app.journal.clear()
        app.transport.emit(event(app, TransportEventKind.network_lost))
        await app.settle()
        lost = app.state()
        assert resources(lost) == (TransportState.Reconnecting, DisplayState.Owned, AudioState.Disabled,
                                  TrustState.Unknown, True)
        assert lost.stream is None and lost.display_handle == before.display_handle
        assert lost.next_retry_at == app.clock.now() + 1
        assert not app.display.removed
        ordered(app.journal, "transport.stop_stream")
        if case == "reconnect_success":
            await app.retry_tick()
            after = app.state()
            assert after.transport is TransportState.Streaming
            assert after.display_handle == before.display_handle
            assert after.session_generation == before.session_generation
            assert after.transport_lease_id != before.transport_lease_id
            assert after.reconnect_attempt == 0 and after.next_retry_at is None
        elif case == "reconnect_stop":
            await app.send(Stop(receiver_id=receiver))
            app.scheduler.advance(120)
            await app.settle()
            assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                              TrustState.Unknown, False)
            assert len(app.journal.calls("transport.start_stream")) == 0
        return
    if case in {"local_budget_exhausted", "daemon_exit"}:
        before = await app.start()
        calls = []
        async def restart():
            calls.append("restart")
            if case == "local_budget_exhausted":
                return Err(error=diagnostic(ErrorCode.daemon_failed))
            app.transport.streams.clear()
            app.transport.daemon_instance_uuid = daemon_record(90).instance_uuid
            return Ok(value=daemon_record(90))
        # The restart port is a frozen dependency: configure its callable owner.
        object.__setattr__(app.runtime.deps, "restart_daemon", restart)
        notice = event(app, TransportEventKind.daemon_exit)
        app.transport.emit(notice)
        await app.settle()
        assert calls == ["restart"]
        after = app.state()
        assert after.display_handle == before.display_handle and after.intent_active
        if case == "local_budget_exhausted":
            assert after.transport is TransportState.Failed
            assert after.next_retry_at is None
        else:
            assert after.transport is TransportState.Streaming
            assert after.stream.daemon_instance_uuid == daemon_record(90).instance_uuid
            app.transport.emit(notice)
            await app.settle()
            assert calls == ["restart"]
        return
    if case == "helper_or_output_loss":
        before = await app.start()
        app.journal.clear()
        app.display.emit(display_event(before, "helper_exit"))
        await app.settle()
        state = app.state()
        assert not state.intent_active and state.stream is None
        assert state.display is DisplayState.None_
        ordered(app.journal, "display.prepare_teardown", "transport.stop_stream", "display.remove")
        return
    if case in {"change_resolution", "cancel_reconfigure", "recreate_accepted", "stop_mismatch"}:
        before = await app.start()
        if case in {"recreate_accepted", "stop_mismatch"}:
            app.transport.emit(event(app, TransportEventKind.accepted_mode,
                                     payload=ModePayload(size=SMALL, reliable=True)))
            await app.settle()
            assert app.state().accepted_resolution == SMALL
            assert app.state().display_handle == before.display_handle and app.state().stream is None
            command = RecreateAccepted(receiver_id=receiver) if case == "recreate_accepted" else Stop(receiver_id=receiver)
        else:
            gate = Deferred()
            if case == "cancel_reconfigure":
                app.transport.queue_result("stop_stream", gate)
            command = ChangeResolution(receiver_id=receiver, decision=decision(size=SMALL))
        app.journal.clear()
        await app.send(command)
        if case == "cancel_reconfigure":
            assert app.state().phase is SessionPhase.Reconfiguring
            assert not app.display.removed
            await app.send(Cancel(receiver_id=receiver))
            gate.resolve(Ok(value=None))
            await app.settle()
        after = app.state()
        if case in {"cancel_reconfigure", "stop_mismatch"}:
            assert resources(after) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                        TrustState.Unknown, False)
            assert not app.display.outputs and not app.transport.streams
        else:
            assert after.transport is TransportState.Streaming and after.display is DisplayState.Owned
            assert after.display_handle != before.display_handle
            assert after.display_handle.source.size == SMALL
            assert len(app.display.outputs) == 1 and len(app.transport.streams) == 1
            ordered(app.journal, "display.remove", "display.create", "transport.start_stream")
        return
    if case in {"stop_or_confirm_switch", "stopped_all_released", "stop_uncertain", "retry_cleanup", "keep_cleanup", "shutdown_any"}:
        before = await app.start(audio=True)
        assert before.audio is AudioState.Enabled
        app.journal.clear()
        if case == "shutdown_any":
            assert isinstance(await app.runtime.shutdown(), Ok)
        elif case == "stop_or_confirm_switch":
            await delayed_switch(app, before)
            return
        else:
            if case in {"stop_uncertain", "retry_cleanup", "keep_cleanup"}:
                app.transport.queue_result("stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
            await app.send(Stop(receiver_id=receiver))
            if case in {"stop_uncertain", "retry_cleanup", "keep_cleanup"}:
                state = app.state()
                assert state.phase is SessionPhase.CleanupRequired and state.cleanup_pending
                assert before.display_handle.owner_uuid in app.ownerships.records
                assert not app.display.removed
                assert app.ownerships.records[before.display_handle.owner_uuid].desired_state == "removing"
                if case == "retry_cleanup":
                    await app.send(RetryCleanup(receiver_id=receiver))
                elif case == "keep_cleanup":
                    app.journal.clear()
                    await app.send(KeepCleanup(receiver_id=receiver))
                    assert app.state().cleanup_pending
                    assert not app.journal.calls("display.remove")
                    return
                else:
                    return
        assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                          TrustState.Unknown, False)
        assert not app.manager.pending_resources(receiver)
        assert not app.display.outputs and not app.transport.streams and not app.ownerships.records
        ordered(app.journal, "display.prepare_teardown", "transport.stop_stream", "transport.stop_audio",
                "display.remove", "process.terminate", "ownership_store.delete")
        return
    raise AssertionError(f"unimplemented acceptance requirement: {case}")


async def delayed_switch(app, before):
    """Durable intent before stop, confirmed stream before helper, then new mode."""
    stop = Deferred()
    app.transport.queue_result("stop_stream", stop)
    try:
        await app.send(ConfirmSwitch(receiver_id=before.receiver_id,
                                     request=request(Mode.MIRROR), confirmed=True))
        record = app.ownerships.records[before.display_handle.owner_uuid]
        assert (record.desired_state, record.stage) == ("removing", "removing")
        ordered(app.journal, "display.prepare_teardown", "ownership_store.write", "transport.stop_stream")
        assert not app.journal.calls("display.remove")
        assert not app.journal.calls("capture.select")
        stop.resolve(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
        await app.settle()
        assert app.state().phase is SessionPhase.CleanupRequired
        assert not app.journal.calls("capture.select")
        await app.send(RetryCleanup(receiver_id=before.receiver_id))
        assert app.state().phase is SessionPhase.Mirroring
        assert app.state().session_generation == before.session_generation + 1
        assert app.state().display_handle is None
        ordered(app.journal, "transport.stop_audio", "display.remove", "process.terminate",
                "ownership_store.delete", "capture.select", "transport.start_stream")
    finally:
        if not stop.resolved:
            stop.resolve(Err(error=diagnostic(ErrorCode.cleanup_timeout)))
        await app.settle()


async def test_stop_empty_receiver_is_idempotent(app_factory):
    app = await app_factory()
    await app.send(Stop(receiver_id=target().receiver_id))
    await app.send(Stop(receiver_id=target().receiver_id))
    assert not app.manager.pending_resources(target().receiver_id)
    assert not app.journal.calls("display.create")
    assert not app.journal.calls("transport.start_stream")


async def test_runtime_command_persistence_and_shutdown_share_the_public_manager(app_factory):
    app = await app_factory()
    before = await app.start(audio=True)
    assert isinstance(await app.runtime.persistence.flush(), Ok)
    stored, = await app.runtime.deps.sessions.load_all()
    assert stored.intent_active and stored.session_generation == before.session_generation
    assert stored.display_handle == before.display_handle
    assert isinstance(await app.runtime.shutdown(), Ok)
    stopped, = await app.runtime.deps.sessions.load_all()
    assert not stopped.intent_active and stopped.display_handle is None
    assert not app.transport.streams and not app.display.outputs and not app.ownerships.records
    receipt = await app.manager.dispatch(Start(receiver_id=before.receiver_id, request=request()))
    assert not receipt.accepted
