"""Receipt-backed endpoint evidence and dormant lifecycle authority.

The filesystem entry and the listening kernel socket have different identities.
The verifier retains the original directory descriptor while it joins both to a
live peer. It sends no application bytes and never cleans up a failed proof.
"""
from __future__ import annotations

import errno
import math
import os
import socket
import stat
import struct
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from airplay_linux.backend.control import (
    BackendFeatures, BackendOperation, DispatchFence, OwnershipProof,
    PublishedDoubletakeProfile, requires_dispatch_fence,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverTarget
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.process.procfs import IdentityState, read_identity_evidence

if TYPE_CHECKING:
    from airplay_linux.domain.ports import ProcessPort
    from airplay_linux.domain.records import (
        DaemonRecord, DaemonReservation, LaunchReceipt, SupervisorReleaseIntent,
    )
    from airplay_linux.process.procfs import TerminationEvidence

# Keep the delivered grace without importing the supervisor's launch machinery.
TERMINATION_GRACE = 3.0
_MAX_READ = 1 << 20
_NONTERMINAL = frozenset({"reserved", "detached", "bound", "ready", "stopping"})
_TERMINAL = frozenset({"failed", "stopped"})
_BOUND_FIELDS = ("entry_device", "entry_inode", "socket_inode", "peer_pid", "peer_start_ticks", "peer_uid")


@dataclass(frozen=True, kw_only=True)
class VerifiedEndpoint:
    daemon: ProcessIdentity = field(repr=False)
    socket_entry: tuple[int, int] = field(repr=False)
    socket_inode: int = field(repr=False)
    peer_uid: int = field(repr=False)
    generation: int = field(repr=False)


class EndpointObservations:
    """Replaceable OS observations, never a substitute verification verdict."""

    open = staticmethod(os.open)
    close = staticmethod(os.close)
    fstat = staticmethod(os.fstat)
    stat = staticmethod(os.stat)
    unlink = staticmethod(os.unlink)
    fsync = staticmethod(os.fsync)
    identity = staticmethod(read_identity_evidence)

    @staticmethod
    def peer(directory: int, name: str) -> tuple[int, int, int]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(1.0)
            # /proc/self/fd dereferences the descriptor we deliberately retained,
            # not the possibly replaced path used to reach it originally.
            connection.connect(f"/proc/self/fd/{directory}/{name}")
            return struct.unpack("3i", connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"),
            ))

    @staticmethod
    def unix_inodes(path: str) -> tuple[int, ...] | None:
        """Inodes of listening sockets bound to exactly ``path``.

        A connected client -- including the verifier's own ``peer`` probe -- is
        listed under the server's path with state 02/03 and either a zero or a
        transient positive inode. Only an unconnected (state 01) row with a
        positive inode is a listening endpoint; a client row must not turn one
        listener into "duplicate rows". Malformed inode evidence still refuses.
        """
        try:
            data = _read(Path("/proc/net/unix"))
            if data is None:
                return None
            rows = []
            for line in data.splitlines()[1:]:
                fields = line.split(None, 7)
                if len(fields) != 8 or fields[7] != os.fsencode(path):
                    continue
                if fields[6] == b"0":
                    continue
                if not fields[6].isdigit() or int(fields[6]) <= 0:
                    return None
                if fields[5] != b"01":
                    continue
                rows.append(int(fields[6]))
            return tuple(rows)
        except OSError:
            return None

    @staticmethod
    def holds_socket(pid: int, inode: int) -> bool | None:
        try:
            # Failure to inspect even one fd leaves the holder set uncertain.
            holders = tuple(os.readlink(entry) for entry in (Path("/proc") / str(pid) / "fd").iterdir())
            return f"socket:[{inode}]" in holders
        except OSError:
            return None

    @staticmethod
    def cmdline(pid: int) -> bytes | None:
        try:
            return _read(Path("/proc") / str(pid) / "cmdline")
        except OSError:
            return None

    @staticmethod
    def boot_id() -> str | None:
        try:
            data = _read(Path("/proc/sys/kernel/random/boot_id"))
            return data.decode("ascii").strip() if data is not None else None
        except (OSError, UnicodeError):
            return None


def _read(path: Path) -> bytes | None:
    with path.open("rb") as stream:
        data = stream.read(_MAX_READ + 1)
    return data if len(data) <= _MAX_READ else None


_observations = EndpointObservations()


def _err(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


class _Refusal(Exception):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code


def _require(condition: bool, code=ErrorCode.ownership_mismatch) -> None:
    if not condition:
        raise _Refusal(code)


def _path(reservation) -> str:
    directory, name = reservation.runtime_directory, reservation.socket_name
    _require(
        type(directory) is str and directory.startswith("/") and "\0" not in directory
        and all(part not in {"", ".", ".."} for part in directory.split("/")[1:])
        and type(name) is str and name not in {"", ".", ".."} and "/" not in name and "\0" not in name,
    )
    path = directory + "/" + name
    _require(len(os.fsencode(path)) < 108)
    return path


@contextmanager
def _anchor(reservation, io) -> Iterator[int]:
    _path(reservation)
    descriptor = None
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = io.open("/", flags)
        for part in reservation.runtime_directory.split("/")[1:]:
            next_descriptor = io.open(part, flags, dir_fd=descriptor)
            io.close(descriptor)
            descriptor = next_descriptor
        _directory(reservation, io, descriptor)
        yield descriptor
    finally:
        if descriptor is not None:
            io.close(descriptor)


def _directory(reservation, io, descriptor) -> None:
    entry = io.fstat(descriptor)
    _require(
        stat.S_ISDIR(entry.st_mode) and stat.S_IMODE(entry.st_mode) == 0o700
        and entry.st_uid == reservation.owner_uid
        and (entry.st_dev, entry.st_ino) == (reservation.runtime_device, reservation.runtime_inode),
    )


def _entry(reservation, io, descriptor) -> tuple[int, int]:
    try:
        entry = io.stat(reservation.socket_name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        raise _Refusal(ErrorCode.stale_socket) from None
    _require(stat.S_ISSOCK(entry.st_mode) and entry.st_uid == reservation.owner_uid, ErrorCode.stale_socket)
    return entry.st_dev, entry.st_ino


def _identity(io, daemon, uid):
    _require(type(daemon) is ProcessIdentity)
    evidence = io.identity(daemon.pid)
    _require(
        evidence.state is IdentityState.present and evidence.identity == daemon
        and evidence.uid == uid,
    )
    return evidence


def _argv(io, daemon, path) -> None:
    raw = io.cmdline(daemon.pid)
    _require(type(raw) is bytes and bool(raw) and raw.endswith(b"\0"))
    argv = raw[:-1].split(b"\0")
    _require(bool(argv[0]) and b"-daemonize" in argv[1:])
    values = []
    for index, argument in enumerate(argv[1:], start=1):
        _require(
            argument != b"--socket"
            and not argument.startswith((b"-socket=", b"--socket=")),
        )
        if argument == b"-socket":
            _require(index + 1 < len(argv))
            values.append(argv[index + 1])
    _require(len(values) == 1 and values[0] == os.fsencode(path))


def _generation(reservation, current_generation, io) -> None:
    _require(
        type(current_generation) is int and current_generation > 0
        and type(reservation.endpoint_generation) is int and reservation.endpoint_generation > 0
        and current_generation == reservation.endpoint_generation,
        ErrorCode.stale_event,
    )
    _require(io.boot_id() == reservation.boot_id and type(reservation.boot_id) is str and bool(reservation.boot_id))


def _bound(reservation, daemon, *, current_generation, io, receipt=None) -> VerifiedEndpoint:
    path = _path(reservation)
    with _anchor(reservation, io) as descriptor:
        before = _entry(reservation, io, descriptor)
        if receipt is not None and receipt.entry_inode is not None:
            _require(before == (receipt.entry_device, receipt.entry_inode), ErrorCode.stale_socket)
        pid, uid, _gid = io.peer(descriptor, reservation.socket_name)
        _require(uid == reservation.owner_uid)
        rows = io.unix_inodes(path)
        _require(rows is not None and len(rows) == 1)
        inode = rows[0]
        _require(type(inode) is int and inode > 0 and io.holds_socket(pid, inode) is True)
        _require(type(daemon) is ProcessIdentity and pid == daemon.pid)
        _identity(io, daemon, reservation.owner_uid)
        if receipt is not None:
            expected = (receipt.peer_pid, receipt.peer_start_ticks, receipt.peer_uid, receipt.socket_inode)
            _require(all(value is None for value in expected) or expected == (pid, daemon.start_ticks, uid, inode))
        _argv(io, daemon, path)
        _identity(io, daemon, reservation.owner_uid)
        _require(_entry(reservation, io, descriptor) == before, ErrorCode.stale_socket)
        _directory(reservation, io, descriptor)
        _generation(reservation, current_generation, io)
        return VerifiedEndpoint(
            daemon=daemon, socket_entry=before, socket_inode=inode,
            peer_uid=uid, generation=current_generation,
        )


def verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]:
    """Read independent peer/entry facts against an externally derived generation."""
    try:
        return Ok(value=_bound(
            receipt, receipt.daemon, current_generation=current_generation,
            io=_observations, receipt=receipt,
        ))
    except _Refusal as refusal:
        return _err(refusal.code)
    except OSError:
        return _err(ErrorCode.ownership_mismatch)


class EvidenceAuthority:
    """Receipt selection and strict teardown, without selecting a public adapter.

    ``receipts`` is a synchronous all-receipts snapshot under the caller's
    lifecycle authority. It must reread durable state on each invocation: the
    post-pidfd-open closure cannot await a repository or use a cached proof.
    Only the supervisor supplies ``supervisor_intent`` for its own release;
    registry recovery reads that document as evidence and never calls this seam
    to re-signal a departed supervisor's daemon.
    """

    def __init__(
        self, *, receipts: Callable[[], Result[tuple[LaunchReceipt, ...]]],
        process: ProcessPort, evidence_factory: Callable[..., TerminationEvidence],
        reservation: DaemonReservation | None = None,
        supervisor_intent: Callable[[], Result[SupervisorReleaseIntent | None]] | None = None,
        observations: EndpointObservations | None = None, features: BackendFeatures | None = None,
    ) -> None:
        self._receipts = receipts
        self._process = process
        self._factory = evidence_factory
        self._reservation = reservation
        self._supervisor_intent = supervisor_intent
        self._io = observations if observations is not None else _observations
        self._features = features if features is not None else PublishedDoubletakeProfile(socket_path="").features

    def allows(self, operation: BackendOperation) -> bool:
        if not isinstance(operation, BackendOperation):
            return False
        if operation in {BackendOperation.status, BackendOperation.discover, BackendOperation.devices}:
            return True
        f = self._features
        if not f.owned_daemon_control:
            return False
        if operation is BackendOperation.disconnect:
            return f.multi_target
        if operation in {BackendOperation.mute, BackendOperation.unmute}:
            return f.multi_target and f.target_mute
        if operation is BackendOperation.connect:
            return f.multi_target and (f.physical_source or f.virtual_source)
        if operation is BackendOperation.shutdown:
            return True
        # This authority represents the public evidence contract. Unsupported
        # trust, audio-restart and mode-query operations retain their false gates.
        return False

    def _select(self, record: DaemonRecord, *, cleanup=False):
        loaded = self._receipts()
        if isinstance(loaded, Err):
            raise _Refusal(loaded.error.code)
        _require(isinstance(loaded, Ok) and type(loaded.value) is tuple)
        group = tuple(receipt for receipt in loaded.value if (
            receipt.runtime_directory == record.runtime_directory
            and receipt.runtime_directory + "/" + receipt.socket_name == record.socket_path
        ))
        if not group:
            return None
        _require(all(type(r.endpoint_generation) is int and r.endpoint_generation > 0 and r.state in _NONTERMINAL | _TERMINAL for r in group))
        maximum = max(receipt.endpoint_generation for receipt in group)
        live = tuple(receipt for receipt in group if receipt.state in _NONTERMINAL)
        _require(len(live) <= 1)
        if live:
            selected = live[0]
            _require(selected.endpoint_generation == maximum, ErrorCode.stale_event)
        else:
            _require(cleanup)
            latest = tuple(receipt for receipt in group if receipt.endpoint_generation == maximum)
            _require(len(latest) == 1)
            selected = latest[0]
        if selected.daemon_instance_uuid is not None and selected.daemon_instance_uuid != record.instance_uuid:
            _require(not any(r.daemon_instance_uuid == record.instance_uuid for r in group), ErrorCode.stale_event)
        self._join(selected, record, complete=not cleanup)
        return selected

    @staticmethod
    def _join(reservation, record, *, no_receipt=False, complete=True):
        _require(
            type(record.daemon) is ProcessIdentity and record.app == reservation.app
            and record.runtime_directory == reservation.runtime_directory
            and record.socket_path == _path(reservation)
            and record.protocol_version == reservation.protocol_version,
        )
        if not no_receipt:
            for observed, expected in (
                (reservation.daemon_instance_uuid, record.instance_uuid),
                (reservation.daemon, record.daemon),
                (reservation.supervisor, record.supervisor),
            ):
                _require((not complete and observed is None) or observed == expected)

    def _proved(self, record):
        receipt = self._select(record)
        _require(receipt is not None)
        _require(receipt.state in {"bound", "ready"})
        endpoint = _bound(
            receipt, record.daemon, current_generation=self._maximum(record),
            io=self._io, receipt=receipt,
        )
        # Re-read authority facts as well as kernel facts before returning.
        _require(self._select(record) == receipt, ErrorCode.stale_event)
        return OwnershipProof(
            kind="evidence", daemon=endpoint.daemon, socket_entry=endpoint.socket_entry,
            generation=endpoint.generation, handshake=None,
        )

    def _maximum(self, record):
        # Selection already compared the unique nonterminal against every
        # receipt, including terminals. Read it again rather than trusting a
        # receipt generation passed back to itself by the caller.
        loaded = self._receipts()
        if isinstance(loaded, Err):
            raise _Refusal(loaded.error.code)
        _require(isinstance(loaded, Ok) and type(loaded.value) is tuple)
        generations = [r.endpoint_generation for r in loaded.value if (
            r.runtime_directory == record.runtime_directory
            and r.runtime_directory + "/" + r.socket_name == record.socket_path
        )]
        _require(bool(generations))
        return max(generations)

    async def prove(self, record: DaemonRecord) -> Result[OwnershipProof]:
        try:
            return Ok(value=self._proved(record))
        except _Refusal as refusal:
            return _err(refusal.code)
        except OSError:
            return _err(ErrorCode.ownership_mismatch)

    async def authorize(
        self, record: DaemonRecord, operation: BackendOperation,
        target: ReceiverTarget | None, fence: DispatchFence | None,
    ) -> Result[OwnershipProof]:
        if operation is BackendOperation.shutdown or not self.allows(operation):
            return _err(ErrorCode.dependency_incompatible)
        if requires_dispatch_fence(operation) and (target is None or fence is None):
            return _err(ErrorCode.dependency_incompatible)
        proof = await self.prove(record)
        if isinstance(proof, Err):
            return proof
        if fence is not None and operation not in {BackendOperation.status, BackendOperation.discover, BackendOperation.devices}:
            checked = fence()
            if isinstance(checked, Err):
                return checked
            if not isinstance(checked, Ok) or checked.value is not None:
                return _err(ErrorCode.dependency_incompatible)
        return proof

    async def confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Result[None]:
        if not isinstance(proof, OwnershipProof) or proof.kind != "evidence":
            return _err(ErrorCode.ownership_mismatch)
        try:
            receipt = self._select(record)
            _require(receipt is not None)
            _require(
                receipt.endpoint_generation == proof.generation and receipt.daemon == proof.daemon
                and (receipt.entry_device, receipt.entry_inode) == proof.socket_entry,
                ErrorCode.stale_event,
            )
            _require(self._proved(record) == proof, ErrorCode.stale_event)
            return Ok(value=None)
        except _Refusal as refusal:
            return _err(refusal.code)
        except OSError:
            return _err(ErrorCode.ownership_mismatch)

    def _intent(self, reservation, receipt, record):
        if receipt is not None and receipt.state == "stopping":
            return
        _require(receipt is None or receipt.state in {"reserved", "detached", "bound", "ready"})
        if receipt is not None:
            self._join(receipt, record, complete=False)
        _require(self._supervisor_intent is not None)
        loaded = self._supervisor_intent()
        if isinstance(loaded, Err):
            raise _Refusal(loaded.error.code)
        _require(isinstance(loaded, Ok) and loaded.value is not None)
        intent = loaded.value
        _require(
            type(intent.schema_version) is int and intent.schema_version == 1
            and intent.launch_uuid == reservation.launch_uuid and intent.instance_uuid == reservation.instance_uuid
            and intent.app == reservation.app and intent.supervisor == record.supervisor
            and intent.daemon == record.daemon and intent.reason in {"application-exited", "parent-mismatch"}
            and type(intent.intent_at) in {int, float} and math.isfinite(intent.intent_at),
        )
        app = self._io.identity(reservation.app.pid)
        _require(
            app.state in {IdentityState.absent, IdentityState.terminated}
            or (app.state is IdentityState.present and app.identity != reservation.app),
        )
        _identity(self._io, record.supervisor, reservation.owner_uid)

    def _stage(self, reservation, receipt, record, *, stage=None):
        io = self._io
        _generation(reservation, self._maximum(record) if receipt is not None else reservation.endpoint_generation, io)
        if stage is None:
            if receipt is not None:
                fields = tuple(getattr(receipt, name) for name in _BOUND_FIELDS)
                _require(all(value is None for value in fields) or all(value is not None for value in fields))
                stage = "pre_bind" if fields[0] is None else "bound"
            else:
                with _anchor(reservation, io) as descriptor:
                    try:
                        _entry(reservation, io, descriptor)
                        stage = "bound"
                    except _Refusal as refusal:
                        # A symlink/wrong-owner entry is never pre-bind absence.
                        try:
                            io.stat(reservation.socket_name, dir_fd=descriptor, follow_symlinks=False)
                        except FileNotFoundError:
                            stage = "pre_bind"
                        else:
                            raise refusal
        if stage == "bound":
            endpoint = _bound(
                reservation, record.daemon,
                current_generation=self._maximum(record) if receipt is not None else reservation.endpoint_generation,
                io=io, receipt=receipt,
            )
            entry, inode = endpoint.socket_entry, endpoint.socket_inode
        else:
            with _anchor(reservation, io) as descriptor:
                child = _identity(io, record.daemon, reservation.owner_uid)
                _identity(io, record.supervisor, reservation.owner_uid)
                _require(child.parent == record.supervisor.pid)
                _argv(io, record.daemon, _path(reservation))
                child_after = _identity(io, record.daemon, reservation.owner_uid)
                _require(child_after.parent == record.supervisor.pid)
                _identity(io, record.supervisor, reservation.owner_uid)
                _directory(reservation, io, descriptor)
            entry, inode = None, None
        return self._factory(
            stage=stage, expected_identity=record.daemon, expected_uid=reservation.owner_uid,
            expected_socket_name=_path(reservation), expected_supervisor=record.supervisor,
            expected_entry=entry, expected_socket_inode=inode,
        )

    def _unchanged(self, receipt, record):
        _require(self._select(record, cleanup=True) == receipt, ErrorCode.stale_event)

    def _clean(self, reservation, receipt, record, expected_entry):
        io = self._io
        self._unchanged(receipt, record)
        _generation(reservation, self._maximum(record) if receipt is not None else reservation.endpoint_generation, io)
        absent = io.identity(record.daemon.pid)
        _require(absent.state in {IdentityState.absent, IdentityState.terminated})
        if absent.state is IdentityState.terminated:
            _require(absent.identity == record.daemon and absent.uid == reservation.owner_uid)
        with _anchor(reservation, io) as descriptor:
            try:
                entry = _entry(reservation, io, descriptor)
            except _Refusal:
                # Only ENOENT is absence; wrong type/owner retains the entry.
                try:
                    io.stat(reservation.socket_name, dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    _require(io.unix_inodes(_path(reservation)) == ())
                    return
                raise
            _require(expected_entry is not None and entry == expected_entry, ErrorCode.stale_socket)
            _require(io.unix_inodes(_path(reservation)) == ())
            try:
                io.peer(descriptor, reservation.socket_name)
            except OSError as error:
                _require(error.errno in {errno.ECONNREFUSED, errno.ENOENT})
            else:
                raise _Refusal(ErrorCode.ownership_mismatch)
            self._unchanged(receipt, record)
            _require(io.identity(record.daemon.pid).state in {IdentityState.absent, IdentityState.terminated})
            _require(_entry(reservation, io, descriptor) == entry, ErrorCode.stale_socket)
            _directory(reservation, io, descriptor)
            try:
                io.unlink(reservation.socket_name, dir_fd=descriptor)
                io.fsync(descriptor)
            except OSError:
                raise _Refusal(ErrorCode.cleanup_timeout) from None

    async def release(self, record: DaemonRecord) -> Result[None]:
        try:
            receipt = self._select(record, cleanup=True)
            reservation = receipt if receipt is not None else self._reservation
            _require(reservation is not None)
            if receipt is None:
                self._join(reservation, record, no_receipt=True)
            io = self._io
            identity = io.identity(record.daemon.pid)
            if identity.state in {IdentityState.absent, IdentityState.terminated}:
                entry = None if receipt is None or receipt.entry_inode is None else (receipt.entry_device, receipt.entry_inode)
                self._clean(reservation, receipt, record, entry)
                return Ok(value=None)
            _identity(io, record.daemon, reservation.owner_uid)
            self._intent(reservation, receipt, record)
            evidence = self._stage(reservation, receipt, record)

            def revalidate() -> Result[TerminationEvidence]:
                try:
                    self._unchanged(receipt, record)
                    self._intent(reservation, receipt, record)
                    fresh = self._stage(reservation, receipt, record, stage=evidence.stage)
                    _require(fresh == evidence)
                    return Ok(value=fresh)
                except _Refusal as refusal:
                    return _err(refusal.code)
                except OSError:
                    return _err(ErrorCode.ownership_mismatch)

            terminated = await self._process.terminate(
                record.daemon, reservation.launch_uuid, reservation.runtime_directory,
                TERMINATION_GRACE, strict=True, evidence=evidence, revalidate=revalidate,
            )
            if isinstance(terminated, Err):
                return terminated
            if io.identity(record.daemon.pid).state is IdentityState.present:
                return _err(ErrorCode.cleanup_timeout)
            self._clean(reservation, receipt, record, evidence.expected_entry)
            return Ok(value=None)
        except _Refusal as refusal:
            return _err(refusal.code)
        except OSError:
            return _err(ErrorCode.ownership_mismatch)


__all__ = ["EvidenceAuthority", "VerifiedEndpoint", "verify_owned_endpoint"]
