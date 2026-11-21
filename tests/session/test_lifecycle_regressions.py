"""Resource-backed lifecycle regressions; every external boundary is deterministic."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    ConfirmSwitch, CredentialRequestPayload, DismissCredentialPrompt, EffectEvent,
    Pair, RetryCleanup, Start, Stop, SubmitCredential, SubmitPairing,
    TransportEvent, TransportEventKind,
)
from airplay_linux.domain.operations import (
    InstanceId, LeaseId, SensitiveText, StreamHandle,
)
from airplay_linux.backend.contract import BackendStream, StatusReply
from airplay_linux.domain.records import DaemonRecord, LaunchReceipt
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.relay import CredentialRequest, NoReply, RelayStartOptions, Submitted
from airplay_linux.session.recovery import CredentialRecoveryCoordinator
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage.repositories import JsonCredentialRecoveryMarkerStore

from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, IntentStage, Mode,
    Ok, SessionPhase, StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import Route
from virtual_scenarios import settle as settle_resources
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import context, physical, target, virtual_handle


async def online_rig(factory=build_lifecycle_handlers):
    rig = SessionRig(factory)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    rig.transport.daemon_instance_uuid = InstanceId(UUID(int=9))
    for adapter in (rig.capture, rig.transport, rig.display, rig.audio):
        adapter.journal = rig.journal
    await rig.settle()
    return rig, destination


def stream_for(destination, state, *, number=1):
    return StreamHandle(
        receiver_id=destination.receiver_id,
        endpoint_generation=destination.endpoint_generation,
        endpoint_fingerprint=destination.endpoint_fingerprint,
        backend_stream_id=f"regression-stream-{number}",
        daemon_instance_uuid=InstanceId(UUID(int=9)),
        lease_id=state.transport_lease_id,
        start_operation_id=state.operation_id,
    )


async def owned_session(rig, destination, *, streaming=True, delivered=True):
    operation = context(lease=1 if streaming else None)
    created = await rig.display.create(
        destination.receiver_id, destination.endpoint_fingerprint,
        virtual_handle().source.size, operation,
    )
    assert isinstance(created, Ok)
    handle = created.value
    state = replace(
        rig.manager.snapshot(destination.receiver_id),
        operation_id=operation.operation_id,
        session_generation=operation.session_generation,
        transport_lease_id=operation.transport_lease_id,
        mode=Mode.VIRTUAL, intent_active=True,
        intent_stage=IntentStage.active if streaming else IntentStage.starting,
        transport=TransportState.Streaming if streaming else TransportState.Connecting,
        audio=AudioState.Enabled if streaming else AudioState.Disabled,
        display=DisplayState.Owned if delivered else DisplayState.Creating,
        display_handle=handle if delivered else None,
        ownership_uuid=handle.owner_uuid if delivered else None,
    )
    if streaming:
        state = replace(state, stream=stream_for(destination, state))
        rig.transport.streams[state.stream.lease_id] = state.stream
    rig.manager.install(state)
    return handle


@pytest.mark.asyncio
@pytest.mark.parametrize("connector", ["window:123", "Virtual-1"])
async def test_provider_verification_blocks_an_unverified_capture(connector):
    # Bypassing capture.verify must admit video here, rather than fail earlier
    # on a substituted connector or the selection double's own admission.
    rig, destination = await online_rig()
    source = physical(connector)
    rig.capture.queue_result("select", Ok(value=source))
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=source),
    ))
    await rig.settle()
    assert "transport.start_stream" not in rig.journal.names()
    assert rig.manager.snapshot(destination.receiver_id).stream is None
    assert rig.capture.released == [source]
    assert rig.manager.snapshot(destination.receiver_id).last_error.code is ErrorCode.unsupported_source


@pytest.mark.asyncio
async def test_stale_trust_generation_cannot_promote_a_trusted_completion():
    def factory(deps):
        routes = dict(build_lifecycle_handlers(deps))
        original = routes[Route.trust]

        def retire_trust(ctx, message):
            if isinstance(message, EffectEvent) and message.kind == "trust:pairing_submit":
                ctx.apply(trust_generation=ctx.state.trust_generation + 1)
            return original(ctx, message)

        routes[Route.trust] = retire_trust
        return routes

    rig, destination = await online_rig(factory)
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    challenge = rig.manager.snapshot(destination.receiver_id).challenge
    assert challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=destination.receiver_id, challenge_id=challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.CleanupRequired
    assert state.challenge is None


@pytest.mark.asyncio
async def test_publication_failure_blocks_all_resource_teardown_until_retry():
    rig, destination = await online_rig()
    handle = await owned_session(rig, destination)

    async def stop_control(stream, operation):
        rig.journal.record("audio.stop_control")
        return Ok(value=None)

    rig.audio.stop_control = stop_control
    rig.display.queue_result("prepare_teardown", Err(error=diagnostic(ErrorCode.persistence_failed)))
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    for name in ("transport.stop_stream", "audio.stop_control", "capture.release", "display.remove"):
        assert name not in rig.journal.names()
    assert handle.owner_uuid in rig.display.handles
    assert rig.manager.snapshot(destination.receiver_id).cleanup_pending
    await rig.manager.dispatch(RetryCleanup(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_owned_output_and_audio_wait_for_exact_transport_and_audio_acknowledgements():
    rig, destination = await online_rig()
    handle = await owned_session(rig, destination)
    original = rig.manager.snapshot(destination.receiver_id).stream
    stop_ack, audio_ack = Deferred(), Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    audio_released = []

    async def stop_control(stream, operation):
        rig.journal.record("audio.stop_control")
        result = await audio_ack.wait()
        if isinstance(result, Ok):
            audio_released.append(stream)
        return result

    rig.audio.stop_control = stop_control
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "display.prepare_teardown" in rig.journal.names()
    assert "transport.stop_stream" in rig.journal.names()
    assert "audio.stop_control" not in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.ownership.records[handle.owner_uuid].desired_state == "removing"
    assert rig.display.process.alive(handle.helper)
    stop_ack.resolve(Ok(value=None))
    await rig.settle()
    assert original.lease_id not in rig.transport.streams
    assert "audio.stop_control" in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    audio_ack.resolve(Ok(value=None))
    await rig.settle()
    assert audio_released == [original]
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.display.process.alive(handle.helper)
    assert handle.source.kscreen_output_id not in rig.display.outputs
    assert handle.owner_uuid not in rig.display.ownership.records
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True], ids=["stop", "switch"])
async def test_failed_late_capture_completes_stop_and_wakes_queued_start(queued):
    rig, destination = await online_rig()
    selected = Deferred()
    rig.capture.queue_result("select", selected)
    request = StartRequest(mode=Mode.MIRROR, source=physical(), audio=AudioPolicy(enabled=False))
    await rig.manager.dispatch(Start(receiver_id=destination.receiver_id, request=request))
    await rig.settle()
    assert rig.manager.context(destination.receiver_id).outstanding_acquisitions() == 1
    command = (ConfirmSwitch(receiver_id=destination.receiver_id, request=request, confirmed=True)
               if queued else Stop(receiver_id=destination.receiver_id))
    await rig.manager.dispatch(command)
    await rig.settle()
    before = rig.manager.snapshot(destination.receiver_id)
    assert before.cleanup_pending, "an unresolved capture can still acquire a resource"
    assert rig.journal.names().count("capture.select") == 1
    # Hold a possible queued restart so it cannot finish/fail for an unrelated
    # lease mismatch in FakeTransport's default handle builder.
    next_stream = Deferred()
    rig.transport.queue_result("start_stream", next_stream)
    selected.resolve(Err(error=diagnostic(ErrorCode.portal_cancelled)))
    await rig.settle()
    after = rig.manager.snapshot(destination.receiver_id)
    if queued:
        assert rig.journal.names().count("capture.select") == 2
        assert after.intent_active
        assert after.session_generation == before.session_generation
        next_stream.resolve(Ok(value=stream_for(destination, after)))
        await rig.settle()
        assert rig.manager.snapshot(destination.receiver_id).phase is SessionPhase.Mirroring
        await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
        await rig.settle()
    else:
        assert not after.cleanup_pending
        assert after.phase is SessionPhase.Available
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_failed_late_release_retains_exact_source_until_retry():
    rig, destination = await online_rig()
    selected = Deferred()
    rig.capture.queue_result("select", selected)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical()),
    ))
    await rig.settle()
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    source = replace(physical(), capture_lease_id=LeaseId(UUID(int=72)))
    rig.capture.queue_result("release", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    selected.resolve(Ok(value=source))
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == (source,)
    assert rig.manager.snapshot(destination.receiver_id).cleanup_pending
    await rig.manager.dispatch(RetryCleanup(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending
    assert rig.capture.released == [source]


def deferred_acquisition(acquired, name):
    """Exercise cleanup for a producer route without depending on later tasks."""
    def factory(deps):
        routes = dict(build_lifecycle_handlers(deps))

        def virtual(ctx, message):
            if not isinstance(message, Start):
                return False
            operation = replace(ctx.operation(), operation_id=ctx.state.operation_id)
            if name == "transport_start":
                operation = ctx.new_transport_lease(operation)

            async def acquire():
                result = await acquired.wait()
                if isinstance(result, Ok) and isinstance(result.value, StreamHandle):
                    deps.transport.streams[result.value.lease_id] = result.value
                return result

            ctx.launch(Route.virtual, name, operation, acquire, acquisition=True)
            return True

        routes[Route.virtual] = virtual
        return routes
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds", [True, False], ids=["stream", "no-resource"])
async def test_owned_display_waits_for_retired_transport_acquisition_and_release(succeeds):
    acquired = Deferred()
    rig, destination = await online_rig(deferred_acquisition(acquired, "transport_start"))
    handle = await owned_session(rig, destination, streaming=False)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await rig.settle()
    late = stream_for(destination, rig.manager.snapshot(destination.receiver_id))
    stop_ack = Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    if succeeds:
        acquired.resolve(Ok(value=late))
        await rig.settle()
        assert "transport.stop_stream" in rig.journal.names()
        assert "display.remove" not in rig.journal.names()
        assert rig.display.process.alive(handle.helper)
        stop_ack.resolve(Ok(value=None))
    else:
        acquired.resolve(Err(error=diagnostic(ErrorCode.transport_failed)))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert not rig.display.process.alive(handle.helper)
    assert not rig.transport.streams
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


@pytest.mark.asyncio
async def test_late_display_waits_for_held_transport_stop_and_durable_publication():
    acquired = Deferred()
    rig, destination = await online_rig(deferred_acquisition(acquired, "display_create"))
    # The creator owns the exact helper but has not delivered its handle yet.
    handle = await owned_session(rig, destination, delivered=False)
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id, request=StartRequest(mode=Mode.VIRTUAL),
    ))
    await rig.settle()
    stop_ack = Deferred()
    rig.transport.queue_result("stop_stream", stop_ack)
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "transport.stop_stream" in rig.journal.names()
    assert "display.remove" not in rig.journal.names()
    acquired.resolve(Ok(value=handle))
    await rig.settle()
    assert "display.remove" not in rig.journal.names()
    assert rig.display.process.alive(handle.helper)
    stop_ack.resolve(Ok(value=None))
    await rig.settle()
    assert rig.display.removed == [handle.owner_uuid]
    assert rig.manager.pending_resources(destination.receiver_id) == ()
    assert not rig.manager.snapshot(destination.receiver_id).cleanup_pending


class CredentialRegistry:
    """Deterministic authenticated registry evidence for the real coordinator."""

    def __init__(self):
        self.record = DaemonRecord(
            schema_version=1, instance_uuid=InstanceId(UUID(int=9)), owner_token="owner",
            app=ProcessIdentity(pid=100, start_ticks=1),
            supervisor=ProcessIdentity(pid=101, start_ticks=2),
            daemon=ProcessIdentity(pid=102, start_ticks=3), protocol_version="v1",
            socket_path="/run/private/relay/daemon.sock", lifecycle="running",
            runtime_directory="/run/private/relay",
        )
        self.receipt = LaunchReceipt(
            schema_version=1, launch_uuid=UUID(int=81), instance_uuid=InstanceId(UUID(int=82)),
            app=self.record.app, owner_uid=1000, boot_id="test-boot",
            runtime_directory=self.record.runtime_directory, runtime_device=1, runtime_inode=2,
            endpoint_generation=1, socket_name="daemon.sock", protocol_version="v1",
            state="ready", daemon_instance_uuid=self.record.instance_uuid,
            supervisor=self.record.supervisor, daemon=self.record.daemon,
            entry_device=1, entry_inode=3, socket_inode=3, peer_pid=102,
            peer_start_ticks=3, peer_uid=1000, status_state="idle",
            credential_service="keyring_proven", failure=None, updated_at=0.0,
        )
        self._record = self.record
        self._ready = True
        self.gate = None
        self.authentication = Ok(value=self.record)

    async def ensure(self):
        return Ok(value=self.record)

    async def _receipt_for(self, record):
        assert record == self.record
        return Ok(value=self.receipt)

    async def authenticate_exact_identity(self, identity):
        if self.gate is not None:
            await self.gate.wait()
        return self.authentication


async def credential_rig(tmp_path, *, installed=False):
    """Keep the actor/store real and script only transport and registry I/O."""
    rig = SessionRig()
    registry = CredentialRegistry()
    store = JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    coordinator = CredentialRecoveryCoordinator(store, registry, fences=rig.fences)
    assert isinstance(await coordinator.load_before_startup(), Ok)
    coordinator._startup_completed()
    deps = replace(rig.deps, credential_recovery=coordinator)
    rig.deps = deps
    from airplay_linux.session.engine import SessionManager
    rig.manager = SessionManager(deps, build_lifecycle_handlers(deps))
    destination = target()
    rig.discovery.add_target(destination)
    pending = Deferred()
    rig.transport.queue_result("start_stream", pending)
    rig.transport.queue_result("record_credential_marker_generation", Ok(value=None))
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, audio=AudioPolicy(enabled=False)),
    ))
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    handle = stream_for(destination, state)
    request = CredentialRequest(
        receiver_id=destination.receiver_id, target=state.target, credential_kind="pin",
        start_operation_id=state.operation_id, endpoint_generation=1,
        session_generation=state.session_generation, owner_lease=state.transport_lease_id,
        daemon_instance_uuid=handle.daemon_instance_uuid, generation=1, observed_at=0.0,
        start_options=RelayStartOptions(
            target_volume_proven=True, audio=state.audio_policy, initial_volume=None,
        ),
    )
    operation = replace(
        context(), operation_id=state.operation_id, session_generation=state.session_generation,
        trust_generation=state.trust_generation, transport_lease_id=state.transport_lease_id,
    )
    event = TransportEvent(
        kind=TransportEventKind.credential_request, receiver_id=destination.receiver_id,
        stream=handle, endpoint_fingerprint=destination.endpoint_fingerprint,
        endpoint_generation=1, daemon_instance_uuid=handle.daemon_instance_uuid,
        context=operation, payload=CredentialRequestPayload(request=request, lifecycle="live", reason=None),
    )
    if installed:
        pending.resolve(Ok(value=handle))
        await rig.settle()
    rig.manager.ingest(event)
    await rig.settle()
    return SimpleNamespace(
        rig=rig, destination=destination, pending=pending, handle=handle,
        event=event, coordinator=coordinator, store=store, registry=registry,
    )


def credential_status(h, state="streaming", *, duplicate=False):
    rows = () if state is None else (BackendStream(
        device="Receiver", target=h.destination.canonical_host, state=state,
        has_audio=False, audio_muted=False,
    ),)
    if duplicate:
        rows = rows + rows
    return Ok(value=StatusReply(
        state="pin_required", device=None, target=None, has_audio=False, audio_muted=False,
        needs_credential=True, credential_kind="pin", streams=rows,
    ))


async def submit_credential(h, result):
    prompt = h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt
    assert prompt is not None, "the exact pending Start must publish its credential prompt"
    h.rig.transport.queue_result("submit_credential", result)
    secret = "relay-session-" + uuid4().hex
    receipt = await h.rig.manager.dispatch(SubmitCredential(
        receiver_id=h.destination.receiver_id, challenge_id=prompt.challenge.challenge_id,
        value=SensitiveText(secret),
    ))
    assert receipt.accepted
    await settle_resources(h.rig)
    return secret


def assert_credential_private(h, secret):
    retained = repr((
        h.rig.manager.snapshot(h.destination.receiver_id), h.coordinator.markers,
        h.rig.transport.journal.entries, h.rig.manager._effects,
        h.rig.manager._completed_effects, h.rig.activity.events,
    ))
    if secret in retained or any(secret.encode() in path.read_bytes() for path in h.store._files.root.rglob("*.json")):
        pytest.fail("credential appeared in retained state")


async def handoff_pending(h, owner, stop_context):
    h.rig.transport.journal.record("transport.stop_pending_start", owner=owner, context=stop_context)
    assert owner.start_operation_id == h.handle.start_operation_id
    assert owner.owner_lease == h.handle.lease_id
    if not h.pending.resolved:
        h.pending.resolve(Err(error=diagnostic(ErrorCode.stale_event)))
    h.rig.transport.streams[h.handle.lease_id] = h.handle
    return Ok(value=h.handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [False, True], ids=["pending", "installed"])
@pytest.mark.parametrize("accepted", [False, True], ids=["negative-reply", "positive-reply"])
async def test_credential_reply_waits_for_target_streaming_and_retirement(tmp_path, installed, accepted):
    h = await credential_rig(tmp_path, installed=installed)
    status = Deferred()
    h.rig.transport.queue_result("read_credential_status", status)
    secret = await submit_credential(h, Ok(value=Submitted(reply_at=0.0, accepted=accepted, post_dispatch_error=None)))
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    assert state.credential_prompt.state == "observing"
    assert state.credential_prompt.report is None
    assert h.coordinator.markers[0].state == "watching"
    assert len(h.rig.transport.journal.calls("transport.read_credential_status")) == 1
    status.resolve(credential_status(h))
    await settle_resources(h.rig)
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    assert state.credential_prompt.report == "connected"
    assert h.coordinator.markers[0].state == "retired"
    assert h.rig.pairing.journal.entries == []
    assert state.trust is TrustState.Unknown
    assert_credential_private(h, secret)
    if not installed:
        h.pending.resolve(Ok(value=h.handle))
        await settle_resources(h.rig)


@pytest.mark.asyncio
async def test_credential_stop_after_dismissal_settles_no_reply_before_disconnect(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, stop_context: handoff_pending(h, owner, stop_context)
    h.rig.transport.queue_result("record_credential_marker_generation", Ok(value=None))
    reply = Deferred()
    secret = await submit_credential(h, reply)
    prompt = h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt
    await h.rig.manager.dispatch(DismissCredentialPrompt(
        receiver_id=h.destination.receiver_id, challenge_id=prompt.challenge.challenge_id,
    ))
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert not h.rig.transport.journal.calls("transport.stop_stream")
    reply.resolve(Ok(value=NoReply(reason="command_timeout")))
    await settle_resources(h.rig)
    marker = h.coordinator.markers[0]
    assert marker.state == "release_pending"
    assert marker.marker_generation == 2
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt is None
    assert len(h.rig.transport.journal.calls("transport.stop_stream")) == 1
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=99)), Err)
    assert_credential_private(h, secret)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no_spawn", "ownership_after_reply", "status_after_stop"])
async def test_credential_failure_settles_stop_without_releasing_uncertain_marker(tmp_path, failure):
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    reply = Deferred()
    await submit_credential(h, reply)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert not h.rig.transport.journal.calls("transport.stop_stream")
    h.rig.transport.queue_result("read_credential_status", Err(error=diagnostic(ErrorCode.transport_failed)))
    if failure == "no_spawn":
        result = Err(error=diagnostic(ErrorCode.invalid_input))
    else:
        result = Ok(value=Submitted(
            reply_at=0.0, accepted=True,
            post_dispatch_error=ErrorCode.ownership_mismatch if failure == "ownership_after_reply" else None,
        ))
    reply.resolve(result)
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt is None
    marker = h.coordinator.markers[0]
    assert marker.state == ("retired" if failure == "no_spawn" else "watching")
    assert len(h.rig.transport.journal.calls("transport.stop_stream")) == 1
    assert len(h.rig.transport.journal.calls("transport.read_credential_status")) == (failure == "status_after_stop")


@pytest.mark.asyncio
async def test_credential_stop_during_marker_create_refuses_dispatch_and_retires_after_cleanup(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    created = Deferred()
    original_create = h.store.create

    async def create(marker):
        result = await original_create(marker)
        await created.wait()
        return result

    h.store.create = create
    await submit_credential(h, Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=999)), Err)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert not h.rig.transport.journal.calls("transport.stop_stream")
    created.resolve(Ok(value=None))
    await settle_resources(h.rig)
    assert not h.rig.transport.journal.calls("transport.submit_credential")
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.coordinator.markers[0].retirement.kind == "no_spawn"


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["mark_release_pending", "retire_after_reply"])
async def test_credential_failed_persistence_preserves_cache_and_target_fence(tmp_path, transition):
    h = await credential_rig(tmp_path, installed=True)
    failed = Deferred()
    original = getattr(h.store, transition)
    attempts = []

    async def fail(*args):
        attempts.append(args)
        return await failed.wait()

    setattr(h.store, transition, fail)
    h.rig.transport.queue_result("read_credential_status", credential_status(h))
    result = (Ok(value=NoReply(reason="incomplete_reply")) if transition == "mark_release_pending"
              else Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    await submit_credential(h, result)
    assert len(attempts) == 1
    assert h.coordinator.markers[0].marker_generation == 1
    assert h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt.report is None
    failed.resolve(Err(error=diagnostic(ErrorCode.persistence_failed)))
    await settle_resources(h.rig)
    assert h.coordinator.markers[0].state == "watching"
    assert h.coordinator.markers[0].marker_generation == 1
    assert h.rig.manager.snapshot(h.destination.receiver_id).last_error.code is ErrorCode.persistence_failed
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=999)), Err)
    setattr(h.store, transition, original)


@pytest.mark.asyncio
async def test_credential_factory_reserves_before_identity_io_and_refuses_unauthenticated_evidence(tmp_path):
    h = await credential_rig(tmp_path)
    h.registry.gate = Deferred()
    task = asyncio.create_task(h.coordinator.begin_submission_for(
        h.destination.receiver_id, h.destination, h.event.context.operation_id,
    ))
    await asyncio.sleep(0)
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=999)), Err)
    h.registry.authentication = Err(error=diagnostic(ErrorCode.ownership_mismatch))
    h.registry.gate.resolve(Ok(value=None))
    assert isinstance(await task, Err)
    assert h.coordinator.markers == ()
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=999)), Ok)
    h.pending.resolve(Ok(value=h.handle))
    await settle_resources(h.rig)


@pytest.mark.asyncio
async def test_credential_coordinator_dismissal_and_tombstone_generation_are_durable(tmp_path):
    h = await credential_rig(tmp_path, installed=True)
    result = await h.coordinator.begin_submission_for(
        h.destination.receiver_id, h.destination, h.event.context.operation_id,
    )
    assert isinstance(result, Ok)
    marker, identity = result.value
    assert identity.launch_uuid == h.registry.receipt.launch_uuid
    assert identity.daemon_process == h.registry.record.daemon
    dismissed = await h.coordinator.dismiss_for(marker, marker.marker_generation)
    assert isinstance(dismissed, Ok)
    assert dismissed.value.state == "release_pending"
    assert not dismissed.value.automatic_status_checks
    assert h.coordinator.markers == (dismissed.value,)
    assert isinstance(h.coordinator.credential_dispatch_fence(marker, 1)(), Err)
    assert isinstance(await h.coordinator.dismiss_for(marker, 1), Err)


@pytest.mark.asyncio
async def test_credential_stop_after_completed_reply_uses_current_marker_generation(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.transport.queue_result("read_credential_status", credential_status(h))
    h.rig.transport.queue_result("record_credential_marker_generation", Ok(value=None))
    await submit_credential(h, Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    assert h.coordinator.markers[0].state == "retired"
    updates = h.rig.transport.journal.calls("transport.record_credential_marker_generation")
    assert len(updates) == 1
    assert updates[0]["previous_generation"] == 1
    assert updates[0]["expected_generation"] == 2
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    handoff = h.rig.transport.journal.calls("transport.stop_pending_start")[-1]
    assert handoff["owner"]["marker_generation"] == 2


class CredentialControl:
    """Two target rows, complete replies, and a held credential child."""

    def __init__(self, registry, clock):
        self.registry, self.clock = registry, clock
        self.state = "pin_required"
        self.reply = Deferred()
        self.submission_context = None
        self.disconnects = []
        self.other = target("other", address="192.0.2.99")
        self.no_spawn = False

    async def handshake(self, owner_token, instance_uuid):
        from airplay_linux.backend.control import Handshake
        return Ok(value=Handshake(
            instance_uuid=instance_uuid, owner_token=owner_token,
            daemon=self.registry.record.daemon, protocol_version="v1",
        ))

    async def request(self, operation, destination, payload, operation_context, *, dispatch_fence=None):
        from airplay_linux.backend.contract import AckReply
        from airplay_linux.backend.control import BackendOperation
        if dispatch_fence is not None:
            checked = dispatch_fence()
            if isinstance(checked, Err):
                return checked
        if operation is BackendOperation.connect:
            return Ok(value=AckReply(operation="connect", target=destination.canonical_host, state="pin_required"))
        if operation is BackendOperation.disconnect:
            self.disconnects.append(destination.canonical_host)
            self.state = None
            return Ok(value=AckReply(operation="disconnect", target=destination.canonical_host, state="pin_required"))
        assert operation is BackendOperation.status
        rows = (BackendStream(
            device="Other receiver", target=self.other.canonical_host, state="pin_required",
            credential_kind="pin", has_audio=False, audio_muted=False,
        ),)
        if self.state is not None:
            rows += (BackendStream(
                device="Receiver", target=target().canonical_host, state=self.state,
                credential_kind="pin" if self.state == "pin_required" else None,
                has_audio=False, audio_muted=False,
            ),)
        return Ok(value=StatusReply(
            state="pin_required", device=None, target=None, has_audio=False, audio_muted=False,
            needs_credential=True, credential_kind="pin", streams=rows,
        ))

    async def submit_credential(self, destination, payload, operation, *, dispatch_fence):
        from airplay_linux.backend.contract import CredentialDispatchFailure, CredentialSubmissionReply
        checked = dispatch_fence()
        assert isinstance(checked, Ok)
        self.submission_context = operation
        await self.reply.wait()
        if self.no_spawn:
            return CredentialDispatchFailure(
                dispatch_state="no_spawn", refusal="stale", error=diagnostic(ErrorCode.stale_event),
            )
        self.state = "connecting"
        return CredentialSubmissionReply(reply_at=self.clock.now(), accepted=True)

    async def close(self):
        pass


async def real_credential_rig(tmp_path):
    from airplay_linux.backend.control import ApplicationContractProfile
    from airplay_linux.session.engine import SessionManager
    from airplay_linux.transport.doubletake import DoubleTakeTransport
    rig = SessionRig()
    registry = CredentialRegistry()
    registry.subscribe = lambda callback: SimpleNamespace(close=lambda: None)
    coordinator = CredentialRecoveryCoordinator(
        JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path)), registry, fences=rig.fences,
    )
    assert isinstance(await coordinator.load_before_startup(), Ok)
    coordinator._startup_completed()
    control = CredentialControl(registry, rig.clock)
    features = replace(ApplicationContractProfile(socket_path=registry.record.socket_path).features,
                       credential_relay=True, target_volume=True)
    transport = DoubleTakeTransport(
        lambda record: control, registry, rig.capture, rig.pairing, rig.receivers,
        rig.fences, features, rig.clock, rig.scheduler, rig.ids,
        recovery_guard=coordinator.start_dispatch_fence,
    )
    deps = replace(rig.deps, transport=transport, credential_recovery=coordinator)
    rig.manager = SessionManager(deps, build_lifecycle_handlers(deps))
    rig.deps, rig.transport = deps, transport
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical(), audio=AudioPolicy(enabled=False)),
    ))
    await settle_resources(rig)
    return SimpleNamespace(rig=rig, destination=destination, control=control, coordinator=coordinator)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pre-submit", "before-reply", "after-reply", "no-spawn"])
async def test_real_transport_actor_stop_transfers_one_handle_and_disconnects_exact_target(tmp_path, stage):
    from airplay_linux.domain.messages import SubmitCredential
    h = await real_credential_rig(tmp_path)
    states = []
    h.rig.manager.subscribe(states.append)
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    assert state.credential_prompt is not None
    assert state.stream is None
    if stage != "pre-submit":
        await h.rig.manager.dispatch(SubmitCredential(
            receiver_id=h.destination.receiver_id,
            challenge_id=state.credential_prompt.challenge.challenge_id,
            value=SensitiveText("real-actor-canary-" + uuid4().hex),
        ))
        await settle_resources(h.rig)
        assert h.control.submission_context is not None
        if stage in ("after-reply", "no-spawn"):
            h.control.no_spawn = stage == "no-spawn"
            h.control.reply.resolve(Ok(value=None))
            await settle_resources(h.rig)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    if stage == "before-reply":
        assert not h.control.disconnects
        h.control.reply.resolve(Ok(value=None))
        await settle_resources(h.rig)
    if stage in ("before-reply", "after-reply"):
        assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
        h.rig.scheduler.advance(20.0)
        await settle_resources(h.rig)
    stopped = h.rig.manager.snapshot(h.destination.receiver_id)
    assert not stopped.cleanup_pending
    assert h.control.disconnects == [h.destination.canonical_host]
    assert all(state.stream is None for state in states)
    assert not h.rig.transport._streams
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert h.control.disconnects == [h.destination.canonical_host]


@pytest.mark.asyncio
async def test_credential_marker_and_settlement_exist_at_dispatch_and_callbacks_are_single_use(tmp_path):
    from airplay_linux.domain.messages import CredentialSubmissionCompletion
    h = await credential_rig(tmp_path, installed=True)
    invoked = []
    original = h.rig.transport.submit_credential

    async def submit(destination, value, operation, *, dispatch_fence):
        marker = await h.store.for_target(destination.canonical_host)
        assert isinstance(marker, Ok) and marker.value is not None
        assert marker.value.state == "watching"
        settlement = h.rig.manager.context(destination.receiver_id).credential_settlements[operation.operation_id]
        assert settlement.phase == "awaiting_reply"
        assert settlement.owner.marker_generation == marker.value.marker_generation
        invoked.append(settlement.owner)
        return await original(destination, value, operation, dispatch_fence=dispatch_fence)

    h.rig.transport.submit_credential = submit
    h.rig.transport.queue_result("read_credential_status", credential_status(h))
    await submit_credential(h, Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    assert len(invoked) == 1
    before = h.rig.manager.snapshot(h.destination.receiver_id)
    settlement = next(iter(h.rig.manager.context(h.destination.receiver_id).credential_settlements.values()))
    for _ in range(2):
        h.rig.manager.ingest(EffectEvent(
            receiver_id=h.destination.receiver_id, operation_id=settlement.submission_id,
            session_generation=settlement.owner.session_generation,
            kind="mirror:credential_submit", resource=None,
            result=Ok(value=CredentialSubmissionCompletion(
                submission_id=settlement.submission_id, **vars(invoked[0]),
                outcome=NoReply(reason="command_timeout"),
            )),
        ))
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id) == before
    assert h.coordinator.markers[0].marker_generation == 2
    assert len(h.rig.transport.journal.calls("transport.read_credential_status")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("sample", [None, "idle", "streaming"])
async def test_no_reply_late_observation_and_second_stop_keep_release_pending(tmp_path, sample):
    from airplay_linux.domain.messages import CredentialStatusCompletion
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    reply = Deferred()
    await submit_credential(h, reply)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    reply.resolve(Ok(value=NoReply(reason="command_timeout")))
    await settle_resources(h.rig)
    marker = h.coordinator.markers[0]
    assert marker.state == "release_pending"
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    settlement = next(iter(h.rig.manager.context(h.destination.receiver_id).credential_settlements.values()))
    h.rig.manager.ingest(EffectEvent(
        receiver_id=h.destination.receiver_id, operation_id=UUID(int=998),
        session_generation=settlement.owner.session_generation, kind="mirror:credential_status", resource=None,
        result=Ok(value=CredentialStatusCompletion(
            read_id=UUID(int=998), **vars(settlement.owner), result=credential_status(h, sample),
        )),
    ))
    h.rig.scheduler.advance(40.0)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert h.coordinator.markers == (marker,)
    assert h.rig.manager.snapshot(h.destination.receiver_id) == state
    assert isinstance(h.coordinator.require_start_allowed(h.destination, UUID(int=997)), Err)


@pytest.mark.asyncio
async def test_credential_failed_target_cleanup_defers_reply_retirement_until_retry(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    h.rig.transport.queue_result("stop_stream", Err(error=diagnostic(ErrorCode.cleanup_timeout)))
    h.rig.transport.queue_result("read_credential_status", credential_status(h))
    reply = Deferred()
    await submit_credential(h, reply)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    reply.resolve(Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None)))
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.coordinator.markers[0].state == "watching"
    await h.rig.manager.dispatch(RetryCleanup(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.coordinator.markers[0].state == "retired"
    assert len(h.rig.transport.journal.calls("transport.stop_stream")) == 2


@pytest.mark.asyncio
async def test_credential_reply_window_uses_actual_reply_time_across_stop(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.transport.stop_pending_start = lambda owner, operation: handoff_pending(h, owner, operation)
    reply = Deferred()
    await submit_credential(h, reply)
    h.rig.scheduler.advance(4.0)
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(2.0)
    h.rig.transport.queue_result("read_credential_status", credential_status(h, "connecting"))
    h.rig.transport.queue_result("read_credential_status", Deferred())
    reply.resolve(Ok(value=Submitted(reply_at=6.0, accepted=False, post_dispatch_error=None)))
    await settle_resources(h.rig)
    h.rig.scheduler.advance(19.0)
    await settle_resources(h.rig)
    assert not h.rig.transport.journal.calls("transport.stop_stream")
    assert h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    h.rig.scheduler.advance(1.0)
    await settle_resources(h.rig)
    assert len(h.rig.transport.journal.calls("transport.stop_stream")) == 1
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.coordinator.markers[0].retirement.reply.reply_at == 6.0


@pytest.mark.asyncio
async def test_marker_factory_never_launches_or_reconciles_a_daemon_to_find_identity(tmp_path):
    h = await credential_rig(tmp_path, installed=True)
    h.registry._record = h.registry.record
    async def forbidden():
        pytest.fail("marker identity resolution must not invoke ensure")
    h.registry.ensure = forbidden
    result = await h.coordinator.begin_submission_for(
        h.destination.receiver_id, h.destination, h.event.context.operation_id,
    )
    assert isinstance(result, Ok)
    assert result.value[1].launch_uuid == h.registry.receipt.launch_uuid


@pytest.mark.asyncio
async def test_transport_start_rechecks_recovery_guard_after_awaited_connect_ownership(tmp_path):
    h = await real_credential_rig(tmp_path)
    # A second operator Start is held inside control ownership work. Its
    # synchronous dispatch predicate must see a newly fenced target.
    destination = target("second", address="192.0.2.55")
    h.rig.discovery.add_target(destination)
    entered, release = asyncio.Event(), Deferred()
    original = h.control.request
    blocked = False
    async def request(operation, target_arg, payload, operation_context, *, dispatch_fence=None):
        from airplay_linux.backend.control import BackendOperation
        if operation is BackendOperation.connect and target_arg.receiver_id == destination.receiver_id:
            entered.set()
            await release.wait()
        return await original(operation, target_arg, payload, operation_context, dispatch_fence=dispatch_fence)
    h.control.request = request
    checked = []
    def guard(destination_arg, start_id):
        checked.append((destination_arg, start_id))
        return Err(error=diagnostic(ErrorCode.ownership_mismatch)) if blocked else Ok(value=None)
    h.rig.transport._recovery_guard = guard
    await h.rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical(), audio=AudioPolicy(enabled=False)),
    ))
    await settle_resources(h.rig)
    assert entered.is_set()
    blocked = True
    release.resolve(Ok(value=None))
    await settle_resources(h.rig)
    assert checked
    assert checked[-1][0].canonical_host == destination.canonical_host
    assert h.rig.manager.snapshot(destination.receiver_id).stream is None
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)


@pytest.mark.asyncio
async def test_pending_request_end_rejects_replayed_live_row(tmp_path):
    h = await credential_rig(tmp_path)
    h.rig.manager.ingest(replace(h.event, payload=replace(h.event.payload, lifecycle="ended", reason="row_ended")))
    assert h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt is None
    h.rig.manager.ingest(h.event)
    await settle_resources(h.rig)
    assert h.rig.manager.snapshot(h.destination.receiver_id).credential_prompt is None
    h.pending.resolve(Ok(value=h.handle))
    await settle_resources(h.rig)


@pytest.mark.asyncio
async def test_stop_between_durable_retirement_and_transport_generation_update_keeps_handoff_admissible(tmp_path):
    h = await real_credential_rig(tmp_path)
    state = h.rig.manager.snapshot(h.destination.receiver_id)
    generation_entered, generation_release = asyncio.Event(), Deferred()
    original = h.rig.transport.record_credential_marker_generation
    async def record(destination, operation, **kwargs):
        generation_entered.set()
        await generation_release.wait()
        return await original(destination, operation, **kwargs)
    h.rig.transport.record_credential_marker_generation = record
    await h.rig.manager.dispatch(SubmitCredential(
        receiver_id=h.destination.receiver_id, challenge_id=state.credential_prompt.challenge.challenge_id,
        value=SensitiveText("generation-handoff-" + uuid4().hex),
    ))
    await settle_resources(h.rig)
    h.control.reply.resolve(Ok(value=None))
    h.control.state = "streaming"
    # The control reply puts the target into connecting. Complete its report by
    # giving the observer a streaming row after that command continuation runs.
    await settle_resources(h.rig)
    h.control.state = "streaming"
    h.rig.scheduler.advance(0.5)
    await settle_resources(h.rig)
    assert generation_entered.is_set()
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
    generation_release.resolve(Ok(value=None))
    await settle_resources(h.rig)
    assert not h.rig.manager.snapshot(h.destination.receiver_id).cleanup_pending
    assert h.control.disconnects == [h.destination.canonical_host]


@pytest.mark.asyncio
async def test_failed_marker_factory_releases_provisional_reservation_and_keeps_store_barrier(tmp_path):
    h = await credential_rig(tmp_path, installed=True)
    async def fail(marker):
        return Err(error=diagnostic(ErrorCode.persistence_failed))
    h.store.create = fail
    result = await h.coordinator.begin_submission_for(
        h.destination.receiver_id, h.destination, h.event.context.operation_id,
    )
    assert isinstance(result, Err)
    assert h.coordinator.markers == ()
    assert not h.coordinator._factory_reservations
    assert not h.coordinator._reservations
    assert isinstance(h.coordinator.require_start_allowed(h.destination, h.event.context.operation_id), Err)


@pytest.mark.asyncio
async def test_start_dispatch_guard_captures_trust_fence_before_awaited_ownership(tmp_path):
    h = await real_credential_rig(tmp_path)
    destination = target("second", address="192.0.2.55")
    h.rig.discovery.add_target(destination)
    release = Deferred()
    original = h.control.request
    async def request(operation, target_arg, payload, operation_context, *, dispatch_fence=None):
        from airplay_linux.backend.control import BackendOperation
        if operation is BackendOperation.connect and target_arg.receiver_id == destination.receiver_id:
            await release.wait()
        return await original(operation, target_arg, payload, operation_context, dispatch_fence=dispatch_fence)
    h.control.request = request
    await h.rig.manager.dispatch(Start(
        receiver_id=destination.receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical(), audio=AudioPolicy(enabled=False)),
    ))
    await settle_resources(h.rig)
    before = h.rig.fences.get(destination.receiver_id)
    h.rig.fences.set(replace(before, trust_generation=before.trust_generation + 1))
    release.resolve(Ok(value=None))
    await settle_resources(h.rig)
    state = h.rig.manager.snapshot(destination.receiver_id)
    assert not state.intent_active
    assert state.stream is None
    await h.rig.manager.dispatch(Stop(receiver_id=h.destination.receiver_id))
    await settle_resources(h.rig)
