"""Bound the complete shutdown, including cancellation-resistant finalizers.

The manager closes command acceptance and sends each actor one Stop. Its hook
joins the existing ordered transactions; receiver effects run concurrently.
The final three seconds are reserved for registry/supervisor escalation and
safe metadata flushing, inside (never after) the whole-application deadline.
"""
from __future__ import annotations

import asyncio
import math

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import EffectEvent
from airplay_linux.domain.operations import OperationId
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result, TrustState

from .cleanup import cleanup_complete
from .engine import Route, SessionManager
from .recovery import build_recovery_handlers


class ShutdownCoordinator:
    def __init__(self, manager: SessionManager, deps, registry, persistence,
                 deadline_seconds: float = 15) -> None:
        if type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds) or not 0 < deadline_seconds <= 15:
            raise ValueError("shutdown deadline must be positive and at most 15 seconds")
        self.manager, self.deps = manager, deps
        self.registry, self.persistence = registry, persistence
        self.deadline_seconds = float(deadline_seconds)
        self._task = None
        self._registry_task = None
        self._manager_task = None
        self._failure = None
        self._grace = None
        self._final = None
        self._background: set[asyncio.Task] = set()
        manager.handlers.setdefault(Route.recovery, build_recovery_handlers(deps)[Route.recovery])
        manager.set_shutdown_hook(self._finish_ordered)

    async def run(self) -> Result[None]:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        # GUI cancellation cannot abandon the single shared shutdown transaction.
        return await asyncio.shield(self._task)

    def _spawn(self, work) -> asyncio.Task:
        task = asyncio.create_task(work)
        self._background.add(task)

        def finished(done):
            self._background.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    async def _result(self, work) -> Result[None]:
        try:
            result = await work()
        except Exception:  # never expose provider/keyring exception strings
            result = Err(error=diagnostic(ErrorCode.cleanup_timeout))
        if not isinstance(result, (Ok, Err)):
            result = Err(error=diagnostic(ErrorCode.cleanup_timeout))
        if isinstance(result, Err):
            self._failure = result
        return result

    def _registry_shutdown(self):
        if self._registry_task is None:
            self._registry_task = self._spawn(self._result(self.registry.shutdown))
        return self._registry_task

    async def _run(self):
        loop = asyncio.get_running_loop()
        self._grace, self._final = loop.create_future(), loop.create_future()
        started = self.deps.clock.now()
        reserve = min(3.0, self.deadline_seconds / 3.0)

        def fire(future):
            if not future.done():
                future.set_result(None)

        # The injected clock proves policy deterministically; independent loop
        # deadlines still bound an adapter when the injected scheduler stalls.
        timers = [
            self.deps.scheduler.call_at(started + self.deadline_seconds - reserve, lambda: fire(self._grace)),
            self.deps.scheduler.call_at(started + self.deadline_seconds, lambda: fire(self._final)),
            loop.call_later(self.deadline_seconds - reserve, fire, self._grace),
            loop.call_later(self.deadline_seconds, fire, self._final),
        ]
        self._manager_task = self._spawn(self._result(self.manager.shutdown))
        try:
            done, _ = await asyncio.wait((self._manager_task, self._final), return_when=asyncio.FIRST_COMPLETED)
            if self._final in done:
                self._failure = Err(error=diagnostic(ErrorCode.cleanup_timeout))
                self._mark_pending()
                # If the hook itself never ran (or is still waiting on an
                # adapter), the exact registry still owns escalation. Never
                # await cancellation acknowledgement past the hard deadline.
                self._registry_shutdown()
                self._spawn(self.persistence.flush())
                return self._failure
            result = self._manager_task.result()
            return self._failure or result
        finally:
            for timer in timers:
                timer.cancel()
            for subscription in tuple(self.manager._subscriptions):
                subscription.close()
            self.manager._subscriptions.clear()

    def _resources_complete(self):
        return all(
            cleanup_complete(state, self.manager.context(state.receiver_id).outstanding_acquisitions(),
                             self.manager.pending_resources(state.receiver_id))
            and state.trust not in (TrustState.Pairing, TrustState.RevocationPending, TrustState.CleanupRequired)
            for state in self.manager._states.values()
        ) and not self.manager._effects

    def _mark_pending(self):
        for state in tuple(self.manager._states.values()):
            self.manager.ingest(EffectEvent(
                receiver_id=state.receiver_id,
                operation_id=state.operation_id or OperationId(self.deps.ids.new()),
                session_generation=state.session_generation, kind="recovery:shutdown_pending",
                result=Ok(value=None), resource=None,
            ))
            self.persistence.record(self.manager.snapshot(state.receiver_id))

    async def _finish_ordered(self):
        # manager.shutdown clears UI subscribers before Stop. Subscribe here,
        # after that barrier, to wake on every actor resource acknowledgement.
        changed = asyncio.Event()
        subscription = self.manager.subscribe(lambda state: changed.set())
        try:
            while not self._resources_complete():
                if self._grace.done():
                    self._failure = Err(error=diagnostic(ErrorCode.cleanup_timeout))
                    self._mark_pending()
                    break
                effects = tuple(self.manager._effects)
                if not effects:
                    # An explicit refused release is terminal until RetryCleanup;
                    # spinning or waiting until the deadline cannot confirm it.
                    self._failure = Err(error=diagnostic(ErrorCode.cleanup_timeout))
                    self._mark_pending()
                    break
                changed.clear()
                wake = asyncio.create_task(changed.wait())
                try:
                    await asyncio.wait((*effects, wake, self._grace), return_when=asyncio.FIRST_COMPLETED)
                    await self.manager.drain()
                finally:
                    wake.cancel()
            for state in tuple(self.manager._states.values()):
                self.persistence.record(state)
            # Registry.shutdown authenticates ownership and delegates TERM/KILL
            # escalation to the supervisor. Session code never signals a PID.
            await asyncio.gather(
                asyncio.shield(self._registry_shutdown()),
                self._result(self.persistence.close),
                self._result(self._close_discovery),
            )
            # Let the manager close transport even after a receiver refused
            # cleanup. The coordinator retains the independent failure Result.
            return Ok(value=None)
        finally:
            subscription.close()

    async def _close_discovery(self):
        await self.deps.discovery.close()
        return Ok(value=None)


__all__ = ["ShutdownCoordinator"]
