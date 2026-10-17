"""Deterministic monotonic time and an in-memory deadline scheduler.

Advancing fake time runs due callbacks; nothing here sleeps, polls, or touches a
real clock, so backoff and expiry are proven without wall-clock delays.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable


class _CancelHandle:
    """Shared cancel/close handle; both names are idempotent."""

    __slots__ = ("_action", "_done")

    def __init__(self, action: Callable[[], None]) -> None:
        self._action: Callable[[], None] | None = action
        self._done = False

    @property
    def cancelled(self) -> bool:
        return self._done

    def cancel(self) -> None:
        if self._done:
            return
        self._done = True
        action, self._action = self._action, None
        if action is not None:
            action()

    def close(self) -> None:
        self.cancel()


class FakeClock:
    """A monotonic clock that only moves when a test moves it."""

    def __init__(self, start: float = 0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("fake time cannot move backwards")
        self._now += float(seconds)
        return self._now

    def _set(self, value: float) -> None:
        self._now = float(value)


class FakeScheduler:
    """A heap of ``(deadline, sequence, callback)`` with stable FIFO ordering."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._heap: list[tuple[float, int, Callable[[], None], bool]] = []
        self._sequence = 0

    def call_at(self, when: float, callback: Callable[[], None]) -> _CancelHandle:
        deadline = float(when)
        self._sequence += 1
        entry: tuple[float, int, Callable[[], None], bool] = (deadline, self._sequence, callback, False)
        heapq.heappush(self._heap, entry)
        return _CancelHandle(lambda: self._cancel(entry))

    def pending(self) -> tuple[float, ...]:
        return tuple(deadline for deadline, _, _, cancelled in sorted(self._heap) if not cancelled)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake time cannot move backwards")
        self._drain(self._clock.now() + float(seconds))

    def _cancel(self, entry: tuple[float, int, Callable[[], None], bool]) -> None:
        """Mark a pending deadline cancelled; ``entry`` is replaced in the heap."""
        index = self._heap.index(entry)
        deadline, sequence, callback, _ = entry
        self._heap[index] = (deadline, sequence, callback, True)
        heapq.heapify(self._heap)

    def _drain(self, target: float) -> None:
        while self._heap and self._heap[0][0] <= target:
            deadline, _, callback, cancelled = heapq.heappop(self._heap)
            if cancelled:
                continue
            self._clock._set(min(max(deadline, self._clock.now()), target))
            callback()
        if self._clock.now() < target:
            self._clock._set(target)
