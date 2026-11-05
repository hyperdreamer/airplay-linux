"""Independent offline facts for the real receipt-backed ownership algorithm."""
from __future__ import annotations

import errno
import importlib.util
import json
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from airplay_linux.backend.control import (
    ApplicationContractProfile, BackendOperation, PublishedDoubletakeProfile,
    PublishedDoubletakeControl,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, TerminationEvidence
from airplay_linux.testing.builders import target


@dataclass(frozen=True, kw_only=True)
class Receipt:
    """The frozen receipt interface, independent of the persistence lane."""

    schema_version: int = 1
    launch_uuid: UUID = UUID(int=101)
    instance_uuid: InstanceId = InstanceId(UUID(int=102))
    app: ProcessIdentity = ProcessIdentity(pid=110, start_ticks=10)
    owner_uid: int = 1000
    boot_id: str = str(UUID(int=103))
    runtime_directory: str = "/run/user/1000/airplay/owned"
    runtime_device: int = 4
    runtime_inode: int = 50
    endpoint_generation: int = 7
    socket_name: str = "daemon.sock"
    protocol_version: str = "v1"
    state: str = "ready"
    daemon_instance_uuid: InstanceId | None = InstanceId(UUID(int=104))
    supervisor: ProcessIdentity | None = ProcessIdentity(pid=120, start_ticks=20)
    daemon: ProcessIdentity | None = ProcessIdentity(pid=130, start_ticks=30)
    entry_device: int | None = 4
    entry_inode: int | None = 60
    socket_inode: int | None = 900
    peer_pid: int | None = 130
    peer_start_ticks: int | None = 30
    peer_uid: int | None = 1000
    status_state: str | None = "idle"
    credential_service: str = "keyring_proven"
    failure: ErrorCode | None = None
    updated_at: float = 1.0


def info(mode, inode, *, device=4, uid=1000):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_dev=device, st_ino=inode)


def live(identity, *, parent=120, uid=1000):
    return IdentityEvidence(
        pid=identity.pid, state=IdentityState.present, identity=identity,
        parent=parent, uid=uid,
    )


class EndpointEvidence:
    """Low-level observations; no replacement of the verification algorithm."""

    def __init__(self):
        self.receipt = Receipt()
        self.current_generation = 7  # independently supplied, not read from receipt
        self.receipts = (self.receipt,)
        self.directory = info(stat.S_IFDIR | 0o700, 50)
        self.entry = info(stat.S_IFSOCK | 0o600, 60)
        self.peer_credentials = (130, 1000, 1000)
        self.rows = (900,)
        self.fds = (900,)
        self.identities = {
            110: live(self.receipt.app, parent=1),
            120: live(self.receipt.supervisor, parent=110),
            130: live(self.receipt.daemon),
        }
        self.argv = (
            b"doubletake", b"-daemonize", b"-socket",
            b"/run/user/1000/airplay/owned/daemon.sock", b"-cred-backend", b"keyring",
        )
        self.raw_argv = None
        self.boot = str(UUID(int=103))
        self.on_peer = None
        self.on_cmdline = None
        self.on_post_open = None
        self.opened = {}
        self.next_fd = 10
        self.walk = []
        self.stat_anchors = []
        self.peer_anchors = []
        self.io_calls = []
        self.signal_calls = []
        self.launch_calls = []
        self.unlink_calls = []
        self.termination_calls = []
        self.intent = None
        self.intent_error = None
        self.source_error = None

    @property
    def record(self):
        return DaemonRecord(
            schema_version=1, instance_uuid=InstanceId(UUID(int=104)),
            owner_token="unused-public-token", app=self.receipt.app,
            supervisor=ProcessIdentity(pid=120, start_ticks=20),
            daemon=ProcessIdentity(pid=130, start_ticks=30), protocol_version="v1",
            socket_path=self.receipt.runtime_directory + "/daemon.sock",
            lifecycle="running", runtime_directory=self.receipt.runtime_directory,
        )

    def open(self, name, flags, *, dir_fd=None):
        assert flags & os.O_DIRECTORY and flags & os.O_NOFOLLOW
        assert name == "/" if dir_fd is None else name not in {"", ".", ".."} and "/" not in name
        if dir_fd is not None:
            assert dir_fd in self.opened
        self.next_fd += 1
        descriptor = self.next_fd
        self.opened[descriptor] = name
        self.walk.append((name, flags, dir_fd))
        return descriptor

    def close(self, descriptor):
        assert descriptor in self.opened
        del self.opened[descriptor]

    def fstat(self, descriptor):
        assert descriptor in self.opened
        return self.directory

    def stat(self, name, *, dir_fd, follow_symlinks):
        assert name == "daemon.sock" and follow_symlinks is False
        assert self.opened[dir_fd] == "owned"
        self.stat_anchors.append(dir_fd)
        if self.entry is None:
            raise FileNotFoundError
        return self.entry

    def peer(self, descriptor, name):
        assert self.opened[descriptor] == "owned" and name == "daemon.sock"
        self.peer_anchors.append(descriptor)
        if self.on_peer is not None:
            self.on_peer()
        if self.identities[130].state is not IdentityState.present:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "offline refused")
        return self.peer_credentials

    def unix_inodes(self, path):
        assert path == "/run/user/1000/airplay/owned/daemon.sock"
        return self.rows

    def holds_socket(self, pid, inode):
        assert pid == self.peer_credentials[0]
        return inode in self.fds

    def identity(self, pid):
        return self.identities.get(pid, IdentityEvidence(pid=pid, state=IdentityState.absent))

    def cmdline(self, pid):
        assert pid == 130
        if self.on_cmdline is not None:
            self.on_cmdline()
        return self.raw_argv if self.raw_argv is not None else b"\0".join(self.argv) + b"\0"

    def boot_id(self):
        return self.boot

    def unlink(self, name, *, dir_fd):
        assert self.opened[dir_fd] == "owned" and name == "daemon.sock"
        self.unlink_calls.append((name, dir_fd))
        self.entry = None

    def fsync(self, descriptor):
        assert descriptor in self.opened

    def source(self):
        return self.source_error or Ok(value=self.receipts)

    def release_intent(self):
        return self.intent_error or Ok(value=self.intent)

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline, *, strict=False, evidence=None, revalidate=None):
        assert strict is True and evidence is not None and revalidate is not None
        assert identity == self.record.daemon and runtime_directory == self.record.runtime_directory
        assert deadline == 3.0
        self.termination_calls.append((identity, evidence))
        if self.on_post_open is not None:
            self.on_post_open()
        checked = revalidate()
        if isinstance(checked, Err):
            return checked
        assert checked.value == evidence
        self.signal_calls.append(identity)
        self.identities[130] = IdentityEvidence(pid=130, state=IdentityState.absent)
        self.rows = ()
        return Ok(value=None)

    async def run(self, spec, timeout, stdin=None):
        self.io_calls.append(spec.argv)
        raise AssertionError("verification reached application control I/O")

    async def spawn(self, spec):
        self.launch_calls.append(spec.argv)
        raise AssertionError("verification launched a process")

    def absent(self):
        self.identities[130] = IdentityEvidence(pid=130, state=IdentityState.absent)
        self.rows = ()
        self.entry = None


def ownership_module():
    assert importlib.util.find_spec("airplay_linux.process.ownership") is not None, "receipt-backed endpoint verifier is missing"
    from airplay_linux.process import ownership
    return ownership


@pytest.fixture
def owned_endpoint_evidence(monkeypatch):
    e = EndpointEvidence()
    monkeypatch.setattr(ownership_module(), "_observations", e)
    yield e
    assert e.opened == {}, "temporary directory descriptors leaked"


@pytest.fixture
def reserved_supervisor_release(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = (replace(
        e.receipt, state="reserved", daemon_instance_uuid=None, supervisor=None, daemon=None,
        entry_device=None, entry_inode=None, socket_inode=None,
        peer_pid=None, peer_start_ticks=None, peer_uid=None,
        status_state=None, credential_service="unknown",
    ),)
    e.entry, e.rows = None, ()
    e.identities[110] = IdentityEvidence(pid=110, state=IdentityState.absent)
    e.intent = SimpleNamespace(
        schema_version=1, launch_uuid=e.receipt.launch_uuid, instance_uuid=e.receipt.instance_uuid,
        app=e.record.app, supervisor=e.record.supervisor, daemon=e.record.daemon,
        reason="application-exited", intent_at=2.0,
    )
    return e


def verify(e):
    return ownership_module().verify_owned_endpoint(e.receipt, current_generation=e.current_generation)


def authority(e, **kwargs):
    kwargs.setdefault("features", replace(PublishedDoubletakeProfile(socket_path=e.record.socket_path).features, owned_daemon_control=True))
    return ownership_module().EvidenceAuthority(
        receipts=e.source, process=e, evidence_factory=TerminationEvidence, **kwargs,
    )


def assert_refused(result, code):
    assert isinstance(result, Err) and result.error.code is code


def assert_no_effects(e):
    assert e.io_calls == e.signal_calls == e.launch_calls == e.unlink_calls == []


def test_verified_endpoint_keeps_entry_and_kernel_inodes_distinct(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    result = verify(e)
    assert isinstance(result, Ok)
    assert result.value.daemon == ProcessIdentity(pid=130, start_ticks=30)
    assert result.value.socket_entry == (4, 60)
    assert result.value.socket_inode == 900
    assert result.value.peer_uid == 1000 and result.value.generation == 7
    assert len(set(e.stat_anchors)) == 1
    assert e.peer_anchors == e.stat_anchors[:1]
    assert [call[0] for call in e.walk[:6]] == ["/", "run", "user", "1000", "airplay", "owned"]
    assert "130" not in repr(result.value) and "900" not in repr(result.value)
    assert_no_effects(e)


def test_different_socket_argv_refuses_before_control_or_signal(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok)
    e.argv = (b"doubletake", b"-daemonize", b"-socket", b"/wrong/socket", b"-cred-backend", b"keyring")
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


def test_peer_start_ticks_must_equal_receipt(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok)
    e.identities[130] = live(ProcessIdentity(pid=130, start_ticks=31))
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


def test_receipt_peer_start_ticks_mismatch_refuses_without_side_effects(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok), "unchanged positive control"
    e.receipt = replace(e.receipt, peer_start_ticks=31)
    assert e.identities[130].identity == ProcessIdentity(pid=130, start_ticks=30)
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert e.termination_calls == []
    assert_no_effects(e)


def test_duplicate_unix_path_rows_refuse(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok)
    e.rows = (900, 901)
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


def test_unlinked_old_listener_and_replacement_entry_refuse(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok)
    e.on_peer = lambda: setattr(e, "entry", info(stat.S_IFSOCK | 0o600, 61))
    assert_refused(verify(e), ErrorCode.stale_socket)
    # The old kernel inode and peer remain valid; the pathname no longer is.
    assert e.rows == (900,) and e.fds == (900,)
    assert_no_effects(e)


FAILURES = (
    "directory_uid", "directory_mode", "directory_inode", "directory_device",
    "entry_uid", "entry_type", "entry_inode", "missing_entry", "peer_uid", "peer_pid",
    "missing_rows", "duplicate_rows", "wrong_kernel_inode", "foreign_fd", "unreadable_fds",
    "uid", "ticks", "unreadable_identity", "missing_identity", "boot", "generation",
    "argv", "missing_daemonize", "second_socket", "inline_socket", "unterminated_argv",
    "entry_replaced", "entry_unlinked", "identity_after_argv",
)


def change_fact(e, name):
    if name.startswith("directory_"):
        field, value = {
            "directory_uid": ("st_uid", 1001), "directory_mode": ("st_mode", stat.S_IFDIR | 0o750),
            "directory_inode": ("st_ino", 51), "directory_device": ("st_dev", 5),
        }[name]
        setattr(e.directory, field, value)
    elif name in {"entry_uid", "entry_type", "entry_inode"}:
        field, value = {
            "entry_uid": ("st_uid", 1001), "entry_type": ("st_mode", stat.S_IFLNK | 0o600),
            "entry_inode": ("st_ino", 61),
        }[name]
        setattr(e.entry, field, value)
    elif name == "missing_entry":
        e.entry = None
    elif name == "peer_uid":
        e.peer_credentials = (130, 1001, 1000)
    elif name == "peer_pid":
        e.peer_credentials = (131, 1000, 1000)
    elif name in {"missing_rows", "duplicate_rows", "wrong_kernel_inode"}:
        e.rows = {"missing_rows": (), "duplicate_rows": (900, 900), "wrong_kernel_inode": (901,)}[name]
    elif name == "foreign_fd":
        e.fds = (901,)
    elif name == "unreadable_fds":
        e.holds_socket = lambda *_: None
    elif name == "uid":
        e.identities[130] = live(e.receipt.daemon, uid=1001)
    elif name == "ticks":
        e.identities[130] = live(ProcessIdentity(pid=130, start_ticks=31))
    elif name in {"unreadable_identity", "missing_identity"}:
        e.identities[130] = IdentityEvidence(pid=130, state=IdentityState.unreadable if name == "unreadable_identity" else IdentityState.absent)
    elif name == "boot":
        e.boot = str(UUID(int=105))
    elif name == "generation":
        e.current_generation = 8
    elif name == "argv":
        e.argv = (*e.argv[:3], b"/wrong/socket", *e.argv[4:])
    elif name == "missing_daemonize":
        e.argv = tuple(item for item in e.argv if item != b"-daemonize")
    elif name == "second_socket":
        e.argv += (b"-socket", b"/wrong/socket")
    elif name == "inline_socket":
        e.argv += (b"-socket=/wrong/socket",)
    elif name == "unterminated_argv":
        e.raw_argv = b"\0".join(e.argv)
    elif name in {"entry_replaced", "entry_unlinked"}:
        e.on_peer = lambda: setattr(e, "entry", None if name == "entry_unlinked" else info(stat.S_IFSOCK | 0o600, 61))
    elif name == "identity_after_argv":
        e.on_cmdline = lambda: e.identities.__setitem__(130, live(ProcessIdentity(pid=130, start_ticks=31)))
    else:
        raise AssertionError(name)


@pytest.mark.parametrize("failure", FAILURES)
def test_verifier_failure_has_no_process_side_effects(owned_endpoint_evidence, failure):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok), "unchanged positive control"
    change_fact(e, failure)
    result = verify(e)
    expected = ErrorCode.stale_event if failure == "generation" else (
        ErrorCode.stale_socket if failure.startswith("entry_") or failure == "missing_entry" else ErrorCode.ownership_mismatch
    )
    assert_refused(result, expected)
    assert_no_effects(e)


async def test_generation_uses_max_across_terminal_receipts_not_recency(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    a = authority(e)
    assert isinstance(await a.prove(e.record), Ok)
    # The newest timestamp is deliberately on the old nonterminal receipt.
    e.receipts = (replace(e.receipt, updated_at=99.0), replace(
        e.receipt, launch_uuid=UUID(int=200), state="stopped", endpoint_generation=8, updated_at=0.0,
    ))
    assert_refused(await a.prove(e.record), ErrorCode.stale_event)
    assert_refused(await a.release(e.record), ErrorCode.stale_event)
    assert_no_effects(e)


async def test_multiple_current_receipts_refuse_without_cleanup(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(await authority(e).prove(e.record), Ok)
    e.receipts += (replace(e.receipt, launch_uuid=UUID(int=201), endpoint_generation=8),)
    a = authority(e)
    assert_refused(await a.prove(e.record), ErrorCode.ownership_mismatch)
    assert_refused(await a.release(e.record), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


async def test_stale_terminal_receipt_cannot_authorize_cleanup(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    failed = replace(e.receipt, state="failed", failure=ErrorCode.daemon_failed)
    e.receipts = (failed,)
    e.absent()
    assert isinstance(await authority(e).release(e.record), Ok), "sole max failed plus independent absence"
    e.receipts += (replace(failed, launch_uuid=UUID(int=202), endpoint_generation=8, daemon_instance_uuid=InstanceId(UUID(int=203))),)
    assert_refused(await authority(e).release(e.record), ErrorCode.stale_event)
    assert_no_effects(e)
    # A lower nonterminal is unresolved even when the maximum is terminal.
    e.receipts = (e.receipt, replace(failed, launch_uuid=UUID(int=202), endpoint_generation=8))
    assert_refused(await authority(e).release(e.record), ErrorCode.stale_event)
    assert_no_effects(e)


async def test_evidence_authority_joins_daemon_instance_and_confirms_original_proof(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    a = authority(e)
    proved = await a.prove(e.record)
    assert isinstance(proved, Ok) and proved.value.kind == "evidence" and proved.value.handshake is None
    assert isinstance(await a.confirm(e.record, proved.value), Ok)
    assert "130" not in repr(proved.value)
    assert_refused(await a.prove(replace(e.record, instance_uuid=InstanceId(UUID(int=999)))), ErrorCode.ownership_mismatch)
    assert_no_effects(e)
    e.receipts = (replace(e.receipt, endpoint_generation=8),)
    assert_refused(await a.confirm(e.record, proved.value), ErrorCode.stale_event)


async def test_confirm_rejects_changed_current_receipt_entry(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    a = authority(e)
    proof = (await a.prove(e.record)).value
    e.receipts = (replace(e.receipt, entry_inode=61),)
    assert_refused(await a.confirm(e.record, proof), ErrorCode.stale_event)
    assert_no_effects(e)


async def test_authorize_reproves_and_evaluates_original_fence(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    a = authority(e)
    checks = []
    def fence():
        checks.append("original")
        return Err(error=diagnostic(ErrorCode.stale_target))
    assert a.allows(BackendOperation.disconnect) is True
    assert a.allows(BackendOperation.submit_pairing) is False
    assert a.allows(BackendOperation.restart_audio) is False
    assert a.allows(BackendOperation.query_modes) is False
    assert_refused(await a.authorize(e.record, BackendOperation.disconnect, target(), None), ErrorCode.dependency_incompatible)
    assert_refused(await a.authorize(e.record, BackendOperation.disconnect, target(), fence), ErrorCode.stale_target)
    assert checks == ["original"]
    e.argv = (b"foreign",)
    assert_refused(await a.authorize(e.record, BackendOperation.disconnect, target(), fence), ErrorCode.ownership_mismatch)
    assert checks == ["original"], "invalid ownership must not consult caller fence"
    assert_no_effects(e)


@pytest.mark.parametrize("stage", ["pre_bind", "bound"])
async def test_release_uses_stopping_receipt_strict_stage_and_post_open_check(owned_endpoint_evidence, stage):
    e = owned_endpoint_evidence
    stopped = replace(e.receipt, state="stopping")
    if stage == "pre_bind":
        stopped = replace(stopped, entry_device=None, entry_inode=None, socket_inode=None, peer_pid=None, peer_start_ticks=None, peer_uid=None)
        e.entry, e.rows = None, ()
    e.receipts = (stopped,)
    assert isinstance(await authority(e).release(e.record), Ok)
    assert len(e.signal_calls) == 1
    evidence = e.termination_calls[0][1]
    assert evidence.stage == stage
    assert evidence.expected_identity == ProcessIdentity(pid=130, start_ticks=30)
    assert evidence.expected_entry == ((4, 60) if stage == "bound" else None)
    assert len(e.unlink_calls) == (1 if stage == "bound" else 0)


@pytest.mark.parametrize("stage", ["pre_bind", "bound"])
async def test_release_post_open_mismatch_sends_no_signal_or_unlink(owned_endpoint_evidence, stage):
    e = owned_endpoint_evidence
    receipt = replace(e.receipt, state="stopping")
    if stage == "pre_bind":
        receipt = replace(receipt, entry_device=None, entry_inode=None, socket_inode=None, peer_pid=None, peer_start_ticks=None, peer_uid=None)
        e.entry, e.rows = None, ()
    e.receipts = (receipt,)
    e.on_post_open = lambda: setattr(e, "argv", (b"doubletake", b"-socket", b"/wrong/socket"))
    assert_refused(await authority(e).release(e.record), ErrorCode.ownership_mismatch)
    assert len(e.termination_calls) == 1
    assert_no_effects(e)


@pytest.mark.parametrize("state", ["ready", "failed", "stopped"])
async def test_outcome_or_observation_alone_never_grants_signal_authority(owned_endpoint_evidence, state):
    e = owned_endpoint_evidence
    e.receipts = (replace(e.receipt, state=state, failure=ErrorCode.daemon_failed if state == "failed" else None),)
    assert_refused(await authority(e).release(replace(e.record, lifecycle="stopping")), ErrorCode.ownership_mismatch)
    assert e.termination_calls == []
    assert_no_effects(e)


async def test_release_refuses_unreadable_identity_and_replacement_after_exit(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = (replace(e.receipt, state="stopping"),)
    e.identities[130] = IdentityEvidence(pid=130, state=IdentityState.unreadable)
    assert_refused(await authority(e).release(e.record), ErrorCode.ownership_mismatch)
    assert_no_effects(e)
    e.identities[130] = live(e.receipt.daemon)
    original = e.terminate
    async def replace_after_exit(*args, **kwargs):
        result = await original(*args, **kwargs)
        e.entry = info(stat.S_IFSOCK | 0o600, 61)
        return result
    e.terminate = replace_after_exit
    assert_refused(await authority(e).release(e.record), ErrorCode.stale_socket)
    assert e.unlink_calls == []


async def test_reserved_receipt_supervisor_release_uses_fresh_pre_bind_evidence(reserved_supervisor_release):
    e = reserved_supervisor_release
    result = await authority(e, supervisor_intent=e.release_intent).release(e.record)
    assert isinstance(result, Ok)
    assert e.signal_calls == [e.record.daemon]
    assert len(e.termination_calls) == 1
    evidence = e.termination_calls[0][1]
    assert evidence.stage == "pre_bind"
    assert evidence.expected_identity == e.record.daemon
    assert evidence.expected_supervisor == e.record.supervisor
    assert evidence.expected_entry is None and evidence.expected_socket_inode is None
    assert e.io_calls == e.launch_calls == e.unlink_calls == []


@pytest.mark.parametrize("member,value", [
    ("daemon_instance_uuid", InstanceId(UUID(int=999))),
    ("daemon", ProcessIdentity(pid=130, start_ticks=31)),
    ("supervisor", ProcessIdentity(pid=120, start_ticks=21)),
])
@pytest.mark.parametrize("after_open", [False, True])
async def test_supervisor_release_refuses_populated_receipt_identity_mismatch(reserved_supervisor_release, member, value, after_open):
    e = reserved_supervisor_release
    observed = dict(
        daemon_instance_uuid=e.record.instance_uuid, daemon=e.record.daemon,
        supervisor=e.record.supervisor,
    )
    observed[member] = value
    def contradict_receipt():
        e.receipts = (replace(e.receipts[0], state="detached", **observed),)
    if after_open:
        e.on_post_open = contradict_receipt
    else:
        contradict_receipt()
    assert_refused(await authority(e, supervisor_intent=e.release_intent).release(e.record), ErrorCode.ownership_mismatch)
    assert len(e.termination_calls) == int(after_open)
    assert_no_effects(e)


@pytest.mark.parametrize("failure", ["parent", "ticks", "supervisor_ticks", "argv", "intent_daemon", "live_app"])
@pytest.mark.parametrize("after_open", [False, True])
async def test_reserved_supervisor_release_rechecks_intent_and_process_facts(reserved_supervisor_release, failure, after_open):
    e = reserved_supervisor_release
    def change():
        if failure == "parent":
            e.identities[130] = live(e.record.daemon, parent=999)
        elif failure == "ticks":
            e.identities[130] = live(ProcessIdentity(pid=130, start_ticks=31))
        elif failure == "supervisor_ticks":
            e.identities[120] = live(ProcessIdentity(pid=120, start_ticks=21), parent=110)
        elif failure == "argv":
            e.argv = (b"doubletake", b"-daemonize", b"-socket", b"/wrong/socket")
        elif failure == "intent_daemon":
            e.intent.daemon = ProcessIdentity(pid=999, start_ticks=30)
        else:
            e.identities[110] = live(e.receipt.app, parent=1)
    if after_open:
        e.on_post_open = change
    else:
        change()
    assert_refused(await authority(e, supervisor_intent=e.release_intent).release(e.record), ErrorCode.ownership_mismatch)
    assert len(e.termination_calls) == int(after_open)
    assert_no_effects(e)


async def test_no_receipt_supervisor_release_requires_saved_reservation_and_own_intent(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = ()
    e.identities[110] = IdentityEvidence(pid=110, state=IdentityState.absent)
    e.intent = SimpleNamespace(
        schema_version=1, launch_uuid=UUID(int=101), instance_uuid=InstanceId(UUID(int=102)),
        app=e.receipt.app, supervisor=e.receipt.supervisor, daemon=e.receipt.daemon,
        reason="application-exited", intent_at=2.0,
    )
    # Only reservation fields are available; no fabricated bound receipt.
    reservation = SimpleNamespace(**{key: getattr(e.receipt, key) for key in (
        "schema_version", "launch_uuid", "instance_uuid", "app", "owner_uid", "boot_id", "runtime_directory",
        "runtime_device", "runtime_inode", "endpoint_generation", "socket_name", "protocol_version",
    )})
    assert_refused(await authority(e).release(e.record), ErrorCode.ownership_mismatch)
    a = authority(e, reservation=reservation, supervisor_intent=e.release_intent)
    assert isinstance(await a.release(e.record), Ok)
    assert len(e.signal_calls) == 1 and len(e.unlink_calls) == 1


@pytest.mark.parametrize("failure", ["live_app", "unreadable_app", "wrong_launch", "wrong_daemon", "missing", "unreadable"])
async def test_supervisor_intent_refusal_has_no_signal(owned_endpoint_evidence, failure):
    e = owned_endpoint_evidence
    e.identities[110] = IdentityEvidence(pid=110, state=IdentityState.absent)
    e.intent = SimpleNamespace(
        schema_version=1, launch_uuid=e.receipt.launch_uuid, instance_uuid=e.receipt.instance_uuid,
        app=e.receipt.app, supervisor=e.receipt.supervisor, daemon=e.receipt.daemon,
        reason="application-exited", intent_at=2.0,
    )
    if failure == "live_app":
        e.identities[110] = live(e.receipt.app, parent=1)
    elif failure == "unreadable_app":
        e.identities[110] = IdentityEvidence(pid=110, state=IdentityState.unreadable)
    elif failure == "wrong_launch":
        e.intent.launch_uuid = UUID(int=999)
    elif failure == "wrong_daemon":
        e.intent.daemon = ProcessIdentity(pid=999, start_ticks=30)
    elif failure == "missing":
        e.intent = None
    else:
        e.intent_error = Err(error=diagnostic(ErrorCode.persistence_failed))
    result = await authority(e, supervisor_intent=e.release_intent).release(e.record)
    assert_refused(result, ErrorCode.persistence_failed if failure == "unreadable" else ErrorCode.ownership_mismatch)
    assert_no_effects(e)


async def test_default_evidence_authority_never_derives_composition_capabilities(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    a = ownership_module().EvidenceAuthority(
        receipts=e.source, process=e, evidence_factory=TerminationEvidence,
    )
    assert a.allows(BackendOperation.status) is True
    for operation in (BackendOperation.connect, BackendOperation.disconnect, BackendOperation.mute, BackendOperation.volume, BackendOperation.audio_off, BackendOperation.shutdown):
        assert a.allows(operation) is False
    assert_no_effects(e)


async def test_stopping_receipt_before_observation_uses_fresh_pre_bind_identity(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = (replace(
        e.receipt, state="stopping", daemon_instance_uuid=None, supervisor=None, daemon=None,
        entry_device=None, entry_inode=None, socket_inode=None, peer_pid=None, peer_start_ticks=None, peer_uid=None,
    ),)
    e.entry, e.rows = None, ()
    assert isinstance(await authority(e).release(e.record), Ok)
    assert e.termination_calls[0][1].stage == "pre_bind"
    assert len(e.signal_calls) == 1 and e.unlink_calls == []


async def test_pre_bind_parent_is_rechecked_after_argv(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = (replace(
        e.receipt, state="stopping", entry_device=None, entry_inode=None, socket_inode=None,
        peer_pid=None, peer_start_ticks=None, peer_uid=None,
    ),)
    e.entry, e.rows = None, ()
    e.on_cmdline = lambda: e.identities.__setitem__(130, live(e.receipt.daemon, parent=999))
    assert_refused(await authority(e).release(e.record), ErrorCode.ownership_mismatch)
    assert e.termination_calls == [], "pre-open evidence must recheck the parent after argv"
    assert_no_effects(e)


async def test_unconfirmed_entry_cleanup_reports_timeout(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    e.receipts = (replace(e.receipt, state="stopping"),)
    def failed_sync(_):
        raise OSError(errno.EIO, "offline durability failure")
    e.fsync = failed_sync
    assert_refused(await authority(e).release(e.record), ErrorCode.cleanup_timeout)
    assert len(e.signal_calls) == len(e.unlink_calls) == 1


@pytest.mark.parametrize("failure", ["ticks", "uid", "peer_pid", "peer_uid", "duplicate_rows", "wrong_kernel_inode", "foreign_fd", "entry_inode", "boot", "receipt_generation", "receipt_entry"])
async def test_bound_post_open_repeats_every_endpoint_fact(owned_endpoint_evidence, failure):
    e = owned_endpoint_evidence
    e.receipts = (replace(e.receipt, state="stopping"),)
    def change_after_open():
        if failure == "receipt_generation":
            e.receipts = (replace(e.receipts[0], endpoint_generation=8),)
        elif failure == "receipt_entry":
            e.receipts = (replace(e.receipts[0], entry_inode=61),)
        else:
            change_fact(e, failure)
    e.on_post_open = change_after_open
    result = await authority(e).release(e.record)
    assert isinstance(result, Err)
    assert len(e.termination_calls) == 1
    assert_no_effects(e)


@pytest.mark.parametrize("name", ["run", "user", "1000", "airplay", "owned"])
def test_failed_component_open_closes_previous_anchor(owned_endpoint_evidence, name):
    e = owned_endpoint_evidence
    original = e.open
    def inaccessible(component, flags, *, dir_fd=None):
        if component == name:
            raise OSError(errno.ELOOP, "offline no-follow refusal")
        return original(component, flags, dir_fd=dir_fd)
    e.open = inaccessible
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert e.opened == {}
    assert_no_effects(e)


@pytest.mark.parametrize("argument", [b"--socket", b"--socket=/wrong/socket"])
def test_conflicting_long_socket_argument_refuses(owned_endpoint_evidence, argument):
    e = owned_endpoint_evidence
    assert isinstance(verify(e), Ok)
    e.argv += (argument, b"/wrong/socket")
    assert_refused(verify(e), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


class FixtureProcess:
    def __init__(self, record):
        self.record = record
        self.calls = []
        self.overrides = {}

    async def run(self, spec, timeout, stdin=None):
        self.calls.append(spec.argv)
        r = self.record
        data = {
            "ok": True, "state": "idle", "has_audio": False, "audio_muted": False,
            "owner_token": r.owner_token,
            "instance_uuid": str(r.instance_uuid), "socket_path": r.socket_path,
            "protocol_version": r.protocol_version, "daemon_pid": r.daemon.pid,
            "daemon_start_ticks": r.daemon.start_ticks,
        }
        data.update(self.overrides)
        return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps(data).encode(), stderr=b""))


@pytest.mark.parametrize("member", ["owner_token", "instance_uuid", "socket_path", "protocol_version", "daemon_pid", "daemon_start_ticks"])
async def test_handshake_authority_retains_each_legacy_equality(owned_endpoint_evidence, member):
    from airplay_linux.backend.control import HandshakeAuthority
    e = owned_endpoint_evidence
    process = FixtureProcess(e.record)
    control = PublishedDoubletakeControl(process, ApplicationContractProfile(
        socket_path=e.record.socket_path, owner_token=e.record.owner_token, instance_uuid=e.record.instance_uuid,
    ))
    a = HandshakeAuthority(control, socket_entry=lambda _: Ok(value=(4, 60)), generation=lambda _: 7)
    good = await a.prove(e.record)
    assert isinstance(good, Ok) and good.value.kind == "handshake" and good.value.handshake is not None
    process.overrides[member] = {
        "owner_token": "foreign", "instance_uuid": str(UUID(int=999)), "socket_path": "/wrong/socket",
        "protocol_version": "v2", "daemon_pid": 131, "daemon_start_ticks": 31,
    }[member]
    result = await a.prove(e.record)
    assert isinstance(result, Err)
    assert all(argv[3] == "status" for argv in process.calls), "raw status must not recurse through authority"
    assert_no_effects(e)


@pytest.mark.parametrize("kind", ["evidence", "handshake"])
async def test_authorities_refuse_external_shutdown_but_keep_strict_release(owned_endpoint_evidence, kind):
    from airplay_linux.backend.control import HandshakeAuthority
    e = owned_endpoint_evidence
    a = strict_authority = authority(e)
    process = FixtureProcess(e.record)
    if kind == "handshake":
        control = PublishedDoubletakeControl(process, ApplicationContractProfile(
            socket_path=e.record.socket_path, owner_token=e.record.owner_token, instance_uuid=e.record.instance_uuid,
        ))
        a = HandshakeAuthority(
            control, socket_entry=lambda _: Ok(value=(4, 60)), generation=lambda _: 7,
            release=strict_authority.release,
        )
    assert a.allows(BackendOperation.shutdown) is True
    assert isinstance(await a.prove(e.record), Ok), "valid ownership is the unchanged control"
    process.calls.clear()
    observations = len(e.walk), len(e.peer_anchors)
    assert_refused(await a.authorize(e.record, BackendOperation.shutdown, None, None), ErrorCode.dependency_incompatible)
    assert process.calls == [] and (len(e.walk), len(e.peer_anchors)) == observations
    assert e.termination_calls == []
    assert_no_effects(e)
    e.receipts = (replace(e.receipt, state="stopping"),)
    assert isinstance(await a.release(e.record), Ok)
    assert e.signal_calls == [e.record.daemon] and len(e.unlink_calls) == 1
    assert len(e.termination_calls) == 1 and e.termination_calls[0][1].stage == "bound"
    assert all(argv[3] == "status" for argv in process.calls)
    assert e.io_calls == e.launch_calls == []


@pytest.mark.parametrize("owned,multi_target,physical_source,expected", [
    (True, True, True, True),
    (False, True, True, False),
    (True, False, True, False),
    (True, True, False, False),
])
@pytest.mark.parametrize("keyring", [False, True])
def test_connect_capability_predicate_is_independent_of_keyring(owned_endpoint_evidence, owned, multi_target, physical_source, expected, keyring):
    e = owned_endpoint_evidence
    features = replace(
        PublishedDoubletakeProfile(socket_path=e.record.socket_path).features,
        owned_daemon_control=owned, multi_target=multi_target, physical_source=physical_source,
        keyring=keyring,
    )
    assert authority(e, features=features).allows(BackendOperation.connect) is expected
    assert e.walk == [] and e.termination_calls == []
    assert_no_effects(e)


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("volume", [False, True])
async def test_volume_authority_requires_three_independent_gates(owned_endpoint_evidence, operation, owned, multi, volume):
    e = owned_endpoint_evidence
    features = replace(
        PublishedDoubletakeProfile(socket_path=e.record.socket_path).features,
        owned_daemon_control=owned, multi_target=multi, target_volume=volume,
    )
    a = authority(e, features=features)
    op = BackendOperation(operation)
    assert a.allows(op) is (owned and multi and volume)
    if not (owned and multi and volume):
        assert_refused(await a.authorize(e.record, op, target(), lambda: Ok(value=None)), ErrorCode.dependency_incompatible)
        assert e.walk == []
    else:
        assert isinstance(await a.authorize(e.record, op, target(), lambda: Ok(value=None)), Ok)
    assert_no_effects(e)


@pytest.mark.parametrize("operation", ["volume", "audio_off"])
async def test_volume_authority_requires_fence_and_current_receipt(owned_endpoint_evidence, operation):
    e = owned_endpoint_evidence
    features = replace(
        PublishedDoubletakeProfile(socket_path=e.record.socket_path).features,
        owned_daemon_control=True, target_volume=True,
    )
    a, op = authority(e, features=features), BackendOperation(operation)
    assert_refused(await a.authorize(e.record, op, target(), None), ErrorCode.dependency_incompatible)
    assert e.walk == []
    assert_refused(await a.authorize(e.record, op, target(), lambda: Err(error=diagnostic(ErrorCode.stale_target))), ErrorCode.stale_target)
    e.receipts = (replace(e.receipt, peer_start_ticks=999),)
    assert_refused(await a.authorize(e.record, op, target(), lambda: Ok(value=None)), ErrorCode.ownership_mismatch)
    assert_no_effects(e)


@pytest.mark.parametrize("keyring", [False, True])
async def test_transport_admission_still_requires_keyring(owned_endpoint_evidence, tmp_path, keyring):
    from airplay_linux.storage import AtomicFiles, JsonReceiverStore
    from airplay_linux.testing.adapters import FakeCapture, SequenceIds
    from airplay_linux.testing.builders import context
    from airplay_linux.testing.time import FakeClock, FakeScheduler
    from airplay_linux.transport.doubletake import DoubleTakeTransport
    from airplay_linux.trust.fences import TrustFenceRegistry
    from airplay_linux.trust.keyring import BackendKeyringStore
    e = owned_endpoint_evidence
    process = FixtureProcess(e.record)
    profile = ApplicationContractProfile(
        socket_path=e.record.socket_path, owner_token=e.record.owner_token, instance_uuid=e.record.instance_uuid,
    )
    features = replace(profile.features, keyring=keyring)
    control = PublishedDoubletakeControl(process, profile)
    ensured = []
    async def ensure():
        ensured.append(e.record)
        return Ok(value=e.record)
    registry = SimpleNamespace(
        ensure=ensure, subscribe=lambda _: SimpleNamespace(close=lambda: None),
    )
    clock = FakeClock(20)
    transport = DoubleTakeTransport(
        lambda _: control, registry, FakeCapture(), BackendKeyringStore(control, features),
        JsonReceiverStore(AtomicFiles(root=tmp_path)), TrustFenceRegistry(), features,
        clock, FakeScheduler(clock), SequenceIds(100),
    )
    receiver = target()
    transport.update_target(receiver, receiver.receiver_id)
    admitted = await transport._admit(receiver, context())
    if keyring:
        assert isinstance(admitted, Ok) and admitted.value.record == e.record
        assert ensured == [e.record] and len(process.calls) == 1
        assert process.calls[0][3] == "status"
    else:
        assert_refused(admitted, ErrorCode.dependency_incompatible)
        assert ensured == process.calls == []
    assert_no_effects(e)


async def test_production_activation_remains_closed(tmp_path, monkeypatch):
    """Eligible real composition still cannot run a backend or outer unshare."""
    from dbus_next.aio import MessageBus
    from airplay_linux import runtime as composition
    from airplay_linux.backend.contract import CredentialProofVerdict, probe_backend
    from airplay_linux.domain.resources import CapabilityReport
    from airplay_linux.policy.capabilities import EnvironmentProbe
    from airplay_linux.process.linux import LinuxProcessRunner

    async def forbidden(*args, **kwargs):
        raise AssertionError("offline tripwire intercepted a host process or bus")
    async def check(_):
        return CapabilityReport(
            environment="kde-plasma-6-wayland", discovery=True, mirror=True, virtual_display=False,
            audio=True, remembered_pairing=False, additional_virtual_outputs=False, diagnostics=(),
        )
    async def nothing():
        return Ok(value=None)
    monkeypatch.setattr(LinuxProcessRunner, "run", forbidden)
    monkeypatch.setattr(LinuxProcessRunner, "spawn", forbidden)
    monkeypatch.setattr(MessageBus, "connect", forbidden)
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace(close=nothing))
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", lambda: SimpleNamespace(check=nothing))
    monkeypatch.setattr(EnvironmentProbe, "check", check)
    for method in (LinuxProcessRunner().run, LinuxProcessRunner().spawn):
        with pytest.raises(AssertionError, match="offline tripwire"):
            await method(SimpleNamespace(argv=("offline",)))
    with pytest.raises(AssertionError, match="offline tripwire"):
        await MessageBus.connect(SimpleNamespace())

    class Recorder:
        def __init__(self):
            self.calls = []
        async def run(self, spec, timeout, stdin=None):
            self.calls.append(("run", spec.argv))
            fixtures = Path(__file__).parents[1] / "fixtures" / "backend"
            payloads = {
                ("doubletake", "--help"): (fixtures / "doubletake-0.5-source-help.txt").read_bytes(),
                ("doubletake-ctl", "--help"): (fixtures / "doubletake-ctl-0.5-help.txt").read_bytes(),
                ("doubletake", "--version"): b"doubletake 0.5.0\n",
                ("doubletake", "-capabilities"): b"capabilities-v1\nstatus-source\nstatus-source-complete\n",
                ("kscreen-doctor", "-j"): b'{"outputs":[{"name":"DP-1","enabled":true,"connected":true,"type":14}]}',
            }
            if spec.argv in payloads:
                return Ok(value=CommandOutput(exit_code=0, stdout=payloads[spec.argv], stderr=b""))
            return Err(error=diagnostic(ErrorCode.dependency_incompatible))
        async def spawn(self, spec):
            self.calls.append(("spawn", spec.argv))
            return Err(error=diagnostic(ErrorCode.dependency_incompatible))
        def subscribe(self, callback):
            return SimpleNamespace(close=lambda: None)

    recorder = Recorder()
    seen = []
    async def complete_probe(process):
        assert process is recorder
        result = await probe_backend(process)
        assert isinstance(result, Ok) and result.value.profile == "doubletake-0.5+source"
        return Ok(value=replace(result.value, source_reporting_complete=True))
    async def credential_proof():
        seen.append("credential-proof")
        await recorder.run(SimpleNamespace(argv=("unshare", "-rn", "offline-proof")), timeout=15.0)
        return Ok(value=CredentialProofVerdict(service_leg="passed", backend_positive_leg="passed", backend_negative_leg="passed", failure=None))
    def installation(config, *, process, launcher):
        assert process is recorder and launcher.production_graph and launcher.available()
        return composition._OwnedDaemonInstallation(
            source_verifier=SimpleNamespace(physical_inventory_available=True), credential_proof=credential_proof,
        )
    monkeypatch.setattr(composition, "probe_backend", complete_probe)
    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    root = tmp_path / "r"
    root.mkdir(mode=0o700)
    result = await composition.build_runtime(
        composition.RuntimeConfig(state_root=tmp_path / "s", runtime_root=root, live=False),
        overrides={"process": recorder},
    )
    assert isinstance(result, Ok)
    runtime = result.value
    try:
        assert runtime.registry._process is recorder
        assert isinstance(await runtime.deps.capture.list_physical(), Ok)
        for attempt in (runtime.registry.ensure, runtime.deps.restart_daemon):
            assert_refused(await attempt(), ErrorCode.dependency_incompatible)
    finally:
        await runtime.shutdown()
    assert seen == []
    assert not any(method == "spawn" for method, _ in recorder.calls)
    assert not any(Path(argv[0]).name == "unshare" for _, argv in recorder.calls)
    assert not any(Path(argv[0]).name == "doubletake" and "-daemonize" in argv[1:] for _, argv in recorder.calls)
    observed = {argv[-1] for _, argv in recorder.calls}
    assert {"--help", "--version", "-capabilities", "-j"} <= observed


# ------------------------------------------------------- /proc/net/unix endpoint rows


def _unix_rows(*rows: str) -> bytes:
    return (
        "Num       RefCount Protocol Flags    Type St Inode Path\n"
        + "\n".join(rows)
        + "\n"
    ).encode()


def _row(index: str, state: str, inode: str, path: str) -> str:
    return (
        f"00000000{index}: 00000002 00000000 00010000 0001 {state} "
        f"{inode:>5} {path}"
    )


def test_unix_inodes_counts_only_the_listening_endpoint(monkeypatch):
    """A connected client row names the server path but is not a second listener."""
    from airplay_linux.process import ownership

    path = "/run/user/1000/airplay/owned/daemon.sock"
    monkeypatch.setattr(ownership, "_read", lambda _: _unix_rows(
        _row("aaaaaaaa", "01", "900", path),
        _row("bbbbbbbb", "02", "0", path),
        _row("cccccccc", "03", "901", path),
        _row("dddddddd", "01", "902", "/elsewhere.sock"),
    ))

    assert ownership.EndpointObservations.unix_inodes(path) == (900,)


def test_unix_inodes_still_counts_a_second_listener(monkeypatch):
    """Two unconnected endpoints remain ambiguous evidence."""
    from airplay_linux.process import ownership

    path = "/run/user/1000/airplay/owned/daemon.sock"
    monkeypatch.setattr(ownership, "_read", lambda _: _unix_rows(
        _row("aaaaaaaa", "01", "900", path),
        _row("bbbbbbbb", "01", "901", path),
    ))

    assert ownership.EndpointObservations.unix_inodes(path) == (900, 901)


def test_unix_inodes_refuses_malformed_inode_evidence(monkeypatch):
    from airplay_linux.process import ownership

    path = "/run/user/1000/airplay/owned/daemon.sock"
    monkeypatch.setattr(ownership, "_read", lambda _: _unix_rows(
        _row("aaaaaaaa", "01", "x", path),
    ))

    assert ownership.EndpointObservations.unix_inodes(path) is None
