"""Observe the final process port through real production runtime composition.

These fakes replace external evidence and unfinished installation seams only.
The recorder never executes a command or connects to a host service.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from airplay_linux import runtime as composition
from airplay_linux.backend.contract import probe_backend
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.domain.resources import CapabilityReport, Err, ErrorCode, Ok
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.runtime import RuntimeConfig, build_runtime

FIXTURES = Path(__file__).parents[1] / "fixtures" / "backend"


@dataclass(frozen=True)
class ProcessObservation:
    method: str
    kind: str
    argv: tuple[str, ...] = field(repr=False)


class ProcessRecorder:
    """Classify actual invocations; never infer a launch from nested argv."""

    def __init__(self):
        self.observations: list[ProcessObservation] = []

    def _record(self, method, spec):
        executable = Path(spec.argv[0]).name
        if executable == "unshare":
            kind = "outer_namespace"
        elif executable == "doubletake" and "-daemonize" in spec.argv[1:]:
            kind = "direct_backend"
        elif spec.argv[-1] == "--help":
            kind = "help_observation"
        elif spec.argv[-1] == "--version":
            kind = "version_observation"
        elif spec.argv[-1] == "-capabilities":
            kind = "capability_observation"
        elif spec.argv == ("kscreen-doctor", "-j"):
            kind = "inventory_observation"
        else:
            kind = "other"
        self.observations.append(ProcessObservation(method, kind, spec.argv))

    async def run(self, spec, timeout, stdin=None):
        self._record("run", spec)
        responses = {
            ("doubletake", "--help"): (FIXTURES / "doubletake-0.5-source-help.txt").read_text(),
            ("doubletake-ctl", "--help"): (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text(),
            ("doubletake", "--version"): "doubletake 0.5.0\n",
            ("doubletake", "-capabilities"): "capabilities-v1\nstatus-source\nstatus-source-complete\n",
            ("kscreen-doctor", "-j"): json.dumps({"outputs": [{
                "name": "DP-1", "enabled": True, "connected": True, "type": 14,
            }]}),
        }
        if spec.argv in responses:
            return Ok(value=CommandOutput(
                exit_code=0, stdout=responses[spec.argv].encode(), stderr=b"",
            ))
        return Err(error=diagnostic(ErrorCode.dependency_incompatible))

    async def spawn(self, spec):
        self._record("spawn", spec)
        return Err(error=diagnostic(ErrorCode.dependency_incompatible))

    def identity(self, pid):
        return None

    def owned(self, identity, owner_uuid, runtime_directory):
        return False

    async def terminate(self, *args, **kwargs):
        raise AssertionError("dormant runtime attempted termination")

    def subscribe(self, callback):
        return SimpleNamespace(close=lambda: None)

    def events(self, kind):
        return [event for event in self.observations if event.kind == kind]


@pytest.fixture
def offline_boundaries(monkeypatch):
    """Tripwires intercept real process and bus dispatch before any runtime I/O."""
    from dbus_next.aio import MessageBus

    async def forbidden(*args, **kwargs):
        raise AssertionError("offline test reached a host process or session bus")

    async def check(_probe):
        return CapabilityReport(
            environment="kde-plasma-6-wayland", discovery=True, mirror=True,
            virtual_display=False, audio=True, remembered_pairing=False,
            additional_virtual_outputs=False, diagnostics=(),
        )

    async def nothing():
        return Ok(value=None)

    monkeypatch.setattr(LinuxProcessRunner, "run", forbidden)
    monkeypatch.setattr(LinuxProcessRunner, "spawn", forbidden)
    monkeypatch.setattr(MessageBus, "connect", forbidden)
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace(close=nothing))
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", lambda: SimpleNamespace(check=nothing))
    monkeypatch.setattr(EnvironmentProbe, "check", check)
    return forbidden


async def test_process_observations_classify_direct_and_unshare_daemon_launches():
    recorder = ProcessRecorder()
    direct = ("doubletake", "-daemonize", "-socket", "/private/daemon.sock", "-cred-backend", "keyring")
    await recorder.run(CommandSpec(argv=direct, env={}), timeout=1.0)
    await recorder.spawn(CommandSpec(argv=direct, env={}))
    await recorder.run(CommandSpec(argv=("unshare", "-rn", *direct), env={}), timeout=1.0)
    await recorder.spawn(CommandSpec(argv=("unshare", "-rn", *direct), env={}))
    await recorder.run(CommandSpec(argv=("doubletake", "--help"), env={}), timeout=1.0)
    await recorder.run(CommandSpec(argv=("doubletake", "--version"), env={}), timeout=1.0)
    await recorder.run(CommandSpec(argv=("doubletake", "-capabilities"), env={}), timeout=1.0)
    await recorder.run(CommandSpec(argv=("kscreen-doctor", "-j"), env={}), timeout=1.0)

    assert [event.method for event in recorder.events("direct_backend")] == ["run", "spawn"]
    assert [event.method for event in recorder.events("outer_namespace")] == ["run", "spawn"]
    for kind in ("help_observation", "version_observation", "capability_observation", "inventory_observation"):
        assert len(recorder.events(kind)) == 1
    assert len(recorder.observations) == 8


async def test_phase_guard_blocks_eligible_production_launch(tmp_path, monkeypatch, offline_boundaries):
    from airplay_linux.backend.contract import CredentialProofVerdict
    from airplay_linux.process.launcher import ProductionLaunchCapability

    recorder = ProcessRecorder()
    root = tmp_path / "r"
    root.mkdir(mode=0o700)
    config = RuntimeConfig(state_root=tmp_path / "s", runtime_root=root, live=False)
    passed = CredentialProofVerdict(
        service_leg="passed", backend_positive_leg="passed",
        backend_negative_leg="passed", failure=None,
    )
    seen = SimpleNamespace(probe_calls=0, launcher=None)
    verifier = SimpleNamespace(physical_inventory_available=True)

    # Recognition belongs to Task 7. Capture its future structural output after
    # the real raw probe has recognized a compatible patched help surface.
    async def complete_source_probe(process):
        assert process is recorder
        result = await probe_backend(process)
        assert isinstance(result, Ok)
        assert result.value.profile == "doubletake-0.5+source"
        return Ok(value=replace(result.value, source_reporting_complete=True))

    async def credential_proof():
        seen.probe_calls += 1
        # An eligible future adapter reaches the exact injected final port.
        await recorder.run(CommandSpec(argv=(
            "unshare", "-rn", sys.executable, "-m", "airplay_linux.process.credential_probe",
        ), env={}), timeout=15.0)
        return Ok(value=passed)

    def installation(config, *, process, launcher):
        assert process is recorder
        assert isinstance(launcher, ProductionLaunchCapability)
        assert launcher.production_graph and launcher.available()
        seen.launcher = launcher
        return composition._OwnedDaemonInstallation(
            source_verifier=verifier, credential_proof=credential_proof,
        )

    monkeypatch.setattr(composition, "probe_backend", complete_source_probe)
    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    # Verify interception independently before entering production composition.
    for method in (LinuxProcessRunner().run, LinuxProcessRunner().spawn):
        with pytest.raises(AssertionError, match="offline test reached"):
            await method(CommandSpec(argv=("offline-tripwire",), env={}))

    result = await build_runtime(config, overrides={"process": recorder})
    assert isinstance(result, Ok)
    runtime = result.value
    try:
        assert runtime.registry._process is recorder
        assert runtime.deps.display.helper._process is recorder
        assert runtime.deps.display.observer._process is recorder
        assert runtime.deps.capture._connectors._process is recorder
        monitors = await runtime.deps.capture.list_physical()
        assert isinstance(monitors, Ok)
        assert [source.connector for source in monitors.value] == ["DP-1"]
        assert runtime.deps.transport._features.owned_daemon_control is True
        assert runtime.deps.transport._features.physical_source is True
        # A pure all-passed verdict is eligible, but the installed proof producer
        # may not run until activation. Raw help's old gates remain closed.
        pure = composition.derive_composition_capabilities(
            runtime.deps.transport._features, seen.launcher, passed, verifier, None,
        )
        assert pure.keyring is True
        for attempt in (runtime.registry.ensure, runtime.deps.restart_daemon):
            refused = await attempt()
            assert isinstance(refused, Err)
            assert refused.error.code is ErrorCode.dependency_incompatible
    finally:
        await runtime.shutdown()
    assert recorder.events("direct_backend") == [], "phase guard allowed a direct backend launch"
    assert recorder.events("outer_namespace") == [], "phase guard allowed an outer namespace launch"
    assert not any(event.method == "spawn" for event in recorder.observations)
    assert seen.probe_calls == 0
    assert runtime.deps.transport._features.keyring is False
    assert runtime.deps.transport._features.foreground_control is False
    assert runtime.deps.transport._features.owner_handshake is False
    for kind in ("help_observation", "version_observation", "capability_observation", "inventory_observation"):
        assert recorder.events(kind), f"final process override bypassed {kind}"
