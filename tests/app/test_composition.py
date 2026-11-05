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


@pytest.mark.parametrize(
    ("factory", "route", "other_owner"),
    [
        ("build_lifecycle_handlers", Route.recovery, "build_recovery_handlers"),
        ("build_virtual_handlers", Route.trust, "build_lifecycle_handlers"),
        ("build_reconnect_handlers", Route.trust, "build_lifecycle_handlers"),
        ("build_audio_handlers", Route.trust, "build_lifecycle_handlers"),
        ("build_recovery_handlers", Route.trust, "build_lifecycle_handlers"),
    ],
)
def test_compose_handlers_rejects_factory_route_collision(
    monkeypatch: pytest.MonkeyPatch, factory: str, route: Route, other_owner: str,
) -> None:
    """An extra route from any factory must not silently displace its owner."""
    from airplay_linux import runtime

    original = getattr(runtime, factory)

    def colliding(deps):
        return {**original(deps), route: lambda *_args: True}

    monkeypatch.setattr(runtime, factory, colliding)

    with pytest.raises((RuntimeError, ValueError)) as caught:
        compose_handlers(SessionRig().deps)

    message = str(caught.value)
    assert route.value in message
    assert factory in message
    assert other_owner in message


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


@pytest.mark.asyncio
async def test_production_preferences_share_one_writer_and_bridge_early_warnings(tmp_path, monkeypatch):
    import json
    from urllib.parse import quote
    from airplay_linux import runtime as composition
    from airplay_linux.policy.capabilities import EnvironmentProbe
    from airplay_linux.storage import AtomicFiles, JsonReceiverStore, encode_receiver
    from airplay_linux.storage.repositories import ReceiverPreferenceWriter
    from airplay_linux.domain.records import SavedReceiver
    from airplay_linux.testing.adapters import FakeProcess
    from airplay_linux.testing.builders import target

    receiver_id = target().receiver_id
    files = AtomicFiles(tmp_path / "state")
    record = SavedReceiver(
        receiver_id=receiver_id, service_metadata=(), display_name="TV", last_endpoint=None,
        continuity_fingerprint="fp", credential_ref=None, remembered_resolution=None,
    )
    raw = json.loads(encode_receiver(record))
    key = "receivers/" + quote(receiver_id, safe="") + ".json"
    files.write(key, json.dumps(raw | {"remembered_volume": "secret-invalid-volume"}).encode())
    constructed = []
    class Writer(ReceiverPreferenceWriter):
        def __init__(self, store, discovery):
            super().__init__(store, discovery)
            constructed.append(self)
    class EarlyStore(JsonReceiverStore):
        def __init__(self, files, **kwargs):
            super().__init__(files, **kwargs)
            # Exercise the real decoder/dedup before ActivityLog construction.
            self._decode(files.read(key))
            self._decode(files.read(key))
    async def unavailable(_process):
        return Err(error=diagnostic(ErrorCode.dependency_missing))
    async def report(_probe):
        return SessionRig().deps.capabilities()
    monkeypatch.setattr(composition, "ReceiverPreferenceWriter", Writer)
    monkeypatch.setattr(composition, "JsonReceiverStore", EarlyStore)
    monkeypatch.setattr(composition, "probe_backend", unavailable)
    monkeypatch.setattr(EnvironmentProbe, "check", report)
    graph = await composition._compose_production(
        RuntimeConfig(state_root=tmp_path / "state", runtime_root=tmp_path / "run"),
        process=FakeProcess(),
    )
    assert constructed == [graph.deps.preferences]
    assert graph.deps.preferences._store is graph.deps.receivers
    assert graph.deps.preferences._discovery is graph.deps.discovery
    log = graph.deps.activity
    assert len(log.entries()) == 1
    assert (await graph.deps.receivers.get(receiver_id)).value.remembered_volume is None
    assert len(log.entries()) == 1
    files.write(key, json.dumps(raw | {"remembered_volume": 0}).encode())
    assert isinstance(await graph.deps.receivers.all(), Ok)
    assert len(log.entries()) == 2
    for event in log.entries():
        assert event.code == "audio.volume_unavailable"
        assert event.receiver_id == receiver_id
        assert dict(event.fields) == {"reason": "invalid_preference", "error_code": "volume_preference_invalid"}
    assert "secret" not in log.export()


def test_session_rig_preferences_share_one_writer():
    from airplay_linux.storage.repositories import ReceiverPreferenceWriter
    rig = SessionRig()
    assert type(rig.deps.preferences) is ReceiverPreferenceWriter
    assert rig.deps.preferences._store is rig.receivers
    assert rig.deps.preferences._discovery is rig.discovery
    assert compose_manager(rig.deps).deps.preferences is rig.deps.preferences


def tmp_path_stub() -> Path:
    from tempfile import mkdtemp

    return Path(mkdtemp())


async def _build(root: Path, deps: ManagerDeps):
    config = RuntimeConfig(state_root=root / "state", runtime_root=root / "runtime", live=False)
    result = await build_runtime(config, {"deps": deps})
    assert isinstance(result, Ok)
    return result.value
