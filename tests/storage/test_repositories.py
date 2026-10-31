from __future__ import annotations

import inspect
import json
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain import ports
from airplay_linux.domain.resources import Ok
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
