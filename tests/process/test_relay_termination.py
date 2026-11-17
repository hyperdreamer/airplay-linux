"""Historical exact-instance termination evidence, without control effects.

The query proves what a previous launch already established: one original
``DaemonRecord``, one matching terminal ``LaunchReceipt``, the exact persisted
cleanup completion and fresh positive exit evidence for the recorded process.
No runtime directory, socket, live daemon or controlled process is required.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.backend.control import ApplicationContractProfile
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonCleanupIntent, DaemonRecord, LaunchReceipt
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, OwnedProcessScan
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage.repositories import JsonDaemonCleanupIntentStore, JsonLaunchReceiptStore
from airplay_linux.testing.adapters import FakeProcess, MemoryDaemonStore, SequenceIds

APP = ProcessIdentity(pid=100, start_ticks=10)
SUPERVISOR = ProcessIdentity(pid=200, start_ticks=20)
DAEMON = ProcessIdentity(pid=300, start_ticks=30)
REPLACEMENT_DAEMON = ProcessIdentity(pid=301, start_ticks=31)
APP_INSTANCE = InstanceId(UUID(int=901))
ORIGINAL = InstanceId(UUID(int=902))
REPLACEMENT = InstanceId(UUID(int=903))
LAUNCH = UUID(int=904)
NEWER_LAUNCH = UUID(int=905)
BOOT = str(UUID(int=906))
INTENT_AT = 1_800_000_000.0
COMPLETED_AT = 1_800_000_001.0
FEATURES = ApplicationContractProfile(socket_path='/unused').features


class Clock:
    """Injected monotonic clock; keeps returning its final value."""

    def __init__(self, values: tuple[float, ...] = (10.0, 11.0)) -> None:
        self.values = list(values)
        self.calls = 0

    def now(self) -> float:
        self.calls += 1
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class RelayCase:
    """Test-local repository/process harness; no runtime or socket is created."""

    def __init__(self, root: Path, clock_values: tuple[float, ...] = (10.0, 11.0)) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.runtime = self.root / 'runtime'
        self.state_root = self.root / 'state'
        files = AtomicFiles(self.state_root)
        self.receipts = JsonLaunchReceiptStore(files)
        self.intents = JsonDaemonCleanupIntentStore(files)
        self.process = FakeProcess()
        self.rows: dict[int, IdentityEvidence] = {}
        self.opened: list[tuple[int, int]] = []
        self.write_fds: list[int] = []
        self.scans: list[tuple[UUID, str]] = []
        self.pidfd_exits = False
        self.clock = Clock(clock_values)
        self.registry = DaemonRegistry(
            process=self.process, store=MemoryDaemonStore(),
            control_factory=lambda record: None, features=FEATURES,
            runtime_directory=self.runtime, record_root=self.root,
            app=APP, app_instance_uuid=APP_INSTANCE,
            ids=SequenceIds(seed=1000), clock=self.clock,
            inspect_process=self.inspect, scan_owned=self.scan,
            receipts=self.receipts, cleanup_intents=self.intents,
            startup_timeout=0.2,
        )
        # A pipe read end models the pidfd: writing makes exit observable and
        # the closed read end makes every open-descriptor path checkable.
        self.registry._open_pidfd = self.open_pidfd

    # -- injected observations -------------------------------------------------

    def inspect(self, pid: int) -> IdentityEvidence:
        return self.rows.get(pid, IdentityEvidence(pid=pid, state=IdentityState.absent))

    def scan(self, owner: UUID, runtime: str) -> OwnedProcessScan:
        self.scans.append((owner, runtime))
        raise AssertionError('termination proof consulted the owned-process scan')

    def open_pidfd(self, pid: int) -> int:
        read_fd, write_fd = os.pipe()
        self.write_fds.append(write_fd)
        self.opened.append((pid, read_fd))
        if self.pidfd_exits:
            os.write(write_fd, b'exit')
        return read_fd

    @staticmethod
    def present(identity: ProcessIdentity) -> IdentityEvidence:
        return IdentityEvidence(pid=identity.pid, state=IdentityState.present, identity=identity)

    @staticmethod
    def absent(pid: int) -> IdentityEvidence:
        return IdentityEvidence(pid=pid, state=IdentityState.absent)

    # -- durable history -------------------------------------------------------

    def record(self, **overrides: object) -> DaemonRecord:
        values: dict[str, object] = {
            'schema_version': 1, 'instance_uuid': ORIGINAL, 'owner_token': 'owner-token',
            'app': APP, 'supervisor': SUPERVISOR, 'daemon': DAEMON, 'protocol_version': 'v1',
            'socket_path': str(self.runtime / 'daemon.sock'), 'lifecycle': 'stopping',
            'runtime_directory': str(self.runtime),
        }
        values.update(overrides)
        return DaemonRecord(**values)  # type: ignore[arg-type]

    def receipt(self, **overrides: object) -> LaunchReceipt:
        values: dict[str, object] = {
            'schema_version': 1, 'launch_uuid': LAUNCH, 'instance_uuid': APP_INSTANCE,
            'app': APP, 'owner_uid': os.getuid(), 'boot_id': BOOT,
            'runtime_directory': str(self.runtime), 'runtime_device': 1, 'runtime_inode': 2,
            'endpoint_generation': 1, 'socket_name': 'daemon.sock', 'protocol_version': 'v1',
            'state': 'stopped', 'daemon_instance_uuid': ORIGINAL,
            'supervisor': SUPERVISOR, 'daemon': DAEMON,
            'entry_device': None, 'entry_inode': None, 'socket_inode': None,
            'peer_pid': None, 'peer_start_ticks': None, 'peer_uid': None,
            'status_state': None, 'credential_service': 'unknown',
            'failure': None, 'updated_at': INTENT_AT,
        }
        values.update(overrides)
        return LaunchReceipt(**values)  # type: ignore[arg-type]

    def current_receipt(self, **overrides: object) -> LaunchReceipt:
        values: dict[str, object] = {
            'launch_uuid': NEWER_LAUNCH, 'daemon_instance_uuid': REPLACEMENT,
            'daemon': REPLACEMENT_DAEMON, 'endpoint_generation': 2, 'state': 'ready',
            'entry_device': 10, 'entry_inode': 11, 'socket_inode': 12,
            'peer_pid': REPLACEMENT_DAEMON.pid,
            'peer_start_ticks': REPLACEMENT_DAEMON.start_ticks,
            'peer_uid': os.getuid(), 'status_state': 'idle',
            'credential_service': 'keyring_proven',
        }
        values.update(overrides)
        return self.receipt(**values)

    def intent(self, **overrides: object) -> DaemonCleanupIntent:
        values: dict[str, object] = {
            'launch_uuid': LAUNCH, 'reason': ErrorCode.daemon_failed,
            'intent_at': INTENT_AT, 'completed_at': COMPLETED_AT, 'outcome': 'stopped',
        }
        values.update(overrides)
        return DaemonCleanupIntent(**values)  # type: ignore[arg-type]

    @staticmethod
    def _reserved(receipt: LaunchReceipt) -> LaunchReceipt:
        return replace(
            receipt, state='reserved', daemon_instance_uuid=None,
            supervisor=None, daemon=None, entry_device=None, entry_inode=None,
            socket_inode=None, peer_pid=None, peer_start_ticks=None, peer_uid=None,
            status_state=None, credential_service='unknown', failure=None,
        )

    async def store(self, receipt: LaunchReceipt, intent: DaemonCleanupIntent | None = None) -> None:
        assert isinstance(await self.receipts.write(self._reserved(receipt)), Ok)
        assert isinstance(await self.receipts.write(receipt), Ok)
        if intent is not None:
            await self.store_intent(intent)

    async def store_intent(self, intent: DaemonCleanupIntent) -> None:
        assert isinstance(await self.intents.write(replace(
            intent, completed_at=None, outcome=None,
        )), Ok)
        if intent.completed_at is not None or intent.outcome is not None:
            assert isinstance(await self.intents.write(intent), Ok)

    async def store_current(self, receipt: LaunchReceipt) -> None:
        detached = replace(
            receipt, state='detached', entry_device=None, entry_inode=None,
            socket_inode=None, peer_pid=None, peer_start_ticks=None, peer_uid=None,
            status_state=None, credential_service='unknown',
        )
        bound = replace(receipt, state='bound', status_state=None, credential_service='unknown')
        for step in (self._reserved(receipt), detached, bound, replace(receipt, state='ready')):
            assert isinstance(await self.receipts.write(step), Ok)

    async def proof(self, record: DaemonRecord | None = None):
        return await self.registry.termination_proof(record if record is not None else self.record())

    # -- assertions/support ----------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(self.root.rglob('*')) if path.is_file()
        }

    def assert_closed(self) -> None:
        for _, descriptor in self.opened:
            with pytest.raises(OSError):
                os.fstat(descriptor)

    def close(self) -> None:
        for descriptor in self.write_fds:
            try:
                os.close(descriptor)
            except OSError:
                pass
        self.write_fds.clear()


@pytest.fixture
def relay_receipts(tmp_path):
    cases: list[RelayCase] = []

    def make(*, clock_values: tuple[float, ...] = (10.0, 11.0)) -> RelayCase:
        case = RelayCase(tmp_path / f'case-{len(cases)}', clock_values)
        cases.append(case)
        return case

    yield make
    for case in cases:
        case.close()


async def test_relay_receipt_vocabulary(relay_receipts):
    """Only the actual receipt/intent vocabulary proves completed teardown."""

    stopped = relay_receipts()
    assert not stopped.runtime.exists()
    await stopped.store(stopped.receipt(state='stopped'))
    stopped.rows[DAEMON.pid] = stopped.absent(DAEMON.pid)
    proved_stopped = await stopped.proof()
    assert isinstance(proved_stopped, Ok)
    assert proved_stopped.value.receipt_state == 'stopped'
    assert proved_stopped.value.cleanup_outcome is None
    assert proved_stopped.value.cleanup_completed_at is None

    failed = relay_receipts()
    await failed.store(
        failed.receipt(state='failed', failure=ErrorCode.daemon_failed),
        failed.intent(outcome='stopped', completed_at=COMPLETED_AT),
    )
    failed.rows[DAEMON.pid] = failed.absent(DAEMON.pid)
    proved_failed = await failed.proof()
    assert isinstance(proved_failed, Ok)
    assert proved_failed.value.receipt_state == 'failed'
    assert proved_failed.value.cleanup_outcome == 'stopped'
    assert proved_failed.value.cleanup_completed_at == COMPLETED_AT
    # The proof vocabulary is exactly the receipt vocabulary; no terminated
    # receipt state or completed outcome is invented by this query.
    assert {proved_stopped.value.receipt_state, proved_failed.value.receipt_state} == {
        'stopped', 'failed',
    }

    negatives = {
        'none': None,
        'incomplete': failed.intent(completed_at=None, outcome=None),
        'error': failed.intent(completed_at=COMPLETED_AT, outcome=ErrorCode.cleanup_timeout),
        'early': failed.intent(completed_at=INTENT_AT - 1.0, outcome='stopped'),
    }
    for kind, intent in negatives.items():
        case = relay_receipts()
        await case.store(case.receipt(state='failed', failure=ErrorCode.daemon_failed), intent)
        case.rows[DAEMON.pid] = case.absent(DAEMON.pid)
        refused = await case.proof()
        assert isinstance(refused, Err), kind
        assert refused.error.code is ErrorCode.cleanup_timeout, kind


async def test_relay_termination_proof(relay_receipts):
    """History survives replacement; refused identities and controls prove nothing."""

    case = relay_receipts()
    record = case.record()
    await case.store(case.receipt(state='stopped'))
    await case.store_current(case.current_receipt())
    case.rows[DAEMON.pid] = case.absent(DAEMON.pid)
    case.rows[REPLACEMENT_DAEMON.pid] = case.present(REPLACEMENT_DAEMON)
    assert not case.runtime.exists()
    before = case.snapshot()
    proved = await case.proof(record)
    assert isinstance(proved, Ok)
    proof = proved.value
    assert proof.daemon_instance_uuid == ORIGINAL
    assert proof.daemon == DAEMON
    assert proof.runtime_directory == record.runtime_directory
    assert proof.launch_uuid == LAUNCH
    assert proof.receipt_state == 'stopped'
    assert proof.process_evidence == 'pid_absent'
    # The query observes: no repository byte, registry record, process control
    # or owned-process scan changes.
    assert case.snapshot() == before
    assert case.process.terminated == []
    assert case.process.spawned == []
    assert case.process.runs == []
    assert case.scans == []

    # A live failed-to-start receipt, even when latest, is not termination.
    failed_start = relay_receipts()
    await failed_start.store(failed_start.receipt(
        state='failed', failure=ErrorCode.daemon_failed,
    ))
    failed_start.rows[DAEMON.pid] = failed_start.present(DAEMON)
    failed_start.pidfd_exits = False
    refused = await failed_start.proof(failed_start.record())
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.cleanup_timeout

    # Wrong instance, process, runtime, missing record process and ambiguity
    # are identity-join failures, never cleanups.
    wrong_instance = relay_receipts()
    await wrong_instance.store(wrong_instance.receipt(daemon_instance_uuid=REPLACEMENT))
    wrong_instance.rows[DAEMON.pid] = wrong_instance.absent(DAEMON.pid)
    refused = await wrong_instance.proof(wrong_instance.record())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_event

    wrong_process = relay_receipts()
    await wrong_process.store(wrong_process.receipt(daemon=REPLACEMENT_DAEMON))
    wrong_process.rows[DAEMON.pid] = wrong_process.absent(DAEMON.pid)
    refused = await wrong_process.proof(wrong_process.record())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_event

    # A recycled PID is a different process instance: a matching pid is not
    # matching start_ticks and cannot become pidfd or absence evidence.
    recycled = relay_receipts()
    await recycled.store(recycled.receipt(state='stopped'))
    recycled.rows[DAEMON.pid] = recycled.present(replace(DAEMON, start_ticks=DAEMON.start_ticks + 1))
    refused = await recycled.proof(recycled.record())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.cleanup_timeout
    assert recycled.opened == []

    wrong_runtime = relay_receipts()
    other_runtime = str(wrong_runtime.root / 'other-runtime')
    await wrong_runtime.store(wrong_runtime.receipt(runtime_directory=other_runtime))
    wrong_runtime.rows[DAEMON.pid] = wrong_runtime.absent(DAEMON.pid)
    refused = await wrong_runtime.proof(wrong_runtime.record())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_event

    missing_process = relay_receipts()
    await missing_process.store(missing_process.receipt(state='stopped'))
    refused = await missing_process.proof(missing_process.record(daemon=None))
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_event

    ambiguous = relay_receipts()
    await ambiguous.store(ambiguous.receipt(state='stopped'))
    await ambiguous.store(ambiguous.receipt(state='stopped', launch_uuid=NEWER_LAUNCH))
    ambiguous.rows[DAEMON.pid] = ambiguous.absent(DAEMON.pid)
    refused = await ambiguous.proof(ambiguous.record())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.stale_event

    # Repository failures propagate unchanged; they never become proof.
    async def fail():
        return Err(error=diagnostic(ErrorCode.persistence_failed))

    receipt_failure = relay_receipts()
    await receipt_failure.store(receipt_failure.receipt(state='stopped'))
    receipt_failure.receipts.all = fail  # type: ignore[method-assign]
    refused = await receipt_failure.proof()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.persistence_failed

    intent_failure = relay_receipts()
    await intent_failure.store(intent_failure.receipt(state='stopped'))
    intent_failure.intents.all = fail  # type: ignore[method-assign]
    refused = await intent_failure.proof()
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.persistence_failed


@pytest.mark.parametrize('terminal', ['stopped', 'failed'])
@pytest.mark.parametrize('evidence', ['live', 'unreadable'])
async def test_relay_termination_requires_exit(relay_receipts, terminal, evidence):
    """Every otherwise-valid terminal record still needs positive exact exit."""

    case = relay_receipts()
    failed = terminal == 'failed'
    await case.store(
        case.receipt(state=terminal, failure=ErrorCode.daemon_failed if failed else None),
        case.intent() if failed else None,
    )
    case.rows[DAEMON.pid] = (
        case.present(DAEMON) if evidence == 'live'
        else IdentityEvidence(pid=DAEMON.pid, state=IdentityState.unreadable)
    )
    case.pidfd_exits = False
    refused = await case.proof()
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.cleanup_timeout
    if evidence == 'live':
        assert [pid for pid, _ in case.opened] == [DAEMON.pid]
        case.assert_closed()
    else:
        assert case.opened == []
    assert case.process.terminated == []
    assert case.process.spawned == []
    assert case.scans == []

    # Positive controls on the very same terminal record prevent blanket
    # refusal from passing this test.
    case.rows[DAEMON.pid] = case.absent(DAEMON.pid)
    absent_proof = await case.proof()
    assert isinstance(absent_proof, Ok)
    assert absent_proof.value.process_evidence == 'pid_absent'

    case.rows[DAEMON.pid] = case.present(DAEMON)
    case.pidfd_exits = True
    pidfd_proof = await case.proof()
    assert isinstance(pidfd_proof, Ok)
    assert pidfd_proof.value.process_evidence == 'pidfd_exited'
    case.assert_closed()


async def test_relay_termination_clock_domains(relay_receipts):
    """Persisted wall-clock cleanup completion is copied, never compared."""

    case = relay_receipts(clock_values=(10.0, 11.0))
    await case.store(
        case.receipt(state='failed', failure=ErrorCode.daemon_failed),
        case.intent(intent_at=INTENT_AT, completed_at=COMPLETED_AT, outcome='stopped'),
    )
    case.rows[DAEMON.pid] = case.present(DAEMON)
    case.pidfd_exits = True
    proved = await case.proof()
    assert isinstance(proved, Ok)
    proof = proved.value
    assert proof.cleanup_completed_at == COMPLETED_AT
    assert proof.identity_verified_at == 10.0
    assert proof.observed_at == 11.0
    assert proof.cleanup_completed_at > proof.observed_at

    early = relay_receipts(clock_values=(10.0, 11.0))
    await early.store(
        early.receipt(state='failed', failure=ErrorCode.daemon_failed),
        early.intent(intent_at=INTENT_AT, completed_at=INTENT_AT - 1.0, outcome='stopped'),
    )
    early.rows[DAEMON.pid] = early.absent(DAEMON.pid)
    refused = await early.proof()
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.cleanup_timeout
