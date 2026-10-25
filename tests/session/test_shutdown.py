"""Shutdown owns the whole deadline, including blocked adapter finalizers."""
from __future__ import annotations

import asyncio

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import EffectEvent, Retry, Start
from airplay_linux.domain.operations import OperationId
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, DisplayState, Err, ErrorCode, Mode, Ok,
    StartRequest, TransportState, TrustState,
)
from airplay_linux.session.engine import Route
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import target
from test_startup_recovery import Registry, install_handlers, prepared, recover
from virtual_scenarios import decision, make_rig, settle as settle_resources


async def settle(rig):
    # The coordinator and persister run outside the manager's effect ledger.
    # Yield their scheduled tasks as well as draining actor effects.
    for _ in range(12):
        await asyncio.sleep(0)
        await settle_resources(rig)


def actor_input(rig, **changes):
    """Install a test precondition through the actor's dispatch boundary."""
    previous = rig.manager.handlers[Route.recovery]

    def apply(ctx, message):
        ctx.apply(**changes)
        return True

    rig.manager.handlers[Route.recovery] = apply
    state = rig.manager.snapshot(rig.receiver_id)
    rig.manager.ingest(EffectEvent(
        receiver_id=rig.receiver_id, operation_id=state.operation_id or OperationId(rig.ids.new()),
        session_generation=state.session_generation, kind="recovery:test_input",
        result=Ok(value=None), resource=None,
    ))
    rig.manager.handlers[Route.recovery] = previous


async def coordinator(rig, registry=None, *, deadline_seconds=15):
    from airplay_linux.session.persistence import SessionPersistence
    from airplay_linux.session.shutdown import ShutdownCoordinator

    registry = registry or Registry([])
    persistence = SessionPersistence(rig.manager, rig.sessions)
    persistence.start()
    return ShutdownCoordinator(rig.manager, rig.deps, registry, persistence, deadline_seconds), registry


async def active_rig():
    rig = install_handlers(make_rig())
    rig.discovery.add_target(target())
    await rig.manager.dispatch(Start(receiver_id=rig.receiver_id, request=StartRequest(
        mode=Mode.VIRTUAL, resolution=decision(), audio=AudioPolicy(enabled=False),
    )))
    await settle(rig)
    assert rig.manager.snapshot(rig.receiver_id).transport is TransportState.Streaming
    return rig


@pytest.mark.asyncio
async def test_idle_shutdown_has_no_unresolved_owned_resource():
    rig = SessionRig()
    result = await rig.manager.shutdown()
    assert isinstance(result, Ok)
    assert not rig.manager.pending_resources(rig.receiver_id)


@pytest.mark.asyncio
async def test_idle_coordinator_shutdown_flushes_and_shuts_registry_once():
    rig = install_handlers(SessionRig())
    shutdown, registry = await coordinator(rig)
    first, second = await asyncio.gather(shutdown.run(), shutdown.run())
    assert isinstance(first, Ok) and first == second
    assert isinstance(await shutdown.run(), Ok)
    assert registry.shutdowns == 1
    assert not rig.scheduler.pending()


@pytest.mark.asyncio
async def test_shutdown_reuses_ordered_cleanup_and_persists_confirmed_release(tmp_path):
    from airplay_linux.storage import AtomicFiles, JsonSessionStore

    rig, process, ownerships, handle = await prepared(tmp_path, online=True)
    await recover(rig)
    await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    await settle(rig)
    before = rig.manager.snapshot(rig.receiver_id)
    assert before.transport is TransportState.Streaming
    order = []
    prepare, stop, remove = rig.display.prepare_teardown, rig.transport.stop_stream, rig.display.remove

    async def published(*args):
        result = await prepare(*args)
        order.append("intent")
        assert ownerships.records[handle.owner_uuid].desired_state == "removing"
        return result

    async def transport_stopped(*args):
        assert order[-1] == "intent"
        order.append("transport")
        return await stop(*args)

    async def audio_stopped(*args):
        assert order[-1] == "transport"
        order.append("audio")
        return Ok(value=None)

    async def removed(*args):
        assert order[-1] == "audio"
        order.append("display")
        return await remove(*args)

    rig.display.prepare_teardown, rig.transport.stop_stream, rig.display.remove = published, transport_stopped, removed
    rig.audio.stop_control = audio_stopped
    # Install an audio lease as a test input before shutdown; the actor owns its
    # release, and cannot clear it until transport release has been confirmed.
    actor_input(rig, audio=AudioState.Enabled)
    disk = JsonSessionStore(AtomicFiles(tmp_path / "sessions"))
    rig.sessions = disk
    shutdown, registry = await coordinator(rig)
    result = await shutdown.run()
    assert isinstance(result, Ok)
    assert order == ["intent", "transport", "audio", "display"]
    assert registry.shutdowns == 1
    after = rig.manager.snapshot(rig.receiver_id)
    assert after.session_generation == before.session_generation + 1
    assert not after.cleanup_pending and after.display_handle is None and after.stream is None
    assert not ownerships.records
    assert process.identity(handle.helper.pid) is None
    (saved,) = await disk.load_all()
    assert not saved.intent_active and not saved.cleanup_pending
    assert saved.display is DisplayState.None_
    receipt = await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))
    assert not receipt.accepted


@pytest.mark.asyncio
async def test_failed_removal_intent_blocks_all_destructive_receiver_teardown():
    rig = await active_rig()
    before = rig.manager.snapshot(rig.receiver_id)
    rig.display.queue_result("prepare_teardown", Err(error=diagnostic(ErrorCode.persistence_failed)))
    shutdown, registry = await coordinator(rig)
    assert isinstance(await shutdown.run(), Err)
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.cleanup_pending and state.stream == before.stream
    assert state.display_handle == before.display_handle
    assert not rig.journal.calls("transport.stop_stream")
    assert not rig.journal.calls("display.remove")
    assert before.ownership_uuid in rig.ownerships.records
    assert registry.shutdowns == 1
    assert rig.sessions.saved[rig.receiver_id].cleanup_pending


@pytest.mark.asyncio
async def test_receiver_cleanups_run_concurrently_without_waiting_for_other_receiver():
    rig = await active_rig()
    second = target("b")
    rig.discovery.add_target(second)
    await rig.manager.dispatch(Start(receiver_id=second.receiver_id, request=StartRequest(
        mode=Mode.VIRTUAL, resolution=decision(second), audio=AudioPolicy(enabled=False),
    )))
    await settle(rig)
    held = Deferred()
    rig.transport.queue_result("stop_stream", held)
    shutdown, registry = await coordinator(rig)
    task = asyncio.create_task(shutdown.run())
    await settle(rig)
    states = [rig.manager.snapshot(receiver) for receiver in (rig.receiver_id, second.receiver_id)]
    assert sum(state.stream is None and state.display_handle is None for state in states) == 1
    assert not task.done()
    held.resolve(Ok(value=None))
    assert isinstance(await asyncio.wait_for(task, 1), Ok)
    assert registry.shutdowns == 1


@pytest.mark.asyncio
async def test_fifteen_second_deadline_preserves_exact_pending_resources_and_escalates():
    rig = await active_rig()
    before = rig.manager.snapshot(rig.receiver_id)
    held = Deferred()
    rig.transport.queue_result("stop_stream", held)
    shutdown, registry = await coordinator(rig)
    task = asyncio.create_task(shutdown.run())
    await settle(rig)
    rig.scheduler.advance(11.9)
    await settle(rig)
    assert registry.shutdowns == 0
    assert not task.done()
    rig.scheduler.advance(0.1)  # reserve the supervisor's three-second escalation grace
    await settle(rig)
    assert registry.shutdowns == 1
    rig.scheduler.advance(3)
    result = await asyncio.wait_for(task, 0.5)
    assert isinstance(result, Err) and result.error.code is ErrorCode.cleanup_timeout
    state = rig.manager.snapshot(rig.receiver_id)
    assert state.cleanup_pending
    assert state.stream == before.stream and state.display_handle == before.display_handle
    assert before.ownership_uuid in rig.ownerships.records
    record = rig.ownerships.records[before.ownership_uuid]
    assert record.stage == record.desired_state == "removing"
    assert rig.sessions.saved[rig.receiver_id].cleanup_pending
    assert not rig.journal.calls("display.remove")
    # A timeout is not release. A later exact acknowledgement still rejoins the
    # original ordered transaction, rather than losing ownership at GUI exit.
    held.resolve(Ok(value=None))
    await settle(rig)
    assert rig.manager.snapshot(rig.receiver_id).stream is None


@pytest.mark.asyncio
async def test_real_deadline_survives_stalled_scheduler_and_cancelled_gui_waiter():
    rig = await active_rig()
    gate, entered = asyncio.Event(), asyncio.Event()

    async def blocked_close():
        entered.set()
        await gate.wait()
        return Ok(value=None)

    rig.transport.shutdown = blocked_close
    shutdown, registry = await coordinator(rig, deadline_seconds=0.05)
    caller = asyncio.create_task(shutdown.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        result = await asyncio.wait_for(shutdown.run(), 0.5)
        assert isinstance(result, Err) and result.error.code is ErrorCode.cleanup_timeout
        assert registry.shutdowns == 1
        assert rig.clock.now() == 0  # injected scheduler never advanced
        assert not rig.scheduler.pending()
    finally:
        gate.set()
        await settle(rig)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["registry", "transport", "persistence", "keyring"])
async def test_deadline_covers_each_shutdown_finalizer_without_waiting_for_cancellation(blocked):
    rig = await active_rig()
    registry = Registry([])
    gate, entered = asyncio.Event(), asyncio.Event()

    async def stubborn(*args):
        entered.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await gate.wait()
        return Ok(value=None)

    if blocked == "registry":
        async def registry_stop():
            registry.shutdowns += 1
            return await stubborn()
        registry.shutdown = registry_stop
    elif blocked == "transport":
        rig.transport.shutdown = stubborn
    elif blocked == "persistence":
        rig.sessions.save = stubborn
    else:
        # A restored revocation tombstone is independent from display release.
        actor_input(rig, trust=TrustState.CleanupRequired)
        rig.pairing.retry_revocation = stubborn
        from airplay_linux.domain.messages import RetryRevocation
        await rig.manager.dispatch(RetryRevocation(receiver_id=rig.receiver_id))
    shutdown, _ = await coordinator(rig, registry)
    task = asyncio.create_task(shutdown.run())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await settle(rig)
        rig.scheduler.advance(15)
        result = await asyncio.wait_for(task, 0.5)
        assert isinstance(result, Err)
        assert registry.shutdowns == 1
        assert not (await rig.manager.dispatch(Retry(receiver_id=rig.receiver_id))).accepted
        if blocked == "keyring":
            assert rig.manager.snapshot(rig.receiver_id).trust is TrustState.CleanupRequired
            assert rig.sessions.saved[rig.receiver_id].trust is TrustState.CleanupRequired
    finally:
        gate.set()
        await settle(rig)
