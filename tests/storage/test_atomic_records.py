"""Atomic publication, versioned codecs, and continuity-aware preferences.

The design's recovery protocol rests on one property: an interrupted publication
leaves the previous complete record or the new complete record, never a
truncated document a later startup could read as an authoritative reservation.
So every durable step -- temporary write, file ``fsync``, rename, directory
``fsync`` -- is exercised with an injected operating-system failure, and the
record on disk is inspected afterwards.

The rest of the file pins the same boundary from the reading side: a malformed,
truncated, or unknown-version document is quarantined as a diagnostic and left
on disk as evidence, a record path can never leave the private root or travel
through a symlink, and a saved preference is only usable while the identity's
continuity still justifies it.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverRecordSet
from airplay_linux.domain.operations import (
    ChallengeId,
    ChallengeView,
    InstanceId,
    LeaseId,
    OperationId,
    OwnerId,
    ProcessIdentity,
    StreamHandle,
)
from airplay_linux.domain.records import (
    DaemonRecord,
    LayoutSnapshot,
    OwnershipRecord,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    PhysicalMonitorSource,
    ResolutionDecision,
    ResolutionEvidence,
    SessionPhase,
    Size,
    StartRequest,
    TransportState,
    TrustState,
)
from airplay_linux.storage import (
    AtomicFiles,
    JsonDaemonStore,
    JsonOwnershipStore,
    JsonReceiverStore,
    JsonSessionStore,
    PosixFilesystem,
    PreferenceAccess,
    RecordError,
    StorageError,
    decode_daemon,
    decode_ownership,
    decode_receiver,
    decode_session,
    encode_daemon,
    encode_ownership,
    encode_receiver,
    encode_session,
    is_ambiguous_identity,
    state_root,
)
from airplay_linux.testing.builders import target, virtual_handle

DIGEST = hashlib.sha256(b"one-record-set").hexdigest()
AMBIGUOUS = ReceiverId(f"airplay-ambiguous:{DIGEST}")
FINGERPRINT = ReceiverId(f"airplay-fingerprint:{DIGEST}")
OTHER = ReceiverId("airplay-device:b")
SESSION_NAME = "airplay-device%3Aa.json"


def run(awaitable):
    return asyncio.run(awaitable)


# --------------------------------------------------------------------------- fixtures


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def files_at(root: Path, filesystem: PosixFilesystem | None = None) -> AtomicFiles:
    if filesystem is None:
        return AtomicFiles(root=root)
    return AtomicFiles(root=root, filesystem=filesystem)


def rich_snapshot() -> SessionSnapshot:
    t = target()
    return SessionSnapshot(
        receiver_id=t.receiver_id,
        target=t,
        online=True,
        mode=Mode.VIRTUAL,
        intent_active=True,
        intent_stage=IntentStage.active,
        phase=SessionPhase.VirtualDisplay,
        transport=TransportState.Streaming,
        display=DisplayState.Owned,
        audio=AudioState.Muted,
        trust=TrustState.Trusted,
        physical_source=PhysicalMonitorSource(connector="DP-1", portal_restore_token=None),
        display_handle=virtual_handle(),
        ownership_uuid=OwnerId(UUID(int=1)),
        resolution=ResolutionDecision(
            size=Size(width=1920, height=1080),
            source="stored",
            evidence=ResolutionEvidence(
                origin="remembered",
                reliable=True,
                receiver_id=t.receiver_id,
                endpoint_fingerprint=t.endpoint_fingerprint,
            ),
        ),
        observed_resolution=Size(width=1920, height=1080),
        accepted_resolution=Size(width=1600, height=900),
        audio_policy=AudioPolicy(enabled=False),
        muted=True,
        stream=StreamHandle(
            receiver_id=t.receiver_id,
            endpoint_generation=t.endpoint_generation,
            endpoint_fingerprint=t.endpoint_fingerprint,
            backend_stream_id="stream-1",
            daemon_instance_uuid=InstanceId(UUID(int=9)),
            lease_id=LeaseId(UUID(int=12)),
            start_operation_id=OperationId(UUID(int=11)),
        ),
        last_error=diagnostic(ErrorCode.transport_failed, t.receiver_id),
        reconnect_attempt=2,
        next_retry_at=12.5,
        operation_id=OperationId(UUID(int=11)),
        session_generation=3,
        transport_lease_id=LeaseId(UUID(int=12)),
        trust_generation=1,
        audio_operation_id=OperationId(UUID(int=13)),
        queued_start=StartRequest(
            mode=Mode.MIRROR,
            source=PhysicalMonitorSource(connector="HDMI-A-1", portal_restore_token=None),
            resolution=None,
            audio=AudioPolicy(),
        ),
        cleanup_pending=True,
    )


def ownership(**overrides: object) -> OwnershipRecord:
    t = target()
    defaults: dict[str, object] = {
        "schema_version": 1,
        "owner_uuid": OwnerId(UUID(int=7)),
        "app_instance_uuid": InstanceId(UUID(int=1)),
        "runtime_directory": (
            "/run/user/1000/airplay-linux/instances/00000000-0000-0000-0000-000000000001"
        ),
        "creation_operation_id": OperationId(UUID(int=8)),
        "receiver_id": t.receiver_id,
        "endpoint_fingerprint": t.endpoint_fingerprint,
        "desired_state": "active",
        "stage": "published",
        "requested_size": Size(width=1920, height=1080),
        "observed_size": Size(width=1920, height=1080),
        "helper": ProcessIdentity(pid=4001, start_ticks=90001),
        "connector": "Virtual-v1",
        "output_id": "v1",
        "created_utc": "2026-10-17T00:00:00Z",
        "last_configuration_step": "configured",
        "baseline": LayoutSnapshot(
            outputs={"DP-1": {"enabled": True, "x": 0}},
            baseline_hash="layout-1",
        ),
        "last_applied": {"Virtual-v1": {"enabled": True}},
        "provider_version": "fake-kscreen-1",
        "capability_fingerprint": "capability-1",
    }
    return OwnershipRecord(**(defaults | overrides))


def daemon(**overrides: object) -> DaemonRecord:
    defaults: dict[str, object] = {
        "schema_version": 1,
        "instance_uuid": InstanceId(UUID(int=9)),
        "owner_token": "owner-token-1",
        "app": ProcessIdentity(pid=3001, start_ticks=80001),
        "supervisor": ProcessIdentity(pid=3002, start_ticks=80002),
        "daemon": ProcessIdentity(pid=3003, start_ticks=80003),
        "protocol_version": "doubletake-0.5",
        "socket_path": (
            "/run/user/1000/airplay-linux/instances/"
            "00000000-0000-0000-0000-000000000009/doubletake.sock"
        ),
        "lifecycle": "running",
        "runtime_directory": (
            "/run/user/1000/airplay-linux/instances/00000000-0000-0000-0000-000000000009"
        ),
    }
    return DaemonRecord(**(defaults | overrides))


def saved(**overrides: object) -> SavedReceiver:
    t = target()
    defaults: dict[str, object] = {
        "receiver_id": t.receiver_id,
        "service_metadata": t.service_records,
        "display_name": "Living Room",
        "last_endpoint": t,
        "continuity_fingerprint": t.endpoint_fingerprint,
        "credential_ref": None,
        "remembered_resolution": ResolutionDecision(
            size=Size(width=1920, height=1080),
            source="user",
            evidence=ResolutionEvidence(
                origin="picker",
                reliable=False,
                receiver_id=t.receiver_id,
                endpoint_fingerprint=None,
            ),
        ),
    }
    return SavedReceiver(**(defaults | overrides))


def record_set(**overrides: object) -> ReceiverRecordSet:
    t = target()
    defaults: dict[str, object] = {
        "records": t.service_records,
        "receiver_id": t.receiver_id,
        "identity_kind": "device",
        "canonical_address": t.canonical_host,
        "endpoint_fingerprint": t.endpoint_fingerprint,
        "endpoint_generation": t.endpoint_generation,
        "persistable": True,
        "continuity_verified": True,
        "replacement_of": None,
        "ambiguous": False,
    }
    return ReceiverRecordSet(**(defaults | overrides))


# --------------------------------------------------------------------------- atomic files


def test_atomic_write_publishes_a_private_complete_record(tmp_path: Path):
    files = files_at(tmp_path / "state")
    files.write("sessions/receiver-a.json", b"first payload")
    assert files.read("sessions/receiver-a.json") == b"first payload"
    assert mode_of(tmp_path / "state") == 0o700
    assert mode_of(tmp_path / "state" / "sessions") == 0o700
    assert mode_of(tmp_path / "state" / "sessions" / "receiver-a.json") == 0o600
    assert files.names("sessions") == ("receiver-a.json",)


def test_an_atomic_write_replaces_the_previous_record_completely(tmp_path: Path):
    files = files_at(tmp_path / "state")
    files.write("sessions/receiver-a.json", b"a much longer previous payload")
    files.write("sessions/receiver-a.json", b"short")
    assert files.read("sessions/receiver-a.json") == b"short"
    assert files.names("sessions") == ("receiver-a.json",)


def test_reading_an_absent_record_returns_nothing(tmp_path: Path):
    files = files_at(tmp_path / "state")
    assert files.read("sessions/missing.json") is None
    assert files.names("sessions") == ()


def test_delete_is_idempotent_and_removes_only_the_named_record(tmp_path: Path):
    files = files_at(tmp_path / "state")
    files.write("ownership/one.json", b"one")
    files.write("ownership/two.json", b"two")
    files.delete("ownership/one.json")
    assert files.read("ownership/one.json") is None
    assert files.read("ownership/two.json") == b"two"
    files.delete("ownership/one.json")
    assert files.names("ownership") == ("two.json",)


def test_a_delete_reports_its_own_step_and_removes_the_exact_record(tmp_path: Path):
    # An unlink is not durable until the directory-entry change is fsynced, so a
    # delete runs the same two-step protocol as a write and reports which half
    # failed. A caller confirms a resource is released from that answer.
    files = files_at(tmp_path / "state")
    files.write("ownership/one.json", b"one")
    with pytest.raises(StorageError) as raised:
        files_at(tmp_path / "state", FaultyFilesystem("directory_fsync")).delete(
            "ownership/one.json",
        )
    assert raised.value.step == "directory_fsync"
    # The unlink happened before the fsync, so the record is gone but the
    # directory entry change is not yet durable -- reported as uncertain.
    assert files.read("ownership/one.json") is None


def test_a_delete_reports_a_failed_unlink(tmp_path: Path):
    files = files_at(tmp_path / "state")
    files.write("ownership/one.json", b"one")
    with pytest.raises(StorageError) as raised:
        files_at(tmp_path / "state", FaultyFilesystem("delete")).delete("ownership/one.json")
    assert raised.value.step == "delete"
    assert files.read("ownership/one.json") == b"one"


class FaultyFilesystem(PosixFilesystem):
    """Real atomic publication with exactly one injected operating-system failure."""

    def __init__(self, step: str) -> None:
        self.step = step
        self.attempts: list[str] = []

    def _maybe_fail(self, step: str) -> None:
        self.attempts.append(step)
        if step == self.step:
            raise OSError(errno.EIO, "scripted atomic write failure")

    def write(self, path: Path, payload: bytes) -> None:
        self._maybe_fail("write")
        super().write(path, payload)

    def fsync(self, path: Path) -> None:
        self._maybe_fail("fsync")
        super().fsync(path)

    def replace(self, source: Path, destination: Path) -> None:
        self._maybe_fail("rename")
        super().replace(source, destination)

    def fsync_directory(self, directory: Path) -> None:
        self._maybe_fail("directory_fsync")
        super().fsync_directory(directory)

    def unlink(self, path: Path) -> None:
        self._maybe_fail("delete")
        super().unlink(path)


@pytest.mark.parametrize("step", ["write", "fsync", "rename", "directory_fsync"])
def test_a_failed_atomic_step_never_publishes_a_partial_record(tmp_path: Path, step: str):
    root = tmp_path / "state"
    old = encode_receiver(saved(display_name="Old name"))
    new = encode_receiver(saved(display_name="New name"))
    # The directories already exist, so the injected failure lands on the
    # publication itself rather than on the very first directory creation.
    files_at(root).write("receivers/receiver.json", old)

    faulty = FaultyFilesystem(step)
    with pytest.raises(StorageError) as raised:
        files_at(root, faulty).write("receivers/receiver.json", new)
    assert raised.value.step == step
    assert step in faulty.attempts

    # Whatever survived is a *complete* record, and it is the one the reached
    # steps imply: the rename is the publication point.
    recorded = files_at(root).read("receivers/receiver.json")
    assert recorded == (old if step in {"write", "fsync", "rename"} else new)
    if recorded == new:
        assert decode_receiver(recorded).display_name == "New name"
    else:
        assert decode_receiver(recorded).display_name == "Old name"
    # No visible temporary artifact is left for a later startup to trip over.
    assert files_at(root).names("receivers") == ("receiver.json",)


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.json",
        "/etc/passwd",
        "sessions/../../escape.json",
        "sessions//double.json",
        "",
        ".hidden.json",
        "sessions/./dot.json",
        "sessions/escape.json\0",
    ],
)
def test_a_record_path_cannot_leave_the_private_root(tmp_path: Path, relative: str):
    root = tmp_path / "state"
    files = files_at(root)
    with pytest.raises(StorageError) as raised:
        files.write(relative, b"payload")
    assert raised.value.step == "path"
    assert list(tmp_path.rglob("*.json")) == []


def test_a_symlinked_root_is_followed_deliberately(tmp_path: Path):
    # A dotfile directory linked elsewhere is ordinary configuration; the records
    # still land in the real directory, and every check below runs against it.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    files = files_at(link)
    files.write("sessions/one.json", b"payload")
    assert (real / "sessions" / "one.json").read_bytes() == b"payload"
    assert files.read("sessions/one.json") == b"payload"
    assert mode_of(real / "sessions") == 0o700


def test_a_symlinked_directory_is_never_written_through(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "state"
    root.mkdir()
    (root / "sessions").symlink_to(outside, target_is_directory=True)
    files = files_at(root)
    with pytest.raises(StorageError) as raised:
        files.write("sessions/receiver.json", b"payload")
    assert raised.value.step == "path"
    assert list(outside.iterdir()) == []


def test_a_symlinked_record_is_neither_written_nor_read(tmp_path: Path):
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"unrelated bytes")
    root = tmp_path / "state"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "receiver.json").symlink_to(outside)
    files = files_at(root)
    with pytest.raises(StorageError) as raised:
        files.write("sessions/receiver.json", b"payload")
    assert raised.value.step == "path"
    with pytest.raises(StorageError) as read_back:
        files.read("sessions/receiver.json")
    assert read_back.value.step == "path"
    assert outside.read_bytes() == b"unrelated bytes"


# --------------------------------------------------------------------------- codecs


def test_a_complete_session_record_round_trips():
    snapshot = rich_snapshot()
    assert decode_session(encode_session(snapshot)) == snapshot


def test_a_minimal_session_record_round_trips():
    snapshot = SessionSnapshot.idle(target().receiver_id)
    assert decode_session(encode_session(snapshot)) == snapshot


def test_equal_records_encode_to_identical_bytes_regardless_of_mapping_order():
    first = LayoutSnapshot(
        outputs={"DP-1": {"x": 0, "enabled": True}, "HDMI-A-1": {"enabled": False}},
        baseline_hash="hash",
    )
    second = LayoutSnapshot(
        outputs={"HDMI-A-1": {"enabled": False}, "DP-1": {"enabled": True, "x": 0}},
        baseline_hash="hash",
    )
    assert encode_ownership(ownership(baseline=first)) == encode_ownership(
        ownership(baseline=second),
    )


def test_the_session_record_keeps_the_connector_and_drops_transient_capture_state():
    t = target()
    snapshot = replace(
        SessionSnapshot.idle(t.receiver_id),
        physical_source=PhysicalMonitorSource(
            connector="DP-1",
            portal_restore_token="portal-token-canary-8496",
            capture_lease_id=LeaseId(UUID(int=99)),
        ),
    )
    payload = encode_session(snapshot)
    decoded = decode_session(payload)
    assert decoded.physical_source == PhysicalMonitorSource(
        connector="DP-1",
        portal_restore_token=None,
    )
    assert decoded.physical_source is not None
    assert decoded.physical_source.capture_lease_id is None
    assert b"portal-token-canary-8496" not in payload
    assert str(UUID(int=99)).encode() not in payload


def test_the_session_record_does_not_persist_a_live_pairing_challenge():
    t = target()
    challenge = ChallengeView(
        challenge_id=ChallengeId(UUID(int=21)),
        receiver_id=t.receiver_id,
        endpoint_fingerprint=t.endpoint_fingerprint,
        endpoint_generation=t.endpoint_generation,
        operation_id=OperationId(UUID(int=22)),
        session_generation=1,
        trust_generation=0,
        created_at=10.0,
        expires_at=130.0,
        credential_kind="pin",
    )
    snapshot = replace(
        SessionSnapshot.idle(t.receiver_id), trust=TrustState.Pairing, challenge=challenge,
    )
    payload = encode_session(snapshot)
    assert b"challenge" not in payload
    assert b"credential_kind" not in payload
    assert decode_session(payload).challenge is None


def test_a_recorded_error_is_rebuilt_from_the_catalog():
    t = target()
    snapshot = replace(
        SessionSnapshot.idle(t.receiver_id),
        last_error=diagnostic(ErrorCode.persistence_failed, t.receiver_id),
    )
    decoded = decode_session(encode_session(snapshot))
    assert decoded.last_error == diagnostic(ErrorCode.persistence_failed, t.receiver_id)


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"", "malformed_json"),
        (b"{", "malformed_json"),
        (b"not json at all", "malformed_json"),
        (b"\xff\xfe", "malformed_json"),
        (b"[]", "not_an_object"),
        (b'"text"', "not_an_object"),
        (b'{"schema": "airplay-linux/other", "version": 1}', "unknown_schema"),
    ],
)
def test_a_malformed_payload_is_rejected_with_a_reason(payload: bytes, reason: str):
    with pytest.raises(RecordError) as raised:
        decode_session(payload)
    assert raised.value.reason == reason


def test_an_unknown_record_version_is_rejected():
    document = json.loads(encode_session(SessionSnapshot.idle(target().receiver_id)))
    document["version"] = 2
    with pytest.raises(RecordError) as raised:
        decode_session(json.dumps(document).encode())
    assert raised.value.reason == "unknown_version"


def test_an_extra_field_is_rejected_rather_than_ignored():
    document = json.loads(encode_session(SessionSnapshot.idle(target().receiver_id)))
    document["guessed_resolution"] = {"width": 1920, "height": 1080}
    with pytest.raises(RecordError) as raised:
        decode_session(json.dumps(document).encode())
    assert raised.value.reason == "unknown_field"


def test_a_withheld_field_cannot_be_reintroduced_by_a_written_record():
    document = json.loads(encode_session(SessionSnapshot.idle(target().receiver_id)))
    document["challenge"] = {"credential_kind": "pin"}
    with pytest.raises(RecordError) as raised:
        decode_session(json.dumps(document).encode())
    assert raised.value.reason == "unknown_field"


def test_a_missing_field_is_rejected_rather_than_defaulted():
    document = json.loads(encode_session(SessionSnapshot.idle(target().receiver_id)))
    del document["session_generation"]
    with pytest.raises(RecordError) as raised:
        decode_session(json.dumps(document).encode())
    assert raised.value.reason == "missing_field"


@pytest.mark.parametrize(
    "mutation",
    [
        {"transport": "Streaming-ish"},
        {"display": "Owned?"},
        {"intent_stage": "later"},
        {"session_generation": -1},
        {"session_generation": True},
        {"session_generation": "3"},
        {"receiver_id": ""},
        {"receiver_id": 5},
        {"operation_id": "not-a-uuid"},
        {"muted": 1},
        {"next_retry_at": "soon"},
        {"target": {"receiver_id": "airplay-device:a"}},
        {"resolution": {"size": {"width": 0, "height": 1080}}},
        {"last_error": {"code": "not_a_code"}},
        {"last_error": {"code": "transport_failed", "message": "backend text"}},
        {"queued_start": {"mode": "mirror", "source": {"kscreen_output_id": "v1"}}},
        {"display_handle": {"owner_uuid": "7"}},
        {"audio_policy": {"enabled": "yes"}},
        {"stream": {"lease_id": "12"}},
    ],
)
def test_an_unacceptable_leaf_value_is_rejected(mutation: dict[str, object]):
    document = json.loads(encode_session(SessionSnapshot.idle(target().receiver_id)))
    document.update(mutation)
    with pytest.raises(RecordError) as raised:
        decode_session(json.dumps(document).encode())
    assert raised.value.reason == "invalid_value"


def test_ownership_record_round_trips_every_stage_and_desired_state():
    for stage in ("reserved", "helper_started", "output_observed", "published", "removing"):
        for desired_state in ("active", "removing"):
            record = ownership(stage=stage, desired_state=desired_state)
            assert decode_ownership(encode_ownership(record)) == record


def test_ownership_record_rejects_a_stage_the_design_never_defines():
    with pytest.raises(RecordError) as raised:
        encode_ownership(ownership(stage="guessed"))
    assert raised.value.reason == "invalid_value"


def test_ownership_record_rejects_a_desired_state_the_design_never_defines():
    with pytest.raises(RecordError) as raised:
        encode_ownership(ownership(desired_state="maybe"))
    assert raised.value.reason == "invalid_value"


def test_ownership_record_rejects_an_unsupported_schema_version():
    with pytest.raises(RecordError) as raised:
        encode_ownership(ownership(schema_version=2))
    assert raised.value.reason == "invalid_value"


def test_a_record_can_describe_a_reservation_before_the_helper_or_output_exists():
    reserved = ownership(
        stage="reserved", helper=None, connector=None, output_id=None, observed_size=None,
    )
    assert decode_ownership(encode_ownership(reserved)) == reserved


def test_daemon_record_round_trips_runtime_evidence():
    record = daemon()
    assert decode_daemon(encode_daemon(record)) == record
    started = daemon(lifecycle="starting", supervisor=None, daemon=None)
    assert decode_daemon(encode_daemon(started)) == started


def test_receiver_record_round_trips_its_non_secret_preferences():
    stored = saved(credential_ref=None)
    assert decode_receiver(encode_receiver(stored)) == stored
    bare = saved(last_endpoint=None, remembered_resolution=None, service_metadata=())
    assert decode_receiver(encode_receiver(bare)) == bare


def test_an_ambiguous_receiver_is_never_persisted():
    assert is_ambiguous_identity(AMBIGUOUS) is True
    assert is_ambiguous_identity(target().receiver_id) is False
    assert is_ambiguous_identity(None) is False
    with pytest.raises(RecordError) as raised:
        encode_receiver(saved(receiver_id=AMBIGUOUS))
    assert raised.value.reason == "invalid_value"
    with pytest.raises(RecordError):
        encode_session(SessionSnapshot.idle(AMBIGUOUS))


def test_the_receiver_store_refuses_an_ambiguous_identity(tmp_path: Path):
    store = JsonReceiverStore(files_at(tmp_path / "state"))
    refused = run(store.put(saved(receiver_id=AMBIGUOUS)))
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.persistence_failed
    assert refused.error.detail == "ambiguous_identity"
    assert run(store.get(AMBIGUOUS)) == Ok(value=None)
    assert run(store.all()) == Ok(value=())
    assert run(store.remove_metadata(AMBIGUOUS)) == Ok(value=None)


# --------------------------------------------------------------------------- repositories


def test_every_repository_implements_its_domain_port():
    """A rename here would break a lane that consumes the frozen protocol.

    The ports are structural, so conformance is decided by the exact method set
    and signature each one declares, read from the protocol object itself rather
    than restated here.
    """
    import inspect

    from airplay_linux.domain.ports import (
        DaemonStore,
        OwnershipStore,
        ReceiverStore,
        SessionStore,
    )

    expected = {
        SessionStore: JsonSessionStore,
        ReceiverStore: JsonReceiverStore,
        OwnershipStore: JsonOwnershipStore,
        DaemonStore: JsonDaemonStore,
    }
    for protocol, implementation in expected.items():
        for name, member in vars(protocol).items():
            if name.startswith("_") or not inspect.isfunction(member):
                continue
            ours = inspect.getattr_static(implementation, name, None)
            assert ours is not None, (implementation.__name__, name)
            assert inspect.iscoroutinefunction(ours), (implementation.__name__, name)
            assert inspect.signature(ours) == inspect.signature(member), (
                implementation.__name__,
                name,
            )


def test_the_session_store_round_trips_through_the_durable_root(tmp_path: Path):
    root = tmp_path / "state"
    store = JsonSessionStore(files_at(root))
    snapshot = rich_snapshot()
    assert run(store.save(snapshot)) == Ok(value=None)
    assert run(store.load_all()) == (snapshot,)
    assert store.quarantined() == ()
    assert [path.name for path in (root / "sessions").iterdir()] == [SESSION_NAME]


def test_the_session_store_quarantines_corruption_without_deleting_it(tmp_path: Path):
    root = tmp_path / "state"
    files = files_at(root)
    good = rich_snapshot()
    files.write("sessions/good.json", encode_session(good))
    corrupt = b'{"schema": "airplay-linux/session", "version": 1, '
    files.write("sessions/broken.json", corrupt)
    files.write("sessions/truncated.json", b"{\n")
    store = JsonSessionStore(files)

    assert run(store.load_all()) == (good,)
    quarantined = store.quarantined()
    assert len(quarantined) == 2
    assert {item.code for item in quarantined} == {ErrorCode.persistence_failed}
    assert {item.receiver_id for item in quarantined} == {None}
    assert {item.detail for item in quarantined} == {"malformed_json"}
    # The bytes stay on disk as evidence; a quarantine is never a deletion.
    assert files.read("sessions/broken.json") == corrupt


def test_the_session_store_quarantines_a_future_version_without_deleting_it(tmp_path: Path):
    files = files_at(tmp_path / "state")
    future = json.dumps(
        {"schema": "airplay-linux/session", "version": 2, "receiver_id": "airplay-device:a"},
    ).encode()
    files.write("sessions/future.json", future)
    store = JsonSessionStore(files)
    assert run(store.load_all()) == ()
    assert [item.detail for item in store.quarantined()] == ["unknown_version"]
    # A newer format is not interpreted *or* discarded: it stays as evidence.
    assert files.read("sessions/future.json") == future


def test_the_session_store_skips_a_temporary_artifact_instead_of_quarantining_it(tmp_path: Path):
    root = tmp_path / "state"
    files = files_at(root)
    snapshot = SessionSnapshot.idle(target().receiver_id)
    files.write("sessions/receiver.json", encode_session(snapshot))
    (root / "sessions" / ".receiver.json.tmp").write_bytes(b"half a record")
    store = JsonSessionStore(files)
    assert run(store.load_all()) == (snapshot,)
    assert store.quarantined() == ()


def test_a_record_written_under_a_restrictive_umask_is_still_owner_only(tmp_path: Path):
    # ``os.open`` applies the process umask, which can only narrow a mode; the
    # directory mode is set explicitly so a permissive umask cannot widen it.
    previous = os.umask(0o777)
    try:
        files = files_at(tmp_path / "state")
        files.write("sessions/receiver.json", b"payload")
    finally:
        os.umask(previous)
    assert mode_of(tmp_path / "state") == 0o700
    assert mode_of(tmp_path / "state" / "sessions") == 0o700
    assert mode_of(tmp_path / "state" / "sessions" / "receiver.json") == 0o600


def test_the_session_store_reports_a_failed_directory_fsync_as_uncertain(tmp_path: Path):
    root = tmp_path / "state"
    old = SessionSnapshot.idle(target().receiver_id)
    files_at(root).write(f"sessions/{SESSION_NAME}", encode_session(old))
    new = replace(old, online=True, session_generation=1)

    store = JsonSessionStore(files_at(root, FaultyFilesystem("directory_fsync")))
    failed = run(store.save(new))
    assert isinstance(failed, Err)
    assert failed.error.code is ErrorCode.persistence_failed
    assert failed.error.detail == "directory_fsync"
    # The rename already happened, so the new record is visible but not durable.
    # The failure is reported as uncertain rather than as a success.
    assert files_at(root).read(f"sessions/{SESSION_NAME}") == encode_session(new)
    retried = JsonSessionStore(files_at(root))
    assert run(retried.save(new)) == Ok(value=None)
    assert run(retried.load_all()) == (new,)


def test_the_session_store_reports_a_failed_rename_without_publishing(tmp_path: Path):
    root = tmp_path / "state"
    old = SessionSnapshot.idle(target().receiver_id)
    files_at(root).write(f"sessions/{SESSION_NAME}", encode_session(old))
    new = replace(old, online=True, session_generation=1)
    store = JsonSessionStore(files_at(root, FaultyFilesystem("rename")))
    failed = run(store.save(new))
    assert isinstance(failed, Err)
    assert failed.error.detail == "rename"
    assert run(JsonSessionStore(files_at(root)).load_all()) == (old,)


def test_the_receiver_store_round_trips_and_removes_metadata(tmp_path: Path):
    store = JsonReceiverStore(files_at(tmp_path / "state"))
    stored = saved()
    assert run(store.put(stored)) == Ok(value=None)
    assert run(store.get(stored.receiver_id)) == Ok(value=stored)
    assert run(store.all()) == Ok(value=(stored,))
    assert run(store.get(OTHER)) == Ok(value=None)
    assert run(store.remove_metadata(stored.receiver_id)) == Ok(value=None)
    assert run(store.get(stored.receiver_id)) == Ok(value=None)
    assert run(store.all()) == Ok(value=())


def test_the_receiver_store_fails_closed_on_a_corrupt_sibling(tmp_path: Path):
    root = tmp_path / "state"
    files = files_at(root)
    store = JsonReceiverStore(files)
    stored = saved()
    assert run(store.put(stored)) == Ok(value=None)
    files.write("receivers/broken.json", b"{ not json")
    # The readable record is still readable by identity...
    assert run(store.get(stored.receiver_id)) == Ok(value=stored)
    # ...but the complete set is not claimed while one record is unreadable.
    failed = run(store.all())
    assert isinstance(failed, Err)
    assert failed.error.code is ErrorCode.persistence_failed
    assert failed.error.detail == "malformed_json"
    assert files.read("receivers/broken.json") == b"{ not json"


def test_the_ownership_store_round_trips_and_deletes_an_exact_owner(tmp_path: Path):
    root = tmp_path / "state"
    store = JsonOwnershipStore(files_at(root))
    record = ownership()
    assert run(store.write(record)) == Ok(value=None)
    assert run(store.all()) == Ok(value=(record,))
    assert run(store.delete(record.owner_uuid)) == Ok(value=None)
    assert run(store.all()) == Ok(value=())
    assert run(store.delete(record.owner_uuid)) == Ok(value=None)


def test_the_ownership_store_fails_closed_so_ownership_is_never_understated(tmp_path: Path):
    root = tmp_path / "state"
    files = files_at(root)
    store = JsonOwnershipStore(files)
    record = ownership()
    assert run(store.write(record)) == Ok(value=None)
    files.write("ownership/broken.json", b'{"stage": "removing"}')
    failed = run(store.all())
    assert isinstance(failed, Err)
    assert failed.error.code is ErrorCode.persistence_failed
    assert files.read("ownership/broken.json") == b'{"stage": "removing"}'


def test_the_ownership_store_refuses_a_stage_outside_the_design(tmp_path: Path):
    store = JsonOwnershipStore(files_at(tmp_path / "state"))
    refused = run(store.write(ownership(stage="invented")))
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.persistence_failed
    assert refused.error.detail == "invalid_value"
    assert run(store.all()) == Ok(value=())


def test_the_ownership_store_reports_a_delete_that_is_not_durable(tmp_path: Path):
    # A delete must not report success unless it is durable, or the record would
    # reappear on the next startup and contradict a confirmed release.
    root = tmp_path / "state"
    record = ownership()
    files_at(root).write(f"ownership/{_owner_name()}", encode_ownership(record))
    store = JsonOwnershipStore(files_at(root, FaultyFilesystem("directory_fsync")))
    failed = run(store.delete(record.owner_uuid))
    assert isinstance(failed, Err)
    assert failed.error.code is ErrorCode.persistence_failed
    assert failed.error.detail == "directory_fsync"


def test_the_daemon_store_round_trips_and_fails_closed(tmp_path: Path):
    root = tmp_path / "runtime"
    files = files_at(root)
    store = JsonDaemonStore(files)
    record = daemon()
    assert run(store.write(record)) == Ok(value=None)
    assert run(store.all()) == Ok(value=(record,))
    files.write("daemon/broken.json", b"[]")
    failed = run(store.all())
    assert isinstance(failed, Err)
    assert failed.error.detail == "not_an_object"
    assert run(store.delete(record.instance_uuid)) == Ok(value=None)
    assert files.read("daemon/broken.json") == b"[]"


def _owner_name() -> str:
    """The percent-encoded record name for the fixture owner UUID."""
    return quote(str(ownership().owner_uuid), safe="") + ".json"


def test_record_names_are_reversible_and_never_traverse(tmp_path: Path):
    store = JsonReceiverStore(files_at(tmp_path / "state"))
    stored = saved(receiver_id=FINGERPRINT)
    assert run(store.put(stored)) == Ok(value=None)
    names = sorted(path.name for path in (tmp_path / "state" / "receivers").iterdir())
    assert names == [f"airplay-fingerprint%3A{DIGEST}.json"]
    assert run(store.get(FINGERPRINT)) == Ok(value=stored)
    # A record name is exactly its encoded identity, so the mapping is injective:
    # two receivers can never share one file, one file never names two.
    assert names[0][: -len(".json")] == quote(str(FINGERPRINT), safe="")


# --------------------------------------------------------------------------- state root


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"XDG_STATE_HOME": "/state", "HOME": "/home/person"}, Path("/state/airplay-linux")),
        ({"HOME": "/home/person"}, Path("/home/person/.local/state/airplay-linux")),
        (
            {"XDG_STATE_HOME": "relative/state", "HOME": "/home/person"},
            Path("/home/person/.local/state/airplay-linux"),
        ),
        (
            {"XDG_STATE_HOME": "", "HOME": "/home/person"},
            Path("/home/person/.local/state/airplay-linux"),
        ),
    ],
)
def test_the_metadata_root_follows_the_xdg_state_rule(environ: dict[str, str], expected: Path):
    assert state_root(environ) == expected


# --------------------------------------------------------------------------- preferences


def test_preference_access_returns_the_saved_record_for_a_continuous_identity():
    stored = saved()
    assert PreferenceAccess.for_record_set(record_set(), stored) is stored


def test_a_stable_device_identity_keeps_its_preferences_across_an_endpoint_change():
    stored = saved()
    moved = record_set(endpoint_fingerprint="endpoint-a-v2", endpoint_generation=2)
    assert PreferenceAccess.for_record_set(moved, stored) is stored


def test_preference_access_allows_a_fallback_identity_only_with_verified_continuity():
    stored = saved(receiver_id=FINGERPRINT)
    fallback = record_set(receiver_id=FINGERPRINT, identity_kind="fingerprint")
    assert PreferenceAccess.for_record_set(fallback, stored) is stored


@pytest.mark.parametrize(
    "withheld",
    [
        {"ambiguous": True},
        {"identity_kind": "ambiguous"},
        {"persistable": False},
        {"replacement_of": OTHER},
        {"receiver_id": OTHER},
    ],
)
def test_preference_access_withholds_an_unjustified_saved_record(withheld: dict[str, object]):
    stored = saved()
    assert PreferenceAccess.for_record_set(record_set(**withheld), stored) is None


def test_a_stable_device_identity_is_its_own_continuity_evidence():
    # A device identifier is stable across a compatible service, port, or host
    # change, so it does not need a separate continuity claim to justify its own
    # saved record -- unlike a fallback identity, whose ID is a hash of the very
    # advertisement data that may have been replaced.
    stored = saved()
    device = record_set(identity_kind="device", continuity_verified=False)
    assert PreferenceAccess.for_record_set(device, stored) is stored


def test_preference_access_withholds_a_fallback_identity_at_a_changed_endpoint():
    stored = saved(receiver_id=FINGERPRINT)
    moved = record_set(
        receiver_id=FINGERPRINT,
        identity_kind="fingerprint",
        endpoint_fingerprint="endpoint-a-v2",
    )
    assert PreferenceAccess.for_record_set(moved, stored) is None


def test_preference_access_withholds_a_fallback_identity_with_unverified_continuity():
    stored = saved(receiver_id=FINGERPRINT)
    unproven = record_set(
        receiver_id=FINGERPRINT, identity_kind="fingerprint", continuity_verified=False,
    )
    assert PreferenceAccess.for_record_set(unproven, stored) is None


def test_preference_access_returns_nothing_without_a_saved_record():
    assert PreferenceAccess.for_record_set(record_set(), None) is None


def test_preference_access_withholds_an_ambiguous_saved_identity_even_when_the_hash_matches():
    # The hashed identity is unchanged, which is exactly the case the design says
    # must not inherit trust or a remembered resolution.
    stored = saved(receiver_id=AMBIGUOUS)
    ambiguous = record_set(
        receiver_id=AMBIGUOUS, identity_kind="ambiguous", ambiguous=True, persistable=False,
    )
    assert PreferenceAccess.for_record_set(ambiguous, stored) is None
