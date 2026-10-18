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

Four boundaries are enforced here rather than at each call site, and each one is
re-established on *every* access rather than once at construction, because a
process can crash between any two steps and a filesystem can change under a
long-lived process:

* a record path is a *relative* path of plain components. Absolute paths,
  ``.``/``..`` components, empty components, control characters, and
  leading-dot components are refused, so nothing can name a file outside the
  private root or one of this module's own temporary artifacts.
* the root itself is part of the boundary. A symlinked application root is
  refused, the root's device/inode identity is pinned, and every operation is
  anchored to a directory descriptor opened with ``O_NOFOLLOW`` -- so replacing
  the root, or a directory inside it, cannot redirect a read, a write, or a
  delete into a directory this package does not own.
* every directory this package creates or reuses under the root must be a real
  directory owned by this user, and is tightened to ``0700``. A pre-existing
  ``0755``/``0777`` record directory would let another user replace metadata.
* a publication is not reported successful until the directory entries it
  depends on are ``fsync``ed -- and no earlier ``fsync`` is ever accepted as
  proof about a later mutation. A directory ``fsync`` makes durable the entries
  present *when it ran*; a rename or unlink performed afterwards is a new entry
  mutation that only a later ``fsync`` of the same directory can make durable.
  So the confirmation runs after every publication point, over the whole chain
  from the changed entry up to the root's parent, and nothing is remembered
  between publications or between retries on the same instance. That single rule
  covers the three cases a confirmation cache got wrong: a second write to a
  directory that was already synced, a delete (whose containing directory must be
  synced after the unlink even when the record was already gone, because absence
  is a publication too), and a newly created child directory (whose *parent* must
  be synced, since the entry naming the child lives there).
"""

from __future__ import annotations

import errno
import os
import string
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import S_IMODE, S_ISDIR, S_ISLNK, S_ISREG
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

#: Fixed token for a path that is not a plain relative record path, or for a
#: directory in the chain that is not a real directory this package may use.
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

    The calls *below* the root take a directory descriptor and one path
    component. That shape is what anchors an operation: the descriptor names a
    verified directory, so no later rename or symlink at the same path can
    redirect the call. Only the calls at and above the root take a path, and only
    the root itself is opened with ``O_NOFOLLOW``.
    """

    # -- at and above the root -------------------------------------------------

    def status_path(self, path: Path) -> os.stat_result | None:
        """``lstat`` for a path, with confirmed absence kept apart from failure."""
        try:
            return os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def open_root(self, path: Path) -> int:
        """Open the root directory without following a final symlink."""
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

    def open_ancestor(self, path: Path) -> int:
        """Open a directory above the root, following links.

        An ancestor is ordinary user configuration -- a state directory reached
        through a linked parent -- and is not part of this package's boundary. It
        is opened only to sync the directory entry it holds.
        """
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)

    def make_root(self, path: Path) -> None:
        """Create the root directory, private regardless of the caller's umask."""
        os.mkdir(path, PRIVATE_DIRECTORY_MODE)
        # ``mkdir`` applies the process umask, so the requested mode is only an
        # upper bound: a 0o777 umask leaves the directory at 0o000, where not even
        # its owner can open it to set the mode properly. ``chmod`` by path works
        # because this process owns the directory it just created.
        os.chmod(path, PRIVATE_DIRECTORY_MODE)

    def close(self, descriptor: int) -> None:
        os.close(descriptor)

    # -- inside the root, anchored to a directory descriptor -------------------

    def open_directory(self, parent: int, name: str) -> int:
        return os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent,
        )

    def make_directory(self, parent: int, name: str) -> None:
        os.mkdir(name, PRIVATE_DIRECTORY_MODE, dir_fd=parent)
        # Same umask reasoning as the root; ``chmod`` needs no descriptor, so a
        # directory created under a 0o777 umask is recoverable.
        os.chmod(name, PRIVATE_DIRECTORY_MODE, dir_fd=parent)

    def status(self, parent: int, name: str) -> os.stat_result | None:
        """``lstat`` one entry, with confirmed absence kept apart from failure.

        A failed lookup must never be reported as absence: an inaccessible
        ownership directory would otherwise read as a successfully empty set of
        owned resources.
        """
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def describe(self, descriptor: int) -> os.stat_result:
        return os.fstat(descriptor)

    def chmod_private(self, descriptor: int) -> None:
        os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)

    def write(self, parent: int, name: str, payload: bytes) -> None:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            PRIVATE_FILE_MODE,
            dir_fd=parent,
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

    def fsync_file(self, parent: int, name: str) -> None:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace(self, parent: int, source: str, destination: str) -> None:
        os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)

    def fsync_directory(self, descriptor: int) -> None:
        os.fsync(descriptor)

    def read(self, parent: int, name: str) -> bytes:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(descriptor)

    def names(self, descriptor: int) -> tuple[str, ...]:
        return tuple(os.listdir(descriptor))

    def unlink(self, parent: int, name: str) -> None:
        os.unlink(name, dir_fd=parent)


def _open_step(error: OSError) -> str:
    """Translate a directory-open failure into one of this module's step tokens.

    A symlink or a non-directory is a boundary refusal and says so; a permission
    or I/O failure is an inspection failure and stays ``read``. Both are
    failures -- neither is reported as absence.
    """
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return PATH_STEP
    return "read"


@dataclass(frozen=True, kw_only=True)
class _Located:
    """An open record directory together with the chain that reaches it.

    ``directories`` runs outermost-first (the root, then each subdirectory), so a
    publication can confirm the durability of every entry it used rather than of
    the last one only. ``missing`` records that the chain stops early: the named
    record directory does not exist, and ``parent`` is the deepest directory that
    does.
    """

    parent: int | None
    directories: tuple[int, ...]
    missing: bool = False


class AtomicFiles:
    """A private root whose records are published atomically and durably.

    The root's identity is pinned on first use and re-verified on every
    operation. A symlinked *root* is refused rather than followed: the storage
    boundary is the root, so "the records really live elsewhere" is exactly the
    redirection this module must not allow. Symlinked ancestors *above* the root
    are ordinary user configuration, so only the root itself is opened with
    ``O_NOFOLLOW``.
    """

    def __init__(self, root: Path, filesystem: PosixFilesystem | None = None) -> None:
        self._filesystem = PosixFilesystem() if filesystem is None else filesystem
        self._root = Path(os.path.abspath(Path(root)))
        #: Device/inode of the verified root, pinned so a replacement is noticed.
        #: Pinned at construction when the root already exists: an instance that
        #: has not yet touched the filesystem would otherwise accept a root
        #: swapped in before its first operation.
        self._identity: tuple[int, int] | None = None
        existing = self._status_path(self._root)
        if existing is not None:
            if S_ISLNK(existing.st_mode) or not S_ISDIR(existing.st_mode):
                raise StorageError(PATH_STEP)
            self._identity = (existing.st_dev, existing.st_ino)

    @property
    def root(self) -> Path:
        return self._root

    # ------------------------------------------------------------------ records

    def write(self, relative: str, payload: bytes) -> None:
        """Publish ``payload`` at ``relative``; raise :class:`StorageError` otherwise."""
        parts = self._components(relative)
        with self._directory(parts[:-1], create=True) as located:
            parent = located.parent
            if parent is None or located.missing:
                raise StorageError(PATH_STEP)
            target = parts[-1]
            self._require_record_slot(parent, target)
            temporary = f".{target}.tmp"
            step = "write"
            try:
                self._filesystem.write(parent, temporary, payload)
                step = "fsync"
                self._filesystem.fsync_file(parent, temporary)
                step = "rename"
                self._filesystem.replace(parent, temporary, target)
            except OSError:
                self._discard(parent, temporary)
                raise StorageError(step) from None
            # The rename is the publication point; the directories that carry it
            # are confirmed durable before the caller is told the write
            # succeeded, so a failure here reports an *uncertain* publication
            # rather than success.
            self._confirm(located)

    def read(self, relative: str) -> bytes | None:
        """The record at ``relative``, or ``None`` when it is confirmed absent."""
        parts = self._components(relative)
        with self._directory(parts[:-1], create=False) as located:
            if located.missing or located.parent is None:
                return None
            found = self._lookup(located.parent, parts[-1])
            if found is None:
                return None
            if not S_ISREG(found.st_mode):
                raise StorageError(PATH_STEP)
            try:
                return self._filesystem.read(located.parent, parts[-1])
            except OSError:
                raise StorageError("read") from None

    def delete(self, relative: str) -> None:
        """Remove the record at ``relative`` and establish durable absence.

        Removing a record that is already gone is not an error, but it is still a
        publication of *absence*: the containing directory is confirmed durable
        either way, so a retry after a failed unlink publication cannot report
        success while the removal is unconfirmed on durable storage.
        """
        parts = self._components(relative)
        with self._directory(parts[:-1], create=False) as located:
            if located.parent is not None and not located.missing:
                found = self._lookup(located.parent, parts[-1])
                if found is not None:
                    if not S_ISREG(found.st_mode):
                        raise StorageError(PATH_STEP)
                    try:
                        self._filesystem.unlink(located.parent, parts[-1])
                    except OSError:
                        raise StorageError("delete") from None
                # Confirmed even when the record was already gone: absence, like
                # presence, is only a durable fact once the entry is synced.
                self._confirm(located)

    def names(self, relative: str) -> tuple[str, ...]:
        """Settled record names in ``relative``, sorted, without temporary artifacts."""
        parts = self._components(relative)
        with self._directory(parts, create=False) as located:
            if located.parent is None or located.missing:
                return ()
            try:
                entries = self._filesystem.names(located.parent)
            except OSError:
                raise StorageError("read") from None
        return tuple(sorted(name for name in entries if not name.startswith(".")))

    # -------------------------------------------------------------------- paths

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

    @contextmanager
    def _directory(
        self, parts: tuple[str, ...], *, create: bool,
    ) -> Iterator[_Located]:
        """Open (and optionally create) the directory holding a record.

        Every link is verified and entered through a descriptor, so the yielded
        directory is the one that was checked rather than a path that could be
        re-pointed after the check. ``missing`` reports that the chain stops
        before the requested directory: what exists is still reported, so a
        caller can confirm the durability of the deepest entry it does have.
        """
        descriptors: list[int] = []
        try:
            root = self._root_descriptor(create=create)
            if root is None:
                yield _Located(parent=None, directories=(), missing=True)
                return
            descriptors.append(root)
            parent = root
            for part in parts:
                entered = self._enter(parent, part, create=create)
                if entered is None:
                    yield _Located(
                        parent=parent,
                        directories=tuple(descriptors),
                        missing=True,
                    )
                    return
                descriptors.append(entered)
                parent = entered
            yield _Located(parent=parent, directories=tuple(descriptors))
        finally:
            for descriptor in reversed(descriptors):
                try:
                    self._filesystem.close(descriptor)
                except OSError:
                    pass

    # ----------------------------------------------------------------- the root

    def _root_descriptor(self, *, create: bool) -> int | None:
        try:
            descriptor = self._filesystem.open_root(self._root)
        except FileNotFoundError:
            if not create:
                return None
            self._create_root()
            try:
                descriptor = self._filesystem.open_root(self._root)
            except OSError as error:
                raise StorageError(_open_step(error)) from None
        except OSError as error:
            # ``ELOOP`` here means the root is a symlink, which is refused.
            raise StorageError(_open_step(error)) from None
        try:
            self._verify_root(descriptor)
        except BaseException:
            self._close_quietly(descriptor)
            raise
        return descriptor

    def _verify_root(self, descriptor: int) -> None:
        try:
            info = self._filesystem.describe(descriptor)
        except OSError:
            raise StorageError("read") from None
        if S_ISLNK(info.st_mode) or not S_ISDIR(info.st_mode):
            raise StorageError(PATH_STEP)
        identity = (info.st_dev, info.st_ino)
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            # The path now names a different directory than the one this instance
            # verified: a replacement cannot inherit our records.
            raise StorageError(PATH_STEP)
        self._ensure_private(descriptor, info)

    def _create_root(self) -> None:
        """Create the root, and any missing ancestor, without following links.

        Durability is not claimed here: the caller confirms every entry this
        creation added -- the root's parent and any ancestor created on the way --
        after publication, so a failure between creating a directory and syncing
        its parent is repaired by any later write or delete rather than assumed
        away.
        """
        missing: list[Path] = []
        current = self._root
        while True:
            existing = self._status_path(current)
            if existing is not None:
                if S_ISLNK(existing.st_mode) or not S_ISDIR(existing.st_mode):
                    raise StorageError(PATH_STEP)
                break
            missing.append(current)
            if current == current.parent:
                raise StorageError("directory")
            current = current.parent
        for path in reversed(missing):
            try:
                self._filesystem.make_root(path)
            except FileExistsError:
                # Another instance created it first; the descriptor verification
                # still decides whether it is a directory this package may use.
                continue
            except OSError:
                raise StorageError("directory") from None

    # ------------------------------------------------------------- the chain

    def _enter(self, parent: int, name: str, *, create: bool) -> int | None:
        found = self._lookup(parent, name)
        if found is None:
            if not create:
                return None
            try:
                self._filesystem.make_directory(parent, name)
            except FileExistsError:
                pass
            except OSError:
                raise StorageError("directory") from None
            found = self._lookup(parent, name)
            if found is None:
                raise StorageError("directory")
        if S_ISLNK(found.st_mode) or not S_ISDIR(found.st_mode):
            raise StorageError(PATH_STEP)
        try:
            descriptor = self._filesystem.open_directory(parent, name)
        except OSError as error:
            raise StorageError(_open_step(error)) from None
        try:
            try:
                opened = self._filesystem.describe(descriptor)
            except OSError:
                raise StorageError("read") from None
            if (opened.st_dev, opened.st_ino) != (found.st_dev, found.st_ino):
                # The entry was replaced between the lookup and the open.
                raise StorageError(PATH_STEP)
            self._ensure_private(descriptor, opened)
        except BaseException:
            self._close_quietly(descriptor)
            raise
        return descriptor

    def _confirm(self, located: _Located) -> None:
        """Establish the durability of the directory entries a publication used.

        This runs after every publication point, and it remembers nothing. A
        ``fsync`` of a directory makes durable the entries that existed when it
        ran; the rename or unlink that follows is a *new* mutation of that same
        directory, so an earlier sync is not evidence about it. There is
        therefore no per-instance or per-pathname confirmation to inherit, on a
        fresh instance or on a retry through the same one -- which is exactly the
        case a failed or crashed publication leaves behind.

        The chain covers every entry this package's own creation can have added:
        the record directory (whose entry the rename or unlink just changed),
        each directory between it and the root, the root, its parent, and any
        ancestor a crashed attempt may have created on the way. A directory that
        merely exists proves only that some attempt reached ``mkdir``; that
        attempt may have failed before the entry naming it was synced.

        Deepest entry first: a child's entry is only durable once the directory
        that holds it has been synced, so the outer directories are confirmed
        after the inner ones. That ordering is also what makes a newly created
        child directory safe: its own sync says nothing about the entry naming it,
        which lives one level further out and is synced afterwards.

        The climb above the root's parent stops at the first directory this
        process cannot open. That stop is sound rather than lazy: every directory
        this package creates is ``0700`` (and every pre-existing one it reuses is
        tightened to ``0700`` first), so a directory this package created is
        always one this process can open. An unopenable ancestor therefore cannot
        hold an entry of ours, and neither can anything above it.
        """
        for descriptor in reversed(located.directories):
            self._fsync(descriptor)
        current = self._root.parent
        while current != current.parent:
            descriptor = self._open_ancestor(current)
            if descriptor is None:
                break
            try:
                self._fsync(descriptor)
            finally:
                self._close_quietly(descriptor)
            current = current.parent

    def _open_ancestor(self, path: Path) -> int | None:
        """Open an ancestor for syncing, or ``None`` when it cannot be ours."""
        try:
            return self._filesystem.open_ancestor(path)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EPERM, errno.ELOOP, errno.ENOTDIR}:
                return None
            raise StorageError(_open_step(error)) from None

    # ---------------------------------------------------------------- helpers

    def _fsync(self, descriptor: int) -> None:
        try:
            self._filesystem.fsync_directory(descriptor)
        except OSError:
            raise StorageError("directory_fsync") from None

    def _status_path(self, path: Path) -> os.stat_result | None:
        try:
            return self._filesystem.status_path(path)
        except OSError:
            raise StorageError("read") from None

    def _lookup(self, parent: int, name: str) -> os.stat_result | None:
        try:
            return self._filesystem.status(parent, name)
        except OSError:
            # An entry this process could not inspect is *unknown*, not absent:
            # reporting absence could turn a live reservation into an unowned one.
            raise StorageError("read") from None

    def _ensure_private(self, descriptor: int, info: os.stat_result) -> None:
        """Ensure a directory this package uses is owned by us and is ``0700``."""
        if info.st_uid != os.geteuid():
            raise StorageError("directory")
        if S_IMODE(info.st_mode) == PRIVATE_DIRECTORY_MODE:
            return
        try:
            self._filesystem.chmod_private(descriptor)
        except OSError:
            raise StorageError("directory") from None

    def _require_record_slot(self, parent: int, name: str) -> None:
        found = self._lookup(parent, name)
        if found is not None and not S_ISREG(found.st_mode):
            # A directory, device, or symlink at the record name is never
            # replaced and never written through.
            raise StorageError(PATH_STEP)

    def _close_quietly(self, descriptor: int) -> None:
        try:
            self._filesystem.close(descriptor)
        except OSError:
            pass

    def _discard(self, parent: int, name: str) -> None:
        """Remove this module's temporary artifact; the published record is untouched."""
        try:
            found = self._filesystem.status(parent, name)
            if found is not None and S_ISREG(found.st_mode):
                self._filesystem.unlink(parent, name)
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
