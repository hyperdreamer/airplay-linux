"""Ownership evidence comes from procfs facts, never from a process name.

Every case here is built on a synthetic procfs tree, so the evidence rules are
pinned without depending on what happens to be running on the test machine: a
recycled pid, a ``comm`` full of spaces and parentheses, an environment this user
cannot read, an environment that names the right owner with the wrong runtime
directory, and a process whose name looks exactly like a helper but carries no
owner token at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.process import procfs
from airplay_linux.process.procfs import (
    MAX_ENVIRONMENT_BYTES,
    OWNER_TOKEN_ENV,
    RUNTIME_DIRECTORY_ENV,
    EnvironmentEvidence,
    IdentityEvidence,
    IdentityState,
    owner_evidence,
    owned_processes,
    read_environment,
    read_identity,
    read_identity_evidence,
    read_parent,
    read_uid,
)

OWNER = UUID("11111111-1111-1111-1111-111111111111")
RUNTIME = "/run/user/1000/airplay-linux/instances/owner"

#: ``/proc/<pid>/stat`` is ``pid (comm) state ppid ...``; ``starttime`` is field
#: 22, and every other field is zero here so a mis-indexed read is visible.
_FIELDS_AFTER_COMM = 50


def _fields(*, state: str, ppid: int, start_ticks: int) -> str:
    values = ["0"] * _FIELDS_AFTER_COMM
    values[0] = state  # field 3
    values[1] = str(ppid)  # field 4
    values[19] = str(start_ticks)  # field 22
    return " ".join(values)


def entry(
    root: Path,
    pid: int,
    *,
    comm: str = "python",
    state: str = "S",
    ppid: int = 1,
    start_ticks: int = 22_000,
    environment: dict[str, str] | None = None,
    stat_text: str | None = None,
) -> Path:
    """Write one synthetic ``/proc/<pid>`` entry and return its directory."""
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    text = stat_text
    if text is None:
        text = f"{pid} ({comm}) {_fields(state=state, ppid=ppid, start_ticks=start_ticks)}\n"
    (directory / "stat").write_text(text)
    if environment is not None:
        (directory / "environ").write_bytes(
            b"".join(f"{name}={value}".encode() + b"\0" for name, value in environment.items())
        )
    return directory


def owned_environment(owner: UUID = OWNER, runtime: str = RUNTIME, **extra: str) -> dict[str, str]:
    return {OWNER_TOKEN_ENV: str(owner), RUNTIME_DIRECTORY_ENV: runtime, **extra}


def test_unknown_pid_is_absent(tmp_path: Path):
    assert read_identity(200, tmp_path) is None
    assert read_identity_evidence(200, tmp_path).state is IdentityState.absent


def test_stat_comm_with_spaces_and_parentheses_still_reads_start_ticks(tmp_path: Path):
    entry(tmp_path, 200, comm="a b) c (d", start_ticks=987_654)
    assert read_identity(200, tmp_path) == ProcessIdentity(pid=200, start_ticks=987_654)


def test_stat_with_missing_fields_is_unreadable_rather_than_another_identity(tmp_path: Path):
    entry(tmp_path, 200, stat_text="200 (python) S 1 0\n")
    evidence = read_identity_evidence(200, tmp_path)
    assert evidence.identity is None
    assert evidence.state is IdentityState.unreadable


def test_zombie_entry_is_terminated_rather_than_running(tmp_path: Path):
    entry(tmp_path, 200, state="Z", environment=owned_environment())
    evidence = read_identity_evidence(200, tmp_path)
    assert evidence.identity == ProcessIdentity(pid=200, start_ticks=22_000)
    assert evidence.state is IdentityState.terminated
    assert evidence.alive is False


def test_stat_of_another_pid_is_not_accepted_as_this_pid(tmp_path: Path):
    entry(tmp_path, 200, stat_text=f"201 (python) {_fields(state='S', ppid=1, start_ticks=5)}\n")
    evidence = read_identity_evidence(200, tmp_path)
    assert evidence.identity is None
    assert evidence.state is IdentityState.unreadable


def test_non_numeric_start_ticks_are_not_an_identity(tmp_path: Path):
    values = _fields(state="S", ppid=1, start_ticks=1).split()
    values[19] = "not-a-number"
    entry(tmp_path, 200, stat_text=f"200 (python) {' '.join(values)}\n")
    assert read_identity(200, tmp_path) is None


def test_recycled_pid_reports_the_current_start_ticks(tmp_path: Path):
    entry(tmp_path, 200, start_ticks=1_000)
    assert read_identity(200, tmp_path) == ProcessIdentity(pid=200, start_ticks=1_000)
    entry(tmp_path, 200, start_ticks=2_000)
    assert read_identity(200, tmp_path) == ProcessIdentity(pid=200, start_ticks=2_000)


def test_read_parent_reads_the_fourth_field(tmp_path: Path):
    entry(tmp_path, 200, ppid=4242)
    assert read_parent(200, tmp_path) == 4242
    assert read_parent(201, tmp_path) is None


def test_read_uid_reports_the_entry_owner_and_none_when_absent(tmp_path: Path):
    entry(tmp_path, 200, environment=owned_environment())
    assert read_uid(200, tmp_path) == os.getuid()
    assert read_uid(201, tmp_path) is None


def test_environment_preserves_exact_names_and_values(tmp_path: Path):
    environment = owned_environment(AIRPLAY_LINUX_EMPTY="", PATH="/usr/bin:/bin")
    entry(tmp_path, 200, environment=environment)
    assert read_environment(200, tmp_path) == environment


def test_missing_environment_is_not_evidence(tmp_path: Path):
    entry(tmp_path, 200)
    assert read_environment(200, tmp_path) is None


def test_duplicate_environment_names_are_ambiguous(tmp_path: Path):
    entry(tmp_path, 200)
    (tmp_path / "200" / "environ").write_bytes(
        f"{OWNER_TOKEN_ENV}={OWNER}\0{OWNER_TOKEN_ENV}={UUID(int=2)}\0".encode()
    )
    assert read_environment(200, tmp_path) is None


def test_unreadable_environment_is_not_evidence(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root can read every environment, so EACCES cannot be provoked")
    directory = entry(tmp_path, 200, environment=owned_environment())
    (directory / "environ").chmod(0)
    try:
        assert read_environment(200, tmp_path) is None
    finally:
        (directory / "environ").chmod(0o600)


def test_oversized_environment_is_not_evidence(tmp_path: Path):
    entry(tmp_path, 200)
    (tmp_path / "200" / "environ").write_bytes(
        b"BIG=" + b"x" * (MAX_ENVIRONMENT_BYTES + 1) + b"\0"
    )
    assert read_environment(200, tmp_path) is None


def test_an_inaccessible_entry_is_unreadable_rather_than_absent(tmp_path: Path):
    """"I could not look" is never reported as "it is not there".

    ``Path.exists()`` answers ``False`` for every lookup failure, so a procfs
    entry this user may not inspect used to be classified as gone -- and a
    caller cleaning up would then report a still-running helper as released.
    """
    if os.geteuid() == 0:
        pytest.skip("root can look into every procfs entry, so EACCES cannot be provoked")
    entry(tmp_path, 200, environment=owned_environment())
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    os.chmod(tmp_path, 0o000)
    try:
        evidence = read_identity_evidence(200, tmp_path)
        owned = owner_evidence(identity, OWNER, RUNTIME, tmp_path)
    finally:
        os.chmod(tmp_path, 0o700)
    assert evidence.state is IdentityState.unreadable
    assert evidence.identity is None
    assert evidence.alive is False
    assert owned is False
    # The entry was there all along, which is why "unreadable" is the answer.
    assert read_identity(200, tmp_path) == identity


def test_owner_evidence_rejects_a_pid_recycled_during_the_environment_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """An identity read and an environment read from two instances prove nothing.

    The environment is read after the identity, so a PID recycled in between
    would otherwise lend the recorded start time to a stranger's environment.
    """
    entry(tmp_path, 200, environment=owned_environment())
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is True

    reading = procfs.read_environment_evidence

    def recycling(pid: int, proc_root: Path = Path("/proc")) -> EnvironmentEvidence:
        environment = reading(pid, proc_root)
        # The PID is handed to another instance between the two reads.
        entry(tmp_path, pid, start_ticks=99_000, environment=owned_environment())
        return environment

    monkeypatch.setattr(procfs, "read_environment_evidence", recycling)
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is False


def test_owner_evidence_requires_ticks_uid_and_the_exact_environment(tmp_path: Path):
    entry(tmp_path, 200, start_ticks=22_000, environment=owned_environment(EXTRA="value"))
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is True
    assert owner_evidence(ProcessIdentity(pid=200, start_ticks=22_001), OWNER, RUNTIME, tmp_path) is False
    assert owner_evidence(identity, UUID(int=2), RUNTIME, tmp_path) is False
    assert owner_evidence(identity, OWNER, "/run/user/1000/airplay-linux/instances/other", tmp_path) is False
    assert owner_evidence(identity, OWNER, RUNTIME + "/", tmp_path) is False
    # A sibling instance or a parent path is not this runtime directory: the
    # evidence is the exact string, never a prefix of one.
    assert owner_evidence(identity, OWNER, RUNTIME + "2", tmp_path) is False
    assert owner_evidence(identity, OWNER, RUNTIME + "-sibling", tmp_path) is False
    assert owner_evidence(
        identity, OWNER, "/run/user/1000/airplay-linux/instances", tmp_path,
    ) is False
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path, expected_uid=os.getuid() + 1) is False
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path, expected_uid=os.getuid()) is True


def test_present_state_is_alive(tmp_path: Path):
    entry(tmp_path, 200, environment=owned_environment())
    evidence = read_identity_evidence(200, tmp_path)
    assert evidence.state is IdentityState.present
    assert evidence.alive is True
    assert evidence.uid == os.getuid()


def test_owner_evidence_refuses_foreign_empty_and_missing_environments(tmp_path: Path):
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    entry(tmp_path, 200, environment={OWNER_TOKEN_ENV: str(OWNER)})
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is False
    entry(tmp_path, 200, environment={RUNTIME_DIRECTORY_ENV: RUNTIME})
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is False
    entry(tmp_path, 200, state="Z", environment=None)
    (tmp_path / "200" / "environ").write_bytes(b"")
    assert owner_evidence(identity, OWNER, RUNTIME, tmp_path) is False


def test_owner_evidence_requires_a_uuid_and_an_absolute_runtime_directory(tmp_path: Path):
    entry(tmp_path, 200, environment=owned_environment())
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    assert owner_evidence(identity, str(OWNER), RUNTIME, tmp_path) is False  # type: ignore[arg-type]
    assert owner_evidence(identity, OWNER, "instances/owner", tmp_path) is False
    assert owner_evidence(identity, OWNER, "", tmp_path) is False
    assert owner_evidence("not-an-identity", OWNER, RUNTIME, tmp_path) is False  # type: ignore[arg-type]


def test_owned_processes_reports_incomplete_when_candidate_evidence_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    entry(tmp_path, 100, environment=owned_environment())
    entry(tmp_path, 200, environment=owned_environment())
    reading = procfs.read_identity_evidence

    def unreadable(pid: int, proc_root: Path = Path("/proc")):
        if pid == 200:
            return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
        return reading(pid, proc_root)

    monkeypatch.setattr(procfs, "read_identity_evidence", unreadable)
    scan = owned_processes(OWNER, RUNTIME, tmp_path)

    assert scan.matches == (ProcessIdentity(pid=100, start_ticks=22_000),)
    assert scan.complete is False


def test_owned_processes_reports_incomplete_when_environment_evidence_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    entry(tmp_path, 200, environment=owned_environment())

    def unreadable(pid: int, proc_root: Path = Path("/proc")) -> EnvironmentEvidence:
        return EnvironmentEvidence(state=IdentityState.unreadable)

    monkeypatch.setattr(procfs, "read_environment_evidence", unreadable)
    scan = owned_processes(OWNER, RUNTIME, tmp_path)

    assert scan.matches == ()
    assert scan.complete is False


def test_owned_processes_matches_exact_owner_and_runtime_evidence_only(tmp_path: Path):
    entry(tmp_path, 300, environment=owned_environment())
    entry(tmp_path, 100, comm="krfb-virtualmonitor", environment=owned_environment())
    entry(tmp_path, 200, environment=owned_environment(runtime="/run/user/1000/other"))
    entry(tmp_path, 150, environment=owned_environment(owner=UUID(int=9)))
    # A name that looks like a helper without an owner token is never a match.
    entry(tmp_path, 400, comm="krfb-virtualmonitor", environment={"PATH": "/usr/bin"})
    entry(tmp_path, 500)
    (tmp_path / "self").mkdir()
    (tmp_path / "sys").mkdir()
    (tmp_path / "meminfo").write_text("")

    scan = owned_processes(OWNER, RUNTIME, tmp_path)
    assert scan.matches == (
        ProcessIdentity(pid=100, start_ticks=22_000),
        ProcessIdentity(pid=300, start_ticks=22_000),
    )
    assert scan.complete is True



def test_owned_processes_is_empty_without_a_usable_owner_pair(tmp_path: Path):
    entry(tmp_path, 300, environment=owned_environment())
    scan = owned_processes(str(OWNER), RUNTIME, tmp_path)  # type: ignore[arg-type]
    assert scan.matches == ()
    assert scan.complete is False
    scan = owned_processes(OWNER, "relative/path", tmp_path)
    assert scan.matches == ()
    assert scan.complete is False


def test_owned_processes_tolerates_a_missing_proc_root(tmp_path: Path):
    scan = owned_processes(OWNER, RUNTIME, tmp_path / "absent")
    assert scan.matches == ()
    assert scan.complete is False
