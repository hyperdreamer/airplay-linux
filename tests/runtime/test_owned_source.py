from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from airplay_linux import runtime as composition
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import CapabilityReport, Err, ErrorCode, Ok
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.runtime import RuntimeConfig, build_runtime

FIXTURES = Path(__file__).parents[1] / "fixtures"


class RecordingProcess:
    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self.spawns = 0

    async def run(self, spec, timeout, stdin=None):
        argv = tuple(spec.argv)
        self.argvs.append(argv)
        responses = {
            ("doubletake", "--help"): (FIXTURES / "backend" / "doubletake-0.5-source-help.txt").read_text(),
            ("doubletake-ctl", "--help"): (FIXTURES / "backend" / "doubletake-ctl-0.5-help.txt").read_text(),
            ("doubletake", "--version"): "doubletake 0.5.0\n",
            ("doubletake", "-capabilities"): "capabilities-v1\nstatus-source\nstatus-source-complete\n",
            ("kscreen-doctor", "-j"): (
                FIXTURES / "display" / "kscreen-baseline.json"
            ).read_text(),
        }
        if argv not in responses:
            return Err(error=diagnostic(ErrorCode.dependency_incompatible))
        return Ok(value=CommandOutput(
            exit_code=0, stdout=responses[argv].encode(), stderr=b"",
        ))

    async def spawn(self, spec):
        self.spawns += 1
        raise AssertionError(f"production spawn reached: {tuple(spec.argv)!r}")

    def identity(self, pid):
        return None

    def owned(self, identity, owner_uuid, runtime_directory):
        return False

    async def terminate(self, *args, **kwargs):
        raise AssertionError("production termination reached")

    def subscribe(self, callback):
        return SimpleNamespace(close=lambda: None)


@pytest.mark.asyncio
async def test_source_preparation_cannot_activate_production(tmp_path, monkeypatch):
    process = RecordingProcess()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)

    async def check(_probe):
        return CapabilityReport(
            environment="kde-plasma-6-wayland", discovery=True, mirror=True,
            virtual_display=False, audio=True, remembered_pairing=False,
            additional_virtual_outputs=False, diagnostics=(),
        )

    async def no_shutdown():
        return Ok(value=None)

    monkeypatch.setattr(EnvironmentProbe, "check", check)
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace())
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", lambda: SimpleNamespace())

    result = await build_runtime(
        RuntimeConfig(state_root=state_root, runtime_root=runtime_root, live=False),
        overrides={"process": process, "shutdown_hook": no_shutdown},
    )

    assert isinstance(result, Ok), result
    runtime = result.value
    source_verifier = runtime.deps.transport._source_verifier
    assert source_verifier is not None
    assert source_verifier.inventory is runtime.deps.capture._connectors._inventory
    assert runtime.deps.transport._source_verifier is source_verifier
    assert runtime.deps.transport._features.source_reporting_complete is True
    assert runtime.deps.transport._features.physical_source is True

    listed = await runtime.deps.capture.list_physical()
    assert isinstance(listed, Ok), listed
    assert [source.connector for source in listed.value] == ["DP-1"]

    shutdown = await runtime.shutdown()
    assert isinstance(shutdown, Ok), shutdown
    assert process.spawns == 0
    assert not any("-daemonize" in argv for argv in process.argvs)
    assert not any(argv and argv[0] == "unshare" for argv in process.argvs)
    assert process.argvs.count(("kscreen-doctor", "-j")) == 1
