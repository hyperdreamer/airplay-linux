"""Production composition must retain one state authority across its lifecycle."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from airplay_linux import runtime as runtime_module
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import EffectEvent
from airplay_linux.domain.operations import OperationId
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import CapabilityReport, Err, ErrorCode, Ok, TrustState
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.runtime import Runtime, RuntimeConfig, build_runtime
from airplay_linux.storage import AtomicFiles, JsonSessionStore
from airplay_linux.testing.builders import target


@pytest.fixture(autouse=True)
def offline_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control external evidence only; keep the production graph and stores real."""
    async def unavailable_backend(_process):
        return Err(error=diagnostic(ErrorCode.dependency_missing))

    async def check(_probe):
        return CapabilityReport(
            environment="unknown", discovery=False, mirror=False,
            virtual_display=False, audio=False, remembered_pairing=False,
            additional_virtual_outputs=False, diagnostics=(),
        )

    monkeypatch.setattr(runtime_module, "probe_backend", unavailable_backend)
    monkeypatch.setattr(EnvironmentProbe, "check", check)


def config(tmp_path: Path, *, live: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        state_root=tmp_path / "state", runtime_root=tmp_path / "run", live=live,
    )


async def close_runtime(runtime: Runtime) -> None:
    try:
        await runtime.shutdown()
    finally:
        # Also release the disconnected manager when running against the defect.
        await runtime.persistence.close()
        for subscription in tuple(runtime.startup_recovery.manager._subscriptions):
            subscription.close()


@pytest.mark.asyncio
async def test_production_manager_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qtbot) -> None:
    """A second construction splits recovery/persistence from UI/shutdown."""
    constructed = []
    original = runtime_module.compose_manager

    def compose(deps):
        manager = original(deps)
        constructed.append(manager)
        return manager

    monkeypatch.setattr(runtime_module, "compose_manager", compose)
    result = await build_runtime(config(tmp_path, live=True))
    assert isinstance(result, Ok)
    runtime = result.value
    qtbot.addWidget(runtime.window)
    try:
        assert runtime.manager is runtime.persistence.manager
        assert runtime.manager is runtime.startup_recovery.manager
        assert runtime.manager is runtime.shutdown_coordinator.manager
        assert runtime.presenter._manager is runtime.manager
        assert runtime.presenter._request_shutdown == runtime.shutdown
        assert runtime.manager._shutdown_hook == runtime.shutdown_coordinator._finish_ordered
        assert constructed == [runtime.manager]
        assert runtime.deps.discovery._subscribers == [runtime.manager.ingest]
        assert runtime.deps.transport._callbacks == [runtime.manager.ingest]
        assert runtime.deps.display._callbacks == [runtime.manager.ingest]
    finally:
        await close_runtime(runtime)


@pytest.mark.asyncio
async def test_production_recovered_cleanup_cannot_disappear_at_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused startup revocation must remain visible to public runtime shutdown."""
    result = await build_runtime(config(tmp_path))
    assert isinstance(result, Ok)
    runtime = result.value
    receiver = target().receiver_id
    saved = replace(SessionSnapshot.idle(receiver), trust=TrustState.CleanupRequired)
    assert isinstance(await runtime.deps.sessions.save(saved), Ok)
    refusals = []

    async def reconciled():
        return Ok(value=())

    async def displays():
        return ()

    async def discovery_ready():
        return Ok(value=None)

    async def refuse_revocation(receiver_id, context):
        refusals.append(receiver_id)
        return Err(error=diagnostic(ErrorCode.trust_cleanup_required, receiver_id))

    monkeypatch.setattr(runtime.registry, "reconcile_previous", reconciled)
    monkeypatch.setattr(runtime.deps.display, "reconcile", displays)
    monkeypatch.setattr(runtime.deps.discovery, "start", discovery_ready)
    monkeypatch.setattr(runtime.deps.discovery, "refresh", discovery_ready)
    monkeypatch.setattr(runtime.deps.pairing, "retry_revocation", refuse_revocation)
    try:
        assert isinstance(await runtime.start(), Ok)
        recovered = runtime.startup_recovery.manager.snapshot(receiver)
        returned = runtime.manager.snapshot(receiver)
        assert recovered.trust is TrustState.CleanupRequired
        assert refusals == [receiver]
        outcome = await runtime.shutdown()
        # Check both observations together so a split fails on the actual state
        # loss AND the successful-looking cleanup, rather than only identity.
        assert (returned.trust, isinstance(outcome, Err)) == (TrustState.CleanupRequired, True)
        assert outcome.error.code is ErrorCode.cleanup_timeout
        assert runtime.manager.snapshot(receiver).trust is TrustState.CleanupRequired
        assert await runtime.shutdown() is outcome
    finally:
        await close_runtime(runtime)


@pytest.mark.asyncio
async def test_production_persistence_observes_live_manager_changes(tmp_path: Path) -> None:
    """An actor publication must reach the JSON file before shutdown runs."""
    settings = config(tmp_path)
    result = await build_runtime(settings)
    assert isinstance(result, Ok)
    runtime = result.value
    receiver = target().receiver_id
    runtime.persistence.start()
    try:
        state = replace(SessionSnapshot.idle(receiver), trust=TrustState.CleanupRequired)
        runtime.manager.install(state)
        runtime.manager.ingest(EffectEvent(
            receiver_id=receiver, operation_id=OperationId(uuid4()),
            session_generation=state.session_generation,
            kind="recovery:persistence_failed", result=Ok(value=None), resource=None,
        ))
        await runtime.manager.drain()
        live = runtime.manager.snapshot(receiver)
        assert live.last_error is not None
        assert live.last_error.code is ErrorCode.persistence_failed
        assert isinstance(await runtime.persistence.flush(), Ok)
        # A fresh store must read the on-disk actor publication, not a cache or
        # a snapshot saved only as a shutdown side effect.
        saved = await JsonSessionStore(AtomicFiles(settings.state_root)).load_all()
        assert len(saved) == 1, "live manager publications never reached the JSON session store"
        assert saved[0].receiver_id == receiver
        assert saved[0].trust is TrustState.CleanupRequired
        assert saved[0].last_error is not None
        assert saved[0].last_error.code is ErrorCode.persistence_failed
        assert runtime._shutdown_task is None
    finally:
        await close_runtime(runtime)
