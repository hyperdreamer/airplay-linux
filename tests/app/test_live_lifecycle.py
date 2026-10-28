"""Exercise the real dashboard, presenter, runtime and application-owned Qt loop.

Only the hardware-facing dependency bundle is replaced. In particular, no test
pumps Qt events on behalf of the entry point or replaces the shutdown coordinator.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from airplay_linux import app
from airplay_linux.diagnostics.activity import ActivityLog
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.runtime import Runtime, RuntimeConfig, build_runtime
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(state_root=tmp_path / "state", runtime_root=tmp_path / "run")


async def _build(config: RuntimeConfig, rig: SessionRig):
    # SessionRig's write-only activity sink has no presenter subscription API.
    return await build_runtime(config, {"deps": rig.deps, "activity": ActivityLog()})


async def _qt_turn() -> None:
    """Require Qt itself to deliver an event while asyncio is running."""
    future = asyncio.get_running_loop().create_future()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(lambda: future.set_result(None) if not future.done() else None)
    timer.start(0)
    try:
        await asyncio.wait_for(future, 0.5)
    except TimeoutError:
        pytest.fail("the application loop did not deliver a Qt timer event")
    finally:
        timer.stop()


async def _until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(0.5):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_live_runtime_start_leaves_dashboard_visible(tmp_path: Path, qapp: QApplication) -> None:
    result = await _build(_config(tmp_path), SessionRig())
    assert isinstance(result, Ok)
    runtime = result.value
    try:
        assert isinstance(await runtime.start(), Ok)
        assert runtime.window.isVisible(), "successful live startup must show the dashboard"
        assert runtime.qt_app is qapp
    finally:
        await runtime.presenter.close()


def _exercise_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    config: RuntimeConfig,
    rig: SessionRig,
    drive: Callable[[Runtime], Awaitable[None]],
) -> tuple[int, Runtime]:
    drivers: list[asyncio.Task[None]] = []
    runtimes: list[Runtime] = []
    watchdogs: list[asyncio.TimerHandle] = []
    loops: list[asyncio.AbstractEventLoop] = []

    async def build(config: RuntimeConfig):
        result = await _build(config, rig)
        assert isinstance(result, Ok)
        runtimes.append(result.value)
        loop = asyncio.get_running_loop()
        loops.append(loop)
        # The production entry point must service this timer without processEvents
        # or a test-owned qasync loop. Plain asyncio.run cannot pass this probe.
        await _qt_turn()
        run_task = asyncio.current_task()
        assert run_task is not None
        watchdogs.append(loop.call_later(3, run_task.cancel))
        drivers.append(asyncio.create_task(drive(result.value)))
        return result

    monkeypatch.setattr(app, "build_runtime", build)
    qt_app = QApplication.instance()
    assert isinstance(qt_app, QApplication)
    quit_on_last_window = qt_app.quitOnLastWindowClosed()
    # Recreate QApplication's production default even if a prior test changed
    # this process-wide setting.
    qt_app.setQuitOnLastWindowClosed(True)
    try:
        status = app._run_guarded(config)
        assert len(drivers) == 1
        assert not drivers[0].cancelled(), "the app exited before the dashboard close completed"
        drivers[0].result()  # propagate assertions made on the owned event loop
        assert loops[0].is_closed(), "the application must release its event loop"
        assert not runtimes[0].window.isVisible()
        return status, runtimes[0]
    finally:
        for timer in watchdogs:
            timer.cancel()
        for runtime in runtimes:
            runtime.window.allow_close()
            runtime.window.close()
        qt_app.setQuitOnLastWindowClosed(quit_on_last_window)


def test_entrypoint_owns_qt_loop_until_dashboard_close_finishes_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qapp: QApplication,
) -> None:
    rig = SessionRig()
    release: Deferred = Deferred()
    rig.transport.queue_result("shutdown", release)

    async def drive(runtime: Runtime) -> None:
        try:
            await _until(runtime.window.isVisible)
            # Let the app's start continuation settle before requesting close:
            # immediate shutdown on startup would enter the blocked adapter here.
            await _qt_turn()
            # Inspect admission to the real shutdown transaction as well as its
            # adapter effects: nested manager/coordinator tasks may not have
            # reached transport.shutdown on this event-loop turn yet.
            assert runtime._shutdown_task is None, (
                "the application requested shutdown before a dashboard close"
            )
            assert "transport.shutdown" not in rig.transport.journal.names(), (
                "the application shut down before a dashboard close request"
            )
            assert runtime.window.close() is False
            await _until(lambda: "transport.shutdown" in rig.transport.journal.names())
            assert runtime.window.isVisible(), "cleanup must finish before the window closes"
            assert rig.discovery.closed
            await _qt_turn()  # the UI remains responsive during asynchronous cleanup
        except BaseException:
            runtime.window.allow_close()
            runtime.window.close()
            raise
        finally:
            release.resolve(Ok(value=None))

    status, runtime = _exercise_entrypoint(monkeypatch, _config(tmp_path), rig, drive)

    assert status == 0
    assert runtime._closed
    assert rig.transport.journal.names().count("transport.shutdown") == 1


def test_live_close_uses_bounded_shutdown_and_retains_unconfirmed_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, qapp: QApplication,
) -> None:
    rig = SessionRig()
    release: Deferred = Deferred()
    rig.transport.queue_result("shutdown", release)
    outcomes = []

    async def drive(runtime: Runtime) -> None:
        try:
            await _until(runtime.window.isVisible)
            # Keep the injected scheduler stalled; only the independent real
            # event-loop deadline can bound this blocked transport shutdown.
            runtime.shutdown_coordinator.deadline_seconds = 0.05
            assert runtime.window.close() is False
            await _until(lambda: "transport.shutdown" in rig.transport.journal.names())
            outcome = await asyncio.wait_for(runtime.shutdown(), 0.75)
            outcomes.append(outcome)
            assert isinstance(outcome, Err)
            assert outcome.error.code is ErrorCode.cleanup_timeout
            await _until(lambda: "closing" not in runtime.window.windowTitle().lower())
            assert runtime.window.isVisible(), "unconfirmed cleanup must retain the dashboard"
            assert not runtime._closed
            await _qt_turn()
        finally:
            release.resolve(Ok(value=None))
            # The failure contract intentionally retains the window. End the
            # test after observing it; the entry point must still report Err.
            runtime.window.allow_close()
            runtime.window.close()

    status, runtime = _exercise_entrypoint(monkeypatch, _config(tmp_path), rig, drive)

    assert status != 0
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], Err)
    assert not runtime._closed
    assert rig.transport.journal.names().count("transport.shutdown") == 1
