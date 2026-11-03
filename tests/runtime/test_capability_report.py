"""The production capability report must expose the composition's own evidence.

Mirror availability is decided twice: the UI and ``can_start`` read
``CapabilityReport.mirror``, while the transport admits a start from the
composition-derived :class:`BackendFeatures`.  A report built from the public
help probe alone can never affirm the capabilities only the composition owns
(``owned_daemon_control``, ``keyring``, ``physical_source``), so it would refuse
every start the transport would accept and show a backend-incompatibility banner
for a backend that is installed correctly.

These tests pin both directions: complete source reporting together with a
proven credential service produces a startable Mirror, and an installed backend
that withholds ``status-source-complete`` keeps Mirror refused with the exact
``physical_source`` diagnostic.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from airplay_linux import runtime as composition
from airplay_linux.backend.contract import CredentialProofVerdict
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.runtime import RuntimeConfig, build_runtime

FIXTURES = Path(__file__).parents[1] / "fixtures"
COMPLETE = "capabilities-v1\nstatus-source\nstatus-source-complete\n"
PARTIAL = "capabilities-v1\nstatus-source\n"


class BackendProcess:
    """Answer the pinned help/capability probes and refuse everything else."""

    def __init__(self, capabilities: str) -> None:
        self.capabilities = capabilities
        self.argvs: list[tuple[str, ...]] = []

    async def run(self, spec, timeout, stdin=None):
        argv = tuple(spec.argv)
        self.argvs.append(argv)
        responses = {
            ("doubletake", "--help"): (
                FIXTURES / "backend" / "doubletake-0.5-source-help.txt"
            ).read_text(),
            ("doubletake-ctl", "--help"): (
                FIXTURES / "backend" / "doubletake-ctl-0.5-help.txt"
            ).read_text(),
            ("doubletake", "--version"): "doubletake 0.5.0\n",
            ("doubletake", "-capabilities"): self.capabilities,
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
        raise AssertionError(f"production spawn reached: {tuple(spec.argv)!r}")

    def identity(self, pid):
        return None

    def owned(self, identity, owner_uuid, runtime_directory):
        return False

    async def terminate(self, *args, **kwargs):
        raise AssertionError("production termination reached")

    def subscribe(self, callback):
        return SimpleNamespace(close=lambda: None)


def _plasma_wayland(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setenv("KDE_SESSION_VERSION", "6")


def _proven_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the real epoch with a proven credential service and no spawn."""

    install = composition._install_owned_daemon_epoch

    async def proven():
        return Ok(value=CredentialProofVerdict(
            service_leg="passed",
            backend_positive_leg="passed",
            backend_negative_leg="passed",
            failure=None,
        ))

    def installation(config, *, process, launcher):
        return replace(
            install(config, process=process, launcher=launcher),
            credential_proof=proven,
        )

    async def keyring_available(_dbus):
        return True

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", installation)
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: "/offline/unshare")
    monkeypatch.setattr(
        "airplay_linux.policy.capabilities.secret_service_available", keyring_available,
    )
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace())


async def _report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capabilities: str):
    _plasma_wayland(monkeypatch)
    _proven_installation(monkeypatch)
    process = BackendProcess(capabilities)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)

    async def no_shutdown():
        return Ok(value=None)

    built = await build_runtime(
        RuntimeConfig(state_root=state_root, runtime_root=runtime_root, live=False),
        overrides={"process": process, "shutdown_hook": no_shutdown},
    )
    assert isinstance(built, Ok), built
    runtime = built.value
    try:
        return runtime.deps.capabilities(), runtime.deps.transport._features, process
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_complete_source_and_proven_keyring_make_mirror_startable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend the transport would admit must not be reported as unusable."""
    report, features, process = await _report(tmp_path, monkeypatch, COMPLETE)

    # The composition proved every prerequisite the transport admits on.
    assert features.owned_daemon_control is True
    assert features.physical_source is True
    assert features.source_reporting_complete is True
    assert features.keyring is True

    assert report.environment == "kde-plasma-6-wayland"
    assert report.mirror is True
    details = {item.detail for item in report.diagnostics}
    assert "physical_source" not in details
    assert "owned_daemon_control" not in details
    assert "keyring" not in details
    # Pairing is a separate guarantee: it stays closed without the backend's
    # trust transactions, and Mirror availability must not claim it.
    assert report.remembered_pairing is False
    assert report.virtual_display is False
    assert not any("-daemonize" in argv for argv in process.argvs)


@pytest.mark.asyncio
async def test_withheld_source_token_keeps_mirror_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend without complete source reporting must still refuse Mirror."""
    report, features, _process = await _report(tmp_path, monkeypatch, PARTIAL)

    assert features.source_reporting is True
    assert features.source_reporting_complete is False
    assert features.physical_source is False
    assert report.mirror is False
    assert "physical_source" in {
        item.detail for item in report.diagnostics
        if item.code is ErrorCode.dependency_incompatible
    }


@pytest.mark.asyncio
async def test_unusable_backend_keeps_its_own_typed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing backend is named as missing, not as a missing derived flag."""
    _plasma_wayland(monkeypatch)
    _proven_installation(monkeypatch)

    async def missing(_process):
        return Err(error=diagnostic(ErrorCode.dependency_missing, command="doubletake"))

    monkeypatch.setattr(composition, "probe_backend", missing)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)

    async def no_shutdown():
        return Ok(value=None)

    built = await build_runtime(
        RuntimeConfig(state_root=state_root, runtime_root=runtime_root, live=False),
        overrides={"process": BackendProcess(PARTIAL), "shutdown_hook": no_shutdown},
    )
    assert isinstance(built, Ok), built
    runtime = built.value
    try:
        report = runtime.deps.capabilities()
    finally:
        await runtime.shutdown()

    assert report.mirror is False
    missing_commands = [
        item for item in report.diagnostics if item.code is ErrorCode.dependency_missing
    ]
    assert any("doubletake" in item.message for item in missing_commands)
    assert "owned_daemon_control" not in {item.detail for item in report.diagnostics}
