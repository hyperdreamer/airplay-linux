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
  ``absent`` is a claim about the kernel's answer, so it is only made when the
  kernel said ``ENOENT``: a lookup this user is not allowed to perform is
  ``unreadable``, never "gone".
* Ownership is established from *one* instant, so the identity is read again
  after the environment read. Two reads of two different process instances are
  not evidence about either of them.
* Nothing here raises for an ordinary procfs condition. A caller that cannot
  read the evidence needs a typed answer, not an ``OSError`` carrying a path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal
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
#: A command line is only used to decide whether an unreadable environment could
#: hide ownership at all, so the bound only has to cover our own argv.
MAX_COMMAND_LINE_BYTES: Final[int] = 1 << 16

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


class OwnershipState(StrEnum):
    """Whether ownership was proved, disproved, or could not be inspected."""

    owned = "owned"
    not_owned = "not_owned"
    unreadable = "unreadable"


@dataclass(frozen=True, kw_only=True)
class TerminationEvidence:
    """The immutable, stage-specific facts a strict teardown must recheck.

    The process port deliberately receives these facts instead of a receipt.  A
    registry or endpoint verifier owns the authority to produce them, while the
    process runner only compares the expected instance and its freshly observed
    identity before it sends a signal.
    """

    stage: Literal["pre_bind", "bound"] = field(repr=False)
    expected_identity: ProcessIdentity = field(repr=False)
    expected_uid: int = field(repr=False)
    expected_socket_name: str = field(repr=False)
    expected_supervisor: ProcessIdentity | None = field(repr=False)
    expected_entry: tuple[int, int] | None = field(repr=False)
    expected_socket_inode: int | None = field(repr=False)


def termination_evidence_complete(evidence: object) -> bool:
    """Whether ``evidence`` contains every fact required for its stage.

    ``None`` has an explicit meaning in the bound fields: pre-bind has no socket
    entry or kernel socket inode yet, while bound must have both.  The process
    runner uses this predicate before opening a pidfd, so an incomplete caller
    can never turn a partially observed record into a signal authorization.
    """
    if not isinstance(evidence, TerminationEvidence):
        return False
    identity = evidence.expected_identity
    if (
        type(identity) is not ProcessIdentity
        or not _valid_pid(identity.pid)
        or type(identity.start_ticks) is not int
        or identity.start_ticks < 0
    ):
        return False
    if type(evidence.expected_uid) is not int or evidence.expected_uid < 0:
        return False
    if (
        type(evidence.expected_socket_name) is not str
        or not evidence.expected_socket_name
        or "\0" in evidence.expected_socket_name
    ):
        return False

    def valid_identity(value: object) -> bool:
        return (
            value is not None
            and type(value) is ProcessIdentity
            and _valid_pid(value.pid)
            and type(value.start_ticks) is int
            and value.start_ticks >= 0
        )

    def valid_entry(value: object) -> bool:
        return (
            type(value) is tuple
            and len(value) == 2
            and type(value[0]) is int
            and value[0] >= 0
            and type(value[1]) is int
            and value[1] > 0
        )

    if evidence.stage == "pre_bind":
        return (
            valid_identity(evidence.expected_supervisor)
            and evidence.expected_entry is None
            and evidence.expected_socket_inode is None
        )
    if evidence.stage == "bound":
        return (
            valid_entry(evidence.expected_entry)
            and type(evidence.expected_socket_inode) is int
            and evidence.expected_socket_inode > 0
            and (
                evidence.expected_supervisor is None
                or valid_identity(evidence.expected_supervisor)
            )
        )
    return False


@dataclass(frozen=True, kw_only=True)
class EnvironmentEvidence:
    """The environment lookup and whether it was complete enough to trust."""

    state: IdentityState
    environment: dict[str, str] | None = None


@dataclass(frozen=True, kw_only=True)
class OwnedProcessScan:
    """Matches from a procfs scan and whether every candidate was inspected."""

    matches: tuple[ProcessIdentity, ...]
    complete: bool


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


def _read_stat(
    pid: int, proc_root: Path,
) -> tuple[ProcessIdentity, int, str] | IdentityState:
    """The identity, parent, and run state of ``pid``, or why it was not read.

    The line is ``pid (comm) state ppid ...`` where ``comm`` may itself contain
    spaces and parentheses, so the *last* ``)`` is the delimiter and the fields
    after it are positional. A line whose leading PID is not this PID's is not
    accepted, because that is precisely how a recycled PID would arrive.

    A ``FileNotFoundError`` for the stat file is the one failure that is
    *confirmed absence*: the entry was there a moment ago and the kernel no
    longer has it. Every other ``OSError`` -- ``EACCES`` on a procfs entry this
    user may not inspect, ``EIO``, a vanished mount -- is ``unreadable``.
    """
    try:
        text = (_entry(pid, proc_root) / "stat").read_bytes().decode("ascii", "replace")
    except FileNotFoundError:
        return IdentityState.absent
    except OSError:
        return IdentityState.unreadable
    opened = text.find("(")
    if opened < 0:
        return IdentityState.unreadable
    if text[:opened].strip() != str(pid):
        return IdentityState.unreadable
    closed = text.rfind(")")
    if closed < opened:
        return IdentityState.unreadable
    fields = text[closed + 1 :].split()
    if len(fields) < 20:
        return IdentityState.unreadable
    if len(fields[0]) != 1:
        return IdentityState.unreadable
    try:
        parent = int(fields[1])
        start_ticks = int(fields[19])
    except (IndexError, ValueError):
        return IdentityState.unreadable
    if parent < 0 or start_ticks < 0:
        return IdentityState.unreadable
    return ProcessIdentity(pid=pid, start_ticks=start_ticks), parent, fields[0]


def read_identity_evidence(pid: int, proc_root: Path = Path("/proc")) -> IdentityEvidence:
    """Every fact this module can establish about one PID, or why it could not.

    The entry is inspected with an explicit ``stat`` rather than ``exists()``,
    because ``exists()`` answers ``False`` for every lookup failure including a
    permission boundary. Here ``ENOENT`` alone means the process is gone; a
    lookup this user may not perform is ``unreadable``, so a caller can never
    mistake "I was not allowed to look" for "it is not there" and report a
    still-running helper as released.
    """
    if not _valid_pid(pid):
        return IdentityEvidence(pid=pid, state=IdentityState.absent)
    try:
        entry_stat = _entry(pid, proc_root).stat()
    except FileNotFoundError:
        return IdentityEvidence(pid=pid, state=IdentityState.absent)
    except OSError:
        return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
    parsed = _read_stat(pid, proc_root)
    if isinstance(parsed, IdentityState):
        return IdentityEvidence(pid=pid, state=parsed)
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
        uid=entry_stat.st_uid,
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


def read_environment_evidence(
    pid: int, proc_root: Path = Path("/proc"),
) -> EnvironmentEvidence:
    """Read an environment while preserving absence versus inaccessible evidence."""
    if not _valid_pid(pid):
        return EnvironmentEvidence(state=IdentityState.absent)
    path = _entry(pid, proc_root) / "environ"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_ENVIRONMENT_BYTES + 1)
    except FileNotFoundError:
        return EnvironmentEvidence(state=IdentityState.absent)
    except OSError:
        return EnvironmentEvidence(state=IdentityState.unreadable)
    if len(data) > MAX_ENVIRONMENT_BYTES:
        return EnvironmentEvidence(state=IdentityState.unreadable)
    environment: dict[str, str] = {}
    for block in data.split(b"\0"):
        if not block:
            continue
        name, separator, value = block.partition(b"=")
        if not separator:
            continue
        key = os.fsdecode(name)
        if key in environment:
            return EnvironmentEvidence(state=IdentityState.unreadable)
        environment[key] = os.fsdecode(value)
    return EnvironmentEvidence(
        state=IdentityState.present, environment=environment,
    )


def read_environment(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str] | None:
    """The exact environment of ``pid``, or ``None`` when it is not evidence."""
    evidence = read_environment_evidence(pid, proc_root)
    if evidence.state is not IdentityState.present:
        return None
    return evidence.environment


def read_command_line_evidence(
    pid: int, proc_root: Path = Path("/proc"),
) -> tuple[IdentityState, bytes | None]:
    """Read enough of ``ARGV`` to tell whether this process names a runtime.

    The command line is world-readable, so it survives the protected environment
    of a non-dumpable daemon. It is still not ownership evidence: nothing here may
    establish "ours" from argv. Callers use it only to decide whether an
    unreadable environment *could* hide ownership for this process.
    """
    if not _valid_pid(pid):
        return IdentityState.absent, None
    path = _entry(pid, proc_root) / "cmdline"
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_COMMAND_LINE_BYTES + 1)
    except FileNotFoundError:
        return IdentityState.absent, None
    except OSError:
        return IdentityState.unreadable, None
    if len(data) > MAX_COMMAND_LINE_BYTES:
        return IdentityState.unreadable, None
    return IdentityState.present, data


def _names_runtime_directory(pid: int, runtime_directory: str, proc_root: Path) -> bool:
    """Whether ``pid``'s argv names this runtime, or cannot be read at all.

    Every process this application spawns carries the private runtime directory
    in its argv (the supervisor ``--runtime-directory``, the backend
    ``-socket``). A process whose readable argv does not mention it cannot have
    been started into this runtime, so its unreadable environment is not a
    missing ownership fact. An unreadable argv keeps the conservative answer.
    """
    state, data = read_command_line_evidence(pid, proc_root)
    if state is IdentityState.present:
        return os.fsencode(runtime_directory) in (data or b"")
    return state is not IdentityState.absent


def _valid_owner(owner_uuid: object) -> bool:
    return type(owner_uuid) is UUID


def _valid_runtime_directory(runtime_directory: object) -> bool:
    return (
        type(runtime_directory) is str
        and runtime_directory != ""
        and runtime_directory.startswith("/")
        and "\0" not in runtime_directory
    )


def _owner_evidence_state(
    identity: ProcessIdentity,
    owner_uuid: UUID,
    runtime_directory: str,
    proc_root: Path = Path("/proc"),
    expected_uid: int | None = None,
) -> OwnershipState:
    if type(identity) is not ProcessIdentity or not _valid_pid(identity.pid):
        return OwnershipState.not_owned
    if not _valid_owner(owner_uuid) or not _valid_runtime_directory(runtime_directory):
        return OwnershipState.not_owned
    uid = os.getuid() if expected_uid is None else expected_uid
    evidence = read_identity_evidence(identity.pid, proc_root)
    if evidence.state is IdentityState.unreadable:
        return OwnershipState.unreadable
    if (
        evidence.state not in (IdentityState.present, IdentityState.terminated)
        or evidence.identity != identity
        or evidence.uid != uid
    ):
        return OwnershipState.not_owned
    environment = read_environment_evidence(identity.pid, proc_root)
    if environment.state is IdentityState.unreadable:
        return OwnershipState.unreadable
    rechecked = read_identity_evidence(identity.pid, proc_root)
    if rechecked.state is IdentityState.unreadable:
        return OwnershipState.unreadable
    if (
        rechecked.state not in (IdentityState.present, IdentityState.terminated)
        or rechecked.identity != identity
        or rechecked.uid != uid
    ):
        return OwnershipState.not_owned
    if environment.state is not IdentityState.present or environment.environment is None:
        return OwnershipState.not_owned
    if (
        environment.environment.get(OWNER_TOKEN_ENV) == str(owner_uuid)
        and environment.environment.get(RUNTIME_DIRECTORY_ENV) == runtime_directory
    ):
        return OwnershipState.owned
    return OwnershipState.not_owned


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
      not ownership;
    * the identity still matches *after* the environment read, because the two
      reads are two file accesses and a PID can be recycled between them. An
      environment read from one instance and an identity read from another is
      not evidence about either.
    """
    return _owner_evidence_state(
        identity, owner_uuid, runtime_directory, proc_root, expected_uid,
    ) is OwnershipState.owned


def owned_processes(
    owner_uuid: UUID, runtime_directory: str, proc_root: Path = Path("/proc"),
) -> OwnedProcessScan:
    """Scan for exact owner evidence and report whether the scan was complete.

    A numeric procfs entry with unreadable stat or environment evidence makes the
    whole scan incomplete. The registry must retain cleanup ownership in that
    case, because an empty match list cannot distinguish "no owned process" from
    "a process that could not be inspected".
    """
    if not _valid_owner(owner_uuid) or not _valid_runtime_directory(runtime_directory):
        return OwnedProcessScan(matches=(), complete=False)
    try:
        names = os.listdir(proc_root)
    except OSError:
        return OwnedProcessScan(matches=(), complete=False)
    found: list[ProcessIdentity] = []
    complete = True
    for name in names:
        if not name or not all(character in _DIGITS for character in name):
            continue
        pid = int(name)
        evidence = read_identity_evidence(pid, proc_root)
        if evidence.state is IdentityState.absent:
            continue
        if evidence.state is IdentityState.unreadable or evidence.identity is None:
            complete = False
            continue
        if evidence.state is IdentityState.terminated:
            continue
        ownership = _owner_evidence_state(
            evidence.identity, owner_uuid, runtime_directory, proc_root,
        )
        if ownership is OwnershipState.owned:
            found.append(evidence.identity)
        elif ownership is OwnershipState.unreadable and _names_runtime_directory(
            evidence.identity.pid, runtime_directory, proc_root,
        ):
            # An unreadable environment can only hide ownership for a process
            # that names this runtime. Long-lived same-uid daemons with a
            # protected environment (a user systemd manager, the compositor, a
            # session helper) do not, so they never make the scan incomplete.
            complete = False
    return OwnedProcessScan(
        matches=tuple(sorted(found, key=lambda item: (item.pid, item.start_ticks))),
        complete=complete,
    )
