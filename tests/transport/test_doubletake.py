"""Exact target/daemon admission and per-stream transport, without live I/O."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.backend.contract import AckReply, BackendSource, BackendStream, StatusReply, decode_status
from airplay_linux.capture.source import SourceRefusal, verify_reported_source
from airplay_linux.display.inventory import KScreenInventory, KScreenReportedSourceVerifier
from airplay_linux.backend.control import (
    ApplicationContractProfile, BackendNotice, BackendNoticeKind, BackendOperation,
    PublishedDoubletakeControl, PublishedDoubletakeProfile,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import MutePayload, SourceVerificationPayload, TransportEventKind
from airplay_linux.domain.operations import InstanceId, OperationId, ProcessIdentity, SensitiveText
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import AudioPolicy, AudioState, Err, ErrorCode, Ok
from airplay_linux.storage import AtomicFiles, JsonReceiverStore
from airplay_linux.testing.adapters import Deferred, FakeCapture, Journal, SequenceIds
from airplay_linux.testing.builders import context, physical, target, virtual_handle
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.transport.targets import TargetRegistry
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore
from airplay_linux.process.daemon_registry import DaemonNotice


class Subscription:
    def __init__(self, callbacks, callback):
        self.callbacks, self.callback = callbacks, callback

    def close(self):
        if self.callback in self.callbacks:
            self.callbacks.remove(self.callback)


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.result = Deferred()

    async def wait(self):
        self.entered.set()
        return await self.result.wait()

    def release(self, value=None):
        self.result.resolve(value)


def daemon(number=9):
    return DaemonRecord(
        schema_version=1, instance_uuid=InstanceId(UUID(int=number)),
        owner_token="private-owner-canary", app=ProcessIdentity(pid=100, start_ticks=1),
        supervisor=ProcessIdentity(pid=200 + number, start_ticks=2),
        daemon=ProcessIdentity(pid=300 + number, start_ticks=3), protocol_version="v1",
        socket_path=f"/run/private/{number}/daemon.sock", lifecycle="running",
        runtime_directory=f"/run/private/{number}",
    )


class Registry:
    """Replace only process ownership admission; transport keeps real fencing."""

    def __init__(self):
        self.record = daemon()
        self.callbacks = []
        self.gate = None
        self.ensure_calls = 0
        self.restart_calls = 0
        self.shutdown_result = Ok(value=None)
        self.shutdown_gate = None
        self.shutdown_calls = 0

    def subscribe(self, callback):
        self.callbacks.append(callback)
        return Subscription(self.callbacks, callback)

    def emit(self, kind, instance=None):
        notice = DaemonNotice(kind=kind, instance_uuid=instance or self.record.instance_uuid)
        for callback in tuple(self.callbacks):
            callback(notice)

    async def ensure(self):
        self.ensure_calls += 1
        if self.gate is not None:
            gate, self.gate = self.gate, None
            await gate.wait()
        return Ok(value=self.record)

    async def restart(self):
        self.restart_calls += 1
        raise AssertionError("transport must never own restart scheduling")

    async def shutdown(self):
        self.shutdown_calls += 1
        if self.shutdown_gate is not None:
            await self.shutdown_gate.wait()
        return self.shutdown_result


class Control:
    """A typed backend boundary with target-local replies and suspension seams."""

    def __init__(self, record):
        self.record = record
        self.callbacks = []
        self.journal = Journal()
        self.gates = {}
        self.results = defaultdict(deque)
        self.connected = set()
        self.audio = set()
        self.muted = {}
        self.closed = False
        self.handshake_gate = None
        self.handshake_wrong = False

    def subscribe(self, callback):
        self.callbacks.append(callback)
        return Subscription(self.callbacks, callback)

    def emit(self, notice):
        for callback in tuple(self.callbacks):
            callback(notice)

    async def handshake(self, owner_token, instance_uuid):
        from airplay_linux.backend.control import Handshake
        if self.handshake_gate is not None:
            await self.handshake_gate.wait()
        return Ok(value=Handshake(
            instance_uuid=InstanceId(UUID(int=999)) if self.handshake_wrong else instance_uuid,
            owner_token=owner_token, daemon=self.record.daemon, protocol_version="v1",
        ))

    async def request(self, operation, receiver, payload, ctx, *, dispatch_fence=None):
        # Mirror the published adapter: a target-addressed state change is never
        # dispatched unfenced, and the fence is evaluated before any effect.
        if dispatch_fence is not None:
            fenced = dispatch_fence()
            if isinstance(fenced, Err):
                return fenced
        self.journal.record("request", operation=operation, target=receiver, payload=payload, context=ctx)
        key = (operation, receiver.receiver_id if receiver is not None else None)
        gate = self.gates.pop(key, None)
        if gate is not None:
            result = await gate.wait()
            if result is not None:
                return result
        queued = self.results[operation]
        if queued:
            result = queued.popleft()
            if isinstance(result, BaseException):
                raise result
            return result
        if operation is BackendOperation.begin_pairing:
            return Ok(value=StatusReply(
                state="pin_required", device="Receiver", target=receiver.canonical_host,
                has_audio=False, audio_muted=False, needs_credential=True,
                credential_kind="pin", streams=(),
            ))
        if operation is BackendOperation.connect:
            # The adjudicated payload works even with the published CLI profile.
            argv = PublishedDoubletakeProfile(socket_path=self.record.socket_path).try_argv(
                operation, receiver, payload,
            )
            assert isinstance(argv, Ok), argv
            self.connected.add(receiver.receiver_id)
        if operation is BackendOperation.disconnect:
            self.connected.discard(receiver.receiver_id)
            self.audio.discard(receiver.receiver_id)
        if operation is BackendOperation.start_audio:
            self.audio.add(receiver.receiver_id)
        if operation in (BackendOperation.mute, BackendOperation.unmute):
            self.muted[receiver.receiver_id] = operation is BackendOperation.mute
        return Ok(value=AckReply(
            operation=operation.value, target=receiver.canonical_host if receiver else None,
            state="streaming" if operation is BackendOperation.connect else None,
            muted=(operation is BackendOperation.mute) if operation in (
                BackendOperation.mute, BackendOperation.unmute,
            ) else None,
        ))

    async def close(self):
        self.closed = True

    def calls(self, operation):
        return [call for call in self.journal.calls("request") if call["operation"] is operation]


class SourceVerifier:
    def __init__(self, result=Ok(value=None)):
        self.calls = []
        self.result = result

    async def verify(self, requested, reported):
        self.calls.append((requested, reported))
        if reported is None or reported.has_position is not True:
            return verify_reported_source(requested, reported, ())
        return self.result


class Harness:
    def __init__(self, tmp_path, source_verifier=None, **features):
        self.features = replace(ApplicationContractProfile(socket_path="/run/private/daemon.sock").features, **features)
        self.registry = Registry()
        self.controls = {}
        self.control = self.factory(self.registry.record)
        self.capture = FakeCapture()
        self.source = physical()
        self.capture.add_physical(self.source)
        self.clock = FakeClock(20)
        self.scheduler = FakeScheduler(self.clock)
        self.fences = TrustFenceRegistry()
        self.credentials = BackendKeyringStore(self.control, self.features)
        self.receivers = JsonReceiverStore(AtomicFiles(root=tmp_path))
        self.transport = DoubleTakeTransport(
            self.factory, self.registry, self.capture, self.credentials, self.receivers,
            self.fences, self.features, self.clock, self.scheduler, SequenceIds(100),
            source_verifier=source_verifier,
        )
        self.a = target()
        self.b = target("b", address="192.0.2.11")
        self.events = []
        self.subscription = self.transport.subscribe(self.events.append)
        self.admit(self.a)
        self.admit(self.b)

    def factory(self, record):
        return self.controls.setdefault(record.instance_uuid, Control(record))

    def admit(self, receiver, ctx=None):
        ctx = ctx or context()
        self.fences.set(TrustFence(
            receiver_id=receiver.receiver_id, endpoint_fingerprint=receiver.endpoint_fingerprint,
            endpoint_generation=receiver.endpoint_generation, session_generation=ctx.session_generation,
            trust_generation=ctx.trust_generation or 0,
        ))
        self.transport.update_target(receiver, receiver.receiver_id)

    async def start(self, receiver=None, source=None, ctx=None, enabled=True):
        return await self.transport.start_stream(
            receiver or self.a, source or self.source, AudioPolicy(enabled=enabled), ctx or context(),
        )

    async def handle(self, receiver=None, **kwargs):
        result = await self.start(receiver, **kwargs)
        assert isinstance(result, Ok), result
        return result.value


def _source_status(receiver, source):
    stream = {
        "device": "Receiver", "device_ip": receiver.canonical_host,
        "state": "streaming", "has_audio": False, "audio_muted": False,
    }
    if source is not None:
        stream["source"] = {
            "type": source.type, "width": source.width, "height": source.height,
            "has_position": source.has_position,
        }
        if source.x is not None and source.y is not None:
            stream["source"].update(x=source.x, y=source.y)
    decoded = decode_status({
        "ok": True, "state": "streaming", "has_audio": False,
        "audio_muted": False, "device": "Receiver",
        "device_ip": receiver.canonical_host, "streams": [stream],
    })
    assert isinstance(decoded, StatusReply), decoded
    return Ok(value=decoded)


class _KScreenProcess:
    def __init__(self, document):
        self.document = document

    async def run(self, spec, timeout, stdin=None):
        return Ok(value=CommandOutput(
            exit_code=0, stdout=json.dumps(self.document).encode(), stderr=b"",
        ))


def _production_source_verifier(*, unknown_enabled_output=False):
    outputs = [{
        "id": 1, "name": "DP-1", "enabled": True, "connected": True,
        "type": 14, "pos": {"x": 0, "y": 0},
        "size": {"width": 1920, "height": 1080}, "scale": 1,
    }]
    if unknown_enabled_output:
        outputs.append({
            "id": 2, "name": "HDMI-1", "connected": True,
            "type": 6, "pos": {"x": 0, "y": 0},
            "size": {"width": 1920, "height": 1080}, "scale": 1,
        })
    return KScreenReportedSourceVerifier(
        KScreenInventory(_KScreenProcess({"outputs": outputs})),
        physical_inventory_available=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reported", "reason", "unknown_enabled_output"),
    [
        (None, SourceRefusal.MISSING_SOURCE, False),
        (
            BackendSource(
                type=1, width=1920, height=1080, x=None, y=None, has_position=False,
            ),
            SourceRefusal.MISSING_POSITION, False,
        ),
        (
            BackendSource(type=1, width=1920, height=1080, x=0, y=0),
            SourceRefusal.UNREADABLE_LAYOUT, True,
        ),
    ],
)
async def test_typed_source_refusals_reach_transport_diagnostic(
    tmp_path, reported, reason, unknown_enabled_output,
):
    verifier = _production_source_verifier(unknown_enabled_output=unknown_enabled_output)
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    h.control.results[BackendOperation.status].append(_source_status(h.a, reported))
    pending = asyncio.create_task(h.start())
    await _drain_transport()
    if reason is SourceRefusal.MISSING_SOURCE:
        assert not pending.done()
        h.scheduler.advance(5.0)
        await _drain_transport()
    result = await pending

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert result.error.detail == reason.value
    assert verifier.last_observation is not None
    assert verifier.last_observation.reason is reason
    failures = [event for event in h.events if event.kind is TransportEventKind.failed]
    assert failures and failures[-1].payload.diagnostic.detail == reason.value
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1


@pytest.mark.asyncio
async def test_verified_source_geometry_is_observable_on_ready_event(tmp_path):
    verifier = _production_source_verifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    reported = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].append(_source_status(h.a, reported))

    result = await h.start()

    assert isinstance(result, Ok), result
    assert result.value.source_connector == "DP-1"
    ready = next(event for event in h.events if event.kind is TransportEventKind.ready)
    assert isinstance(ready.payload, SourceVerificationPayload)
    assert ready.payload.connector == "DP-1"
    assert (ready.payload.source_type, ready.payload.width, ready.payload.height) == (1, 1920, 1080)
    assert (ready.payload.x, ready.payload.y) == (0, 0)
    assert not hasattr(ready.payload, "token")
    assert not hasattr(ready.payload, "socket_path")


async def _drain_transport():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", ["other_target", "absent_source", "duplicate_target", "wrong_reply", "read_error"])
async def test_reported_source_waits_for_exact_target_evidence(tmp_path, incomplete):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    observations = {
        "other_target": _source_status(h.b, source),
        "absent_source": _source_status(h.a, None),
        "duplicate_target": Ok(value=replace(
            _source_status(h.a, source).value,
            streams=_source_status(h.a, source).value.streams * 2,
        )),
        "wrong_reply": Ok(value=AckReply(operation="status")),
        "read_error": Err(error=diagnostic(ErrorCode.transport_failed)),
    }
    h.control.results[BackendOperation.status].extend([
        observations[incomplete], _source_status(h.a, source),
    ])

    pending = asyncio.create_task(h.start())
    await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 1
    assert not pending.done()
    expected_before_retry = [("DP-1", None)] if incomplete == "absent_source" else []
    assert verifier.calls == expected_before_retry
    assert not any(event.kind is TransportEventKind.ready for event in h.events)

    h.scheduler.advance(0.099)
    await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 1
    h.scheduler.advance(0.001)
    result = await pending

    assert isinstance(result, Ok), result
    assert len(h.control.calls(BackendOperation.status)) == 2
    expected_calls = [("DP-1", source)]
    if incomplete == "absent_source":
        expected_calls.insert(0, ("DP-1", None))
    assert verifier.calls == expected_calls
    assert any(event.kind is TransportEventKind.ready for event in h.events)
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
async def test_reported_source_credential_challenge(tmp_path):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    stream = BackendStream(
        device="Receiver", target=h.a.canonical_host, state="pin_required",
        has_audio=False, audio_muted=False, source=None,
    )
    h.control.results[BackendOperation.status].append(Ok(value=StatusReply(
        state="pin_required", device="Receiver", target=h.a.canonical_host,
        has_audio=False, audio_muted=False, needs_credential=True,
        credential_kind="pin", streams=(stream,),
    )))

    result = await h.start()

    assert_error(result, ErrorCode.dependency_incompatible)
    assert verifier.calls == []
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.control.calls(BackendOperation.disconnect)[0]["target"]["receiver_id"] == h.a.receiver_id
    assert h.transport._streams == {}
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
async def test_reported_source_foreign_credential_challenge(tmp_path):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    requested = _source_status(h.a, source).value
    foreign = replace(
        requested.streams[0], target=h.b.canonical_host,
        state="pin_required", credential_kind="pin",
    )
    h.control.results[BackendOperation.status].append(Ok(value=replace(
        requested, state="pin_required", target=h.b.canonical_host,
        needs_credential=True, credential_kind="pin",
        streams=(foreign, *requested.streams),
    )))

    result = await h.start()

    assert isinstance(result, Ok), result
    assert verifier.calls == [("DP-1", source)]
    assert h.control.calls(BackendOperation.disconnect) == []
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind", ["pin", "password"])
@pytest.mark.parametrize("projected_target", ["requested", "other"])
@pytest.mark.parametrize("cleanup", ["confirmed", "rejected", "timeout"])
async def test_reported_source_credential_challenge_refuses_complete_geometry(
    tmp_path, credential_kind, projected_target, cleanup,
):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].append(_source_status(h.b, source))
    b = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    verifier.calls.clear()

    reported = _source_status(h.a, source).value
    waiting = replace(
        reported.streams[0], state="pin_required", credential_kind=credential_kind,
    )
    challenge = replace(
        reported, state="pin_required", needs_credential=True,
        credential_kind=credential_kind, streams=(waiting,),
    )
    if projected_target == "other":
        challenge = replace(
            challenge, target=h.b.canonical_host,
            streams=(replace(waiting, target=h.b.canonical_host), waiting),
        )
    h.control.results[BackendOperation.status].extend([
        _source_status(h.a, None), Ok(value=challenge),
    ])
    if cleanup == "rejected":
        h.control.results[BackendOperation.disconnect].append(
            Err(error=diagnostic(ErrorCode.transport_failed)),
        )
    elif cleanup == "timeout":
        disconnect = h.control.gates[(BackendOperation.disconnect, h.a.receiver_id)] = Gate()

    pending = asyncio.create_task(h.start())
    await _drain_transport()
    assert not pending.done()
    h.scheduler.advance(0.1)
    await _drain_transport()

    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert verifier.calls == [("DP-1", None)]
    if cleanup == "timeout":
        assert disconnect.entered.is_set()
        assert not pending.done()
        h.scheduler.advance(5.0)
        await _drain_transport()
    assert pending.done()
    assert_error(
        await pending,
        ErrorCode.dependency_incompatible if cleanup == "confirmed" else ErrorCode.cleanup_timeout,
    )
    calls = h.control.calls(BackendOperation.disconnect)
    assert len(calls) == 1
    assert calls[0]["target"]["receiver_id"] == h.a.receiver_id
    assert h.b.receiver_id in h.control.connected
    assert h.transport._streams[b.lease_id].handle == b
    assert (context().transport_lease_id in h.transport._streams) is (cleanup != "confirmed")
    assert {call["operation"] for call in h.control.journal.calls("request")} == {
        BackendOperation.connect, BackendOperation.status, BackendOperation.disconnect,
    }
    assert h.scheduler.pending() == ()
    if cleanup == "timeout":
        disconnect.release()
        await _drain_transport()
        assert h.control.connected == {h.b.receiver_id}
        assert context().transport_lease_id not in h.transport._streams


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind", ["pin", "password"])
async def test_reported_source_foreign_credential_challenge_does_not_refuse_requested_stream(
    tmp_path, credential_kind,
):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    reported = _source_status(h.a, source).value
    foreign = replace(
        reported.streams[0], target=h.b.canonical_host,
        state="pin_required", credential_kind=credential_kind,
    )
    h.control.results[BackendOperation.status].append(Ok(value=replace(
        reported, state="pin_required", target=h.b.canonical_host,
        needs_credential=True, credential_kind=credential_kind,
        streams=(foreign, *reported.streams),
    )))

    result = await h.start()

    assert isinstance(result, Ok), result
    assert verifier.calls == [("DP-1", source)]
    assert any(event.kind is TransportEventKind.ready for event in h.events)
    assert h.control.calls(BackendOperation.disconnect) == []
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("final_observation", ["source", "challenge"])
async def test_source_and_challenge_share_one_post_connect_deadline(tmp_path, final_observation):
    verifier = SourceVerifier()
    h = Harness(tmp_path, source_verifier=verifier, physical_source=True, source_reporting_complete=True)
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    foreign = _source_status(h.b, source).value
    foreign_challenge = Ok(value=replace(
        foreign, state="pin_required", needs_credential=True, credential_kind="pin",
        streams=(replace(foreign.streams[0], state="pin_required", credential_kind="pin"),),
    ))
    # Alternate incomplete requested evidence and another target's challenge.
    # Neither observation establishes readiness or replenishes the original budget.
    h.control.results[BackendOperation.status].extend(
        _source_status(h.a, None) if index % 2 == 0 else foreign_challenge
        for index in range(49)
    )
    pending = asyncio.create_task(h.start())
    await _drain_transport()
    for _ in range(48):
        h.scheduler.advance(0.1)
        await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 49
    late = h.control.gates[(BackendOperation.status, None)] = Gate()
    h.scheduler.advance(0.1)
    await _drain_transport()
    assert late.entered.is_set() and not pending.done()
    final = _source_status(h.a, source)
    if final_observation == "challenge":
        report = final.value
        final = Ok(value=replace(
            report, state="pin_required", needs_credential=True, credential_kind="pin",
            streams=(replace(report.streams[0], state="pin_required", credential_kind="pin"),),
        ))
    # Let a reply return at the absolute deadline before delivering timer callbacks.
    h.clock.advance(0.1)
    late.release(final)
    await _drain_transport()
    assert pending.done(), "alternating source/challenge observations reset the shared deadline"
    assert_error(await pending, ErrorCode.unsupported_source)
    assert len(h.control.calls(BackendOperation.status)) == 50
    assert len(verifier.calls) == 25
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert h.transport._streams == {}
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source", "ack_challenge", "status_challenge"])
@pytest.mark.parametrize("cleanup", ["rejected", "timeout"])
async def test_unconfirmed_start_disconnect_takes_cleanup_timeout_precedence(tmp_path, failure, cleanup):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier, physical_source=True, source_reporting_complete=True,
        owned_daemon_control=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].append(_source_status(h.b, source))
    b = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    verifier.calls.clear()
    verifier.result = Err(error=diagnostic(ErrorCode.unsupported_source)) if failure == "source" else Ok(value=None)
    if failure == "ack_challenge":
        h.control.results[BackendOperation.connect].append(Ok(value=AckReply(
            operation="connect", target=h.a.canonical_host, state="pin_required",
        )))
    else:
        report = _source_status(h.a, source).value
        if failure == "status_challenge":
            report = replace(
                report, state="pin_required", needs_credential=True, credential_kind="pin",
                streams=(replace(report.streams[0], state="pin_required", credential_kind="pin"),),
            )
        h.control.results[BackendOperation.status].append(Ok(value=report))
    h.control.connected.add(h.a.receiver_id)
    if cleanup == "rejected":
        h.control.results[BackendOperation.disconnect].append(Err(error=diagnostic(ErrorCode.transport_failed)))
    else:
        gate = h.control.gates[(BackendOperation.disconnect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.start())
    await _drain_transport()
    if cleanup == "timeout":
        assert gate.entered.is_set() and not pending.done()
        h.scheduler.advance(5.0)
        await _drain_transport()
    assert pending.done()
    assert_error(await pending, ErrorCode.cleanup_timeout)
    assert context().transport_lease_id in h.transport._streams
    assert h.transport._streams[b.lease_id].handle == b
    assert h.b.receiver_id in h.control.connected
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.control.calls(BackendOperation.disconnect)[0]["target"]["receiver_id"] == h.a.receiver_id
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert {call["operation"] for call in h.control.journal.calls("request")} <= {
        BackendOperation.connect, BackendOperation.status, BackendOperation.disconnect,
    }
    if cleanup == "timeout":
        gate.release()
        await _drain_transport()
        assert context().transport_lease_id not in h.transport._streams
        assert h.control.connected == {h.b.receiver_id}
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
async def test_reported_source_wait_has_one_five_second_deadline(tmp_path):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].extend(
        _source_status(h.b, source) if index % 2 else _source_status(h.a, None)
        for index in range(60)
    )

    pending = asyncio.create_task(h.start())
    await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 1
    assert not pending.done()

    for _ in range(49):
        h.scheduler.advance(0.1)
        await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 50
    assert not pending.done()

    h.scheduler.advance(0.1)
    await _drain_transport()
    assert pending.done()
    result = await pending

    assert_error(result, ErrorCode.unsupported_source)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert len(h.control.calls(BackendOperation.status)) == 50
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.transport._streams == {}
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["status", "verifier"])
@pytest.mark.parametrize("expiry", ["scheduler", "late_reply"])
async def test_reported_source_deadline_bounds_inflight_observation(tmp_path, blocked, expiry):
    gate = Gate()

    class WaitingVerifier(SourceVerifier):
        async def verify(self, requested, reported):
            await gate.wait()
            return await super().verify(requested, reported)

    verifier = WaitingVerifier() if blocked == "verifier" else SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    if blocked == "status":
        h.control.gates[(BackendOperation.status, None)] = gate
    else:
        h.control.results[BackendOperation.status].append(_source_status(
            h.a, BackendSource(type=1, width=1920, height=1080, x=0, y=0),
        ))
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    h.scheduler.advance(4.9)
    await _drain_transport()
    assert not pending.done()
    if expiry == "scheduler":
        h.scheduler.advance(0.1)
    else:
        h.clock.advance(0.1)
        gate.release(_source_status(
            h.a, BackendSource(type=1, width=1920, height=1080, x=0, y=0),
        ))
    await _drain_transport()
    assert pending.done()

    assert_error(await pending, ErrorCode.unsupported_source)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.transport._streams == {}
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
async def test_reported_source_deadline_starts_after_connect_acknowledgement(tmp_path):
    h = Harness(
        tmp_path, source_verifier=SourceVerifier(),
        physical_source=True, source_reporting_complete=True,
    )
    connect = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    status = h.control.gates[(BackendOperation.status, None)] = Gate()
    pending = asyncio.create_task(h.start())
    await connect.entered.wait()
    h.scheduler.advance(4.0)
    connect.release()
    await status.entered.wait()

    h.scheduler.advance(4.9)
    await _drain_transport()
    assert not pending.done()
    h.scheduler.advance(0.1)
    await _drain_transport()
    assert pending.done()

    assert_error(await pending, ErrorCode.unsupported_source)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert not any(event.kind is TransportEventKind.ready for event in h.events)


@pytest.mark.asyncio
async def test_reported_source_deadline_does_not_reset_after_incomplete_reads(tmp_path):
    verified = Gate()

    class WaitingVerifier(SourceVerifier):
        async def verify(self, requested, reported):
            await verified.wait()
            return await super().verify(requested, reported)

    h = Harness(
        tmp_path, source_verifier=WaitingVerifier(),
        physical_source=True, source_reporting_complete=True,
    )
    h.control.results[BackendOperation.status].extend([
        Ok(value=AckReply(operation="status")), _source_status(h.b, None),
        _source_status(h.a, BackendSource(type=1, width=1920, height=1080, x=0, y=0)),
    ])
    pending = asyncio.create_task(h.start())
    await _drain_transport()
    h.scheduler.advance(0.1)
    await _drain_transport()
    h.scheduler.advance(0.1)
    await verified.entered.wait()

    # A completion at the original deadline is too late even if its timeout
    # callback has not run yet; the two reads must not replenish the budget.
    h.clock.advance(4.8)
    verified.release()
    assert_error(await pending, ErrorCode.unsupported_source)
    assert len(h.control.calls(BackendOperation.status)) == 3
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
async def test_reported_source_wall_deadline_does_not_restart_after_a_read(tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    timers = []

    def call_later(delay, callback):
        timer = loop.call_at(loop.time() + delay, callback)
        timers.append((delay, callback, timer))
        return timer

    monkeypatch.setattr(loop, "call_later", call_later)
    h = Harness(
        tmp_path, source_verifier=SourceVerifier(),
        physical_source=True, source_reporting_complete=True,
    )
    status = h.control.gates[(BackendOperation.status, None)] = Gate()
    pending = asyncio.create_task(h.start())
    await status.entered.wait()
    assert [timer[0] for timer in timers] == [5.0, 5.0]
    source_expire = timers[1][1]
    h.scheduler.advance(2.0)
    status.release(_source_status(h.a, None))
    await _drain_transport()
    h.scheduler.advance(0.1)
    await _drain_transport()
    assert len(h.control.calls(BackendOperation.status)) == 2
    assert len(timers) == 2
    assert timers[0][2].cancelled()
    assert not timers[1][2].cancelled()
    assert not pending.done()

    # Drive the single wall expiration while injected time is still at 22.1.
    source_expire()
    assert_error(await pending, ErrorCode.unsupported_source)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert h.scheduler.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_result", ["absent", "mismatch"])
@pytest.mark.parametrize("cleanup", ["confirmed", "rejected", "timeout"])
async def test_unverified_source_cleanup_precedence_retains_exact_obligation(tmp_path, source_result, cleanup):
    verifier = SourceVerifier()
    h = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].append(_source_status(h.b, source))
    b = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    if source_result == "mismatch":
        verifier.result = Err(error=diagnostic(ErrorCode.unsupported_source))
    h.control.results[BackendOperation.status].append(
        _source_status(h.a, source if source_result == "mismatch" else None),
    )
    if cleanup == "rejected":
        h.control.results[BackendOperation.disconnect].append(
            Err(error=diagnostic(ErrorCode.transport_failed)),
        )
    elif cleanup == "timeout":
        disconnect = h.control.gates[(BackendOperation.disconnect, h.a.receiver_id)] = Gate()

    pending = asyncio.create_task(h.start())
    await _drain_transport()
    if source_result == "absent":
        assert not pending.done()
        h.scheduler.advance(5.0)
        await _drain_transport()
    if cleanup == "timeout":
        await asyncio.wait_for(disconnect.entered.wait(), timeout=1.0)
        assert not pending.done()
        h.scheduler.advance(5.0)
        await _drain_transport()
        assert pending.done()

    result = await pending
    assert_error(result, ErrorCode.unsupported_source if cleanup == "confirmed" else ErrorCode.cleanup_timeout)
    calls = h.control.calls(BackendOperation.disconnect)
    assert len(calls) == 1
    assert calls[0]["target"]["receiver_id"] == h.a.receiver_id
    assert h.b.receiver_id in h.control.connected
    assert h.transport._streams[b.lease_id].handle == b
    assert (context().transport_lease_id in h.transport._streams) is (cleanup != "confirmed")
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    if cleanup == "timeout":
        disconnect.release()
        await _drain_transport()
        assert h.control.connected == {h.b.receiver_id}
        assert context().transport_lease_id not in h.transport._streams


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidated", ["cancelled", "replacement"])
async def test_late_source_verification_cannot_publish_an_invalidated_start(tmp_path, invalidated):
    verified = Gate()

    class WaitingVerifier(SourceVerifier):
        async def verify(self, requested, reported):
            await verified.wait()
            return await super().verify(requested, reported)

    h = Harness(
        tmp_path, source_verifier=WaitingVerifier(),
        physical_source=True, source_reporting_complete=True,
    )
    h.control.results[BackendOperation.status].append(_source_status(
        h.a, BackendSource(type=1, width=1920, height=1080, x=0, y=0),
    ))
    pending = asyncio.create_task(h.start())
    await verified.entered.wait()
    if invalidated == "cancelled":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        replacement = target("replacement")
        h.admit(replacement)
    verified.release()
    await _drain_transport()

    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    if invalidated == "cancelled":
        assert len(h.control.calls(BackendOperation.disconnect)) == 1
        assert h.control.connected == set()
    else:
        assert_error(await pending, ErrorCode.cleanup_timeout)
        assert h.control.calls(BackendOperation.disconnect) == []
        assert context().transport_lease_id in h.transport._streams


@pytest.mark.asyncio
async def test_physical_stream_requires_fresh_backend_source_verification(tmp_path):
    verifier = SourceVerifier()
    harness = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    harness.control.results[BackendOperation.status].append(
        _source_status(
            harness.a,
            BackendSource(type=1, width=1920, height=1080, x=0, y=0),
        )
    )

    result = await harness.start()

    assert isinstance(result, Ok), result
    assert verifier.calls == [("DP-1", BackendSource(
        type=1, width=1920, height=1080, x=0, y=0,
    ))]
    assert len(harness.control.calls(BackendOperation.status)) == 1


@pytest.mark.asyncio
async def test_physical_stream_source_refusal_disconnects_before_returning_error(tmp_path):
    verifier = SourceVerifier(Err(error=diagnostic(ErrorCode.unsupported_source)))
    harness = Harness(
        tmp_path, source_verifier=verifier,
        physical_source=True, source_reporting_complete=True,
    )
    harness.control.results[BackendOperation.status].append(
        _source_status(
            harness.a,
            BackendSource(type=1, width=1920, height=1080, x=0, y=0),
        )
    )

    result = await harness.start()

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert harness.control.calls(BackendOperation.disconnect)
    assert harness.transport._streams == {}


def contract_status(record, *, address="192.0.2.10", muted=False):
    """Status envelope proving the exact daemon named by ``record``."""
    stream = {
        "device": "Living Room", "device_ip": address, "state": "streaming",
        "has_audio": True, "audio_muted": muted,
    }
    return {
        "ok": True, "state": "streaming", "device": "Living Room",
        "device_ip": address, "has_audio": True, "audio_muted": muted,
        "owner_token": record.owner_token,
        "instance_uuid": str(record.instance_uuid),
        "socket_path": record.socket_path,
        "protocol_version": record.protocol_version,
        "daemon_pid": record.daemon.pid,
        "daemon_start_ticks": record.daemon.start_ticks,
        "streams": [stream],
    }


class ContractProcess:
    """Gateable process port that journals the real adapter's argv dispatch."""

    def __init__(self, record):
        self.record = record
        self.journal = Journal()
        self.status_calls = 0
        self.gated_at = None
        self.gate = None

    def gate_next_status(self):
        gate = Gate()
        self.gate, self.gated_at = gate, self.status_calls + 1
        return gate

    def commands(self, operation):
        return [
            tuple(call["argv"]) for call in self.journal.calls("run")
            if tuple(call["argv"])[3] == operation
        ]

    def mutations(self):
        return [
            tuple(call["argv"]) for call in self.journal.calls("run")
            if tuple(call["argv"])[3] not in {"status", "discover", "devices"}
        ]

    async def run(self, spec, timeout, stdin=None):
        argv = tuple(spec.argv)
        self.journal.record("run", argv=argv)
        operation = argv[3]
        if operation == "status":
            self.status_calls += 1
            if self.gated_at == self.status_calls:
                gate, self.gate, self.gated_at = self.gate, None, None
                await gate.wait()
            return self._output(contract_status(self.record))
        target_text = argv[4] if len(argv) > 4 else None
        if operation in {"mute", "unmute"}:
            return self._output(contract_status(
                self.record, address=target_text, muted=operation == "mute",
            ))
        if operation == "disconnect":
            return self._output({"ok": True, "state": "idle"})
        return self._output({"ok": True, "state": "streaming"})

    @staticmethod
    def _output(payload):
        return Ok(value=CommandOutput(
            exit_code=0, stdout=json.dumps(payload).encode(), stderr=b"",
        ))


class ContractHarness(Harness):
    """The real control adapter composed with a gateable process port."""

    def __init__(self, tmp_path):
        self.process = ContractProcess(daemon())
        super().__init__(tmp_path)

    def factory(self, record):
        self.process.record = record
        return PublishedDoubletakeControl(
            self.process,
            ApplicationContractProfile(
                socket_path=record.socket_path,
                owner_token=record.owner_token,
                instance_uuid=record.instance_uuid,
                protocol_version=record.protocol_version,
            ),
        )


class _LegacyControl:
    """A control whose ``request`` predates the dispatch fence.

    It still satisfies the old four-parameter protocol shape, so nothing in the
    type system rejects it. Transport must refuse to dispatch a target-addressed
    state change through it rather than calling it unfenced.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls = []

    def subscribe(self, callback):
        return self._inner.subscribe(callback)

    async def handshake(self, owner_token, instance_uuid):
        return await self._inner.handshake(owner_token, instance_uuid)

    async def request(self, operation, target, payload, context):
        self.calls.append(operation)
        return await self._inner.request(operation, target, payload, context)


class _FenceDroppingWrapper:
    """Preserves the fenced signature but can discard the keyword.

    Signature inspection cannot tell this apart from an honest adapter, so the
    guarantee has to come from the inner adapter refusing to spawn unfenced.
    ``forward`` is switched off only after a stream exists, so the test reaches
    the dispatch fence instead of failing earlier at ownership revalidation.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.forward = True

    def subscribe(self, callback):
        return self._inner.subscribe(callback)

    async def handshake(self, owner_token, instance_uuid):
        return await self._inner.handshake(owner_token, instance_uuid)

    async def request(self, operation, target, payload, context, *, dispatch_fence=None):
        self.calls += 1
        if self.forward:
            return await self._inner.request(
                operation, target, payload, context, dispatch_fence=dispatch_fence,
            )
        # Deliberately drop dispatch_fence to simulate a betraying decorator.
        return await self._inner.request(operation, target, payload, context)


class _WrappedHarness(ContractHarness):
    def __init__(self, tmp_path, wrapper):
        self.wrapper_factory = wrapper
        super().__init__(tmp_path)

    def factory(self, record):
        self.process.record = record
        key = record.instance_uuid
        if key not in self.controls:
            inner = PublishedDoubletakeControl(
                self.process,
                ApplicationContractProfile(
                    socket_path=record.socket_path,
                    owner_token=record.owner_token,
                    instance_uuid=record.instance_uuid,
                    protocol_version=record.protocol_version,
                ),
            )
            self.controls[key] = self.wrapper_factory(inner)
        return self.controls[key]


@pytest.mark.asyncio
async def test_legacy_control_without_a_fence_is_rejected_before_any_call(tmp_path):
    """F-5: a control that cannot be fenced must not receive a mutation at all."""
    h = _WrappedHarness(tmp_path, _LegacyControl)
    before = h.process.mutations()

    result = await h.start(h.a)

    # The transport fails closed. The surfaced code is a mapped cleanup/staleness
    # variant; the load-bearing evidence is that the control was never called and
    # no mutating command ever reached the process port.
    assert isinstance(result, Err), result
    wrapper = h.control
    assert isinstance(wrapper, _LegacyControl)
    assert wrapper.calls == [], wrapper.calls
    assert h.process.mutations() == before


@pytest.mark.asyncio
async def test_fence_dropping_wrapper_cannot_dispatch_a_mutation(tmp_path):
    """F-5: a signature-preserving decorator that drops the keyword still spawns nothing."""
    h = _WrappedHarness(tmp_path, _FenceDroppingWrapper)
    # Establish a stream while the wrapper still forwards the fence, so the
    # later mutation is evaluated at the dispatch boundary rather than failing
    # earlier during admission.
    handle = await h.handle()
    assert isinstance(await h.transport.start_audio(handle, context()), Ok)
    wrapper = h.control
    assert isinstance(wrapper, _FenceDroppingWrapper)
    wrapper.forward = False
    before = h.process.mutations()
    calls_before = wrapper.calls

    result = await h.transport.set_muted(handle, True, context())

    assert isinstance(result, Err), result
    assert wrapper.calls > calls_before, "the wrapper was expected to be reached"
    # The inner adapter refused to spawn an unfenced state change.
    assert h.process.mutations() == before


def assert_error(result, code):
    assert isinstance(result, Err), result
    assert result.error.code is code


def test_old_endpoint_is_not_a_current_target():
    registry = TargetRegistry()
    old = target(endpoint_generation=1)
    registry.update(target(endpoint_generation=2), old.receiver_id)
    assert_error(registry.require_current(old), ErrorCode.stale_target)


def test_replacement_at_same_address_invalidates_the_previous_receiver():
    registry = TargetRegistry()
    registry.update(target(), target().receiver_id)
    registry.update(target("replacement"), target("replacement").receiver_id)
    assert_error(registry.require_current(target()), ErrorCode.stale_target)
    assert isinstance(registry.require_current(target("replacement")), Ok)


@pytest.mark.asyncio
async def test_two_receivers_connect_concurrently_and_controls_are_stream_local(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.handle())
    await gate.entered.wait()
    b = await h.handle(h.b, ctx=context(lease=2))
    assert h.control.connected == {h.b.receiver_id}
    gate.release()
    a = await pending
    assert a.lease_id != b.lease_id
    assert a.backend_stream_id != b.backend_stream_id
    assert a.daemon_instance_uuid == h.registry.record.instance_uuid
    assert isinstance(await h.transport.start_audio(a, context()), Ok)
    assert isinstance(await h.transport.set_muted(a, True, context()), Ok)
    assert h.control.muted == {h.a.receiver_id: True}
    assert isinstance(await h.transport.stop_stream(a, context(generation=4)), Ok)
    assert h.control.connected == {h.b.receiver_id}
    assert h.control.calls(BackendOperation.disconnect)[0]["target"]["receiver_id"] == h.a.receiver_id
    ready = [event for event in h.events if event.kind is TransportEventKind.ready]
    assert {event.stream for event in ready} == {a, b}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["receiver", "endpoint", "fingerprint", "host"])
async def test_replaced_targets_cannot_receive_active_commands(tmp_path, changed):
    h = Harness(tmp_path)
    handle = await h.handle()
    if changed == "receiver":
        fresh = target("replacement")
        h.admit(fresh)
    else:
        changes = {
            "endpoint": {"endpoint_generation": 2},
            "fingerprint": {"endpoint_fingerprint": "replacement"},
            "host": {"canonical_host": "192.0.2.55"},
        }
        h.transport.update_target(replace(h.a, **changes[changed]), h.a.receiver_id)
    before = len(h.control.journal.entries)
    assert_error(await h.transport.query_modes(h.a, context()), ErrorCode.stale_target)
    assert_error(await h.start(), ErrorCode.stale_target)
    assert_error(await h.transport.set_muted(handle, True, context()), ErrorCode.stale_target)
    assert_error(await h.transport.start_audio(handle, context()), ErrorCode.stale_target)
    assert_error(await h.transport.begin_pairing(h.a, context()), ErrorCode.stale_target)
    assert len(h.control.journal.entries) == before


@pytest.mark.asyncio
async def test_exact_stop_uses_the_original_endpoint_after_receiver_moves(tmp_path):
    h = Harness(tmp_path)
    old = await h.handle()
    fresh = target(endpoint_generation=2, address="192.0.2.77")
    h.transport.update_target(fresh, h.a.receiver_id)
    assert isinstance(await h.transport.stop_stream(old, context(generation=8, endpoint_generation=2)), Ok)
    request = h.control.calls(BackendOperation.disconnect)[0]
    assert request["target"]["canonical_host"] == "192.0.2.10"
    assert request["target"]["endpoint_generation"] == 1
    stopped = [event for event in h.events if event.kind is TransportEventKind.stopped]
    assert stopped[0].stream == old
    assert stopped[0].endpoint_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [target(endpoint_generation=2), target("replacement")])
async def test_target_only_disconnect_cannot_touch_a_replacement_at_the_old_address(tmp_path, replacement):
    h = Harness(tmp_path)
    old = await h.handle()
    h.transport.update_target(replacement, replacement.receiver_id)
    assert_error(await h.transport.stop_stream(old, context(generation=8)), ErrorCode.cleanup_timeout)
    assert h.control.calls(BackendOperation.disconnect) == []
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)


@pytest.mark.asyncio
async def test_replacement_connect_waits_for_old_stream_or_daemon_cleanup(tmp_path):
    h = Harness(tmp_path)
    await h.handle()
    replacement = target("replacement")
    h.admit(replacement)
    assert_error(await h.start(replacement, ctx=context(lease=2)), ErrorCode.cleanup_timeout)
    assert len(h.control.calls(BackendOperation.connect)) == 1


@pytest.mark.asyncio
async def test_shutdown_cannot_confirm_stop_ahead_of_an_inflight_connect(tmp_path):
    h = Harness(tmp_path)
    connect_gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    daemon_gate = h.registry.shutdown_gate = Gate()
    connect = asyncio.create_task(h.start())
    await connect_gate.entered.wait()
    shutdown = asyncio.create_task(h.transport.shutdown())
    for _ in range(30):
        await asyncio.sleep(0)
    assert h.control.calls(BackendOperation.disconnect) == []
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)
    connect_gate.release()
    assert isinstance(await connect, Err)
    await daemon_gate.entered.wait()
    daemon_gate.release()
    assert isinstance(await shutdown, Ok)
    assert h.control.connected == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("after", ["handshake", "stream_lock"])
async def test_target_freshness_is_rechecked_after_each_deferred_admission(tmp_path, after):
    h = Harness(tmp_path)
    if after == "handshake":
        gate = h.control.handshake_gate = Gate()
        pending = asyncio.create_task(h.start())
    else:
        handle = await h.handle()
        assert isinstance(await h.transport.start_audio(handle, context()), Ok)
        gate = h.control.gates[(BackendOperation.mute, h.a.receiver_id)] = Gate()
        first = asyncio.create_task(h.transport.set_muted(handle, True, context()))
        await gate.entered.wait()
        pending = asyncio.create_task(h.transport.start_audio(handle, context()))
        await asyncio.sleep(0)
    await gate.entered.wait()
    h.transport.update_target(target("replacement"), target("replacement").receiver_id)
    gate.release()
    assert_error(await pending, ErrorCode.stale_target)
    if after == "handshake":
        assert h.control.calls(BackendOperation.connect) == []
    else:
        assert_error(await first, ErrorCode.stale_target)
        assert len(h.control.calls(BackendOperation.start_audio)) == 1
        assert not any(event.kind is TransportEventKind.mute_result for event in h.events)


@pytest.mark.asyncio
async def test_cancelled_start_before_capture_finishes_cannot_acquire_backend_resources(tmp_path):
    h = Harness(tmp_path)
    complete = Deferred()
    h.capture.queue_result("verify", complete)
    pending = asyncio.create_task(h.start())
    for _ in range(30):
        if h.capture.journal.calls("capture.verify"):
            break
        await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    complete.resolve(Ok(value=None))
    for _ in range(30):
        await asyncio.sleep(0)
    assert h.control.calls(BackendOperation.connect) == []
    assert isinstance(await h.start(), Ok)


@pytest.mark.asyncio
async def test_virtual_connecting_returns_owned_handle_without_claiming_readiness(tmp_path):
    h = Harness(tmp_path)
    b = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    h.control.results[BackendOperation.connect].append(Ok(value=AckReply(
        operation="connect", target=h.a.canonical_host, state="connecting",
    )))
    h.control.connected.add(h.a.receiver_id)

    result = await h.start(source=virtual_handle().source)

    assert isinstance(result, Ok), result
    assert result.value.lease_id == context().transport_lease_id
    assert h.transport._streams[result.value.lease_id].handle == result.value
    assert h.transport._streams[result.value.lease_id].ready is False
    assert h.transport._streams[b.lease_id].handle == b
    assert h.control.connected == {h.a.receiver_id, h.b.receiver_id}
    assert h.events == []
    assert h.control.calls(BackendOperation.disconnect) == []
    assert h.control.calls(BackendOperation.start_audio) == []
    assert isinstance(await h.transport.stop_stream(result.value, context()), Ok)
    assert h.control.connected == {h.b.receiver_id}
    assert h.control.calls(BackendOperation.disconnect)[0]["target"]["receiver_id"] == h.a.receiver_id


@pytest.mark.asyncio
@pytest.mark.parametrize("source,state", [
    (physical(), state) for state in (None, "idle", "discovering", "connecting", "pin_required")
] + [
    (virtual_handle().source, state) for state in (None, "idle", "discovering", "pin_required")
])
@pytest.mark.parametrize("cleanup", ["confirmed", "rejected", "timeout"])
async def test_connect_acceptance_without_streaming_does_not_claim_video_readiness(tmp_path, source, state, cleanup):
    h = Harness(tmp_path)
    b = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    h.control.results[BackendOperation.connect].append(Ok(value=AckReply(
        operation="connect", target=h.a.canonical_host, state=state,
    )))
    # Queued replies replace the fake's effects; model the possible acquisition.
    h.control.connected.add(h.a.receiver_id)
    if cleanup == "rejected":
        h.control.results[BackendOperation.disconnect].append(
            Err(error=diagnostic(ErrorCode.transport_failed)),
        )
    elif cleanup == "timeout":
        gate = h.control.gates[(BackendOperation.disconnect, h.a.receiver_id)] = Gate()

    pending = asyncio.create_task(h.start(source=source))
    if cleanup == "timeout":
        await asyncio.wait_for(gate.entered.wait(), timeout=1.0)
        h.scheduler.advance(5.0)
        await _drain_transport()
        assert pending.done()
    result = await pending

    assert_error(result, ErrorCode.unsupported_source if cleanup == "confirmed" else ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    calls = h.control.calls(BackendOperation.disconnect)
    assert len(calls) == 1
    assert calls[0]["target"]["receiver_id"] == h.a.receiver_id
    assert calls[0]["target"]["canonical_host"] == h.a.canonical_host
    assert calls[0]["target"]["endpoint_generation"] == h.a.endpoint_generation
    assert h.b.receiver_id in h.control.connected
    assert h.transport._streams[b.lease_id].handle == b
    assert (context().transport_lease_id in h.transport._streams) is (cleanup != "confirmed")
    assert not any(h.control.calls(operation) for operation in (
        BackendOperation.begin_pairing, BackendOperation.submit_pairing,
    ))
    if cleanup == "timeout":
        gate.release()
        await _drain_transport()
        assert h.control.connected == {h.b.receiver_id}
        assert context().transport_lease_id not in h.transport._streams


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_audio_start_refusal_preserves_its_failure_event(tmp_path, ready):
    h = Harness(tmp_path)
    handle = await h.handle()
    # Preserve the old test's pre-ready audio branch without admitting an
    # unconfirmed connect through the public start API.
    h.transport._streams[handle.lease_id].ready = ready
    h.control.results[BackendOperation.start_audio].append(Err(error=diagnostic(ErrorCode.audio_denied)))
    assert_error(await h.transport.start_audio(handle, context()), ErrorCode.audio_denied)
    if ready:
        assert h.events[-1].kind is TransportEventKind.audio_failed
    else:
        assert h.events[-1].kind is TransportEventKind.audio_start_failed


@pytest.mark.asyncio
async def test_wrong_daemon_handshake_cannot_admit_any_backend_command(tmp_path):
    h = Harness(tmp_path)
    h.control.handshake_wrong = True
    assert_error(await h.start(), ErrorCode.ownership_mismatch)
    assert h.control.journal.entries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ctx", [context(generation=2), context(lease=7)])
async def test_stale_session_or_lease_cannot_change_active_audio(tmp_path, ctx):
    h = Harness(tmp_path)
    handle = await h.handle()
    assert_error(await h.transport.start_audio(handle, ctx), ErrorCode.stale_event)
    assert_error(await h.transport.set_muted(handle, True, ctx), ErrorCode.stale_event)
    assert h.control.calls(BackendOperation.start_audio) == []
    assert h.control.calls(BackendOperation.mute) == []


@pytest.mark.asyncio
async def test_stop_timeout_retains_exact_handle_until_late_ack_and_allows_retry(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    gate = h.control.gates[(BackendOperation.disconnect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.transport.stop_stream(handle, context(generation=3)))
    await gate.entered.wait()
    h.scheduler.advance(5)
    assert_error(await pending, ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)
    assert_error(await h.start(ctx=context(lease=2)), ErrorCode.cleanup_timeout)
    gate.release()
    for _ in range(30):
        if any(event.kind is TransportEventKind.stopped for event in h.events):
            break
        await asyncio.sleep(0)
    assert isinstance(await h.transport.stop_stream(handle, context(generation=3)), Ok)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert isinstance(await h.start(ctx=context(lease=2)), Ok)


@pytest.mark.asyncio
async def test_rejected_stop_reply_retains_ownership_and_can_be_retried(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    h.control.results[BackendOperation.disconnect].append(Err(error=diagnostic(ErrorCode.transport_failed)))
    assert_error(await h.transport.stop_stream(handle, context()), ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)
    assert isinstance(await h.transport.stop_stream(handle, context()), Ok)
    assert len(h.control.calls(BackendOperation.disconnect)) == 2


@pytest.mark.asyncio
async def test_failed_unmute_preserves_last_confirmed_mute(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    assert isinstance(await h.transport.start_audio(handle, context()), Ok)
    assert isinstance(await h.transport.set_muted(handle, True, context()), Ok)
    h.control.results[BackendOperation.unmute].append(Err(error=diagnostic(ErrorCode.audio_mute_failed)))
    assert_error(await h.transport.set_muted(handle, False, context()), ErrorCode.audio_mute_failed)
    assert h.events[-1].payload == MutePayload(muted=True, ok=False)
    assert h.control.muted == {h.a.receiver_id: True}


@pytest.mark.asyncio
async def test_backend_exception_text_is_not_copied_into_transport_events(tmp_path):
    h = Harness(tmp_path)
    h.control.results[BackendOperation.connect].append(RuntimeError("private-exception-canary"))
    result = await h.start()
    assert_error(result, ErrorCode.transport_failed)
    assert "private-exception-canary" not in repr(result)
    assert "private-exception-canary" not in repr(h.events)
    assert h.control.connected == set()


@pytest.mark.asyncio
async def test_shutdown_callers_join_one_daemon_cleanup(tmp_path):
    h = Harness(tmp_path)
    await h.handle()
    gate = h.registry.shutdown_gate = Gate()
    first = asyncio.create_task(h.transport.shutdown())
    await gate.entered.wait()
    second = asyncio.create_task(h.transport.shutdown())
    for _ in range(20):
        await asyncio.sleep(0)
    gate.release()
    assert isinstance(await first, Ok)
    assert isinstance(await second, Ok)
    assert h.registry.shutdown_calls == 1


@pytest.mark.asyncio
async def test_removing_a_replacement_does_not_make_old_address_cleanup_safe(tmp_path):
    h = Harness(tmp_path)
    old = await h.handle()
    replacement = target("replacement")
    h.transport.update_target(replacement, replacement.receiver_id)
    h.transport.update_target(None, replacement.receiver_id)
    assert_error(await h.transport.stop_stream(old, context()), ErrorCode.cleanup_timeout)
    assert h.control.calls(BackendOperation.disconnect) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["query", "start", "pair"])
async def test_target_is_rechecked_after_deferred_daemon_admission(tmp_path, operation):
    h = Harness(tmp_path)
    gate = h.registry.gate = Gate()
    command = {
        "query": lambda: h.transport.query_modes(h.a, context()),
        "start": h.start,
        "pair": lambda: h.transport.begin_pairing(h.a, context(lease=None)),
    }[operation]
    pending = asyncio.create_task(command())
    await gate.entered.wait()
    h.transport.update_target(target(endpoint_generation=2), h.a.receiver_id)
    gate.release()
    assert_error(await pending, ErrorCode.stale_target)
    assert h.control.journal.entries == []


@pytest.mark.asyncio
async def test_target_is_rechecked_after_capture_verification(tmp_path):
    h = Harness(tmp_path)
    completion = Deferred()
    h.capture.queue_result("verify", completion)
    pending = asyncio.create_task(h.start())
    for _ in range(20):
        if h.capture.journal.calls("capture.verify"):
            break
        await asyncio.sleep(0)
    assert h.capture.journal.calls("capture.verify")
    h.transport.update_target(target(endpoint_generation=2), h.a.receiver_id)
    completion.resolve(Ok(value=None))
    assert_error(await pending, ErrorCode.stale_target)
    assert h.control.calls(BackendOperation.connect) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [physical("window:4"), "DP-1", object()])
async def test_nonphysical_or_untyped_mirror_source_never_connects(tmp_path, source):
    h = Harness(tmp_path)
    assert_error(await h.start(source=source), ErrorCode.unsupported_source)
    assert h.control.calls(BackendOperation.connect) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,source", [
    ("physical_source", physical()), ("virtual_source", virtual_handle().source),
])
async def test_unaffirmed_source_capability_performs_no_connect_io(tmp_path, capability, source):
    h = Harness(tmp_path, **{capability: False})
    assert_error(await h.start(source=source), ErrorCode.unsupported_source)
    assert h.control.journal.entries == []


@pytest.mark.asyncio
async def test_verified_virtual_source_is_consumed_without_touching_its_helper(tmp_path):
    h = Harness(tmp_path)
    display = virtual_handle()
    handle = await h.handle(source=display.source)
    assert h.capture.journal.entries == []
    assert isinstance(await h.transport.stop_stream(handle, context()), Ok)
    assert display.helper == ProcessIdentity(pid=4001, start_ticks=90001)
    assert h.control.calls(BackendOperation.connect)[0]["payload"].kind == "ConnectPayload"


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_connect_records_audio_intent_and_only_separate_audio_start_enables_it(tmp_path, enabled):
    h = Harness(tmp_path)
    handle = await h.handle(enabled=enabled)
    assert h.control.audio == set()
    assert h.control.calls(BackendOperation.start_audio) == []
    connect = h.control.calls(BackendOperation.connect)[0]
    assert connect["payload"].kind == "ConnectPayload"
    result = await h.transport.start_audio(handle, context(audio_operation_id=OperationId(UUID(int=30))))
    if enabled:
        assert isinstance(result, Ok)
        assert h.control.audio == {h.a.receiver_id}
        event = next(event for event in h.events if event.kind is TransportEventKind.audio_recovered)
        assert event.stream == handle
        assert event.context.audio_operation_id == OperationId(UUID(int=30))
    else:
        assert_error(result, ErrorCode.unsupported_operation)
        assert h.control.calls(BackendOperation.start_audio) == []
        assert not any(event.kind is TransportEventKind.audio_start_failed for event in h.events)


@pytest.mark.asyncio
async def test_video_only_stream_records_disabled_audio_distinct_from_unavailable(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle(enabled=False)
    audio = h.transport._streams[handle.lease_id].audio
    assert audio is AudioState.Disabled
    assert audio is not AudioState.Unavailable


@pytest.mark.asyncio
async def test_video_only_stream_rejects_mute_without_backend_io(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle(enabled=False)
    before = list(h.control.journal.entries)
    result = await h.transport.set_muted(handle, True, context())
    assert h.control.journal.entries == before
    assert_error(result, ErrorCode.audio_unavailable)


@pytest.mark.asyncio
async def test_audio_capability_failure_preserves_video_and_disables_mute(tmp_path):
    h = Harness(tmp_path, audio_restart=False)
    handle = await h.handle()
    assert_error(await h.transport.start_audio(handle, context()), ErrorCode.audio_unavailable)
    assert_error(await h.transport.set_muted(handle, True, context()), ErrorCode.audio_unavailable)
    assert h.control.connected == {h.a.receiver_id}
    assert h.control.calls(BackendOperation.start_audio) == []
    assert h.control.calls(BackendOperation.mute) == []


@pytest.mark.asyncio
async def test_failed_audio_can_retry_on_the_same_stream_and_display_tokens(tmp_path):
    h = Harness(tmp_path)
    display = virtual_handle()
    handle = await h.handle(source=display.source)
    h.control.results[BackendOperation.start_audio].append(Err(error=diagnostic(ErrorCode.audio_denied)))
    assert_error(await h.transport.start_audio(handle, context()), ErrorCode.audio_denied)
    assert isinstance(await h.transport.start_audio(handle, context()), Ok)
    assert len(h.control.calls(BackendOperation.connect)) == 1
    assert {event.stream for event in h.events} == {handle}
    assert h.capture.journal.entries == []


@pytest.mark.asyncio
async def test_wrong_mute_ack_does_not_confirm_a_state_change(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    assert isinstance(await h.transport.start_audio(handle, context()), Ok)
    h.control.results[BackendOperation.mute].append(Ok(value=AckReply(
        operation="mute", target=h.b.canonical_host, muted=True,
    )))
    assert_error(await h.transport.set_muted(handle, True, context()), ErrorCode.audio_mute_failed)
    results = [event for event in h.events if event.kind is TransportEventKind.mute_result]
    assert results[-1].payload == MutePayload(muted=False, ok=False)
    assert isinstance(await h.transport.set_muted(handle, True, context()), Ok)
    assert h.events[-1].payload == MutePayload(muted=True, ok=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["backend_stream_id", "lease_id", "daemon_instance_uuid", "start_operation_id"])
async def test_forged_handles_cannot_control_or_release_an_owned_stream(tmp_path, field):
    h = Harness(tmp_path)
    handle = await h.handle()
    wrong = replace(handle, **{field: "foreign" if field == "backend_stream_id" else UUID(int=999)})
    before = len(h.control.journal.entries)
    assert_error(await h.transport.stop_stream(wrong, context()), ErrorCode.stale_event)
    assert_error(await h.transport.start_audio(wrong, context()), ErrorCode.stale_event)
    assert_error(await h.transport.set_muted(wrong, True, context()), ErrorCode.stale_event)
    assert len(h.control.journal.entries) == before
    assert isinstance(await h.transport.stop_stream(handle, context()), Ok)


@pytest.mark.asyncio
async def test_daemon_exit_invalidates_its_streams_without_scheduling_restart(tmp_path):
    h = Harness(tmp_path)
    a = await h.handle()
    b = await h.handle(h.b, ctx=context(lease=2))
    h.registry.emit("exit")
    notices = [event for event in h.events if event.kind is TransportEventKind.daemon_exit]
    assert {event.stream for event in notices} == {a, b}
    assert_error(await h.transport.start_audio(a, context()), ErrorCode.stale_event)
    assert h.registry.restart_calls == 0
    h.registry.emit("exit")
    assert len([event for event in h.events if event.kind is TransportEventKind.daemon_exit]) == 2


@pytest.mark.asyncio
async def test_daemon_exit_during_connect_cannot_publish_a_late_ready(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    h.registry.emit("exit")
    gate.release()
    assert_error(await pending, ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)
    assert h.registry.restart_calls == 0


@pytest.mark.asyncio
async def test_daemon_exit_during_mode_query_rejects_the_old_reply(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.query_modes, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.transport.query_modes(h.a, context(lease=None)))
    await gate.entered.wait()
    h.registry.emit("exit")
    gate.release()
    assert_error(await pending, ErrorCode.stale_event)


@pytest.mark.asyncio
async def test_old_daemon_and_ambiguous_backend_notices_cannot_change_new_streams(tmp_path):
    h = Harness(tmp_path)
    old = await h.handle()
    h.registry.emit("exit")
    h.registry.record = daemon(10)
    current = await h.handle(ctx=context(lease=2))
    count = len(h.events)
    h.registry.emit("ready", old.daemon_instance_uuid)
    h.registry.emit("exit", old.daemon_instance_uuid)
    h.control.emit(BackendNotice(kind=BackendNoticeKind.stream, target=h.a.canonical_host))
    h.factory(h.registry.record).emit(BackendNotice(kind=BackendNoticeKind.failed, target=h.b.canonical_host))
    assert len(h.events) == count
    assert isinstance(await h.transport.start_audio(current, context(lease=2)), Ok)
    assert h.factory(h.registry.record).audio == {h.a.receiver_id}


@pytest.mark.asyncio
async def test_first_old_daemon_exit_after_new_daemon_stream_preserves_current_controls(tmp_path):
    h = Harness(tmp_path)
    old = await h.handle()
    h.registry.record = daemon(10)
    current = await h.handle(h.b, ctx=context(lease=2))
    assert current.daemon_instance_uuid != old.daemon_instance_uuid
    assert isinstance(await h.transport.start_audio(current, context(lease=2)), Ok)

    # The first exit for the old daemon arrives only after the new stream is live.
    h.registry.emit("exit", old.daemon_instance_uuid)
    exited = [event.stream for event in h.events if event.kind is TransportEventKind.daemon_exit]
    assert exited == [old]
    assert isinstance(await h.transport.set_muted(current, True, context(lease=2)), Ok)
    current_control = h.factory(h.registry.record)
    assert current_control.muted == {h.b.receiver_id: True}
    assert isinstance(await h.transport.stop_stream(current, context(lease=2)), Ok)
    assert current_control.connected == set()


@pytest.mark.asyncio
async def test_target_local_connect_failure_preserves_other_receiver(tmp_path):
    h = Harness(tmp_path)
    a = await h.handle()
    h.control.results[BackendOperation.connect].append(Err(error=diagnostic(ErrorCode.receiver_rejected)))
    assert_error(await h.start(h.b, ctx=context(lease=2)), ErrorCode.receiver_rejected)
    assert h.control.connected == {h.a.receiver_id}
    assert isinstance(await h.transport.start_audio(a, context()), Ok)
    assert h.control.calls(BackendOperation.disconnect) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [
    AckReply(operation="disconnect", target="192.0.2.10"),
    AckReply(operation="connect", target="192.0.2.11"),
])
async def test_wrong_connect_reply_never_becomes_ready(tmp_path, reply):
    h = Harness(tmp_path)
    h.control.results[BackendOperation.connect].append(Ok(value=reply))
    result = await h.start()
    assert isinstance(result, Err)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_unknown_pre_stream_modes_are_none_without_inventing_sizes(tmp_path, supported):
    h = Harness(tmp_path, query_modes=supported)
    result = await h.transport.query_modes(h.a, context(lease=None))
    assert isinstance(result, Ok)
    assert result.value is None
    assert len(h.control.calls(BackendOperation.query_modes)) == int(supported)


@pytest.mark.asyncio
async def test_overlapping_pairing_is_authorized_once_per_receiver(tmp_path):
    h = Harness(tmp_path)
    ctx = context(lease=None)
    a, b = await asyncio.gather(
        h.transport.begin_pairing(h.a, ctx), h.transport.begin_pairing(h.b, ctx),
    )
    assert isinstance(a, Ok) and isinstance(b, Ok)
    assert a.value.challenge_id != b.value.challenge_id
    duplicate = await h.transport.begin_pairing(h.a, ctx)
    assert isinstance(duplicate, Err)
    results = await asyncio.gather(
        h.transport.submit_pairing(a.value.challenge_id, SensitiveText("8496"), ctx),
        h.transport.submit_pairing(b.value.challenge_id, SensitiveText("1537"), ctx),
    )
    assert [result.kind for result in results] == ["Trusted", "Trusted"]
    assert len(h.control.calls(BackendOperation.begin_pairing)) == 2
    assert len(h.control.calls(BackendOperation.submit_pairing)) == 2
    assert len(h.control.calls(BackendOperation.commit_pairing)) == 2
    assert "8496" not in repr(h.control.journal)
    assert "1537" not in repr(h.events)


@pytest.mark.asyncio
async def test_replacement_retires_active_pairing_before_input_can_reach_backend(tmp_path):
    h = Harness(tmp_path)
    ctx = context(lease=None)
    begun = await h.transport.begin_pairing(h.a, ctx)
    assert isinstance(begun, Ok)
    h.transport.update_target(target("replacement"), target("replacement").receiver_id)
    result = await h.transport.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), ctx)
    assert result.kind != "Trusted"
    assert h.control.calls(BackendOperation.submit_pairing) == []
    assert h.control.calls(BackendOperation.cancel_pairing) == []
    assert h.control.calls(BackendOperation.revoke_pairing) == []


@pytest.mark.asyncio
async def test_trust_cleanup_blocks_stream_start_and_forget_requires_actual_stop(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    # The caller cannot evade the coordinator's lease guard by omitting it.
    assert_error(await h.transport.forget_trust(h.a.receiver_id, context(lease=None)), ErrorCode.invalid_input)
    assert isinstance(await h.transport.stop_stream(handle, context()), Ok)
    ch = await h.transport.begin_pairing(h.a, context(lease=None))
    assert isinstance(ch, Ok)
    h.control.results[BackendOperation.cancel_pairing].append(Err(error=diagnostic(ErrorCode.keyring_unavailable)))
    assert isinstance(await h.transport.cancel_pairing(h.a.receiver_id, ch.value.challenge_id, context(lease=None)), Err)
    before = len(h.control.calls(BackendOperation.connect))
    assert_error(await h.start(), ErrorCode.trust_cleanup_required)
    assert len(h.control.calls(BackendOperation.connect)) == before
    assert isinstance(await h.transport.retry_revocation(h.a.receiver_id, context(lease=None)), Ok)


@pytest.mark.asyncio
async def test_late_connect_success_after_endpoint_change_stays_cleanup_uncertain(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    h.transport.update_target(target("replacement"), target("replacement").receiver_id)
    gate.release()
    assert_error(await pending, ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    uncertain = [event for event in h.events if event.kind is TransportEventKind.failed]
    assert uncertain[-1].stream is not None
    assert h.control.calls(BackendOperation.disconnect) == []


@pytest.mark.asyncio
async def test_cancelled_connect_waiter_still_releases_late_success(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    gate.release()
    for _ in range(40):
        if h.control.calls(BackendOperation.disconnect):
            break
        await asyncio.sleep(0)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.control.connected == set()
    assert not any(event.kind is TransportEventKind.ready for event in h.events)


@pytest.mark.asyncio
async def test_cancelled_waiter_after_ready_still_releases_the_completed_acquisition(tmp_path):
    h = Harness(tmp_path)
    pending = asyncio.create_task(h.start())

    def cancel_on_ready(event):
        if event.kind is TransportEventKind.ready:
            pending.cancel()

    h.transport.subscribe(cancel_on_ready)
    with pytest.raises(asyncio.CancelledError):
        await pending
    for _ in range(40):
        if h.control.calls(BackendOperation.disconnect):
            break
        await asyncio.sleep(0)
    assert len(h.control.calls(BackendOperation.disconnect)) == 1
    assert h.control.connected == set()
    assert h.events[-1].kind is TransportEventKind.stopped


@pytest.mark.asyncio
async def test_connect_timeout_keeps_the_late_acquisition_observed(tmp_path):
    h = Harness(tmp_path)
    gate = h.control.gates[(BackendOperation.connect, h.a.receiver_id)] = Gate()
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    h.scheduler.advance(5)
    assert_error(await pending, ErrorCode.transport_failed)
    gate.release()
    for _ in range(40):
        if h.control.calls(BackendOperation.disconnect):
            break
        await asyncio.sleep(0)
    assert h.control.connected == set()
    assert len(h.control.calls(BackendOperation.disconnect)) == 1


@pytest.mark.asyncio
async def test_shutdown_confirms_daemon_release_and_closes_subscriptions(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    assert isinstance(await h.transport.shutdown(), Ok)
    assert h.control.connected == set()
    assert h.control.closed
    count = len(h.events)
    h.registry.emit("exit", handle.daemon_instance_uuid)
    assert len(h.events) == count
    assert isinstance(await h.start(), Err)


@pytest.mark.asyncio
async def test_shutdown_does_not_claim_cleanup_when_registry_cannot_confirm_it(tmp_path):
    h = Harness(tmp_path)
    await h.handle()
    h.transport.update_target(target("replacement"), target("replacement").receiver_id)
    h.registry.shutdown_result = Err(error=diagnostic(ErrorCode.cleanup_timeout))
    assert_error(await h.transport.shutdown(), ErrorCode.cleanup_timeout)
    assert not any(event.kind is TransportEventKind.stopped for event in h.events)


@pytest.mark.asyncio
async def test_expired_challenge_allows_fresh_pairing_after_confirmed_cleanup(tmp_path):
    h = Harness(tmp_path)
    first = await h.transport.begin_pairing(h.a, context(lease=None))
    assert isinstance(first, Ok)
    h.scheduler.advance(120)
    for _ in range(40):
        if h.control.calls(BackendOperation.cancel_pairing):
            break
        await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    fence = h.fences.get(h.a.receiver_id)
    fresh_context = context(lease=None, trust_generation=fence.trust_generation)
    second = await h.transport.begin_pairing(h.a, fresh_context)
    assert isinstance(second, Ok), second
    assert second.value.challenge_id != first.value.challenge_id
    assert isinstance(await h.transport.cancel_pairing(h.a.receiver_id, second.value.challenge_id, fresh_context), Ok)


@pytest.mark.asyncio
async def test_pairing_can_follow_a_committed_receiver_to_a_new_endpoint(tmp_path):
    h = Harness(tmp_path)
    ctx = context(lease=None)
    begun = await h.transport.begin_pairing(h.a, ctx)
    assert isinstance(begun, Ok)
    assert (await h.transport.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), ctx)).kind == "Trusted"
    fresh = target(endpoint_generation=2, address="192.0.2.77")
    h.transport.update_target(fresh, h.a.receiver_id)
    new_context = context(lease=None, endpoint_generation=2)
    next_pairing = await h.transport.begin_pairing(fresh, new_context)
    assert isinstance(next_pairing, Ok), next_pairing
    assert (await h.transport.submit_pairing(next_pairing.value.challenge_id, SensitiveText("1537"), new_context)).kind == "Trusted"
    assert h.control.calls(BackendOperation.commit_pairing)[-1]["target"]["canonical_host"] == "192.0.2.77"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [BackendOperation.submit_pairing, BackendOperation.prepare_pairing, BackendOperation.commit_pairing])
@pytest.mark.parametrize("invalidation", ["cancel", "invalid", "endpoint", "replacement", "daemon"])
async def test_transport_and_real_keyring_keep_late_pairing_behind_compensation(tmp_path, stage, invalidation):
    h = Harness(tmp_path)
    ctx = context(lease=None)
    begun = await h.transport.begin_pairing(h.a, ctx)
    assert isinstance(begun, Ok)
    gate = h.control.gates[(stage, h.a.receiver_id)] = Gate()
    submitting = asyncio.create_task(h.transport.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), ctx))
    await gate.entered.wait()
    cleanup = None
    if invalidation in ("cancel", "invalid"):
        operation = h.transport.cancel_pairing if invalidation == "cancel" else h.transport.mark_invalid
        args = (h.a.receiver_id, begun.value.challenge_id, ctx) if invalidation == "cancel" else (h.a.receiver_id, ctx)
        cleanup = asyncio.create_task(operation(*args))
    elif invalidation == "endpoint":
        h.transport.update_target(target(endpoint_generation=2, address="192.0.2.77"), h.a.receiver_id)
    elif invalidation == "replacement":
        replacement = target("replacement")
        h.admit(replacement)
    else:
        h.registry.emit("exit")
    for _ in range(20):
        await asyncio.sleep(0)
    assert h.fences.get(h.a.receiver_id).active_challenge_id is None
    assert h.control.calls(BackendOperation.revoke_pairing) == []
    gate.release()
    result = await submitting
    assert result.kind != "Trusted"
    assert result.credential_ref is None
    if cleanup is not None:
        assert isinstance(await cleanup, Ok)
    if invalidation in ("replacement", "daemon"):
        assert result.kind == "TrustCleanupRequired"
        assert h.control.calls(BackendOperation.cancel_pairing) == []
        assert h.control.calls(BackendOperation.revoke_pairing) == []
    else:
        operations = [call["operation"] for _, call in h.control.journal.entries]
        assert operations[-1] is BackendOperation.revoke_pairing
        assert operations.index(stage) < len(operations) - 1
        assert (await h.credentials.load_credential_ref(h.a.receiver_id)).value is None
    assert list(tmp_path.rglob("*.json")) == []


@pytest.mark.asyncio
async def test_unconfirmed_old_trust_blocks_replacement_pairing_and_start_at_its_address(tmp_path):
    h = Harness(tmp_path)
    begun = await h.transport.begin_pairing(h.a, context(lease=None))
    assert isinstance(begun, Ok)
    replacement = target("replacement")
    h.admit(replacement)
    result = await h.transport.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), context(lease=None))
    assert result.kind == "TrustCleanupRequired"
    assert_error(await h.start(replacement, ctx=context(lease=2)), ErrorCode.trust_cleanup_required)
    assert_error(await h.transport.begin_pairing(replacement, context(lease=None)), ErrorCode.trust_cleanup_required)
    assert h.control.calls(BackendOperation.connect) == []
    assert len(h.control.calls(BackendOperation.begin_pairing)) == 1


@pytest.mark.asyncio
async def test_pending_trust_cleanup_for_a_does_not_serialize_pairing_for_b(tmp_path):
    h = Harness(tmp_path)
    ctx = context(lease=None)
    first = await h.transport.begin_pairing(h.a, ctx)
    gate = h.control.gates[(BackendOperation.cancel_pairing, h.a.receiver_id)] = Gate()
    cancelling = asyncio.create_task(h.transport.cancel_pairing(h.a.receiver_id, first.value.challenge_id, ctx))
    await gate.entered.wait()
    second = await h.transport.begin_pairing(h.b, ctx)
    assert isinstance(second, Ok)
    assert (await h.transport.submit_pairing(second.value.challenge_id, SensitiveText("1537"), ctx)).kind == "Trusted"
    assert not cancelling.done()
    gate.release()
    assert isinstance(await cancelling, Ok)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["target_bound_pairing", "keyring", "provisional_commit", "cancel_pairing", "revoke_pairing"])
async def test_unaffirmed_pairing_capability_has_no_backend_side_effect(tmp_path, missing):
    h = Harness(tmp_path, **{missing: False})
    assert_error(await h.transport.begin_pairing(h.a, context(lease=None)), ErrorCode.keyring_unavailable)
    assert h.control.journal.entries == []
    assert h.registry.ensure_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        ("connect", ErrorCode.cleanup_timeout),
        ("disconnect", ErrorCode.cleanup_timeout),
        ("mute", ErrorCode.stale_target),
    ],
)
async def test_replacement_during_ownership_revalidation_never_dispatches_a_mutation(
    tmp_path, operation, expected,
):
    """F-1: the process journal, not a post-reply check, must prove no dispatch."""
    h = ContractHarness(tmp_path)
    if operation == "connect":
        # The shared daemon handshake completes before the gated revalidation.
        await h.handle(h.b, ctx=context(lease=2))
        before = h.process.mutations()
        gate = h.process.gate_next_status()
        pending = asyncio.create_task(h.start(h.a))
    else:
        handle = await h.handle()
        if operation == "mute":
            assert isinstance(await h.transport.start_audio(handle, context()), Ok)
        before = h.process.mutations()
        gate = h.process.gate_next_status()
        pending = asyncio.create_task(
            h.transport.stop_stream(handle, context())
            if operation == "disconnect"
            else h.transport.set_muted(handle, True, context())
        )
    await gate.entered.wait()
    replacement = target("replacement")
    h.transport.update_target(replacement, replacement.receiver_id)
    gate.release()
    assert_error(await pending, expected)
    # Nothing new reached the process, so the replacement cannot be touched.
    assert h.process.mutations() == before
    assert [
        argv for argv in h.process.commands(operation)
        if len(argv) > 4 and argv[4] == h.a.canonical_host
    ] == []


@pytest.mark.asyncio
async def test_daemon_exit_before_admission_is_remembered_without_connect_io(tmp_path):
    h = Harness(tmp_path)
    gate = h.registry.gate = Gate()
    pending = asyncio.create_task(h.start())
    await gate.entered.wait()
    h.registry.emit("exit")
    gate.release()
    assert_error(await pending, ErrorCode.daemon_failed)
    assert h.control.calls(BackendOperation.connect) == []
    assert not any(event.kind is TransportEventKind.ready for event in h.events)


@pytest.mark.asyncio
async def test_unaffirmed_multi_target_capability_rejects_start_without_backend_io(tmp_path):
    h = Harness(tmp_path, multi_target=False)
    assert_error(await h.start(), ErrorCode.unsupported_operation)
    assert h.control.journal.entries == []


@pytest.mark.asyncio
async def test_contradictory_mute_ack_cannot_promote_the_requested_state(tmp_path):
    h = Harness(tmp_path)
    handle = await h.handle()
    assert isinstance(await h.transport.start_audio(handle, context()), Ok)
    h.control.results[BackendOperation.mute].append(Ok(value=AckReply(
        operation="mute", target=h.a.canonical_host, muted=False,
    )))
    assert_error(await h.transport.set_muted(handle, True, context()), ErrorCode.audio_mute_failed)
    assert h.events[-1].payload == MutePayload(muted=False, ok=False)
    assert h.control.muted == {}
    assert isinstance(await h.transport.set_muted(handle, True, context()), Ok)
    assert h.events[-1].payload == MutePayload(muted=True, ok=True)


def test_fence_compatibility_matches_actual_keyword_callability():
    """F-7/F-8: the helper must agree with whether the call really works.

    Enumerated rather than hand-listed: the previous two rounds each fixed one
    parameter kind and broke another, so the domain is checked wholesale. Every
    shape is built as a real method and probed with an actual
    ``request(..., dispatch_fence=...)`` call, including the mixed cases where a
    same-named positional-only or ``*args`` parameter coexists with ``**kwargs``.
    """
    from airplay_linux.transport.doubletake import _accepts_dispatch_fence

    def build(params: str):
        source = (
            "class Probe:\n"
            f"    def request(self, operation, target, payload, context{params}):\n"
            "        return True\n"
        )
        namespace: dict = {}
        exec(source, namespace)  # noqa: S102 - fixed source, no user input
        return namespace["Probe"]()

    def callable_with(instance) -> bool:
        try:
            instance.request(1, 2, 3, 4, dispatch_fence=lambda: None)
        except TypeError:
            return False
        return True

    fence_forms = [
        "",
        ", dispatch_fence=None",
        ", *, dispatch_fence=None",
        ", dispatch_fence=None, /",
        ", **dispatch_fence",
        ", *dispatch_fence",
    ]
    catchalls = ["", ", **kwargs"]
    extras = ["", ", *extra"]

    checked = 0
    for form in fence_forms:
        for catchall in catchalls:
            for extra in extras:
                # a positional-only marker must precede *args; skip invalid orders
                if "/" in form and extra:
                    continue
                params = form + extra + catchall
                try:
                    instance = build(params)
                except SyntaxError:
                    continue
                expected = callable_with(instance)
                actual = _accepts_dispatch_fence(instance)
                assert actual is expected, (
                    f"shape {params!r}: helper={actual} real call works={expected}"
                )
                checked += 1
    assert checked >= 12, checked
