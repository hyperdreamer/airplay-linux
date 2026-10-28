from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from airplay_linux import app
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.runtime import RuntimeConfig, build_runtime
from airplay_linux.session import Route
from airplay_linux.session.shutdown import ShutdownCoordinator
from airplay_linux.session.testing import SessionRig


def _config(tmp_path: Path, *, live: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
        live=live,
    )


@pytest.mark.asyncio
async def test_runtime_builds_real_adapters_without_dependency_override(tmp_path: Path) -> None:
    result = await build_runtime(_config(tmp_path))

    assert isinstance(result, Ok)
    runtime = result.value

    from airplay_linux.capture.physical import PhysicalCaptureProvider
    from airplay_linux.discovery.zeroconf_adapter import ZeroconfDiscovery
    from airplay_linux.process.daemon_registry import DaemonRegistry
    from airplay_linux.transport.doubletake import DoubleTakeTransport

    assert isinstance(runtime.deps.transport, DoubleTakeTransport)
    assert isinstance(runtime.deps.capture, PhysicalCaptureProvider)
    assert isinstance(runtime.deps.discovery, ZeroconfDiscovery)
    assert isinstance(runtime.registry, DaemonRegistry)
    assert runtime.gated_capabilities
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_live_build_wires_production_presenter_and_dialog_factory(tmp_path: Path) -> None:
    pytest.importorskip("PySide6")

    result = await build_runtime(_config(tmp_path, live=True))

    assert isinstance(result, Ok)
    runtime = result.value
    from airplay_linux.ui.presenter import Presenter, QtDialogFactory

    assert isinstance(runtime.presenter, Presenter)
    assert isinstance(runtime.dialogs, QtDialogFactory)
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_start_and_diagnostics_are_public_lifecycle_operations(tmp_path: Path) -> None:
    rig = SessionRig()
    result = await build_runtime(
        _config(tmp_path),
        {"deps": rig.deps, "capabilities": rig.deps.capabilities},
    )
    assert isinstance(result, Ok)
    runtime = result.value

    started = await runtime.start()

    assert isinstance(started, Ok)
    assert rig.discovery.started
    assert runtime.diagnostics() == rig.deps.capabilities()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_one_transaction_and_replays_the_same_failure(tmp_path: Path) -> None:
    rig = SessionRig()
    result = await build_runtime(_config(tmp_path), {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value
    failure = Err(error=diagnostic(ErrorCode.trust_cleanup_required))
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def failing_shutdown() -> Err:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return failure

    runtime._shutdown_hook = failing_shutdown
    first_task = asyncio.create_task(runtime.shutdown())
    await entered.wait()
    second_task = asyncio.create_task(runtime.shutdown())
    await asyncio.sleep(0)
    release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert calls == 1
    assert first is failure
    assert second is failure


@pytest.mark.asyncio
async def test_shutdown_preserves_manager_typed_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = SessionRig()
    result = await build_runtime(_config(tmp_path), {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value
    failure = Err(error=diagnostic(ErrorCode.trust_cleanup_required))

    async def shutdown() -> Err:
        return failure

    monkeypatch.setattr(runtime.manager, "shutdown", shutdown)

    outcome = await runtime.shutdown()

    assert outcome is failure
    assert outcome.error.code is ErrorCode.trust_cleanup_required


@pytest.mark.asyncio
async def test_individual_adapter_override_rebuilds_manager_deps(tmp_path: Path) -> None:
    rig = SessionRig()
    replacement = object()
    result = await build_runtime(
        _config(tmp_path),
        {"deps": rig.deps, "capture": replacement},
    )

    assert isinstance(result, Ok)
    deps = result.value.manager.deps
    assert deps is not rig.deps
    assert deps.capture is replacement
    assert deps.transport is rig.deps.transport
    await result.value.shutdown()


@pytest.mark.asyncio
async def test_individual_adapter_override_requires_dependency_bundle(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="deps"):
        await build_runtime(_config(tmp_path), {"capture": object()})


@pytest.mark.asyncio
async def test_shutdown_coordinator_rejects_recovery_route_collision() -> None:
    rig = SessionRig()
    existing = lambda *_args: True
    rig.manager.handlers[Route.recovery] = existing

    with pytest.raises((RuntimeError, ValueError), match="recovery"):
        ShutdownCoordinator(
            rig.manager,
            rig.deps,
            object(),
            object(),
            deadline_seconds=15,
        )



def test_check_environment_awaits_real_probe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    seen: dict[str, Any] = {}

    class Probe:
        async def check(self) -> object:
            seen["checked"] = True
            return "report"

    def from_environment(cls: type[EnvironmentProbe], **kwargs: Any) -> Probe:
        seen.update(kwargs)
        return Probe()

    monkeypatch.setattr(EnvironmentProbe, "from_environment", classmethod(from_environment))

    status = app.main(["--check-environment"])

    assert status == 0
    assert seen["checked"] is True
    assert "process" in seen
    assert "helper" in seen
    assert "coroutine" not in capsys.readouterr().out


def test_run_starts_runtime_and_returns_nonzero_on_shutdown_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    calls: list[str] = []
    failure = Err(error=diagnostic(ErrorCode.cleanup_timeout))

    class RuntimeStub:
        gated_capabilities: dict[str, str] = {}

        async def start(self) -> Ok:
            calls.append("start")
            return Ok(value=None)

        async def shutdown(self) -> Err:
            calls.append("shutdown")
            return failure

    async def build(_config: RuntimeConfig) -> Ok:
        return Ok(value=RuntimeStub())

    monkeypatch.setattr(app, "build_runtime", build)

    status = asyncio.run(app._run(_config(tmp_path)))

    assert status != 0
    assert calls == ["start", "shutdown"]


@pytest.mark.asyncio
async def test_shutdown_coordinator_is_bound_to_runtime_shutdown(tmp_path: Path) -> None:
    rig = SessionRig()
    result = await build_runtime(_config(tmp_path), {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value

    assert isinstance(runtime.shutdown_coordinator, ShutdownCoordinator)
    await runtime.shutdown()
