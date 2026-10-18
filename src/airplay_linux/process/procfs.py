"""Ownership evidence read from procfs, with no guess from a process name.

A PID is not an identity: the kernel recycles it. So every ownership question in
this application is answered from three facts that must all hold at the same
instant -- the PID's start time in ``/proc/<pid>/stat``, the UID that owns the
procfs entry, and the exact owner-token/runtime-directory pair in
``/proc/<pid>/environ``. A helper or daemon is matched by that evidence, never by
``comm`` or a command line, because a process name is both spoofable and
unrelated to ownership.

Three consequences shape this module:

* A stat line is parsed from the *last* ``)`` on the line. ``comm`` is
  attacker-and-user controlled, may contain spaces and parentheses, and is
  truncated at 15 bytes; field 22 (``starttime``) is only reachable once the
  final comm delimiter is located.
* A missing, unreadable, oversized, or duplicated-environment entry is *not*
  evidence. It reports what procfs said (``absent``, ``unreadable``) and never
  degrades into "probably ours", because the caller's next step is a signal.
* Nothing here raises for an ordinary procfs condition. A caller that cannot
  read the evidence needs a typed answer, not an ``OSError`` carrying a path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

from airplay_linux.domain.operations import ProcessIdentity

#: The environment names this application's own children are launched with.
#:
#: The design requires a helper to carry the owner UUID and the exact app-instance
#: runtime directory, and startup reconciliation scans for exactly that pair. The
#: names are constants here rather than literals at each call site so the launch
#: side and the scan side cannot drift apart.
OWNER_TOKEN_ENV: Final[str] = "AIRPLAY_LINUX_OWNER_UUID"
RUNTIME_DIRECTORY_ENV: Final[str] = "AIRPLAY_LINUX_RUNTIME_DIRECTORY"

#: A ``/proc/<pid>/environ`` larger than this is treated as unreadable evidence.
#:
#: The kernel caps ``ARG_MAX`` (and therefore the environment block) at a few
#: megabytes; a file beyond that is not a process environment this application
#: wrote, and reading it whole would be unbounded work driven by another process.
MAX_ENVIRONMENT_BYTES: Final[int] = 1 << 20

#: State characters that mean the entry cannot run any more.
#:
#: ``Z`` is an unreaped zombie and ``X`` is dead; both still have identity fields,
#: but neither can be signalled, so a caller may treat them as already gone.
TERMINAL_STATES: Final[frozenset[str]] = frozenset({"Z", "X"})

_DIGITS: Final[str] = "0123456789"


class IdentityState(StrEnum):
    """What procfs said about one PID, as a closed set of answers."""

    present = "present"
    terminated = "terminated"
    absent = "absent"
    unreadable = "unreadable"


@dataclass(frozen=True, kw_only=True)
class IdentityEvidence:
    """Everything one PID lookup established, including why it failed."""

    pid: int
    state: IdentityState
    identity: ProcessIdentity | None = None
    parent: int | None = None
    uid: int | None = None

    @property
    def alive(self) -> bool:
        """Whether the entry is a live process, as opposed to gone or a zombie."""
        return self.state is IdentityState.present


def _valid_pid(pid: object) -> bool:
    return type(pid) is int and pid > 0


def _entry(pid: int, proc_root: Path) -> Path:
    return proc_root / str(pid)


def _read_stat(pid: int, proc_root: Path) -> tuple[ProcessIdentity, int, str] | None:
    """The identity, parent, and run state of ``pid``, or ``None`` if unusable.

    The line is ``pid (comm) state ppid ...`` where ``comm`` may itself contain
    spaces and parentheses, so the *last* ``)`` is the delimiter and the fields
    after it are positional. A line whose leading PID is not this PID's is not
    accepted, because that is precisely how a recycled PID would arrive.
    """
    try:
        text = (_entry(pid, proc_root) / "stat").read_bytes().decode("ascii", "replace")
    except OSError:
        return None
    opened = text.find("(")
    if opened < 0:
        return None
    if text[:opened].strip() != str(pid):
        return None
    closed = text.rfind(")")
    if closed < opened:
        return None
    fields = text[closed + 1 :].split()
    if len(fields) < 20:
        return None
    if len(fields[0]) != 1:
        return None
    try:
        parent = int(fields[1])
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        return None
    if parent < 0 or start_ticks < 0:
        return None
    return ProcessIdentity(pid=pid, start_ticks=start_ticks), parent, fields[0]


def _entry_uid(entry: Path) -> int | None:
    """The owner of the procfs directory, which is the process's real UID."""
    try:
        return entry.stat().st_uid
    except OSError:
        return None


def read_identity_evidence(pid: int, proc_root: Path = Path("/proc")) -> IdentityEvidence:
    """Every fact this module can establish about one PID, or why it could not."""
    if not _valid_pid(pid):
        return IdentityEvidence(pid=pid, state=IdentityState.absent)
    entry = _entry(pid, proc_root)
    if not entry.exists():
        return IdentityEvidence(pid=pid, state=IdentityState.absent)
    parsed = _read_stat(pid, proc_root)
    if parsed is None:
        return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
    identity, parent, run_state = parsed
    state = (
        IdentityState.terminated
        if run_state in TERMINAL_STATES
        else IdentityState.present
    )
    return IdentityEvidence(
        pid=pid,
        state=state,
        identity=identity,
        parent=parent,
        uid=_entry_uid(entry),
    )


def read_identity(pid: int, proc_root: Path = Path("/proc")) -> ProcessIdentity | None:
    """The exact identity of ``pid``, or ``None`` when none can be read.

    A terminated entry still has an identity: it exists, it simply cannot run.
    Callers that need "is it alive" read :func:`read_identity_evidence` instead.
    """
    return read_identity_evidence(pid, proc_root).identity


def read_parent(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """The parent PID of ``pid``, used to confirm a child really is ours."""
    return read_identity_evidence(pid, proc_root).parent


def read_uid(pid: int, proc_root: Path = Path("/proc")) -> int | None:
    """The real UID that owns ``pid``'s procfs entry, or ``None`` if absent."""
    return read_identity_evidence(pid, proc_root).uid


def read_environment(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str] | None:
    """The exact environment of ``pid``, or ``None`` when it is not evidence.

    ``None`` means "do not decide ownership from this": the block may have been
    unreadable for another user's process, truncated by a permission boundary,
    larger than this module will read, or contain the same name twice (where the
    kernel keeps only the first, so the file's later value would be a lie).
    """
    if not _valid_pid(pid):
        return None
    path = _entry(pid, proc_root) / "environ"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENVIRONMENT_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_ENVIRONMENT_BYTES:
        return None
    environment: dict[str, str] = {}
    for block in data.split(b"\0"):
        if not block:
            continue
        name, separator, value = block.partition(b"=")
        if not separator:
            continue
        key = os.fsdecode(name)
        if key in environment:
            return None
        environment[key] = os.fsdecode(value)
    return environment


def _valid_owner(owner_uuid: object) -> bool:
    return type(owner_uuid) is UUID


def _valid_runtime_directory(runtime_directory: object) -> bool:
    return (
        type(runtime_directory) is str
        and runtime_directory != ""
        and runtime_directory.startswith("/")
        and "\0" not in runtime_directory
    )


def owner_evidence(
    identity: ProcessIdentity,
    owner_uuid: UUID,
    runtime_directory: str,
    proc_root: Path = Path("/proc"),
    expected_uid: int | None = None,
) -> bool:
    """Whether this exact process carries this app instance's ownership evidence.

    All of it is required, and all of it is re-read now rather than remembered:

    * the process at ``identity.pid`` still has ``identity.start_ticks`` (a
      recycled PID fails here before anything is signalled);
    * its procfs entry is owned by the expected UID, so another user's process
      cannot be adopted by setting the environment names;
    * its environment holds this owner UUID and this exact runtime directory,
      byte for byte -- a prefix, a sibling instance, or a missing variable is
      not ownership.
    """
    if type(identity) is not ProcessIdentity or not _valid_pid(identity.pid):
        return False
    if not _valid_owner(owner_uuid) or not _valid_runtime_directory(runtime_directory):
        return False
    uid = os.getuid() if expected_uid is None else expected_uid
    evidence = read_identity_evidence(identity.pid, proc_root)
    if evidence.identity != identity or evidence.uid != uid:
        return False
    environment = read_environment(identity.pid, proc_root)
    if environment is None:
        return False
    return (
        environment.get(OWNER_TOKEN_ENV) == str(owner_uuid)
        and environment.get(RUNTIME_DIRECTORY_ENV) == runtime_directory
    )


def owned_processes(
    owner_uuid: UUID, runtime_directory: str, proc_root: Path = Path("/proc"),
) -> tuple[ProcessIdentity, ...]:
    """Every live process carrying this owner's exact evidence, ordered by PID.

    This is the crash-window scan the design requires: a helper launched but not
    yet recorded still carries the owner pair in its environment, so it can be
    found without trusting a name, a command line, or "newest process" ordering.
    Names that are not all digits are skipped: procfs has ``self``, ``thread-self``,
    ``sys``, and a handful of plain files next to the PID directories.
    """
    if not _valid_owner(owner_uuid) or not _valid_runtime_directory(runtime_directory):
        return ()
    try:
        names = os.listdir(proc_root)
    except OSError:
        return ()
    found: list[ProcessIdentity] = []
    for name in names:
        if not name or not all(character in _DIGITS for character in name):
            continue
        pid = int(name)
        evidence = read_identity_evidence(pid, proc_root)
        if not evidence.alive or evidence.identity is None:
            continue
        if owner_evidence(evidence.identity, owner_uuid, runtime_directory, proc_root):
            found.append(evidence.identity)
    return tuple(sorted(found, key=lambda item: (item.pid, item.start_ticks)))
