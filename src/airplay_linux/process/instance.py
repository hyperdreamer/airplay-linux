"""Authenticated single-instance ownership for the desktop application.

Qt's lock file is deliberately kept as the first part of the file: other Qt
programs can still read its normal PID/host/application fields.  This module
adds a versioned process start-time extension and a private companion record so
PID reuse, a crash during publication, and inaccessible procfs evidence all
fail closed.
"""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Final
from uuid import UUID

from PySide6.QtCore import QLockFile

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.ports import ProcessPort
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.process.procfs import IdentityState, read_identity_evidence


_EXTENSION: Final[bytes] = b"AIRPLAY_LINUX_OWNER_V1\n"
_SCHEMA: Final[int] = 1
_OWNER_MODE: Final[int] = 0o600
_DIRECTORY_MODE: Final[int] = 0o700


def _failure() -> Err:
    return Err(error=diagnostic(ErrorCode.ownership_mismatch))


def _instance_uuid(value: object) -> bool:
    return type(value) is UUID


def _safe_directory(path: Path) -> bool:
    """Reject a foreign/symlinked runtime root before any lock operation."""
    if not path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return False
    try:
        if path.exists():
            info = path.lstat()
            if not info or not path.is_dir() or path.is_symlink():
                return False
            if info.st_uid != os.getuid():
                return False
            if info.st_mode & 0o077:
                os.chmod(path, _DIRECTORY_MODE)
            return True
        path.mkdir(parents=True, mode=_DIRECTORY_MODE, exist_ok=True)
        info = path.lstat()
        return path.is_dir() and not path.is_symlink() and info.st_uid == os.getuid()
    except OSError:
        return False


def _read_regular(path: Path) -> bytes | None:
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid():
            return None
        return path.read_bytes()
    except OSError:
        return None


def _read_lock_info(path: Path) -> tuple[int, bytes] | None:
    """Read Qt's compatibility fields without asking QLockFile to remove it."""
    payload = _read_regular(path)
    if payload is None:
        return None
    try:
        qt_lock = QLockFile(str(path))
        qt_lock.setStaleLockTime(0)
        info = qt_lock.getLockInfo()
        pid = int(info[0])
        lines = payload.splitlines()
        # Qt 6 writes the PID, host, application, and its identity fields.
        # A shorter file is a crash-truncated lock, not stale-owner evidence.
        if pid <= 0 or len(lines) < 4 or lines[0] != str(pid).encode("ascii"):
            return None
    except (IndexError, TypeError, ValueError, OSError):
        return None
    return pid, payload


def _extension(payload: bytes) -> tuple[UUID, int] | None:
    """Parse only our trailing extension; Qt's leading fields stay opaque."""
    if _EXTENSION not in payload:
        return None
    prefix, suffix = payload.split(_EXTENSION, 1)
    if not prefix or not suffix.endswith(b"\n"):
        return None
    lines = suffix.splitlines()
    if len(lines) != 2:
        return None
    try:
        owner = UUID(lines[0].decode("ascii"))
        start_ticks = int(lines[1].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return None
    if start_ticks < 0:
        return None
    return owner, start_ticks


def _read_owner(path: Path) -> dict[str, object] | None:
    payload = _read_regular(path)
    if payload is None:
        return None
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if type(value) is not dict:
        return None
    if value.get("schema_version") != _SCHEMA:
        return None
    if type(value.get("pid")) is not int or type(value.get("start_ticks")) is not int:
        return None
    try:
        UUID(value["instance_uuid"])
    except (KeyError, TypeError, ValueError):
        return None
    if value["pid"] <= 0 or value["start_ticks"] < 0:
        return None
    return value


def _atomic_owner(path: Path, *, instance_uuid: UUID, identity: ProcessIdentity) -> bool:
    """Publish the private companion record with a file and directory fsync."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    value = {
        "schema_version": _SCHEMA,
        "instance_uuid": str(instance_uuid),
        "pid": identity.pid,
        "start_ticks": identity.start_ticks,
        "hostname": socket.gethostname(),
    }
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(temporary, flags, _OWNER_MODE)
        try:
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


def _append_extension(path: Path, instance_uuid: UUID, identity: ProcessIdentity) -> bool:
    """Append our data only after QLockFile has acquired the OS lock."""
    try:
        flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid():
                return False
            os.fchmod(descriptor, _OWNER_MODE)
            data = _EXTENSION + f"{instance_uuid}\n{identity.start_ticks}\n".encode("ascii")
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
    except OSError:
        return False


def _stable_unlink(path: Path, before: bytes) -> bool:
    """Unlink only the exact lock bytes inspected before liveness checking."""
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or path.read_bytes() != before:
            return False
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                return False
        finally:
            os.close(descriptor)
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    except OSError:
        return False


class InstanceGuard:
    """Hold the one application lock under ``runtime_root``."""

    def __init__(self, runtime_root: Path, process: ProcessPort) -> None:
        self.runtime_root = Path(runtime_root)
        self.process = process
        self.lock_path = self.runtime_root / "airplay-linux.lock"
        self.owner_path = self.runtime_root / "airplay-linux.lock.owner"
        self._lock: QLockFile | None = None

    def acquire(self, instance_uuid: InstanceId) -> Result[None]:
        if not _instance_uuid(instance_uuid) or not _safe_directory(self.runtime_root):
            return _failure()
        current = self.process.identity(os.getpid())
        if current is None:
            return _failure()

        # QLockFile's age based stale deletion is disabled.  We inspect the
        # existing bytes first, then remove them only after exact proc evidence.
        if self.lock_path.exists() or self.lock_path.is_symlink():
            if not self._retire_existing():
                return _failure()
        lock = QLockFile(str(self.lock_path))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            return _failure()
        if not _append_extension(self.lock_path, instance_uuid, current):
            lock.unlock()
            return _failure()
        if not _atomic_owner(self.owner_path, instance_uuid=instance_uuid, identity=current):
            lock.unlock()
            return _failure()
        self._lock = lock
        return Ok(value=None)

    def _retire_existing(self) -> bool:
        inspected = _read_lock_info(self.lock_path)
        if inspected is None:
            return False
        pid, before = inspected
        identity = read_identity_evidence(pid)
        if identity.state is IdentityState.unreadable:
            return False
        if identity.state is IdentityState.present:
            # A live PID without a start-time extension is still live.  A live
            # PID with a mismatching extension may be a recycled PID. Both are
            # ambiguous and therefore block a new application instance.
            return False
        if _stable_unlink(self.lock_path, before) is False:
            return False
        # The companion is metadata, never liveness evidence. Remove it only
        # after the lock itself has been proven stale; an invalid record is safe
        # to replace on the next successful acquisition.
        try:
            if self.owner_path.exists() and not self.owner_path.is_symlink():
                self.owner_path.unlink()
        except OSError:
            return False
        return True

    def release(self) -> None:
        lock, self._lock = self._lock, None
        if lock is None:
            return
        try:
            if self.owner_path.exists() and not self.owner_path.is_symlink():
                self.owner_path.unlink()
        except OSError:
            pass
        try:
            lock.unlock()
        except (OSError, RuntimeError):
            pass


__all__ = ["InstanceGuard"]
