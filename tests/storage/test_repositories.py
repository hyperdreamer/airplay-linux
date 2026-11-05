from __future__ import annotations

import asyncio
import inspect
import json
import sys
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain import ports
from airplay_linux.domain.resources import Err, ErrorCode, Ok, VolumeLevel
from airplay_linux.testing.adapters import Deferred, FakeDiscovery, MemoryReceiverStore
from airplay_linux.testing.builders import target
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


@pytest.mark.parametrize("first_read", ["get", "all"])
@pytest.mark.parametrize("shape", ["integer", "negative", "array", "object"])
async def test_oversized_volume_token_drops_only_preference(tmp_path, first_read, shape):
    """Raw numeric tokens beyond int's digit limit must not block the receiver list."""
    from urllib.parse import quote

    files = AtomicFiles(tmp_path)
    warnings = []
    store = repositories.JsonReceiverStore(files, on_diagnostic=warnings.append)
    original = saved(remembered_volume=30)
    sibling = saved(receiver_id="airplay-device:b", last_endpoint=None)
    assert await store.put(sibling) == Ok(value=None)
    digits = b"9" * max(5000, sys.get_int_max_str_digits() + 1)
    leaf = {"integer": digits, "negative": b"-" + digits,
            "array": b"[" + digits + b"]", "object": b'{"level":' + digits + b"}"}[shape]
    payload = codecs.encode_receiver(original).replace(b'"remembered_volume":30', b'"remembered_volume":' + leaf)
    key = "receivers/" + quote(original.receiver_id, safe="") + ".json"
    files.write(key, payload)
    clean = replace(original, remembered_volume=None)
    if first_read == "get":
        assert await store.get(original.receiver_id) == Ok(value=clean)
    else:
        assert await store.all() == Ok(value=(clean, sibling))
    assert warnings == [diagnostic(ErrorCode.volume_preference_invalid, original.receiver_id)]
    assert await store.get(original.receiver_id) == Ok(value=clean)
    assert await store.all() == Ok(value=(clean, sibling))
    assert len(warnings) == 1 and files.read(key) == payload


@pytest.mark.parametrize("field", [
    "receiver_id", "service_metadata", "display_name", "last_endpoint",
    "continuity_fingerprint", "remembered_resolution", "version", "schema",
])
async def test_oversized_old_receiver_field_still_refuses_record(tmp_path, field):
    from urllib.parse import quote

    files = AtomicFiles(tmp_path)
    warnings = []
    store = repositories.JsonReceiverStore(files, on_diagnostic=warnings.append)
    original = saved(remembered_volume=30)
    raw = json.loads(codecs.encode_receiver(original))
    # A placeholder lets the test inject an unquoted integer without int()/dumps().
    raw[field] = "oversized-numeric-token"
    payload = json.dumps(raw).encode().replace(b'"oversized-numeric-token"', b"9" * max(5000, sys.get_int_max_str_digits() + 1))
    key = "receivers/" + quote(original.receiver_id, safe="") + ".json"
    files.write(key, payload)
    assert isinstance(await store.get(original.receiver_id), Err)
    assert isinstance(await store.all(), Err)
    assert not warnings and files.read(key) == payload


@pytest.fixture
def interleaved_preferences():
    class BarrierStore(MemoryReceiverStore):
        def __init__(self):
            super().__init__()
            self.get_entered, self.get_release = Deferred(), Deferred()
            self.put_entered, self.put_release = Deferred(), Deferred()
            self.gets = []
            self.hold_get = self.hold_put = True

        async def get(self, receiver_id):
            self.gets.append(receiver_id)
            if receiver_id == target().receiver_id and self.hold_get:
                self.hold_get = False
                self.get_entered.resolve(None)
                await self.get_release.wait()
            return await super().get(receiver_id)

        async def put(self, record):
            if record.receiver_id == target().receiver_id and self.hold_put:
                self.hold_put = False
                self.put_entered.resolve(None)
                await self.put_release.wait()
            return await super().put(record)

    store, discovery = BarrierStore(), FakeDiscovery()
    discovery.add_target(target())
    discovery.add_target(target("b", address="192.0.2.11"))
    return store, discovery


def test_preferences_writer_api():
    writer = repositories.ReceiverPreferenceWriter
    assert inspect.iscoroutinefunction(writer.update)
    assert tuple(inspect.signature(writer.update).parameters) == (
        "self", "record_set", "remembered_resolution", "remembered_volume", "still_current",
    )


@pytest.mark.parametrize("volume_first", [False, True])
async def test_preferences_merge_both_orderings_and_independent_receivers(interleaved_preferences, volume_first):
    store, discovery = interleaved_preferences
    writer = repositories.ReceiverPreferenceWriter(store, discovery)
    record_set = discovery.current(target().receiver_id).record_set
    original = saved(remembered_resolution=None)
    store.receivers[original.receiver_id] = original
    resolution = saved().remembered_resolution
    fields = [{"remembered_resolution": resolution}, {"remembered_volume": VolumeLevel(value=30)}]
    if volume_first:
        fields.reverse()
    async with asyncio.TaskGroup() as tasks:
        first = tasks.create_task(writer.update(record_set, **fields[0], still_current=lambda: True))
        await asyncio.wait_for(store.get_entered.wait(), 1)
        attempted = asyncio.Event()
        async def second_write():
            attempted.set()
            return await writer.update(record_set, **fields[1], still_current=lambda: True)
        second = tasks.create_task(second_write())
        await attempted.wait()
        assert store.gets.count(original.receiver_id) == 1
        other = discovery.current(target("b").receiver_id).record_set
        independent = await asyncio.wait_for(writer.update(other, remembered_volume=VolumeLevel(value=40), still_current=lambda: True), 1)
        assert isinstance(independent, Ok)
        assert store.receivers[other.receiver_id].remembered_volume == 40
        store.get_release.resolve(None)
        await asyncio.wait_for(store.put_entered.wait(), 1)
        assert store.gets.count(original.receiver_id) == 1
        store.put_release.resolve(None)
    assert isinstance(first.result(), Ok) and isinstance(second.result(), Ok)
    assert store.receivers[original.receiver_id] == replace(original, remembered_resolution=resolution, remembered_volume=30)
    assert second.result().value == store.receivers[original.receiver_id]


@pytest.mark.parametrize("kwargs", [
    {}, {"remembered_volume": VolumeLevel(value=0)}, {"remembered_volume": 30},
    {"remembered_resolution": "bad"},
    {"remembered_volume": VolumeLevel(value=30), "remembered_resolution": saved().remembered_resolution},
])
async def test_preferences_reject_invalid_arguments_without_io(kwargs):
    store, discovery = MemoryReceiverStore(), FakeDiscovery()
    discovery.add_target(target())
    writer = repositories.ReceiverPreferenceWriter(store, discovery)
    result = await writer.update(discovery.current(target().receiver_id).record_set, **kwargs, still_current=lambda: True)
    assert isinstance(result, Err) and result.error.code is ErrorCode.invalid_input
    assert store.journal.names() == []


@pytest.mark.parametrize("phase", ["after_get", "before_put", "after_put"])
@pytest.mark.parametrize("invalidate", ["intent", "replacement", "offline", "continuity"])
async def test_preferences_recheck_after_await_and_never_roll_back_newer_bytes(phase, invalidate):
    discovery = FakeDiscovery()
    discovery.add_target(target())
    view = discovery.current(target().receiver_id)
    record_set = replace(view.record_set, identity_kind="fingerprint") if invalidate == "continuity" else view.record_set
    discovery.views[target().receiver_id] = replace(view, record_set=record_set)
    current = True
    original = saved(remembered_volume=20)
    newer = replace(original, remembered_volume=80)
    def invalidate_now():
        nonlocal current
        if invalidate == "intent":
            current = False
        elif invalidate == "replacement":
            discovery.add_target(target(endpoint_generation=2))
        elif invalidate == "offline":
            discovery.remove_target(original.receiver_id)
        else:
            discovery.views[original.receiver_id] = replace(view, record_set=replace(record_set, continuity_verified=False))

    class Store(MemoryReceiverStore):
        async def get(self, receiver_id):
            result = await super().get(receiver_id)
            if phase == "after_get":
                invalidate_now()
            return result
        async def put(self, record):
            result = await super().put(record)
            if phase == "after_put":
                invalidate_now()
                self.receivers[record.receiver_id] = newer
            return result
    store = Store()
    store.receivers[original.receiver_id] = original
    if phase == "before_put":
        invalidate_now()
    result = await repositories.ReceiverPreferenceWriter(store, discovery).update(
        record_set, remembered_volume=VolumeLevel(value=30), still_current=lambda: current,
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.stale_target
    assert store.receivers[original.receiver_id] == (newer if phase == "after_put" else original)
    assert len(store.journal.calls("receiver_store.put")) == (1 if phase == "after_put" else 0)


@pytest.mark.parametrize("change", [
    {"persistable": False}, {"ambiguous": True}, {"replacement_of": "airplay-device:b"},
    {"identity_kind": "fingerprint", "continuity_verified": False},
])
@pytest.mark.parametrize("exists", [False, True])
async def test_preferences_refuse_unjustified_identity(change, exists):
    store, discovery = MemoryReceiverStore(), FakeDiscovery()
    discovery.add_target(target())
    view = discovery.current(target().receiver_id)
    record_set = replace(view.record_set, **change)
    discovery.views[target().receiver_id] = replace(view, record_set=record_set)
    if exists:
        store.receivers[target().receiver_id] = saved()
    result = await repositories.ReceiverPreferenceWriter(store, discovery).update(
        record_set, remembered_volume=VolumeLevel(value=30), still_current=lambda: True,
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.stale_target
    assert not store.journal.calls("receiver_store.put")


async def test_preferences_refuse_existing_fallback_record_without_continuity():
    store, discovery = MemoryReceiverStore(), FakeDiscovery()
    discovery.add_target(target())
    view = discovery.current(target().receiver_id)
    record_set = replace(view.record_set, identity_kind="fingerprint", continuity_verified=True)
    discovery.views[target().receiver_id] = replace(view, record_set=record_set)
    original = saved(continuity_fingerprint="older-endpoint", remembered_volume=20)
    store.receivers[original.receiver_id] = original
    result = await repositories.ReceiverPreferenceWriter(store, discovery).update(
        record_set, remembered_volume=VolumeLevel(value=30), still_current=lambda: True,
    )
    assert isinstance(result, Err) and result.error.code is ErrorCode.stale_target
    assert store.receivers[original.receiver_id] == original
    assert not store.journal.calls("receiver_store.put")


async def test_preferences_create_current_metadata_and_preserve_typed_store_failures():
    store, discovery = MemoryReceiverStore(), FakeDiscovery()
    discovery.add_target(target())
    view = discovery.current(target().receiver_id)
    writer = repositories.ReceiverPreferenceWriter(store, discovery)
    created = await writer.update(view.record_set, remembered_volume=VolumeLevel(value=30), still_current=lambda: True)
    assert created == Ok(value=saved(display_name=view.display_name, remembered_resolution=None, remembered_volume=30))
    failure = Err(error=diagnostic(ErrorCode.persistence_failed, target().receiver_id))
    for method in ("get", "put"):
        store.queue_result(method, failure)
        assert await writer.update(view.record_set, remembered_volume=VolumeLevel(value=40), still_current=lambda: True) == failure
        assert store.receivers[target().receiver_id] == created.value


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
