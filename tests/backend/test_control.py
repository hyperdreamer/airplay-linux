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
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
    SourcePayload,
    StatusReply,
    UnsupportedControl,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, OperationContext, OperationId
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok
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
        return output(json.dumps({"ok": True, "state": "streaming"}))


def output(payload, *, exit_code=0, stderr=b""):
    if isinstance(payload, str):
        payload = payload.encode()
    return Ok(value=CommandOutput(exit_code=exit_code, stdout=payload, stderr=stderr))


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
    for operation, payload in payloads.items():
        operation_target = None if operation in {
            BackendOperation.status,
            BackendOperation.discover,
            BackendOperation.devices,
            BackendOperation.shutdown,
        } else target()
        result = asyncio.run(control.request(operation, operation_target, payload, context()))
        assert isinstance(result, Ok), operation
        if operation not in {
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
        features=replace(secure_features(), **{feature: False}),
    )
    process = _Process([])
    control = PublishedDoubletakeControl(process, profile)
    payload = MutePayload(muted=True) if operation in {BackendOperation.mute, BackendOperation.unmute} else EmptyPayload()
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
    assert result.error.code is ErrorCode.dependency_incompatible
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


def test_profile_refuses_unqualified_destructive_controls():
    profile = PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock")

    for operation in (BackendOperation.disconnect, BackendOperation.mute, BackendOperation.unmute):
        result = profile.try_argv(operation, None, EmptyPayload())
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

    result = asyncio.run(control.request(BackendOperation.connect, target(), EmptyPayload(), context()))

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


def test_mutation_requires_authenticated_owner_before_any_mutation_io():
    process = _Process([])
    control = PublishedDoubletakeControl(process, secure_profile())

    result = asyncio.run(control.request(BackendOperation.disconnect, target(), EmptyPayload(), context()))

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
    result = asyncio.run(control.request(BackendOperation.disconnect, target(), EmptyPayload(), context()))

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

    result = asyncio.run(control.request(BackendOperation.connect, target(), EmptyPayload(), context()))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_operation
    assert process.calls == []


def test_published_control_fails_closed_per_operation_before_process_io():
    process = _Process([])
    control = PublishedDoubletakeControl(
        process,
        PublishedDoubletakeProfile(socket_path="/run/user/1000/private.sock"),
    )

    result = asyncio.run(control.request(BackendOperation.connect, target(), EmptyPayload(), context()))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert process.calls == []


class _SlowProcess:
    async def run(self, spec, timeout, stdin=None):
        await asyncio.sleep(1)


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


def test_dispatch_fence_runs_after_ownership_revalidation_and_blocks_spawn():
    async def scenario():
        process = _GatedProcess([
            output(json.dumps(owned_status())),
            output(json.dumps(owned_status())),
            output(json.dumps({"ok": True, "state": "idle"})),
        ])
        control = PublishedDoubletakeControl(process, secure_profile())
        assert isinstance(
            await control.handshake("owner-token", InstanceId(UUID(int=9))), Ok,
        )
        process.gate_operation = "status"
        seen = []

        def fence():
            seen.append(len(process.calls))
            return Err(error=diagnostic(ErrorCode.stale_target))

        pending = asyncio.create_task(control.request(
            BackendOperation.disconnect, target(), EmptyPayload(), context(),
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


def test_dispatch_fence_passing_allows_exactly_one_mutating_command():
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status())),
        output(json.dumps({"ok": True, "state": "idle"})),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())
    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)
    seen = []

    def fence():
        seen.append(len(process.calls))
        return Ok(value=None)

    result = asyncio.run(control.request(
        BackendOperation.disconnect, target(), EmptyPayload(), context(),
        dispatch_fence=fence,
    ))

    assert isinstance(result, Ok)
    assert seen == [2]
    assert [call[0].argv[3] for call in process.calls] == ["status", "status", "disconnect"]


def test_failed_ownership_revalidation_never_consults_the_dispatch_fence():
    process = _Process([
        output(json.dumps(owned_status())),
        output(json.dumps(owned_status(instance=10))),
    ])
    control = PublishedDoubletakeControl(process, secure_profile())
    assert isinstance(asyncio.run(control.handshake("owner-token", InstanceId(UUID(int=9)))), Ok)

    def fence():
        raise AssertionError("fence must not run when ownership revalidation failed")

    result = asyncio.run(control.request(
        BackendOperation.disconnect, target(), EmptyPayload(), context(),
        dispatch_fence=fence,
    ))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch
    assert all(call[0].argv[3] != "disconnect" for call in process.calls)


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
    result = asyncio.run(control.request(BackendOperation.mute, receiver_b, MutePayload(muted=True), context()))

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
    result = asyncio.run(control.request(BackendOperation.unmute, receiver_b, MutePayload(muted=False), context()))

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
    result = asyncio.run(control.request(BackendOperation.mute, receiver_b, EmptyPayload(), context()))

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
    result = asyncio.run(control.request(BackendOperation.mute, target(address="192.0.2.11"), MutePayload(muted=True), context()))

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
    result = asyncio.run(control.request(BackendOperation.disconnect, target(address="192.0.2.11"), EmptyPayload(), context()))

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
