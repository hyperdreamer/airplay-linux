"""Acceptance probes for the activated owned-daemon composition."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from airplay_linux import runtime as composition
from airplay_linux.backend.contract import (
    BackendFeatures,
    BackendSource,
    CredentialProofVerdict,
)
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    EmptyPayload,
    Handshake,
    MutePayload,
    OwnershipProof,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import TransportEventKind
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.ownership import EvidenceAuthority
from airplay_linux.process.procfs import TerminationEvidence
from airplay_linux.runtime import RuntimeConfig, build_runtime
from airplay_linux.testing.builders import context, target
from tests.acceptance.test_no_production_spawn import ProcessRecorder, offline_boundaries
from tests.process.test_ownership import EndpointEvidence
from tests.transport.test_doubletake import (
    Harness as TransportHarness,
    SourceVerifier,
    _source_status,
)


class _StatusProcess:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.calls = []

    async def run(self, spec, timeout, stdin=None):
        self.calls.append(spec)
        return Ok(value=CommandOutput(exit_code=0, stdout=self.payload, stderr=b""))

    async def spawn(self, spec):
        self.calls.append(spec)
        return Err(error=diagnostic(ErrorCode.dependency_incompatible))

    def identity(self, pid):
        return None

    def owned(self, identity, owner_uuid, runtime_directory):
        return False

    async def terminate(self, *args, **kwargs):
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    def subscribe(self, callback):
        return SimpleNamespace(close=lambda: None)


class _PublicAuthority:
    def __init__(self, record, proof):
        self.record = record
        self.proof = proof
        self.prove_calls = 0
        self.confirm_calls = 0

    def allows(self, operation):
        return operation in {BackendOperation.status, BackendOperation.connect}

    async def prove(self, record):
        self.prove_calls += 1
        return Ok(value=self.proof) if record == self.record else Err(
            error=diagnostic(ErrorCode.ownership_mismatch),
        )

    async def authorize(self, record, operation, target, fence):
        return await self.prove(record)

    async def confirm(self, record, proof):
        self.confirm_calls += 1
        return Ok(value=None) if proof == self.proof else Err(
            error=diagnostic(ErrorCode.stale_event),
        )

    async def release(self, record):
        return Ok(value=None)


class _ChangingAuthority(_PublicAuthority):
    def __init__(self, record, authorized, current):
        super().__init__(record, authorized)
        self.authorized = authorized
        self.current = current

    def allows(self, operation):
        return operation in {
            BackendOperation.status,
            BackendOperation.connect,
            BackendOperation.disconnect,
            BackendOperation.mute,
            BackendOperation.unmute,
        }

    async def prove(self, record):
        self.prove_calls += 1
        return Ok(value=self.current) if record == self.record else Err(
            error=diagnostic(ErrorCode.ownership_mismatch),
        )

    async def authorize(self, record, operation, target, fence):
        return Ok(value=self.authorized) if record == self.record else Err(
            error=diagnostic(ErrorCode.ownership_mismatch),
        )


def _owned_features():
    return BackendFeatures(
        profile="doubletake-0.5+source", version="0.5.0",
        foreground_control=False, owner_handshake=False, multi_target=True,
        target_bound_pairing=False, physical_source=True, virtual_source=False,
        audio_probe=True, audio_restart=False, target_mute=True, keyring=True,
        provisional_commit=False, cancel_pairing=False, revoke_pairing=False,
        query_modes=False, source_reporting=True, owned_daemon_control=True,
        source_reporting_complete=True,
    )


def _record():
    return DaemonRecord(
        schema_version=1, instance_uuid=InstanceId(UUID(int=9)), owner_token="unused",
        app=ProcessIdentity(pid=100, start_ticks=1),
        supervisor=ProcessIdentity(pid=200, start_ticks=2),
        daemon=ProcessIdentity(pid=300, start_ticks=3), protocol_version="v1",
        socket_path="/run/private/daemon.sock", lifecycle="running",
        runtime_directory="/run/private",
    )


def _status_payload():
    return {
        "ok": True, "state": "idle", "has_audio": False, "audio_muted": False,
        "device": None, "device_ip": None, "streams": [],
    }


@pytest.mark.asyncio
async def test_public_shape_complete_graph_uses_evidence_authority(
    tmp_path, monkeypatch,
):
    recorder = ProcessRecorder()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    config = RuntimeConfig(state_root=tmp_path / "state", runtime_root=runtime_root, live=False)

    async def complete_source_probe(process):
        from airplay_linux.backend.contract import probe_backend
        result = await probe_backend(process)
        assert isinstance(result, Ok)
        return Ok(value=replace(result.value, source_reporting_complete=True))

    async def passed_proof():
        return Ok(value=CredentialProofVerdict(
            service_leg="passed", backend_positive_leg="passed",
            backend_negative_leg="passed", failure=None,
        ))

    original_installation = composition._install_owned_daemon_epoch

    def installation(config, *, process, launcher):
        return replace(
            original_installation(config, process=process, launcher=launcher),
            credential_proof=passed_proof,
        )

    async def environment(_probe):
        from airplay_linux.domain.resources import CapabilityReport
        return CapabilityReport(
            environment="kde-plasma-6-wayland", discovery=True, mirror=True,
            virtual_display=False, audio=True, remembered_pairing=False,
            additional_virtual_outputs=False, diagnostics=(),
        )

    async def nothing():
        return Ok(value=None)

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    monkeypatch.setattr(composition, "probe_backend", complete_source_probe)
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: "/fake/unshare")
    monkeypatch.setattr(composition.EnvironmentProbe, "check", environment)
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace(close=nothing))
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", lambda: SimpleNamespace(check=nothing))

    result = await build_runtime(config, overrides={"process": recorder})
    assert isinstance(result, Ok), result
    runtime = result.value
    try:
        assert runtime.registry._public_seams_complete is True
        assert isinstance(runtime.registry._ownership, EvidenceAuthority)
        assert runtime.registry._receipts is not None
        assert runtime.registry._launcher is not None
        assert runtime.deps.transport._features.owned_daemon_control is True
        detail = runtime.gated_capabilities["owned_daemon_startup"]
        for fact in (
            "authority=EvidenceAuthority", "credential-service=passed",
            "credential-positive=passed", "credential-negative=passed",
            "source-verifier=wired", "source-complete=True", "physical-inventory=True",
        ):
            assert fact in detail
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("source_state", ["complete", "incomplete", "no_inventory"])
async def test_startup_reports_authority_source_and_withheld_public_flags(
    tmp_path, monkeypatch, offline_boundaries, source_state,
):
    recorder = ProcessRecorder()
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    install = composition._install_owned_daemon_epoch
    probe = composition.probe_backend

    async def probed(process):
        result = await probe(process)
        assert isinstance(result, Ok)
        return Ok(value=replace(result.value, source_reporting_complete=source_state != "incomplete"))

    async def failed_witness():
        return Ok(value=CredentialProofVerdict(
            service_leg="not_run", backend_positive_leg="not_run", backend_negative_leg="not_run", failure=None,
        ))

    def installation(config, *, process, launcher):
        return replace(
            install(config, process=process, launcher=launcher), credential_proof=failed_witness,
            source_verifier=SimpleNamespace(physical_inventory_available=source_state != "no_inventory"),
        )

    monkeypatch.setattr(composition, "probe_backend", probed)
    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: "/fake/unshare")
    built = await build_runtime(RuntimeConfig(state_root=tmp_path / "state", runtime_root=root, live=False),
                                overrides={"process": recorder})
    assert isinstance(built, Ok)
    runtime = built.value
    try:
        gates = runtime.gated_capabilities
        assert gates["ownership_authority"] == "EvidenceAuthority: credential service leg"
        detail = gates["owned_daemon_startup"]
        assert "authority=EvidenceAuthority" in detail
        assert "source-verifier=wired" in detail
        assert f"source-complete={source_state != 'incomplete'}" in detail
        assert f"physical-inventory={source_state != 'no_inventory'}" in detail
        for leg in ("service", "positive", "negative"):
            assert f"credential-{leg}=not_run" in detail
        for leg in ("service_leg", "backend_positive_leg", "backend_negative_leg"):
            assert gates["credential_" + leg] == "not_run"
        for flag in ("foreground_control", "owner_handshake", "target_bound_pairing", "provisional_commit",
                     "cancel_pairing", "revoke_pairing", "audio_restart", "query_modes"):
            assert gates[flag] == "not affirmed by the public backend"
        assert ("source_reporting_complete" in gates) is (source_state == "incomplete")
        assert ("physical_inventory" in gates) is (source_state == "no_inventory")
        refused = await runtime.registry.ensure()
        assert isinstance(refused, Err) and refused.error.code is ErrorCode.dependency_incompatible
    finally:
        await runtime.shutdown()
    assert not recorder.events("outer_namespace")
    assert not any(event.method == "spawn" for event in recorder.observations)


@pytest.mark.parametrize("operation", [
    BackendOperation.status, BackendOperation.discover, BackendOperation.devices,
])
@pytest.mark.parametrize("boundary", [
    "current", "record_absent", "authority_absent", "changed_during_io", "changed_during_refusal",
])
async def test_public_readonly_replies_require_current_authority(operation, boundary):
    record = _record()
    proof = OwnershipProof(
        kind="evidence", daemon=record.daemon, socket_entry=(1, 2),
        generation=1, handshake=None,
    )
    authority = _PublicAuthority(record, proof)

    class ReadProcess(_StatusProcess):
        async def run(self, spec, timeout, stdin=None):
            result = await super().run(spec, timeout, stdin)
            if boundary.startswith("changed_during_"):
                authority.proof = replace(proof, generation=2)
            return result

    payload = _status_payload() if operation is BackendOperation.status else {"ok": True, "devices": []}
    if boundary == "changed_during_refusal":
        payload = {"ok": False, "state": "idle"}
    process = ReadProcess(payload)
    control = PublishedDoubletakeControl(
        process, PublishedDoubletakeProfile(socket_path=record.socket_path, features=_owned_features()),
        record=None if boundary == "record_absent" else record,
        authority=None if boundary == "authority_absent" else authority,
    )

    result = await control.request(operation, None, EmptyPayload(), None)

    if boundary in {"record_absent", "authority_absent"}:
        assert isinstance(result, Err) and result.error.code is ErrorCode.dependency_incompatible
        assert process.calls == []
        assert authority.prove_calls == authority.confirm_calls == 0
    else:
        assert len(process.calls) == 1
        assert authority.prove_calls == authority.confirm_calls == 1
        if boundary == "current":
            assert isinstance(result, Ok), result
        else:
            assert isinstance(result, Err) and result.error.code is ErrorCode.stale_event


@pytest.mark.asyncio
async def test_public_status_without_legacy_metadata_is_accepted():
    record = _record()
    proof = OwnershipProof(
        kind="evidence", daemon=record.daemon, socket_entry=(1, 2),
        generation=1, handshake=None,
    )
    authority = _PublicAuthority(record, proof)
    control = PublishedDoubletakeControl(
        _StatusProcess(_status_payload()),
        PublishedDoubletakeProfile(
            socket_path=record.socket_path, name="doubletake-0.5+source",
            version="0.5.0", features=_owned_features(),
        ),
        record=record, authority=authority,
    )

    result = await control.request(BackendOperation.status, None, EmptyPayload(), None)

    assert isinstance(result, Ok), result
    assert authority.prove_calls >= 1
    assert authority.confirm_calls == 1


@pytest.mark.asyncio
async def test_public_shape_target_controls_and_whole_shutdown_are_distinct():
    e = EndpointEvidence()

    class TargetProcess(_StatusProcess):
        async def run(self, spec, timeout, stdin=None):
            self.calls.append(spec)
            operation = spec.argv[3]
            payload = _status_payload() if operation == "status" else {
                **_status_payload(), "state": "streaming", "device": "Receiver",
                "device_ip": target().canonical_host, "has_audio": True,
                "audio_muted": operation == "mute",
                "streams": [{
                    "device": "Receiver", "device_ip": target().canonical_host, "state": "streaming",
                    "has_audio": True, "audio_muted": operation == "mute",
                }],
            }
            return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps(payload).encode(), stderr=b""))

    authority = EvidenceAuthority(
        receipts=e.source, process=e, observations=e,
        evidence_factory=TerminationEvidence, features=_owned_features(),
    )
    process = TargetProcess(_status_payload())
    control = PublishedDoubletakeControl(
        process, PublishedDoubletakeProfile(
            socket_path=e.record.socket_path, features=_owned_features(),
        ), record=e.record, authority=authority,
    )
    proof = await control.handshake("unused", e.record.instance_uuid)
    assert isinstance(proof, Ok) and proof.value.kind == "evidence"
    for operation in (
        BackendOperation.connect, BackendOperation.disconnect, BackendOperation.mute, BackendOperation.unmute,
    ):
        payload = MutePayload(muted=operation is BackendOperation.mute) if operation in {
            BackendOperation.mute, BackendOperation.unmute,
        } else EmptyPayload()
        sent = await control.request(
            operation, target(), payload, context(),
            dispatch_fence=lambda: Ok(value=None), ownership_proof=proof.value,
        )
        assert isinstance(sent, Ok), (operation, sent)
    assert [call.argv[3] for call in process.calls if call.argv[3] != "status"] == [
        "connect", "disconnect", "mute", "unmute",
    ]
    whole = await control.request(
        BackendOperation.shutdown, None, EmptyPayload(), None, ownership_proof=proof.value,
    )
    assert isinstance(whole, Err) and whole.error.code is ErrorCode.unsupported_operation
    assert all(call.argv[3] != "shutdown" for call in process.calls)
    e.receipts = (replace(e.receipt, state="stopping"),)
    assert await authority.release(e.record) == Ok(value=None)
    assert e.signal_calls == [e.record.daemon]
    assert len(e.termination_calls) == 1
    assert e.launch_calls == []
    assert e.opened == {}


def _evidence_control(e, authority=None):
    process = _StatusProcess(_status_payload())
    authority = authority or EvidenceAuthority(
        receipts=e.source, process=e, observations=e,
        evidence_factory=TerminationEvidence, features=_owned_features(),
    )
    control = PublishedDoubletakeControl(
        process, PublishedDoubletakeProfile(
            socket_path=e.record.socket_path, name="doubletake-0.5+source",
            version="0.5.0", features=_owned_features(),
        ), record=e.record, authority=authority,
    )
    return control, process


@pytest.mark.parametrize("operation", [
    BackendOperation.connect, BackendOperation.disconnect,
    BackendOperation.mute, BackendOperation.unmute,
])
async def test_changed_current_receipt_blocks_every_mutation_before_io(operation):
    e = EndpointEvidence()

    class ReplacingAuthority(EvidenceAuthority):
        async def authorize(self, record, operation, receiver, fence):
            authorized = await super().authorize(record, operation, receiver, fence)
            assert isinstance(authorized, Ok)
            # Change only the independently read receipt source after authorization.
            e.receipts = (replace(e.receipt, endpoint_generation=8),)
            return authorized

    authority = ReplacingAuthority(
        receipts=e.source, process=e, observations=e,
        evidence_factory=TerminationEvidence, features=_owned_features(),
    )
    control, process = _evidence_control(e, authority)
    payload = MutePayload(muted=operation is BackendOperation.mute) if operation in {
        BackendOperation.mute, BackendOperation.unmute,
    } else EmptyPayload()
    result = await control.request(
        operation, target(), payload, context(), dispatch_fence=lambda: Ok(value=None),
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.stale_event
    assert process.calls == []
    assert e.opened == {}


@pytest.mark.parametrize("when", ["missing", "authorization", "dispatch"])
async def test_wrong_fence_refuses_before_io(when):
    e = EndpointEvidence()
    control, process = _evidence_control(e)
    calls = []

    def fence():
        calls.append(True)
        if when == "authorization" or len(calls) == 2:
            return Err(error=diagnostic(ErrorCode.stale_event))
        return Ok(value=None)

    result = await control.request(
        BackendOperation.connect, target(), EmptyPayload(), context(),
        dispatch_fence=None if when == "missing" else fence,
    )
    assert isinstance(result, Err)
    assert result.error.code is (
        ErrorCode.dependency_incompatible if when == "missing" else ErrorCode.stale_event
    )
    assert len(calls) == {"missing": 0, "authorization": 1, "dispatch": 2}[when]
    assert process.calls == []
    assert e.opened == {}


@pytest.mark.parametrize("challenge_at", ["ack", "status"])
async def test_connect_challenge_disconnects_only_exact_target(tmp_path, challenge_at):
    verifier = SourceVerifier()
    h = TransportHarness(
        tmp_path, source_verifier=verifier,
        profile="doubletake-0.5+source", foreground_control=False,
        owner_handshake=False, owned_daemon_control=True, keyring=True,
        physical_source=True, source_reporting_complete=True,
        target_bound_pairing=False, cancel_pairing=False, provisional_commit=False,
        revoke_pairing=False,
    )
    source = BackendSource(type=1, width=1920, height=1080, x=0, y=0)
    h.control.results[BackendOperation.status].append(_source_status(h.b, source))
    other = await h.handle(h.b, ctx=context(lease=2))
    h.events.clear()
    verifier.calls.clear()
    if challenge_at == "ack":
        from airplay_linux.backend.contract import AckReply
        h.control.results[BackendOperation.connect].append(Ok(value=AckReply(
            operation="connect", target=h.a.canonical_host, state="pin_required",
        )))
    else:
        reported = _source_status(h.a, source).value
        h.control.results[BackendOperation.status].append(Ok(value=replace(
            reported, state="pin_required", needs_credential=True, credential_kind="pin",
            streams=(replace(reported.streams[0], state="pin_required", credential_kind="pin"),),
        )))
    result = await h.start()
    assert isinstance(result, Err) and result.error.code is ErrorCode.dependency_incompatible
    disconnects = h.control.calls(BackendOperation.disconnect)
    assert len(disconnects) == 1
    assert disconnects[0]["target"]["receiver_id"] == h.a.receiver_id
    assert h.transport._streams[other.lease_id].handle == other
    assert h.control.connected == {h.b.receiver_id}
    assert not any(event.kind is TransportEventKind.ready for event in h.events)
    assert {call["operation"] for call in h.control.journal.calls("request")} <= {
        BackendOperation.connect, BackendOperation.status, BackendOperation.disconnect,
    }
    assert verifier.calls == []


@pytest.mark.parametrize(("member", "label"), [
    ("owned_daemon_control", "owned daemon control"),
    ("receipts", "receipt production"),
    ("endpoint_verifier", "endpoint verifier"),
    ("strict_teardown", "strict teardown"),
    ("shared_authority", "shared authority"),
    ("ownership", "ownership authority"),
    ("credential_proof", "credential proof producer"),
    ("source_verifier", "source verifier"),
    ("unshare", "unshare"),
])
async def test_each_missing_composition_prerequisite_is_named(
    tmp_path, monkeypatch, offline_boundaries, member, label,
):
    recorder = ProcessRecorder()
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    original = composition._install_owned_daemon_epoch

    def install(config, *, process, launcher):
        installed = original(config, process=process, launcher=launcher)
        if member == "owned_daemon_control":
            monkeypatch.setattr(type(launcher), "available", lambda self: False)
            return installed
        if member == "unshare":
            return installed
        value = (lambda *args: None) if member == "strict_teardown" else None
        return replace(installed, **{member: value})

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", install)
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: None if member == "unshare" else "/fake/unshare")
    result = await build_runtime(
        RuntimeConfig(state_root=tmp_path / "state", runtime_root=root, live=False),
        overrides={"process": recorder},
    )
    assert isinstance(result, Ok)
    runtime = result.value
    try:
        assert runtime.gated["owned_daemon_control"] == label
        refused = await runtime.registry.ensure()
        assert isinstance(refused, Err) and refused.error.code is ErrorCode.dependency_incompatible
    finally:
        await runtime.shutdown()
    assert recorder.events("outer_namespace") == []
    assert recorder.events("direct_backend") == []
    assert not any(event.method == "spawn" for event in recorder.observations)


async def test_legacy_fixture_selects_handshake_authority():
    from airplay_linux.backend.control import HandshakeAuthority
    e = EndpointEvidence()
    payload = {
        **_status_payload(), "owner_token": e.record.owner_token,
        "instance_uuid": str(e.record.instance_uuid), "socket_path": e.record.socket_path,
        "protocol_version": e.record.protocol_version,
        "daemon_pid": e.record.daemon.pid, "daemon_start_ticks": e.record.daemon.start_ticks,
    }
    process = _StatusProcess(payload)
    control = PublishedDoubletakeControl(process, ApplicationContractProfile(
        socket_path=e.record.socket_path, owner_token=e.record.owner_token,
        instance_uuid=e.record.instance_uuid, protocol_version=e.record.protocol_version,
    ))
    authority = HandshakeAuthority(
        control, socket_entry=lambda _: Ok(value=(4, 60)), generation=lambda _: 7,
    )
    proof = await authority.prove(e.record)
    assert isinstance(proof, Ok) and proof.value.kind == "handshake"
    assert isinstance(proof.value.handshake, Handshake)
    assert proof.value.handshake.owner_token == e.record.owner_token
    process.payload = json.dumps(_status_payload()).encode()
    refused = await authority.prove(e.record)
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.ownership_mismatch


async def test_crash_restart_requires_reconciliation_and_new_generation(tmp_path, monkeypatch):
    from airplay_linux.testing.adapters import SequenceIds
    from tests.process import test_owned_launcher as identities
    c = identities.OwnedCase(tmp_path, monkeypatch)
    try:
        first = await c.registry.ensure()
        assert isinstance(first, Ok), first
        receipt = await c.current()
        c.unreadable.add(receipt.daemon.pid)
        blocked = await c.registry.restart()
        assert isinstance(blocked, Err) and blocked.error.code is ErrorCode.ownership_mismatch
        assert len(c.process.spawned) == 1
        assert c.process.terminated == []
        c.unreadable.clear()
        c.absence()
        monkeypatch.setattr(identities, "SUPERVISOR", ProcessIdentity(pid=220, start_ticks=40))
        monkeypatch.setattr(identities, "DAEMON", ProcessIdentity(pid=230, start_ticks=50))
        c.facts.peer_pid = identities.DAEMON.pid
        # A reopened registry must reconcile the previous receipt before replacement.
        c.reopen()
        c.registry._ids = SequenceIds(seed=2000)
        next_launch = await c.registry.ensure()
        assert isinstance(next_launch, Ok), next_launch
        successor = await c.current()
        assert successor.launch_uuid != receipt.launch_uuid
        assert successor.endpoint_generation == receipt.endpoint_generation + 1
        history = (await c.receipts.all()).value
        assert next(r for r in history if r.launch_uuid == receipt.launch_uuid).state == "stopped"
        assert c.events.index("terminal-write") < c.events.index("spawn", c.events.index("spawn") + 1)
        assert len(c.process.spawned) == 2
        assert await c.registry.shutdown() == Ok(value=None)
    finally:
        c.close()
