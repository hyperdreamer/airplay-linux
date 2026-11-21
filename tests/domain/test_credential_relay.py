"""Signed-off Start-bound credential relay contracts."""

from __future__ import annotations

import ast
import inspect
import pickle
from dataclasses import FrozenInstanceError, fields, replace
from types import MappingProxyType
from pathlib import Path
from typing import get_args
from uuid import UUID

import pytest

from airplay_linux.domain import messages, ports, records, relay, resources
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import ChallengeId, ChallengeView, InstanceId, LeaseId, OperationContext, OperationId, SensitiveText
from airplay_linux.domain.resources import AudioPolicy, Err, Ok, VolumeLevel
from airplay_linux.testing.builders import target

TARGET = target("a")
OWNER_LEASE = LeaseId(UUID(int=1))
DAEMON = InstanceId(UUID(int=2))
START = OperationId(UUID(int=3))
CHALLENGE = ChallengeId(UUID(int=4))
SUBMISSION = OperationId(UUID(int=5))
CANARY = "relay-sensitive-canary-7d22"


def require(module, name):
    assert hasattr(module, name), f"missing domain contract {module.__name__}.{name}"
    return getattr(module, name)


def request_for(**changes):
    options = relay.RelayStartOptions(
        target_volume_proven=True,
        audio=AudioPolicy(enabled=True),
        initial_volume=VolumeLevel(value=30),
    )
    values = {
        "receiver_id": TARGET.receiver_id,
        "target": TARGET,
        "credential_kind": "pin",
        "start_operation_id": START,
        "endpoint_generation": TARGET.endpoint_generation,
        "session_generation": 7,
        "owner_lease": OWNER_LEASE,
        "daemon_instance_uuid": DAEMON,
        "generation": 2,
        "observed_at": 123.0,
        "start_options": options,
    }
    values.update(changes)
    return relay.CredentialRequest(**values)


def challenge_for(request):
    return ChallengeView(
        challenge_id=CHALLENGE,
        receiver_id=request.receiver_id,
        endpoint_fingerprint=request.target.endpoint_fingerprint,
        endpoint_generation=request.endpoint_generation,
        operation_id=request.start_operation_id,
        session_generation=request.session_generation,
        trust_generation=9,
        created_at=request.observed_at,
        expires_at=request.observed_at + 60.0,
        credential_kind=request.credential_kind,
    )


def owner_for(request):
    return relay.CredentialEffectOwner(
        challenge_id=CHALLENGE,
        receiver_id=request.receiver_id,
        target=request.target,
        start_operation_id=request.start_operation_id,
        endpoint_fingerprint=request.target.endpoint_fingerprint,
        endpoint_generation=request.endpoint_generation,
        session_generation=request.session_generation,
        trust_generation=9,
        owner_lease=request.owner_lease,
        daemon_instance_uuid=request.daemon_instance_uuid,
        relay_generation=request.generation,
        marker_generation=3,
    )


def test_no_reply_reason_is_exact():
    assert get_args(resources.NoReplyReason) == (
        "command_timeout", "service_shutdown", "child_cleanup_uncertain",
        "incomplete_reply", "malformed_reply", "dispatch_cancelled",
    )


def test_representability_is_the_exact_profile_independent_ast_predicate():
    predicate = relay.relay_credential_representable
    assert tuple(inspect.signature(predicate).parameters) == ("value",)
    tree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
    definition = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "relay_credential_representable")
    body = definition.body[1:] if ast.get_docstring(definition) else definition.body
    expected = ast.parse('return not value.startswith("-")').body
    assert [ast.dump(node, include_attributes=False) for node in body] == [ast.dump(node, include_attributes=False) for node in expected]
    assert [predicate(value) for value in ("-secret", "--audio", "--volume", "-")] == [False] * 4
    assert predicate("ordinary") is True


def test_request_and_prompt_are_start_bound_and_frozen():
    request = request_for()
    prompt = relay.CredentialPromptState(
        request=request, challenge=challenge_for(request), state="editing",
        submission_id=None, report=None,
    )
    assert (request.receiver_id, request.start_operation_id, request.credential_kind) == (TARGET.receiver_id, START, "pin")
    assert tuple(field.name for field in fields(request)) == (
        "receiver_id", "target", "credential_kind", "start_operation_id",
        "endpoint_generation", "session_generation", "owner_lease",
        "daemon_instance_uuid", "generation", "observed_at", "start_options",
    )
    assert tuple(field.name for field in fields(prompt)) == ("request", "challenge", "state", "submission_id", "report")
    with pytest.raises(FrozenInstanceError):
        prompt.state = "submitting"


def test_request_lifecycle_payload_has_live_and_row_ended():
    request = request_for()
    live = messages.CredentialRequestPayload(request=request, lifecycle="live", reason=None)
    ended = messages.CredentialRequestPayload(request=request, lifecycle="ended", reason="row_ended")
    assert live.reason is None and ended.reason == "row_ended"
    with pytest.raises(ValueError):
        messages.CredentialRequestPayload(request=request, lifecycle="ended", reason=None)
    with pytest.raises(ValueError):
        messages.CredentialRequestPayload(request=request, lifecycle="live", reason="row_ended")
    assert messages.CredentialRequestPayload in get_args(messages.TransportPayload)
    assert messages.TransportEventKind.credential_request.value == "credential_request"
    context = OperationContext(
        operation_id=request.start_operation_id,
        session_generation=request.session_generation,
        endpoint_generation=request.endpoint_generation,
        transport_lease_id=request.owner_lease,
        trust_generation=9,
    )
    event = messages.TransportEvent(
        kind=messages.TransportEventKind.credential_request,
        receiver_id=request.receiver_id,
        stream=None,
        endpoint_fingerprint=request.target.endpoint_fingerprint,
        endpoint_generation=request.endpoint_generation,
        daemon_instance_uuid=request.daemon_instance_uuid,
        context=context,
        payload=live,
    )
    assert event.payload is live
    with pytest.raises(ValueError, match="credential request identity"):
        replace(event, context=replace(context, operation_id=OperationId(UUID(int=99))))


def test_new_commands_are_closed_and_routeable_shapes():
    shapes = {
        messages.SubmitCredential: ("receiver_id", "challenge_id", "value"),
        messages.DismissCredentialPrompt: ("receiver_id", "challenge_id"),
        messages.StopCredentialRecovery: ("receiver_id", "marker_generation"),
        messages.DismissCredentialRecovery: ("receiver_id", "marker_generation"),
        messages.RestartReceiverToRelease: ("receiver_id", "marker_generation"),
    }
    for command_type, expected in shapes.items():
        assert tuple(field.name for field in fields(command_type)) == expected
        assert command_type in get_args(messages.Command)
        assert command_type.__dataclass_params__.frozen
    command = messages.SubmitCredential(
        receiver_id=TARGET.receiver_id, challenge_id=CHALLENGE, value=SensitiveText(CANARY),
    )
    assert command.value.reveal_for_backend() == CANARY
    assert CANARY not in repr(command) and CANARY not in str(command)
    with pytest.raises(TypeError) as raised:
        pickle.dumps(command)
    assert CANARY not in str(raised.value)


def test_submission_records_and_pending_cleanup_owner_are_exact_and_frozen():
    request = request_for()
    owner = owner_for(request)
    submitted = relay.Submitted(reply_at=124.0, accepted=False, post_dispatch_error=None)
    no_reply = relay.NoReply(reason="command_timeout")
    settlement = relay.CredentialSubmissionSettlement(
        submission_id=SUBMISSION, read_id=None, owner=owner, marker_generation=3,
        stop_operation_id=None, report_suppressed=False, phase="awaiting_reply",
    )
    pending = relay.PendingStartCleanupOwner(
        receiver_id=request.receiver_id, target=request.target,
        start_operation_id=request.start_operation_id,
        endpoint_fingerprint=request.target.endpoint_fingerprint,
        endpoint_generation=request.endpoint_generation,
        session_generation=request.session_generation,
        trust_generation=9, owner_lease=request.owner_lease,
        daemon_instance_uuid=request.daemon_instance_uuid,
        stop_operation_id=OperationId(UUID(int=7)),
        credential_submission_id=SUBMISSION, marker_generation=3,
    )
    assert tuple(field.name for field in fields(submitted)) == ("reply_at", "accepted", "post_dispatch_error")
    assert tuple(field.name for field in fields(no_reply)) == ("reason",)
    assert tuple(field.name for field in fields(owner)) == (
        "challenge_id", "receiver_id", "target", "start_operation_id", "endpoint_fingerprint",
        "endpoint_generation", "session_generation", "trust_generation", "owner_lease",
        "daemon_instance_uuid", "relay_generation", "marker_generation",
    )
    assert tuple(field.name for field in fields(settlement)) == (
        "submission_id", "read_id", "owner", "marker_generation", "stop_operation_id",
        "report_suppressed", "phase",
    )
    assert tuple(field.name for field in fields(pending)) == (
        "receiver_id", "target", "start_operation_id", "endpoint_fingerprint",
        "endpoint_generation", "session_generation", "trust_generation", "owner_lease",
        "daemon_instance_uuid", "stop_operation_id", "credential_submission_id",
        "marker_generation",
    )
    assert all(record.__dataclass_params__.frozen for record in (submitted, no_reply, owner, settlement, pending))


def test_effect_completions_repeat_the_full_owner_tuple():
    request = request_for()
    owner = owner_for(request)
    outcome = relay.Submitted(reply_at=124.0, accepted=True, post_dispatch_error=None)
    completion = messages.CredentialSubmissionCompletion(
        submission_id=SUBMISSION, challenge_id=owner.challenge_id,
        receiver_id=owner.receiver_id, target=owner.target,
        start_operation_id=owner.start_operation_id,
        endpoint_fingerprint=owner.endpoint_fingerprint,
        endpoint_generation=owner.endpoint_generation,
        session_generation=owner.session_generation,
        trust_generation=owner.trust_generation, owner_lease=owner.owner_lease,
        daemon_instance_uuid=owner.daemon_instance_uuid,
        relay_generation=owner.relay_generation, marker_generation=owner.marker_generation,
        outcome=outcome,
    )
    assert tuple(field.name for field in fields(completion)) == (
        "submission_id", "challenge_id", "receiver_id", "target", "start_operation_id",
        "endpoint_fingerprint", "endpoint_generation", "session_generation", "trust_generation",
        "owner_lease", "daemon_instance_uuid", "relay_generation", "marker_generation", "outcome",
    )
    assert tuple(field.name for field in fields(messages.CredentialStatusCompletion)) == (
        "read_id", "challenge_id", "receiver_id", "target", "start_operation_id",
        "endpoint_fingerprint", "endpoint_generation", "session_generation", "trust_generation",
        "owner_lease", "daemon_instance_uuid", "relay_generation", "marker_generation", "result",
    )
    callback = messages.EffectEvent(
        receiver_id=request.receiver_id, operation_id=SUBMISSION,
        session_generation=request.session_generation, kind="credential_submit",
        result=Ok(value=completion), resource=None,
    )
    assert CANARY not in repr(callback) and CANARY not in str(callback)


def test_transport_protocol_has_named_relay_methods():
    expected = {
        "submit_credential": ("target", "value", "context", "dispatch_fence"),
        "read_credential_status": ("target", "context"),
        "stop_pending_start": ("owner", "stop_context"),
    }
    for name, parameters in expected.items():
        method = getattr(ports.TransportPort, name)
        assert tuple(inspect.signature(method).parameters)[1:] == parameters
    assert inspect.signature(ports.TransportPort.submit_credential).return_annotation == "Result[Submitted | NoReply]"


def test_prompt_is_removed_from_safe_snapshot_and_codec():
    request = request_for()
    prompt = relay.CredentialPromptState(
        request=request, challenge=challenge_for(request), state="editing",
        submission_id=None, report=None,
    )
    snapshot = replace(records.SessionSnapshot.idle(request.receiver_id), credential_prompt=prompt)
    from airplay_linux.session.persistence import _safe_snapshot
    from airplay_linux.storage.codecs import _SESSION_FIELDS, decode_session, encode_session
    safe = _safe_snapshot(snapshot)
    assert safe.credential_prompt is None
    encoded = encode_session(snapshot)
    assert CANARY.encode() not in encoded
    assert decode_session(encoded).credential_prompt is None
    assert "credential_prompt" not in {name for name, _codec in _SESSION_FIELDS}


def test_transactional_pairing_contract_remains_unchanged():
    command = messages.SubmitPairing(
        receiver_id=TARGET.receiver_id, challenge_id=CHALLENGE, value=SensitiveText(CANARY),
    )
    assert messages.SubmitPairing in get_args(messages.Command)
    assert messages.RelaySubmitCompletion.__annotations__["result"] == "PairingResult"
    assert CANARY not in repr(command)




@pytest.mark.parametrize("indices", [(-1,), (2,), (True,), (1, 1), None, [1], {1}, "1", (1.0,), ("1",)])
def test_command_secret_indices_must_be_valid_integer_tuple(indices):
    with pytest.raises(ValueError, match="^invalid secret indices$"):
        ports.CommandSpec(argv=("ctl", "value"), env={}, secret_indices=indices)


def test_command_secret_indices_accept_populated_positions_and_redact_all():
    command = ports.CommandSpec(
        argv=("doubletake-ctl", "connect", "target", CANARY, "tail"),
        env={}, secret_indices=(0, 3),
    )
    assert command.argv[3] == CANARY
    assert command.redacted_argv == (
        "<redacted>", "connect", "target", "<redacted>", "tail",
    )
    assert CANARY not in repr(command)
    assert CANARY not in repr({"spec": command})
    assert CANARY not in repr([command, command])


def test_command_env_is_detached_and_frozen():
    env = {"AIRPLAY_LINUX_OWNER_UUID": "owner"}
    command = ports.CommandSpec(argv=("helper",), env=env)
    env["AIRPLAY_LINUX_OWNER_UUID"] = "changed"
    assert command.env["AIRPLAY_LINUX_OWNER_UUID"] == "owner"
    assert isinstance(command.env, MappingProxyType)
    with pytest.raises(FrozenInstanceError):
        command.secret_indices = (0,)


def test_process_protocol_has_secret_safe_credential_result_variants():
    assert tuple(field.name for field in fields(ports.CredentialProcessNotSpawned)) == ("diagnostic",)
    assert tuple(field.name for field in fields(ports.CredentialProcessCompleted)) == ("output",)
    assert tuple(field.name for field in fields(ports.CredentialProcessUncertain)) == ("reason",)
    output = ports.CredentialProcessCompleted(
        output=ports.CommandOutput(exit_code=0, stdout=CANARY.encode(), stderr=b""),
    )
    assert "stdout" not in repr(output) and CANARY not in repr(output)
    method = inspect.signature(ports.ProcessPort.run_credential)
    assert tuple(method.parameters) == ("self", "spec", "timeout", "dispatch_fence")
    assert method.return_annotation == "CredentialProcessResult"
    assert get_args(ports.CredentialProcessResult) == (
        ports.CredentialProcessNotSpawned,
        ports.CredentialProcessCompleted,
        ports.CredentialProcessUncertain,
    )
    with pytest.raises(TypeError, match="credential process output cannot be serialized"):
        pickle.dumps(output)


def test_relay_module_has_no_runtime_layer_imports_or_secret_fields():
    tree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
    forbidden = (
        "asyncio", "PySide6", "qasync", "airplay_linux.transport",
        "airplay_linux.process", "airplay_linux.storage", "airplay_linux.backend",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module]
        else:
            continue
        assert not any(
            module == root or module.startswith(root + ".")
            for module in names for root in forbidden
        )
    for name in (
        "CredentialRequest", "CredentialEffectOwner", "CredentialPromptState",
        "CredentialSubmissionSettlement", "PendingStartCleanupOwner", "Submitted", "NoReply",
    ):
        assert "SensitiveText" not in {field.name for field in fields(require(relay, name))}
