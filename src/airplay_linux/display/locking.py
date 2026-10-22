"""Provider-wide locking for virtual-output creation.

The in-process lock makes the normal path cheap and deterministic.  The small
fcntl lock file is the process boundary: another application instance using the
same runtime directory cannot reconcile or create a virtual output concurrently.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from types import TracebackType
from typing import Self

import fcntl


class _HeldLock:
    def __init__(self, owner: "CreationLock", descriptor: int) -> None:
        self._owner = owner
        self._descriptor = descriptor
        self._released = False

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._released:
            return
        self._released = True
        self._owner._release_descriptor(self._descriptor)


class CreationLock:
    """Serialize virtual-output creation across tasks and app instances."""

    def __init__(self, runtime_directory: str | os.PathLike[str]) -> None:
        directory = Path(runtime_directory)
        if not directory.is_absolute():
            raise ValueError("runtime_directory must be absolute")
        self.runtime_directory = directory
        self.lock_path = directory / "virtual-display-creation.lock"
        self._task_lock = asyncio.Lock()

    def acquire(self) -> "_CreationLockContext":
        return _CreationLockContext(self)

    async def _enter(self) -> _HeldLock:
        await self._task_lock.acquire()
        descriptor: int | None = None
        try:
            self.runtime_directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0)
            return _HeldLock(self, descriptor)
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            self._task_lock.release()
            raise

    def _release_descriptor(self, descriptor: int) -> None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as error:
            if error.errno not in (errno.EBADF, errno.EINVAL):
                raise
        finally:
            try:
                os.close(descriptor)
            finally:
                self._task_lock.release()


class _CreationLockContext:
    def __init__(self, owner: CreationLock) -> None:
        self._owner = owner
        self._held: _HeldLock | None = None

    async def __aenter__(self) -> None:
        self._held = await self._owner._enter()
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        held = self._held
        if held is not None:
            await held.__aexit__(exc_type, exc, traceback)
            self._held = None


__all__ = ["CreationLock"]
