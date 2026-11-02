from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import Ok
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage import codecs, repositories

from test_reservation_codecs import (
    APP_ID, BOOT_ID, PublicationFault, assert_bad_leaf, assert_refused,
    malformed_documents, persistence_refusal, supervisor_document, assert_store_leaf_refused,
)


@pytest.fixture
def supervisor_release_intent_documents():
    return supervisor_document()


def store_at(root, filesystem=None):
    assert hasattr(repositories, "JsonSupervisorReleaseIntentStore"), "missing supervisor intent store"
    return repositories.JsonSupervisorReleaseIntentStore(AtomicFiles(root, filesystem=filesystem))


async def test_supervisor_release_intent_codec_rejection_matrix(tmp_path, supervisor_release_intent_documents):
    item = supervisor_release_intent_documents
    raw = codecs.encode_supervisor_release_intent(item)
    files = AtomicFiles(tmp_path)
    store = repositories.JsonSupervisorReleaseIntentStore(files)
    assert set(json.loads(raw)) == {
        "schema", "version", "schema_version", "launch_uuid", "instance_uuid", "app",
        "supervisor", "daemon", "reason", "intent_at",
    }
    assert codecs.decode_supervisor_release_intent(raw) == item
    for label, payload in malformed_documents(raw):
        assert_refused(codecs.decode_supervisor_release_intent, payload)
        files = AtomicFiles(tmp_path)
        files.write("release-intent.json", payload)
        persistence_refusal(await repositories.JsonSupervisorReleaseIntentStore(files).read())
        assert files.read("release-intent.json") == payload
    for name, values in {
        "schema_version": (True, [], None, "1", 0, 2),
        "launch_uuid": (True, [], None, "bad", BOOT_ID.upper(), BOOT_ID.replace("-", "")),
        "instance_uuid": (True, [], None, "bad", BOOT_ID.upper(), BOOT_ID.replace("-", "")),
        "reason": (True, [], None, "bad"),
        "intent_at": (True, [], None, float("nan"), float("inf")),
    }.items():
        for value in values:
            await assert_store_leaf_refused(item, name, value, codecs.encode_supervisor_release_intent,
                                           codecs.decode_supervisor_release_intent, files, store, "release-intent.json", reader="read")
    for name in ("app", "supervisor", "daemon"):
        for value in (None, True, [], "bad", ProcessIdentity(pid=0, start_ticks=1),
                      ProcessIdentity(pid=True, start_ticks=1), ProcessIdentity(pid=2**31, start_ticks=1),
                      ProcessIdentity(pid=1, start_ticks=-1), ProcessIdentity(pid=1, start_ticks=True)):
            await assert_store_leaf_refused(item, name, value, codecs.encode_supervisor_release_intent,
                                           codecs.decode_supervisor_release_intent, files, store, "release-intent.json", reader="read")
    files.write("release-intent.json", raw)
    assert await store.read() == Ok(value=item)
    assert isinstance(await store.write(item), Ok)
    persistence_refusal(await store.write(replace(item, intent_at=3.0)))
    assert files.read("release-intent.json") == raw


async def test_changed_supervisor_intent_repeat_refuses_without_rewriting(tmp_path):
    item = supervisor_document()
    store = store_at(tmp_path)
    assert isinstance(await store.write(item), Ok)
    before = AtomicFiles(tmp_path).read("release-intent.json")
    changes = {
        "launch_uuid": UUID(int=99), "instance_uuid": UUID(int=98),
        "app": ProcessIdentity(pid=32001, start_ticks=1),
        "supervisor": ProcessIdentity(pid=32002, start_ticks=1),
        "daemon": ProcessIdentity(pid=32003, start_ticks=1),
        "reason": "parent-mismatch", "intent_at": 3.0,
    }
    for name, value in changes.items():
        persistence_refusal(await store_at(tmp_path).write(replace(item, **{name: value})))
        assert AtomicFiles(tmp_path).read("release-intent.json") == before
    assert isinstance(await store_at(tmp_path).write(item), Ok)
    assert await store_at(tmp_path).read() == Ok(value=item)


@pytest.mark.parametrize("reopen", (False, True))
async def test_supervisor_repeat_confirms_durability_after_uncertain_publication(tmp_path, reopen):
    item = supervisor_document()
    fault = PublicationFault()
    store = store_at(tmp_path, fault)
    persistence_refusal(await store.write(item))
    before = AtomicFiles(tmp_path).read("release-intent.json")
    assert before == codecs.encode_supervisor_release_intent(item)
    confirmations = fault.confirmations
    if reopen:
        store = store_at(tmp_path, fault)
    persistence_refusal(await store.write(item))
    assert fault.confirmations > confirmations
    assert AtomicFiles(tmp_path).read("release-intent.json") == before
    fault.step = None
    assert isinstance(await store.write(item), Ok)


@pytest.mark.parametrize("step", ("write", "fsync", "rename", "directory_fsync"))
async def test_supervisor_intent_atomic_step_failures_are_typed(tmp_path, step):
    store = store_at(tmp_path, PublicationFault(step=step))
    persistence_refusal(await store.write(supervisor_document()))
