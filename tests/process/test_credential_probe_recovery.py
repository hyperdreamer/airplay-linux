"""A-3/A-4/A-5: public fresh-instance recovery at durable lifecycle boundaries."""
from __future__ import annotations

import asyncio
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.storage.atomic import AtomicFiles, StorageError

from test_credential_probe import (
    fake_probe_runtime_directories,
    namespace_probe_witness,
    private_socket_root,
    probe_module,
    recording_probe_processes,
)


@pytest.mark.parametrize("failure", ["unshare_exit", "exec_missing"])
async def test_pre_child_failure_then_fresh_instance_success(recording_probe_processes, failure):
    """Removing no-receipt dead ends must allow an actual two-leg witness."""
    c = recording_probe_processes
    roots = []

    class PreChildFailure:
        async def run(self, spec, timeout):
            roots.append(Path(spec.argv[spec.argv.index("--socket") + 1]).parent.parent)
            if failure == "unshare_exit":
                return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))
            return Err(error=diagnostic(ErrorCode.dependency_missing))

    state = c.runtime_config.state_root
    for _ in range(3):
        first = await c.module.CredentialServiceProbe(
            process=PreChildFailure(), record_root=state, unshare="/fake/unshare",
        ).check()
        assert isinstance(first, Err)
    assert not c.inner_launches
    assert len(roots) == 3
    # Count record UUIDs before either fixture can reclaim temporary trees.
    assert list((state / "daemon/probe").iterdir()) == [], "A-7: empty probe records accumulated"
    second = await c.module.CredentialServiceProbe(
        process=c.outer, record_root=state, unshare="/fake/unshare",
    ).check()
    assert isinstance(second, Ok) and second.value.proven, "A-3: pre-child failure blocked healthy retry"
    assert len(c.outer_unshare) == 1 and len(c.inner_launches) == 2
    assert all(not root.parent.exists() for root in roots)
    assert not list(state.rglob("runtime-cleanup.json"))


@pytest.mark.parametrize("artifact", [
    None, "runtime-lifecycle.json", "runtime-cleanup.json", "receipts", "cleanup-intents",
    ".runtime-lifecycle.json.tmp", "backend-dispatch.json", "verdict.json",
])
async def test_recovery_retires_only_completely_empty_probe_records(tmp_path, monkeypatch, artifact):
    p = probe_module()
    state = tmp_path / "state"
    root = state / "daemon/probe" / str(uuid4())
    root.mkdir(mode=0o700, parents=True)
    if artifact in {"receipts", "cleanup-intents"}:
        (root / artifact).mkdir(mode=0o700)
    elif artifact is not None:
        (root / artifact).write_bytes(b"retained evidence")
    original_rmdir, original_fsync = os.rmdir, os.fsync
    retired, synced = [], []

    def track_removal(path, *args, **kwargs):
        original_rmdir(path, *args, **kwargs)
        if path == root.name:
            retired.append(path)

    def track_sync(descriptor):
        if retired and os.readlink(f"/proc/self/fd/{descriptor}") == str(root.parent):
            synced.append(descriptor)
        return original_fsync(descriptor)

    class PreChildExit:
        async def run(self, spec, timeout):
            return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))

    monkeypatch.setattr(os, "rmdir", track_removal)
    monkeypatch.setattr(os, "fsync", track_sync)
    result = await p.CredentialServiceProbe(process=PreChildExit(), record_root=state, unshare="unshare").check()
    assert isinstance(result, Err)
    if artifact is None:
        assert not root.exists(), "A-7: recovery stranded an empty UUID directory"
        assert retired == [root.name] and synced, "record retirement must fsync its parent after rmdir"
    else:
        assert root.is_dir() and (root / artifact).exists()
        if (root / artifact).is_file():
            assert (root / artifact).read_bytes() == b"retained evidence"
        assert not retired


async def test_recovery_cannot_retire_a_probe_between_mkdir_and_lifecycle(tmp_path, monkeypatch):
    p = probe_module()
    state = tmp_path / "state"
    original_create = p.CredentialServiceProbe._create_root
    observed = []

    class PreChildExit:
        async def run(self, spec, timeout):
            return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))

    def check_competitor():
        return asyncio.run(p.CredentialServiceProbe(
            process=PreChildExit(), record_root=state, unshare="unshare",
        ).check())

    with ThreadPoolExecutor(max_workers=1) as executor:
        def pause_after_mkdir(self, probe_uuid):
            root = original_create(self, probe_uuid)
            if not observed:
                observed.append(root)
                assert list(root.iterdir()) == []
                competitor = executor.submit(check_competitor).result(timeout=5)
                assert root.is_dir() and list(root.iterdir()) == []
                assert isinstance(competitor, Err) and competitor.error.code is ErrorCode.ownership_mismatch
            return root

        monkeypatch.setattr(p.CredentialServiceProbe, "_create_root", pause_after_mkdir)
        result = await p.CredentialServiceProbe(
            process=PreChildExit(), record_root=state, unshare="unshare",
        ).check()
    assert isinstance(result, Err)
    assert len(observed) == 1
    assert list((state / "daemon/probe").iterdir()) == []


async def test_concurrent_recovery_preserves_live_binding_and_lifecycle(tmp_path):
    p = probe_module()
    state = tmp_path / "state"
    entered, finish = asyncio.Event(), asyncio.Event()
    records = []

    class LiveOuter:
        async def run(self, spec, timeout):
            root = Path(spec.argv[spec.argv.index("--record-root") + 1])
            records.append(root)
            entered.set()
            await finish.wait()
            return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))

    class NoDispatch:
        async def run(self, spec, timeout):
            pytest.fail("concurrent recovery dispatched over a live probe")

    active = asyncio.create_task(p.CredentialServiceProbe(
        process=LiveOuter(), record_root=state, unshare="unshare",
    ).check())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        root, = records
        evidence = {name: (root / name).read_bytes()
                    for name in ("runtime-cleanup.json", "runtime-lifecycle.json")}
        assert json.loads(evidence["runtime-lifecycle.json"])["dispatch"] == "dispatching"
        competitor = await p.CredentialServiceProbe(
            process=NoDispatch(), record_root=state, unshare="unshare",
        ).check()
        assert isinstance(competitor, Err) and competitor.error.code is ErrorCode.ownership_mismatch
        assert root.is_dir()
        assert all((root / name).read_bytes() == payload for name, payload in evidence.items())
    finally:
        finish.set()
        await active


class RecoveryCrash(RuntimeError):
    """Model process loss after a syscall; no in-memory continuation survives."""


@pytest.mark.parametrize("boundary", [
    "positive", "negative", "root", "parent", "obligation",
    "fsync_positive", "fsync_negative", "fsync_root", "fsync_parent",
    "journal_1", "journal_2", "journal_3", "journal_4",
    "after_obligation_delete", "after_journal_delete",
])
@pytest.mark.parametrize("interruption", ["io_error", "crash"])
async def test_interrupted_runtime_removal_then_fresh_instance_success(
    recording_probe_processes, monkeypatch, boundary, interruption,
):
    """Each destructive boundary must resume without rewriting terminal evidence."""
    c = recording_probe_processes
    c.inner.terminate_error = ErrorCode.cleanup_timeout
    runtime = await c.build_complete_production_and_probe()
    try:
        launch, = c.inner_launches
        record, sockets = c.config.record_root, c.probe_runtime_root
        obligation = record / "runtime-cleanup.json"
        evidence = {path: path.read_bytes() for directory in ("receipts", "cleanup-intents")
                    for path in (record / directory).glob("*.json")}
        assert len(evidence) == 2
        c.inner.exit(launch.identity, -9)
        c.facts.supervisor_present = False
        c.facts.rows[str(sockets / "positive/daemon.sock")] = ()
        c.inner.terminate_error = None
        monkeypatch.setattr(c.module, "EndpointObservations", lambda: c.facts)
        original_rmdir, original_delete = os.rmdir, AtomicFiles.delete
        original_fsync, original_write = os.fsync, AtomicFiles.write
        tripped, removed = [], []
        removal_target = boundary.removeprefix("fsync_")
        target = {"positive": "positive", "negative": "negative", "root": sockets.name,
                  "parent": sockets.parent.name}.get(removal_target)

        def interrupt():
            tripped.append(boundary)
            if interruption == "crash":
                raise RecoveryCrash(boundary)
            raise OSError(errno.EIO, "injected recovery interruption")

        def interrupted_rmdir(path, *args, **kwargs):
            original_rmdir(path, *args, **kwargs)
            if path == target:
                removed.append(path)
                if not boundary.startswith("fsync_") and not tripped:
                    interrupt()

        def interrupted_fsync(descriptor):
            container = {"positive": sockets, "negative": sockets, "root": sockets.parent,
                         "parent": Path("/tmp")}.get(removal_target)
            if (boundary.startswith("fsync_") and removed and not tripped
                    and os.readlink(f"/proc/self/fd/{descriptor}") == str(container)):
                interrupt()
            return original_fsync(descriptor)

        def interrupted_write(self, relative, payload):
            if self.root == record and relative == "runtime-lifecycle.json" and boundary.startswith("journal_"):
                progress = json.loads(payload)["removal"]
                if progress is not None and progress["step"] == int(boundary[-1]) and not tripped:
                    interrupt()
            return original_write(self, relative, payload)

        def interrupted_delete(self, relative):
            if self.root == record and not tripped:
                if boundary == "obligation" and relative == "runtime-cleanup.json":
                    interrupt()
                if ((boundary == "after_obligation_delete" and relative == "runtime-cleanup.json")
                        or (boundary == "after_journal_delete" and relative == "runtime-lifecycle.json")):
                    original_delete(self, relative)
                    interrupt()
            return original_delete(self, relative)

        class HealthyAfterRecovery:
            async def run(self, spec, timeout):
                assert not sockets.parent.exists()
                assert not obligation.exists()
                assert all(path.read_bytes() == data for path, data in evidence.items())
                return await c.outer.run(spec, timeout)

        monkeypatch.setattr(os, "rmdir", interrupted_rmdir)
        monkeypatch.setattr(AtomicFiles, "delete", interrupted_delete)
        monkeypatch.setattr(AtomicFiles, "write", interrupted_write)
        monkeypatch.setattr(os, "fsync", interrupted_fsync)
        probe = c.module.CredentialServiceProbe(
            process=HealthyAfterRecovery(), record_root=c.runtime_config.state_root, unshare="/fake/unshare",
        )
        if interruption == "crash":
            with pytest.raises(RecoveryCrash):
                await probe.check()
        else:
            failed = await probe.check()
            assert isinstance(failed, Err) and failed.error.code is ErrorCode.persistence_failed
        assert tripped == [boundary]
        assert obligation.is_file() is (not boundary.startswith("after_"))
        assert all(path.read_bytes() == data for path, data in evidence.items())
        assert len(c.outer_unshare) == 1
        monkeypatch.setattr(os, "rmdir", original_rmdir)
        monkeypatch.setattr(AtomicFiles, "delete", original_delete)
        monkeypatch.setattr(AtomicFiles, "write", original_write)
        monkeypatch.setattr(os, "fsync", original_fsync)
        result = await c.module.CredentialServiceProbe(
            process=HealthyAfterRecovery(), record_root=c.runtime_config.state_root, unshare="/fake/unshare",
        ).check()
        assert isinstance(result, Ok) and result.value.proven, "A-4: partial removal blocked healthy retry"
        assert len(c.outer_unshare) == 2 and len(c.inner_launches) == 3
        assert all(path.read_bytes() == data for path, data in evidence.items())
        assert not sockets.parent.exists() and not obligation.exists()
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("record_name,publication", [
    ("runtime-cleanup.json", "before_rename"),
    ("runtime-cleanup.json", "visible_but_unconfirmed"),
    ("runtime-lifecycle.json", "before_rename"),
    ("runtime-lifecycle.json", "visible_but_unconfirmed"),
    ("/tmp", "directory_fsync"),
])
async def test_repeated_binding_publication_failures_do_not_accumulate_roots(
    tmp_path, monkeypatch, publication, record_name,
):
    """Count retained allocations before the autouse fixture can clean anything."""
    p = probe_module()
    parents, dispatches = [], []
    original_mkdtemp, original_write, original_fsync = p.mkdtemp, AtomicFiles.write, os.fsync

    def tracked(**kwargs):
        parent = Path(original_mkdtemp(**kwargs))
        parents.append(parent)
        return str(parent)

    def failed_publication(self, relative, payload):
        if relative == record_name:
            if publication == "visible_but_unconfirmed":
                original_write(self, relative, payload)
            raise StorageError("write")
        return original_write(self, relative, payload)

    def failed_parent_sync(descriptor):
        if record_name == "/tmp" and os.readlink(f"/proc/self/fd/{descriptor}") == "/tmp":
            raise OSError(errno.EIO, "runtime parent publication failed")
        return original_fsync(descriptor)

    class NoDispatch:
        async def run(self, spec, timeout):
            dispatches.append(spec)
            pytest.fail("dispatch followed failed obligation publication")

    monkeypatch.setattr(p, "mkdtemp", tracked)
    monkeypatch.setattr(AtomicFiles, "write", failed_publication)
    monkeypatch.setattr(os, "fsync", failed_parent_sync)
    state = tmp_path / "state"
    for attempt in range(3):
        result = await p.CredentialServiceProbe(process=NoDispatch(), record_root=state, unshare="unshare").check()
        assert isinstance(result, Err) and result.error.code is ErrorCode.persistence_failed
        assert len(parents) == attempt + 1
    # All three failed allocations are counted before ANY fixture teardown.
    assert len(parents) == 3 and not dispatches
    assert sum(parent.exists() for parent in parents) == 0, "A-5: undispatched runtime parents accumulated"
    assert not list(state.rglob("runtime-cleanup.json"))
    assert not list(state.rglob("runtime-lifecycle.json"))


@pytest.mark.parametrize("completion", ["uncertain_error", "gate_then_exit", "gate_then_exec_refusal"])
async def test_missing_receipts_never_prove_absence_without_dispatch_evidence(tmp_path, completion):
    p = probe_module()
    calls, roots = [], []

    class UncertainDispatch:
        async def run(self, spec, timeout):
            calls.append(spec)
            record = Path(spec.argv[spec.argv.index("--record-root") + 1])
            roots.append(Path(spec.argv[spec.argv.index("--socket") + 1]).parent.parent)
            lifecycle = json.loads((record / "runtime-lifecycle.json").read_bytes())
            assert lifecycle["dispatch"] == "dispatching" and lifecycle["removal"] is None
            if completion.startswith("gate_"):
                AtomicFiles(record).write("backend-dispatch.json", b"durable child dispatch gate")
            if completion == "gate_then_exit":
                return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))
            return Err(error=diagnostic(ErrorCode.dependency_missing if completion == "gate_then_exec_refusal"
                                        else ErrorCode.daemon_failed))

    state = tmp_path / "state"
    for _ in range(3):
        result = await p.CredentialServiceProbe(process=UncertainDispatch(), record_root=state, unshare="unshare").check()
        assert isinstance(result, Err)
    assert len(calls) == 1
    assert roots[0].is_dir()
    assert len(list(state.rglob("runtime-cleanup.json"))) == 1
    assert not list(state.rglob("receipts"))


@pytest.mark.parametrize("phase", ["allocated", "dispatching", "no_child", "complete"])
@pytest.mark.parametrize("publication", ["absent", "visible"])
async def test_single_lifecycle_publication_failure_allows_fresh_success(
    recording_probe_processes, monkeypatch, phase, publication,
):
    c = recording_probe_processes
    original_write = AtomicFiles.write
    tripped = []

    def fail_once(self, relative, payload):
        if relative == "runtime-lifecycle.json":
            document = json.loads(payload)
            if document["dispatch"] == phase and document["removal"] is None and not tripped:
                tripped.append(self.root)
                if publication == "visible":
                    original_write(self, relative, payload)
                raise StorageError("fsync")
        return original_write(self, relative, payload)

    class PreChildExit:
        async def run(self, spec, timeout):
            return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))

    monkeypatch.setattr(AtomicFiles, "write", fail_once)
    first = await c.module.CredentialServiceProbe(
        process=PreChildExit() if phase == "no_child" else c.outer,
        record_root=c.runtime_config.state_root, unshare="/fake/unshare",
    ).check()
    assert isinstance(first, Err) and first.error.code is ErrorCode.persistence_failed
    assert len(tripped) == 1
    record, = tripped
    evidence = {path: path.read_bytes() for directory in ("receipts", "cleanup-intents")
                for path in (record / directory).glob("*.json")}
    monkeypatch.setattr(AtomicFiles, "write", original_write)
    second = await c.module.CredentialServiceProbe(
        process=c.outer, record_root=c.runtime_config.state_root, unshare="/fake/unshare",
    ).check()
    assert isinstance(second, Ok) and second.value.proven
    assert all(path.read_bytes() == data for path, data in evidence.items())
    assert not list(c.runtime_config.state_root.rglob("runtime-cleanup.json"))
    assert not list(c.runtime_config.state_root.rglob("runtime-lifecycle.json"))


async def test_failed_binding_and_failed_empty_cleanup_leave_resumable_allocation(recording_probe_processes, monkeypatch):
    c = recording_probe_processes
    original_write, original_rmdir = AtomicFiles.write, os.rmdir
    records, interruptions = [], []

    def refuse_binding(self, relative, payload):
        if relative == "runtime-cleanup.json":
            records.append(self.root)
            raise StorageError("write")
        return original_write(self, relative, payload)

    def refuse_first_removal(path, *args, **kwargs):
        if records and path == records[0].name and not interruptions:
            interruptions.append(path)
            raise OSError(errno.EIO, "undispatched cleanup interruption")
        return original_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(AtomicFiles, "write", refuse_binding)
    monkeypatch.setattr(os, "rmdir", refuse_first_removal)
    first = await c.module.CredentialServiceProbe(
        process=c.outer, record_root=c.runtime_config.state_root, unshare="/fake/unshare",
    ).check()
    assert isinstance(first, Err) and first.error.code is ErrorCode.persistence_failed
    assert len(records) == len(interruptions) == 1
    record, = records
    lifecycle = json.loads((record / "runtime-lifecycle.json").read_bytes())
    assert lifecycle["dispatch"] == "allocated" and lifecycle["removal"]["step"] == 2
    sockets = Path(lifecycle["binding"]["runtime_root"])
    assert sockets.is_dir() and not (record / "runtime-cleanup.json").exists()
    assert not c.outer_unshare and not c.inner_launches
    monkeypatch.setattr(AtomicFiles, "write", original_write)
    monkeypatch.setattr(os, "rmdir", original_rmdir)
    second = await c.module.CredentialServiceProbe(
        process=c.outer, record_root=c.runtime_config.state_root, unshare="/fake/unshare",
    ).check()
    assert isinstance(second, Ok) and second.value.proven
    assert not sockets.parent.exists()
    assert not (record / "runtime-lifecycle.json").exists()
