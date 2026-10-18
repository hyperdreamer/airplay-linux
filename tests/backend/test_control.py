"""Control adapter tests for published and application-contract profiles."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from airplay_linux.backend.control import (
    BackendOperation,
    ConnectPayload,
    EmptyPayload,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
    StatusReply,
    UnsupportedControl,
)
from airplay_linux.domain.operations import InstanceId, OperationContext, OperationId
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.testing.builders import target


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


def output(payload, *, exit_code=0, stderr=b""):
    if isinstance(payload, str):
        payload = payload.encode()
    return Ok(value=CommandOutput(exit_code=exit_code, stdout=payload, stderr=stderr))


def context():
    return OperationContext(
        operation_id=OperationId(UUID(int=1)),
        session_generation=1,
        endpoint_generation=1,
    )


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


def test_control_decodes_status_reply_without_exposing_backend_error_text():
    process = _Process([
        output(json.dumps({
            "ok": True,
            "state": "streaming",
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
