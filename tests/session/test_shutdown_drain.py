"""Pin the effect drain inside the bounded acquisition window (OBS-002).

``ShutdownCoordinator._await_acquisitions`` must deliver already-queued callbacks
between waits.  An acquisition whose completion callback registers a *further*
acquisition is only visible after a drain: without it the coordinator sees an empty
pending set, returns early, and tears down while an acquisition is still in flight.
That ordering is the whole reason the drain exists, so it is pinned here rather than
left as untested defensive redundancy.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from airplay_linux.session.shutdown import ShutdownCoordinator


class _Manager:
    """Minimal effect ledger: drain() can register work the first time it runs."""

    def __init__(self) -> None:
        self._effects: dict[asyncio.Task[object], SimpleNamespace] = {}
        self.drained = 0
        self.register_on_first_drain = False
        self.second_started: asyncio.Event | None = None

    async def drain(self) -> None:
        self.drained += 1
        if self.drained == 1 and self.register_on_first_drain:
            task = asyncio.ensure_future(_slow_acquisition(self.second_started))
            self._effects[task] = SimpleNamespace(acquisition=True)


async def _slow_acquisition(started: asyncio.Event | None) -> None:
    if started is not None:
        started.set()
    await asyncio.sleep(0.05)


def _coordinator(manager: _Manager) -> ShutdownCoordinator:
    coordinator = object.__new__(ShutdownCoordinator)
    coordinator.manager = manager
    loop = asyncio.get_running_loop()
    coordinator._grace = loop.create_future()
    coordinator._final = loop.create_future()
    return coordinator


@pytest.mark.asyncio
async def test_queued_callback_acquisition_is_drained_before_teardown_proceeds() -> None:
    """A second acquisition registered by a drained callback must still be awaited."""

    manager = _Manager()
    manager.register_on_first_drain = True
    manager.second_started = asyncio.Event()
    first = asyncio.ensure_future(_slow_acquisition(None))
    manager._effects[first] = SimpleNamespace(acquisition=True)

    coordinator = _coordinator(manager)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator._await_acquisitions(5.0)
    elapsed = loop.time() - started

    assert manager.drained >= 1, "the coordinator never drained the effect ledger"
    assert manager.second_started is not None and manager.second_started.is_set(), (
        "the callback-registered acquisition was never started, so teardown would have "
        "proceeded with an acquisition in flight"
    )
    assert elapsed >= 0.05, "the coordinator returned before the second acquisition settled"


@pytest.mark.asyncio
async def test_window_expires_without_waiting_for_unsettled_acquisition() -> None:
    """The bounded window still owns the budget: a hung acquisition cannot extend it."""

    manager = _Manager()
    hung = asyncio.ensure_future(asyncio.sleep(10))
    manager._effects[hung] = SimpleNamespace(acquisition=True)

    coordinator = _coordinator(manager)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await coordinator._await_acquisitions(0.05)
    elapsed = loop.time() - started
    hung.cancel()
    try:
        await hung
    except asyncio.CancelledError:
        pass

    assert elapsed < 2.0, "the acquisition window did not honour its bound"
