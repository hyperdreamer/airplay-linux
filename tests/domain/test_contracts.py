"""The shared boundary must preserve identities, resources, and secret isolation."""

import inspect
import json
import pickle
from dataclasses import FrozenInstanceError, asdict, fields, is_dataclass, replace
from enum import StrEnum
from types import MappingProxyType
from typing import get_args
from uuid import UUID

import pytest

from airplay_linux.domain import identity, messages, operations, ports, records, resources
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    ChallengeId,
    DisplayHandle,
    InstanceId,
    LeaseId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationContext,
    OperationId,
    OwnerId,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    Diagnostic,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    PhysicalMonitorSource,
    RecoveryAction,
    SessionPhase,
    Size,
    StartRequest,
    TransportState,
    TrustState,
    VirtualOutputSource,
)


RECEIVER = ReceiverId("airplay-device:001122334455")


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (Mode, ("mirror", "virtual")),
        (TransportState, ("Idle", "Pairing", "Connecting", "Streaming", "Reconnecting", "Stopping", "Failed")),
        (DisplayState, ("None", "Resolving", "Creating", "Owned", "Reconfiguring", "Removing", "Failed", "CleanupRequired")),
        (AudioState, ("Disabled", "Starting", "Enabled", "Muted", "Unavailable")),
        (TrustState, ("Unknown", "Untrusted", "Trusted", "Pairing", "RevocationPending", "CleanupRequired")),
        (SessionPhase, ("Available", "Offline", "ResolutionRequired", "AudioDecisionRequired", "CreatingDisplay", "Pairing", "Connecting", "Mirroring", "VirtualDisplay", "Reconnecting", "Reconfiguring", "Recovering", "Stopping", "CleanupRequired", "TrustCleanupRequired", "Failed")),
        (IntentStage, ("idle", "starting", "active", "resolution", "audio_decision", "reconfiguring", "recovering", "stopping", "cleanup")),
        (ErrorCode, ("portal_cancelled", "portal_denied", "keyring_unavailable", "unsupported_source", "unsupported_mode", "unsupported_operation", "dependency_missing", "dependency_incompatible", "audio_denied", "audio_unavailable", "audio_mute_failed", "receiver_rejected", "stale_target", "stale_event", "stale_socket", "ownership_mismatch", "helper_failed", "configuration_failed", "cleanup_timeout", "trust_cleanup_required", "resolution_mismatch", "discovery_failed", "transport_failed", "daemon_failed", "persistence_failed", "invalid_input", "pairing_expired")),
        (RecoveryAction, ("retry", "stop", "back", "choose_mode", "video_only", "retry_audio", "retry_cleanup", "retry_revocation", "keep_blocked", "remove_metadata", "recreate", "confirm_remove", "keep")),
        (messages.TransportEventKind, ("ready", "stopped", "network_lost", "pairing_required", "credential_invalid", "audio_start_failed", "audio_failed", "audio_recovered", "mute_result", "accepted_mode", "failed", "daemon_exit")),
    ],
)
def test_serialized_enums_round_trip(enum, values):
    assert issubclass(enum, StrEnum)
    assert tuple(member.value for member in enum) == values
    assert all(enum(value) == value for value in values)
    assert DisplayState.None_.value == "None"


@pytest.mark.parametrize("bad", [0, -1, 16385, 1.5, True, "1920"])
@pytest.mark.parametrize("dimension", ["width", "height"])
def test_size_rejects_invalid_dimensions(dimension, bad):
    dimensions = {"width": 1920, "height": 1080, dimension: bad}
    with pytest.raises((ValueError, TypeError)):
        Size(**dimensions)


def test_size_accepts_boundary_dimensions():
    assert Size(width=1, height=16384).height == 16384
    assert Size(width=16384, height=1).width == 16384


def test_contract_dataclasses_are_frozen_and_keyword_only():
    for module in (identity, resources, operations, records, messages, ports):
        for cls in vars(module).values():
            if not inspect.isclass(cls) or cls.__module__ != module.__name__ or not is_dataclass(cls):
                continue
            assert cls.__dataclass_params__.frozen, cls.__name__
            assert all(field.kw_only for field in fields(cls)), cls.__name__
    size = Size(width=1920, height=1080)
    with pytest.raises(FrozenInstanceError):
        size.width = 1280
    with pytest.raises(TypeError):
        Size(1920, 1080)


def test_id_wrappers_keep_uuid_and_receiver_id_distinct():
    assert ReceiverId.__supertype__ is str
    for identifier in (OperationId, LeaseId, ChallengeId, OwnerId, InstanceId):
        assert identifier.__supertype__ is UUID
        value = UUID(int=1)
        assert identifier(value) == value


def test_resources_are_independent():
    state = SessionSnapshot.idle(ReceiverId('airplay-device:001122334455'))
    state = replace(state, transport=TransportState.Reconnecting,
                    display=DisplayState.Owned, audio=AudioState.Unavailable)
    assert state.display is DisplayState.Owned
    assert state.transport is TransportState.Reconnecting


def test_idle_snapshot_has_no_acquired_resources_or_pending_intent():
    offline = SessionSnapshot.idle(RECEIVER)
    online = SessionSnapshot.idle(RECEIVER, online=True)
    assert offline.phase is SessionPhase.Offline
    assert online.phase is SessionPhase.Available
    assert offline.transport is TransportState.Idle
    assert offline.display is DisplayState.None_
    assert offline.audio is AudioState.Disabled
    assert offline.trust is TrustState.Unknown
    assert offline.intent_stage is IntentStage.idle
    assert offline.intent_active is False
    assert offline.audio_policy == AudioPolicy(enabled=True)
    assert offline.muted is False
    assert offline.session_generation == offline.trust_generation == offline.reconnect_attempt == 0
    assert offline.cleanup_pending is False
    for name in ("target", "mode", "physical_source", "display_handle", "ownership_uuid", "resolution", "observed_resolution", "accepted_resolution", "stream", "challenge", "last_error", "next_retry_at", "operation_id", "transport_lease_id", "audio_operation_id", "queued_start"):
        assert getattr(offline, name) is None, name


def test_failed_transport_can_retain_an_exact_display_handle_and_trust_cleanup():
    source = VirtualOutputSource(connector="Virtual-1", kscreen_output_id="42", size=Size(width=1920, height=1080))
    handle = DisplayHandle(owner_uuid=OwnerId(UUID(int=5)), source=source, helper=ProcessIdentity(pid=321, start_ticks=987))
    state = replace(
        SessionSnapshot.idle(RECEIVER, online=True), mode=Mode.VIRTUAL,
        transport=TransportState.Failed, display=DisplayState.Owned,
        audio=AudioState.Unavailable, trust=TrustState.CleanupRequired,
        display_handle=handle, ownership_uuid=handle.owner_uuid, intent_active=True,
    )
    assert state.display_handle is handle
    assert state.ownership_uuid == OwnerId(UUID(int=5))
    assert state.display is DisplayState.Owned
    assert state.trust is TrustState.CleanupRequired


def test_sources_are_distinct_and_mirror_requests_accept_only_physical_sources():
    physical = PhysicalMonitorSource(connector="DP-1", portal_restore_token=None)
    virtual = VirtualOutputSource(connector="Virtual-1", kscreen_output_id="42", size=Size(width=1920, height=1080))
    assert isinstance(physical, resources.CaptureSource)
    assert isinstance(virtual, resources.CaptureSource)
    assert not isinstance(virtual, PhysicalMonitorSource)
    assert physical.capture_lease_id is None
    request = StartRequest(mode=Mode.MIRROR, source=physical)
    assert request.audio.enabled is True
    assert request.resolution is None
    with pytest.raises(TypeError):
        StartRequest(mode=Mode.MIRROR, source=virtual)


def test_sensitive_input_has_no_printable_value():
    secret = SensitiveText('canary-pin-8496')
    assert '8496' not in repr(secret)
    assert '8496' not in str(secret)
    with pytest.raises(TypeError):
        json.dumps(secret)
    assert secret.reveal_for_backend() == "canary-pin-8496"


@pytest.mark.parametrize("wrapper", [SensitiveText, OpaqueCredentialRef, OpaqueProvisionalCredentialRef, OpaqueBackendToken])
def test_secrets_reject_generic_serialization_and_mutation(wrapper):
    secret = wrapper("canary-private-8496")
    assert "8496" not in str(secret)
    assert "8496" not in repr(secret)
    assert not hasattr(secret, "__dict__")
    with pytest.raises(TypeError):
        json.dumps(secret)
    with pytest.raises(TypeError):
        secret.__getstate__()
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        with pytest.raises(TypeError) as raised:
            pickle.dumps(secret, protocol=protocol)
        assert "8496" not in str(raised.value)
    with pytest.raises((AttributeError, TypeError)):
        secret.value = "replacement"
    with pytest.raises((AttributeError, TypeError)):
        secret.__init__("replacement")


@pytest.mark.parametrize("wrapper", [OpaqueCredentialRef, OpaqueProvisionalCredentialRef, OpaqueBackendToken])
def test_opaque_references_compare_by_private_id_and_reference_kind(wrapper):
    first = wrapper("opaque-one")
    assert first == wrapper("opaque-one")
    assert first != wrapper("opaque-two")
    assert len({first, wrapper("opaque-one")}) == 1
    assert first != "opaque-one"
    other = OpaqueBackendToken if wrapper is not OpaqueBackendToken else OpaqueCredentialRef
    assert first != other("opaque-one")


def challenge_kwargs():
    return dict(
        challenge_id=ChallengeId(UUID(int=1)), receiver_id=RECEIVER,
        endpoint_fingerprint="endpoint-v1", endpoint_generation=1,
        operation_id=OperationId(UUID(int=2)), session_generation=1,
        trust_generation=0, created_at=10.0, expires_at=130.0,
        credential_kind="pin",
    )


def test_public_pairing_view_has_no_backend_token_or_secret_projection():
    private = operations.PairingChallenge(**challenge_kwargs(), backend_token=OpaqueBackendToken("private-token-8496"))
    public = operations.ChallengeView(**challenge_kwargs())
    assert "backend_token" not in {field.name for field in fields(public)}
    assert "8496" not in repr(private)
    assert "8496" not in json.dumps(asdict(public), default=str)
    with pytest.raises(TypeError):
        asdict(private)


def test_stream_handles_include_endpoint_daemon_lease_and_start_operation():
    handle = StreamHandle(
        receiver_id=RECEIVER, endpoint_generation=2, endpoint_fingerprint="endpoint-v2",
        backend_stream_id="stream-a", daemon_instance_uuid=InstanceId(UUID(int=3)),
        lease_id=LeaseId(UUID(int=4)), start_operation_id=OperationId(UUID(int=5)),
    )
    assert handle != replace(handle, daemon_instance_uuid=InstanceId(UUID(int=6)))
    assert handle != replace(handle, lease_id=LeaseId(UUID(int=7)))
    assert handle != replace(handle, endpoint_generation=3)


@pytest.mark.parametrize("bad", [-1, 1.5, True])
@pytest.mark.parametrize("generation", ["session_generation", "endpoint_generation", "trust_generation"])
def test_operation_context_rejects_invalid_generations(generation, bad):
    kwargs = dict(operation_id=OperationId(UUID(int=1)), session_generation=0, endpoint_generation=0, trust_generation=0)
    kwargs[generation] = bad
    with pytest.raises((ValueError, TypeError)):
        OperationContext(**kwargs)


def test_identity_and_event_generations_cannot_be_negative():
    with pytest.raises(ValueError):
        identity.ReceiverTarget(receiver_id=RECEIVER, service_records=(), canonical_host="192.0.2.1", service_port=7000, protocol="airplay", endpoint_fingerprint="v1", endpoint_generation=-1)
    with pytest.raises(ValueError):
        replace(SessionSnapshot.idle(RECEIVER), session_generation=-1)
    with pytest.raises(ValueError):
        operations.ChallengeView(**(challenge_kwargs() | {"trust_generation": -1}))
    with pytest.raises(ValueError):
        messages.EffectEvent(receiver_id=RECEIVER, operation_id=OperationId(UUID(int=1)), session_generation=-1, kind="captured", result=Ok(value=None), resource=None)


def test_mapping_inputs_are_detached_and_deeply_read_only():
    txt = {"model": "AppleTV"}
    service = identity.ServiceRecord(key="tv", service_type="_airplay._tcp", instance="TV", model="AppleTV", device_id=None, host="tv.local", addresses=("192.0.2.10",), port=7000, txt=txt, seen_at=1.0)
    txt["model"] = "changed"
    assert service.txt["model"] == "AppleTV"
    with pytest.raises(TypeError):
        service.txt["model"] = "changed"
    outputs = {"DP-1": {"enabled": True, "x": 0}}
    baseline = records.LayoutSnapshot(outputs=outputs, baseline_hash="hash")
    outputs["DP-1"]["x"] = 100
    assert baseline.outputs["DP-1"]["x"] == 0
    with pytest.raises(TypeError):
        baseline.outputs["DP-1"]["x"] = 100
    env = {"AIRPLAY_LINUX_OWNER_UUID": "owner"}
    spec = ports.CommandSpec(argv=("helper",), env=env)
    env["AIRPLAY_LINUX_OWNER_UUID"] = "changed"
    assert spec.env["AIRPLAY_LINUX_OWNER_UUID"] == "owner"
    assert isinstance(spec.env, MappingProxyType)
    assert spec.cwd is None and spec.pass_fds == ()


def test_typed_result_and_public_event_unions_do_not_need_raw_backend_messages():
    error = Diagnostic(code=ErrorCode.unsupported_operation, message="Operation is unavailable", recovery=(RecoveryAction.back,))
    success = Ok(value=None)
    failure = Err(error=error)
    assert success.value is None
    assert failure.error is error
    assert get_args(resources.Result[int]) == (Ok[int], Err)
    assert error.receiver_id is error.detail is None
    payload = messages.FailurePayload(diagnostic=error)
    assert isinstance(payload, messages.TransportPayload)
    assert not isinstance(payload, messages.EmptyPayload)
    command = messages.SubmitPairing(receiver_id=RECEIVER, challenge_id=ChallengeId(UUID(int=1)), value=SensitiveText("pin-canary-8496"))
    assert isinstance(command, messages.Command)
    assert "8496" not in repr(command)
    assert set(get_args(messages.ResourceEvent)) == {messages.TransportEvent, messages.DisplayEvent, messages.DiscoveryEvent, messages.TimerEvent, messages.EffectEvent}


def test_protocol_boundaries_exist_without_instantiating_adapters():
    expected = {
        "Clock": ("now",), "Scheduler": ("call_at",), "CancelHandle": ("cancel",),
        "IdFactory": ("new",), "Subscription": ("close",),
        "DiscoveryPort": ("start", "refresh", "current", "subscribe", "close"),
        "CapturePort": ("list_physical", "select", "verify", "release"),
        "TransportPort": ("update_target", "query_modes", "start_stream", "stop_stream", "set_muted", "start_audio", "subscribe", "shutdown"),
        "PairingPort": ("begin_pairing", "submit_pairing", "cancel_pairing", "forget_trust", "mark_invalid", "retry_revocation"),
        "CredentialStore": ("load_credential_ref", "prepare_pairing", "commit_pairing", "cancel_pairing", "revoke_pairing", "mark_invalid"),
        "DisplayPort": ("create", "prepare_teardown", "mark_removing", "remove", "reconcile", "approve_orphan", "subscribe"),
        "SessionStore": ("save", "load_all"), "ReceiverStore": ("get", "all", "put", "remove_metadata"),
        "OwnershipStore": ("write", "all", "delete"), "DaemonStore": ("write", "all", "delete"),
        "ActivitySink": ("emit",), "ProcessPort": ("run", "spawn", "identity", "owned", "terminate", "subscribe"),
        "AudioProbe": ("check",), "ManagerPort": ("dispatch", "ingest", "snapshot", "subscribe", "shutdown"),
    }
    for name, methods in expected.items():
        protocol = getattr(ports, name)
        assert protocol._is_protocol, name
        for method in methods:
            assert callable(getattr(protocol, method)), (name, method)
