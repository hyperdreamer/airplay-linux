"""Atomic publication of non-secret records under a private root.

Every durable write in this application follows the design's protocol: write a
temporary file in the destination directory, ``fsync`` the file, rename it over
the target, then ``fsync`` the directory. The rename is the publication point,
so an interruption leaves either the previous complete record or the new
complete record -- never a document a later startup could read as an
authoritative reservation.

The operating-system calls live behind :class:`PosixFilesystem` so a test can
fail one step and inspect what survived. The *step* a failure happened at is the
whole answer to "did this publish?", and it travels with the raised
:class:`StorageError` as a fixed token rather than as the failure's text: an
``OSError`` message can quote a path or a caller value, and a storage adapter's
failure text must not become user-visible copy.

Two boundaries are enforced here rather than at each call site:

* a record path is a *relative* path of plain components. Absolute paths,
  ``.``/``..`` components, empty components, control characters, and
  leading-dot components are refused, so nothing can name a file outside the
  private root or one of this module's own temporary artifacts.
* nothing is ever written or read through a symbolic link. The root, every
  directory in the chain, and the record itself are checked.
"""

from __future__ import annotations

import os
import string
from collections.abc import Mapping
from pathlib import Path
from typing import Final

#: Directories this package creates are private to the user.
PRIVATE_DIRECTORY_MODE: Final[int] = 0o700

#: Records hold receiver identities and endpoints; they are user-readable only.
PRIVATE_FILE_MODE: Final[int] = 0o600

#: The characters a record path component may use. Record names are
#: percent-encoded from their identity token, so this covers every name this
#: package writes while excluding whitespace, separators, and shell characters.
#: ``~`` is included because ``urllib.parse.quote`` never escapes it.
_PATH_CHARACTERS: Final[frozenset[str]] = frozenset(
    string.ascii_letters + string.digits + "%._-~",
)

#: Fixed token for a path that is not a plain relative record path.
PATH_STEP: Final[str] = "path"


class StorageError(Exception):
    """A durable operation that did not complete, tagged with the failing step.

    ``step`` is one of a closed set of source-written tokens (``path``,
    ``write``, ``fsync``, ``rename``, ``directory_fsync``, ``directory``,
    ``read``, ``delete``). It never holds path text or an operating-system
    message, because this exception's caller turns the step into a diagnostic.
    """

    def __init__(self, step: str) -> None:
        self.step = step
        super().__init__(f"durable storage step {step} failed")

    def __repr__(self) -> str:
        return f"StorageError(step={self.step!r})"


class PosixFilesystem:
    """The operating-system calls one atomic publication needs.

    Subclassing this is the supported way to inject a failure at exactly one
    step: the real system calls stay in place, so the test observes the true
    on-disk result of the steps that did run before the failure.
    """

    def write(self, path: Path, payload: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
        )
        try:
            # ``os.open`` applies the process umask, so the requested mode is only
            # an upper bound: a hardened umask such as 0o077 would leave a record
            # readable only through root, and a 0o777 umask would leave it at 0o000
            # and make even this process's own ``fsync`` fail. Set the mode
            # explicitly, so the record's accessibility never depends on the
            # caller's umask.
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        finally:
            os.close(descriptor)

    def fsync(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace(self, source: Path, destination: Path) -> None:
        os.replace(source, destination)

    def fsync_directory(self, directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def read(self, path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    def names(self, directory: Path) -> tuple[str, ...]:
        return tuple(os.listdir(directory))

    def unlink(self, path: Path) -> None:
        os.unlink(path)

    def mkdir(self, path: Path) -> None:
        os.mkdir(path, PRIVATE_DIRECTORY_MODE)
        # ``mkdir`` applies the process umask; the directory holds receiver
        # identities and must be private regardless of the caller's umask.
        os.chmod(path, PRIVATE_DIRECTORY_MODE)
    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def is_symlink(self, path: Path) -> bool:
        return os.path.islink(path)

    def is_directory(self, path: Path) -> bool:
        return os.path.isdir(path) and not os.path.islink(path)

    def is_regular_file(self, path: Path) -> bool:
        return os.path.isfile(path) and not os.path.islink(path)


class AtomicFiles:
    """A private root whose records are published atomically and durably.

    The root is resolved to its real path once, at construction. A symlinked
    *root* is ordinary user configuration -- a dotfile directory linked elsewhere
    is common -- so it is followed deliberately rather than rejected, and every
    path check below is then made against the directory the records really live
    in. A symlink *inside* the root is a different matter: it is how a record
    path could reach a file this package does not own, so those are refused.
    """

    def __init__(self, root: Path, filesystem: PosixFilesystem | None = None) -> None:
        self._root = Path(os.path.realpath(Path(root)))
        self._filesystem = PosixFilesystem() if filesystem is None else filesystem

    @property
    def root(self) -> Path:
        return self._root

    def write(self, relative: str, payload: bytes) -> None:
        """Publish ``payload`` at ``relative``; raise :class:`StorageError` otherwise."""
        target = self._resolve(relative)
        self._ensure_directory(target.parent)
        temporary = target.with_name(f".{target.name}.tmp")
        step = "write"
        try:
            self._filesystem.write(temporary, payload)
            step = "fsync"
            self._filesystem.fsync(temporary)
            step = "rename"
            self._filesystem.replace(temporary, target)
            step = "directory_fsync"
            self._filesystem.fsync_directory(target.parent)
        except OSError:
            self._discard(temporary)
            raise StorageError(step) from None

    def read(self, relative: str) -> bytes | None:
        """The record at ``relative``, or ``None`` when it does not exist."""
        target = self._resolve(relative)
        if not self._filesystem.exists(target):
            return None
        if not self._filesystem.is_regular_file(target):
            raise StorageError(PATH_STEP)
        try:
            return self._filesystem.read(target)
        except OSError:
            raise StorageError("read") from None

    def delete(self, relative: str) -> None:
        """Remove the record at ``relative``; removing an absent record is not an error."""
        target = self._resolve(relative)
        if not self._filesystem.exists(target):
            return
        if not self._filesystem.is_regular_file(target):
            raise StorageError(PATH_STEP)
        try:
            self._filesystem.unlink(target)
        except OSError:
            raise StorageError("delete") from None
        try:
            self._filesystem.fsync_directory(target.parent)
        except OSError:
            raise StorageError("directory_fsync") from None

    def names(self, relative: str) -> tuple[str, ...]:
        """Settled record names in ``relative``, sorted, without temporary artifacts."""
        directory = self._resolve(relative)
        if not self._filesystem.exists(directory):
            return ()
        if not self._filesystem.is_directory(directory):
            raise StorageError(PATH_STEP)
        try:
            entries = self._filesystem.names(directory)
        except OSError:
            raise StorageError("read") from None
        return tuple(sorted(name for name in entries if not name.startswith(".")))

    def _resolve(self, relative: str) -> Path:
        parts = self._components(relative)
        path = self._root
        for part in parts:
            path = path / part
            if self._filesystem.is_symlink(path):
                raise StorageError(PATH_STEP)
        return path

    @staticmethod
    def _components(relative: str) -> tuple[str, ...]:
        if type(relative) is not str or not relative:
            raise StorageError(PATH_STEP)
        if relative.startswith("/") or "\x00" in relative:
            raise StorageError(PATH_STEP)
        parts = tuple(relative.split("/"))
        for part in parts:
            if not part or part in {".", ".."} or part.startswith("."):
                raise StorageError(PATH_STEP)
            if not set(part) <= _PATH_CHARACTERS:
                raise StorageError(PATH_STEP)
        return parts

    def _ensure_directory(self, directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        while True:
            if self._filesystem.is_symlink(current):
                raise StorageError(PATH_STEP)
            if self._filesystem.is_directory(current):
                break
            if self._filesystem.exists(current):
                raise StorageError(PATH_STEP)
            missing.append(current)
            if current == current.parent:
                break
            current = current.parent
        for path in reversed(missing):
            try:
                self._filesystem.mkdir(path)
            except OSError:
                raise StorageError("directory") from None
            try:
                self._filesystem.fsync_directory(path.parent)
            except OSError:
                raise StorageError("directory_fsync") from None

    def _discard(self, temporary: Path) -> None:
        """Remove this module's temporary artifact; the published record is untouched."""
        try:
            if self._filesystem.exists(temporary) and not self._filesystem.is_symlink(temporary):
                self._filesystem.unlink(temporary)
        except OSError:
            # The failure being reported is the durable step, not the cleanup of
            # an artifact a later write overwrites anyway.
            return


def state_root(environ: Mapping[str, str] | None = None) -> Path:
    """``$XDG_STATE_HOME/airplay-linux``, or the ``~/.local/state`` fallback.

    A relative or empty ``XDG_STATE_HOME`` is ignored, exactly as the XDG base
    directory specification requires, so a stray value cannot move durable
    metadata into the current working directory.
    """
    environment = os.environ if environ is None else environ
    configured = environment.get("XDG_STATE_HOME") or ""
    if configured and os.path.isabs(configured):
        base = Path(configured)
    else:
        home = environment.get("HOME") or ""
        base = (Path(home) if home else Path.home()) / ".local" / "state"
    return base / "airplay-linux"
