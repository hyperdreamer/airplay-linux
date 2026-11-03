"""One isolated, receipt-backed credential witness and its file-only parent proof.

The synchronous child seam owns its receipt source, process subscription and
kernel observations. It never opens the application's shared authority. The
parent keeps a directory descriptor across the single namespace invocation and
accepts only durable, strictly decoded files after that child exits successfully.
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import mkdtemp
from typing import Final, Literal
from uuid import UUID, uuid4

from airplay_linux.backend.contract import CredentialProofVerdict, decode_status
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.ports import CommandOutput, CommandSpec, ProcessExit, ProcessPort
from airplay_linux.domain.records import (
    DaemonCleanupIntent, DaemonRecord, DaemonReservation, LaunchReceipt, ProbeVerdict,
)
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.policy.audio import DBusCapabilityClient, secret_service_available
from airplay_linux.process.ownership import EvidenceAuthority, EndpointObservations, verify_owned_endpoint
from airplay_linux.process.procfs import (
    IdentityState, OWNER_TOKEN_ENV, RUNTIME_DIRECTORY_ENV, TerminationEvidence,
    read_identity_evidence,
)
from airplay_linux.storage.atomic import AtomicFiles, StorageError
from airplay_linux.storage.codecs import (
    RecordError, decode_launch_receipt, decode_probe_verdict, encode_daemon_reservation,
    encode_probe_verdict,
)
from airplay_linux.storage.repositories import (
    JsonDaemonCleanupIntentStore, JsonLaunchReceiptStore, LAUNCH_RECEIPT_DIRECTORY, _lifecycle_all,
)

SOCKET_IDLE_TIMEOUT = 3.0
IDLE_CONFIRMATION = 2.0
TERMINATION_GRACE = 3.0
POLL_INTERVAL = 0.05
UNREACHABLE_BUS = "unix:path=/nonexistent/airplay-credential-probe-bus"
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_RUNTIME_OBLIGATION = "runtime-cleanup.json"
_RUNTIME_LIFECYCLE = "runtime-lifecycle.json"
_BACKEND_DISPATCH = "backend-dispatch.json"


@dataclass(frozen=True, kw_only=True)
class ProbeConfig:
    probe_uuid: UUID = field(repr=False)
    reservations: tuple[DaemonReservation, DaemonReservation] = field(repr=False)
    backend_executable: str = field(repr=False)
    record_root: Path = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class WitnessEnvironment:
    """Namespace-local external observations; never a replacement verdict."""

    observations: EndpointObservations = field(repr=False)
    service: Callable[[], Awaitable[bool]] = field(repr=False)
    now: Callable[[], float] = field(repr=False)
    sleep: Callable[[float], Awaitable[None]] = field(repr=False)


def _witness_environment() -> WitnessEnvironment:
    client = DBusCapabilityClient()
    return WitnessEnvironment(
        observations=EndpointObservations(), service=lambda: secret_service_available(client),
        now=time.monotonic, sleep=asyncio.sleep,
    )


class _Refused(Exception):
    def __init__(self, code: ErrorCode = ErrorCode.ownership_mismatch) -> None:
        self.code = code


def _require(condition: bool, code: ErrorCode = ErrorCode.ownership_mismatch) -> None:
    if not condition:
        raise _Refused(code)


def _err(code: ErrorCode = ErrorCode.ownership_mismatch) -> Err:
    return Err(error=diagnostic(code))


def _private(info) -> None:
    _require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700
             and info.st_uid == os.getuid())


def _open_directory(path: Path) -> int:
    _require(path.is_absolute() and str(path) != "/" and "\0" not in str(path)
             and all(part not in {".", ".."} for part in path.parts))
    descriptor = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            entered = os.open(part, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = entered
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_probe_root(root: Path, probe_uuid: UUID) -> int:
    """Retain this no-follow root across dispatch; the caller closes the fd."""
    _require(type(probe_uuid) is UUID and root.name == str(probe_uuid))
    descriptor = _open_directory(root)
    try:
        _private(os.fstat(descriptor))
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _same_root(root: Path, probe_uuid: UUID, descriptor: int) -> None:
    pinned = os.fstat(descriptor)
    _private(pinned)
    fresh = open_probe_root(root, probe_uuid)
    try:
        found = os.fstat(fresh)
        _require((pinned.st_dev, pinned.st_ino) == (found.st_dev, found.st_ino))
    finally:
        os.close(fresh)


@dataclass
class _RuntimeAllocation:
    root: Path
    descriptor: int
    retained: bool = False


@contextmanager
def _short_runtime_root(
    probe_uuid: UUID, record_root: Path | None = None,
) -> Iterator[_RuntimeAllocation]:
    """Own the pinned empty allocation until durable dispatch publication.

    Only the caller's explicit retention transfer allows this tree to outlive
    the context. Before that point no namespace/backend dispatch is permitted.
    """
    parent = Path(mkdtemp(prefix="ap-", dir="/tmp"))
    descriptor = None
    allocation = None
    try:
        descriptor = _open_directory(parent)
        _private(os.fstat(descriptor))
        os.mkdir(str(probe_uuid), 0o700, dir_fd=descriptor)
        os.fsync(descriptor)
        temporary = _open_directory(parent.parent)
        try:
            os.fsync(temporary)
        finally:
            os.close(temporary)
        root = parent / str(probe_uuid)
        allocation = _RuntimeAllocation(root, open_probe_root(root, probe_uuid))
        yield allocation
    finally:
        try:
            if allocation is not None and not allocation.retained:
                _reclaim_undispatched(record_root, allocation)
            elif allocation is None:
                # Nothing was handed to a caller, so no dispatch was possible.
                shutil.rmtree(parent)
        finally:
            if allocation is not None:
                os.close(allocation.descriptor)
            if descriptor is not None:
                os.close(descriptor)


def _runtime_binding(root: Path, descriptor: int) -> dict:
    parent = _open_directory(root.parent)
    try:
        _private(os.fstat(parent))
        info, outer = os.fstat(descriptor), os.fstat(parent)
        return dict(schema="airplay-linux/probe-runtime-cleanup/v1", runtime_root=str(root),
                    runtime_device=info.st_dev, runtime_inode=info.st_ino,
                    parent_device=outer.st_dev, parent_inode=outer.st_ino)
    finally:
        os.close(parent)


def _binding_bytes(binding: dict) -> bytes:
    return json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_runtime_binding(binding: dict, probe_uuid: UUID) -> dict:
    _require(type(binding) is dict and set(binding) == {
        "schema", "runtime_root", "runtime_device", "runtime_inode", "parent_device", "parent_inode",
    })
    _require(binding["schema"] == "airplay-linux/probe-runtime-cleanup/v1"
             and type(binding["runtime_root"]) is str)
    for key in ("runtime_device", "runtime_inode", "parent_device", "parent_inode"):
        _require(type(binding[key]) is int and binding[key] >= (0 if key.endswith("device") else 1))
    root = Path(binding["runtime_root"])
    _require(root.name == str(probe_uuid) and root.parent.parent == Path("/tmp")
             and re.fullmatch(r"ap-[a-z0-9_]+", root.parent.name) is not None)
    _require(len(os.fsencode(root / "positive/daemon.sock")) < 108)
    return binding


def _read_runtime_binding(descriptor: int, probe_uuid: UUID) -> dict:
    payload = _read_file(descriptor, _RUNTIME_OBLIGATION, 4096)
    binding = _validate_runtime_binding(json.loads(payload), probe_uuid)
    _require(_binding_bytes(binding) == payload)
    return binding


def _same_runtime(binding: dict, descriptor: int) -> None:
    root = Path(binding["runtime_root"])
    _same_root(root, UUID(root.name), descriptor)
    _require(_runtime_binding(root, descriptor) == binding)


def _new_lifecycle(binding: dict, dispatch: str = "allocated") -> dict:
    return dict(schema="airplay-linux/probe-runtime-lifecycle/v1", binding=binding,
                dispatch=dispatch, removal=None)


def _write_lifecycle(record_root: Path, lifecycle: dict) -> None:
    AtomicFiles(record_root).write(_RUNTIME_LIFECYCLE, _binding_bytes(lifecycle))


def _record_completion(record_root: Path, lifecycle: dict, dispatch: str) -> None:
    lifecycle["dispatch"] = dispatch
    try:
        _write_lifecycle(record_root, lifecycle)
    except (OSError, StorageError):
        # The ProcessPort completion is exact evidence we cannot obtain again.
        # Preserve it across one failed/uncertain publication before reporting
        # that failure. A later instance then needs no nonexistent child receipt.
        _write_lifecycle(record_root, lifecycle)
        raise


def _read_lifecycle(descriptor: int, probe_uuid: UUID) -> dict:
    payload = _read_file(descriptor, _RUNTIME_LIFECYCLE, 4096)
    lifecycle = json.loads(payload)
    _require(type(lifecycle) is dict and set(lifecycle) == {"schema", "binding", "dispatch", "removal"})
    _require(lifecycle["schema"] == "airplay-linux/probe-runtime-lifecycle/v1"
             and type(lifecycle["dispatch"]) is str
             and lifecycle["dispatch"] in {"allocated", "dispatching", "no_child", "complete"}
             and _binding_bytes(lifecycle) == payload)
    _validate_runtime_binding(lifecycle["binding"], probe_uuid)
    removal = lifecycle["removal"]
    if removal is not None:
        _require(type(removal) is dict and set(removal) == {"step", "legs"}
                 and type(removal["step"]) is int and 0 <= removal["step"] <= 4
                 and type(removal["legs"]) is dict and set(removal["legs"]) <= {"positive", "negative"})
        for pin in removal["legs"].values():
            _require(type(pin) is list and len(pin) == 2 and type(pin[0]) is int
                     and pin[0] >= 0 and type(pin[1]) is int and pin[1] > 0)
    return lifecycle


def _pin(descriptor: int) -> list[int]:
    info = os.fstat(descriptor)
    _private(info)
    return [info.st_dev, info.st_ino]


def _missing(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _remove_empty(parent: int, name: str, pin: list[int] | None) -> None:
    """Finish a previously authorised removal, including its directory sync."""
    try:
        entered = os.open(name, _DIR_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        pass
    else:
        try:
            _require(pin is not None and _pin(entered) == pin and not os.listdir(entered))
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _require([current.st_dev, current.st_ino] == pin)
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(entered)
    # An absent entry may be the result of a crash just after rmdir. Sync it on
    # every retry; ENOENT never bypasses durability or permits another entry.
    os.fsync(parent)


def _retire_empty_probe(root: Path, probe_uuid: UUID, descriptor: int) -> None:
    """Caller holds the probe-parent lock; preserve every remaining artifact."""
    _same_root(root, probe_uuid, descriptor)
    if os.listdir(descriptor):
        return
    os.fsync(descriptor)
    parent = _open_directory(root.parent)
    try:
        _private(os.fstat(parent))
        _remove_empty(parent, root.name, _pin(descriptor))
    finally:
        os.close(parent)


def _prepare_removal(record_root: Path, lifecycle: dict, descriptor: int) -> None:
    """Persist exact empty-directory pins BEFORE the first destructive step."""
    _same_runtime(lifecycle["binding"], descriptor)
    _require(set(os.listdir(descriptor)) <= {"positive", "negative"})
    legs = {}
    for leg in os.listdir(descriptor):
        entered = os.open(leg, _DIR_FLAGS, dir_fd=descriptor)
        try:
            legs[leg] = _pin(entered)
            _require(not os.listdir(entered))
        finally:
            os.close(entered)
    lifecycle["removal"] = dict(step=0, legs=legs)
    _write_lifecycle(record_root, lifecycle)


def _remove_runtime(record_root: Path, lifecycle: dict, *, undispatched: bool = False) -> None:
    """Replay durable removal intent; never reopen a directory already removed.

    step identifies the NEXT removal: positive, negative, UUID root, outer
    parent. Every step is already durable before rmdir and advances only after
    fsync. Missing entries at that step are retried as absence, not failures.
    """
    binding, removal = lifecycle["binding"], lifecycle["removal"]
    _require(removal is not None)
    root = Path(binding["runtime_root"])

    def advance(step):
        removal["step"] = step
        try:
            _write_lifecycle(record_root, lifecycle)
        except (OSError, StorageError):
            # Before dispatch, this context still owns the empty allocation.
            # A journal outage cannot require leaking it. Keep attempting each
            # progress publication so an interrupted reclaim remains recoverable.
            if not undispatched:
                raise

    temporary = _open_directory(root.parent.parent)
    parent = descriptor = None
    try:
        try:
            parent = os.open(root.parent.name, _DIR_FLAGS, dir_fd=temporary)
        except FileNotFoundError:
            _require(removal["step"] >= 3)
            os.fsync(temporary)
            advance(4)
        else:
            _require(_pin(parent) == [binding["parent_device"], binding["parent_inode"]]
                     and removal["step"] < 4)
            if removal["step"] <= 2:
                try:
                    descriptor = os.open(root.name, _DIR_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    _require(removal["step"] == 2)
                else:
                    _require(_pin(descriptor) == [binding["runtime_device"], binding["runtime_inode"]])
                    for step, leg in enumerate(("positive", "negative")):
                        if removal["step"] <= step:
                            _same_runtime(binding, descriptor)
                            _remove_empty(descriptor, leg, removal["legs"].get(leg))
                            advance(step + 1)
                        else:
                            _require(_missing(descriptor, leg))
                _remove_empty(parent, root.name, [binding["runtime_device"], binding["runtime_inode"]])
                advance(3)
            else:
                _require(_missing(parent, root.name))
                os.fsync(parent)
            _remove_empty(temporary, root.parent.name, [binding["parent_device"], binding["parent_inode"]])
            advance(4)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)
        os.close(temporary)
    # The journal is deleted LAST: if obligation deletion or its fsync fails,
    # a fresh instance can still finish from the durable parent-removed step.
    files = AtomicFiles(record_root)
    files.delete(_RUNTIME_OBLIGATION)
    files.delete(_RUNTIME_LIFECYCLE)


def _reclaim_undispatched(record_root: Path | None, allocation: _RuntimeAllocation) -> None:
    """Publication failed before dispatch: the pinned allocation is still ours."""
    binding = _runtime_binding(allocation.root, allocation.descriptor)
    lifecycle = _new_lifecycle(binding)
    if record_root is None:
        raise _Refused()
    try:
        _prepare_removal(record_root, lifecycle, allocation.descriptor)
    except (OSError, StorageError):
        # Only a failed publication after all pin/emptiness checks can use the
        # in-memory intent. No child can exist: retention has not transferred
        # and the caller has not crossed its first dispatch/await boundary.
        if lifecycle["removal"] is None:
            raise
    _remove_runtime(record_root, lifecycle, undispatched=True)
    probe_uuid = UUID(record_root.name)
    descriptor = open_probe_root(record_root, probe_uuid)
    try:
        _retire_empty_probe(record_root, probe_uuid, descriptor)
    finally:
        os.close(descriptor)


def _recover_runtime(record_root: Path, lifecycle: dict) -> None:
    """Prove exact absence before unlinking; failed receipts grant no signals.

    Recovery runs outside the old user/network namespace. Directory ownership
    is therefore checked against this parent's UID; receipt identity and entry
    pins remain unchanged. Unreadable/live children or supervisors retain all
    evidence and block another witness, including an uncertain spawn gap.
    """
    if lifecycle["removal"] is not None:
        _remove_runtime(record_root, lifecycle)
        return
    binding = lifecycle["binding"]
    root = Path(binding["runtime_root"])
    descriptor = open_probe_root(root, UUID(root.name))
    try:
        _same_runtime(binding, descriptor)
        if lifecycle["dispatch"] in {"allocated", "no_child", "complete"}:
            _prepare_removal(record_root, lifecycle, descriptor)
            _remove_runtime(record_root, lifecycle)
            return
        receipts = _lifecycle_all(_ProbeFiles(record_root), LAUNCH_RECEIPT_DIRECTORY, decode_launch_receipt)
        _require(bool(receipts) and len(receipts) <= 2)
        legs = [Path(receipt.runtime_directory).name for receipt in receipts]
        _require(len(set(legs)) == len(legs) and "positive" in legs and set(legs) <= {"positive", "negative"})
        io = EndpointObservations()

        def absent():
            for receipt in receipts:
                _require(receipt.boot_id == io.boot_id() and receipt.daemon is not None
                         and receipt.supervisor is not None and receipt.app == receipt.supervisor
                         and receipt.owner_uid in {0, os.getuid()})
                for identity in (receipt.supervisor, receipt.daemon):
                    found = io.identity(identity.pid)
                    _require(found.state is IdentityState.absent or (
                        found.state is IdentityState.terminated and found.identity == identity
                        and found.uid == os.getuid()))

        absent()
        for receipt, leg in zip(receipts, legs, strict=True):
            _require(receipt.runtime_directory == str(root / leg) and receipt.socket_name == "daemon.sock")
            entered = os.open(leg, _DIR_FLAGS, dir_fd=descriptor)
            try:
                info = os.fstat(entered)
                _private(info)
                _require((info.st_dev, info.st_ino) == (receipt.runtime_device, receipt.runtime_inode))
                path = receipt.runtime_directory + "/" + receipt.socket_name
                _require(io.unix_inodes(path) == ())
                try:
                    entry = io.stat(receipt.socket_name, dir_fd=entered, follow_symlinks=False)
                except FileNotFoundError:
                    io.fsync(entered)
                    continue
                _require(stat.S_ISSOCK(entry.st_mode) and entry.st_uid == os.getuid()
                         and (entry.st_dev, entry.st_ino) == (receipt.entry_device, receipt.entry_inode))
                try:
                    io.peer(entered, receipt.socket_name)
                except OSError as error:
                    _require(error.errno in {errno.ECONNREFUSED, errno.ENOENT})
                else:
                    raise _Refused()
                absent()
                current = io.stat(receipt.socket_name, dir_fd=entered, follow_symlinks=False)
                _require((current.st_dev, current.st_ino) == (entry.st_dev, entry.st_ino))
                _same_runtime(binding, descriptor)
                io.unlink(receipt.socket_name, dir_fd=entered)
                io.fsync(entered)
            finally:
                os.close(entered)
        absent()
        _prepare_removal(record_root, lifecycle, descriptor)
        _remove_runtime(record_root, lifecycle)
    finally:
        os.close(descriptor)


def _read_file(directory: int, name: str, limit: int) -> bytes:
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.getuid()
                 and type(before.st_size) is int and 1 <= before.st_size <= limit)
        chunks = []
        count = 0
        while count <= limit:
            data = os.read(descriptor, limit + 1 - count)
            if not data:
                break
            chunks.append(data)
            count += len(data)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        _require(count == before.st_size == after.st_size and count <= limit)
        _require((before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns))
        _require((after.st_dev, after.st_ino) == (entry.st_dev, entry.st_ino)
                 and stat.S_ISREG(entry.st_mode) and entry.st_uid == os.getuid())
        return payload
    finally:
        os.close(descriptor)


def read_probe_verdict(
    root: Path, probe_uuid: UUID, descriptor: int, output: CommandOutput,
) -> Result[ProbeVerdict]:
    """No stdout fallback, including on a nonzero or incomplete child result."""
    try:
        _require(type(output.exit_code) is int and output.exit_code == 0)
        _same_root(root, probe_uuid, descriptor)
        payload = _read_file(descriptor, "verdict.json", 4096)
        digest = _read_file(descriptor, "verdict.sha256", 65)
        _require(re.fullmatch(rb"[0-9a-f]{64}\n", digest) is not None)
        _require(hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n" == digest)
        verdict = decode_probe_verdict(payload)
        _same_root(root, probe_uuid, descriptor)
        return Ok(value=verdict)
    except (OSError, RecordError, _Refused, ValueError, TypeError):
        return _err()


class _ProbeFiles(AtomicFiles):
    """Reuse canonical stores with a child-only, flat private receipt directory."""

    @staticmethod
    def _local(relative: str) -> str:
        if relative == "daemon/receipts" or relative.startswith("daemon/receipts/"):
            return relative.removeprefix("daemon/")
        if relative == "daemon/cleanup-intents" or relative.startswith("daemon/cleanup-intents/"):
            return relative.removeprefix("daemon/")
        raise StorageError("path")

    def names(self, relative: str) -> tuple[str, ...]:
        return super().names(self._local(relative))

    def read(self, relative: str) -> bytes | None:
        return super().read(self._local(relative))

    def write(self, relative: str, payload: bytes) -> None:
        super().write(self._local(relative), payload)


def prepare_config(
    record_root: Path, *, backend_executable: str, supervisor: ProcessIdentity, boot_id: str,
    runtime_root: Path | None = None,
) -> ProbeConfig:
    """Create UUID-bound socket legs and durable leg file directories in the child."""
    probe_uuid = UUID(record_root.name)
    runtime_root = record_root if runtime_root is None else runtime_root
    descriptor = open_probe_root(record_root, probe_uuid)
    reservations = []
    instance = InstanceId(uuid4())
    try:
        runtime_descriptor = open_probe_root(runtime_root, probe_uuid)
        try:
            for leg in ("positive", "negative"):
                os.mkdir(leg, 0o700, dir_fd=runtime_descriptor)
                if runtime_root != record_root:
                    os.mkdir(leg, 0o700, dir_fd=descriptor)
                entered = os.open(leg, _DIR_FLAGS, dir_fd=runtime_descriptor)
                try:
                    info = os.fstat(entered)
                    _private(info)
                    reservation = DaemonReservation(
                        schema_version=1, launch_uuid=uuid4(), instance_uuid=instance,
                        app=supervisor, owner_uid=os.getuid(), boot_id=boot_id,
                        runtime_directory=str(runtime_root / leg), runtime_device=info.st_dev,
                        runtime_inode=info.st_ino, endpoint_generation=1, socket_name="daemon.sock",
                        protocol_version="v1",
                    )
                    encode_daemon_reservation(reservation)
                    reservations.append(reservation)
                finally:
                    os.close(entered)
            os.fsync(runtime_descriptor)
            os.fsync(descriptor)
        finally:
            os.close(runtime_descriptor)
    finally:
        os.close(descriptor)
    return ProbeConfig(probe_uuid=probe_uuid, reservations=tuple(reservations),
                       backend_executable=backend_executable, record_root=record_root)


def _validate_config(config: ProbeConfig, supervisor: ProcessIdentity) -> None:
    descriptor = open_probe_root(config.record_root, config.probe_uuid)
    os.close(descriptor)
    _require(type(config.reservations) is tuple and len(config.reservations) == 2)
    _require(type(config.backend_executable) is str and bool(config.backend_executable)
             and "\0" not in config.backend_executable)
    positive, negative = config.reservations
    _require(positive.launch_uuid != negative.launch_uuid)
    runtime_root = Path(positive.runtime_directory).parent
    descriptor = open_probe_root(runtime_root, config.probe_uuid)
    os.close(descriptor)
    for reservation, leg in zip(config.reservations, ("positive", "negative"), strict=True):
        encode_daemon_reservation(reservation)
        _require(reservation.runtime_directory == str(runtime_root / leg)
                 and reservation.app == supervisor and reservation.owner_uid == os.getuid()
                 and reservation.socket_name == "daemon.sock")
        _require(len(os.fsencode(reservation.runtime_directory + "/daemon.sock")) < 108,
                 ErrorCode.dependency_incompatible)
        entered = _open_directory(Path(reservation.runtime_directory))
        try:
            info = os.fstat(entered)
            _private(info)
            _require((info.st_dev, info.st_ino) == (reservation.runtime_device, reservation.runtime_inode))
        finally:
            os.close(entered)
        entered = _open_directory(config.record_root / leg)
        try:
            _private(os.fstat(entered))
        finally:
            os.close(entered)


def _reserved(reservation: DaemonReservation, now: float) -> LaunchReceipt:
    # Deliberate projection: inherited observation/token fields never enter new
    # lifecycle records, and the reservation remains a separate identity channel.
    return LaunchReceipt(
        schema_version=reservation.schema_version, launch_uuid=reservation.launch_uuid,
        instance_uuid=reservation.instance_uuid, app=reservation.app,
        owner_uid=reservation.owner_uid, boot_id=reservation.boot_id,
        runtime_directory=reservation.runtime_directory, runtime_device=reservation.runtime_device,
        runtime_inode=reservation.runtime_inode, endpoint_generation=reservation.endpoint_generation,
        socket_name=reservation.socket_name, protocol_version=reservation.protocol_version,
        state="reserved", daemon_instance_uuid=None, supervisor=None, daemon=None,
        entry_device=None, entry_inode=None, socket_inode=None, peer_pid=None,
        peer_start_ticks=None, peer_uid=None, status_state=None, credential_service="unknown",
        failure=None, updated_at=now,
    )


@dataclass(frozen=True, kw_only=True)
class _LegOutcome:
    passed: bool
    cleanup_confirmed: bool
    failure: ErrorCode | None


class _Leg:
    def __init__(self, config, leg, process, supervisor, environment):
        self.config, self.leg, self.process = config, leg, process
        self.supervisor, self.env = supervisor, environment
        self.receipt = _reserved(config.reservations[0 if leg == "positive" else 1], environment.now())
        self.files = _ProbeFiles(config.record_root)
        self.store = JsonLaunchReceiptStore(self.files)
        self.intents = JsonDaemonCleanupIntentStore(self.files)
        self.record = None
        self.exits: dict[ProcessIdentity, int] = {}
        self.authority = EvidenceAuthority(
            receipts=self._receipt_source, process=process, evidence_factory=TerminationEvidence,
            observations=environment.observations,
        )
        self.deadline = environment.now() + SOCKET_IDLE_TIMEOUT

    def _receipt_source(self):
        # Synchronous post-pidfd-open revalidation reopens/decodes ALL local
        # receipts. It must never return a cached pre-signal snapshot.
        try:
            return Ok(value=_lifecycle_all(self.files, LAUNCH_RECEIPT_DIRECTORY, decode_launch_receipt))
        except (RecordError, StorageError):
            return _err(ErrorCode.persistence_failed)

    def _completion(self, event: ProcessExit) -> None:
        if type(event) is ProcessExit and type(event.exit_code) is int:
            self.exits[event.identity] = event.exit_code

    async def _write(self, **changes) -> None:
        updated = replace(self.receipt, updated_at=self.env.now(), **changes)
        result = await self.store.write(updated)
        if isinstance(result, Err):
            raise _Refused(ErrorCode.persistence_failed)
        self.receipt = updated

    async def _poll(self, deadline: float) -> None:
        remaining = deadline - self.env.now()
        _require(remaining > 0, ErrorCode.cleanup_timeout)
        await self.env.sleep(min(POLL_INTERVAL, remaining))

    def _socket_present(self) -> bool:
        entered = _open_directory(Path(self.receipt.runtime_directory))
        try:
            try:
                self.env.observations.stat(self.receipt.socket_name, dir_fd=entered, follow_symlinks=False)
                return True
            except FileNotFoundError:
                return False
        finally:
            os.close(entered)

    def _no_credentials_file(self) -> bool:
        # Refuse every unexpected entry in both locations. HOME/XDG files live
        # under the state root so failures retain them after socket-root cleanup.
        runtime = Path(self.receipt.runtime_directory)
        for directory in {runtime, self.config.record_root / self.leg}:
            entered = _open_directory(directory)
            try:
                _private(os.fstat(entered))
                allowed = {self.receipt.socket_name} if directory == runtime else set()
                if not set(os.listdir(entered)) <= allowed:
                    return False
            finally:
                os.close(entered)
        return True

    async def _bind(self) -> bool:
        history = self._receipt_source()
        if isinstance(history, Err):
            raise _Refused(history.error.code)
        matching = tuple(receipt for receipt in history.value
                         if receipt.runtime_directory == self.receipt.runtime_directory
                         and receipt.socket_name == self.receipt.socket_name)
        _require(bool(matching))
        generation = max(receipt.endpoint_generation for receipt in matching)
        _require(sum(receipt.endpoint_generation == generation
                     and receipt.state not in {"failed", "stopped"} for receipt in matching) == 1
                 and not any(receipt.endpoint_generation < generation
                             and receipt.state not in {"failed", "stopped"} for receipt in matching))
        proof = verify_owned_endpoint(self.receipt, current_generation=generation)
        if isinstance(proof, Err):
            return False
        endpoint = proof.value
        await self._write(
            state="bound", entry_device=endpoint.socket_entry[0], entry_inode=endpoint.socket_entry[1],
            socket_inode=endpoint.socket_inode, peer_pid=endpoint.daemon.pid,
            peer_start_ticks=endpoint.daemon.start_ticks, peer_uid=endpoint.peer_uid,
        )
        return True

    async def _idle(self, deadline: float) -> bool:
        _require(self.env.now() < deadline, ErrorCode.cleanup_timeout)
        proof = await self.authority.prove(self.record)
        if isinstance(proof, Err):
            raise _Refused(proof.error.code)
        output = await self.process.run(CommandSpec(
            argv=("doubletake-ctl", "-socket", self.record.socket_path, "status"), env={},
        ), timeout=deadline - self.env.now())
        _require(self.env.now() < deadline, ErrorCode.cleanup_timeout)
        if isinstance(output, Err):
            return False
        _require(type(output.value.exit_code) is int and output.value.exit_code == 0,
                 ErrorCode.daemon_failed)
        # decode_status is the delivered public contract, never the application
        # owner-handshake fixture. The free-form output never leaves this scope.
        import json
        try:
            raw = json.loads(output.value.stdout)
        except (ValueError, UnicodeError):
            raise _Refused(ErrorCode.dependency_incompatible) from None
        status = decode_status(raw)
        if isinstance(status, Err):
            raise _Refused(status.error.code)
        confirmed = await self.authority.confirm(self.record, proof.value)
        if isinstance(confirmed, Err):
            raise _Refused(confirmed.error.code)
        return status.state == "idle"

    async def _positive(self) -> None:
        while self.env.now() < self.deadline:
            _require(self.record.daemon not in self.exits, ErrorCode.daemon_failed)
            if self.receipt.state == "detached":
                if not await self._bind():
                    await self._poll(self.deadline)
                    continue
            if await self._idle(self.deadline):
                break
            await self._poll(self.deadline)
        else:
            raise _Refused(ErrorCode.cleanup_timeout)
        await self._write(state="ready", status_state="idle", credential_service="keyring_proven")
        # This is a second observation after the full stability interval. Its
        # budget is independent of, and cannot replenish, socket+initial idle.
        await self.env.sleep(IDLE_CONFIRMATION)
        _require(await self._idle(self.env.now() + SOCKET_IDLE_TIMEOUT), ErrorCode.keyring_unavailable)
        _require(self.record.daemon not in self.exits, ErrorCode.daemon_failed)
        _require(self._no_credentials_file(), ErrorCode.keyring_unavailable)

    async def _negative(self) -> None:
        while self.record.daemon not in self.exits:
            await self._poll(self.deadline)
        _require(self.exits[self.record.daemon] != 0, ErrorCode.keyring_unavailable)
        _require(not self._socket_present() and self._no_credentials_file(), ErrorCode.keyring_unavailable)
        identity = self.env.observations.identity(self.record.daemon.pid)
        _require(identity.state is IdentityState.absent or (
            identity.state is IdentityState.terminated and identity.identity == self.record.daemon
            and identity.uid == self.receipt.owner_uid))

    async def _finish(self, failure: ErrorCode | None) -> _LegOutcome:
        cleanup_failure = None
        if self.record is not None:
            try:
                # If a socket appeared, pre-bind evidence must not silently
                # authorize a signal after a failed endpoint proof.
                if self.receipt.entry_inode is None and self._socket_present():
                    _require(await self._bind())
                await self._write(state="stopping")
                released = await self.authority.release(self.record)
                if isinstance(released, Err):
                    raise _Refused(released.error.code)
            except (_Refused, OSError, StorageError) as error:
                cleanup_failure = error.code if isinstance(error, _Refused) else ErrorCode.cleanup_timeout
        terminal_failure = cleanup_failure or failure
        try:
            if terminal_failure is not None:
                intent = DaemonCleanupIntent(
                    launch_uuid=self.receipt.launch_uuid, reason=terminal_failure,
                    intent_at=self.env.now(), completed_at=None, outcome=None,
                )
                _require(isinstance(await self.intents.write(intent), Ok), ErrorCode.persistence_failed)
                _require(isinstance(await self.intents.write(replace(
                    intent, completed_at=self.env.now(), outcome=cleanup_failure or "stopped",
                )), Ok), ErrorCode.persistence_failed)
            await self._write(state="failed" if terminal_failure else "stopped", failure=terminal_failure)
        except (_Refused, OSError, StorageError):
            cleanup_failure = ErrorCode.persistence_failed
        return _LegOutcome(passed=terminal_failure is None and cleanup_failure is None,
                           cleanup_confirmed=cleanup_failure is None,
                           failure=cleanup_failure or terminal_failure)

    async def run(self) -> _LegOutcome:
        subscription = self.process.subscribe(self._completion)
        failure = None
        try:
            await self._write()
            directory = self.receipt.runtime_directory
            files_directory = str(self.config.record_root / self.leg)
            overlay = {
                OWNER_TOKEN_ENV: str(self.receipt.launch_uuid), RUNTIME_DIRECTORY_ENV: directory,
                "HOME": files_directory, "XDG_CONFIG_HOME": files_directory,
                "XDG_DATA_HOME": files_directory, "XDG_CACHE_HOME": files_directory,
            }
            if self.leg == "negative":
                overlay["DBUS_SESSION_BUS_ADDRESS"] = UNREACHABLE_BUS
            # This durable child-side gate precedes EVERY backend dispatch.
            # Only an observed outer completion/exec refusal plus its confirmed
            # absence can prove that no backend child was ever dispatched.
            AtomicFiles(self.config.record_root).write(_BACKEND_DISPATCH, _binding_bytes({
                "schema": "airplay-linux/probe-backend-dispatch/v1",
                "probe_uuid": str(self.config.probe_uuid),
            }))
            result = await self.process.spawn(CommandSpec(
                argv=(self.config.backend_executable, "-daemonize", "-socket",
                      directory + "/" + self.receipt.socket_name, "-cred-backend", "keyring"),
                env=overlay,
            ))
            if isinstance(result, Err):
                raise _Refused(result.error.code)
            _require(type(result.value) is ProcessIdentity)
            self.record = DaemonRecord(
                schema_version=1, instance_uuid=InstanceId(uuid4()), owner_token="unused",
                app=self.receipt.app, supervisor=self.supervisor, daemon=result.value,
                protocol_version=self.receipt.protocol_version,
                socket_path=directory + "/" + self.receipt.socket_name,
                lifecycle="starting", runtime_directory=directory,
            )
            await self._write(state="detached", daemon_instance_uuid=self.record.instance_uuid,
                              supervisor=self.supervisor, daemon=result.value)
            if self.leg == "positive":
                await self._positive()
            else:
                await self._negative()
        except (_Refused, OSError, RecordError, StorageError) as error:
            failure = error.code if isinstance(error, _Refused) else ErrorCode.persistence_failed
        try:
            return await self._finish(failure)
        finally:
            subscription.close()


async def run_backend_leg(
    config: ProbeConfig, leg: Literal["positive", "negative"], *,
    process: ProcessPort, supervisor: ProcessIdentity,
) -> _LegOutcome:
    """The same direct-launch helper used by the complete witness and controls."""
    _require(leg in {"positive", "negative"})
    _validate_config(config, supervisor)
    return await _Leg(config, leg, process, supervisor, _witness_environment()).run()


def _verdict(service="not_run", positive="not_run", negative="not_run", failure=None) -> ProbeVerdict:
    return ProbeVerdict(schema="airplay-linux/credential-service-proof", version=1,
                        service_leg=service, backend_positive_leg=positive,
                        backend_negative_leg=negative, failure=failure)


async def _witness(config, process, namespace) -> tuple[ProbeVerdict, bool]:
    try:
        isolated = namespace(("unshare", "-rn"))
        _require(isinstance(isolated, Ok) and type(isolated.value) is ProcessIdentity,
                 ErrorCode.dependency_incompatible)
        _validate_config(config, isolated.value)
        environment = _witness_environment()
        service = await environment.service()
        if service is not True:
            return _verdict(service="failed", failure=ErrorCode.keyring_unavailable), True
        positive = await _Leg(config, "positive", process, isolated.value, environment).run()
        if not positive.cleanup_confirmed:
            return _verdict(service="passed", positive="failed", failure=positive.failure), False
        negative = await _Leg(config, "negative", process, isolated.value, environment).run()
        return _verdict(
            service="passed", positive="passed" if positive.passed else "failed",
            negative="passed" if negative.passed else "failed", failure=positive.failure or negative.failure,
        ), negative.cleanup_confirmed
    except (_Refused, OSError, StorageError, RecordError, ValueError, TypeError) as error:
        code = error.code if isinstance(error, _Refused) else ErrorCode.persistence_failed
        return _verdict(failure=code), False


def run(
    config: ProbeConfig, *, process: ProcessPort,
    namespace: Callable[[tuple[str, ...]], Result[ProcessIdentity]],
) -> ProbeVerdict:
    """Run inside one isolated child (or its test thread), then publish files.

    ``namespace`` checks the current user/network isolation and supplies the
    exact direct-child supervisor identity; it receives the required namespace
    invocation prefix. Both backend legs use ``process`` without a nested runner.
    """
    verdict, complete = asyncio.run(_witness(config, process, namespace))
    if complete:
        try:
            descriptor = open_probe_root(config.record_root, config.probe_uuid)
            try:
                payload = encode_probe_verdict(verdict)
                files = AtomicFiles(config.record_root)
                _same_root(config.record_root, config.probe_uuid, descriptor)
                files.write("verdict.json", payload)
                files.write("verdict.sha256", hashlib.sha256(payload).hexdigest().encode("ascii") + b"\n")
                _same_root(config.record_root, config.probe_uuid, descriptor)
            finally:
                os.close(descriptor)
        except (OSError, StorageError, RecordError, _Refused):
            return _verdict(failure=ErrorCode.persistence_failed)
    return verdict


class CredentialServiceProbe:
    """Parent-side one-shot orchestration, constructed inertly during assembly."""

    def __init__(self, *, process: ProcessPort, record_root: Path, unshare: str | None) -> None:
        self._process, self._record_root, self._unshare = process, record_root, unshare
        self._task: asyncio.Task[Result[CredentialProofVerdict]] | None = None

    async def check(self) -> Result[CredentialProofVerdict]:
        if self._task is None:
            self._task = asyncio.create_task(self._check())
        return await asyncio.shield(self._task)

    def _lock_probes(self) -> int:
        # Lock the parent BEFORE mkdir, and retain the shared open description
        # through dispatch/cleanup. Recovery cannot mistake a paused creator's
        # fresh empty UUID directory for a discharged one. No lock file remains.
        with AtomicFiles(self._record_root)._directory(("daemon", "probe"), create=True) as located:
            _require(located.parent is not None and not located.missing)
            try:
                fcntl.flock(located.parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _Refused() from None
            return os.dup(located.parent)

    def _create_root(self, probe_uuid: UUID) -> Path:
        files = AtomicFiles(self._record_root)
        with files._directory(("daemon", "probe"), create=True) as located:
            _require(located.parent is not None and not located.missing)
            os.mkdir(str(probe_uuid), 0o700, dir_fd=located.parent)
            os.fsync(located.parent)
        return self._record_root / "daemon" / "probe" / str(probe_uuid)

    def _archive(self, root: Path, probe_uuid: UUID, descriptor: int, runtime_root: Path) -> None:
        _same_root(root, probe_uuid, descriptor)
        entered = os.open("receipts", _DIR_FLAGS, dir_fd=descriptor)
        try:
            _private(os.fstat(entered))
            names = os.listdir(entered)
            _require(len(names) == 2)
            records = []
            payloads = []
            for name in sorted(names):
                payload = _read_file(entered, name, 65536)
                receipt = decode_launch_receipt(payload)
                _require(name == str(receipt.launch_uuid) + ".json" and receipt.state == "stopped"
                         and receipt.failure is None)
                records.append(receipt)
                payloads.append((name, payload))
            _require(records[0].launch_uuid != records[1].launch_uuid)
            by_leg = {Path(record.runtime_directory).name: record for record in records}
            _require(set(by_leg) == {"positive", "negative"})
            for leg, receipt in by_leg.items():
                _require(receipt.runtime_directory == str(runtime_root / leg) and receipt.socket_name == "daemon.sock"
                         and receipt.daemon is not None and receipt.supervisor is not None)
            _require(by_leg["positive"].status_state == "idle"
                     and by_leg["positive"].credential_service == "keyring_proven"
                     and by_leg["positive"].socket_inode is not None
                     and by_leg["negative"].socket_inode is None)
        finally:
            os.close(entered)
        # Publication is complete only after every atomic write has confirmed
        # both file and containing-directory durability. Failure leaves originals.
        destination = f"archives/probe/{probe_uuid}"
        archive = AtomicFiles(self._record_root)
        for name, payload in payloads:
            archive.write(f"{destination}/receipts/{name}", payload)
        archive.write(f"{destination}/verdict.json", _read_file(descriptor, "verdict.json", 4096))
        archive.write(f"{destination}/verdict.sha256", _read_file(descriptor, "verdict.sha256", 65))
        _same_root(root, probe_uuid, descriptor)
        parent = _open_directory(root.parent)
        try:
            # shutil's fd-based implementation refuses symlink replacements.
            current = os.stat(root.name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(descriptor)
            _require((current.st_dev, current.st_ino) == (pinned.st_dev, pinned.st_ino))
            shutil.rmtree(root.name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def _recover_pending(self) -> None:
        files = AtomicFiles(self._record_root)
        for name in files.names("daemon/probe"):
            probe_uuid = UUID(name)
            root = self._record_root / "daemon/probe" / name
            descriptor = open_probe_root(root, probe_uuid)
            try:
                try:
                    lifecycle = _read_lifecycle(descriptor, probe_uuid)
                except FileNotFoundError:
                    try:
                        binding = _read_runtime_binding(descriptor, probe_uuid)
                    except FileNotFoundError:
                        # Finish a journal deletion that was visible but whose
                        # containing-directory sync failed on the last attempt.
                        os.fsync(descriptor)
                        _retire_empty_probe(root, probe_uuid, descriptor)
                        continue
                    # A-2 obligations have no dispatch journal: missing receipts
                    # are NEVER absence evidence for this legacy/uncertain case.
                    lifecycle = _new_lifecycle(binding, "dispatching")
                else:
                    try:
                        binding = _read_runtime_binding(descriptor, probe_uuid)
                    except FileNotFoundError:
                        pass  # Allocation publication or final discharge gap.
                    else:
                        _require(binding == lifecycle["binding"])
                _recover_runtime(root, lifecycle)
                _same_root(root, probe_uuid, descriptor)
                _retire_empty_probe(root, probe_uuid, descriptor)
            finally:
                os.close(descriptor)

    async def _check(self) -> Result[CredentialProofVerdict]:
        descriptor = lock_descriptor = None
        try:
            _require(type(self._unshare) is str and bool(self._unshare), ErrorCode.dependency_incompatible)
            lock_descriptor = self._lock_probes()
            # Also confirm a prior retirement whose parent fsync was interrupted.
            os.fsync(lock_descriptor)
            self._recover_pending()
            probe_uuid = uuid4()
            root = self._create_root(probe_uuid)
            descriptor = open_probe_root(root, probe_uuid)
            with _short_runtime_root(probe_uuid, root) as allocation:
                runtime_root, runtime_descriptor = allocation.root, allocation.descriptor
                socket_path = str(runtime_root / "positive" / "daemon.sock")
                _require(len(os.fsencode(socket_path)) < 108, ErrorCode.dependency_incompatible)
                binding = _runtime_binding(runtime_root, runtime_descriptor)
                lifecycle = _new_lifecycle(binding)
                _same_root(root, probe_uuid, descriptor)
                # Allocation is recoverable even if obligation publication fails.
                # Retention transfers only after BOTH files and dispatch intent
                # have confirmed durability. No await/dispatch precedes this.
                _write_lifecycle(root, lifecycle)
                AtomicFiles(root).write(_RUNTIME_OBLIGATION, _binding_bytes(binding))
                lifecycle["dispatch"] = "dispatching"
                _write_lifecycle(root, lifecycle)
                allocation.retained = True
                result = await self._process.run(CommandSpec(argv=(
                    self._unshare, "-rn", sys.executable, "-m", "airplay_linux.process.credential_probe",
                    "--socket", socket_path, "--record-root", str(root),
                    "--parent-namespaces", json.dumps(_self_namespace_pins()),
                ), env={}), timeout=30.0)
                _same_runtime(binding, runtime_descriptor)
                completed = isinstance(result, Ok) and type(result.value.exit_code) is int
                not_started = isinstance(result, Err) and result.error.code is ErrorCode.dependency_missing
                if completed or not_started:
                    _same_root(root, probe_uuid, descriptor)
                    if AtomicFiles(root).read(_BACKEND_DISPATCH) is None:
                        # The outer process is finished (or exec never began),
                        # AND the mandatory child dispatch gate was never crossed.
                        # A missing receipt by itself never reaches this branch.
                        receipts = _lifecycle_all(_ProbeFiles(root), LAUNCH_RECEIPT_DIRECTORY, decode_launch_receipt)
                        _require(all(receipt.daemon is None for receipt in receipts))
                        _record_completion(root, lifecycle, "no_child")
                if isinstance(result, Err):
                    if lifecycle["dispatch"] == "no_child":
                        _prepare_removal(root, lifecycle, runtime_descriptor)
                        _remove_runtime(root, lifecycle)
                        _retire_empty_probe(root, probe_uuid, descriptor)
                    return result
                decoded = read_probe_verdict(root, probe_uuid, descriptor, result.value)
                if isinstance(decoded, Err):
                    if lifecycle["dispatch"] == "no_child":
                        _prepare_removal(root, lifecycle, runtime_descriptor)
                        _remove_runtime(root, lifecycle)
                        _retire_empty_probe(root, probe_uuid, descriptor)
                    return decoded
                verdict = decoded.value
                _record_completion(root, lifecycle, "complete")
                _prepare_removal(root, lifecycle, runtime_descriptor)
                _remove_runtime(root, lifecycle)
                _retire_empty_probe(root, probe_uuid, descriptor)
                proof = CredentialProofVerdict(
                    service_leg=verdict.service_leg, backend_positive_leg=verdict.backend_positive_leg,
                    backend_negative_leg=verdict.backend_negative_leg, failure=verdict.failure,
                )
                if proof.proven:
                    self._archive(root, probe_uuid, descriptor, runtime_root)
                return Ok(value=proof)
        except (OSError, StorageError, RecordError, _Refused, ValueError, TypeError) as error:
            return _err(error.code if isinstance(error, _Refused) else ErrorCode.persistence_failed)
        finally:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                if lock_descriptor is not None:
                    os.close(lock_descriptor)


_NAMESPACE_ARGV: Final[tuple[str, str]] = ("unshare", "-rn")
_NAMESPACE_KINDS: Final[tuple[str, str]] = ("user", "net")


def _namespace_pin(name: str) -> tuple[int, int]:
    """The ``(device, inode)`` pair that names one namespace of this process."""
    info = os.stat(f"/proc/self/ns/{name}")
    _require(info.st_ino > 0)
    return (info.st_dev, info.st_ino)


def _self_namespace_pins() -> list[list[int]]:
    """The spawning process's own namespace pins, in :data:`_NAMESPACE_KINDS` order.

    Read before dispatch: the child can never read these for itself, because
    entering a user namespace moves the caller below the target's namespace for
    ptrace purposes and the kernel then refuses ``/proc/<parent>/ns/*``.
    """
    return [
        [device, inode]
        for device, inode in (_namespace_pin(name) for name in _NAMESPACE_KINDS)
    ]


def _parent_namespaces(raw: str | None) -> tuple[tuple[int, int], ...] | None:
    """Decode the pins the spawning process recorded, or ``None`` if unusable."""
    if type(raw) is not str:
        return None
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if type(decoded) is not list or len(decoded) != len(_NAMESPACE_KINDS):
        return None
    pins: list[tuple[int, int]] = []
    for entry in decoded:
        if type(entry) is not list or len(entry) != 2:
            return None
        if any(type(part) is not int or part < 0 for part in entry):
            return None
        pins.append((entry[0], entry[1]))
    return tuple(pins)


def _isolation_proven(
    argv: tuple[str, ...], uid: int,
    expected: tuple[tuple[int, int], ...] | None,
    observed: tuple[tuple[int, int], ...] | None,
) -> bool:
    """Whether the witness's own namespace pins differ from its spawner's.

    Every input is evidence, so nothing truthy is accepted in a wrong shape and
    missing evidence can never read as isolation. Both user and network
    namespaces must differ: a private user namespace alone would leave the
    witness on the real network, which is exactly what this gate forbids.
    """
    if type(argv) is not tuple or argv != _NAMESPACE_ARGV:
        return False
    if type(uid) is not int or uid != 0:
        return False
    if expected is None or observed is None:
        return False
    if len(expected) != len(_NAMESPACE_KINDS) or len(observed) != len(_NAMESPACE_KINDS):
        return False
    for pin in (*expected, *observed):
        if type(pin) is not tuple or len(pin) != 2:
            return False
        if any(type(part) is not int or part < 0 for part in pin):
            return False
    return all(observed[index] != expected[index] for index in range(len(_NAMESPACE_KINDS)))


def _namespace_identity(
    argv: tuple[str, ...], *, expected: tuple[tuple[int, int], ...] | None = None,
) -> Result[ProcessIdentity]:
    """Prove this witness runs in namespaces the spawning process does not.

    The child cannot read its parent's ``/proc/<pid>/ns/*`` links: entering a
    user namespace puts the caller below the target's namespace for ptrace
    purposes, so the kernel answers ``EACCES`` for exactly the comparison this
    evidence needs. The spawning process therefore records its own pins and
    passes them; this process reads its own pins and requires both to differ.
    That is the same fact, stated with evidence each side can legally obtain.
    Absent or malformed spawner evidence refuses rather than assuming isolation.
    """
    try:
        observed = (
            tuple(_namespace_pin(name) for name in _NAMESPACE_KINDS)
            if argv == _NAMESPACE_ARGV else None
        )
        _require(_isolation_proven(argv, os.getuid(), expected, observed))
        identity = read_identity_evidence(os.getpid())
        _require(identity.state is IdentityState.present and identity.identity is not None)
        return Ok(value=identity.identity)
    except (OSError, _Refused):
        return _err(ErrorCode.dependency_incompatible)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one isolated credential service witness")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--record-root", required=True)
    parser.add_argument("--parent-namespaces", default=None)
    arguments = parser.parse_args(argv)
    try:
        root = Path(arguments.record_root)
        runtime_root = Path(arguments.socket).parent.parent
        _require(arguments.socket == str(runtime_root / "positive" / "daemon.sock"))
        parent_namespaces = _parent_namespaces(arguments.parent_namespaces)
        identity = _namespace_identity(_NAMESPACE_ARGV, expected=parent_namespaces)
        _require(isinstance(identity, Ok))
        backend = shutil.which("doubletake")
        boot = EndpointObservations.boot_id()
        _require(backend is not None and boot is not None, ErrorCode.dependency_incompatible)
        config = prepare_config(root, backend_executable=backend, supervisor=identity.value,
                                boot_id=boot, runtime_root=runtime_root)
        from airplay_linux.process.linux import LinuxProcessRunner
        run(config, process=LinuxProcessRunner(),
            namespace=lambda required: _namespace_identity(required, expected=parent_namespaces))
        descriptor = open_probe_root(root, config.probe_uuid)
        try:
            # A completed failed leg exits zero too, but a partial publication or
            # cleanup uncertainty cannot pass the parent child-exit gate.
            checked = read_probe_verdict(root, config.probe_uuid, descriptor,
                                         CommandOutput(exit_code=0, stdout=b"", stderr=b""))
            return 0 if isinstance(checked, Ok) else 1
        finally:
            os.close(descriptor)
    except (OSError, StorageError, RecordError, _Refused, ValueError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
