"""Bounded command execution and exact, evidence-checked child termination.

The real-child cases here are deliberately small and harmless: a Python child
that exits, prints, sleeps, or ignores ``SIGTERM``. Every one of them is created
inside a single coroutine whose ``finally`` releases that exact child through the
public port, and no signal is ever sent to a process the runner did not itself
spawn or was not asked to terminate.

Two properties are worth naming, because they are the reason the module is not a
thin wrapper over ``create_subprocess_exec``:

* a command that never stops writing must not deadlock the waiter, so both pipes
  are drained while the process is waited on and each stream is capped at 1 MiB;
* a deadline must kill the exact child it started, and the caller has to be able
  to tell "the child exited" apart from "the child was killed because it ran out
  of time", so a timeout is a typed diagnostic rather than a truncated success.
"""

from __future__ import annotations

import asyncio
import errno
import gc
import os
import signal
import sys
import time
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.domain import ports
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.linux import (
    OUTPUT_LIMIT,
    LinuxProcessRunner,
    inherited_environment,
)
from airplay_linux.process.procfs import (
    OWNER_TOKEN_ENV,
    RUNTIME_DIRECTORY_ENV,
    IdentityState,
    read_identity,
    read_identity_evidence,
)

OWNER = UUID("22222222-2222-2222-2222-222222222222")
RUNTIME = "/run/user/1000/airplay-linux/instances/runner"
OTHER_RUNTIME = "/run/user/1000/airplay-linux/instances/other"


def run(awaitable):
    return asyncio.run(awaitable)


def python(*arguments: str) -> tuple[str, ...]:
    return (sys.executable, *arguments)


def spec(
    *arguments: str, env: dict[str, str] | None = None, cwd: str | None = None,
) -> ports.CommandSpec:
    return ports.CommandSpec(argv=python(*arguments), env=env or {}, cwd=cwd)


def owned_env(**extra: str) -> dict[str, str]:
    return {OWNER_TOKEN_ENV: str(OWNER), RUNTIME_DIRECTORY_ENV: RUNTIME, **extra}


def sleeper(
    pid_file: Path | None = None, seconds: int = 60, *, ignore_sigterm: bool = False,
    ready_file: Path | None = None,
) -> tuple[str, ...]:
    """Arguments for a child that stays alive until it is terminated.

    ``ready_file`` is written *after* any signal disposition is installed, so a
    test that needs the child to already be ignoring ``SIGTERM`` can wait for the
    evidence instead of racing interpreter startup.
    """
    lines = ["import os, signal, time"]
    if ignore_sigterm:
        lines.append("signal.signal(signal.SIGTERM, signal.SIG_IGN)")
    if pid_file is not None:
        lines.append(f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))")
    if ready_file is not None:
        lines.append(f"open({str(ready_file)!r}, 'w').write('ready')")
    lines.append(f"time.sleep({seconds})")
    return python("-c", "\n".join(lines))


def _synthetic_entry(root: Path, pid: int, *, state: str = "S", start_ticks: int = 22_000) -> None:
    """Write one synthetic ``/proc/<pid>`` entry carrying this test's evidence."""
    directory = root / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    values = ["0"] * 50
    values[0] = state
    values[1] = "1"
    values[19] = str(start_ticks)
    (directory / "stat").write_text(f"{pid} (python) {' '.join(values)}\n")
    (directory / "environ").write_bytes(
        f"{OWNER_TOKEN_ENV}={OWNER}\0{RUNTIME_DIRECTORY_ENV}={RUNTIME}\0".encode()
    )


#: Wall-clock bound for a call that must respect its own deadline.
#:
#: A runner that ignores ``timeout`` would otherwise hang the suite instead of
#: failing it, so every deadline case is itself bounded by this much larger one.
CALL_BOUND: float = 30.0


async def wait_for_file(path: Path, timeout: float = 10.0) -> None:
    """Wait until the child has published its own evidence file."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"child never wrote {path.name}")


async def wait_gone(pid: int, timeout: float = 10.0) -> bool:
    """Wait until ``pid`` is absent from procfs, so nothing survived the test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_identity_evidence(pid).state is IdentityState.absent:
            return True
        await asyncio.sleep(0.02)
    return False


# --------------------------------------------------------------------------- run


def test_run_returns_exact_exit_code_and_both_streams():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec(
                "-c",
                "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)",
            ),
            10.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.exit_code == 3
    assert output.value.stdout == b"out"
    assert output.value.stderr == b"err"


def test_run_never_uses_a_shell(tmp_path: Path):
    marker = tmp_path / "must-not-exist"
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec(
                "-c",
                "import sys; sys.stdout.write(sys.argv[1])",
                f"; touch {marker}",
            ),
            10.0,
        )
    )
    assert isinstance(output, Ok)
    # The metacharacters arrived as argv data, not as a shell command line.
    assert output.value.stdout == f"; touch {marker}".encode()
    assert not marker.exists()


def test_run_bounds_stdout_at_one_mebibyte_and_still_exits_normally():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec("-c", f"import sys; sys.stdout.write('x' * ({OUTPUT_LIMIT} * 3))"),
            20.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.exit_code == 0
    assert len(output.value.stdout) == OUTPUT_LIMIT
    assert output.value.stdout == b"x" * OUTPUT_LIMIT


def test_run_bounds_stderr_independently_of_stdout():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec("-c", "import sys; sys.stderr.write('e' * 4096); sys.stdout.write('o' * 4096)"),
            20.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.stdout == b"o" * 4096
    assert output.value.stderr == b"e" * 4096


def test_run_bounds_stderr_at_one_mebibyte_and_still_exits_normally():
    """The cap is a bound on each stream, so stderr needs its own boundary case.

    A drain that honours ``OUTPUT_LIMIT`` for stdout while buffering stderr
    without it would keep every other test here green and still let a chatty
    helper exhaust memory, so this case exceeds the cap on stderr alone.
    """
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec("-c", f"import sys; sys.stderr.write('e' * ({OUTPUT_LIMIT} * 3))"),
            20.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.exit_code == 0
    assert len(output.value.stderr) == OUTPUT_LIMIT
    assert output.value.stderr == b"e" * OUTPUT_LIMIT


def test_run_caps_both_streams_at_the_same_limit_simultaneously():
    """Neither stream may be bounded by the other's budget."""
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec(
                "-c",
                "import sys\n"
                f"sys.stdout.write('o' * ({OUTPUT_LIMIT} * 2))\n"
                f"sys.stderr.write('e' * ({OUTPUT_LIMIT} * 2))\n",
            ),
            20.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.exit_code == 0
    assert output.value.stdout == b"o" * OUTPUT_LIMIT
    assert output.value.stderr == b"e" * OUTPUT_LIMIT


def test_run_timeout_kills_through_the_launch_pidfd_not_a_bare_pid(tmp_path: Path, monkeypatch):
    """Deadline cleanup signals the descriptor taken at launch.

    The child handle's own ``kill`` is a poll-then-``os.kill`` on a bare PID, so
    a recycle between the poll and the signal would kill a stranger. Making that
    call fail proves the cleanup really goes through the descriptor instead.
    """
    if not hasattr(os, "pidfd_open"):
        pytest.skip("this kernel does not offer pidfd_open")
    pid_file = tmp_path / "pid"

    def forbidden_kill(self) -> None:
        raise AssertionError("deadline cleanup used the child handle's bare-PID kill")

    monkeypatch.setattr(asyncio.base_subprocess.BaseSubprocessTransport, "kill", forbidden_kill)

    class Recording(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.signals: list[tuple[bool, int]] = []

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append((pidfd is not None, signum))
            return super()._signal(pidfd, pid, signum)

    async def scenario() -> tuple[Err, int, list[tuple[bool, int]]]:
        runner = Recording()
        output = await asyncio.wait_for(
            runner.run(spec(*sleeper(pid_file)[1:]), 1.0), CALL_BOUND,
        )
        await wait_for_file(pid_file)
        return output, int(pid_file.read_text()), list(runner.signals)

    output, pid, signals = run(scenario())
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.cleanup_timeout
    assert signals == [(True, signal.SIGKILL)]
    assert run(wait_gone(pid))


def test_run_timeout_without_a_pidfd_or_an_identity_signals_nothing(monkeypatch):
    """Cleanup that cannot prove the child never degrades to a raw PID kill.

    The launch identity is unavailable and no descriptor could be opened, so
    the only remaining primitive would be ``os.kill`` on a bare PID -- the one
    thing that can redirect across a recycle. The runner reports the deadline
    instead, and the child handle is left for the test's own reap.
    """

    class FakeChild:
        """A child handle that records the bare-PID kill it must never receive."""

        pid = 4_194_303
        returncode: int | None = None
        stdin = None
        stdout = None
        stderr = None

        def __init__(self) -> None:
            self.killed = False

        async def wait(self) -> int:
            await asyncio.sleep(60)
            return 0

        def kill(self) -> None:
            self.killed = True

    class NoPidfd(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.fake = FakeChild()
            self.signalled: list[int] = []

        async def _exec(self, argv, spec, *, stdin, stdout, stderr):  # type: ignore[override]
            return self.fake

        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

        def _launch_identity(self, pid: int) -> ProcessIdentity | None:
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signalled.append(signum)
            return True

    raw_kills: list[int] = []

    def recording_kill(pid: int, signum: int) -> None:
        raw_kills.append(pid)
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", recording_kill)

    async def scenario() -> tuple[Err, bool, list[int]]:
        runner = NoPidfd()
        output = await asyncio.wait_for(
            runner.run(spec("-c", "pass"), 0.05), CALL_BOUND,
        )
        return output, runner.fake.killed, list(runner.signalled)

    output, killed, signalled = run(scenario())
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.cleanup_timeout
    assert killed is False
    assert signalled == []
    assert raw_kills == []


def test_run_kills_an_endless_writer_at_the_deadline():
    runner = LinuxProcessRunner()

    async def scenario():
        started = time.monotonic()
        bounded = asyncio.ensure_future(
            runner.run(
                spec(
                    "-c",
                    "import sys\nwhile True:\n    sys.stdout.write('y' * 65536)\n    sys.stdout.flush()",
                ),
                1.0,
            )
        )
        try:
            output = await asyncio.wait_for(asyncio.shield(bounded), CALL_BOUND)
        except TimeoutError:
            bounded.cancel()
            raise AssertionError("the runner ignored its deadline and never returned")
        return output, time.monotonic() - started

    output, elapsed = run(scenario())
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.cleanup_timeout
    assert elapsed < 20.0


def test_run_timeout_leaves_no_surviving_child(tmp_path: Path):
    pid_file = tmp_path / "pid"
    runner = LinuxProcessRunner()

    async def scenario() -> tuple[Err, int]:
        # The child records its own PID, so the reap can be confirmed either way.
        bounded = asyncio.ensure_future(runner.run(spec(*sleeper(pid_file)[1:]), 1.0))
        try:
            output = await asyncio.wait_for(asyncio.shield(bounded), CALL_BOUND)
        except TimeoutError:
            bounded.cancel()
            raise AssertionError("the runner ignored its deadline and never returned")
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.02)
        return output, int(pid_file.read_text())

    output, pid = run(scenario())
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.cleanup_timeout
    assert run(wait_gone(pid))


def test_run_timeout_message_names_no_raw_stderr():
    runner = LinuxProcessRunner()

    async def scenario():
        bounded = asyncio.ensure_future(
            runner.run(
                spec("-c", "import sys, time; sys.stderr.write('SECRET-ECHO'); time.sleep(60)"),
                1.0,
            )
        )
        try:
            return await asyncio.wait_for(asyncio.shield(bounded), CALL_BOUND)
        except TimeoutError:
            bounded.cancel()
            raise AssertionError("the runner ignored its deadline and never returned")

    output = run(scenario())
    assert isinstance(output, Err)
    assert "SECRET-ECHO" not in output.error.message
    assert "SECRET-ECHO" not in (output.error.detail or "")
    assert output.error.detail is None


def test_run_missing_command_is_a_typed_dependency_failure():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(ports.CommandSpec(argv=("airplay-linux-absent-command", "--help"), env={}), 5.0),
    )
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.dependency_missing
    assert "airplay-linux-absent-command" not in output.error.message


def test_run_names_an_allowlisted_missing_command():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(ports.CommandSpec(argv=("krfb-virtualmonitor", "--help"), env={}), 5.0),
    )
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.dependency_missing
    assert "krfb-virtualmonitor" in output.error.message


def test_run_reports_a_signalled_exit_exactly_so_cleanup_can_tell_it_apart():
    """A killed helper must not look like a helper that succeeded."""
    runner = LinuxProcessRunner()
    output = run(
        runner.run(spec("-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"), 10.0),
    )
    assert isinstance(output, Ok)
    assert output.value.exit_code == -signal.SIGTERM


def test_run_reports_a_nonzero_exit_as_output_not_as_a_diagnostic():
    runner = LinuxProcessRunner()
    output = run(runner.run(spec("-c", "import sys; sys.exit(17)"), 10.0))
    assert isinstance(output, Ok)
    assert output.value.exit_code == 17


def test_run_delivers_stdin_and_closes_it_for_eof():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec("-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"),
            10.0,
            stdin=b"challenge-response",
        )
    )
    assert isinstance(output, Ok)
    assert output.value.stdout == b"CHALLENGE-RESPONSE"


def test_run_teardown_is_bounded_when_a_grandchild_holds_unread_stdin(tmp_path: Path):
    """Closing our pipe must not wait for an unrelated descendant to read it."""
    ready = tmp_path / "grandchild-ready"
    release = tmp_path / "release"
    done = tmp_path / "grandchild-done"
    os.mkfifo(release)
    code = (
        "import pathlib, subprocess, sys\n"
        "grandchild = (\n"
        "    \"import os, pathlib, sys\\n\"\n"
        "    \"pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\\n\"\n"
        "    \"with open(sys.argv[2], 'rb') as barrier: barrier.read()\\n\"\n"
        "    \"pathlib.Path(sys.argv[3]).write_text('done')\\n\"\n"
        ")\n"
        "subprocess.Popen([sys.executable, '-c', grandchild, "
        "sys.argv[1], sys.argv[2], sys.argv[3]], stdin=sys.stdin, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )

    async def release_barrier() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                descriptor = os.open(release, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
                await asyncio.sleep(0.02)
                continue
            try:
                os.write(descriptor, b"release")
            finally:
                os.close(descriptor)
            return
        raise AssertionError("grandchild never opened the release barrier")

    async def scenario() -> tuple[bool, Ok | Err]:
        runner = LinuxProcessRunner()
        task = asyncio.create_task(
            runner.run(
                spec("-c", code, str(ready), str(release), str(done)),
                1.0,
                stdin=b"x" * (4 * 1024 * 1024),
            )
        )
        try:
            await wait_for_file(ready)
            try:
                await asyncio.wait_for(asyncio.shield(task), 2.0)
            except TimeoutError:
                completed_before_release = False
            else:
                completed_before_release = True
            await release_barrier()
            result = await asyncio.wait_for(asyncio.shield(task), CALL_BOUND)
            await wait_for_file(done)
            assert await wait_gone(int(ready.read_text()))
            return completed_before_release, result
        finally:
            if not done.exists():
                try:
                    await release_barrier()
                except (AssertionError, FileNotFoundError):
                    pass
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    completed_before_release, output = run(scenario())
    assert completed_before_release is True
    assert isinstance(output, Ok)
    assert output.value.exit_code == 0


def test_run_environment_is_the_minimal_inherited_allowlist():
    runner = LinuxProcessRunner()
    output = run(
        runner.run(
            spec(
                "-c",
                "import os; print(os.environ.get('PATH') is not None, "
                "'HOME' in os.environ, 'PYTEST_CURRENT_TEST' in os.environ, "
                "os.environ.get('AIRPLAY_EXTRA', '-'))",
                env={"AIRPLAY_EXTRA": "kept"},
            ),
            10.0,
        )
    )
    assert isinstance(output, Ok)
    assert output.value.stdout == b"True False False kept\n"


def test_run_rejects_an_empty_argv_without_spawning():
    runner = LinuxProcessRunner()
    output = run(runner.run(ports.CommandSpec(argv=(), env={}), 5.0))
    assert isinstance(output, Err)
    assert output.error.code is ErrorCode.dependency_missing


def test_run_honours_cwd(tmp_path: Path):
    runner = LinuxProcessRunner()
    output = run(
        runner.run(spec("-c", "import os; print(os.getcwd())", cwd=str(tmp_path)), 10.0),
    )
    assert isinstance(output, Ok)
    assert output.value.stdout.strip().decode() == str(tmp_path.resolve())


def test_run_propagates_cancellation_and_reaps_the_child(tmp_path: Path):
    pid_file = tmp_path / "pid"

    async def scenario() -> int:
        runner = LinuxProcessRunner()
        task = asyncio.ensure_future(runner.run(spec(*sleeper(pid_file)[1:]), 60.0))
        for _ in range(400):
            if pid_file.exists() and pid_file.read_text():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pid_file.read_text())

    pid = run(scenario())
    assert run(wait_gone(pid))


# --------------------------------------------------------------------------- spawn


def test_spawn_cancellation_during_identity_evidence_reaps_the_unowned_child(tmp_path: Path):
    """Cancellation cannot leave a launched child between spawn ownership states."""

    class EvidenceBlockedRunner(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path / "empty-proc")
            self.evidence_started = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.allow_cleanup = asyncio.Event()
            self.launched: asyncio.subprocess.Process | None = None

        async def _exec(self, argv, spec, *, stdin, stdout, stderr):  # type: ignore[override]
            self.launched = await super()._exec(
                argv, spec, stdin=stdin, stdout=stdout, stderr=stderr,
            )
            return self.launched

        async def _read_spawned_identity(self, pid: int):  # type: ignore[override]
            self.evidence_started.set()
            return await super()._read_spawned_identity(pid)

        async def _kill_direct_child(self, process, pidfd, identity):  # type: ignore[override]
            self.cleanup_started.set()
            await self.allow_cleanup.wait()
            await super()._kill_direct_child(process, pidfd, identity)

    async def scenario() -> tuple[bool, int]:
        runner = EvidenceBlockedRunner()
        task = asyncio.create_task(
            runner.spawn(spec("-c", "import time; time.sleep(60)"))
        )
        await asyncio.wait_for(runner.evidence_started.wait(), 2.0)
        assert runner.launched is not None
        process = runner.launched
        task.cancel()
        await asyncio.wait_for(runner.cleanup_started.wait(), 2.0)
        assert runner.retained_reapers >= 1
        runner.allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), 1.0)
        except TimeoutError:
            disappeared = False
        else:
            disappeared = True
        if process.returncode is None:
            process.kill()
            await process.wait()
        return disappeared, runner.retained_reapers

    disappeared, retained_reapers = run(scenario())
    assert disappeared is True
    assert retained_reapers == 0


def test_spawn_cancellation_without_pidfd_or_evidence_retains_cleanup_ownership(
    tmp_path: Path,
):
    """A launch cleanup cannot target is kept, never finished by dropping it.

    The descriptor is what makes a signal exact, and a freshly read launch
    identity is the only fallback evidence there is. With neither available --
    a kernel that offers no ``pidfd_open``, or one where the open failed, and
    identity evidence that cannot be read -- cleanup has no provable target.
    Completing there would return while a child this runner started is still
    alive, so the obligation has to stay with the runner: a retained reaper for
    that exact child, and the uncertainty recorded under its PID. The child is
    confirmed alive first, then killed and reaped here, because a test may not
    leave a stray process behind to prove a point.
    """

    class UnprovableCleanup(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path / "empty-proc")
            self.evidence_started = asyncio.Event()
            self.launched: asyncio.subprocess.Process | None = None
            self.signals: list[int] = []

        async def _exec(self, argv, spec, *, stdin, stdout, stderr):  # type: ignore[override]
            self.launched = await super()._exec(
                argv, spec, stdin=stdin, stdout=stdout, stderr=stderr,
            )
            return self.launched

        async def _read_spawned_identity(self, pid: int):  # type: ignore[override]
            self.evidence_started.set()
            return await super()._read_spawned_identity(pid)

        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return False

    async def scenario():
        runner = UnprovableCleanup()
        task = asyncio.create_task(
            runner.spawn(spec("-c", "import time; time.sleep(60)"))
        )
        await asyncio.wait_for(runner.evidence_started.wait(), 2.0)
        process = runner.launched
        assert process is not None
        pid = process.pid
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10.0)
            immediate = runner.retained_reapers
            # The obligation has to outlive the cancelled call rather than be
            # recorded for an instant: a silently completed cleanup would
            # already have dropped it by this re-read.
            await asyncio.sleep(0.25)
            observed = (
                tuple(runner.signals),
                read_identity_evidence(pid).state is IdentityState.present,
                immediate,
                runner.retained_reapers,
            )
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(asyncio.shield(process.wait()), 10.0)
        # A confirmed reap is the one fact that ends the retained obligation.
        for _ in range(200):
            if runner.retained_reapers == 0:
                break
            await asyncio.sleep(0.02)
        return (
            pid,
            observed,
            runner.retained_reapers,
            await wait_gone(pid),
        )

    pid, observed, reapers_after, gone = run(scenario())
    signals, alive, immediate, reapers = observed
    assert signals == ()
    assert alive is True
    # Neither silent completion nor an unbacked claim: the child was kept.
    assert immediate >= 1
    assert reapers >= 1
    assert reapers_after == 0
    assert gone is True


def test_spawn_cancellation_with_identity_but_failed_no_pidfd_signal_retains_cleanup_ownership():
    """A failed exact fallback signal must keep the launched child obligation."""

    class SignalBlockedRunner(LinuxProcessRunner):
        kill_timeout = 0.05

        def __init__(self) -> None:
            super().__init__()
            self.evidence_started = asyncio.Event()
            self.launched: asyncio.subprocess.Process | None = None

        async def _exec(self, argv, spec, *, stdin, stdout, stderr):  # type: ignore[override]
            self.launched = await super()._exec(
                argv, spec, stdin=stdin, stdout=stdout, stderr=stderr,
            )
            return self.launched

        async def _read_spawned_identity(self, pid: int):  # type: ignore[override]
            self.evidence_started.set()
            await asyncio.Event().wait()

        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            return False

    async def scenario() -> tuple[int, bool]:
        runner = SignalBlockedRunner()
        task = asyncio.create_task(
            runner.spawn(spec("-c", "import time; time.sleep(60)"))
        )
        await asyncio.wait_for(runner.evidence_started.wait(), 2.0)
        process = runner.launched
        assert process is not None
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10.0)
            await asyncio.sleep(0)
            observed = runner.retained_reapers, process.returncode is None
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(asyncio.shield(process.wait()), 10.0)
            for _ in range(200):
                if runner.retained_reapers == 0:
                    break
                await asyncio.sleep(0.01)
        return observed

    retained, alive = run(scenario())
    assert retained >= 1
    assert alive is True


def test_spawn_cancellation_with_a_pidfd_still_reaps_exactly_the_child(tmp_path: Path):
    """The control for the retained-ownership case: a descriptor needs no fallback.

    Nothing else changes -- the launch is cancelled inside the same identity
    wait, with identity evidence equally unavailable -- so a difference in the
    outcome can only come from the descriptor. Exactly that child must be gone
    and the runner must own nothing afterwards.
    """
    if not hasattr(os, "pidfd_open"):
        pytest.skip("this kernel does not offer pidfd_open")

    class DescriptorCleanup(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path / "empty-proc")
            self.evidence_started = asyncio.Event()
            self.launched: asyncio.subprocess.Process | None = None
            self.signals: list[tuple[bool, int]] = []

        async def _exec(self, argv, spec, *, stdin, stdout, stderr):  # type: ignore[override]
            self.launched = await super()._exec(
                argv, spec, stdin=stdin, stdout=stdout, stderr=stderr,
            )
            return self.launched

        async def _read_spawned_identity(self, pid: int):  # type: ignore[override]
            self.evidence_started.set()
            return await super()._read_spawned_identity(pid)

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append((pidfd is not None, signum))
            return super()._signal(pidfd, pid, signum)

    async def scenario():
        runner = DescriptorCleanup()
        task = asyncio.create_task(
            runner.spawn(spec("-c", "import time; time.sleep(60)"))
        )
        await asyncio.wait_for(runner.evidence_started.wait(), 2.0)
        process = runner.launched
        assert process is not None
        pid = process.pid
        try:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10.0)
            return (
                pid,
                tuple(runner.signals),
                runner.retained_reapers,
                await wait_gone(pid),
            )
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(asyncio.shield(process.wait()), 10.0)

    pid, signals, reapers, gone = run(scenario())
    assert signals == ((True, signal.SIGKILL),)
    assert reapers == 0
    assert gone is True
    assert read_identity(pid) is None


def test_spawn_returns_the_real_identity_and_the_child_exits(tmp_path: Path):
    pid_file = tmp_path / "pid"

    async def scenario() -> ProcessIdentity:
        runner = LinuxProcessRunner()
        spawned = await runner.spawn(spec(*sleeper(pid_file)[1:], env=owned_env()))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        try:
            await wait_for_file(pid_file)
            assert identity.pid == int(pid_file.read_text())
            assert identity.start_ticks > 0
            assert runner.identity(identity.pid) == identity
            assert runner.owned(identity, OWNER, RUNTIME) is True
            assert runner.owned(identity, UUID(int=7), RUNTIME) is False
        finally:
            assert await runner.terminate(identity, OWNER, RUNTIME, deadline=5.0) == Ok(value=None)
        assert await wait_gone(identity.pid)
        return identity

    identity = run(scenario())
    assert read_identity(identity.pid) is None


def test_spawn_missing_command_is_a_typed_failure():
    runner = LinuxProcessRunner()
    result = run(
        runner.spawn(ports.CommandSpec(argv=("airplay-linux-absent-command",), env={})),
    )
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_missing


def test_emitted_exit_is_observed_for_a_spawned_child_that_exits_abruptly():
    runner = LinuxProcessRunner()
    exits: list[ports.ProcessExit] = []

    async def scenario() -> ProcessIdentity:
        result = await runner.spawn(
            spec("-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"),
        )
        assert isinstance(result, Ok)
        subscription = runner.subscribe(exits.append)
        try:
            for _ in range(400):
                if exits:
                    break
                await asyncio.sleep(0.02)
        finally:
            subscription.close()
        assert await wait_gone(result.value.pid)
        return result.value

    identity = run(scenario())
    assert [(event.identity, event.exit_code) for event in exits] == [(identity, -9)]


def test_the_runner_retains_its_reaper_until_the_child_is_reaped():
    """A detached task is held only weakly by the loop; the runner must own it."""
    runner = LinuxProcessRunner()

    async def scenario():
        result = await runner.spawn(spec("-c", "import time; time.sleep(0.3)"))
        assert isinstance(result, Ok)
        retained = runner.retained_reapers
        # The reaper is alive and referenced by the runner, not by the caller.
        assert retained == 1
        assert list(runner._reapers) == [task for task in runner._reapers if not task.done()]
        gc.collect()
        assert runner.retained_reapers == 1
        for _ in range(500):
            if runner.retained_reapers == 0:
                break
            await asyncio.sleep(0.02)
        return retained

    assert run(scenario()) == 1
    assert runner.retained_reapers == 0


def test_a_successfully_reaped_run_is_not_reported_as_an_abrupt_exit():
    runner = LinuxProcessRunner()
    exits: list[ports.ProcessExit] = []
    subscription = runner.subscribe(exits.append)
    try:
        output = run(runner.run(spec("-c", "import sys; sys.exit(4)"), 10.0))
    finally:
        subscription.close()
    assert isinstance(output, Ok)
    assert output.value.exit_code == 4
    assert exits == []


def test_a_closed_subscription_stops_receiving_exits():
    runner = LinuxProcessRunner()
    exits: list[ports.ProcessExit] = []

    async def scenario() -> None:
        subscription = runner.subscribe(exits.append)
        subscription.close()
        subscription.close()
        spawned = await runner.spawn(spec("-c", "pass"))
        assert isinstance(spawned, Ok)
        assert await wait_gone(spawned.value.pid)
        await asyncio.sleep(0.1)

    run(scenario())
    assert exits == []


# --------------------------------------------------------------------------- ownership


def test_identity_of_a_foreign_pid_is_exact_and_not_owned():
    runner = LinuxProcessRunner()
    first = runner.identity(1)
    assert first is not None
    assert first.pid == 1
    assert runner.owned(first, OWNER, RUNTIME) is False
    assert runner.identity(-1) is None
    assert runner.identity(0) is None


def test_terminate_refuses_a_recycled_pid_and_signals_nothing(tmp_path: Path):
    pid_file = tmp_path / "pid"

    async def scenario():
        runner = LinuxProcessRunner()
        spawned = await runner.spawn(spec(*sleeper(pid_file)[1:], env=owned_env()))
        assert isinstance(spawned, Ok)
        live = spawned.value
        try:
            await wait_for_file(pid_file)
            stale = ProcessIdentity(pid=live.pid, start_ticks=live.start_ticks - 1)
            refused = await runner.terminate(stale, OWNER, RUNTIME, deadline=2.0)
            assert read_identity(live.pid) == live
        finally:
            released = await runner.terminate(live, OWNER, RUNTIME, deadline=5.0)
        return refused, released

    refused, released = run(scenario())
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch
    assert released == Ok(value=None)


def test_terminate_refuses_a_foreign_owner_or_runtime_directory():
    async def scenario():
        runner = LinuxProcessRunner()
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)", env=owned_env()))
        assert isinstance(spawned, Ok)
        live = spawned.value
        try:
            wrong_owner = await runner.terminate(live, UUID(int=7), RUNTIME, deadline=2.0)
            wrong_runtime = await runner.terminate(live, OWNER, OTHER_RUNTIME, deadline=2.0)
            wrong_type = await runner.terminate(live, str(OWNER), RUNTIME, deadline=2.0)
            assert read_identity(live.pid) == live
        finally:
            released = await runner.terminate(live, OWNER, RUNTIME, deadline=5.0)
        return wrong_owner, wrong_runtime, wrong_type, released

    wrong_owner, wrong_runtime, wrong_type, released = run(scenario())
    assert [
        wrong_owner.error.code,  # type: ignore[union-attr]
        wrong_runtime.error.code,  # type: ignore[union-attr]
        wrong_type.error.code,  # type: ignore[union-attr]
    ] == [ErrorCode.ownership_mismatch] * 3
    assert released == Ok(value=None)


def test_terminate_escalates_from_sigterm_to_sigkill_after_the_grace_period(tmp_path: Path):
    runner = LinuxProcessRunner()
    exits: list[ports.ProcessExit] = []

    async def scenario():
        ready = tmp_path / "ready"
        spawned = await runner.spawn(
            spec(
                *sleeper(ignore_sigterm=True, ready_file=ready)[1:], env=owned_env(),
            )
        )
        assert isinstance(spawned, Ok)
        for _ in range(400):
            if ready.exists():
                break
            await asyncio.sleep(0.02)
        subscription = runner.subscribe(exits.append)
        try:
            started = time.monotonic()
            terminated = await runner.terminate(spawned.value, OWNER, RUNTIME, deadline=1.0)
            elapsed = time.monotonic() - started
            assert await wait_gone(spawned.value.pid)
        finally:
            subscription.close()
        return spawned.value, terminated, elapsed

    identity, terminated, elapsed = run(scenario())
    assert terminated == Ok(value=None)
    assert elapsed < 15.0
    assert read_identity(identity.pid) is None
    assert [event.exit_code for event in exits] == [-9]


def test_terminate_reports_cleanup_timeout_when_a_child_cannot_be_killed():
    class StubbornRunner(LinuxProcessRunner):
        """Signalling can be blocked, so escalation provably fails."""

        blocked = True

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            if self.blocked:
                return False
            return super()._signal(pidfd, pid, signum)

    async def scenario():
        runner = StubbornRunner()
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)", env=owned_env()))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        try:
            started = time.monotonic()
            blocked = await runner.terminate(identity, OWNER, RUNTIME, deadline=0.2)
            elapsed = time.monotonic() - started
            still_there = read_identity(identity.pid) == identity
            runner.blocked = False
        finally:
            released = await runner.terminate(identity, OWNER, RUNTIME, deadline=5.0)
        assert await wait_gone(identity.pid)
        return blocked, elapsed, still_there, released

    blocked, elapsed, still_there, released = run(scenario())
    assert isinstance(blocked, Err)
    assert blocked.error.code is ErrorCode.cleanup_timeout
    assert elapsed < 15.0
    assert still_there is True
    assert released == Ok(value=None)


def test_signal_with_an_open_pidfd_fails_closed_when_the_python_wrapper_is_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = LinuxProcessRunner()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(signal, "pidfd_send_signal", None, raising=False)
    monkeypatch.setattr(os, "kill", lambda pid, signum: sent.append((pid, signum)))

    assert runner._signal(123, 456, signal.SIGKILL) is False
    assert sent == []


def test_terminate_signals_the_validated_instance_through_a_pidfd():
    if not hasattr(os, "pidfd_open"):
        pytest.skip("this kernel does not offer pidfd_open")

    class Recording(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.signals: list[tuple[bool, int]] = []

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append((pidfd is not None, signum))
            return super()._signal(pidfd, pid, signum)

    async def scenario():
        runner = Recording()
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)", env=owned_env()))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        terminated = await runner.terminate(identity, OWNER, RUNTIME, deadline=2.0)
        gone = await wait_gone(identity.pid)
        return runner.signals, terminated, gone

    signals, terminated, gone = run(scenario())
    assert signals == [(True, signal.SIGTERM)]
    assert terminated == Ok(value=None)
    assert gone is True


def test_pidfd_opening_failure_falls_back_to_the_evidence_checked_pid():
    class NoPidfd(LinuxProcessRunner):
        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

    async def scenario():
        runner = NoPidfd()
        spawned = await runner.spawn(
            spec(
                "-c",
                "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n",
                env=owned_env(),
            )
        )
        assert isinstance(spawned, Ok)
        identity = spawned.value
        terminated = await runner.terminate(identity, OWNER, RUNTIME, deadline=0.5)
        gone = await wait_gone(identity.pid)
        return terminated, gone

    terminated, gone = run(scenario())
    assert terminated == Ok(value=None)
    assert gone is True


def test_a_zombie_with_matching_evidence_is_accepted_without_a_signal(tmp_path: Path):
    """A terminated entry cannot run, so it is gone rather than owned-and-live."""
    _synthetic_entry(tmp_path, 200, state="Z")

    class Recording(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return True

    runner = Recording()
    identity = ProcessIdentity(pid=200, start_ticks=22_000)
    assert runner.identity(200) == identity
    # The environment and UID evidence both match; only the run state says gone.
    assert runner.owned(identity, OWNER, RUNTIME) is True
    assert run(runner.terminate(identity, OWNER, RUNTIME, deadline=1.0)) == Ok(value=None)
    assert runner.signals == []


def test_terminate_of_an_unreadable_procfs_entry_is_a_mismatch_not_a_release(tmp_path: Path):
    """"I could not look" must never be reported as "it is gone".

    An inaccessible entry leaves the recorded process unproven, so cleanup is a
    mismatch the caller can retry -- not ``Ok``, which would claim a helper was
    released while it may still be running.
    """
    if os.geteuid() == 0:
        pytest.skip("root can look into every procfs entry, so EACCES cannot be provoked")
    _synthetic_entry(tmp_path, 200)
    identity = ProcessIdentity(pid=200, start_ticks=22_000)

    class Recording(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return True

    runner = Recording()
    os.chmod(tmp_path, 0o000)
    try:
        refused = run(runner.terminate(identity, OWNER, RUNTIME, deadline=1.0))
    finally:
        os.chmod(tmp_path, 0o700)
    assert runner.signals == []
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch


def test_terminate_revalidates_evidence_immediately_before_the_fallback_sigterm(
    tmp_path: Path,
):
    """A PID handed over between the first check and the signal is never hit.

    Without a descriptor the only defence is evidence re-read at the instant of
    the signal, so the recycle is forced in exactly that window: nothing may be
    signalled, and the caller learns the release is unproven.
    """
    _synthetic_entry(tmp_path, 200)
    identity = ProcessIdentity(pid=200, start_ticks=22_000)

    class RecycledBeforeFallback(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []

        def _open_pidfd(self, pid: int) -> int | None:
            # The first evidence check passed and no descriptor is available, so
            # the bare-PID path is what would be attempted next.
            _synthetic_entry(tmp_path, pid, start_ticks=99_000)
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return True

    runner = RecycledBeforeFallback()
    refused = run(runner.terminate(identity, OWNER, RUNTIME, deadline=1.0))
    assert runner.signals == []
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch


def test_a_pid_recycled_after_sigterm_is_not_escalated_to_sigkill(tmp_path: Path):
    """Escalation re-validates first: a reused PID never receives the kill."""
    _synthetic_entry(tmp_path, 200)
    identity = ProcessIdentity(pid=200, start_ticks=22_000)

    class RecycledOnSigterm(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []

        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            if signum == signal.SIGTERM:
                # The graceful wait expires while the PID is handed to someone
                # else; only the re-validation stands between them and SIGKILL.
                _synthetic_entry(tmp_path, pid, start_ticks=99_000)
            return True

    runner = RecycledOnSigterm()
    refused = run(runner.terminate(identity, OWNER, RUNTIME, deadline=0.05))
    assert runner.signals == [signal.SIGTERM]
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch


def test_a_pid_recycled_while_the_pidfd_opened_is_never_signalled(tmp_path: Path):
    """The descriptor and the evidence must name the same instance before a signal."""
    _synthetic_entry(tmp_path, 200)
    identity = ProcessIdentity(pid=200, start_ticks=22_000)

    class RecycledDuringOpen(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []
            self.opened = 0

        def _open_pidfd(self, pid: int) -> int | None:
            self.opened += 1
            # The PID is reused between the evidence read and the descriptor:
            # the same PID now names a different process instance.
            _synthetic_entry(tmp_path, pid, start_ticks=99_000)
            return os.open(os.devnull, os.O_RDONLY)

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return True

    runner = RecycledDuringOpen()
    refused = run(runner.terminate(identity, OWNER, RUNTIME, deadline=1.0))
    assert runner.opened == 1
    assert runner.signals == []
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch


def test_without_a_pidfd_a_foreign_process_is_still_never_signalled(tmp_path: Path):
    """The fallback path validates evidence itself; it does not trust the caller."""
    _synthetic_entry(tmp_path, 200)
    (tmp_path / "200" / "environ").write_bytes(
        f"{OWNER_TOKEN_ENV}={OWNER}\0{RUNTIME_DIRECTORY_ENV}={OTHER_RUNTIME}\0".encode()
    )
    identity = ProcessIdentity(pid=200, start_ticks=22_000)

    class NoPidfd(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__(proc_root=tmp_path)
            self.signals: list[int] = []

        @staticmethod
        def _open_pidfd(pid: int) -> int | None:
            return None

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return True

    runner = NoPidfd()
    assert runner.owned(identity, OWNER, RUNTIME) is False
    refused = run(runner.terminate(identity, OWNER, RUNTIME, deadline=1.0))
    assert runner.signals == []
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch


def test_a_refused_terminate_sends_no_signal_at_all():
    class Recording(LinuxProcessRunner):
        def __init__(self) -> None:
            super().__init__()
            self.signals: list[int] = []

        def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
            self.signals.append(signum)
            return super()._signal(pidfd, pid, signum)

    async def scenario():
        runner = Recording()
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)", env=owned_env()))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        try:
            await runner.terminate(
                ProcessIdentity(pid=identity.pid, start_ticks=identity.start_ticks + 1),
                OWNER, RUNTIME, deadline=1.0,
            )
            await runner.terminate(identity, UUID(int=7), RUNTIME, deadline=1.0)
            signed = list(runner.signals)
        finally:
            released = await runner.terminate(identity, OWNER, RUNTIME, deadline=5.0)
        return signed, released

    signed, released = run(scenario())
    assert signed == []
    assert released == Ok(value=None)


def test_terminate_accepts_an_already_exited_child_as_absent():
    async def scenario():
        runner = LinuxProcessRunner()
        spawned = await runner.spawn(spec("-c", "pass", env=owned_env()))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        for _ in range(400):
            state = read_identity_evidence(identity.pid).state
            if state in (IdentityState.absent, IdentityState.terminated):
                break
            await asyncio.sleep(0.02)
        return await runner.terminate(identity, OWNER, RUNTIME, deadline=5.0)

    assert run(scenario()) == Ok(value=None)


def test_terminate_refuses_a_foreign_process_that_merely_shares_a_name():
    async def scenario():
        runner = LinuxProcessRunner()
        # Spawn without the owner environment: same executable, no ownership evidence.
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)"))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        try:
            terminated = await runner.terminate(identity, OWNER, RUNTIME, deadline=1.0)
            alive = read_identity(identity.pid) == identity
        finally:
            try:
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert await wait_gone(identity.pid)
        return terminated, alive

    terminated, alive = run(scenario())
    assert isinstance(terminated, Err)
    assert terminated.error.code is ErrorCode.ownership_mismatch
    assert alive is True


def test_terminate_of_an_unverifiable_environment_is_a_mismatch_not_a_kill():
    async def scenario():
        runner = LinuxProcessRunner()
        spawned = await runner.spawn(spec("-c", "import time; time.sleep(60)"))
        assert isinstance(spawned, Ok)
        identity = spawned.value
        try:
            refused = await runner.terminate(identity, OWNER, "relative/runtime", deadline=1.0)
            alive = read_identity(identity.pid) == identity
        finally:
            try:
                os.kill(identity.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert await wait_gone(identity.pid)
        return refused, alive

    refused, alive = run(scenario())
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.ownership_mismatch
    assert alive is True


# --------------------------------------------------------------------------- helpers


def test_inherited_environment_keeps_the_allowlist_and_drops_everything_else(monkeypatch):
    monkeypatch.setenv("PATH", "/bin:/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("SECRET_TOKEN", "never-inherited")
    environment = inherited_environment({"AIRPLAY_EXTRA": "kept"})
    assert environment["PATH"] == "/bin:/usr/bin"
    assert environment["LANG"] == "C.UTF-8"
    assert environment["AIRPLAY_EXTRA"] == "kept"
    assert "SECRET_TOKEN" not in environment
    assert "PYTEST_CURRENT_TEST" not in environment


def test_inherited_environment_refuses_a_name_or_value_that_cannot_be_executed():
    environment = inherited_environment(
        {"AIRPLAY_EMPTY": "", "AIRPLAY_NUL": "a\0b", "": "nameless", "AIRPLAY_INT": 7},  # type: ignore[dict-item]
    )
    assert environment["AIRPLAY_EMPTY"] == ""
    assert "AIRPLAY_NUL" not in environment
    assert "" not in environment
    assert "AIRPLAY_INT" not in environment
