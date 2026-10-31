"""Strict new documents use their own validation; legacy codecs stay delivered."""
from __future__ import annotations

import errno
import json
from dataclasses import MISSING, fields, replace
from uuid import UUID

import pytest

from airplay_linux.domain import records
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.resources import Err, ErrorCode
from airplay_linux.storage import AtomicFiles, PosixFilesystem
from airplay_linux.storage import codecs

APP_ID = InstanceId(UUID("11111111-1111-4111-8111-111111111111"))
LAUNCH_ID = UUID("22222222-2222-4222-8222-222222222222")
DAEMON_ID = InstanceId(UUID("33333333-3333-4333-8333-333333333333"))
BOOT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RESERVATION_KEYS = {
    "schema_version", "launch_uuid", "instance_uuid", "app", "owner_uid", "boot_id",
    "runtime_directory", "runtime_device", "runtime_inode", "endpoint_generation",
    "socket_name", "protocol_version",
}
SAFE_REASONS = {
    "malformed_json", "not_an_object", "unknown_schema", "unknown_version",
    "unknown_field", "missing_field", "invalid_value",
}


def reservation():
    assert hasattr(records, "DaemonReservation"), "missing lifecycle DTO DaemonReservation"
    return records.DaemonReservation(
        schema_version=1, launch_uuid=LAUNCH_ID, instance_uuid=APP_ID,
        app=ProcessIdentity(pid=31001, start_ticks=81001), owner_uid=1000, boot_id=BOOT_ID,
        runtime_directory="/run/user/1000/airplay-linux/owned", runtime_device=0,
        runtime_inode=91, endpoint_generation=37, socket_name="doubletake.sock",
        protocol_version="doubletake-0.5",
    )


def reservation_values():
    item = reservation()
    return {name: getattr(item, name) for name in RESERVATION_KEYS}


def receipt_states():
    assert hasattr(records, "LaunchReceipt"), "missing lifecycle DTO LaunchReceipt"
    reserved = records.LaunchReceipt(
        **reservation_values(), state="reserved", daemon_instance_uuid=None,
        supervisor=None, daemon=None, entry_device=None, entry_inode=None, socket_inode=None,
        peer_pid=None, peer_start_ticks=None, peer_uid=None, status_state=None,
        credential_service="unknown", failure=None, updated_at=1.0,
    )
    detached = replace(
        reserved, state="detached", daemon_instance_uuid=DAEMON_ID,
        supervisor=ProcessIdentity(pid=31002, start_ticks=81002),
        daemon=ProcessIdentity(pid=31003, start_ticks=81003),
    )
    bound = replace(
        detached, state="bound", entry_device=0, entry_inode=92, socket_inode=93,
        peer_pid=31003, peer_start_ticks=81003, peer_uid=1000,
    )
    ready = replace(bound, state="ready", status_state="idle", credential_service="keyring_proven")
    return {
        "reserved": reserved, "detached": detached, "bound": bound, "ready": ready,
        "stopping": replace(ready, state="stopping"),
        "stopped": replace(ready, state="stopped"),
        "failed": replace(ready, state="failed", failure=ErrorCode.daemon_failed),
    }


def cleanup_documents():
    assert hasattr(records, "DaemonCleanupIntent"), "missing lifecycle DTO DaemonCleanupIntent"
    initial = records.DaemonCleanupIntent(
        launch_uuid=LAUNCH_ID, reason=ErrorCode.daemon_failed,
        intent_at=2.0, completed_at=None, outcome=None,
    )
    return (initial, replace(initial, completed_at=3.0, outcome="stopped"),
            replace(initial, completed_at=3.0, outcome=ErrorCode.cleanup_timeout))


def supervisor_document():
    assert hasattr(records, "SupervisorReleaseIntent"), "missing lifecycle DTO SupervisorReleaseIntent"
    receipt = receipt_states()["detached"]
    return records.SupervisorReleaseIntent(
        schema_version=1, launch_uuid=LAUNCH_ID, instance_uuid=APP_ID,
        app=receipt.app, supervisor=receipt.supervisor, daemon=receipt.daemon,
        reason="application-exited", intent_at=2.0,
    )


def owner_document():
    assert hasattr(records, "AuthorityOwner"), "missing lifecycle DTO AuthorityOwner"
    return records.AuthorityOwner(
        schema="airplay-linux/daemon-authority-owner/v1", instance_uuid=APP_ID,
        app=reservation().app, boot_id=BOOT_ID, acquired_at=1.0,
    )


def verdict_document():
    assert hasattr(records, "ProbeVerdict"), "missing lifecycle DTO ProbeVerdict"
    return records.ProbeVerdict(
        schema="airplay-linux/credential-service-proof", version=1, service_leg="passed",
        backend_positive_leg="passed", backend_negative_leg="passed", failure=None,
    )


def wire(document):
    return json.dumps(document, separators=(",", ":")).encode()


def malformed_documents(raw):
    """Envelope/member categories, including nested duplicate-member ingestion."""
    doc = json.loads(raw)
    yield "unknown-schema", wire(doc | {"schema": "airplay-linux/future"})
    if "version" in doc:
        for version in (0, 2, True, "1", None, []):
            yield "unknown-version", wire(doc | {"version": version})
    for name in doc:
        yield "missing-" + name, wire({k: v for k, v in doc.items() if k != name})
        # A duplicate with an identical value is still forbidden.
        yield "duplicate-" + name, b"{" + wire({name: doc[name]})[1:-1] + b"," + raw[1:]
    yield "extra", wire(doc | {"proven": True})
    for name, value in doc.items():
        if isinstance(value, dict):
            for member in value:
                duplicated = b"{" + wire({member: value[member]})[1:-1] + b"," + wire(value)[1:]
                yield "nested-duplicate", raw.replace(wire(value), duplicated)
                partial = dict(value)
                del partial[member]
                yield "nested-missing", wire(doc | {name: partial})
            yield "nested-extra", wire(doc | {name: value | {"secret": "never-copy-this"}})
    for time_field in ("intent_at", "completed_at", "updated_at", "acquired_at"):
        if doc.get(time_field) is not None:
            member = wire({time_field: doc[time_field]})[1:-1]
            yield "nonfinite-exponent-" + time_field, raw.replace(member, ('"' + time_field + '":1e400').encode())
    for suffix in (b" ", b"\n", b"\t", b"{}", b"\0"):
        yield "trailing", raw + suffix
    for invalid in (b"", b"{", b"[]", b"null", b"true", b"\xff", b"\xef\xbb\xbf" + raw,
                    b"\xc2\xa0" + raw):
        yield "malformed", invalid


def assert_refused(decode, payload):
    with pytest.raises(codecs.RecordError) as caught:
        decode(payload)
    assert caught.value.reason in SAFE_REASONS
    assert "never-copy-this" not in str(caught.value)


def assert_bad_leaf(record, name, value, encode, decode):
    """Both ingress directions must refuse safely, including unhashable leaves."""
    bad = replace(record, **{name: value})
    with pytest.raises(codecs.RecordError) as caught:
        encode(bad)
    assert caught.value.reason in SAFE_REASONS
    if isinstance(value, (ProcessIdentity, UUID, ErrorCode)):
        if isinstance(value, ProcessIdentity):
            value = {"pid": value.pid, "start_ticks": value.start_ticks}
        else:
            value = str(value)
    payload = wire(json.loads(encode(record)) | {name: value})
    assert_refused(decode, payload)
    return payload


async def assert_store_leaf_refused(record, name, value, encode, decode, files, store, key, *, reader="all"):
    payload = assert_bad_leaf(record, name, value, encode, decode)
    original = encode(record)
    files.write(key, original)
    persistence_refusal(await store.write(replace(record, **{name: value})))
    assert files.read(key) == original
    files.write(key, payload)
    persistence_refusal(await getattr(store, reader)())
    assert files.read(key) == payload


def reservation_bad_leaves():
    for name in ("schema_version", "owner_uid", "runtime_device", "runtime_inode", "endpoint_generation"):
        for value in (True, "1", None, [], -1, 2**63, float("nan"), float("inf")):
            yield name, value
    for name in ("runtime_inode", "endpoint_generation", "schema_version"):
        yield name, 0
    yield "schema_version", 2
    for name in ("launch_uuid", "instance_uuid", "boot_id"):
        for value in (None, True, [], "bad-uuid", BOOT_ID.upper(), BOOT_ID.replace("-", "")):
            yield name, value
    for value in (None, "invalid", [], {"pid": True, "start_ticks": 2},
                  ProcessIdentity(pid=0, start_ticks=1), ProcessIdentity(pid=-1, start_ticks=1),
                  ProcessIdentity(pid=True, start_ticks=1), ProcessIdentity(pid=2**31, start_ticks=1),
                  ProcessIdentity(pid=1, start_ticks=-1), ProcessIdentity(pid=1, start_ticks=True),
                  ProcessIdentity(pid=1, start_ticks=2**63)):
        yield "app", value
    for value in (None, [], True, "relative/runtime", "/", "/a/../b", "/a/./b", "/a//b", "/a\0b", "/" + "a" * 4096):
        yield "runtime_directory", value
    for value in (None, [], True, "", ".", "..", "/socket", "a/b", "a\0b", "s" * 257):
        yield "socket_name", value
    for value in (None, [], True, "", "v\0x", "p" * 257):
        yield "protocol_version", value


@pytest.fixture
def reservation_documents():
    return reservation()


def test_reservation_codec_rejection_matrix(reservation_documents):
    item = reservation_documents
    raw = codecs.encode_daemon_reservation(item)
    doc = json.loads(raw)
    assert set(doc) == RESERVATION_KEYS | {"schema", "version"}
    assert doc["schema"] == "airplay-linux/daemon-reservation"
    assert doc["schema_version"] == doc["version"] == 1
    assert codecs.decode_daemon_reservation(raw) == item
    for label, payload in malformed_documents(raw):
        assert_refused(codecs.decode_daemon_reservation, payload)
    for name, value in reservation_bad_leaves():
        assert_bad_leaf(item, name, value, codecs.encode_daemon_reservation, codecs.decode_daemon_reservation)
    # A correctly shaped JSON object is valid ingress; the encoder requires its DTO.
    process_dict = {"pid": 1, "start_ticks": 2}
    with pytest.raises(codecs.RecordError):
        codecs.encode_daemon_reservation(replace(item, app=process_dict))
    assert codecs.decode_daemon_reservation(wire(doc | {"app": process_dict})).app == ProcessIdentity(pid=1, start_ticks=2)
    edge = replace(item, app=ProcessIdentity(pid=2**31 - 1, start_ticks=2**63 - 1), owner_uid=0,
                   runtime_device=2**63 - 1, runtime_inode=2**63 - 1, endpoint_generation=2**63 - 1)
    assert codecs.decode_daemon_reservation(codecs.encode_daemon_reservation(edge)) == edge
    for value in (None, {}, "invalid", receipt_states()["reserved"]):
        with pytest.raises(codecs.RecordError):
            codecs.encode_daemon_reservation(value)


def test_new_record_constructors_are_required_frozen_keyword_only_and_redacted():
    for item in (reservation(), *receipt_states().values(), *cleanup_documents(),
                 supervisor_document(), owner_document(), verdict_document()):
        for member in fields(item):
            assert member.default is MISSING and member.default_factory is MISSING
            assert member.kw_only
            if member.name in {
                "launch_uuid", "instance_uuid", "daemon_instance_uuid", "app", "supervisor", "daemon",
                "boot_id", "runtime_directory", "socket_name", "peer_pid", "peer_start_ticks", "peer_uid",
            }:
                assert not member.repr, (type(item).__name__, member.name)
                assert member.name + "=" not in repr(item)
        with pytest.raises(AttributeError):
            item.schema = "changed"
        assert "owner_token" not in {f.name for f in fields(item)}


def test_bounded_paths_encode_decode_symmetrically_without_a_document_text_limit():
    path = "/" + "/".join(["a" * 200] * 19)
    assert len(path) == 3819
    for item, encode, decode in (
        (reservation(), codecs.encode_daemon_reservation, codecs.decode_daemon_reservation),
        (receipt_states()["ready"], codecs.encode_launch_receipt, codecs.decode_launch_receipt),
    ):
        item = replace(item, runtime_directory=path)
        raw = encode(item)
        assert len(raw) > 4096
        assert decode(raw) == item
        assert encode(decode(raw)) == raw


class PublicationFault(PosixFilesystem):
    """Real rename followed by a durable-publication refusal, without any process I/O."""
    def __init__(self, step="directory_fsync"):
        self.step = step
        self.renames = 0
        self.confirmations = 0

    def write(self, parent, name, payload):
        if self.step == "write":
            raise OSError(errno.EIO, "never-copy-this")
        return super().write(parent, name, payload)

    def fsync_file(self, parent, name):
        if self.step == "fsync":
            raise OSError(errno.EIO, "never-copy-this")
        return super().fsync_file(parent, name)

    def replace(self, parent, source, destination):
        if self.step == "rename":
            raise OSError(errno.EIO, "never-copy-this")
        super().replace(parent, source, destination)
        self.renames += 1

    def fsync_directory(self, descriptor):
        self.confirmations += 1
        if self.step == "directory_fsync":
            raise OSError(errno.EIO, "never-copy-this")
        return super().fsync_directory(descriptor)


def persistence_refusal(result):
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    assert "never-copy-this" not in repr(result)


@pytest.mark.parametrize("kind", ("receipt", "cleanup", "completion", "supervisor", "owner"))
def test_finite_timestamp_controls_accept_signed_values_and_decode_to_float(kind):
    cases = {
        "receipt": (replace(receipt_states()["reserved"], updated_at=-2.0), "updated_at", codecs.encode_launch_receipt, codecs.decode_launch_receipt),
        "cleanup": (replace(cleanup_documents()[0], intent_at=-2.0), "intent_at", codecs.encode_daemon_cleanup_intent, codecs.decode_daemon_cleanup_intent),
        "completion": (replace(cleanup_documents()[1], completed_at=0.0), "completed_at", codecs.encode_daemon_cleanup_intent, codecs.decode_daemon_cleanup_intent),
        "supervisor": (replace(supervisor_document(), intent_at=-2.0), "intent_at", codecs.encode_supervisor_release_intent, codecs.decode_supervisor_release_intent),
        "owner": (replace(owner_document(), acquired_at=-2.0), "acquired_at", codecs.encode_authority_owner, codecs.decode_authority_owner),
    }
    item, member, encode, decode = cases[kind]
    assert decode(encode(item)) == item
    document = json.loads(encode(item))
    document[member] = int(document[member])
    decoded = decode(wire(document))
    assert decoded == item
    assert type(getattr(decoded, member)) is float


def test_authority_owner_and_probe_verdict_strict_matrices():
    for item, encode, decode, expected in (
        (owner_document(), codecs.encode_authority_owner, codecs.decode_authority_owner,
         {"schema", "instance_uuid", "app", "boot_id", "acquired_at"}),
        (verdict_document(), codecs.encode_probe_verdict, codecs.decode_probe_verdict,
         {"schema", "version", "service_leg", "backend_positive_leg", "backend_negative_leg", "failure"}),
    ):
        raw = encode(item)
        assert set(json.loads(raw)) == expected
        assert decode(raw) == item
        for label, payload in malformed_documents(raw):
            assert_refused(decode, payload)
    owner = owner_document()
    for name, values in {
        "schema": ([], True, None, "future"),
        "instance_uuid": ([], True, None, "bad", BOOT_ID.upper()),
        "boot_id": ([], True, None, "bad", BOOT_ID.upper()),
        "app": ([], True, None, "bad", ProcessIdentity(pid=True, start_ticks=1),
                ProcessIdentity(pid=2**31, start_ticks=1), ProcessIdentity(pid=1, start_ticks=-1)),
        "acquired_at": ([], True, None, float("nan"), float("inf")),
    }.items():
        for value in values:
            assert_bad_leaf(owner, name, value, codecs.encode_authority_owner, codecs.decode_authority_owner)
    verdict = verdict_document()
    raw = codecs.encode_probe_verdict(verdict)
    exactly_bounded = b" " * (4096 - len(raw)) + raw
    assert codecs.decode_probe_verdict(exactly_bounded) == verdict
    assert_refused(codecs.decode_probe_verdict, b" " + exactly_bounded)
    for name, values in {
        "schema": ([], True, None, "future"), "version": ([], True, None, "1", 0, 2),
        "service_leg": ([], True, None, "other"),
        "backend_positive_leg": ([], True, None, "other"),
        "backend_negative_leg": ([], True, None, "other"), "failure": ([], True, "bad", ErrorCode.daemon_failed),
    }.items():
        for value in values:
            assert_bad_leaf(verdict, name, value, codecs.encode_probe_verdict, codecs.decode_probe_verdict)
    for name in ("service_leg", "backend_positive_leg", "backend_negative_leg"):
        assert_bad_leaf(verdict, name, "failed", codecs.encode_probe_verdict, codecs.decode_probe_verdict)
        failed = replace(verdict, **{name: "failed"}, failure=ErrorCode.keyring_unavailable)
        assert codecs.decode_probe_verdict(codecs.encode_probe_verdict(failed)) == failed
    # Timestamps are wire numbers, canonicalized to float, including zero ticks/UID/devices.
    owner = replace(owner, acquired_at=0, app=ProcessIdentity(pid=1, start_ticks=0))
    decoded = codecs.decode_authority_owner(codecs.encode_authority_owner(owner))
    assert type(decoded.acquired_at) is float and decoded.acquired_at == 0.0
    for item, encode, decode, field in (
        (receipt_states()["reserved"], codecs.encode_launch_receipt, codecs.decode_launch_receipt, "updated_at"),
        (cleanup_documents()[0], codecs.encode_daemon_cleanup_intent, codecs.decode_daemon_cleanup_intent, "intent_at"),
        (supervisor_document(), codecs.encode_supervisor_release_intent, codecs.decode_supervisor_release_intent, "intent_at"),
    ):
        decoded = decode(encode(replace(item, **{field: 0})))
        assert type(getattr(decoded, field)) is float
