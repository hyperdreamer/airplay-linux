"""Real shared flocks and durable owners; all process evidence is offline."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from airplay_linux.backend.control import ApplicationContractProfile
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import AuthorityOwner
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, OwnedProcessScan
from airplay_linux.storage import AtomicFiles, PosixFilesystem
from airplay_linux.storage.codecs import (
    RecordError, decode_authority_owner, encode_authority_owner,
)
from airplay_linux.storage.repositories import JsonDaemonStore
from airplay_linux.testing.adapters import FakeProcess

BOOT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
APP_A = ProcessIdentity(pid=31001, start_ticks=81)
APP_B = ProcessIdentity(pid=31002, start_ticks=82)


def authority_class():
    name = "airplay_linux.process.authority"
    assert importlib.util.find_spec(name) is not None, "missing shared-root flock authority"
    module = importlib.import_module(name)
    assert module.AuthorityOwner is AuthorityOwner
    return module.SharedRootAuthority


def test_authority_uses_the_canonical_owner_type():
    assert authority_class() is not None


def owner(app=APP_A, number=1):
    return AuthorityOwner(
        schema="airplay-linux/daemon-authority-owner/v1",
        instance_uuid=InstanceId(UUID(int=number)), app=app,
        boot_id=BOOT, acquired_at=1.25,
    )


@pytest.fixture
def two_registry_barrier(tmp_path):
    live = {APP_A.pid: APP_A, APP_B.pid: APP_B}
    unreadable = set()

    def inspect(pid):
        if pid in unreadable:
            return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
        identity = live.get(pid)
        return IdentityEvidence(
            pid=pid, state=IdentityState.present if identity else IdentityState.absent,
            identity=identity, uid=os.getuid(),
        )

    cls = authority_class()
    a = cls(tmp_path, inspect_process=inspect, boot_id=lambda: BOOT)
    b = cls(tmp_path, inspect_process=inspect, boot_id=lambda: BOOT)
    launch_calls = []

    class Recorder(FakeProcess):
        async def spawn(self, spec):
            launch_calls.append(spec)
            return await super().spawn(spec)

    processes = [Recorder(), Recorder()]
    owners = [owner(), owner(APP_B, 2)]
    registries = []
    for index, (authority, instance, process) in enumerate(zip((a, b), owners, processes)):
        process.queue_result("spawn", Err(error=diagnostic(ErrorCode.daemon_failed)))
        registries.append(DaemonRegistry(
            process=process, store=JsonDaemonStore(AtomicFiles(tmp_path)),
            control_factory=lambda record: pytest.fail("unexpected backend control"),
            features=ApplicationContractProfile(socket_path="/unused").features,
            runtime_directory=tmp_path / f"instance-{index}", record_root=tmp_path,
            app=instance.app, app_instance_uuid=instance.instance_uuid, authority=authority,
            inspect_process=inspect,
            scan_owned=lambda *_: OwnedProcessScan(matches=(), complete=True),
        ))

    async def publish_initial():
        acquired = await a.acquire(owners[0])
        assert isinstance(acquired, Ok)
        try:
            assert await a.publish_owner(owners[0]) == Ok(value=None)
        finally:
            acquired.value.unlock()

    asyncio.run(publish_initial())
    return SimpleNamespace(
        authority_a=a, authority_b=b, owner_a=owners[0], owner_b=owners[1],
        registry_a=registries[0], registry_b=registries[1], processes=processes,
        owner_path=tmp_path / "daemon/authority.owner", root=tmp_path,
        live=live, unreadable=unreadable, launch_calls=launch_calls, inspect=inspect,
    )


async def test_owner_survives_unlock_and_blocks_second_instance(two_registry_barrier):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    before = c.owner_path.read_bytes()
    first.value.unlock()
    first.value.unlock()
    assert c.owner_path.read_bytes() == before
    second = await c.authority_b.acquire(c.owner_b)
    assert isinstance(second, Err) and second.error.code == ErrorCode.ownership_mismatch
    assert c.launch_calls == []
    assert all(not process.spawned and not process.terminated for process in c.processes)


async def test_shared_flock_serializes_owner_publication(two_registry_barrier):
    """Removing flock lets both independent registries reach the publication barrier."""
    c = two_registry_barrier
    lease = await c.authority_a.acquire(c.owner_a)
    assert isinstance(lease, Ok)
    try:
        assert await c.authority_a.clear_owner(c.owner_a) == Ok(value=None)
    finally:
        lease.value.unlock()
    entered = []
    publication = asyncio.Event()
    release = asyncio.Event()
    for authority in (c.authority_a, c.authority_b):
        original = authority.publish_owner

        async def gated(instance, original=original):
            entered.append(instance.instance_uuid)
            publication.set()
            await release.wait()
            return await original(instance)

        authority.publish_owner = gated
    first = asyncio.create_task(c.registry_a.ensure())
    await asyncio.wait_for(publication.wait(), 1.0)
    second = asyncio.create_task(c.registry_b.ensure())
    try:
        # The second fd must contend on the real flock while the first writer
        # is paused before publication, with no in-process lock in common.
        await asyncio.sleep(0.12)
        assert len(entered) == 1, "flock removal admitted both owner publishers"
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first, second), 2.0)
    assert len(entered) == 1
    assert isinstance(results[1], Err)
    assert results[1].error.code is ErrorCode.ownership_mismatch
    assert sum(len(p.journal.calls("process.spawn")) for p in c.processes) == 1
    assert len(c.launch_calls) == 1
    assert decode_authority_owner(c.owner_path.read_bytes()).instance_uuid == c.owner_a.instance_uuid


async def test_stale_clear_owner_cannot_remove_successor(two_registry_barrier):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    cleared = asyncio.Event()

    async def clear_then_wait():
        try:
            assert await c.authority_a.clear_owner(c.owner_a) == Ok(value=None)
            first.value.unlock()
            cleared.set()
            await asyncio.Future()
        finally:
            first.value.unlock()

    pending = asyncio.create_task(clear_then_wait())
    await asyncio.wait_for(cleared.wait(), 1.0)
    successor = await c.authority_b.acquire(c.owner_b)
    assert isinstance(successor, Ok)
    assert await c.authority_b.publish_owner(c.owner_b) == Ok(value=None)
    before = c.owner_path.read_bytes()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert c.owner_path.read_bytes() == before
    # Completed clear cannot be replayed after unlocking, nor through the
    # successor's lease with the old expected record.
    stale = await c.authority_a.clear_owner(c.owner_a)
    assert isinstance(stale, Err)
    stale = await c.authority_b.clear_owner(c.owner_a)
    assert isinstance(stale, Err)
    assert c.owner_path.read_bytes() == before
    first.value.unlock()
    assert c.owner_path.read_bytes() == before
    successor.value.unlock()


async def test_flock_deadline_and_backoff_map_concurrent_writer(two_registry_barrier):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    times = SimpleNamespace(now=11.0, delays=[])

    async def pause(delay):
        times.delays.append(delay)
        times.now += delay

    contender = authority_class()(
        c.root, inspect_process=c.inspect, boot_id=lambda: BOOT,
        clock=SimpleNamespace(now=lambda: times.now), sleep=pause,
    )
    before = c.owner_path.read_bytes()
    try:
        refused = await contender.acquire(c.owner_b)
    finally:
        first.value.unlock()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.ownership_mismatch
    assert times.now == pytest.approx(16.0)
    assert sum(times.delays) == pytest.approx(5.0)
    assert all(0 < delay <= 0.05 for delay in times.delays)
    assert len(times.delays) in {100, 101}
    assert c.owner_path.read_bytes() == before
    assert all(not p.spawned and not p.terminated for p in c.processes)


@pytest.mark.parametrize('elapsed', [5.0, 5.25])
async def test_expired_flock_budget_cannot_accept_a_late_lock(two_registry_barrier, elapsed):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    times = SimpleNamespace(now=11.0, delays=[])

    async def pause(delay):
        times.delays.append(delay)
        times.now += elapsed
        first.value.unlock()

    contender = authority_class()(
        c.root, inspect_process=c.inspect, boot_id=lambda: BOOT,
        clock=SimpleNamespace(now=lambda: times.now), sleep=pause,
    )
    before = c.owner_path.read_bytes()
    result = await contender.acquire(c.owner_a)
    if isinstance(result, Ok):
        result.value.unlock()
    assert isinstance(result, Err) and result.error.code is ErrorCode.ownership_mismatch
    assert times.delays == [0.05]
    assert c.owner_path.read_bytes() == before
    assert c.launch_calls == []


def malformed_owner_documents():
    valid = json.loads(encode_authority_owner(owner()))
    cases = []
    for key in valid:
        document = dict(valid)
        del document[key]
        cases.append(json.dumps(document).encode())
    for change in (
        {"schema": "airplay-linux/daemon-authority-owner/v2"}, {"version": 1},
        {"instance_uuid": "not-a-uuid"}, {"instance_uuid": valid["instance_uuid"].upper() + "x"},
        {"boot_id": "not-a-boot"}, {"boot_id": None},
        {"acquired_at": True}, {"acquired_at": "1"}, {"acquired_at": float("inf")},
        {"acquired_at": float("nan")}, {"app": None}, {"app": []},
        {"app": {"pid": True, "start_ticks": 81}},
        {"app": {"pid": 0, "start_ticks": 81}},
        {"app": {"pid": 2**31, "start_ticks": 81}},
        {"app": {"pid": 31001, "start_ticks": -1}},
        {"app": {"pid": 31001, "start_ticks": 2**63}},
        {"app": {"pid": 31001, "start_ticks": False}},
        {"app": {"pid": 31001, "start_ticks": 1.0}},
        {"app": {"pid": 31001}},
        {"app": {"pid": 31001, "start_ticks": 81, "extra": None}},
    ):
        cases.append(json.dumps(valid | change).encode())
    payload = encode_authority_owner(owner())
    cases.extend((payload + b" ", payload + b"\n", payload + b"{}", b"[]", b"null", b"\xff"))
    cases.append(payload[:-1] + b',"acquired_at":2}')
    cases.append(payload.replace(b'"pid":31001', b'"pid":31001,"pid":31001'))
    return cases


async def test_authority_owner_codec_rejection_matrix(two_registry_barrier):
    c = two_registry_barrier
    payload = encode_authority_owner(c.owner_a)
    assert set(json.loads(payload)) == {"schema", "instance_uuid", "app", "boot_id", "acquired_at"}
    assert decode_authority_owner(payload) == c.owner_a
    assert type(decode_authority_owner(payload).acquired_at) is float
    for malformed in malformed_owner_documents():
        with pytest.raises(RecordError):
            decode_authority_owner(malformed)
        c.owner_path.write_bytes(malformed)
        # Exercise both authority entrypoints and both registry directions.
        result = await c.authority_b.acquire(c.owner_b)
        assert isinstance(result, Err)
        assert c.owner_path.read_bytes() == malformed
        for operation in (c.registry_b.ensure, c.registry_b.shutdown):
            result = await operation()
            assert isinstance(result, Err)
            assert c.owner_path.read_bytes() == malformed
    assert all(not p.spawned and not p.terminated for p in c.processes)


@pytest.mark.parametrize("evidence", [
    "unreadable", "same-owner-unreadable", "reused", "boot-unreadable", "boot-mismatch",
])
async def test_uncertain_owner_evidence_never_authorizes_replacement(two_registry_barrier, evidence):
    c = two_registry_barrier
    if evidence in {"unreadable", "same-owner-unreadable"}:
        c.unreadable.add(APP_A.pid)
    elif evidence == "reused":
        c.live[APP_A.pid] = replace(APP_A, start_ticks=999)
    elif evidence == "boot-unreadable":
        c.authority_b._boot_id = lambda: None
    else:
        c.authority_b._boot_id = lambda: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    before = c.owner_path.read_bytes()
    registry = c.registry_a if evidence == "same-owner-unreadable" else c.registry_b
    result = await registry.ensure()
    assert isinstance(result, Err) and result.error.code is ErrorCode.ownership_mismatch
    assert c.owner_path.read_bytes() == before
    assert all(not p.spawned and not p.terminated for p in c.processes)


async def test_absent_owner_is_retained_until_locked_reconciliation(two_registry_barrier):
    c = two_registry_barrier
    c.live.pop(APP_A.pid)
    before = c.owner_path.read_bytes()
    lease = await c.authority_b.acquire(c.owner_b)
    assert isinstance(lease, Ok)
    assert lease.value.owner == c.owner_a
    assert c.owner_path.read_bytes() == before
    # Acquisition alone never publishes or erases the prior endpoint obligation.
    lease.value.unlock()
    assert c.owner_path.read_bytes() == before


async def test_nested_acquire_and_cancelled_wait_preserve_owner(two_registry_barrier):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    nested = await c.authority_a.acquire(c.owner_a)
    assert isinstance(nested, Err) and nested.error.code is ErrorCode.ownership_mismatch
    before = c.owner_path.read_bytes()
    pending = asyncio.create_task(c.authority_b.acquire(c.owner_b))
    await asyncio.sleep(0)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    first.value.unlock()
    assert c.owner_path.read_bytes() == before
    again = await c.authority_a.acquire(c.owner_a)
    assert isinstance(again, Ok)
    again.value.unlock()


async def test_authority_refuses_symlink_lock_and_owner(two_registry_barrier):
    c = two_registry_barrier
    marker = c.root / "marker"
    marker.write_text("unchanged")
    lock = c.root / "daemon/authority.lock"
    lock.unlink()
    lock.symlink_to(marker)
    refused = await c.authority_b.acquire(c.owner_b)
    assert isinstance(refused, Err)
    assert marker.read_text() == "unchanged"
    lock.unlink()
    c.owner_path.unlink()
    c.owner_path.symlink_to(marker)
    refused = await c.authority_b.acquire(c.owner_b)
    assert isinstance(refused, Err)
    assert marker.read_text() == "unchanged"


async def test_owner_publication_failure_preserves_obligation_and_unlocks(two_registry_barrier):
    c = two_registry_barrier

    class FailDirectorySync(PosixFilesystem):
        def fsync_directory(self, descriptor):
            raise OSError("injected directory fsync refusal")

    authority = authority_class()(
        AtomicFiles(c.root, FailDirectorySync()), inspect_process=c.inspect, boot_id=lambda: BOOT,
    )
    first = await authority.acquire(c.owner_a)
    assert isinstance(first, Ok)
    try:
        refused = await authority.publish_owner(c.owner_a)
        assert isinstance(refused, Err) and refused.error.code is ErrorCode.persistence_failed
    finally:
        first.value.unlock()
    assert decode_authority_owner(c.owner_path.read_bytes()) == c.owner_a
    next_lease = await c.authority_a.acquire(c.owner_a)
    assert isinstance(next_lease, Ok)
    next_lease.value.unlock()


async def test_unresolved_owner_blocks_production_launch(two_registry_barrier, tmp_path, monkeypatch):
    # Reuse the real production composition probe and its offline tripwires;
    # this calls build_runtime with the final process override, never a rig.
    import runpy
    from pathlib import Path

    proof = runpy.run_path(str(Path(__file__).parents[1] / "acceptance/test_no_production_spawn.py"))
    c = two_registry_barrier
    refused = await c.registry_b.ensure()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.ownership_mismatch
    assert all(not p.spawned and not p.terminated for p in c.processes)
    tripwire = proof["offline_boundaries"].__wrapped__(monkeypatch)
    (tmp_path / "public").mkdir()
    await proof["test_phase_guard_blocks_eligible_production_launch"](
        tmp_path / "public", monkeypatch, tripwire,
    )
