"""A small single-loop actor engine used by the session coordinator."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.messages import (
    Cancel,
    ChangeResolution,
    ChooseResolution,
    Command,
    CommandReceipt,
    ConfirmOrphanRemoval,
    ConfirmSwitch,
    DiscoveryEvent,
    DisplayEvent,
    EffectEvent,
    Forget,
    KeepCleanup,
    Pair,
    RecreateAccepted,
    ResourceEvent,
    Retry,
    RetryAudio,
    RetryCleanup,
    RetryRevocation,
    SetMuted,
    Start,
    StartVideoOnly,
    Stop,
    SubmitPairing,
    TimerEvent,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    ChallengeView,
    DisplayHandle,
    InstanceId,
    LeaseId,
    OperationContext,
    OperationId,
    OwnerId,
    StreamHandle,
)
from airplay_linux.domain.ports import (
    ActivitySink,
    AudioProbe,
    CapturePort,
    Clock,
    DiscoveryPort,
    DisplayPort,
    IdFactory,
    OwnershipStore,
    PairingPort,
    ReceiverStore,
    Scheduler,
    SessionStore,
    Subscription,
    TransportPort,
)
from airplay_linux.domain.records import DaemonRecord, SessionSnapshot
from airplay_linux.domain.resources import (
    AudioState,
    CapabilityReport,
    DisplayState,
    Err,
    ErrorCode,
    IntentStage,
    Mode,
    Ok,
    PhysicalMonitorSource,
    Result,
    SessionPhase,
    TransportState,
    TrustState,
)
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry

from .fencing import accept_challenge, accept_cleanup, accept_display, accept_transport
from .phase import derive_phase


class Route(StrEnum):
    mirror = "mirror"
    virtual = "virtual"
    trust = "trust"
    cleanup = "cleanup"
    resolution = "resolution"
    reconnect = "reconnect"
    audio = "audio"
    recovery = "recovery"


Handler = Callable[["SessionContext", Command | ResourceEvent], bool]


@dataclass(frozen=True, kw_only=True)
class ManagerDeps:
    transport: TransportPort
    pairing: PairingPort
    display: DisplayPort
    capture: CapturePort
    discovery: DiscoveryPort
    audio_probe: AudioProbe
    sessions: SessionStore
    receivers: ReceiverStore
    ownerships: OwnershipStore
    fences: TrustFenceRegistry
    clock: Clock
    scheduler: Scheduler
    ids: IdFactory
    activity: ActivitySink
    capabilities: Callable[[], CapabilityReport]
    restart_daemon: Callable[[], Awaitable[Result[DaemonRecord]]]


@dataclass
class _Effect:
    receiver_id: ReceiverId
    operation_id: OperationId
    session_generation: int
    route: Route
    name: str
    work: Callable[[], Awaitable[Result[object]]]
    resource: StreamHandle | DisplayHandle | PhysicalMonitorSource | None
    acquisition: bool
    context: OperationContext
    endpoint_fingerprint: str | None
    ownership_uuid: OwnerId | None
    daemon_instance_uuid: InstanceId | None
    task: asyncio.Task[object] | None = None


class _ClosedSubscription:
    def __init__(self, close: Callable[[], None]) -> None:
        self._close = close

    def close(self) -> None:
        self._close()


class SessionContext:
    """Synchronous actor state and effect registration for one receiver."""

    def __init__(self, manager: "SessionManager", state: SessionSnapshot) -> None:
        self._manager = manager
        self.__state = state
        self.deps = manager.deps
        self._timers: list[object] = []
        self._audio_operation_id: OperationId | None = state.audio_operation_id

    @property
    def state(self) -> SessionSnapshot:
        return self.__state

    def _require_dispatch(self) -> None:
        receiver_id = self.state.receiver_id
        if (
            receiver_id not in self._manager._draining
            or self._manager._contexts.get(receiver_id) is not self
        ):
            raise RuntimeError("session context mutation requires actor dispatch")

    def apply(self, **changes: object) -> SessionSnapshot:
        self._require_dispatch()
        changes.pop("phase", None)
        if "audio_operation_id" in changes:
            self._audio_operation_id = cast(
                OperationId | None,
                changes["audio_operation_id"],
            )
        next_state = replace(self.state, **changes)
        next_state = replace(next_state, phase=derive_phase(next_state))
        self.__state = next_state
        self._manager._changed(self.state)
        return self.state

    def operation(self) -> OperationContext:
        return self._context(self._new_operation_id())

    def supersede(self, reason: Literal["start", "cancel", "stop", "switch", "reconfigure", "shutdown"]) -> OperationContext:
        self._require_dispatch()
        del reason
        context = replace(
            self._context(self._new_operation_id(), generation=self.state.session_generation + 1),
            transport_lease_id=None,
            audio_operation_id=None,
        )
        self.apply(
            operation_id=context.operation_id,
            session_generation=context.session_generation,
            transport_lease_id=None,
            audio_operation_id=None,
        )
        self.cancel_timers()
        self.invalidate_trust()
        self._manager._fence_receiver(self.state)
        return replace(context, trust_generation=self.state.trust_generation)

    def new_transport_lease(self, context: OperationContext) -> OperationContext:
        self._require_dispatch()
        lease = LeaseId(self.deps.ids.new())
        updated = replace(context, transport_lease_id=lease)
        self.apply(transport_lease_id=lease)
        return updated

    def invalidate_trust(self) -> None:
        self._require_dispatch()
        generation = self.state.trust_generation + 1
        target = self.state.target if self.state.online else None
        endpoint_fingerprint = target.endpoint_fingerprint if target else None
        endpoint_generation = target.endpoint_generation if target else 0
        self.deps.fences.set(
            TrustFence(
                receiver_id=self.state.receiver_id,
                endpoint_fingerprint=endpoint_fingerprint,
                endpoint_generation=endpoint_generation,
                session_generation=self.state.session_generation,
                trust_generation=generation,
                active_challenge_id=None,
            )
        )
        self.apply(trust_generation=generation, challenge=None)

    def cancel_timers(self) -> None:
        self._require_dispatch()
        for timer in self._timers:
            cancel = getattr(timer, "cancel", None)
            if cancel is not None:
                cancel()
        self._timers.clear()

    def cancel_audio(self) -> None:
        self._require_dispatch()
        self._audio_operation_id = None
        self.apply(audio_operation_id=None)

    def launch(
        self,
        route: Route,
        name: str,
        context: OperationContext,
        work: Callable[[], Awaitable[Result[object]]],
        acquisition: bool = False,
        resource: StreamHandle | DisplayHandle | PhysicalMonitorSource | None = None,
    ) -> None:
        self._require_dispatch()
        effect = _Effect(
            receiver_id=self.state.receiver_id,
            operation_id=context.operation_id,
            session_generation=context.session_generation,
            route=route,
            name=name,
            work=work,
            resource=resource,
            acquisition=acquisition,
            context=context,
            endpoint_fingerprint=self.state.target.endpoint_fingerprint if self.state.target else None,
            ownership_uuid=self.state.ownership_uuid,
            daemon_instance_uuid=(
                self.state.stream.daemon_instance_uuid
                if self.state.stream is not None and self.state.stream.lease_id == context.transport_lease_id
                else None
            ),
        )
        self._manager._launch(effect)

    def enqueue(self, event: ResourceEvent) -> None:
        self._manager.ingest(event)

    def schedule(self, kind: str, when: float, context: OperationContext) -> None:
        self._require_dispatch()

        def fire() -> None:
            self.enqueue(
                TimerEvent(
                    receiver_id=self.state.receiver_id,
                    kind=cast(Literal["reconnect", "pairing_expiry", "cleanup_deadline", "local_retry"], kind),
                    context=context,
                )
            )

        self._timers.append(self.deps.scheduler.call_at(when, fire))

    def outstanding_acquisitions(self, *, names: frozenset[str] | None = None) -> int:
        return self._manager._outstanding(self.state.receiver_id, names=names)

    def _new_operation_id(self) -> OperationId:
        return OperationId(self.deps.ids.new())

    def _context(
        self,
        operation_id: OperationId,
        *,
        generation: int | None = None,
        lease: LeaseId | None = None,
        audio: OperationId | None = None,
    ) -> OperationContext:
        target = self.state.target
        return OperationContext(
            operation_id=operation_id,
            session_generation=self.state.session_generation if generation is None else generation,
            endpoint_generation=target.endpoint_generation if target else 0,
            transport_lease_id=self.state.transport_lease_id if lease is None else lease,
            trust_generation=self.state.trust_generation,
            audio_operation_id=self._audio_operation_id if audio is None else audio,
        )


class SessionManager:
    """Serialize state delivery per receiver while effects run independently."""

    def __init__(self, deps: ManagerDeps, handlers: Mapping[Route, Handler]) -> None:
        self.deps = deps
        self.handlers = dict(handlers)
        self._states: dict[ReceiverId, SessionSnapshot] = {}
        self._contexts: dict[ReceiverId, SessionContext] = {}
        self._queues: dict[ReceiverId, deque[Command | ResourceEvent]] = defaultdict(deque)
        self._draining: set[ReceiverId] = set()
        self._effects: dict[asyncio.Task[object], _Effect] = {}
        self._pending: dict[ReceiverId, list[StreamHandle | DisplayHandle | PhysicalMonitorSource]] = defaultdict(list)
        self._completed_effects: dict[tuple[ReceiverId, OperationId, str], _Effect] = {}
        self._subscribers: list[Callable[[SessionSnapshot], None]] = []
        self._subscriptions: list[Subscription] = []
        self._shutdown_hook: Callable[[], Awaitable[Result[None]]] | None = None
        self._commands_accepted = False
        self._installed: set[ReceiverId] = set()
        self._shutdown = False
        self._shutdown_task: asyncio.Task[Result[None]] | None = None

        self._subscriptions.append(deps.discovery.subscribe(self.ingest))
        self._subscriptions.append(deps.transport.subscribe(self.ingest))
        self._subscriptions.append(deps.display.subscribe(self.ingest))

    async def dispatch(self, command: Command) -> CommandReceipt:
        operation_id = OperationId(self.deps.ids.new())
        receiver_id = command.receiver_id
        if self._shutdown:
            return CommandReceipt(
                operation_id=operation_id,
                accepted=False,
                diagnostic=diagnostic(ErrorCode.unsupported_operation, receiver_id),
            )
        route = self._route(command)
        if route is None or route not in self.handlers:
            return CommandReceipt(
                operation_id=operation_id,
                accepted=False,
                diagnostic=diagnostic(ErrorCode.unsupported_operation, receiver_id),
            )
        self._commands_accepted = True
        self._ensure(receiver_id)
        self._queues[receiver_id].append(command)
        self._schedule_drain(receiver_id)
        return CommandReceipt(operation_id=operation_id, accepted=True, diagnostic=None)

    def ingest(self, event: ResourceEvent) -> None:
        receiver_id = self._receiver_of(event)
        if self._shutdown and not isinstance(event, EffectEvent):
            return
        if receiver_id is None:
            return
        self._ensure(receiver_id)
        self._queues[receiver_id].append(event)
        self._schedule_drain(receiver_id)

    def snapshot(self, receiver_id: ReceiverId) -> SessionSnapshot:
        return self._ensure(receiver_id)

    def context(self, receiver_id: ReceiverId) -> SessionContext:
        self._ensure(receiver_id)
        return self._contexts[receiver_id]

    def subscribe(self, callback: Callable[[SessionSnapshot], None]) -> Subscription:
        self._subscribers.append(callback)
        return _ClosedSubscription(lambda: self._subscribers.remove(callback) if callback in self._subscribers else None)

    async def shutdown(self) -> Result[None]:
        """Join the one shutdown transaction and replay its exact result."""
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_once())
        return await asyncio.shield(self._shutdown_task)

    async def _shutdown_once(self) -> Result[None]:
        self._shutdown = True
        self._subscribers.clear()
        for state in tuple(self._states.values()):
            self._queues[state.receiver_id].append(Stop(receiver_id=state.receiver_id))
            self._schedule_drain(state.receiver_id)
        await self.drain()
        if self._shutdown_hook is not None:
            result = await self._shutdown_hook()
            if isinstance(result, Err):
                return result
        for subscription in self._subscriptions:
            subscription.close()
        self._subscriptions.clear()
        return await self.deps.transport.shutdown()

    async def drain(self) -> None:
        """Apply queued events and callbacks from effects already completed.

        Deliberately unresolved backend tasks are left alone.  A bounded number
        of event-loop turns lets an already-resolved awaitable resume its
        waiter and its completion callback without making ``drain`` wait for a
        genuinely unresolved effect.
        """
        idle_turns = 0
        while True:
            progressed = False
            for receiver_id in tuple(self._queues):
                if self._queues[receiver_id] and receiver_id not in self._draining:
                    self._drain_receiver(receiver_id)
                    progressed = True
            if any(self._queues.values()):
                idle_turns = 0
            elif any(task.done() for task in self._effects):
                idle_turns = 0
            elif self._effects:
                idle_turns += 1
            elif progressed:
                idle_turns = 0
            else:
                return
            if idle_turns >= 3:
                return
            await asyncio.sleep(0)

    def install(self, snapshot: SessionSnapshot) -> None:
        if self._commands_accepted or snapshot.receiver_id in self._installed:
            raise RuntimeError("recovery snapshots must be installed before commands are accepted")
        restored = replace(snapshot, phase=derive_phase(snapshot))
        self._states[snapshot.receiver_id] = restored
        self._contexts[snapshot.receiver_id] = SessionContext(self, restored)
        self._installed.add(snapshot.receiver_id)

    def pending_resources(self, receiver_id: ReceiverId) -> tuple[StreamHandle | DisplayHandle | PhysicalMonitorSource, ...]:
        return tuple(self._pending.get(receiver_id, ()))

    def set_shutdown_hook(self, callback: Callable[[], Awaitable[Result[None]]]) -> None:
        self._shutdown_hook = callback

    @staticmethod
    def _target_from_view(view: object) -> ReceiverTarget:
        record_set = view.record_set
        return ReceiverTarget(
            receiver_id=record_set.receiver_id,
            service_records=record_set.records,
            canonical_host=record_set.canonical_address,
            service_port=record_set.records[0].port if record_set.records else 7000,
            protocol="airplay",
            endpoint_fingerprint=record_set.endpoint_fingerprint,
            endpoint_generation=record_set.endpoint_generation,
        )

    def _ensure(self, receiver_id: ReceiverId) -> SessionSnapshot:
        if receiver_id not in self._states:
            view = self.deps.discovery.current(receiver_id)
            target = self._target_from_view(view) if view is not None else None
            self._states[receiver_id] = SessionSnapshot.idle(receiver_id, online=view.online if view is not None else False)
            self._states[receiver_id] = replace(self._states[receiver_id], target=target, phase=derive_phase(self._states[receiver_id]))
            self._contexts[receiver_id] = SessionContext(self, self._states[receiver_id])
        return self._states[receiver_id]

    def _changed(self, state: SessionSnapshot) -> None:
        self._states[state.receiver_id] = state
        for callback in tuple(self._subscribers):
            callback(state)

    def _schedule_drain(self, receiver_id: ReceiverId) -> None:
        if receiver_id in self._draining:
            return
        self._drain_receiver(receiver_id)

    def _drain_receiver(self, receiver_id: ReceiverId) -> None:
        self._draining.add(receiver_id)
        try:
            queue = self._queues[receiver_id]
            while queue:
                message = queue.popleft()
                context = self._contexts[receiver_id]
                if isinstance(message, DiscoveryEvent):
                    self._apply_discovery(context, message)
                route = self._route(message)
                if route is None:
                    continue
                admitted = self._admit(context, message, route)
                if admitted is None:
                    if isinstance(message, EffectEvent):
                        self._completed_effects.pop((receiver_id, message.operation_id, message.kind), None)
                    continue
                route, message = admitted
                handler = self.handlers.get(route)
                handled = False
                if handler is not None:
                    handled = handler(context, message)
                if isinstance(message, EffectEvent):
                    if handled:
                        self._finish_acquisition(message, context)
                    else:
                        # Delivery is complete even when cleanup retains an
                        # acquired resource in the independent pending ledger.
                        self._completed_effects.pop(
                            (receiver_id, message.operation_id, message.kind), None,
                        )
        finally:
            self._draining.remove(receiver_id)

    def _admit(
        self, context: SessionContext, message: Command | ResourceEvent, route: Route,
    ) -> tuple[Route, Command | ResourceEvent] | None:
        """Check current resource identities immediately before handler delivery."""
        state = context.state
        owned = tuple(self._pending.get(state.receiver_id, ())) + tuple(
            resource for resource in (state.stream, state.display_handle, state.physical_source)
            if resource is not None
        )
        if isinstance(message, TransportEvent):
            if not accept_transport(state, message):
                if message.kind is TransportEventKind.stopped and accept_cleanup(owned, message):
                    return Route.cleanup, message
                return None
        elif isinstance(message, DisplayEvent):
            if not accept_display(state, message):
                if message.kind == "removed" and accept_cleanup(owned, message):
                    return Route.cleanup, message
                return None
        elif isinstance(message, SubmitPairing):
            challenge = state.challenge
            if (
                challenge is None or message.challenge_id != challenge.challenge_id
                or not accept_challenge(state, challenge)
            ):
                return None
        elif isinstance(message, TimerEvent):
            if message.kind == "pairing_expiry":
                challenge = state.challenge
                if (
                    challenge is None or not accept_challenge(state, challenge)
                    or message.context.operation_id != challenge.operation_id
                    or message.context.session_generation != challenge.session_generation
                    or message.context.endpoint_generation != challenge.endpoint_generation
                    or message.context.trust_generation != challenge.trust_generation
                ):
                    return None
            elif not self._context_current(state, message.context):
                return None
        elif isinstance(message, EffectEvent):
            key = (message.receiver_id, message.operation_id, message.kind)
            effect = self._completed_effects.get(key)
            # Trust results must reach the authority that owns compensation.
            if route is Route.trust:
                return route, message
            if effect is not None and effect.acquisition:
                if not self._acquisition_current(state, effect, message.resource):
                    route = Route.cleanup
                    message = replace(message, kind=f"{route.value}:{effect.name}")
                    self._completed_effects.pop(key)
                    self._completed_effects[(message.receiver_id, message.operation_id, message.kind)] = effect
            elif route is Route.cleanup and message.resource is not None and message.resource in owned:
                # Exact release acknowledgements survive intent/endpoint replacement.
                return route, message
            elif effect is not None:
                if not self._effect_current(state, effect):
                    return None
            elif (
                message.session_generation != state.session_generation
                or state.operation_id not in (None, message.operation_id)
            ):
                return None
        return route, message

    @staticmethod
    def _context_current(
        state: SessionSnapshot, operation: OperationContext, *,
        check_operation: bool = True, check_audio: bool = False,
    ) -> bool:
        target = state.target
        return (
            operation.session_generation == state.session_generation
            and operation.endpoint_generation == (target.endpoint_generation if target else 0)
            and operation.transport_lease_id == state.transport_lease_id
            and (not check_audio or operation.audio_operation_id == state.audio_operation_id)
            and (not check_operation or state.operation_id in (None, operation.operation_id))
        )

    def _effect_current(self, state: SessionSnapshot, effect: _Effect) -> bool:
        target = state.target
        current = state.stream
        return (
            self._context_current(
                state, effect.context,
                check_operation=effect.route is not Route.audio or effect.context.audio_operation_id is None,
                check_audio=effect.route is Route.audio,
            )
            and effect.endpoint_fingerprint == (target.endpoint_fingerprint if target else None)
            and (effect.daemon_instance_uuid is None or (
                current is not None and current.daemon_instance_uuid == effect.daemon_instance_uuid
            ))
        )

    def _acquisition_current(
        self, state: SessionSnapshot, effect: _Effect,
        resource: StreamHandle | DisplayHandle | PhysicalMonitorSource | None,
    ) -> bool:
        if effect.session_generation != state.session_generation:
            return False
        if isinstance(resource, DisplayHandle):
            # Display ownership is independent of transport and endpoint churn.
            return (
                effect.ownership_uuid in (None, resource.owner_uuid)
                and state.ownership_uuid in (None, resource.owner_uuid)
                and state.display_handle in (None, resource)
                and (state.ownership_uuid is not None or state.operation_id in (None, effect.operation_id))
            )
        target = state.target
        if (
            not self._context_current(state, effect.context, check_operation=False)
            or effect.endpoint_fingerprint != (target.endpoint_fingerprint if target else None)
        ):
            return False
        if isinstance(resource, StreamHandle):
            current = state.stream
            return (
                resource.receiver_id == state.receiver_id
                and resource.lease_id == effect.context.transport_lease_id
                and resource.start_operation_id == effect.operation_id
                and resource.endpoint_generation == effect.context.endpoint_generation
                and resource.endpoint_fingerprint == effect.endpoint_fingerprint
                and effect.daemon_instance_uuid in (None, resource.daemon_instance_uuid)
                and (current is None or current.lease_id != resource.lease_id
                     or current.daemon_instance_uuid == resource.daemon_instance_uuid)
            )
        return True

    def _apply_discovery(self, context: SessionContext, event: DiscoveryEvent) -> None:
        if event.receiver is None:
            return
        target = self._target_from_view(event.receiver)
        online = event.receiver.online
        previous = context.state
        fence = self.deps.fences.get(previous.receiver_id)
        old_target = previous.target
        endpoint_changed = old_target is None or (
            old_target.endpoint_fingerprint != target.endpoint_fingerprint
            or old_target.endpoint_generation != target.endpoint_generation
            or old_target.canonical_host != target.canonical_host
            or old_target.service_port != target.service_port
            or old_target.protocol != target.protocol
        )
        if fence is not None and (endpoint_changed or previous.online != online):
            # Retire authority synchronously, before publishing discovery or
            # resuming any coordinator await. This also covers assemblies whose
            # transport and pairing ports do not share a target registry.
            self.deps.fences.set(replace(
                fence,
                endpoint_fingerprint=target.endpoint_fingerprint if online else None,
                endpoint_generation=target.endpoint_generation,
                trust_generation=fence.trust_generation + 1,
                active_challenge_id=None,
            ))
        try:
            self.deps.transport.update_target(target if online else None, previous.receiver_id)
        except Exception:  # noqa: BLE001 - keep discovery fencing fail-closed
            context.apply(last_error=diagnostic(ErrorCode.stale_target, previous.receiver_id))
        context.apply(target=target, online=online)

    def _finish_acquisition(self, event: EffectEvent, context: SessionContext) -> None:
        resource = event.resource
        effect = self._completed_effects.pop(
            (event.receiver_id, event.operation_id, event.kind),
            None,
        )
        if resource is None or not isinstance(event.result, Ok):
            return
        if (
            effect is not None
            and effect.acquisition
            and event.kind.startswith(f"{Route.cleanup.value}:")
            and not self._acquisition_current(context.state, effect, resource)
        ):
            return
        pending = self._pending.get(event.receiver_id, [])
        for index, candidate in enumerate(pending):
            if candidate == resource:
                del pending[index]
                break

    def _launch(self, effect: _Effect) -> None:
        task = asyncio.create_task(self._run_effect(effect))
        effect.task = task
        self._effects[task] = effect
        task.add_done_callback(self._effect_done)

    async def _run_effect(self, effect: _Effect) -> object:
        try:
            return await effect.work()
        except asyncio.CancelledError:
            raise
        except Exception:
            return Err(error=diagnostic(ErrorCode.transport_failed, effect.receiver_id))

    def _effect_done(self, task: asyncio.Task[object]) -> None:
        effect = self._effects.pop(task, None)
        if effect is None:
            return
        try:
            result = task.result()
        except asyncio.CancelledError:
            return
        if not isinstance(result, (Ok, Err)):
            result = Err(error=diagnostic(ErrorCode.transport_failed, effect.receiver_id))
        resource = effect.resource
        if resource is None and isinstance(result, Ok) and isinstance(
            result.value, (StreamHandle, DisplayHandle, PhysicalMonitorSource),
        ):
            resource = result.value
        if effect.acquisition and resource is not None and isinstance(result, Ok):
            self._pending[effect.receiver_id].append(resource)
        route = effect.route
        kind = f"{route.value}:{effect.name}"
        self._completed_effects[(effect.receiver_id, effect.operation_id, kind)] = effect
        self.ingest(
            EffectEvent(
                receiver_id=effect.receiver_id,
                operation_id=effect.operation_id,
                session_generation=effect.session_generation,
                kind=kind,
                result=cast(Result[object], result),
                resource=resource,
            )
        )

    def _outstanding(self, receiver_id: ReceiverId, *, names: frozenset[str] | None = None) -> int:
        # A completed acquisition remains uncertain until its queued delivery
        # has been handled. In particular a straggler's recheck runs after that
        # delivery removes the completed-effect record.
        return sum(
            effect.receiver_id == receiver_id and effect.acquisition
            and (names is None or effect.name in names)
            for effect in (*self._effects.values(), *self._completed_effects.values())
        )

    def _fence_receiver(self, state: SessionSnapshot) -> None:
        target = state.target if state.online else None
        self.deps.fences.set(
            TrustFence(
                receiver_id=state.receiver_id,
                endpoint_fingerprint=target.endpoint_fingerprint if target else None,
                endpoint_generation=target.endpoint_generation if target else 0,
                session_generation=state.session_generation,
                trust_generation=state.trust_generation,
                active_challenge_id=None,
            )
        )

    @staticmethod
    def _receiver_of(event: ResourceEvent) -> ReceiverId | None:
        receiver_id = getattr(event, "receiver_id", None)
        if receiver_id is not None:
            return receiver_id
        if isinstance(event, DiscoveryEvent) and event.receiver is not None:
            return event.receiver.record_set.receiver_id
        return None

    @staticmethod
    def _route(message: Command | ResourceEvent) -> Route | None:
        if isinstance(message, Start):
            if message.request.mode is Mode.VIRTUAL:
                return Route.virtual
            return Route.mirror
        if isinstance(message, (Stop, Cancel)):
            return Route.cleanup
        if isinstance(message, (Retry, StartVideoOnly)):
            return Route.reconnect
        if isinstance(message, (Pair, Forget, SubmitPairing, RetryRevocation)):
            return Route.trust
        if isinstance(message, (ChooseResolution, ChangeResolution, RecreateAccepted)):
            return Route.resolution
        if isinstance(message, (SetMuted, RetryAudio)):
            return Route.audio
        if isinstance(message, (RetryCleanup, KeepCleanup, ConfirmOrphanRemoval)):
            return Route.cleanup
        if isinstance(message, ConfirmSwitch):
            return Route.cleanup
        if isinstance(message, (DiscoveryEvent, TransportEvent)):
            return Route.reconnect
        if isinstance(message, DisplayEvent):
            return Route.virtual
        if isinstance(message, TimerEvent):
            return Route.reconnect
        if isinstance(message, EffectEvent):
            prefix = message.kind.split(":", 1)[0]
            try:
                return Route(prefix)
            except ValueError:
                return Route.recovery
        return None


__all__ = ["Handler", "ManagerDeps", "Route", "SessionContext", "SessionManager", "derive_phase"]
