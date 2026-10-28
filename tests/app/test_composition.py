from __future__ import annotations

from pathlib import Path

import pytest

import asyncio

from airplay_linux.app import parse_args
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.runtime import RuntimeConfig, build_runtime, compose_handlers, compose_manager
from airplay_linux.session import ManagerDeps, Route
from airplay_linux.session.testing import SessionRig


def test_parse_args_diagnostics_is_read_only_command() -> None:
    args = parse_args(["--diagnostics"])
    assert args.diagnostics
    assert not args.check_environment


def test_compose_handlers_owns_every_session_route_once() -> None:
    rig = SessionRig()
    handlers = compose_handlers(rig.deps)

    assert set(handlers) == set(Route)
    assert all(callable(handler) for handler in handlers.values())


def test_compose_manager_uses_production_handler_composition() -> None:
    rig = SessionRig()
    manager = compose_manager(rig.deps)

    assert set(manager.handlers) == set(Route)
    assert manager.deps is rig.deps


@pytest.mark.asyncio
async def test_runtime_accepts_explicit_test_dependency_override(tmp_path: Path) -> None:
    rig = SessionRig()
    config = RuntimeConfig(
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
        live=False,
    )

    result = await build_runtime(
        config,
        {"deps": rig.deps, "capabilities": rig.deps.capabilities},
    )

    assert isinstance(result, Ok)
    runtime = result.value
    assert runtime.manager.deps is rig.deps
    assert runtime.presenter is None
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_override_name(tmp_path: Path) -> None:
    config = RuntimeConfig(
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
        live=False,
    )

    with pytest.raises(TypeError, match="unknown runtime override"):
        await build_runtime(config, {"fake_everything": object()})


def test_runtime_config_requires_absolute_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(state_root=Path("state"), runtime_root=tmp_path / "runtime")
    with pytest.raises(ValueError):
        RuntimeConfig(state_root=tmp_path / "state", runtime_root=Path("runtime"))


# -- the five deferred obligations ------------------------------------------------
#
# Each prior lane fails closed because the authority it needs can only come from
# this composition root. These tests pin the composition's *answer*: either the
# obligation is discharged, or the capability is reported gated with a reason. A
# permissive stub would satisfy neither, which is the point.


def test_transport_keyring_control_binding_is_gated_not_faked() -> None:
    """T-011: the keyring store's control cannot be rebound by this composition."""

    runtime = asyncio.run(_build(tmp_path_stub(), SessionRig().deps))
    assert "remembered_trust" in runtime.gated_capabilities
    assert runtime.gated_capabilities["remembered_trust"]


def test_display_association_verifier_is_gated_not_faked() -> None:
    """T-014: creation refuses to publish without a real association verifier."""

    runtime = asyncio.run(_build(tmp_path_stub(), SessionRig().deps))
    assert "virtual_display" in runtime.gated_capabilities
    reason = runtime.gated_capabilities["virtual_display"]
    assert "verifier" in reason


@pytest.mark.asyncio
async def test_capture_cleanup_timeout_is_cleanup_required_not_success(tmp_path: Path) -> None:
    """T-012: an unconfirmed release is cleanup-required, never success."""

    uptime_root = tmp_path / "state"
    rig = SessionRig()
    config = RuntimeConfig(state_root=uptime_root, runtime_root=tmp_path / "runtime", live=False)
    result = await build_runtime(config, {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value

    async def failing_shutdown():
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    runtime._shutdown_hook = failing_shutdown
    outcome = await runtime.shutdown()
    assert isinstance(outcome, Err)
    assert outcome.error.code is ErrorCode.cleanup_timeout


@pytest.mark.asyncio
async def test_composition_does_not_swallow_cleanup_required(tmp_path: Path) -> None:
    """T-019: CleanupRequired must survive composition, not be downgraded."""

    rig = SessionRig()
    config = RuntimeConfig(state_root=tmp_path / "state", runtime_root=tmp_path / "runtime", live=False)
    result = await build_runtime(config, {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value

    async def cleanup_required():
        return Err(error=diagnostic(ErrorCode.trust_cleanup_required))

    runtime._shutdown_hook = cleanup_required
    outcome = await runtime.shutdown()
    assert isinstance(outcome, Err)
    assert outcome.error.code is ErrorCode.trust_cleanup_required


@pytest.mark.asyncio
async def test_presenter_override_is_wired_for_the_ui_lane(tmp_path: Path) -> None:
    """T-026: the production presenter is injected, never reconstructed here."""

    rig = SessionRig()
    sentinel = object()
    config = RuntimeConfig(state_root=tmp_path / "state", runtime_root=tmp_path / "runtime", live=False)
    result = await build_runtime(config, {"deps": rig.deps, "presenter": sentinel})
    assert isinstance(result, Ok)
    assert result.value.presenter is sentinel


@pytest.mark.asyncio
async def test_shutdown_is_idempotent(tmp_path: Path) -> None:
    """A second shutdown must not run teardown twice."""

    rig = SessionRig()
    config = RuntimeConfig(state_root=tmp_path / "state", runtime_root=tmp_path / "runtime", live=False)
    result = await build_runtime(config, {"deps": rig.deps})
    assert isinstance(result, Ok)
    runtime = result.value
    first = await runtime.shutdown()
    second = await runtime.shutdown()
    assert isinstance(first, Ok)
    assert isinstance(second, Ok)


def tmp_path_stub() -> Path:
    from tempfile import mkdtemp

    return Path(mkdtemp())


async def _build(root: Path, deps: ManagerDeps):
    config = RuntimeConfig(state_root=root / "state", runtime_root=root / "runtime", live=False)
    result = await build_runtime(config, {"deps": deps})
    assert isinstance(result, Ok)
    return result.value
