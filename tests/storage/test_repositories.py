from __future__ import annotations

import inspect
import json
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain import ports
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.diagnostics import diagnostic
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage import codecs, repositories
import airplay_linux.storage as storage

from test_atomic_records import daemon, ownership, rich_snapshot, saved
from test_reservation_codecs import (
    assert_refused, cleanup_documents, malformed_documents, owner_document,
    persistence_refusal, receipt_states, reservation, supervisor_document, verdict_document,
)


async def test_legacy_store_roundtrip_is_byte_identical_while_new_types_are_strict(tmp_path):
    files = AtomicFiles(tmp_path)
    cases = (
        (rich_snapshot(), codecs.encode_session, codecs.decode_session, "sessions", repositories.JsonSessionStore(files), "save", "load_all"),
        (ownership(), codecs.encode_ownership, codecs.decode_ownership, "ownership", repositories.JsonOwnershipStore(files), "write", "all"),
        (daemon(), codecs.encode_daemon, codecs.decode_daemon, "daemon", repositories.JsonDaemonStore(files), "write", "all"),
        (saved(), codecs.encode_receiver, codecs.decode_receiver, "receivers", repositories.JsonReceiverStore(files), "put", "all"),
    )
    for item, encode, decode, directory, store, writer, reader in cases:
        raw = encode(item)
        doc = json.loads(raw)
        if directory == "receivers":
            assert doc["version"] == 2
            assert "remembered_volume" in doc
            old = {key: value for key, value in doc.items() if key != "remembered_volume"}
            old["version"] = 1
            assert decode(json.dumps(old).encode()) == item
        else:
            assert doc["version"] == 1
        # Pin delivered duplicate-member and trailing-whitespace tolerance in every family.
        duplicated = b'{"schema":' + json.dumps(doc["schema"]).encode() + b"," + raw[1:] + b" \n\t"
        assert decode(duplicated) == decode(raw)
        assert isinstance(await getattr(store, writer)(item), Ok)
        names = files.names(directory)
        assert len(names) == 1
        key = directory + "/" + names[0]
        assert files.read(key) == raw
        files.write(key, duplicated)
        loaded = await getattr(store, reader)()
        loaded = loaded.value if isinstance(loaded, Ok) else loaded
        assert len(loaded) == 1
        assert isinstance(await getattr(store, writer)(loaded[0]), Ok)
        assert files.read(key) == raw
        if directory == "daemon":
            assert json.loads(raw)["owner_token"] == "owner-token-1"
            assert set(json.loads(raw)) == {
                "schema", "version", "schema_version", "instance_uuid", "owner_token", "app",
                "supervisor", "daemon", "protocol_version", "socket_path", "lifecycle", "runtime_directory",
            }
    for item, encode, decode in (
        (reservation(), codecs.encode_daemon_reservation, codecs.decode_daemon_reservation),
        (receipt_states()["reserved"], codecs.encode_launch_receipt, codecs.decode_launch_receipt),
        (cleanup_documents()[0], codecs.encode_daemon_cleanup_intent, codecs.decode_daemon_cleanup_intent),
        (supervisor_document(), codecs.encode_supervisor_release_intent, codecs.decode_supervisor_release_intent),
        (owner_document(), codecs.encode_authority_owner, codecs.decode_authority_owner),
        (verdict_document(), codecs.encode_probe_verdict, codecs.decode_probe_verdict),
    ):
        raw = encode(item)
        assert decode(raw) == item
        for label, payload in malformed_documents(raw):
            assert_refused(decode, payload)
        for wrong in (None, {}, "invalid", daemon()):
            with pytest.raises(codecs.RecordError):
                encode(wrong)


@pytest.mark.parametrize("raises", [False, True])
async def test_receiver_volume_warnings_are_content_sensitive_and_never_rewrite(tmp_path, raises):
    from urllib.parse import quote
    files = AtomicFiles(tmp_path)
    warnings = []
    def receive(warning):
        warnings.append(warning)
        if raises:
            raise RuntimeError("secret callback text")
    store = repositories.JsonReceiverStore(files, on_diagnostic=receive)
    original = saved(remembered_volume=30)
    sibling = saved(receiver_id="airplay-device:b", last_endpoint=None)
    assert await store.put(sibling) == Ok(value=None)
    key = "receivers/" + quote(original.receiver_id, safe="") + ".json"
    raw = json.loads(codecs.encode_receiver(original))
    async def check_bad(value, count):
        payload = json.dumps(raw | {"remembered_volume": value}).encode()
        files.write(key, payload)
        for _ in range(2):
            assert await store.get(original.receiver_id) == Ok(value=replace(original, remembered_volume=None))
            listed = await store.all()
            assert isinstance(listed, Ok)
            assert set(item.receiver_id for item in listed.value) == {original.receiver_id, sibling.receiver_id}
        assert files.read(key) == payload
        assert warnings == [diagnostic(ErrorCode.volume_preference_invalid, original.receiver_id)] * count
    await check_bad("secret-invalid-volume", 1)
    await check_bad(0, 2)
    files.write(key, codecs.encode_receiver(original))
    assert await store.all() == Ok(value=(original, sibling))
    await check_bad(0, 3)
    files.write(key, json.dumps(raw | {"receiver_id": "secret-invalid-identity", "remembered_volume": 0}).encode())
    assert isinstance(await store.all(), Err)
    assert len(warnings) == 3


def test_public_exports_and_receipt_free_process_signature():
    for name in ("JsonLaunchReceiptStore", "JsonDaemonCleanupIntentStore", "JsonSupervisorReleaseIntentStore"):
        assert hasattr(repositories, name), name
        assert getattr(storage, name) is getattr(repositories, name)
        assert name in storage.__all__
        assert inspect.iscoroutinefunction(getattr(getattr(storage, name), "write"))
        assert not hasattr(getattr(storage, name), "delete")
    for stem in ("daemon_reservation", "launch_receipt", "daemon_cleanup_intent", "supervisor_release_intent", "authority_owner", "probe_verdict"):
        for prefix in ("encode_", "decode_"):
            name = prefix + stem
            assert getattr(storage, name) is getattr(codecs, name)
            assert name in storage.__all__
    for name in ("LaunchReceiptStore", "DaemonCleanupIntentStore", "SupervisorReleaseIntentStore"):
        assert hasattr(ports, name), name
        assert inspect.iscoroutinefunction(getattr(getattr(ports, name), "write"))
    parameters = inspect.signature(ports.ProcessPort.terminate).parameters
    assert tuple(parameters) == ("self", "identity", "owner_uuid", "runtime_directory", "deadline", "strict", "evidence", "revalidate")
    assert parameters["strict"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["strict"].default is False
    assert parameters["evidence"].default is parameters["revalidate"].default is None
    assert "LaunchReceipt" not in str(inspect.signature(ports.ProcessPort.terminate))


@pytest.mark.parametrize("directory,kind", (("daemon/receipts", "receipt"), ("daemon/cleanup-intents", "cleanup")))
@pytest.mark.parametrize("filename", ("notes.txt", "unexpected", "not-a-uuid.json", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA.json", "nested"))
async def test_lifecycle_enumeration_never_skips_misnamed_files_or_directories(tmp_path, directory, kind, filename):
    files = AtomicFiles(tmp_path)
    if kind == "receipt":
        item = receipt_states()["reserved"]
        encode = codecs.encode_launch_receipt
        store = repositories.JsonLaunchReceiptStore(files)
    else:
        item = cleanup_documents()[0]
        encode = codecs.encode_daemon_cleanup_intent
        store = repositories.JsonDaemonCleanupIntentStore(files)
    assert isinstance(await store.write(item), Ok)
    if filename == "nested":
        files.write(directory + "/nested/hidden.json", b"{}")
    else:
        files.write(directory + "/" + filename, encode(item))
    persistence_refusal(await store.all())
    # A valid target is still blocked by unrelated, unclassified durable evidence.
    persistence_refusal(await store.write(item))
    assert files.read(directory + "/" + str(item.launch_uuid) + ".json") == encode(item)


@pytest.mark.parametrize("kind", ("receipt", "cleanup"))
async def test_filename_canonical_uuid_is_required_even_when_identity_matches(tmp_path, kind):
    item = receipt_states()["reserved"] if kind == "receipt" else cleanup_documents()[0]
    item = replace(item, launch_uuid=UUID("abcdefab-cdef-4abc-8def-abcdefabcdef"))
    encode = codecs.encode_launch_receipt if kind == "receipt" else codecs.encode_daemon_cleanup_intent
    directory = "daemon/receipts" if kind == "receipt" else "daemon/cleanup-intents"
    store_cls = repositories.JsonLaunchReceiptStore if kind == "receipt" else repositories.JsonDaemonCleanupIntentStore
    files = AtomicFiles(tmp_path)
    key = directory + "/" + str(item.launch_uuid).upper() + ".json"
    files.write(key, encode(item))
    persistence_refusal(await store_cls(files).all())
    assert files.read(key) == encode(item)


@pytest.mark.parametrize("kind", ("receipt", "cleanup"))
async def test_unreadable_or_disappearing_evidence_never_becomes_an_empty_success(tmp_path, kind):
    from airplay_linux.storage import PosixFilesystem, StorageError
    class Unreadable(PosixFilesystem):
        fail = False
        def read(self, parent, name):
            if self.fail:
                raise PermissionError("never-copy-this")
            return super().read(parent, name)
    fs = Unreadable()
    files = AtomicFiles(tmp_path, filesystem=fs)
    if kind == "receipt":
        item = receipt_states()["reserved"]
        store = repositories.JsonLaunchReceiptStore(files)
    else:
        item = cleanup_documents()[0]
        store = repositories.JsonDaemonCleanupIntentStore(files)
    assert isinstance(await store.write(item), Ok)
    fs.fail = True
    persistence_refusal(await store.all())
    persistence_refusal(await store.write(item))
