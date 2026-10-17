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


class _Entry:
    """One scheduled deadline with a lifecycle independent of heap membership.

    A fired entry is popped from the heap, so cancellation has to work from the
    entry itself rather than by locating it in the heap; a handle that outlives
    its deadline (or that cancels itself from inside its own callback) is a
    harmless no-op.
    """

    __slots__ = ("deadline", "sequence", "callback", "cancelled", "fired")

    def __init__(self, deadline: float, sequence: int, callback: Callable[[], None]) -> None:
        self.deadline = deadline
        self.sequence = sequence
        self.callback = callback
        self.cancelled = False
        self.fired = False


class FakeScheduler:
    """A heap of ``(deadline, sequence, entry)`` with stable FIFO ordering."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock
        self._heap: list[tuple[float, int, _Entry]] = []
        self._sequence = 0

    def call_at(self, when: float, callback: Callable[[], None]) -> _CancelHandle:
        deadline = float(when)
        self._sequence += 1
        entry = _Entry(deadline, self._sequence, callback)
        heapq.heappush(self._heap, (deadline, entry.sequence, entry))
        return _CancelHandle(lambda: self._cancel(entry))

    def pending(self) -> tuple[float, ...]:
        return tuple(
            sorted(
                entry.deadline
                for _, _, entry in self._heap
                if not entry.cancelled and not entry.fired
            )
        )

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("fake time cannot move backwards")
        self._drain(self._clock.now() + float(seconds))

    def _cancel(self, entry: _Entry) -> None:
        entry.cancelled = True

    def _drain(self, target: float) -> None:
        while self._heap and self._heap[0][0] <= target:
            _, _, entry = heapq.heappop(self._heap)
            if entry.cancelled:
                continue
            entry.fired = True
            self._clock._set(min(max(entry.deadline, self._clock.now()), target))
            entry.callback()
        if self._clock.now() < target:
            self._clock._set(target)
