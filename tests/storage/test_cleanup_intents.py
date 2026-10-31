from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.resources import ErrorCode, Ok
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage import codecs, repositories

from test_reservation_codecs import (
    LAUNCH_ID, BOOT_ID, PublicationFault, assert_bad_leaf, assert_refused, cleanup_documents,
    malformed_documents, persistence_refusal, wire, assert_store_leaf_refused,
)


@pytest.fixture
def cleanup_intent_documents():
    return cleanup_documents()


def store_at(root, filesystem=None):
    assert hasattr(repositories, "JsonDaemonCleanupIntentStore"), "missing cleanup intent store"
    return repositories.JsonDaemonCleanupIntentStore(AtomicFiles(root, filesystem=filesystem))


def key():
    return f"daemon/cleanup-intents/{LAUNCH_ID}.json"


async def test_cleanup_intent_codec_rejection_matrix(tmp_path, cleanup_intent_documents):
    for index, item in enumerate(cleanup_intent_documents):
        raw = codecs.encode_daemon_cleanup_intent(item)
        files = AtomicFiles(tmp_path / str(index))
        store = repositories.JsonDaemonCleanupIntentStore(files)
        assert set(json.loads(raw)) == {
            "schema", "version", "launch_uuid", "reason", "intent_at", "completed_at", "outcome",
        }
        assert codecs.decode_daemon_cleanup_intent(raw) == item
        for label, payload in malformed_documents(raw):
            assert_refused(codecs.decode_daemon_cleanup_intent, payload)
            files = AtomicFiles(tmp_path / str(index))
            files.write(key(), payload)
            persistence_refusal(await repositories.JsonDaemonCleanupIntentStore(files).all())
            assert files.read(key()) == payload
        for name, values in {
            "launch_uuid": (True, [], None, "bad", BOOT_ID.upper(), LAUNCH_ID.hex),
            "reason": (True, [], None, "bad"),
            "intent_at": (True, [], None, float("nan"), float("inf")),
            "completed_at": (True, [], float("nan"), float("inf")),
            "outcome": (True, [], "bad"),
        }.items():
            for value in values:
                await assert_store_leaf_refused(item, name, value, codecs.encode_daemon_cleanup_intent,
                                               codecs.decode_daemon_cleanup_intent, files, store, key())
        files.write(key(), raw)
        assert await store.all() == Ok(value=(item,))
        assert isinstance(await store.write(item), Ok)
        if item.completed_at is not None:
            persistence_refusal(await store.write(replace(item, completed_at=4.0)))
            assert files.read(key()) == raw
    initial, completed, _ = cleanup_intent_documents
    for item in (replace(initial, outcome="stopped"), replace(completed, completed_at=None),
                 replace(completed, outcome=None)):
        with pytest.raises(codecs.RecordError):
            codecs.encode_daemon_cleanup_intent(item)
        valid = json.loads(codecs.encode_daemon_cleanup_intent(initial))
        valid.update(completed_at=item.completed_at, outcome=item.outcome)
        assert_refused(codecs.decode_daemon_cleanup_intent, wire(valid))
    files = AtomicFiles(tmp_path / "filename")
    files.write(f"daemon/cleanup-intents/{UUID(int=99)}.json", codecs.encode_daemon_cleanup_intent(initial))
    persistence_refusal(await repositories.JsonDaemonCleanupIntentStore(files).all())


async def test_cleanup_first_publication_requires_explicit_null_completion(tmp_path):
    initial, completed, timed_out = cleanup_documents()
    for index, item in enumerate((completed, timed_out)):
        root = tmp_path / str(index)
        store = store_at(root)
        persistence_refusal(await store.write(item))
        assert AtomicFiles(root).read(key()) is None
        assert await store.all() == Ok(value=())
        assert isinstance(await store.write(initial), Ok)
        assert isinstance(await store.write(item), Ok)


async def test_cleanup_completion_is_one_way_and_reopen_preserves_exact_bytes(tmp_path):
    initial, completed, timed_out = cleanup_documents()
    store = store_at(tmp_path)
    assert isinstance(await store.write(initial), Ok)
    assert isinstance(await store.write(completed), Ok)
    before = AtomicFiles(tmp_path).read(key())
    store = store_at(tmp_path)
    for item in (initial, timed_out, replace(completed, intent_at=1.0),
                 replace(completed, reason=ErrorCode.cleanup_timeout)):
        persistence_refusal(await store.write(item))
        assert AtomicFiles(tmp_path).read(key()) == before
    assert isinstance(await store.write(completed), Ok)
    assert AtomicFiles(tmp_path).read(key()) == before
    assert await store_at(tmp_path).all() == Ok(value=(completed,))


@pytest.mark.parametrize("reopen", (False, True))
async def test_completed_cleanup_repeat_confirms_durability_after_uncertain_publication(tmp_path, reopen):
    initial, completed, _ = cleanup_documents()
    fault = PublicationFault(step=None)
    store = store_at(tmp_path, fault)
    assert isinstance(await store.write(initial), Ok)
    fault.step = "directory_fsync"
    persistence_refusal(await store.write(completed))
    before = AtomicFiles(tmp_path).read(key())
    assert before == codecs.encode_daemon_cleanup_intent(completed)
    confirmations = fault.confirmations
    if reopen:
        store = store_at(tmp_path, fault)
    persistence_refusal(await store.write(completed))
    assert fault.confirmations > confirmations
    assert AtomicFiles(tmp_path).read(key()) == before
    fault.step = None
    assert isinstance(await store.write(completed), Ok)


@pytest.mark.parametrize("step", ("write", "fsync", "rename", "directory_fsync"))
async def test_cleanup_intent_atomic_step_failures_are_typed(tmp_path, step):
    store = store_at(tmp_path, PublicationFault(step=step))
    persistence_refusal(await store.write(cleanup_documents()[0]))
