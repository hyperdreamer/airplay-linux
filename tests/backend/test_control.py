"""Control adapter tests for published and application-contract profiles."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.backend.contract import BackendFeatures
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    ConnectPayload,
    EmptyPayload,
    ModesPayload,
    MutePayload,
    PairingPayload,
    OwnershipProof,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
    SourcePayload,
    StatusReply,
    UnsupportedControl,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, OperationContext, OperationId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import AudioPolicy, Err, ErrorCode, Ok, VolumeLevel
from airplay_linux.testing.builders import physical, target


class _Process:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    async def run(self, spec, timeout, stdin=None):
        self.calls.append((spec, timeout, stdin))
        outcome = self.outputs.pop(0)
        if callable(outcome):
            return outcome(spec, timeout, stdin)
        return outcome


FIXTURES = Path(__file__).parents[1] / "fixtures" / "backend"


class _ApplicationContractProcess:
    """Deterministic process fake for the non-live application contract."""

    def __init__(self, operations):
        self.operations = set(operations)
        self.calls = []

    async def run(self, spec, timeout, stdin=None):
        self.calls.append((spec, timeout, stdin))
        operation = spec.argv[3]
        if operation not in self.operations:
            return output(json.dumps({"ok": False, "state": "idle"}), exit_code=1)
        if operation == "status":
            return output(json.dumps(owned_status()))
        if operation in {"mute", "unmute"}:
            target_text = spec.argv[4]
            status = owned_status()
            status["device_ip"] = target_text
            status["audio_muted"] = operation == "mute"
            status["streams"] = [{
                "device": "Living Room",
                "device_ip": target_text,
                "state": "streaming",
                "has_audio": True,
                "audio_muted": operation == "mute",
            }]
            return output(json.dumps(status))
        if operation in {"volume", "audio_off"}:
            return output(json.dumps(volume_status(closed=operation == "audio_off")))
        return output(json.dumps({"ok": True, "state": "streaming"}))


def output(payload, *, exit_code=0, stderr=b""):
    if isinstance(payload, str):
        payload = payload.encode()
    return Ok(value=CommandOutput(exit_code=exit_code, stdout=payload, stderr=stderr))


def _open_fence():
    """A fence that always passes, for tests not about dispatch freshness."""

    return Ok(value=None)


def secure_features():
    return BackendFeatures(
        profile="application-contract-v1",
        version="v1",
        foreground_control=True,
        owner_handshake=True,
        multi_target=True,
        target_bound_pairing=True,
        physical_source=True,
        virtual_source=True,
        audio_probe=True,
        audio_restart=True,
        target_mute=True,
        keyring=True,
        provisional_commit=True,
        cancel_pairing=True,
        revoke_pairing=True,
        query_modes=True,
    )


def secure_profile():
    return PublishedDoubletakeProfile(
        socket_path="/run/user/1000/private.sock",
        name="application-contract-v1",
        version="v1",
        provenance="application-contract-fixture",
        live_support_proven=False,
        features=secure_features(),
        owner_token="owner-token",
        instance_uuid=InstanceId(UUID(int=9)),
        protocol_version="v1",
    )


def owned_status(*, instance=9, owner="owner-token", socket="/run/user/1000/private.sock", pid=123, ticks=456):
    return {
        "ok": True,
        "state": "streaming",
        "device": "Living Room",
        "device_ip": "192.0.2.10",
        "has_audio": True,
        "audio_muted": False,
        "owner_token": owner,
        "instance_uuid": str(UUID(int=instance)),
        "socket_path": socket,
        "protocol_version": "v1",
        "daemon_pid": pid,
        "daemon_start_ticks": ticks,
        "streams": [{
            "device": "Living Room",
            "device_ip": "192.0.2.10",
            "state": "streaming",
            "has_audio": True,
            "audio_muted": False,
        }],
    }


def context():
    return OperationContext(
        operation_id=OperationId(UUID(int=1)),
        session_generation=1,
        endpoint_generation=1,
    )


def volume_profile():
    profile = secure_profile()
    return replace(profile, features=replace(profile.features, target_volume=True))


def control_volume_payload(operation="volume"):
    from airplay_linux.backend.control import AudioGatePayload, VolumePayload
    return VolumePayload(level=VolumeLevel(value=30)) if operation == "volume" else AudioGatePayload()


def test_volume_profile_uses_exact_targeted_argv():
    from airplay_linux.backend.control import AudioGatePayload, VolumePayload
    profile = PublishedDoubletakeProfile(socket_path="/run/private/control.sock")
    profile = replace(profile, features=replace(profile.features, target_volume=True))
    receiver = target()
    assert profile.argv(BackendOperation.volume, receiver, VolumePayload(level=VolumeLevel(value=30))) == (
        "doubletake-ctl", "-socket", "/run/private/control.sock", "volume", "30", receiver.canonical_host,
    )
    assert profile.argv(BackendOperation.audio_off, receiver, AudioGatePayload()) == (
        "doubletake-ctl", "-socket", "/run/private/control.sock", "audio", "off", receiver.canonical_host,
    )


@pytest.mark.parametrize("audio,level,suffix", [
    (AudioPolicy(enabled=True), VolumeLevel(value=30), ("--audio", "on", "--volume", "30")),
    (AudioPolicy(enabled=True), None, ("--audio", "on")),
    (AudioPolicy(enabled=False), None, ("--audio", "off")),
    (None, None, ()),
])
def test_volume_capable_connect_argv_and_legacy_refusal(audio, level, suffix):
    profile = volume_profile()
    payload = ConnectPayload(audio=audio, initial_volume=level)
    base = ("doubletake-ctl", "-socket", profile.socket_path, "connect", "192.0.2.10")
    assert profile.argv(BackendOperation.connect, target(), payload) == base + suffix
    legacy = replace(profile, features=replace(profile.features, target_volume=False))
    result = legacy.try_argv(BackendOperation.connect, target(), payload)
    if audio is None and level is None:
        assert result == Ok(value=base)
    else:
        assert isinstance(result, Err) and result.error.code is ErrorCode.unsupported_operation


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
def test_volume_operations_require_feature_target_and_exact_payload(operation):
    profile, payload = volume_profile(), control_volume_payload(operation)
    op = BackendOperation(operation)
    for receiver, argument, expected in (
        (None, payload, ErrorCode.unsupported_operation),
        (target(), EmptyPayload(), ErrorCode.invalid_input),
        (target(), object(), ErrorCode.invalid_input),
        (target(), control_volume_payload("audio_off" if operation == "volume" else "volume"), ErrorCode.invalid_input),
    ):
        result = profile.try_argv(op, receiver, argument)
        assert isinstance(result, Err) and result.error.code is expected
    legacy = replace(profile, features=replace(profile.features, target_volume=False))
    result = legacy.try_argv(op, target(), payload)
    assert isinstance(result, Err) and result.error.code is ErrorCode.unsupported_operation


@pytest.mark.parametrize("value", [True, 0, 30, 30.0, "30", None, object()])
def test_volume_payloads_reject_wrong_domain_types(value):
    from airplay_linux.backend.control import VolumePayload
    with pytest.raises((TypeError, ValueError)):
        VolumePayload(level=value)
    if value is not None:
        with pytest.raises((TypeError, ValueError)):
            ConnectPayload(initial_volume=value)


@pytest.mark.parametrize("value", [True, 0, 1, None, "off", object()])
def test_volume_audio_gate_is_false_only(value):
    from airplay_linux.backend.control import AudioGatePayload
    with pytest.raises((TypeError, ValueError)):
        AudioGatePayload(enabled=value)


@pytest.mark.parametrize("value", [True, False, "on", 1, object()])
def test_volume_connect_policy_is_exact_domain_type(value):
    with pytest.raises((TypeError, ValueError)):
        ConnectPayload(audio=value)


def test_volume_connect_forbids_disabled_level_and_preserves_secret_source_refusals():
    profile = volume_profile()
    for payload, expected in (
        (ConnectPayload(audio=AudioPolicy(enabled=False), initial_volume=VolumeLevel(value=30)), ErrorCode.invalid_input),
        (ConnectPayload(audio=AudioPolicy(), credential="canary-secret"), ErrorCode.unsupported_operation),
        (ConnectPayload(audio=AudioPolicy(), source=physical("canary-secret")), ErrorCode.unsupported_source),
    ):
        result = profile.try_argv(BackendOperation.connect, target(), payload)
        assert isinstance(result, Err) and result.error.code is expected
        assert "canary-secret" not in repr(payload)
        assert "canary-secret" not in repr(result)


def volume_status(*, closed=False, connecting=False):
    status = owned_status()
    status["streams"][0].update(volume=30, volume_state="applied", audio_closed=closed)
    if connecting:
        status.update(state="connecting", has_audio=False, audio_muted=False)
        status.pop("device")
        status.pop("device_ip")
        status["streams"][0].update(state="connecting", has_audio=False)
    return status


@pytest.mark.parametrize("operation,connecting", [("volume", False), ("audio_off", False), ("audio_off", True)])
async def test_volume_live_success_returns_full_status(operation, connecting):
    from airplay_linux.backend.contract import decode_status
    reply = volume_status(closed=operation == "audio_off", connecting=connecting)
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply))])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    notices = []
    control.subscribe(notices.append)
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=_open_fence)
    assert result == Ok(value=decode_status(reply))
    assert type(result.value) is StatusReply
    assert notices[-1].status == result.value
    assert len(process.calls) == 3


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("damage", ["aggregate_only", "foreign", "duplicate", "no_latch", "wrong_latch", "missing_entry"])
async def test_volume_live_requires_exact_target_confirmation(operation, damage):
    reply = volume_status(closed=operation == "audio_off")
    if damage in {"aggregate_only", "missing_entry"}:
        reply["streams"] = []
        reply.update(state="idle", has_audio=False, audio_muted=False)
        reply.pop("device")
        reply.pop("device_ip")
    elif damage == "foreign":
        reply["streams"][0]["device_ip"] = reply["device_ip"] = "192.0.2.11"
    elif damage == "duplicate":
        reply["streams"] *= 2
    elif damage == "no_latch":
        del reply["streams"][0]["audio_closed"]
    else:
        reply["streams"][0]["audio_closed"] = operation != "audio_off"
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply))])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=_open_fence)
    assert isinstance(result, Err) and result.error.code is ErrorCode.audio_volume_failed


@pytest.mark.parametrize("change", [
    {"volume": 40}, {"volume_state": "pending"}, {"volume_state": "refused"},
    {"volume_state": "unrequested"}, {"has_audio": False}, {"volume": None},
])
async def test_volume_live_requires_applied_matching_level_with_audio(change):
    reply = volume_status()
    reply["streams"][0].update(change)
    if "has_audio" in change:
        reply["has_audio"] = change["has_audio"]
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply))])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(BackendOperation.volume, target(), control_volume_payload(), context(), dispatch_fence=_open_fence)
    assert isinstance(result, Err) and result.error.code is ErrorCode.audio_volume_failed


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("ok,exit_code,expected", [
    (False, 1, ErrorCode.audio_volume_failed), (False, 0, ErrorCode.daemon_failed),
    (False, 2, ErrorCode.daemon_failed), (True, 1, ErrorCode.daemon_failed),
    ("false", 0, ErrorCode.dependency_incompatible), (None, 0, ErrorCode.dependency_incompatible),
])
async def test_volume_live_rejection_envelope_is_strict_and_sanitized(operation, ok, exit_code, expected):
    reply = volume_status(closed=operation == "audio_off") | {"ok": ok, "error": "canary-secret"}
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply), exit_code=exit_code)])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=_open_fence)
    assert isinstance(result, Err) and result.error.code is expected
    assert "canary-secret" not in repr(result)


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("exit_code,expected", [(0, ErrorCode.dependency_incompatible), (1, ErrorCode.daemon_failed)])
async def test_volume_live_malformed_json_preserves_dependency_errors(operation, exit_code, expected):
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output("canary-secret", exit_code=exit_code)])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=_open_fence)
    assert isinstance(result, Err) and result.error.code is expected
    assert "canary-secret" not in repr(result)


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("change,expected", [
    ({"owner_token": "foreign"}, ErrorCode.ownership_mismatch),
    ({"instance_uuid": str(UUID(int=99))}, ErrorCode.ownership_mismatch),
    ({"socket_path": "/foreign.sock"}, ErrorCode.stale_socket),
    ({"protocol_version": "foreign"}, ErrorCode.dependency_incompatible),
])
async def test_volume_live_checks_ownership_metadata(operation, change, expected):
    reply = volume_status(closed=operation == "audio_off") | change
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply))])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=_open_fence)
    assert isinstance(result, Err) and result.error.code is expected


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
async def test_volume_live_missing_fence_never_does_any_io(operation):
    process = _Process([])
    control = PublishedDoubletakeControl(process, volume_profile())
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context())
    assert isinstance(result, Err) and result.error.code is ErrorCode.dependency_incompatible
    assert process.calls == []


@pytest.mark.parametrize("level", [0, 100])
async def test_volume_live_receipt_uses_original_target_in_multistream_status(level):
    from airplay_linux.backend.control import VolumePayload
    reply = volume_status()
    reply["streams"][0]["volume"] = 70
    reply["streams"].append({
        "device": "Office", "device_ip": "192.0.2.11", "state": "streaming",
        "has_audio": True, "audio_muted": level == 0,
        "volume": level, "volume_state": "applied", "audio_closed": False,
    })
    process = _Process([output(json.dumps(owned_status())), output(json.dumps(owned_status())), output(json.dumps(reply))])
    control = PublishedDoubletakeControl(process, volume_profile())
    assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
    result = await control.request(
        BackendOperation.volume, target("b", address="192.0.2.11"), VolumePayload(level=VolumeLevel(value=level)),
        context(), dispatch_fence=_open_fence,
    )
    assert isinstance(result, Ok) and isinstance(result.value, StatusReply)
    assert result.value.streams[1].volume == level
    assert process.calls[-1][0].argv[-3:] == ("volume", str(level), "192.0.2.11")


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("stage", ["missing_fence", "stale_target", "rejected_owner", "late_owner", "valid"])
async def test_volume_live_public_authority_keeps_fence_and_confirmation(operation, stage):
    proof = OwnershipProof(kind="evidence", daemon=ProcessIdentity(pid=300, start_ticks=3), socket_entry=(1, 2), generation=1, handshake=None)
    calls, current = [], True
    class Authority:
        def allows(self, operation):
            return True
        async def authorize(self, record, operation, target, fence):
            nonlocal current
            calls.append("authorize")
            if stage == "rejected_owner":
                return Err(error=diagnostic(ErrorCode.ownership_mismatch))
            if stage == "stale_target":
                current = False
            return Ok(value=proof)
        async def prove(self, record):
            calls.append("prove")
            return Ok(value=proof)
        async def confirm(self, record, original):
            calls.append("confirm")
            assert original == proof
            return Err(error=diagnostic(ErrorCode.ownership_mismatch)) if stage == "late_owner" else Ok(value=None)
    reply = volume_status(closed=operation == "audio_off")
    for key in ("owner_token", "instance_uuid", "socket_path", "protocol_version", "daemon_pid", "daemon_start_ticks"):
        del reply[key]
    process = _Process([output(json.dumps(reply))])
    profile = volume_profile()
    profile = replace(profile, owner_token=None, instance_uuid=None, protocol_version=None, features=replace(profile.features, profile="doubletake-0.5+source", owned_daemon_control=True, owner_handshake=False))
    control = PublishedDoubletakeControl(process, profile, record=object(), authority=Authority())
    def fence():
        calls.append("fence")
        return Ok(value=None) if current else Err(error=diagnostic(ErrorCode.stale_target))
    result = await control.request(BackendOperation(operation), target(), control_volume_payload(operation), context(), dispatch_fence=None if stage == "missing_fence" else fence)
    if stage == "valid":
        assert isinstance(result, Ok) and isinstance(result.value, StatusReply)
    else:
        expected = {
            "missing_fence": ErrorCode.dependency_incompatible, "stale_target": ErrorCode.stale_target,
            "rejected_owner": ErrorCode.ownership_mismatch, "late_owner": ErrorCode.ownership_mismatch,
        }[stage]
        assert isinstance(result, Err) and result.error.code is expected
    assert len(process.calls) == (1 if stage in {"valid", "late_owner"} else 0)
    if stage == "missing_fence":
        assert calls == []
    elif stage in {"valid", "late_owner"}:
        assert calls == ["authorize", "prove", "fence", "confirm"]


def test_application_contract_profile_is_explicitly_non_live_and_secret_free():
    profile = ApplicationContractProfile(socket_path="/run/user/1000/private.sock")
    argv = profile.argv(
        BackendOperation.submit_pairing,
        target(),
        PairingPayload(credential="contract-secret"),
    )

    assert profile.provenance == "application-contract-fixture"
    assert profile.live_support_proven is False
    assert profile.protocol_version == "v1"
    assert "contract-secret" not in repr(profile)
    assert "contract-secret" not in repr(argv)
    with pytest.raises(ValueError):
        ApplicationContractProfile(
            socket_path="/run/user/1000/private.sock",
            live_support_proven=True,
        )


def test_application_contract_fake_exercises_every_typed_operation():
    contract = json.loads((FIXTURES / "contract-v1.json").read_text())
    operations = {item["operation"] for item in contract["requests"]}
    process = _ApplicationContractProcess(operations)
    control = PublishedDoubletakeControl(
        process,
        ApplicationContractProfile(
            socket_path="/run/user/1000/private.sock",
            owner_token="owner-token",
            instance_uuid=InstanceId(UUID(int=9)),
        ),
    )
    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    notices = []
    subscription = control.subscribe(notices.append)

    payloads = {
        BackendOperation.status: EmptyPayload(),
        BackendOperation.discover: EmptyPayload(),
        BackendOperation.devices: EmptyPayload(),
        BackendOperation.connect: ConnectPayload(),
        BackendOperation.disconnect: EmptyPayload(),
        BackendOperation.mute: MutePayload(muted=True),
        BackendOperation.unmute: MutePayload(muted=False),
        BackendOperation.volume: control_volume_payload(),
        BackendOperation.audio_off: control_volume_payload("audio_off"),
        BackendOperation.query_modes: ModesPayload(),
        BackendOperation.start_audio: SourcePayload(source=physical()),
        BackendOperation.restart_audio: SourcePayload(source=physical()),
        BackendOperation.begin_pairing: PairingPayload(credential="secret"),
        BackendOperation.submit_pairing: PairingPayload(credential="secret"),
        BackendOperation.cancel_pairing: EmptyPayload(),
        BackendOperation.prepare_pairing: PairingPayload(credential="secret"),
        BackendOperation.commit_pairing: PairingPayload(credential="secret"),
        BackendOperation.revoke_pairing: EmptyPayload(),
        BackendOperation.mark_invalid: EmptyPayload(),
        BackendOperation.shutdown: EmptyPayload(),
    }
    assert set(payloads) == set(BackendOperation)
    assert {op.value for op in payloads} == operations
    for operation, payload in payloads.items():
        selected_control = control
        if operation in {BackendOperation.volume, BackendOperation.audio_off}:
            assert control._profile.features.target_volume is False
            capable = replace(control._profile, features=replace(control._profile.features, target_volume=True))
            selected_control = PublishedDoubletakeControl(process, capable)
            assert isinstance(asyncio.run(selected_control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
        operation_target = None if operation in {
            BackendOperation.status,
            BackendOperation.discover,
            BackendOperation.devices,
            BackendOperation.shutdown,
        } else target()
        result = asyncio.run(selected_control.request(
            operation, operation_target, payload, context(), dispatch_fence=_open_fence,
        ))
        assert isinstance(result, Ok), operation
        if operation in {BackendOperation.volume, BackendOperation.audio_off}:
            assert isinstance(result.value, StatusReply)
        elif operation not in {
            BackendOperation.status,
            BackendOperation.discover,
            BackendOperation.devices,
        }:
            assert result.value.operation == operation.value

    assert all("secret" not in repr(call[0].argv) for call in process.calls)
    assert all(call[2] is None for call in process.calls)
    assert any(notice.kind.value == "status" for notice in notices)
    assert any(notice.kind.value == "stream" for notice in notices)
    subscription.close()


@pytest.mark.parametrize(
    ("operation", "feature"),
    [
        (BackendOperation.connect, "owner_handshake"),
        (BackendOperation.disconnect, "multi_target"),
        (BackendOperation.mute, "target_mute"),
        (BackendOperation.unmute, "target_mute"),
        (BackendOperation.volume, "multi_target"),
        (BackendOperation.audio_off, "multi_target"),
        (BackendOperation.volume, "owner_handshake"),
        (BackendOperation.audio_off, "owner_handshake"),
        (BackendOperation.volume, "target_volume"),
        (BackendOperation.audio_off, "target_volume"),
        (BackendOperation.query_modes, "query_modes"),
        (BackendOperation.start_audio, "audio_restart"),
        (BackendOperation.restart_audio, "audio_restart"),
        (BackendOperation.begin_pairing, "target_bound_pairing"),
        (BackendOperation.submit_pairing, "keyring"),
        (BackendOperation.cancel_pairing, "cancel_pairing"),
        (BackendOperation.prepare_pairing, "provisional_commit"),
        (BackendOperation.commit_pairing, "provisional_commit"),
        (BackendOperation.revoke_pairing, "revoke_pairing"),
        (BackendOperation.mark_invalid, "revoke_pairing"),
        (BackendOperation.shutdown, "owner_handshake"),
    ],
)
def test_application_contract_capability_denial_is_fail_closed(operation, feature):
    profile = ApplicationContractProfile(
        socket_path="/run/user/1000/private.sock",
        features=replace(secure_features(), **{
            "target_volume": operation in {BackendOperation.volume, BackendOperation.audio_off},
            feature: False,
        }),
    )
    process = _Process([])
    control = PublishedDoubletakeControl(process, profile)
    payload = MutePayload(muted=True) if operation in {BackendOperation.mute, BackendOperation.unmute} else EmptyPayload()
    if operation in {BackendOperation.volume, BackendOperation.audio_off}:
        payload = control_volume_payload(operation.value)
    if operation in {BackendOperation.query_modes}:
        payload = ModesPayload()
    if operation in {BackendOperation.start_audio, BackendOperation.restart_audio}:
        payload = SourcePayload(source=physical())
    if operation in {BackendOperation.begin_pairing, BackendOperation.submit_pairing, BackendOperation.prepare_pairing, BackendOperation.commit_pairing}:
        payload = PairingPayload(credential="secret")

    result = asyncio.run(control.request(
        operation,
        None if operation is BackendOperation.shutdown else target(),
        payload,
        context(),
    ))

    assert isinstance(result, Err)
    assert result.error.code is (
        ErrorCode.unsupported_operation if feature == "target_volume" else ErrorCode.dependency_incompatible
    )
    assert process.calls == []


def test_profile_maps_only_explicit_target_scoped_published_commands():
    profile = PublishedDoubletakeProfile(socket_path="/run/user/1000/airplay-linux/doubletake.sock")
    receiver = target()

    assert profile.argv(BackendOperation.status, None, EmptyPayload()) == (
        "doubletake-ctl", "-socket", profile.socket_path, "status"
    )
    assert profile.argv(BackendOperation.disconnect, receiver, EmptyPayload())[-2:] == (
        "disconnect", receiver.canonical_host
    )
    assert profile.argv(BackendOperation.mute, receiver, EmptyPayload())[-2:] == (
        "mute", receiver.canonical_host
    )
    assert profile.argv(BackendOperation.unmute, receiver, EmptyPayload())[-2:] == (
        "unmute", receiver.canonical_host
    )


@pytest.mark.parametrize("capable", [False, True])
def test_profile_refuses_unqualified_destructive_controls(capable):
    profile = PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock")
    profile = replace(profile, features=replace(profile.features, target_volume=capable))
    operations = (BackendOperation.disconnect, BackendOperation.mute, BackendOperation.unmute)
    if capable:
        operations += (BackendOperation.volume, BackendOperation.audio_off)
    for operation in operations:
        payload = control_volume_payload(operation.value) if operation in {BackendOperation.volume, BackendOperation.audio_off} else EmptyPayload()
        result = profile.try_argv(operation, None, payload)
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.unsupported_operation


def test_profile_never_places_secret_payload_in_argv():
    profile = PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock")
    payload = ConnectPayload(credential="canary-secret")

    result = profile.try_argv(BackendOperation.connect, target(), payload)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_operation
    assert "canary-secret" not in repr(payload)


def test_unknown_or_negated_contract_cannot_open_connect_io():
    from airplay_linux.backend.contract import features_from_help

    features = features_from_help(
        "doubletake 99.1.0\\nUnsupported: foreground owner-token instance-uuid handshake "
        "physical source connector verify keyring transaction provisional commit pairing "
        "cancel-pairing revoke-pairing query-modes",
        "status\\ndiscover\\ndevices\\nconnect [target] [PIN-or-password]\\n"
        "disconnect [target]\\nmute [target]\\nunmute [target]\\n"
        "Unsupported: target-bound pairing",
    )
    process = _Process([])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(
            socket_path="/run/user/1000/private.sock",
            features=features,
        ),
    )

    result = asyncio.run(control.request(
        BackendOperation.connect, target(), EmptyPayload(), context(),
    ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert process.calls == []


def test_control_decodes_omitted_empty_discovery_response():
    process = _Process([output(json.dumps({"ok": True, "state": "idle"}))])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    result = asyncio.run(control.request(BackendOperation.discover, None, EmptyPayload(), None))

    assert isinstance(result, Ok)
    assert result.value.devices == ()


def test_control_decodes_status_reply_without_exposing_backend_error_text():
    process = _Process([
        output(json.dumps({
            "ok": True,
            "state": "streaming",
            "device": "Living Room",
            "device_ip": "192.0.2.10",
            "has_audio": True,
            "audio_muted": False,
            "streams": [{
                "device": "Living Room",
                "device_ip": "192.0.2.10",
                "state": "streaming",
                "has_audio": True,
                "audio_muted": False,
            }],
        }))
    ])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    result = asyncio.run(control.request(BackendOperation.status, None, EmptyPayload(), None))

    assert isinstance(result, Ok)
    assert isinstance(result.value, StatusReply)
    assert result.value.state == "streaming"
    assert result.value.streams[0].target == "192.0.2.10"


@pytest.mark.parametrize("operation", [BackendOperation.disconnect, BackendOperation.volume, BackendOperation.audio_off])
def test_mutation_requires_authenticated_owner_before_any_mutation_io(operation):
    process = _Process([])
    profile = secure_profile() if operation is BackendOperation.disconnect else volume_profile()
    payload = EmptyPayload() if operation is BackendOperation.disconnect else control_volume_payload(operation.value)
    control = PublishedDoubletakeControl(process, profile)

    result = asyncio.run(control.request(operation, target(), payload, context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert process.calls == []


def test_mutation_rejects_a_stale_daemon_instance_before_sending_command():
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status(instance=10))),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    handshake = asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9))))
    assert isinstance(handshake, Ok)
    result = asyncio.run(control.request(BackendOperation.disconnect, target(), EmptyPayload(), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch
    assert len(process.calls) == 2
    assert all(call[0].argv[3] != "disconnect" for call in process.calls)


def test_control_rejects_malformed_json():
    process = _Process([output(b"not-json")])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    result = asyncio.run(control.request(BackendOperation.status, None, EmptyPayload(), None))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert "not-json" not in result.error.message


def test_control_rejects_wrong_owner_and_mismatched_socket_metadata():
    for metadata in (
        {"owner_token": "wrong", "instance_uuid": str(UUID(int=9))},
        {"owner_token": "owner", "instance_uuid": str(UUID(int=9)), "socket_path": "/other.sock"},
    ):
        process = _Process([output(json.dumps({
            "ok": True,
            "state": "idle",
            "has_audio": False,
            "audio_muted": False,
            **metadata,
        }))])
        control = PublishedDoubletakeControl(
            process,
            PublishedDoubletakeProfile(
                socket_path="/run/user/1000/private.sock",
                owner_token="owner",
                instance_uuid=InstanceId(UUID(int=9)),
            ),
        )

        result = asyncio.run(control.request(BackendOperation.status, None, EmptyPayload(), None))

        assert isinstance(result, Err)
        assert result.error.code in {ErrorCode.ownership_mismatch, ErrorCode.stale_socket}


def test_unsupported_control_rejects_without_process_io():
    process = _Process([])
    control = UnsupportedControl()

    result = asyncio.run(control.request(
        BackendOperation.connect, target(), EmptyPayload(), context(),
    ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_operation
    assert process.calls == []


def test_published_control_fails_closed_per_operation_before_process_io():
    process = _Process([])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    result = asyncio.run(control.request(BackendOperation.connect, target(), EmptyPayload(), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert process.calls == []


class _SlowProcess:
    async def run(self, spec, timeout, stdin=None):
        await asyncio.sleep(1)


@pytest.mark.parametrize("disabled", [
    None, "target_bound_pairing", "provisional_commit", "cancel_pairing", "revoke_pairing",
])
def test_connect_does_not_consult_each_former_pairing_prerequisite(disabled):
    from airplay_linux.process.ownership import EvidenceAuthority
    from airplay_linux.process.procfs import TerminationEvidence

    pairing = {name: disabled is not None and name != disabled for name in (
        "target_bound_pairing", "provisional_commit", "cancel_pairing", "revoke_pairing",
    )}
    features = replace(
        secure_features(), profile="doubletake-0.5+source", version="0.5.0",
        foreground_control=False, owner_handshake=False, owned_daemon_control=True,
        physical_source=True, virtual_source=False, **pairing,
    )
    authority = EvidenceAuthority(
        receipts=lambda: (), process=object(), evidence_factory=TerminationEvidence, features=features,
    )
    control = PublishedDoubletakeControl(
        _Process([]), PublishedDoubletakeProfile(socket_path="/run/private.sock", features=features),
        record=object(), authority=authority,
    )
    assert control._available(BackendOperation.connect) is True


async def test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags():
    requirements = {
        BackendOperation.begin_pairing: ("target_bound_pairing", "cancel_pairing"),
        BackendOperation.submit_pairing: ("target_bound_pairing", "keyring", "provisional_commit"),
        BackendOperation.prepare_pairing: ("target_bound_pairing", "keyring", "provisional_commit"),
        BackendOperation.commit_pairing: ("keyring", "provisional_commit"),
        BackendOperation.mark_invalid: ("revoke_pairing",),
        BackendOperation.revoke_pairing: ("revoke_pairing",),
        BackendOperation.cancel_pairing: ("cancel_pairing",),
    }
    pairing_flags = {"target_bound_pairing", "keyring", "provisional_commit", "cancel_pairing", "revoke_pairing"}
    for operation, own_flags in requirements.items():
        flags = {name: name in own_flags for name in pairing_flags}
        profile = ApplicationContractProfile(
            socket_path="/run/user/1000/private.sock", owner_token="owner-token",
            instance_uuid=InstanceId(UUID(int=9)), features=replace(secure_features(), **flags),
        )
        process = _ApplicationContractProcess({"status", operation.value})
        control = PublishedDoubletakeControl(process, profile)
        assert control._available(operation) is True, operation
        assert isinstance(await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok)
        admitted = await control.request(operation, target(), EmptyPayload(), context())
        assert isinstance(admitted, Ok), (operation, admitted)
        assert [call[0].argv[3] for call in process.calls] == ["status", "status", operation.value]
        for flag in own_flags:
            blocked = replace(profile.features, **{flag: False})
            blocked_process = _Process([])
            blocked_control = PublishedDoubletakeControl(blocked_process, replace(profile, features=blocked))
            assert blocked_control._available(operation) is False, (operation, flag)
            refused = await blocked_control.request(operation, target(), EmptyPayload(), context())
            assert isinstance(refused, Err) and refused.error.code is ErrorCode.dependency_incompatible
            assert blocked_process.calls == []


@pytest.mark.parametrize("operation", [op for op in BackendOperation if op not in {
    BackendOperation.status, BackendOperation.discover, BackendOperation.devices,
}])
def test_fixture_mutations_still_require_owner_handshake(operation):
    profile = replace(secure_profile(), features=replace(
        secure_features(), owner_handshake=False,
        target_volume=operation in {BackendOperation.volume, BackendOperation.audio_off},
    ))
    control = PublishedDoubletakeControl(_Process([]), profile)
    assert control._available(operation) is False


@pytest.mark.parametrize("operation", [BackendOperation.connect, BackendOperation.volume, BackendOperation.audio_off])
def test_replacement_between_authorization_and_dispatch_refuses(operation):
    daemon = ProcessIdentity(pid=300, start_ticks=3)
    first = OwnershipProof(
        kind="evidence", daemon=daemon, socket_entry=(1, 2), generation=1,
        handshake=None,
    )
    replacement = replace(first, generation=2)

    class Authority:
        async def authorize(self, record, operation, target, fence):
            return Ok(value=first)

        async def prove(self, record):
            return Ok(value=replacement)

        def allows(self, requested):
            return requested is operation

        async def confirm(self, record, proof):
            return Ok(value=None)

    process = _Process([])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(
            socket_path="/run/private.sock",
            features=replace(
                secure_features(), profile="doubletake-0.5", version="0.5.0",
                foreground_control=False, owner_handshake=False,
                owned_daemon_control=True, physical_source=True,
                target_volume=operation in {BackendOperation.volume, BackendOperation.audio_off},
            ),
        ),
        record=object(), authority=Authority(),
    )

    result = asyncio.run(control.request(
        operation, target(), ConnectPayload() if operation is BackendOperation.connect else control_volume_payload(operation.value), context(),
        dispatch_fence=_open_fence, ownership_proof=first,
    ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.stale_event
    assert process.calls == []


class _GatedProcess:
    """Process fake that can suspend one selected command before it completes."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.gate_operation = None
        self.gated = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, spec, timeout, stdin=None):
        self.calls.append((spec, timeout, stdin))
        if (
            self.gate_operation is not None
            and spec.argv[3] == self.gate_operation
            and not self.gated
        ):
            self.gated = True
            self.entered.set()
            await self.release.wait()
        return self.outputs.pop(0)


@pytest.mark.parametrize("operation", [BackendOperation.disconnect, BackendOperation.volume, BackendOperation.audio_off])
def test_dispatch_fence_runs_after_ownership_revalidation_and_blocks_spawn(operation):
    async def scenario():
        process = _GatedProcess([
            output(json.dumps(owned_status())),
            output(json.dumps(owned_status())),
            output(json.dumps({"ok": True, "state": "idle"})),
        ])
        profile = secure_profile() if operation is BackendOperation.disconnect else volume_profile()
        payload = EmptyPayload() if operation is BackendOperation.disconnect else control_volume_payload(operation.value)
        control = PublishedDoubletakeControl(process, profile)
        assert isinstance(
            await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok,
        )
        process.gate_operation = "status"
        seen = []

        def fence():
            seen.append(len(process.calls))
            return Err(error=diagnostic(ErrorCode.stale_target))

        pending = asyncio.create_task(control.request(
            operation, target(), payload, context(),
            dispatch_fence=fence,
        ))
        await asyncio.wait_for(process.entered.wait(), timeout=1)
        assert seen == []
        process.release.set()
        result = await pending
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.stale_target
        assert seen == [2]
        assert [call[0].argv[3] for call in process.calls] == ["status", "status"]

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", [BackendOperation.disconnect, BackendOperation.volume, BackendOperation.audio_off])
def test_dispatch_fence_passing_allows_exactly_one_mutating_command(operation):
    reply = {"ok": True, "state": "idle"} if operation is BackendOperation.disconnect else volume_status(closed=operation is BackendOperation.audio_off)
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps(reply)),
    ])
    profile = secure_profile() if operation is BackendOperation.disconnect else volume_profile()
    payload = EmptyPayload() if operation is BackendOperation.disconnect else control_volume_payload(operation.value)
    control = PublishedDoubletakeControl(process, profile)
    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    seen = []

    def fence():
        seen.append(len(process.calls))
        return Ok(value=None)

    result = asyncio.run(control.request(
        operation, target(), payload, context(),
        dispatch_fence=fence,
    ))

    assert isinstance(result, Ok)
    assert seen == [2]
    wire_operation = "audio" if operation is BackendOperation.audio_off else operation.value
    assert [call[0].argv[3] for call in process.calls] == ["status", "status", wire_operation]


@pytest.mark.parametrize("operation", [BackendOperation.disconnect, BackendOperation.volume, BackendOperation.audio_off])
def test_failed_ownership_revalidation_never_consults_the_dispatch_fence(operation):
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status(instance=10))),
    ])
    profile = secure_profile() if operation is BackendOperation.disconnect else volume_profile()
    payload = EmptyPayload() if operation is BackendOperation.disconnect else control_volume_payload(operation.value)
    control = PublishedDoubletakeControl(process, profile)
    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)

    def fence():
        raise AssertionError("fence must not run when ownership revalidation failed")

    result = asyncio.run(control.request(
        operation, target(), payload, context(),
        dispatch_fence=fence,
    ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch
    assert [call[0].argv[3] for call in process.calls] == ["status", "status"]


def test_dispatch_fence_does_not_gate_read_only_commands():
    process = _Process([output(json.dumps({
        "ok": True, "state": "idle", "has_audio": False, "audio_muted": False,
    }))])
    control = PublishedDoubletakeControl(
        process, PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    def fence():
        raise AssertionError("read-only commands have no dispatch fence")

    result = asyncio.run(control.request(
        BackendOperation.status, None, EmptyPayload(), None, dispatch_fence=fence,
    ))

    assert isinstance(result, Ok)
    assert [call[0].argv[3] for call in process.calls] == ["status"]


def test_mute_confirmation_comes_from_the_exact_target_stream():
    receiver_b = target(address="192.0.2.11")
    status = owned_status()
    status["streams"] = [
        {
            "device": "Living Room",
            "device_ip": "192.0.2.10",
            "state": "streaming",
            "has_audio": True,
            "audio_muted": False,
        },
        {
            "device": "Office",
            "device_ip": "192.0.2.11",
            "state": "streaming",
            "has_audio": True,
            "audio_muted": True,
        },
    ]
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(status)),
        output(json.dumps(status)),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    result = asyncio.run(control.request(BackendOperation.mute, receiver_b, MutePayload(muted=True), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Ok)
    assert result.value.target == "192.0.2.11"
    assert result.value.muted is True


def test_unmute_confirmation_comes_from_the_exact_target_stream():
    receiver_b = target(address="192.0.2.11")
    status = owned_status()
    status["device"] = "Office"
    status["device_ip"] = "192.0.2.11"
    status["streams"] = [{
        "device": "Office",
        "device_ip": "192.0.2.11",
        "state": "streaming",
        "has_audio": True,
        "audio_muted": False,
    }]
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps(status)),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    result = asyncio.run(control.request(BackendOperation.unmute, receiver_b, MutePayload(muted=False), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Ok)
    assert result.value.target == "192.0.2.11"
    assert result.value.muted is False


def test_mute_confirmation_rejects_missing_or_ambiguous_target_stream():
    receiver_b = target(address="192.0.2.11")
    status = owned_status()
    status["streams"] = [{
        "device": "Living Room",
        "device_ip": "192.0.2.10",
        "state": "streaming",
        "has_audio": True,
        "audio_muted": False,
    }]
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps(status)),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    result = asyncio.run(control.request(BackendOperation.mute, receiver_b, EmptyPayload(), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_mute_failed


def test_valid_cli_mute_rejection_uses_fixed_audio_diagnostic():
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps({
            "ok": False,
            "state": "streaming",
            "error": "no active stream to 192.0.2.11",
        }), exit_code=1),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    result = asyncio.run(control.request(BackendOperation.mute, target(address="192.0.2.11"), MutePayload(muted=True), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_mute_failed
    assert "no active" not in result.error.message


def test_valid_cli_rejection_exit_is_not_reported_as_daemon_failure():
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps({
            "ok": False,
            "state": "streaming",
            "error": "no active stream to 192.0.2.11",
        }), exit_code=1),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())

    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    result = asyncio.run(control.request(BackendOperation.disconnect, target(address="192.0.2.11"), EmptyPayload(), context(),
            dispatch_fence=_open_fence,
        ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.receiver_rejected
    assert "192.0.2.11" not in result.error.message
    assert "no active" not in result.error.message


def test_malformed_nonzero_output_is_still_a_daemon_failure():
    process = _Process([output("not-json", exit_code=1)])
    control = PublishedDoubletakeControl(process, PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"))

    result = asyncio.run(control.request(BackendOperation.status, None, EmptyPayload(), None))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.daemon_failed


def test_control_timeout_is_bounded_and_sanitized():
    control = PublishedDoubletakeControl(
        _SlowProcess(),
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
        timeout=0.001,
    )

    result = asyncio.run(control.request(BackendOperation.status, None, EmptyPayload(), None))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.daemon_failed


def test_subscriptions_are_idempotently_closed_and_notified():
    process = _Process([])
    control = UnsupportedControl()
    seen = []
    subscription = control.subscribe(seen.append)
    subscription.close()
    subscription.close()
    asyncio.run(control.close())
    assert seen == []
