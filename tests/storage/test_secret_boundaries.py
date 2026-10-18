"""No durable record, quarantine report, or storage failure may carry a secret.

The design forbids PINs, passwords, portal restore tokens, capture leases,
backend correlation tokens, provisional credentials, and keyring secrets from
reaching application metadata or any message derived from it. Those are *shape*
rules, not conventions: a secret has nowhere to be written because no record has
a field for it, the codecs refuse a non-text leaf rather than stringify it, and
every failure this package reports is rebuilt from the diagnostics catalog with a
fixed reason token.

The canary strings below are what each test looks for in the emitted bytes, in a
decoded record, in a quarantine diagnostic, and in a storage failure's repr.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Final
from uuid import UUID

import pytest

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    InstanceId,
    LeaseId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationId,
    OwnerId,
    ProcessIdentity,
    SensitiveText,
)
from airplay_linux.domain.records import (
    DaemonRecord,
    LayoutSnapshot,
    OwnershipRecord,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    ErrorCode,
    PhysicalMonitorSource,
    ResolutionDecision,
    ResolutionEvidence,
    Size,
)
from airplay_linux.storage import (
    AtomicFiles,
    JsonDaemonStore,
    JsonOwnershipStore,
    JsonReceiverStore,
    JsonSessionStore,
    PosixFilesystem,
    RecordError,
    StorageError,
    decode_receiver,
    encode_daemon,
    encode_ownership,
    encode_receiver,
    encode_session,
)
from airplay_linux.testing.builders import target

PIN_CANARY: Final[str] = "canary-pin-8496"
TOKEN_CANARY: Final[str] = "canary-portal-token-8496"
SECRET_CANARY: Final[str] = "canary-backend-secret-8496"
CORRELATION_CANARY: Final[str] = "canary-correlation-8496"
PROVISIONAL_CANARY: Final[str] = "canary-provisional-8496"
LEASE_CANARY: Final[UUID] = UUID(int=8496)

STORAGE_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[2] / "src" / "airplay_linux" / "storage"
)

DIGEST: Final[str] = hashlib.sha256(b"continuity").hexdigest()
AMBIGUOUS: Final[ReceiverId] = ReceiverId(f"airplay-ambiguous:{DIGEST}")


def run(awaitable):
    return asyncio.run(awaitable)


def canaries() -> tuple[bytes, ...]:
    """Every secret this package must never write, as bytes to search for."""
    return (
        PIN_CANARY.encode(),
        TOKEN_CANARY.encode(),
        SECRET_CANARY.encode(),
        CORRELATION_CANARY.encode(),
        PROVISIONAL_CANARY.encode(),
        str(LEASE_CANARY).encode(),
        PIN_CANARY.encode().hex().encode(),
        PROVISIONAL_CANARY.encode().hex().encode(),
    )


def assert_secret_free(payload: bytes) -> None:
    for canary in canaries():
        assert canary not in payload


def source_snapshot() -> SessionSnapshot:
    """A session that holds every transient capture and secret-adjacent value."""
    t = target()
    return replace(
        SessionSnapshot.idle(t.receiver_id),
        target=t,
        online=True,
        physical_source=PhysicalMonitorSource(
            connector="DP-1",
            portal_restore_token=TOKEN_CANARY,
            capture_lease_id=LeaseId(LEASE_CANARY),
        ),
    )


def saved_receiver() -> SavedReceiver:
    t = target()
    return SavedReceiver(
        receiver_id=t.receiver_id,
        service_metadata=t.service_records,
        display_name="Living Room",
        last_endpoint=t,
        continuity_fingerprint=t.endpoint_fingerprint,
        credential_ref=OpaqueCredentialRef(PROVISIONAL_CANARY),
        remembered_resolution=ResolutionDecision(
            size=Size(width=1920, height=1080),
            source="stored",
            evidence=ResolutionEvidence(
                origin="stored",
                reliable=True,
                receiver_id=t.receiver_id,
                endpoint_fingerprint=t.endpoint_fingerprint,
            ),
        ),
    )


def ownership_record() -> OwnershipRecord:
    t = target()
    return OwnershipRecord(
        schema_version=1,
        owner_uuid=OwnerId(UUID(int=7)),
        app_instance_uuid=InstanceId(UUID(int=1)),
        runtime_directory="/run/user/1000/airplay-linux/instances/one",
        creation_operation_id=OperationId(UUID(int=8)),
        receiver_id=t.receiver_id,
        endpoint_fingerprint=t.endpoint_fingerprint,
        desired_state="active",
        stage="published",
        requested_size=Size(width=1920, height=1080),
        observed_size=Size(width=1920, height=1080),
        helper=ProcessIdentity(pid=4001, start_ticks=90001),
        connector="Virtual-v1",
        output_id="v1",
        created_utc="2026-10-17T00:00:00Z",
        last_configuration_step="configured",
        baseline=LayoutSnapshot(outputs={"DP-1": {"enabled": True}}, baseline_hash="layout-1"),
        last_applied={"Virtual-v1": {"enabled": True}},
        provider_version="fake-kscreen-1",
        capability_fingerprint="capability-1",
    )


def daemon_record() -> DaemonRecord:
    return DaemonRecord(
        schema_version=1,
        instance_uuid=InstanceId(UUID(int=9)),
        owner_token="owner-token-1",
        app=ProcessIdentity(pid=3001, start_ticks=80001),
        supervisor=ProcessIdentity(pid=3002, start_ticks=80002),
        daemon=ProcessIdentity(pid=3003, start_ticks=80003),
        protocol_version="doubletake-0.5",
        socket_path="/run/user/1000/airplay-linux/instances/nine/doubletake.sock",
        lifecycle="running",
        runtime_directory="/run/user/1000/airplay-linux/instances/nine",
    )


# --------------------------------------------------------------------------- record shapes


def test_the_session_disk_shape_has_no_challenge_or_secret():
    state = replace(SessionSnapshot.idle(target().receiver_id), target=target())
    payload = encode_session(state)
    assert b"backend_token" not in payload
    assert b"portal_restore_token" not in payload
    assert b"password" not in payload


def test_a_session_holding_a_restore_token_and_lease_writes_neither():
    payload = encode_session(source_snapshot())
    assert_secret_free(payload)
    assert b"portal_restore_token" not in payload
    assert b"capture_lease_id" not in payload
    # The connector itself is durable; it is a display identity, not a secret.
    assert b"DP-1" in payload


def test_a_receiver_holding_provisional_credentials_writes_no_secret():
    """A saved receiver may never be persisted with a credential reference.

    Version 1 has no representation for a committed reference (a recorded
    interface conflict), so the write is refused rather than silently dropping
    required non-secret metadata. What matters for this module's boundary is that
    the refusal is a fixed token: no reference value, provisional or committed,
    can reach disk, a diagnostic, or a failure's text either way.
    """
    payload_candidate = saved_receiver()
    with pytest.raises(RecordError) as raised:
        encode_receiver(payload_candidate)
    assert raised.value.reason == "unsupported_credential_ref"
    assert_secret_free(repr(raised.value).encode())
    assert PROVISIONAL_CANARY not in str(raised.value)


def test_a_receiver_without_a_credential_reference_writes_no_reference_field():
    payload = encode_receiver(replace(saved_receiver(), credential_ref=None))
    assert_secret_free(payload)
    assert b"credential_ref" not in payload
    assert b"opaquecredentialref" not in payload.lower()
    # Nothing is invented on the way back: a decode never claims a reference.
    assert decode_receiver(payload).credential_ref is None


def test_a_receiver_store_refuses_a_record_holding_a_credential_reference(
    tmp_path: Path,
):
    root = tmp_path / "state"
    store = JsonReceiverStore(AtomicFiles(root=root))
    refused = run(store.put(saved_receiver()))
    assert refused.error.code is ErrorCode.persistence_failed
    assert refused.error.detail == "unsupported_credential_ref"
    assert PROVISIONAL_CANARY not in repr(refused)
    assert list(root.rglob("*.json")) == []


def test_an_ownership_record_writes_no_capture_or_backend_identity():
    payload = encode_ownership(ownership_record())
    assert_secret_free(payload)
    assert b"portal_restore_token" not in payload
    assert b"auth" not in payload.lower()


def test_a_daemon_record_keeps_only_its_own_runtime_owner_token():
    # The runtime record legitimately carries the owner token that authenticates
    # *this* app instance's daemon; that is not a receiver credential. What it
    # must never carry is backend authentication material.
    payload = encode_daemon(daemon_record())
    assert_secret_free(payload)
    assert b"backend_token" not in payload
    assert b"password" not in payload


# --------------------------------------------------------------------------- leaf refusals


@pytest.mark.parametrize(
    "secret",
    [
        SensitiveText(PIN_CANARY),
        OpaqueCredentialRef(PROVISIONAL_CANARY),
        OpaqueProvisionalCredentialRef(PROVISIONAL_CANARY),
        OpaqueBackendToken(CORRELATION_CANARY),
    ],
)
def test_a_transient_private_value_is_never_coerced_into_a_record(secret: object):
    # A private value has no printable rendering, so writing one would have to go
    # through ``str()`` -- which is exactly how a secret would leak. The codecs
    # refuse the type instead.
    snapshot = replace(
        SessionSnapshot.idle(target().receiver_id),
        physical_source=PhysicalMonitorSource(
            connector=secret,  # type: ignore[arg-type]
            portal_restore_token=None,
        ),
    )
    with pytest.raises(RecordError) as raised:
        encode_session(snapshot)
    assert raised.value.reason == "invalid_value"
    assert PIN_CANARY not in str(raised.value)
    assert PROVISIONAL_CANARY not in str(raised.value)
    assert CORRELATION_CANARY not in repr(raised.value)


def test_a_catalog_error_is_rebuilt_so_a_quoted_backend_message_cannot_be_persisted():
    t = target()
    from airplay_linux.domain.resources import Diagnostic, RecoveryAction

    forged = Diagnostic(
        code=ErrorCode.transport_failed,
        message=f"backend said {SECRET_CANARY}",
        recovery=(RecoveryAction.retry,),
        receiver_id=t.receiver_id,
        detail=CORRELATION_CANARY,
    )
    payload = encode_session(replace(SessionSnapshot.idle(t.receiver_id), last_error=forged))
    assert_secret_free(payload)
    assert CORRELATION_CANARY.encode() not in payload
    assert b"backend said" not in payload


# --------------------------------------------------------------------------- quarantine reports


def test_a_quarantine_report_never_repeats_the_corrupt_record(tmp_path: Path):
    root = tmp_path / "state"
    files = AtomicFiles(root=root)
    files.write(
        "sessions/broken.json",
        b'{"schema": "airplay-linux/session", "version": 1, "note": "' + PIN_CANARY.encode() + b'"}',
    )
    store = JsonSessionStore(files)
    assert run(store.load_all()) == ()
    quarantined = store.quarantined()
    assert len(quarantined) == 1
    rendered = repr(quarantined[0]) + quarantined[0].message + str(quarantined[0].detail)
    assert PIN_CANARY not in rendered
    assert str(root) not in rendered
    assert quarantined[0].detail == "unknown_field"


def test_a_store_failure_reports_a_fixed_step_and_never_a_path_or_record(tmp_path: Path):
    class Exploding(PosixFilesystem):
        def write(self, parent: int, name: str, payload: bytes) -> None:
            raise OSError(f"cannot write {name}: {PIN_CANARY}")

    store = JsonSessionStore(AtomicFiles(root=tmp_path / "state", filesystem=Exploding()))
    failed = run(store.save(source_snapshot()))
    assert failed.error.code is ErrorCode.persistence_failed
    assert failed.error.detail == "write"
    rendered = repr(failed) + failed.error.message
    assert PIN_CANARY not in rendered
    assert TOKEN_CANARY not in rendered
    assert str(tmp_path) not in rendered


def test_a_storage_error_carries_only_its_step_token(tmp_path: Path):
    class Exploding(PosixFilesystem):
        def fsync_directory(self, descriptor: int) -> None:
            raise OSError(f"cannot fsync {descriptor}: {SECRET_CANARY}")

    files = AtomicFiles(root=tmp_path / "state", filesystem=Exploding())
    with pytest.raises(StorageError) as raised:
        files.write("sessions/one.json", b"{}")
    assert raised.value.step == "directory_fsync"
    assert SECRET_CANARY not in str(raised.value)
    assert SECRET_CANARY not in repr(raised.value)
    assert str(tmp_path) not in str(raised.value)


def test_no_store_failure_detail_is_free_text(tmp_path: Path):
    """Every reason this package emits is a token it wrote, not caller content."""
    store = JsonDaemonStore(AtomicFiles(root=tmp_path / "runtime"))
    files = AtomicFiles(root=tmp_path / "runtime")
    files.write("daemon/one.json", b'{"detail": "' + CORRELATION_CANARY.encode() + b'"}')
    failed = run(store.all())
    assert failed.error.detail == "unknown_schema"
    assert CORRELATION_CANARY not in repr(failed)


def test_a_receiver_store_refuses_an_ambiguous_identity_without_echoing_it(tmp_path: Path):
    store = JsonReceiverStore(AtomicFiles(root=tmp_path / "state"))
    stored = replace(saved_receiver(), receiver_id=AMBIGUOUS)
    refused = run(store.put(stored))
    assert refused.error.detail == "ambiguous_identity"
    assert DIGEST not in repr(refused)
    assert list((tmp_path / "state").rglob("*.json")) == []


# --------------------------------------------------------------------------- no generic projection


def test_the_storage_package_never_uses_a_generic_projection():
    """``asdict``/``pickle``/``vars`` would serialize whatever a DTO happens to hold.

    Each of those would copy every field of a record -- including a field someone
    later adds for a transient value -- with no allowlist in the way, so the
    storage package uses none of them.
    """
    forbidden_imports = {"pickle", "copyreg", "shelve", "marshal"}
    forbidden_calls = {"asdict", "astuple", "vars"}
    for path in sorted(STORAGE_SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_imports, path.name
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root not in forbidden_imports, path.name
                for alias in node.names:
                    assert alias.name not in forbidden_actions(), (path.name, alias.name)
            elif isinstance(node, ast.Call):
                assert _call_name(node) not in forbidden_calls, path.name
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {"__dict__", "__reduce__", "__getstate__"}, path.name


def forbidden_actions() -> frozenset[str]:
    return frozenset({"asdict", "astuple"})


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_the_storage_package_has_no_secret_typed_field():
    """No record field is named for a secret, so nothing secret can be added by habit."""
    suspicious = {"pin", "password", "secret", "token", "credential", "passphrase"}
    for path in sorted(STORAGE_SOURCE.glob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        for name in suspicious:
            # ``owner_token`` is the daemon's own runtime identity, and the
            # capture token / credential reference are the two fields this
            # package deliberately drops; all three are accounted for in the
            # record-shape tests above.
            occurrences = text.count(f'"{name}"')
            assert occurrences == 0, (path.name, name, occurrences)
