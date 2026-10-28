"""KDE virtual-display contracts over private fake helper and KScreen commands.

The provider, creator, recovery, remover and observer are the production
adapters.  The two external commands they spawn are private fakes:

* ``krfb-virtualmonitor`` -- prints a documented ``--resolution`` surface, writes
  its simulated compositor output (and its own PID/owner association) into files
  inside ``tmp_path``, removes exactly its own output on ``SIGTERM``, and can be
  asked to crash before or after that persistence;
* ``kscreen-doctor`` -- reads/applies settings only in the same private JSON file.

Association between a helper process and a compositor output is deliberately
*not* claimed by KDE: the tests supply an explicit verifier that re-proves process
ownership through the real runner and matches the helper's own persisted
association record.  The contracts pinned here:

* one creation publishes exactly one owned, verified output and removal restores
  the baseline exactly;
* a crash before persistence invents no output, and a crash after persistence
  never publishes a handle (the orphan is retained as cleanup intent);
* ambiguous new outputs are refused, and an active creation blocks a second one;
* a remove-then-create cycle yields a distinct output.

Manual commands: ``pytest -q tests/adapter_contracts/test_kde_contract.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kde import KdeVirtualDisplayProvider
from airplay_linux.display.kscreen import KScreenObserver
from airplay_linux.display.locking import CreationLock
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.display.removal import DisplayRemover
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import OperationContext, OperationId
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import (
    IdentityState,
    OwnedProcessScan,
    read_identity_evidence,
)
from airplay_linux.storage import AtomicFiles, JsonOwnershipStore

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402
import fake_kscreen  # noqa: E402
import fake_virtualmonitor  # noqa: E402

SIZE = Size(width=1920, height=1080)


class _Clock:
    now = staticmethod(time.monotonic)


class _Scheduler:
    def call_at(self, when, callback):
        return asyncio.get_running_loop().call_at(when, callback)


class _Ids:
    new = staticmethod(uuid4)


@dataclass
class World:
    runner: LinuxProcessRunner
    provider: KdeVirtualDisplayProvider
    ownership: JsonOwnershipStore
    state: Path
    pids: Path
    runtime: Path

    def layout(self):
        return fake_kscreen.read_layout(self.state)

    def helper_records(self):
        return json.loads(self.pids.read_text()) if self.pids.exists() else []

    async def records(self):
        found = await self.ownership.all()
        assert isinstance(found, Ok)
        return found.value


def _context() -> OperationContext:
    return OperationContext(
        operation_id=OperationId(uuid4()),
        session_generation=1,
        endpoint_generation=1,
    )


def _confined_owner_scan(owner_uuid, runtime_directory, proc_root=Path("/proc")):
    """The real helper owner scan, confined to this user's inspectable processes.

    A global ``/proc`` scan is *incomplete* on a real desktop because unrelated
    non-dumpable processes hide their environment (see the transport contract's
    incomplete-scan test).  The private world restricts the candidate population;
    ownership is still proved against real procfs by the production scan code
    path (``_owner_evidence_state`` -> ``LinuxProcessRunner.owned``).
    """
    matches = []
    for entry in os.listdir(proc_root):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if (proc_root / entry).stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        evidence = read_identity_evidence(pid, proc_root)
        if evidence.state is not IdentityState.present or evidence.identity is None:
            continue
        from airplay_linux.process.procfs import owner_evidence

        if owner_evidence(evidence.identity, owner_uuid, runtime_directory, proc_root):
            matches.append(evidence.identity)
    return OwnedProcessScan(
        matches=tuple(sorted(matches, key=lambda item: (item.pid, item.start_ticks))),
        complete=True,
    )


async def kde_world(tmp_path, monkeypatch, *, helper_mode="normal") -> World:
    state = tmp_path / "fake-kscreen.json"
    pids = tmp_path / "fake-helper-pids.json"
    fake_kscreen.write_layout(state, fake_kscreen.baseline_layout())
    installed = fake_doubletake.install(
        tmp_path / "bin",
        env={
            "kscreen-doctor": {"AIRPLAY_FAKE_KSCREEN_STATE": str(state)},
            "krfb-virtualmonitor": fake_virtualmonitor.helper_env(
                state, pids, mode=helper_mode,
            ),
        },
    )
    fake_doubletake.private_path(monkeypatch, tmp_path / "bin")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    # ``VirtualHelper.owned_scan`` reads procfs directly, with no constructor seam.
    monkeypatch.setattr(
        "airplay_linux.display.helper.owned_processes", _confined_owner_scan,
    )

    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    ownership = JsonOwnershipStore(AtomicFiles(state_root))
    runner = LinuxProcessRunner()
    clock, scheduler, ids = _Clock(), _Scheduler(), _Ids()
    observer = KScreenObserver(runner, clock, scheduler, poll_interval=0.02)
    helper = VirtualHelper(runner, clock)
    lock = CreationLock(runtime)

    async def transport_released(record, context):
        return Ok(value=None)

    async def verifier(owner_uuid, runtime_directory, identity, candidate):
        """The only association authority here: helper-persisted, re-proved."""
        records = json.loads(pids.read_text()) if pids.exists() else []
        match = [
            record for record in records
            if record["pid"] == identity.pid and record["owner_uuid"] == str(owner_uuid)
        ]
        if len(match) != 1:
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        current = runner.identity(identity.pid)
        if current != identity or not runner.owned(identity, owner_uuid, runtime_directory):
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        if (match[0]["connector"], match[0]["output_id"]) != (
            candidate.connector, candidate.output_id,
        ):
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        return Ok(value=None)

    remover = DisplayRemover(
        helper, observer, ownership, lock, clock,
        process=runner, output_verifier=verifier,
        transport_released=transport_released,
    )
    recovery = DisplayRecovery(
        helper, observer, ownership, lock, clock, remover,
        output_verifier=verifier,
    )
    creator = DisplayCreator(
        helper, observer, ownership, lock,
        runtime_directory=str(runtime), app_instance_uuid=uuid4(),
        clock=clock, scheduler=scheduler, id_factory=ids,
        output_verifier=verifier,
    )
    provider = KdeVirtualDisplayProvider(creator, recovery, remover, observer, helper)
    return World(runner, provider, ownership, state, pids, runtime)


async def test_create_publishes_one_owned_output_and_remove_restores_baseline(
    tmp_path, monkeypatch,
):
    world = await kde_world(tmp_path, monkeypatch)
    baseline = world.layout()
    receiver = ReceiverId("airplay-device:a")
    try:
        created = await world.provider.create(
            receiver, "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(created, Ok), created
        handle = created.value

        current = world.layout()
        added = [entry for entry in current["outputs"] if entry not in baseline["outputs"]]
        assert len(added) == 1
        assert added[0]["name"].lower().startswith("virtual")
        assert added[0]["enabled"] is True
        assert added[0]["size"] == {"width": 1920, "height": 1080}
        assert added[0]["pos"] == {"x": 0, "y": 0}
        assert handle.source.connector == added[0]["name"]
        assert handle.source.kscreen_output_id == str(added[0]["id"])
        # The real process really carries the exact ownership environment.
        assert world.runner.owned(
            handle.helper, handle.owner_uuid, str(world.runtime),
        ) is True
        records = await world.records()
        assert [(record.owner_uuid, record.stage) for record in records] == [
            (handle.owner_uuid, "published"),
        ]

        removed = await world.provider.remove(handle.owner_uuid, _context())
        assert isinstance(removed, Ok), removed
        assert fake_kscreen.read_layout(world.state) == baseline
        assert world.helper_records() == []
        assert await world.records() == ()
    finally:
        fake_doubletake.reap_private(str(tmp_path))


async def test_a_crash_before_persistence_publishes_nothing(tmp_path, monkeypatch):
    world = await kde_world(tmp_path, monkeypatch, helper_mode="crash-before-persist")
    baseline = world.layout()
    try:
        created = await world.provider.create(
            ReceiverId("airplay-device:a"), "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(created, Err)
        assert created.error.code in {
            ErrorCode.helper_failed,
            ErrorCode.ownership_mismatch,
            ErrorCode.cleanup_timeout,
        }
        # No compositor output was invented and no helper claimed a PID.
        assert world.layout() == baseline
        assert world.helper_records() == []
        assert not [record for record in await world.records() if record.stage == "published"]
    finally:
        fake_doubletake.reap_private(str(tmp_path))


async def test_a_crash_after_persistence_never_publishes_the_orphan(
    tmp_path, monkeypatch,
):
    world = await kde_world(tmp_path, monkeypatch, helper_mode="crash-after-persist")
    baseline = world.layout()
    try:
        created = await world.provider.create(
            ReceiverId("airplay-device:a"), "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(created, Err), created
        # The output the dead helper made cannot be cleaned by a live helper, so
        # the transaction reports cleanup uncertainty and retains intent instead
        # of claiming success (or silently deleting another output).
        assert created.error.code is ErrorCode.cleanup_timeout
        orphans = [
            entry for entry in world.layout()["outputs"]
            if entry not in baseline["outputs"]
        ]
        assert len(orphans) == 1
        assert world.helper_records()[0]["pid"] > 0
        retained = [
            record for record in await world.records()
            if record.desired_state == "removing" and record.stage == "removing"
        ]
        assert len(retained) == 1
        assert not [record for record in await world.records() if record.stage == "published"]
    finally:
        fake_doubletake.reap_private(str(tmp_path))


async def test_an_ambiguous_new_output_is_never_adopted(tmp_path, monkeypatch):
    world = await kde_world(tmp_path, monkeypatch, helper_mode="duplicate-output")
    baseline = world.layout()
    try:
        created = await world.provider.create(
            ReceiverId("airplay-device:a"), "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(created, Err)
        # Selection never guesses between two new candidates: it reports doubt
        # (the observation deadline) instead of adopting one of them.
        assert created.error.code in {
            ErrorCode.configuration_failed, ErrorCode.cleanup_timeout,
        }
        assert not [record for record in await world.records() if record.stage == "published"]
        # Nothing was adopted; the exactly-owned helper was stopped and its two
        # outputs are gone again rather than one being claimed.
        assert world.layout() == baseline
        assert world.helper_records() == []
    finally:
        fake_doubletake.reap_private(str(tmp_path))


async def test_an_active_creation_blocks_a_second_one(tmp_path, monkeypatch):
    world = await kde_world(tmp_path, monkeypatch)
    try:
        first = await world.provider.create(
            ReceiverId("airplay-device:a"), "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(first, Ok), first
        second = await world.provider.create(
            ReceiverId("airplay-device:b"), "endpoint-b-v1", SIZE, _context(),
        )
        assert isinstance(second, Err)
        assert second.error.code is ErrorCode.unsupported_operation
        assert len(world.helper_records()) == 1
        assert isinstance(
            await world.provider.remove(first.value.owner_uuid, _context()), Ok,
        )
    finally:
        fake_doubletake.reap_private(str(tmp_path))


async def test_a_remove_then_create_cycle_yields_a_distinct_output(tmp_path, monkeypatch):
    world = await kde_world(tmp_path, monkeypatch)
    baseline = world.layout()
    try:
        first = await world.provider.create(
            ReceiverId("airplay-device:a"), "endpoint-a-v1", SIZE, _context(),
        )
        assert isinstance(first, Ok), first
        assert isinstance(
            await world.provider.remove(first.value.owner_uuid, _context()), Ok,
        )
        assert fake_kscreen.read_layout(world.state) == baseline

        second = await world.provider.create(
            ReceiverId("airplay-device:b"), "endpoint-b-v1", SIZE, _context(),
        )
        assert isinstance(second, Ok), second
        assert second.value.owner_uuid != first.value.owner_uuid
        assert isinstance(
            await world.provider.remove(second.value.owner_uuid, _context()), Ok,
        )
        assert fake_kscreen.read_layout(world.state) == baseline
    finally:
        fake_doubletake.reap_private(str(tmp_path))
