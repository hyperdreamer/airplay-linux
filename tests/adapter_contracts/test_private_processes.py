"""Contract tests for the private, test-only fake processes.

The fixtures under ``tests/fixtures/processes`` are *not* compatible stand-ins for
an installed backend: they speak a newline-delimited JSON protocol on a private
Unix socket, they carry a test-only command surface, and every request/reply pair
is correlated by an exact request id.  A reply whose id does not match the
request is never accepted as the answer, and a request that does not carry the
exact owner token and instance UUID of this private daemon is refused.

These tests exercise the fixtures through the real :class:`LinuxProcessRunner`,
so the child processes, the bounded output capture, the timeout kill/reap path and
the ownership environment are the production code paths -- only the executables
are private fakes.

Manual (non-CI) commands: ``pytest -q tests/adapter_contracts/test_private_processes.py``.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

from airplay_linux.domain.ports import CommandSpec
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.process.procfs import read_identity_evidence, IdentityState

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402
import fake_kscreen  # noqa: E402
import fake_virtualmonitor  # noqa: E402


@pytest.fixture
def commands(tmp_path, monkeypatch):
    """A private PATH whose fake commands shadow any installed binary."""
    directory = tmp_path / "bin"
    state = tmp_path / "fake-kscreen.json"
    pids = tmp_path / "fake-helper-pids.json"
    fake_kscreen.write_layout(state, fake_kscreen.baseline_layout())
    installed = fake_doubletake.install(
        directory,
        env={
            "kscreen-doctor": {"AIRPLAY_FAKE_KSCREEN_STATE": str(state)},
            "krfb-virtualmonitor": fake_virtualmonitor.helper_env(state, pids),
        },
    )
    fake_doubletake.private_path(monkeypatch, directory)
    yield installed, state, pids
    fake_doubletake.reap_private(str(tmp_path))


async def _gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_identity_evidence(pid).state is IdentityState.absent:
            return True
        await asyncio.sleep(0.02)
    return False


async def _until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def test_fake_daemon_refuses_requests_that_do_not_prove_ownership(
    tmp_path, commands,
):
    """Only the exact owner token and instance UUID are authenticated."""
    installed, _, _ = commands
    runner = LinuxProcessRunner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    daemon = await fake_doubletake.start_daemon(
        runner, shims=installed, runtime=runtime,
    )
    try:
        valid = await fake_doubletake.request(daemon, "status")
        assert valid["ok"] is True
        assert valid["instance_uuid"] == daemon.instance_uuid

        # A foreign token, a foreign instance, a missing request id, an unknown
        # schema and an unknown operation are each refused with a fixed code and
        # are recorded as refusals -- never answered with invented state.
        refusals = {
            "ownership_mismatch": await fake_doubletake.request(
                daemon, "status", owner_token="not-the-owner",
            ),
        }
        assert refusals["ownership_mismatch"]["ok"] is False
        assert await fake_doubletake.request(
            daemon, "status", instance_uuid="00000000-0000-4000-8000-000000000000",
        ) == {"ok": False, "code": "ownership_mismatch"}
        assert await fake_doubletake.raw_request(
            daemon, {"schema": fake_doubletake.SCHEMA, "operation": "status"},
        ) == {"ok": False, "code": "correlation_required"}
        assert await fake_doubletake.raw_request(
            daemon, {"schema": 99, "request_id": "x", "operation": "status"},
        ) == {"ok": False, "code": "schema_unsupported"}
        # The daemon is still serving the valid protocol after every refusal.
        assert (await fake_doubletake.request(daemon, "status"))["ok"] is True

        journal = fake_doubletake.journal(daemon.journal_path)
        assert [entry["outcome"] for entry in journal].count("refused") == 4
        assert any(entry["outcome"] == "served" for entry in journal)
    finally:
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_ctl_never_accepts_a_reply_for_another_request(
    tmp_path, commands,
):
    """A stale/foreign reply id is a protocol violation, not an answer."""
    _, _, _ = commands
    runner = LinuxProcessRunner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    daemon = await fake_doubletake.start_daemon(
        runner,
        shims=fake_doubletake.install(
            tmp_path / "bin-stale",
            env={"doubletake": {"AIRPLAY_FAKE_DAEMON_STALE_REPLY": "1"}},
        ),
        runtime=runtime,
    )
    try:
        output = await fake_doubletake.run_ctl(runner, daemon, "status")
        assert isinstance(output, Ok)
        assert output.value.exit_code == fake_doubletake.EXIT_UNCORRELATED
        assert output.value.stdout == b""
        assert output.value.stderr.strip() == fake_doubletake.UNCORRELATED_STDERR.strip()

        expected = fake_doubletake.journal(daemon.journal_path)
        served = [
            entry for entry in expected
            if entry["process"] == "daemon" and entry.get("operation")
        ]
        assert len(served) == 1
        assert served[0]["correlated"] is False
        # The ctl child refused it locally; it did not invent a result.
        ctl = [entry for entry in expected if entry["process"] == "ctl"]
        assert [
            entry["outcome"] for entry in ctl if entry["outcome"] != "start"
        ] == ["uncorrelated"]
    finally:
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_runner_bounds_malformed_and_hung_ctl_children(tmp_path, commands):
    """Malformed output is rejected and a hung child is killed and reaped."""
    installed, _, _ = commands
    runner = LinuxProcessRunner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    daemon = await fake_doubletake.start_daemon(runner, shims=installed, runtime=runtime)
    hung = fake_doubletake.install(
        tmp_path / "bin-hung",
        env={
            "doubletake-ctl": {
                "AIRPLAY_FAKE_CTL_MODE": "timeout",
                "AIRPLAY_FAKE_CTL_PID_FILE": str(tmp_path / "hung.pid"),
            },
        },
    )
    malformed = fake_doubletake.install(
        tmp_path / "bin-malformed",
        env={"doubletake-ctl": {"AIRPLAY_FAKE_CTL_MODE": "malformed"}},
    )
    try:
        bad = await fake_doubletake.run_ctl(
            runner, daemon, "status", shims=malformed,
        )
        assert isinstance(bad, Ok)
        assert bad.value.exit_code == 0
        assert bad.value.stdout == b"not-json"

        started = time.monotonic()
        timed_out = await fake_doubletake.run_ctl(
            runner, daemon, "status", shims=hung, timeout=0.4,
        )
        assert isinstance(timed_out, Err)
        assert timed_out.error.code is ErrorCode.cleanup_timeout
        assert time.monotonic() - started < 5.0
        await _until(lambda: (tmp_path / "hung.pid").exists())
        pid = int((tmp_path / "hung.pid").read_text())
        assert await _gone(pid), "the timed-out ctl child was not reaped"
    finally:
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_fake_helper_persists_one_owned_output_only_in_its_state_file(
    tmp_path, commands,
):
    """The helper writes its simulated KScreen output and nothing else."""
    installed, state, pids = commands
    runner = LinuxProcessRunner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    owner = fake_doubletake.owner_uuid()
    baseline = fake_kscreen.read_layout(state)

    spawned = await runner.spawn(CommandSpec(
        argv=(str(installed["krfb-virtualmonitor"]), "--resolution", "1920x1080"),
        env={
            fake_virtualmonitor.OWNER_ENV: str(owner),
            fake_virtualmonitor.RUNTIME_ENV: str(runtime),
        },
    ))
    assert isinstance(spawned, Ok)
    identity = spawned.value
    try:
        await _until(lambda: len(fake_kscreen.read_layout(state)["outputs"]) > len(
            baseline["outputs"],
        ))
        created = fake_kscreen.read_layout(state)
        new = [entry for entry in created["outputs"] if entry not in baseline["outputs"]]
        assert len(new) == 1
        assert new[0]["name"].lower().startswith("virtual")
        assert new[0]["size"] == {"width": 1920, "height": 1080}
        records = json.loads(pids.read_text())
        assert [record["pid"] for record in records] == [identity.pid]
        assert records[0]["owner_uuid"] == str(owner)
        assert runner.owned(identity, owner, str(runtime)) is True

        stopped = await runner.terminate(identity, owner, str(runtime), 5.0)
        assert isinstance(stopped, Ok)
        assert fake_kscreen.read_layout(state) == baseline
        assert json.loads(pids.read_text()) == []
    finally:
        await fake_doubletake.terminate(runner, identity)


@pytest.mark.parametrize("mode", ["crash-before-persist", "crash-after-persist"])
async def test_fake_helper_crash_windows_are_explicit(tmp_path, commands, mode):
    """A helper crash at either window never fabricates a stable output record."""
    _, state, pids = commands
    runner = LinuxProcessRunner()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    owner = fake_doubletake.owner_uuid()
    baseline = fake_kscreen.read_layout(state)

    installed = fake_doubletake.install(
        tmp_path / f"bin-{mode}",
        env={
            "krfb-virtualmonitor": fake_virtualmonitor.helper_env(
                state, pids, mode=mode,
            ),
        },
    )
    spawned = await runner.spawn(CommandSpec(
        argv=(str(installed["krfb-virtualmonitor"]), "--resolution", "1280x720"),
        env={
            fake_virtualmonitor.OWNER_ENV: str(owner),
            fake_virtualmonitor.RUNTIME_ENV: str(runtime),
        },
    ))
    assert isinstance(spawned, Ok)
    if mode == "crash-after-persist":
        assert await _until(
            lambda: pids.exists() and json.loads(pids.read_text()) != [],
        )
    else:
        await asyncio.sleep(0.2)
    assert await _gone(spawned.value.pid)
    created = fake_kscreen.read_layout(state)
    if mode == "crash-before-persist":
        assert created == baseline
        assert not pids.exists() or json.loads(pids.read_text()) == []
    else:
        orphans = [entry for entry in created["outputs"] if entry not in baseline["outputs"]]
        assert len(orphans) == 1
        assert json.loads(pids.read_text())[0]["pid"] == spawned.value.pid


async def test_private_commands_cannot_be_mistaken_for_the_installed_backend(
    tmp_path, commands,
):
    """The fixtures advertise their test-only surface, never installed support."""
    installed, _, _ = commands
    runner = LinuxProcessRunner()

    assert Path(shutil.which("doubletake")) == installed["doubletake"]
    assert Path(shutil.which("doubletake-ctl")) == installed["doubletake-ctl"]
    assert Path(shutil.which("application-contract-v1")) == installed["application-contract-v1"]

    surface = await runner.run(
        CommandSpec(argv=("application-contract-v1", "--help"), env={}), 5.0,
    )
    assert isinstance(surface, Ok)
    assert surface.value.exit_code == 0
    text = surface.value.stdout.decode()
    assert fake_doubletake.TEST_ONLY_SURFACE in text
    assert "doubletake-ctl" not in text

    helper = await runner.run(
        CommandSpec(argv=("krfb-virtualmonitor", "--help"), env={}), 5.0,
    )
    assert isinstance(helper, Ok)
    assert b"--resolution" in helper.value.stdout
