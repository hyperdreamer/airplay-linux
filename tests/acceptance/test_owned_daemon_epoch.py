"""Acceptance probes for the activated owned-daemon composition."""
from __future__ import annotations

import asyncio
import json
import os
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
from tests.acceptance.test_no_production_spawn import (
    ProcessRecorder, offline_boundaries, public_production_graph,
)
from tests.process.test_credential_probe import (
    namespace_probe_witness, private_socket_root, recording_probe_processes,
)
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


@pytest.fixture
async def public_runtime(public_production_graph, monkeypatch):
    """Assemble production adapters; fake only process, portal and kernel facts."""
    from pathlib import Path

    from airplay_linux.trust.fences import TrustFence
    from airplay_linux.process import ownership
    from tests.capture.test_physical_sources import FakePortal
    from tests.process import test_owned_launcher as lifecycle

    witness = public_production_graph
    probe_process = witness.outer
    case = lifecycle.OwnedCase(witness.socket_root, monkeypatch)
    portal = FakePortal()
    case.targets = {}
    case.control_calls = []
    case.inventory_reads = 0
    case.source_available = True
    case.events.clear()

    def status():
        streams = [{
            "device": host, "device_ip": host, "state": "streaming",
            "has_audio": True, "audio_muted": muted,
            **({"source": {"Type": 1, "Width": 1920, "Height": 1080,
                            "X": -1920, "Y": 0}} if case.source_available else {}),
        } for host, muted in case.targets.items()]
        first = streams[0] if streams else {}
        return {
            "ok": True, "state": "streaming" if streams else "idle",
            "device": first.get("device"), "device_ip": first.get("device_ip"),
            "has_audio": bool(streams), "audio_muted": first.get("audio_muted", False),
            "streams": streams,
        }

    class Process(lifecycle.Process):
        async def run(self, spec, timeout, stdin=None):
            if spec.argv == ("kscreen-doctor", "-j"):
                witness.recorder._record("run", spec)
                case.inventory_reads += 1
                payload = {"outputs": [{
                    "id": 1, "name": "DP-1", "enabled": True, "connected": True,
                    "type": 14, "pos": {"x": -1920, "y": 0},
                    "size": {"width": 1920, "height": 1080}, "scale": 1,
                }]}
            elif Path(spec.argv[0]).name == "doubletake-ctl" and spec.argv[1] == "-socket":
                witness.recorder._record("run", spec)
                assert spec.argv[2] == str(case.runtime / "daemon.sock")
                operation = spec.argv[3]
                host = spec.argv[4] if len(spec.argv) == 5 else None
                case.control_calls.append((operation, host))
                if operation == "connect":
                    case.targets[host] = False
                elif operation == "disconnect":
                    case.targets.pop(host)
                elif operation in {"mute", "unmute"}:
                    assert host in case.targets
                    case.targets[host] = operation == "mute"
                else:
                    assert operation == "status", spec.argv
                payload = status()
                if operation == "disconnect":
                    payload = {"ok": True, "state": "idle"}
            else:
                # The namespace witness has independent kernel observations;
                # retain its actual positive and negative inner process ports.
                with monkeypatch.context() as scoped:
                    scoped.setattr(ownership, "_observations", witness.facts)
                    return await probe_process.run(spec, timeout, stdin)
            return Ok(value=CommandOutput(
                exit_code=0, stdout=json.dumps(payload).encode(), stderr=b"",
            ))

        async def spawn(self, spec):
            witness.recorder._record("spawn", spec)
            case.hit("spawn")
            assert len(spec.pass_fds) == 1
            config = json.loads(os.read(spec.pass_fds[0], 4096))
            history = (await case.receipts.all()).value
            # Store enumeration is filename order, so correlate to the config
            # actually delivered to this process rather than a list position.
            candidates = [receipt for receipt in history
                          if str(receipt.launch_uuid) == config["launch_uuid"]]
            assert len(candidates) == 1
            receipt = candidates[0]
            assert receipt.state == "reserved"
            assert config["endpoint_generation"] == receipt.endpoint_generation
            assert "owner_token" not in config
            assert case.authority.owner is not None
            case.runtime = Path(receipt.runtime_directory)
            case.configs.append(config)
            records = (await case.store.all()).value
            record = next(record for record in records if record.app == lifecycle.APP)
            self.spawned.append(spec)
            env = self.owner_environment(receipt.launch_uuid, record.runtime_directory)
            self.register(lifecycle.SUPERVISOR, env)
            self.register(lifecycle.DAEMON, env)
            case.record = replace(record, supervisor=lifecycle.SUPERVISOR,
                                  daemon=lifecycle.DAEMON)
            await case.store.write(case.record)
            case.hit("supervisor-publication")
            if case.bind_on_spawn:
                case.bind()
            return Ok(value=lifecycle.SUPERVISOR)

    async def current():
        history = (await case.receipts.all()).value
        return max(history, key=lambda receipt: receipt.endpoint_generation)

    case.current = current
    case.process = Process(case)
    witness.outer = case.process
    install = composition._install_owned_daemon_epoch
    registry = composition.DaemonRegistry

    def installation(config, *, process, launcher):
        installed = install(config, process=process, launcher=launcher)
        case.root = config.state_root
        case.receipts = installed.receipts
        case.authority = installed.shared_authority
        case.authority._inspect = case.facts.identity
        case.authority._boot_id = case.facts.boot_id
        case.ownership = installed.ownership
        write = installed.receipts.write

        async def receipt_write(receipt):
            result = await write(receipt)
            if isinstance(result, Ok):
                case.events.append("receipt:" + receipt.state)
            return result

        monkeypatch.setattr(installed.receipts, "write", receipt_write)
        return installed

    def observed_registry(*args, **kwargs):
        kwargs.update(app=lifecycle.APP, inspect_process=case.facts.identity,
                      scan_owned=case.scan, startup_timeout=0.1)
        case.store = kwargs["store"]
        case.registry = registry(*args, **kwargs)
        return case.registry

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    monkeypatch.setattr(composition, "DaemonRegistry", observed_registry)
    monkeypatch.setattr(composition, "PortalClient", lambda: portal)
    runtime = await witness.build_complete_production_and_probe()
    case.runtime_graph = runtime
    case.witness = witness
    case.portal = portal
    case.transport_events = []
    subscription = runtime.deps.transport.subscribe(case.transport_events.append)

    def admit(receiver, operation):
        runtime.deps.fences.set(TrustFence(
            receiver_id=receiver.receiver_id,
            endpoint_fingerprint=receiver.endpoint_fingerprint,
            endpoint_generation=receiver.endpoint_generation,
            session_generation=operation.session_generation,
            trust_generation=operation.trust_generation or 0,
        ))
        runtime.deps.transport.update_target(receiver, receiver.receiver_id)

    case.admit = admit
    try:
        yield case
    finally:
        await runtime.deps.transport.shutdown()
        await runtime.shutdown()
        subscription.close()
        case.close()


@pytest.mark.asyncio
async def test_public_shape_complete_graph_uses_evidence_authority(public_runtime):
    from airplay_linux.domain.resources import AudioPolicy

    c = public_runtime
    runtime = c.runtime_graph
    assert runtime.registry._process is c.process
    assert runtime.registry._public_seams_complete is True
    assert isinstance(runtime.registry._ownership, EvidenceAuthority)
    assert runtime.registry._ownership is runtime.deps.transport._registry._ownership
    assert len(c.witness.outer_unshare) == 1
    assert [item.leg for item in c.witness.inner_launches] == ["positive", "negative"]
    assert c.process.spawned == []
    launched = await runtime.registry.ensure()
    assert isinstance(launched, Ok), launched
    receipt = await c.current()
    assert receipt.state == "ready"
    control = runtime.registry._control_factory(launched.value)
    proof = await control.handshake("unused", launched.value.instance_uuid)
    assert isinstance(proof, Ok) and proof.value.kind == "evidence"
    assert proof.value.handshake is None
    assert c.control_calls == [("status", None)]
    receiver, operation = target(), context()
    c.admit(receiver, operation)
    source = await runtime.deps.capture.select("DP-1")
    assert isinstance(source, Ok), source
    c.source_available = False
    acquisition = asyncio.create_task(runtime.deps.transport.start_stream(
        receiver, source.value, AudioPolicy(enabled=True), operation,
    ))
    for _ in range(100):
        if ("connect", receiver.canonical_host) in c.control_calls:
            break
        await asyncio.sleep(0)
    assert ("connect", receiver.canonical_host) in c.control_calls
    assert not acquisition.done()
    assert not any(event.kind is TransportEventKind.ready for event in c.transport_events)
    c.source_available = True
    started = await acquisition
    assert isinstance(started, Ok), started
    assert [event.kind for event in c.transport_events].count(TransportEventKind.ready) == 1
    assert c.inventory_reads >= 3
    assert await runtime.deps.transport.stop_stream(started.value, operation) == Ok(value=None)
    assert await runtime.deps.capture.release(source.value) == Ok(value=None)
    detail = runtime.gated_capabilities["owned_daemon_startup"]
    for fact in (
        "authority=EvidenceAuthority", "credential-service=passed",
        "credential-positive=passed", "credential-negative=passed",
        "source-verifier=wired", "source-complete=True", "physical-inventory=True",
    ):
        assert fact in detail
    assert len(c.process.spawned) == 1
    assert c.process.terminated == []


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
async def test_public_shape_target_controls_and_whole_shutdown_are_distinct(public_runtime):
    from airplay_linux.domain.resources import AudioPolicy

    c = public_runtime
    runtime = c.runtime_graph
    transport = runtime.deps.transport
    receivers = (target(), target("b", address="192.0.2.11"))
    contexts = (context(), context(lease=2))
    source = await runtime.deps.capture.select("DP-1")
    assert isinstance(source, Ok), source
    handles = []
    for receiver, operation in zip(receivers, contexts):
        c.admit(receiver, operation)
        result = await transport.start_stream(receiver, source.value, AudioPolicy(enabled=True), operation)
        assert isinstance(result, Ok), result
        handles.append(result.value)
    assert len(c.process.spawned) == 1
    assert c.targets == {receiver.canonical_host: False for receiver in receivers}
    c.control_calls.clear()
    daemon = transport._daemons[handles[0].daemon_instance_uuid]
    # Published 0.5 withholds audio_restart; exercise its target mute adapter
    # using the actual transport-owned proof and current dispatch fence.
    for muted, operation in ((True, BackendOperation.mute), (False, BackendOperation.unmute)):
        changed = await daemon.control.request(
            operation, receivers[0], MutePayload(muted=muted), contexts[0],
            ownership_proof=daemon.ownership_proof,
            dispatch_fence=transport._dispatch_fence(receivers[0], contexts[0], cleanup=False),
        )
        assert isinstance(changed, Ok) and changed.value.muted is muted
        assert c.targets == {receivers[0].canonical_host: muted, receivers[1].canonical_host: False}
    assert await transport.stop_stream(handles[0], contexts[0]) == Ok(value=None)
    assert c.targets == {receivers[1].canonical_host: False}
    assert handles[1].lease_id in transport._streams
    assert c.process.terminated == []
    assert [(op, host) for op, host in c.control_calls if op != "status"] == [
        ("mute", receivers[0].canonical_host), ("unmute", receivers[0].canonical_host),
        ("disconnect", receivers[0].canonical_host),
    ]
    assert await transport.shutdown() == Ok(value=None)
    assert await runtime.shutdown() == Ok(value=None)
    assert c.targets == {}
    assert [call for call in c.control_calls if call[0] == "disconnect"] == [
        ("disconnect", receiver.canonical_host) for receiver in receivers
    ]
    assert all(op != "shutdown" for op, host in c.control_calls)
    assert c.process.terminated == [c.record.daemon]
    assert c.process.strict_terminations[0].stage == "bound"
    assert (await c.current()).state == "stopped"
    assert await runtime.deps.capture.release(source.value) == Ok(value=None)


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


@pytest.mark.parametrize("changed", ("generation", "entry_inode"))
@pytest.mark.parametrize("operation", [
    BackendOperation.connect, BackendOperation.disconnect,
    BackendOperation.mute, BackendOperation.unmute,
])
async def test_changed_current_receipt_blocks_every_mutation_before_io(operation, changed):
    e = EndpointEvidence()

    class ReplacingAuthority(EvidenceAuthority):
        async def authorize(self, record, operation, receiver, fence):
            authorized = await super().authorize(record, operation, receiver, fence)
            assert isinstance(authorized, Ok)
            # Replace the independently read current endpoint after authorization.
            if changed == "generation":
                e.receipts = (replace(e.receipt, endpoint_generation=8),)
            else:
                e.entry.st_ino += 1
                e.receipts = (replace(e.receipt, entry_inode=e.entry.st_ino),)
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
    public_production_graph, monkeypatch, member, label,
):
    c = public_production_graph
    original = composition._install_owned_daemon_epoch

    def install(config, *, process, launcher):
        installed = original(config, process=process, launcher=launcher)
        assert process is c.outer
        if member == "owned_daemon_control":
            monkeypatch.setattr(type(launcher), "available", lambda self: False)
            return installed
        if member == "unshare":
            return installed
        value = (lambda *args: None) if member == "strict_teardown" else None
        return replace(installed, **{member: value})

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", install)
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: None if member == "unshare" else "/fake/unshare")
    runtime = await c.build_complete_production_and_probe()
    try:
        assert runtime.registry._process is c.outer
        assert runtime.gated["owned_daemon_control"] == label
        if member == "source_verifier":
            assert runtime.deps.transport._features.physical_source is False
        for attempt in (runtime.registry.ensure, runtime.deps.restart_daemon):
            refused = await attempt()
            assert isinstance(refused, Err) and refused.error.code is ErrorCode.dependency_incompatible
    finally:
        await runtime.shutdown()
    assert c.events == ["derive", "registry"]
    assert c.outer_unshare == [] and c.inner_launches == []
    assert c.inner.invocations == [] and not c.outer.spawned
    assert c.recorder.events("outer_namespace") == []
    assert c.recorder.events("direct_backend") == []
    assert not any(event.method == "spawn" for event in c.recorder.observations)


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


async def test_crash_restart_requires_reconciliation_and_new_generation(public_runtime, monkeypatch):
    from tests.process import test_owned_launcher as identities

    c = public_runtime
    assert c.registry is c.runtime_graph.registry
    # The next reservation sorts before the terminal receipt by filename.
    # Neither the process fake nor the assertions may select history[-1].
    ids = iter(UUID(int=value) for value in (3000, 3001, 1000, 1001))
    monkeypatch.setattr(c.registry._ids, "new", lambda: next(ids))
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
    reconciled = await c.registry.reconcile_previous()
    assert isinstance(reconciled, Ok), reconciled
    assert (await c.current()).state == "stopped"
    assert len(c.process.spawned) == 1 and c.process.terminated == []
    next_launch = await c.registry.ensure()
    assert isinstance(next_launch, Ok), next_launch
    successor = await c.current()
    assert successor.launch_uuid != receipt.launch_uuid
    assert successor.endpoint_generation == receipt.endpoint_generation + 1
    history = (await c.receipts.all()).value
    assert next(r for r in history if r.launch_uuid == receipt.launch_uuid).state == "stopped"
    assert c.events.index("receipt:stopped") < c.events.index("spawn", c.events.index("spawn") + 1)
    assert len(c.process.spawned) == 2
    assert await c.registry.shutdown() == Ok(value=None)
