"""Owned virtual-monitor helpers: probed capabilities and exact ownership evidence.

``krfb-virtualmonitor`` is not installed on the reference machine, so every test
drives the published argument contract through :class:`FakeProcess`. The one test
that needs the production owner scan builds a synthetic procfs tree, so nothing
here starts a real helper or reads a real process environment.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.helper import (
    HelperFeatures,
    VirtualHelper,
)
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV,
    RUNTIME_DIRECTORY_ENV,
    owner_evidence,
    read_identity,
)
from airplay_linux.testing.adapters import FakeProcess
from airplay_linux.testing.time import FakeClock

OWNER = UUID("11111111-1111-1111-1111-111111111111")
OTHER_OWNER = UUID("22222222-2222-2222-2222-222222222222")
RUNTIME = "/run/user/1000/airplay-linux/instances/owner"
OTHER_RUNTIME = "/run/user/1000/airplay-linux/instances/other"
SIZE = Size(width=1920, height=1080)

#: The documented ``krfb-virtualmonitor --help`` contract this adapter relies on.
HELP = """Usage: krfb-virtualmonitor [options]

Options:
  -h, --help                Displays help on commandline options.
  --help-all                Displays help, including generic Qt options.
  -v, --version             Displays version information.
  --resolution <WxH>        The resolution of the virtual monitor.
  --name <name>             The name of the virtual monitor.
  --scale <scale>           The scale factor of the virtual monitor.
  --desktopfile <path>      The desktop file name.
"""

VERSION = "krfb-virtualmonitor 24.08.2\n"


def output(exit_code: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> Ok[CommandOutput]:
    return Ok(value=CommandOutput(exit_code=exit_code, stdout=stdout, stderr=stderr))


def help_result(text: str = HELP, exit_code: int = 0) -> Ok[CommandOutput]:
    return output(exit_code=exit_code, stdout=text.encode("utf-8"))


def version_result(text: str = VERSION) -> Ok[CommandOutput]:
    return output(stdout=text.encode("utf-8"))


async def probed(process: FakeProcess, clock: FakeClock | None = None) -> VirtualHelper:
    helper = VirtualHelper(process, clock or FakeClock())
    process.queue_result("run", help_result())
    process.queue_result("run", version_result())
    features = await helper.probe()
    assert isinstance(features, Ok), features
    return helper


def model(*, owner: UUID = OWNER, runtime: str = RUNTIME) -> FakeProcess:
    process = FakeProcess()
    process.register(
        ProcessIdentity(pid=5001, start_ticks=100),
        {OWNER_TOKEN_ENV: str(owner), RUNTIME_DIRECTORY_ENV: runtime},
    )
    return process


# --------------------------------------------------------------------------- probe


async def test_probe_reports_dependency_missing_with_the_exact_command():
    process = FakeProcess()
    process.queue_result(
        "run", Err(error=diagnostic(ErrorCode.dependency_missing, command="krfb-virtualmonitor")),
    )
    helper = VirtualHelper(process, FakeClock())
    result = await helper.probe()
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_missing
    assert "krfb-virtualmonitor" in result.error.message
    assert process.spawned == []


async def test_probe_rejects_help_that_does_not_document_resolution():
    process = FakeProcess()
    process.queue_result("run", help_result("Usage: krfb-virtualmonitor\n  -h, --help\n"))
    helper = VirtualHelper(process, FakeClock())
    result = await helper.probe()
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_incompatible


async def test_probe_rejects_a_help_exit_status_that_is_not_success():
    process = FakeProcess()
    process.queue_result("run", help_result(exit_code=1))
    helper = VirtualHelper(process, FakeClock())
    result = await helper.probe()
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_incompatible


async def test_probe_claims_only_the_documented_single_output_profile():
    process = FakeProcess()
    helper = await probed(process)
    features = await helper.probe()
    assert isinstance(features, Ok)
    feature_set = features.value
    assert isinstance(feature_set, HelperFeatures)
    assert feature_set.version == "24.08.2"
    assert feature_set.distinct_outputs is False
    assert len(feature_set.capability_fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in feature_set.capability_fingerprint)
    # The successful probe is cached: a second call does not run the helper again.
    assert len(process.runs) == 2


async def test_probe_tolerates_an_absent_version_banner():
    process = FakeProcess()
    process.queue_result("run", help_result())
    helper = VirtualHelper(process, FakeClock())
    result = await helper.probe()
    assert isinstance(result, Ok)
    assert result.value.version == "unknown"
    assert result.value.distinct_outputs is False
    assert len(result.value.capability_fingerprint) == 64


async def test_helper_rejects_a_command_outside_the_diagnostics_allowlist():
    with pytest.raises(ValueError):
        VirtualHelper(FakeProcess(), FakeClock(), command="sh")


async def test_probe_is_capability_fingerprinted_by_documented_contract_and_version():
    first_process = FakeProcess()
    first = await probed(first_process)
    second_process = FakeProcess()
    second = await probed(second_process)
    third_process = FakeProcess()
    third_process.queue_result("run", help_result())
    third_process.queue_result("run", version_result("krfb-virtualmonitor 25.01.0\n"))
    third = VirtualHelper(third_process, FakeClock())
    third_result = await third.probe()
    assert isinstance(third_result, Ok)
    first_value = (await first.probe()).value  # type: ignore[union-attr]
    second_value = (await second.probe()).value  # type: ignore[union-attr]
    third_value = third_result.value
    assert first_value.capability_fingerprint == second_value.capability_fingerprint
    assert first_value.capability_fingerprint != third_value.capability_fingerprint


# --------------------------------------------------------------------------- launch


async def test_launch_carries_the_owner_environment_and_a_documented_resolution():
    process = FakeProcess()
    helper = await probed(process)
    result = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(result, Ok)
    identity = result.value
    (spec,) = process.spawned
    assert spec.argv == ("krfb-virtualmonitor", "--resolution", "1920x1080")
    assert OWNER_TOKEN_ENV == "AIRPLAY_LINUX_OWNER_UUID"
    assert RUNTIME_DIRECTORY_ENV == "AIRPLAY_LINUX_RUNTIME_DIRECTORY"
    assert dict(spec.env) == {
        "AIRPLAY_LINUX_OWNER_UUID": str(OWNER),
        "AIRPLAY_LINUX_RUNTIME_DIRECTORY": RUNTIME,
    }
    assert str(OWNER) not in " ".join(spec.argv)
    assert identity.start_ticks > 0
    assert process.owned(identity, OWNER, RUNTIME)
    assert process.identity(identity.pid) == identity


async def test_launch_uses_the_requested_size_and_invents_no_extra_flags():
    process = FakeProcess()
    helper = await probed(process)
    result = await helper.launch(OWNER, RUNTIME, Size(width=2560, height=1440))
    assert isinstance(result, Ok)
    (spec,) = process.spawned
    assert spec.argv == ("krfb-virtualmonitor", "--resolution", "2560x1440")
    assert "--password" not in spec.argv
    assert "--port" not in spec.argv


async def test_launch_never_spawns_without_a_supported_helper_profile():
    process = FakeProcess()
    process.queue_result(
        "run", Err(error=diagnostic(ErrorCode.dependency_missing, command="krfb-virtualmonitor")),
    )
    helper = VirtualHelper(process, FakeClock())
    result = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.dependency_missing
    assert process.spawned == []


async def test_launch_refuses_a_second_helper_for_the_same_reservation():
    process = FakeProcess()
    helper = await probed(process)
    first = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(first, Ok)
    second = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(second, Err)
    assert second.error.code == ErrorCode.helper_failed
    assert len(process.spawned) == 1
    assert process.alive(first.value)


async def test_launch_allows_a_replacement_after_the_first_helper_exited():
    process = FakeProcess()
    helper = await probed(process)
    first = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(first, Ok)
    process.exit(first.value)
    second = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(second, Ok)
    assert second.value != first.value
    assert len(process.spawned) == 2


async def test_launch_rejects_malformed_ownership_inputs_without_spawning():
    process = FakeProcess()
    helper = await probed(process)
    for owner, runtime in (
        ("not-a-uuid", RUNTIME),  # type: ignore[arg-type]
        (OWNER, "relative/runtime"),
        (OWNER, ""),
        (OWNER, "/run/user/1000/airplay\0-linux"),
    ):
        result = await helper.launch(owner, runtime, SIZE)  # type: ignore[arg-type]
        assert isinstance(result, Err)
        assert result.error.code == ErrorCode.invalid_input
    assert process.spawned == []


async def test_launch_refuses_an_identity_it_cannot_prove_is_owned():
    class UntrustingProcess(FakeProcess):
        def owned(
            self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
        ) -> bool:
            return False

    process = UntrustingProcess()
    helper = await probed(process)
    result = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    assert len(process.spawned) == 1
    # Cleanup was attempted through the ownership-checked terminate boundary, which
    # refused to signal anything it could not prove.
    assert process.terminated == []


# --------------------------------------------------------------------------- find_owned


async def test_find_owned_returns_only_exact_owner_and_runtime_matches():
    process = FakeProcess()
    helper = await probed(process)
    mine = await helper.launch(OWNER, RUNTIME, SIZE)
    other = await helper.launch(OTHER_OWNER, RUNTIME, SIZE)
    assert isinstance(mine, Ok) and isinstance(other, Ok)
    assert helper.find_owned(OWNER, RUNTIME) == (mine.value,)
    assert helper.find_owned(OTHER_OWNER, RUNTIME) == (other.value,)
    assert helper.find_owned(OWNER, OTHER_RUNTIME) == ()
    assert helper.find_owned(OTHER_OWNER, OTHER_RUNTIME) == ()


async def test_find_owned_returns_empty_for_an_unknown_owner():
    process = FakeProcess()
    helper = await probed(process)
    assert helper.find_owned(OWNER, RUNTIME) == ()
    assert helper.find_owned("bad", RUNTIME) == ()  # type: ignore[arg-type]


async def test_a_recycled_pid_is_neither_claimed_nor_signalled():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    # The recorded instance is gone and an unrelated process now owns the PID.
    recycled = ProcessIdentity(pid=launched.value.pid, start_ticks=launched.value.start_ticks + 1)
    process.register(recycled, {"PATH": "/usr/bin"})
    assert helper.find_owned(OWNER, RUNTIME) == ()
    result = await helper.stop(launched.value, OWNER, RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    assert process.terminated == []
    assert process.journal.calls("process.terminate") == []
    assert process.alive(recycled)


async def test_find_owned_never_claims_a_process_without_owner_evidence():
    process = FakeProcess()
    helper = await probed(process)
    process.register(ProcessIdentity(pid=5100, start_ticks=200), {"PATH": "/usr/bin"})
    assert helper.find_owned(OWNER, RUNTIME) == ()


async def test_find_owned_scans_procfs_when_the_port_has_no_environment_table(tmp_path: Path):
    entry(tmp_path, 4321, start_ticks=777, environment={
        OWNER_TOKEN_ENV: str(OWNER), RUNTIME_DIRECTORY_ENV: RUNTIME,
    })
    entry(tmp_path, 4322, start_ticks=778, environment={
        OWNER_TOKEN_ENV: str(OWNER), RUNTIME_DIRECTORY_ENV: OTHER_RUNTIME,
    })
    entry(tmp_path, 4323, start_ticks=779, environment={"PATH": "/usr/bin"})
    process = ProcfsOnlyProcess(tmp_path)
    helper = VirtualHelper(process, FakeClock(), proc_root=tmp_path)
    assert helper.find_owned(OWNER, RUNTIME) == (ProcessIdentity(pid=4321, start_ticks=777),)


# --------------------------------------------------------------------------- stop


async def test_stop_terminates_the_exact_owned_helper():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    result = await helper.stop(launched.value, OWNER, RUNTIME, 5.0)
    assert isinstance(result, Ok)
    assert process.terminated == [launched.value]
    assert process.identity(launched.value.pid) is None
    (terminated,) = process.journal.calls("process.terminate")
    assert terminated["identity"] == {
        "pid": launched.value.pid, "start_ticks": launched.value.start_ticks,
    }
    assert terminated["owner_uuid"] == OWNER
    assert terminated["runtime_directory"] == RUNTIME
    assert terminated["deadline"] == 5.0


async def test_stop_never_signals_a_helper_owned_by_another_reservation():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    result = await helper.stop(launched.value, OTHER_OWNER, RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    assert process.terminated == []
    assert process.journal.calls("process.terminate") == []
    assert process.alive(launched.value)


async def test_stop_never_signals_a_helper_from_another_runtime_directory():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    result = await helper.stop(launched.value, OWNER, OTHER_RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    assert process.terminated == []
    assert process.alive(launched.value)


async def test_stop_never_signals_a_wrong_pid_or_start_time():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    wrong_start = ProcessIdentity(
        pid=launched.value.pid, start_ticks=launched.value.start_ticks + 1,
    )
    result = await helper.stop(wrong_start, OWNER, RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    # A PID that does not exist cannot be the recorded instance any more, so the
    # release is already confirmed -- but nothing is signalled.
    absent_pid = ProcessIdentity(
        pid=launched.value.pid + 100, start_ticks=launched.value.start_ticks,
    )
    assert isinstance(await helper.stop(absent_pid, OWNER, RUNTIME, 5.0), Ok)
    assert process.terminated == []
    assert process.journal.calls("process.terminate") == []
    assert process.alive(launched.value)


async def test_stop_never_signals_a_preexisting_named_process():
    process = FakeProcess()
    helper = await probed(process)
    stranger = ProcessIdentity(pid=5200, start_ticks=300)
    process.register(stranger, {})
    result = await helper.stop(stranger, OWNER, RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
    assert process.terminated == []
    assert process.alive(stranger)


async def test_stop_is_idempotent_after_the_helper_already_exited():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    process.exit(launched.value)
    result = await helper.stop(launched.value, OWNER, RUNTIME, 5.0)
    assert isinstance(result, Ok)
    assert process.terminated == []


async def test_stop_rejects_malformed_deadlines_and_ownership_inputs():
    process = FakeProcess()
    helper = await probed(process)
    launched = await helper.launch(OWNER, RUNTIME, SIZE)
    assert isinstance(launched, Ok)
    for deadline in (float("nan"), float("inf"), -1.0, 0.0):
        result = await helper.stop(launched.value, OWNER, RUNTIME, deadline)
        assert isinstance(result, Err)
        assert result.error.code == ErrorCode.invalid_input
    for owner, runtime in (
        ("not-a-uuid", RUNTIME),  # type: ignore[arg-type]
        (OWNER, "relative/runtime"),
    ):
        result = await helper.stop(launched.value, owner, runtime, 5.0)  # type: ignore[arg-type]
        assert isinstance(result, Err)
        assert result.error.code == ErrorCode.invalid_input
    assert process.terminated == []
    assert process.alive(launched.value)


# --------------------------------------------------------------------------- synthetic procfs


def entry(
    root: Path, pid: int, *, start_ticks: int, environment: dict[str, str],
) -> None:
    """One synthetic ``/proc/<pid>`` with a stat line and an environment block."""
    directory = root / str(pid)
    directory.mkdir()
    fields = ["0"] * 50
    fields[0] = "S"
    fields[1] = "1"
    fields[19] = str(start_ticks)
    (directory / "stat").write_text(f"{pid} (krfb-virtualmonitor) {' '.join(fields)}\n")
    (directory / "environ").write_bytes(
        b"".join(f"{name}={value}\0".encode() for name, value in environment.items()),
    )


class ProcfsOnlyProcess:
    """The production ownership scan, with no fake environment table."""

    def __init__(self, proc_root: Path) -> None:
        self._proc_root = proc_root
        self.terminated: list[ProcessIdentity] = []

    def identity(self, pid: int) -> ProcessIdentity | None:
        return read_identity(pid, self._proc_root)

    def owned(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str) -> bool:
        return owner_evidence(
            identity, owner_uuid, runtime_directory, self._proc_root, os.getuid(),
        )

    async def run(self, spec: Any, timeout: float, stdin: bytes | None = None) -> Any:
        raise AssertionError("the production-scan test must not run a command")

    async def spawn(self, spec: Any) -> Any:
        raise AssertionError("the production-scan test must not spawn a process")

    async def terminate(
        self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
        deadline: float,
    ) -> Any:
        raise AssertionError("the production-scan test must not signal a process")


async def test_owned_scan_reports_an_uninspectable_entry_as_incomplete(tmp_path: Path):
    entry(tmp_path, 4321, start_ticks=777, environment={
        OWNER_TOKEN_ENV: str(OWNER), RUNTIME_DIRECTORY_ENV: RUNTIME,
    })
    broken = tmp_path / "6000"
    broken.mkdir()
    (broken / "stat").write_text("this is not a procfs stat line\n")
    (broken / "environ").write_bytes(b"")
    process = ProcfsOnlyProcess(tmp_path)
    helper = VirtualHelper(process, FakeClock(), proc_root=tmp_path)
    scan = helper.owned_scan(OWNER, RUNTIME)
    assert scan.matches == (ProcessIdentity(pid=4321, start_ticks=777),)
    assert scan.complete is False
    assert helper.find_owned(OWNER, RUNTIME) == scan.matches
    assert helper.owned_scan("bad", RUNTIME).complete is False  # type: ignore[arg-type]


async def test_stop_never_reports_an_unreadable_procfs_entry_as_released(tmp_path: Path):
    broken = tmp_path / "6000"
    broken.mkdir()
    (broken / "stat").write_text("this is not a procfs stat line\n")
    (broken / "environ").write_bytes(b"")
    process = ProcfsOnlyProcess(tmp_path)
    helper = VirtualHelper(process, FakeClock(), proc_root=tmp_path)
    result = await helper.stop(ProcessIdentity(pid=6000, start_ticks=10), OWNER, RUNTIME, 5.0)
    assert isinstance(result, Err)
    assert result.error.code == ErrorCode.ownership_mismatch
