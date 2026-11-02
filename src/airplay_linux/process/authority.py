"""Serialize cooperative shared-root writers without shortening owner lifetime.

The flock is an operation lease. The owner document outlives it and is published
only after the registry admits a launch. A dead application grants reconciliation
access, never permission to replace its still-unresolved daemon obligation.
"""
from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import Clock
from airplay_linux.domain.records import AuthorityOwner
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.process.procfs import IdentityEvidence, IdentityState, read_identity_evidence
from airplay_linux.storage import AtomicFiles, StorageError
from airplay_linux.storage.codecs import (
    RecordError, decode_authority_owner, encode_authority_owner,
)

OWNER_NAME = "daemon/authority.owner"
LOCK_TIMEOUT = 5.0
LOCK_BACKOFF = 0.05


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return None


def _refused(code: ErrorCode = ErrorCode.ownership_mismatch) -> Err:
    return Err(error=diagnostic(code))


@dataclass(kw_only=True)
class AuthorityLease:
    owner: AuthorityOwner = field(repr=False)
    _lock_fd: int = field(repr=False)
    _authority: SharedRootAuthority = field(repr=False)
    _unlocked: bool = False

    def unlock(self) -> None:
        """Release just this local lock, at most once; never erase durable data."""
        if self._unlocked:
            return
        self._unlocked = True
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            try:
                os.close(self._lock_fd)
            finally:
                if self._authority._lease is self:
                    self._authority._lease = None


class SharedRootAuthority:
    """One non-reentrant operation lease over a private AtomicFiles root.

    ``clear_owner`` has a lifecycle precondition: its caller has proved exact
    process/endpoint absence while holding this lease. The registry supplies that
    proof through its locked reconciliation, before calling compare-clear.
    Neither acquisition nor publication can discard a previous owner instead.
    """

    def __init__(
        self, root: Path | AtomicFiles, *,
        inspect_process: Callable[[int], IdentityEvidence] = read_identity_evidence,
        boot_id: Callable[[], str | None] = read_boot_id,
        clock: Clock | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._files = root if isinstance(root, AtomicFiles) else AtomicFiles(root)
        self._inspect = inspect_process
        self._boot_id = boot_id
        self._now = clock.now if clock is not None else time.monotonic
        self._sleep = sleep
        self._lease: AuthorityLease | None = None
        self._acquiring = False
        self._candidate: AuthorityOwner | None = None
        self._durable_owner: AuthorityOwner | None = None
        self._expected_payload: bytes | None = None

    def boot_id(self) -> str | None:
        try:
            value = self._boot_id()
            return value if type(value) is str and str(UUID(value)) == value else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    @property
    def owner(self) -> AuthorityOwner | None:
        """The owner read/published under the currently held lease."""
        return self._durable_owner if self._lease is not None else None

    def _open_lock(self) -> int:
        # Use the storage root's descriptor walk and identity pin, rather than
        # opening a possibly redirected daemon subdirectory by its absolute path.
        with self._files._directory(("daemon",), create=True) as located:
            if located.parent is None or located.missing:
                raise StorageError("path")
            descriptor = os.open(
                "authority.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                0o600, dir_fd=located.parent,
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                    raise StorageError("path")
                os.fchmod(descriptor, 0o600)
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor

    async def acquire(self, instance: AuthorityOwner) -> Result[AuthorityLease]:
        if self._lease is not None or self._acquiring:
            return _refused()
        self._acquiring = True
        descriptor = None
        try:
            encode_authority_owner(instance)
            boot = self.boot_id()
            if boot is None or boot != instance.boot_id:
                return _refused()
            deadline = self._now() + LOCK_TIMEOUT
            descriptor = self._open_lock()
            while self._now() < deadline:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {errno.EAGAIN, errno.EACCES}:
                        raise
                    remaining = deadline - self._now()
                    if remaining <= 0:
                        # CONCURRENT_WRITER_DETECTED has this daemon-path mapping.
                        return _refused()
                    await self._sleep(min(LOCK_BACKOFF, remaining))
            else:
                return _refused()
            payload = self._files.read(OWNER_NAME)
            previous = None if payload is None else decode_authority_owner(payload)
            if previous is not None:
                if previous.boot_id != boot:
                    return _refused()
                same_instance = (
                    previous.instance_uuid == instance.instance_uuid and previous.app == instance.app
                )
                evidence = self._inspect(previous.app.pid)
                if evidence.state is IdentityState.present:
                    if not same_instance or evidence.identity != previous.app or evidence.uid != os.getuid():
                        return _refused()
                elif evidence.state is not IdentityState.absent and not (
                    evidence.state is IdentityState.terminated and evidence.identity == previous.app
                ):
                    return _refused()
            lease = AuthorityLease(
                owner=previous if previous is not None else instance,
                _lock_fd=descriptor, _authority=self,
            )
            self._candidate = instance
            self._durable_owner = previous
            self._expected_payload = payload
            self._lease = lease
            descriptor = None
            return Ok(value=lease)
        except (OSError, StorageError, RecordError):
            return _refused()
        finally:
            self._acquiring = False
            if descriptor is not None:
                os.close(descriptor)

    def _held(self) -> bool:
        return self._lease is not None and not self._lease._unlocked

    async def publish_owner(self, instance: AuthorityOwner) -> Result[None]:
        """Publish admitted ownership; a prior owner must first be reconciled."""
        if not self._held() or instance != self._candidate:
            return _refused()
        try:
            payload = encode_authority_owner(instance)
            current = self._files.read(OWNER_NAME)
            if current != self._expected_payload or (
                current is not None and decode_authority_owner(current) != instance
            ):
                return _refused()
            self._files.write(OWNER_NAME, payload)
            self._expected_payload = payload
            self._durable_owner = instance
            self._lease.owner = instance
            return Ok(value=None)
        except RecordError:
            return _refused()
        except StorageError:
            return _refused(ErrorCode.persistence_failed)

    async def clear_owner(self, expected: AuthorityOwner) -> Result[None]:
        """Compare-clear after the caller proves teardown absence under this lease."""
        if not self._held() or self._lease.owner != expected or self._durable_owner != expected:
            return _refused()
        try:
            payload = self._files.read(OWNER_NAME)
            if payload is None or payload != self._expected_payload or decode_authority_owner(payload) != expected:
                return _refused()
            self._files.delete(OWNER_NAME)
            self._durable_owner = None
            self._expected_payload = None
            return Ok(value=None)
        except RecordError:
            return _refused()
        except StorageError:
            return _refused(ErrorCode.persistence_failed)


__all__ = ["AuthorityOwner", "AuthorityLease", "SharedRootAuthority"]
