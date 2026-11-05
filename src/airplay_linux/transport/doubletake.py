"""Concurrent, target-bound transport and the application's sole pairing authority.

The published control surface addresses targets, not wire stream IDs. Handles
therefore carry a local request correlation ID; it is never sent as an invented
backend field. Unsolicited backend notices lack that correlation and cannot
supply stream state. Only exact request replies and owned daemon exits do so.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from inspect import Parameter, signature
from functools import lru_cache
from typing import TYPE_CHECKING

from airplay_linux.backend.contract import AckReply, BackendFeatures, BackendReply, StatusReply
from airplay_linux.capture.source import (
    SourceObservation, SourceRefusal, SourceVerificationRefusal,
)
from airplay_linux.backend.control import (
    BackendControl, BackendOperation, BackendPayload, ConnectPayload, EmptyPayload,
    Handshake, ModesPayload, MutePayload, OwnershipProof, requires_dispatch_fence,
    VolumePayload, AudioGatePayload,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.messages import (
    EmptyPayload as EventEmpty, FailurePayload, MutePayload as EventMute,
    SourceVerificationPayload, TransportEvent, TransportEventKind, VolumePayload as EventVolume,
)
from airplay_linux.domain.operations import (
    ChallengeId, InstanceId, LeaseId, OperationContext, PairingChallenge,
    PairingResult, SensitiveText, StreamHandle,
)
from airplay_linux.domain.ports import (
    CapturePort, Clock, CredentialStore, IdFactory, ReceiverStore, Scheduler, Subscription,
)
from airplay_linux.domain.records import AudioStartReceipt, DaemonRecord
from airplay_linux.domain.resources import (
    AudioPolicy, AudioState, CaptureSource, Err, ErrorCode, Ok,
    PhysicalMonitorSource, ReceiverCapabilities, Result, VirtualOutputSource, VolumeLevel,
)
from airplay_linux.process.daemon_registry import DaemonNotice, DaemonRegistry
from airplay_linux.trust.coordinator import PairingCoordinator
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import KeyringPairingPort

from .targets import TargetRegistry

if TYPE_CHECKING:
    from airplay_linux.capture.source import ReportedSourceVerifier


CONTROL_TIMEOUT = 5.0
SOURCE_TIMEOUT = 5.0
SOURCE_POLL_INTERVAL = 0.1
SHUTDOWN_TIMEOUT = 15.0
#: Budget for an accepted ``connect`` to reach streaming.  The published
#: backend answers the command before its RTSP handshake and capture
#: preparation finish, so the acknowledgement is progress, not readiness.
CONNECT_STREAMING_TIMEOUT = 20.0
#: A desktop capture waits for the screen prompt the portal shows. That is a
#: human answer, so the permission phase is generously bounded and remains
#: cancellable; only an unanswered prompt reaches this ceiling.
PERMISSION_WAIT_TIMEOUT = 300.0
#: Consecutive status reads that may fail before the control is treated as
#: unusable. A single timeout is normal while the backend waits on the portal.
STATUS_FAILURE_BUDGET = 3


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


@lru_cache(maxsize=None)
def _request_accepts_dispatch_fence(method: object) -> bool:
    """True when this control's ``request`` accepts a ``dispatch_fence`` keyword.

    This establishes **keyword compatibility only**, and only for a parameter
    that can genuinely be passed by keyword: a ``POSITIONAL_ONLY`` or
    ``VAR_POSITIONAL`` parameter that merely shares the name does not qualify.
    Beyond that it shows nothing — not that a control, decorator or wrapper
    forwards the keyword to the adapter that spawns, and not the predicate's
    origin or meaning.

    What the answer is used for: transport refuses to invoke a control that
    cannot accept a fence for a target-addressed state change (see ``_request``),
    and the adapter independently refuses such a change when no fence arrives.
    Between them, a missing or rejecting fence blocks the six fenced operations.
    Neither this check nor the adapter's guard can stop same-privilege code that
    substitutes a permissive predicate, changes the target, or bypasses
    ``request`` entirely; see the trust-boundary note on
    :class:`DoubleTakeTransport`.
    """

    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return False
    # The keyword can be delivered by a parameter that actually binds keywords.
    # A same-named ``POSITIONAL_ONLY`` or ``VAR_POSITIONAL`` parameter cannot (F-7),
    # so it must not short-circuit the catchall check: ``(..., dispatch_fence, /,
    # **kwargs)`` still accepts the keyword through ``**kwargs`` (F-8).
    candidate = parameters.get("dispatch_fence")
    if candidate is not None and candidate.kind in {
        Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY,
        Parameter.VAR_KEYWORD,
    }:
        return True
    return any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


@lru_cache(maxsize=None)
def _request_accepts_ownership_proof(method: object) -> bool:
    try:
        parameters = signature(method).parameters
    except (TypeError, ValueError):
        return False
    candidate = parameters.get("ownership_proof")
    if candidate is not None and candidate.kind in {
        Parameter.POSITIONAL_OR_KEYWORD, Parameter.KEYWORD_ONLY,
        Parameter.VAR_KEYWORD,
    }:
        return True
    return any(
        parameter.kind is Parameter.VAR_KEYWORD for parameter in parameters.values()
    )

def _accepts_dispatch_fence(control: object) -> bool:
    method = getattr(control, "request", None)
    if method is None:
        return False
    return _request_accepts_dispatch_fence(getattr(method, "__func__", method))


def _accepts_ownership_proof(control: object) -> bool:
    method = getattr(control, "request", None)
    if method is None:
        return False
    return _request_accepts_ownership_proof(getattr(method, "__func__", method))


async def _safe(operation: Awaitable, code: ErrorCode = ErrorCode.transport_failed):
    try:
        result = await operation
    except Exception:
        return _failure(code)
    if isinstance(result, Ok):
        return result
    if isinstance(result, Err) and type(result.error.code) is ErrorCode:
        return _failure(result.error.code)
    return _failure(code)


async def _safe_source_verification(operation: Awaitable):
    """Retain the typed refusal produced by the source verifier."""
    try:
        result = await operation
    except Exception:
        return _failure(ErrorCode.unsupported_source)
    if isinstance(result, Ok):
        return result
    if isinstance(result, SourceVerificationRefusal):
        return result
    return _failure(ErrorCode.unsupported_source)


def _ack(result, operation: BackendOperation, target: ReceiverTarget) -> bool:
    return isinstance(result, Ok) and isinstance(result.value, AckReply) and (
        result.value.operation == operation.value
        and result.value.target == target.canonical_host
    )


@dataclass
class _Subscription:
    action: Callable[[], None] | None

    def close(self) -> None:
        action, self.action = self.action, None
        if action is not None:
            action()


@dataclass(kw_only=True, repr=False)
class _Daemon:
    record: DaemonRecord
    control: BackendControl
    ownership_proof: OwnershipProof | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    authenticated: bool = False
    alive: bool = True


@dataclass(kw_only=True, repr=False)
class _VolumeTask:
    handle: StreamHandle
    context: OperationContext
    level: VolumeLevel
    task: asyncio.Task | None = None
    expired: bool = False


@dataclass(kw_only=True, repr=False)
class _CloseTask:
    task: asyncio.Task
    expired: bool = False


@dataclass(kw_only=True, repr=False)
class _Stream:
    target: ReceiverTarget
    context: OperationContext
    source: CaptureSource
    audio_policy: AudioPolicy
    lease: LeaseId
    audio: AudioState
    daemon: _Daemon | None = None
    handle: StreamHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    ready: bool = False
    muted: bool = False
    initial_volume: VolumeLevel | None = None
    audio_closed: bool = False
    volume_state: str | None = None
    has_audio: bool = False
    volume_task: _VolumeTask | None = None
    close_task: _CloseTask | None = None
    source_observation: SourceObservation | None = None
    abandoned: bool = False
    released: bool = False


@dataclass(kw_only=True, repr=False)
class _PairingBinding:
    target: ReceiverTarget
    context: OperationContext
    daemon: _Daemon | None = None


class _PairingControl:
    """Only the private coordinator uses this request router."""

    def __init__(self, transport: DoubleTakeTransport) -> None:
        self._transport = transport

    async def request(
        self, operation: BackendOperation, target: ReceiverTarget | None,
        payload: BackendPayload, context: OperationContext | None,
    ) -> Result[BackendReply]:
        binding = self._transport._pairings.get(target.receiver_id) if target is not None else None
        if binding is None or target != binding.target or context is None:
            return _failure(ErrorCode.stale_target)
        checked = self._transport._pairing_current(binding)
        if isinstance(checked, Err):
            return checked
        return await self._transport._request(binding.daemon, operation, target, payload, context)

    def subscribe(self, callback) -> Subscription:
        # Uncorrelated backend notices cannot confer pairing authority.
        return _Subscription(action=None)

    async def handshake(self, owner_token, instance_uuid) -> Result[Handshake]:
        # Authentication belongs to admission of a concrete owned daemon.
        return _failure(ErrorCode.unsupported_operation)

    async def close(self) -> None:
        # The transport owns the shared controls, including their shutdown.
        pass


class _PairingCredentials:
    """Keep every credential transaction under the coordinator and its route.

    The supplied store owns all opaque references. This wrapper only fences its
    calls; it neither manufactures credentials nor implements authentication.
    Cleanup can use the retained old endpoint only while its address is safe.
    """

    def __init__(self, transport: DoubleTakeTransport, store: CredentialStore) -> None:
        self._transport, self._store = transport, store

    async def begin_pairing_token(self, target, context):
        if not isinstance(self._store, KeyringPairingPort):
            return _failure(ErrorCode.keyring_unavailable)
        binding = self._transport._pairings[target.receiver_id]
        admitted = await self._transport._admit(target, context)
        if isinstance(admitted, Err):
            return admitted
        binding.daemon = admitted.value
        checked = self._transport._pairing_current(binding)
        if isinstance(checked, Err):
            return checked
        return await self._store.begin_pairing_token(target, context)

    async def load_credential_ref(self, receiver_id):
        return await self._store.load_credential_ref(receiver_id)

    def _check(self, receiver_id, *, cleanup=False):
        binding = self._transport._pairings.get(receiver_id)
        if binding is None:
            return _failure(ErrorCode.keyring_unavailable)
        return self._transport._pairing_current(binding, cleanup=cleanup)

    async def prepare_pairing(self, challenge, backend_result):
        checked = self._check(challenge.receiver_id)
        if isinstance(checked, Err):
            return checked
        result = await self._store.prepare_pairing(challenge, backend_result)
        if isinstance(result, Ok):
            self._transport._provisional_receivers[result.value] = challenge.receiver_id
        return result

    async def commit_pairing(self, provisional_ref, trust_generation):
        receiver_id = self._transport._provisional_receivers.get(provisional_ref)
        checked = self._check(receiver_id)
        if isinstance(checked, Err):
            return checked
        return await self._store.commit_pairing(provisional_ref, trust_generation)

    async def cancel_pairing(self, challenge):
        checked = self._check(challenge.receiver_id, cleanup=True)
        if isinstance(checked, Err):
            return checked
        return await self._store.cancel_pairing(challenge)

    async def revoke_pairing(self, ref_or_receiver):
        receiver_id = self._transport._provisional_receivers.get(ref_or_receiver, ref_or_receiver)
        checked = self._check(receiver_id, cleanup=True)
        if isinstance(checked, Err):
            return checked
        return await self._store.revoke_pairing(ref_or_receiver)

    async def mark_invalid(self, receiver_id):
        # Local invalidation must still run when the endpoint or daemon is gone.
        return await self._store.mark_invalid(receiver_id)


class DoubleTakeTransport:
    """Implement TransportPort and PairingPort on the same authority object.

    Daemon creation/authentication is shared, while receiver operations remain
    independent. A stream lock serializes controls and cleanup for that stream.
    Deadlines bound waiters, retaining in-flight acquisition ownership so late
    success is cleaned up or exposed with its exact handle as uncertain.

    Replacement-safe dispatch guarantee (what this class does and does not do):

    1. For connect, disconnect, mute, unmute, start_audio and restart_audio this
       class binds its own exact-target predicate (or the retained
       address-ownership predicate on the cleanup path) and supplies it to the
       control. A control whose ``request`` cannot accept a fence is rejected
       before invocation, so it receives no command at all.
    2. ``PublishedDoubletakeControl.request`` refuses those six operations when no
       fence is supplied, before performing any I/O at all, and otherwise evaluates
       the supplied fence after its own daemon-ownership await and immediately
       before spawning the mutating child. A typed ``Err`` prevents the mutation.
       When the fence *rejects*, the ownership-status child may already have run:
       the guarantee is *no target mutation*, not *no subprocess*.
    3. With trusted controls and wrappers that forward the original predicate and
       the unchanged target, target invalidation during the ownership await is
       detected before mutation. A wrapper that merely drops the keyword is caught
       by (2).
    4. **Trust boundary.** The adapter does not authenticate the provenance of the
       predicate, and no signature establishes that another component's
       implementation honours it. A same-process wrapper that substitutes or
       caches a permissive predicate, alters the target, ignores this contract,
       calls the adapter's ``_run`` directly, or launches a child itself is outside
       the guarantee. Post-reply freshness checks still reject stale *results*;
       they cannot undo effects such a component has already dispatched. Injected
       controls and runners must honour the ordering contract above.
    """

    def __init__(
        self, control_factory: Callable[[DaemonRecord], BackendControl],
        registry: DaemonRegistry, capture: CapturePort, credentials: CredentialStore,
        receivers: ReceiverStore, fences: TrustFenceRegistry, features: BackendFeatures,
        clock: Clock, scheduler: Scheduler, ids: IdFactory,
        *, source_verifier: ReportedSourceVerifier | None = None,
    ) -> None:
        self._control_factory = control_factory
        self._registry, self._capture = registry, capture
        self._fences, self._features = fences, features
        self._clock, self._scheduler, self._ids = clock, scheduler, ids
        self._source_verifier = source_verifier
        self._targets = TargetRegistry()
        self._daemons: dict[InstanceId, _Daemon] = {}
        self._unusable: set[InstanceId] = set()
        self._streams: dict[LeaseId, _Stream] = {}
        self._retired: dict[StreamHandle, _Stream] = {}
        self._released: set[StreamHandle] = set()
        self._pairings: dict[ReceiverId, _PairingBinding] = {}
        self._beginnings: set[ReceiverId] = set()
        self._provisional_receivers: dict[object, ReceiverId] = {}
        self._tasks: set[asyncio.Task] = set()
        self._callbacks: list[Callable[[TransportEvent], None]] = []
        self._closing = False
        self._closed = False
        self._shutdown_task: asyncio.Task | None = None
        self._pairing = PairingCoordinator(
            _PairingControl(self), _PairingCredentials(self, credentials), receivers,
            fences, clock, scheduler, ids,
        )
        self._daemon_subscription = registry.subscribe(self._on_daemon)

    def update_target(self, target: ReceiverTarget | None, receiver_id: ReceiverId) -> None:
        self._targets.update(target, receiver_id)
        fence = self._fences.get(receiver_id)
        if fence is not None:
            self._fences.set(replace(
                fence, endpoint_fingerprint=target.endpoint_fingerprint if target else None,
                endpoint_generation=target.endpoint_generation if target else fence.endpoint_generation,
            ))
        # A new receiver at an old address also invalidates the old trust fence.
        for binding in tuple(self._pairings.values()):
            if isinstance(self._targets.require_current(binding.target), Err):
                old = self._fences.get(binding.target.receiver_id)
                if old is not None and (
                    old.endpoint_fingerprint == binding.target.endpoint_fingerprint
                    and old.endpoint_generation == binding.target.endpoint_generation
                ):
                    self._fences.set(replace(old, endpoint_fingerprint=None))

    def subscribe(self, callback: Callable[[TransportEvent], None]) -> Subscription:
        self._callbacks.append(callback)

        def remove() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _Subscription(action=remove)

    def _emit(self, stream: _Stream, kind: TransportEventKind, payload=None, context=None) -> None:
        if stream.handle is None:
            return
        ctx = replace(
            context or stream.context, endpoint_generation=stream.target.endpoint_generation,
            transport_lease_id=stream.lease,
        )
        event = TransportEvent(
            kind=kind, receiver_id=stream.target.receiver_id, stream=stream.handle,
            endpoint_fingerprint=stream.target.endpoint_fingerprint,
            endpoint_generation=stream.target.endpoint_generation,
            daemon_instance_uuid=stream.handle.daemon_instance_uuid,
            context=ctx, payload=payload if payload is not None else EventEmpty(),
        )
        for callback in tuple(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue

    def _failed(self, stream: _Stream, failure: Err) -> Err:
        self._emit(stream, TransportEventKind.failed, FailurePayload(diagnostic=failure.error))
        return failure

    def _on_daemon(self, notice: DaemonNotice) -> None:
        if notice.kind == "ready" or notice.instance_uuid in self._unusable:
            return
        self._unusable.add(notice.instance_uuid)
        daemon = self._daemons.get(notice.instance_uuid)
        if daemon is not None:
            daemon.alive = False
        for lease, stream in tuple(self._streams.items()):
            if stream.daemon is not daemon or daemon is None:
                continue
            self._retire_volume(stream)
            del self._streams[lease]
            if stream.handle is not None:
                self._retired[stream.handle] = stream
            code = ErrorCode.daemon_failed if notice.kind == "exit" else ErrorCode.cleanup_timeout
            self._emit(stream, TransportEventKind.daemon_exit, FailurePayload(diagnostic=diagnostic(code)))
        for binding in self._pairings.values():
            if binding.daemon is daemon and daemon is not None:
                fence = self._fences.get(binding.target.receiver_id)
                if fence is not None:
                    self._fences.set(replace(fence, trust_generation=fence.trust_generation + 1))

    def _spawn(self, operation: Awaitable) -> asyncio.Task:
        task = asyncio.create_task(operation)
        self._tasks.add(task)

        def finished(completed):
            self._tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        return task

    async def _wait(
        self, task: asyncio.Task, code: ErrorCode, abandon=None, *, timeout=CONTROL_TIMEOUT,
        timeout_until: asyncio.Future | None = None,
    ):
        loop = asyncio.get_running_loop()
        deadline = loop.create_future()

        def expired():
            if task.done() or deadline.done() or (timeout_until is not None and timeout_until.done()):
                return
            if abandon is not None:
                abandon()
            deadline.set_result(None)

        scheduled = self._scheduler.call_at(self._clock.now() + timeout, expired)
        realtime = loop.call_later(timeout, expired)
        try:
            waiting = (task, deadline) if timeout_until is None else (task, deadline, timeout_until)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return task.result()
            if deadline in done:
                return _failure(code)
            # Connect has answered. Its admission timer must not preempt the
            # bounded source wait or the exact-target cleanup that now owns time.
            scheduled.cancel()
            realtime.cancel()
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if abandon is not None:
                abandon()
            raise
        finally:
            scheduled.cancel()
            realtime.cancel()
            deadline.cancel()
            if timeout_until is not None and not timeout_until.done():
                timeout_until.cancel()

    def _current(self, target: ReceiverTarget, context: OperationContext) -> Result[None]:
        checked = self._targets.require_current(target)
        if isinstance(checked, Err):
            return checked
        if context.endpoint_generation != target.endpoint_generation:
            return _failure(ErrorCode.stale_target)
        if self._closing:
            return _failure(ErrorCode.transport_failed)
        return Ok(value=None)

    async def _admit(self, target: ReceiverTarget, context: OperationContext) -> Result[_Daemon]:
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if self._features.profile in {"doubletake-0.5", "doubletake-0.5+source"}:
            admitted = self._features.owned_daemon_control and self._features.keyring
        else:
            admitted = (
                self._features.foreground_control
                and self._features.owner_handshake
                and self._features.keyring
            )
        if not admitted:
            return _failure(ErrorCode.dependency_incompatible)
        ensured = await _safe(self._registry.ensure(), ErrorCode.daemon_failed)
        if isinstance(ensured, Err):
            return ensured
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        record = ensured.value
        if not isinstance(record, DaemonRecord) or record.instance_uuid in self._unusable:
            return _failure(ErrorCode.daemon_failed)
        daemon = self._daemons.get(record.instance_uuid)
        if daemon is None:
            try:
                control = self._control_factory(record)
            except Exception:
                return _failure(ErrorCode.daemon_failed)
            daemon = _Daemon(record=record, control=control)
            self._daemons[record.instance_uuid] = daemon
            # BackendNotice has neither request UUID nor exact stream handle.
            # Do not attribute its target-only notifications to current sessions.
        elif daemon.record != record:
            return _failure(ErrorCode.ownership_mismatch)
        async with daemon.lock:
            checked = self._current(target, context)
            if isinstance(checked, Err):
                return checked
            if not daemon.alive:
                return _failure(ErrorCode.daemon_failed)
            if not daemon.authenticated:
                proof = await _safe(
                    daemon.control.handshake(record.owner_token, record.instance_uuid),
                    ErrorCode.ownership_mismatch,
                )
                if isinstance(proof, Err):
                    return proof
                value = proof.value
                if isinstance(value, OwnershipProof):
                    if value.kind != "evidence" or value.daemon != record.daemon:
                        return _failure(ErrorCode.ownership_mismatch)
                    daemon.ownership_proof = value
                else:
                    h = value
                    owner = h.owner_token.reveal_for_backend() if isinstance(h, Handshake) and isinstance(h.owner_token, SensitiveText) else getattr(h, "owner_token", None)
                    if not isinstance(h, Handshake) or (
                        h.instance_uuid != record.instance_uuid or owner != record.owner_token
                        or h.daemon != record.daemon or h.protocol_version != record.protocol_version
                    ):
                        return _failure(ErrorCode.ownership_mismatch)
                daemon.authenticated = True
            checked = self._current(target, context)
            if isinstance(checked, Err):
                return checked
            if not daemon.alive:
                return _failure(ErrorCode.daemon_failed)
        return Ok(value=daemon)

    def _dispatch_fence(
        self, target: ReceiverTarget, context: OperationContext, *, cleanup: bool,
    ) -> Callable[[], Result[None]]:
        """Bind the exact target/cleanup ownership the adapter rechecks at dispatch.

        The adapter evaluates this after its own awaited ownership query and
        immediately before the mutating child is spawned.  A replacement admitted
        during that window therefore cannot receive the old stream's command.
        """

        def check() -> Result[None]:
            return self._targets.require_cleanup(target) if cleanup else self._current(target, context)

        return check

    async def _request(
        self, daemon: _Daemon, operation: BackendOperation, target: ReceiverTarget,
        payload: BackendPayload, context: OperationContext, *, cleanup=False,
    ) -> Result[BackendReply]:
        checked = self._targets.require_cleanup(target) if cleanup else self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if not daemon.alive or not daemon.authenticated:
            return _failure(ErrorCode.cleanup_timeout if cleanup else ErrorCode.stale_event)
        fence = self._dispatch_fence(target, context, cleanup=cleanup)
        if operation in (BackendOperation.volume, BackendOperation.audio_off):
            original = self._streams.get(context.transport_lease_id)
            def fence():
                if original is None or original.handle is None:
                    return _failure(ErrorCode.stale_event)
                current = self._audio_owned(original.handle, context)
                if isinstance(current, Err):
                    return current
                if operation is BackendOperation.volume and (original.audio_closed or not original.audio_policy.enabled):
                    return _failure(ErrorCode.audio_volume_failed)
                return Ok(value=None)
            checked = fence()
            if isinstance(checked, Err):
                return checked
        fenced = _accepts_dispatch_fence(daemon.control)
        if requires_dispatch_fence(operation) and not fenced:
            # Fail closed. A control that cannot accept the dispatch fence would
            # spawn this state change unchecked, so it must not be called at all:
            # a replacement admitted during the adapter's awaited ownership
            # revalidation would otherwise receive the command.
            return _failure(ErrorCode.dependency_incompatible)
        request_kwargs: dict[str, object] = {}
        if fenced:
            request_kwargs["dispatch_fence"] = fence
        if _accepts_ownership_proof(daemon.control):
            request_kwargs["ownership_proof"] = daemon.ownership_proof
        result = await _safe(daemon.control.request(
            operation, target, payload, context, **request_kwargs,
        ))
        if not daemon.alive or not daemon.authenticated:
            return _failure(ErrorCode.cleanup_timeout if cleanup else ErrorCode.stale_event)
        checked = self._targets.require_cleanup(target) if cleanup else self._current(target, context)
        return checked if isinstance(checked, Err) else result

    async def query_modes(self, target: ReceiverTarget, context: OperationContext) -> Result[ReceiverCapabilities | None]:
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if not self._features.query_modes:
            return Ok(value=None)
        return await self._wait(self._spawn(self._query_modes(target, context)), ErrorCode.transport_failed)

    async def _query_modes(self, target, context):
        admitted = await self._admit(target, context)
        if isinstance(admitted, Err):
            return admitted
        result = await self._request(admitted.value, BackendOperation.query_modes, target, ModesPayload(), context)
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if isinstance(result, Err) and result.error.code is not ErrorCode.unsupported_operation:
            return result
        # The integrated reply union contains no resolution evidence. An ack or
        # status cannot be converted into a size, reliable mode, or provenance.
        return Ok(value=None)

    def _trust_blocked(self, target: ReceiverTarget) -> bool:
        if self._pairing.pending_cleanup(target.receiver_id):
            return True
        return any(
            binding.target.canonical_host == target.canonical_host
            and self._pairing.pending_cleanup(receiver_id)
            for receiver_id, binding in self._pairings.items()
        )

    async def start_stream(
        self, target: ReceiverTarget, source: CaptureSource | None,
        audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel | None = None,
    ) -> Result[StreamHandle]:
        if initial_volume is not None and type(initial_volume) is not VolumeLevel:
            return _failure(ErrorCode.invalid_input)
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if self._trust_blocked(target):
            return _failure(ErrorCode.trust_cleanup_required)
        if source is None or isinstance(source, PhysicalMonitorSource):
            # ``None`` is desktop capture: the backend opens the session and the
            # portal decides which screen it gets.
            supported = self._features.physical_source
        elif isinstance(source, VirtualOutputSource):
            supported = self._features.virtual_source and bool(source.connector and source.kscreen_output_id)
        else:
            supported = False
        if not supported:
            return _failure(ErrorCode.unsupported_source)
        if not self._features.multi_target:
            return _failure(ErrorCode.unsupported_operation)
        if any(
            stream.target.receiver_id == target.receiver_id
            or stream.target.canonical_host == target.canonical_host
            for stream in self._streams.values()
        ):
            return _failure(ErrorCode.cleanup_timeout)
        lease = context.transport_lease_id or LeaseId(self._ids.new())
        if lease in self._streams:
            return _failure(ErrorCode.stale_event)
        stream = _Stream(
            target=target, context=replace(context, transport_lease_id=lease), source=source,
            audio_policy=audio_policy, lease=lease, initial_volume=initial_volume,
            audio_closed=self._features.target_volume and not audio_policy.enabled,
            audio=AudioState.Starting if audio_policy.enabled else AudioState.Disabled,
        )
        self._streams[lease] = stream
        connected = asyncio.get_running_loop().create_future()
        acquisition = self._spawn(self._start(stream, connected))

        def abandon() -> None:
            stream.abandoned = True
            # Cancellation can arrive after _start returned but before its
            # waiter received the handle. The completed task can no longer
            # observe abandonment, so retain a separate cleanup observer.
            if acquisition.done() and stream.handle is not None and not stream.released:
                self._spawn(self._abandon_completed(stream))

        return await self._wait(
            acquisition, ErrorCode.transport_failed, abandon, timeout_until=connected,
        )

    async def _abandon_completed(self, stream: _Stream) -> Result[None]:
        cleaned = await self._stop_owned(stream, stream.context)
        return self._failed(stream, cleaned) if isinstance(cleaned, Err) else cleaned

    def _drop(self, stream: _Stream) -> None:
        self._retire_volume(stream)
        if self._streams.get(stream.lease) is stream:
            del self._streams[stream.lease]
        if stream.handle is not None:
            self._retired.pop(stream.handle, None)
        stream.released = True

    async def _verify_reported_source(self, stream: _Stream) -> Result[None]:
        verifier = self._source_verifier
        if verifier is None:
            return Ok(value=None)
        if self._features.source_reporting_complete is not True:
            return _failure(ErrorCode.unsupported_source)
        daemon = stream.daemon
        if daemon is None or not daemon.alive or not daemon.authenticated:
            return _failure(ErrorCode.stale_event)
        deadline = self._clock.now() + SOURCE_TIMEOUT
        polling = self._spawn(self._poll_reported_source(stream, deadline))
        try:
            return await self._wait(polling, ErrorCode.unsupported_source, timeout=SOURCE_TIMEOUT)
        finally:
            # Status and inventory reads confer no acquisition authority. Retire
            # the poll on expiry so a late reply cannot publish or start a poll.
            if not polling.done():
                polling.cancel()

    async def _await_connecting_stream(self, stream: _Stream) -> Result[None]:
        """Wait for an accepted ``connect`` to reach streaming evidence.

        A ``connecting`` acknowledgement is progress: the backend is still
        preparing capture, which for a desktop capture means waiting for the
        portal's screen prompt. That phase is a human answer, so it is generously
        bounded and stays cancellable; once the stream carries a source only the
        RTSP handshake is left and the connect budget applies. Readiness is never
        inferred from the acknowledgement, credential prompts end the wait as
        their own refusal, and target or daemon loss outranks any later
        observation.
        """
        accepted = self._clock.now() + CONNECT_STREAMING_TIMEOUT
        permission = self._clock.now() + PERMISSION_WAIT_TIMEOUT
        handshake: float | None = None
        seen = False
        failures = 0
        loop = asyncio.get_running_loop()
        while True:
            daemon = stream.daemon
            if stream.abandoned or daemon is None or not daemon.alive or not daemon.authenticated:
                return _failure(ErrorCode.stale_event)
            checked = self._current(stream.target, stream.context)
            if isinstance(checked, Err):
                return checked
            status = await _safe(
                daemon.control.request(BackendOperation.status, None, EmptyPayload(), None),
                ErrorCode.unsupported_source,
            )
            if isinstance(status, Err):
                # A status read can time out while the backend is blocked on the
                # portal prompt. One unanswered read is not a failed capture, but
                # a control that cannot answer three times in a row is.
                failures += 1
                if failures >= STATUS_FAILURE_BUDGET:
                    return status
                matches: tuple = ()
                examined = False
            else:
                failures = 0
                matches = ()
                examined = isinstance(status.value, StatusReply)
            if examined:
                matches = tuple(
                    item for item in status.value.streams
                    if item.target == stream.target.canonical_host
                )
                seen = seen or bool(matches)
                prompting = (
                    status.value.state == "pin_required"
                    or status.value.needs_credential
                    or status.value.credential_kind is not None
                    or any(
                        item.state == "pin_required" or item.credential_kind is not None
                        for item in matches
                    )
                )
                if prompting:
                    return _failure(ErrorCode.dependency_incompatible)
                if len(matches) == 1 and matches[0].state == "streaming":
                    return Ok(value=None)
            # The backend publishes a connecting stream before it has a capture
            # source: that is the desktop's permission prompt, and a human may
            # take minutes to answer it. Once the stream carries a source, only
            # the handshake is left, which is bounded.
            prepared = examined and len(matches) == 1 and matches[0].source is not None
            if prepared:
                if handshake is None:
                    handshake = self._clock.now() + CONNECT_STREAMING_TIMEOUT
                if self._clock.now() >= handshake:
                    return _failure(ErrorCode.unsupported_source)
            else:
                handshake = None
                if examined and seen and not matches:
                    # The stream this target had is gone: the backend dropped it.
                    return _failure(ErrorCode.unsupported_source)
                if not seen and self._clock.now() >= accepted:
                    # No stream for this target ever appeared: the backend never
                    # accepted the capture, so no prompt is pending.
                    return _failure(ErrorCode.unsupported_source)
            if self._clock.now() >= permission:
                return _failure(ErrorCode.unsupported_source)
            wake = loop.create_future()

            def tick():
                if not wake.done():
                    wake.set_result(None)

            scheduled = self._scheduler.call_at(
                self._clock.now() + SOURCE_POLL_INTERVAL, tick,
            )
            try:
                await wake
            finally:
                scheduled.cancel()

    async def _claim_streaming_readiness(self, stream: _Stream) -> Err | None:
        """Publish the ready event from local evidence, or return the refusal."""
        source_checked = (
            Ok(value=None)
            if isinstance(stream.source, VirtualOutputSource)
            else await self._verify_reported_source(stream)
        )
        checked = self._current(stream.target, stream.context)
        if isinstance(source_checked, Err):
            return source_checked
        if isinstance(checked, Err):
            return checked
        if stream.abandoned or not stream.daemon.alive:
            return _failure(ErrorCode.stale_event)
        stream.ready = True
        observation = stream.source_observation
        payload = EventEmpty()
        if (
            observation is not None
            and observation.matched_connector is not None
            and observation.reported is not None
        ):
            payload = SourceVerificationPayload(
                connector=observation.matched_connector,
                source_type=observation.reported.type,
                width=observation.reported.width,
                height=observation.reported.height,
                x=observation.reported.x,
                y=observation.reported.y,
            )
        self._emit(stream, TransportEventKind.ready, payload)
        return None

    async def _poll_reported_source(self, stream: _Stream, deadline: float) -> Result[None]:
        loop = asyncio.get_running_loop()
        last_missing_source: SourceVerificationRefusal | None = None
        while self._clock.now() < deadline:
            daemon = stream.daemon
            if stream.abandoned or daemon is None or not daemon.alive or not daemon.authenticated:
                return _failure(ErrorCode.stale_event)
            checked = self._current(stream.target, stream.context)
            if isinstance(checked, Err):
                return checked
            status = await _safe(
                daemon.control.request(BackendOperation.status, None, EmptyPayload(), None),
                ErrorCode.unsupported_source,
            )
            if self._clock.now() >= deadline:
                break
            if isinstance(status, Ok) and isinstance(status.value, StatusReply):
                matches = tuple(
                    item for item in status.value.streams
                    if item.target == stream.target.canonical_host
                )
                if (
                    status.value.target == stream.target.canonical_host
                    and (
                        status.value.state == "pin_required"
                        or status.value.needs_credential
                        or status.value.credential_kind is not None
                    )
                ) or any(
                    item.state == "pin_required" or item.credential_kind is not None
                    for item in matches
                ):
                    return _failure(ErrorCode.dependency_incompatible)
                if len(matches) == 1:
                    requested = (
                        stream.source.connector
                        if isinstance(stream.source, PhysicalMonitorSource)
                        else None
                    )
                    verified = await _safe_source_verification(
                        self._source_verifier.verify(requested, matches[0].source),
                    )
                    if self._clock.now() >= deadline:
                        return _failure(ErrorCode.unsupported_source)
                    if isinstance(verified, SourceVerificationRefusal):
                        if verified.observation.reason is SourceRefusal.MISSING_SOURCE:
                            last_missing_source = verified
                        else:
                            return verified
                    elif isinstance(verified, Err):
                        return verified
                    elif isinstance(verified, Ok):
                        observation = verified.value
                        if (
                            isinstance(observation, SourceObservation)
                            and observation.accepted
                            and observation.matched_connector is not None
                            and observation.reported is not None
                        ):
                            stream.source_observation = observation
                            stream.handle = replace(
                                stream.handle, source_connector=observation.matched_connector,
                            )
                        return verified
            # Every read shares the same absolute deadline; incomplete or foreign
            # observations cannot extend it or certify the requested connector.
            wake = loop.create_future()

            def tick():
                if not wake.done():
                    wake.set_result(None)

            scheduled = self._scheduler.call_at(
                min(self._clock.now() + SOURCE_POLL_INTERVAL, deadline), tick,
            )
            try:
                await wake
            finally:
                scheduled.cancel()
        return last_missing_source or _failure(ErrorCode.unsupported_source)

    async def _start(self, stream: _Stream, connected: asyncio.Future) -> Result[StreamHandle]:
        if isinstance(stream.source, PhysicalMonitorSource):
            verified = await _safe(self._capture.verify(stream.source), ErrorCode.unsupported_source)
            if isinstance(verified, Err):
                self._drop(stream)
                return verified
        admitted = await self._admit(stream.target, stream.context)
        if isinstance(admitted, Err):
            self._drop(stream)
            return admitted
        stream.daemon = admitted.value
        async with stream.lock:
            stream.handle = StreamHandle(
                receiver_id=stream.target.receiver_id, endpoint_generation=stream.target.endpoint_generation,
                endpoint_fingerprint=stream.target.endpoint_fingerprint,
                backend_stream_id=f"request-{stream.context.operation_id}-{stream.lease}",
                daemon_instance_uuid=stream.daemon.record.instance_uuid, lease_id=stream.lease,
                start_operation_id=stream.context.operation_id,
            )
            if stream.abandoned or self._trust_blocked(stream.target):
                self._drop(stream)
                return _failure(ErrorCode.stale_event)
            # Stop must join this acquisition barrier before its ack can prove
            # release: disconnecting ahead of a late connect is not cleanup.
            result = await self._request(
                stream.daemon, BackendOperation.connect, stream.target,
                ConnectPayload(audio=stream.audio_policy,
                               initial_volume=stream.initial_volume if stream.audio_policy.enabled else None)
                if self._features.target_volume else ConnectPayload(), stream.context,
            )
            if not connected.done():
                connected.set_result(None)
            if isinstance(result, Err) and result.error.code in (
                ErrorCode.receiver_rejected, ErrorCode.unsupported_source,
                ErrorCode.unsupported_operation,
            ):
                self._drop(stream)
                return self._failed(stream, result)
            checked = self._current(stream.target, stream.context)
            if _ack(result, BackendOperation.connect, stream.target) and isinstance(checked, Ok) and not stream.abandoned and stream.daemon.alive:
                if isinstance(stream.source, VirtualOutputSource) and result.value.state == "connecting":
                    # Preserve the virtual acquisition contract: the caller owns
                    # this handle while waiting for a separate readiness event.
                    return Ok(value=stream.handle)
                if self._features.owned_daemon_control and result.value.state == "pin_required":
                    failure = _failure(ErrorCode.dependency_incompatible)
                elif result.value.state == "connecting" and not isinstance(
                    stream.source, VirtualOutputSource
                ):
                    # The backend accepts ``connect`` before its handshake and
                    # capture preparation finish, so this ack is progress. Any
                    # readiness claim still needs the daemon's own status to
                    # report exactly this target streaming, within one budget.
                    waited = await self._await_connecting_stream(stream)
                    claimed = (
                        waited if isinstance(waited, Err)
                        else await self._claim_streaming_readiness(stream)
                    )
                    if not isinstance(claimed, Err):
                        return Ok(value=stream.handle)
                    failure = claimed
                elif result.value.state != "streaming":
                    failure = _failure(ErrorCode.unsupported_source)
                else:
                    claimed = await self._claim_streaming_readiness(stream)
                    if not isinstance(claimed, Err):
                        return Ok(value=stream.handle)
                    failure = claimed
            else:
                failure = result if isinstance(result, Err) else (
                    checked if isinstance(checked, Err) else _failure(
                        ErrorCode.stale_event if stream.abandoned else ErrorCode.transport_failed
                    )
                )
        # Source provenance is local verification plus the affirmed feature;
        # the exact request ack supplies connect acceptance only.
        cleaned = await self._wait(
            self._spawn(self._stop_owned(stream, stream.context)), ErrorCode.cleanup_timeout,
        )
        return self._failed(stream, cleaned if isinstance(cleaned, Err) else failure)

    def _owned(self, handle: StreamHandle, context: OperationContext | None = None) -> Result[_Stream]:
        stream = self._streams.get(handle.lease_id)
        if stream is None or stream.handle != handle or stream.released:
            return _failure(ErrorCode.stale_event)
        if stream.daemon is None or not stream.daemon.alive:
            return _failure(ErrorCode.stale_event)
        if context is not None:
            checked = self._current(stream.target, context)
            if isinstance(checked, Err):
                return checked
            if context.session_generation != stream.context.session_generation or (
                context.transport_lease_id is not None and context.transport_lease_id != handle.lease_id
            ):
                return _failure(ErrorCode.stale_event)
        return Ok(value=stream)

    async def stop_stream(self, handle: StreamHandle, context: OperationContext) -> Result[None]:
        if handle in self._released:
            return Ok(value=None)
        if handle in self._retired:
            return _failure(ErrorCode.cleanup_timeout)
        owned = self._owned(handle)
        if isinstance(owned, Err):
            return owned
        self._retire_volume(owned.value)
        return await self._wait(self._spawn(self._stop_owned(owned.value, context)), ErrorCode.cleanup_timeout)

    async def _stop_owned(self, stream: _Stream, context: OperationContext) -> Result[None]:
        async with stream.lock:
            if stream.released:
                return Ok(value=None)
            if stream.daemon is None or not stream.daemon.alive:
                return _failure(ErrorCode.cleanup_timeout)
            result = await self._request(
                stream.daemon, BackendOperation.disconnect, stream.target, EmptyPayload(), context,
                cleanup=True,
            )
            checked = self._targets.require_cleanup(stream.target)
            if isinstance(checked, Err):
                return checked
            if not _ack(result, BackendOperation.disconnect, stream.target) or not stream.daemon.alive or (
                result.value.state in ("streaming", "connecting", "pin_required")
            ):
                return _failure(ErrorCode.cleanup_timeout)
            self._drop(stream)
            self._released.add(stream.handle)
            self._emit(stream, TransportEventKind.stopped, context=context)
            return Ok(value=None)

    async def start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]:
        owned = self._owned(handle, context)
        if isinstance(owned, Err):
            return owned
        if not owned.value.audio_policy.enabled or owned.value.audio_closed:
            return _failure(ErrorCode.unsupported_operation)
        return await self._wait(self._spawn(self._start_audio(owned.value, context)), ErrorCode.audio_unavailable)

    async def _start_audio(self, stream: _Stream, context: OperationContext) -> Result[AudioStartReceipt]:
        async with stream.lock:
            checked = self._owned(stream.handle, context)
            if isinstance(checked, Err):
                return checked
            if not stream.audio_policy.enabled or stream.audio_closed:
                return _failure(ErrorCode.audio_unavailable)
            if self._features.audio_restart and not self._features.target_volume:
                result = await self._request(
                    stream.daemon, BackendOperation.start_audio, stream.target, EmptyPayload(), context,
                )
                checked = self._owned(stream.handle, context)
                if isinstance(checked, Err):
                    return checked
                result = Ok(value=AudioStartReceipt(audio=True)) if _ack(
                    result, BackendOperation.start_audio, stream.target,
                ) else result if isinstance(result, Err) else _failure(ErrorCode.audio_unavailable)
            else:
                result = await self._reported_stream_audio(stream, context)
            if isinstance(result, Err):
                stream.audio = AudioState.Unavailable
                stream.muted = False
                self._emit(stream, TransportEventKind.audio_failed if stream.ready else TransportEventKind.audio_start_failed,
                           FailurePayload(diagnostic=result.error), context)
                return result
            receipt = result.value
            stream.has_audio = receipt.audio
            if self._features.target_volume:
                stream.volume_state = receipt.volume_state
                if receipt.volume_state == "applied" and receipt.audio:
                    stream.muted = receipt.level == 0
                    stream.audio = AudioState.Muted if stream.muted else AudioState.Enabled
                elif receipt.volume_state == "unrequested" and receipt.audio:
                    stream.audio = AudioState.Muted if stream.muted else AudioState.Enabled
                else:
                    stream.muted = False
                    stream.audio = AudioState.Unavailable
            else:
                stream.audio = AudioState.Muted if stream.muted else AudioState.Enabled
                self._emit(stream, TransportEventKind.audio_recovered, context=context)
            return result

    async def _reported_stream_audio(self, stream: _Stream, context: OperationContext) -> Result[AudioStartReceipt]:
        """Return the exact target's receipt from the existing single status read."""
        daemon = stream.daemon
        if daemon is None or not daemon.alive or not daemon.authenticated:
            return _failure(ErrorCode.stale_event)
        reported = await _safe(
            daemon.control.request(BackendOperation.status, None, EmptyPayload(), None),
            ErrorCode.audio_unavailable,
        )
        checked = self._owned(stream.handle, context)
        if isinstance(checked, Err):
            return checked
        if not stream.audio_policy.enabled or stream.audio_closed:
            return _failure(ErrorCode.audio_unavailable)
        if isinstance(reported, Err):
            return reported
        status = reported.value
        if not isinstance(status, StatusReply):
            return _failure(ErrorCode.audio_unavailable)
        matching = tuple(item for item in status.streams if item.target == stream.target.canonical_host)
        if len(matching) != 1 or matching[0].state != "streaming" or matching[0].audio_closed is True:
            return _failure(ErrorCode.audio_unavailable)
        entry = matching[0]
        if not self._features.target_volume and not entry.has_audio:
            return _failure(ErrorCode.audio_unavailable)
        return Ok(value=AudioStartReceipt(audio=entry.has_audio, volume_state=entry.volume_state, level=entry.volume))

    @staticmethod
    def _retire_volume(stream: _Stream) -> None:
        if stream.volume_task is not None:
            stream.volume_task.expired = True

    def _audio_owned(self, handle: StreamHandle, context: OperationContext) -> Result[_Stream]:
        owned = self._owned(handle, context)
        if isinstance(owned, Err):
            return owned
        stream = owned.value
        fence = self._fences.get(handle.receiver_id)
        if self._trust_blocked(stream.target):
            return _failure(ErrorCode.trust_cleanup_required)
        if fence is None or (
            fence.endpoint_fingerprint != handle.endpoint_fingerprint
            or fence.endpoint_generation != handle.endpoint_generation
            or fence.session_generation != context.session_generation
            or fence.trust_generation != (context.trust_generation or 0)
            or fence.active_challenge_id is not None
        ):
            return _failure(ErrorCode.stale_event)
        return owned

    async def set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]:
        if type(handle) is not StreamHandle or type(level) is not VolumeLevel or type(context) is not OperationContext:
            return _failure(ErrorCode.invalid_input)
        record = _VolumeTask(handle=handle, context=context, level=level)
        record.task = self._spawn(self._set_volume(record))
        record.task.add_done_callback(lambda task: self._volume_settled(record))
        return await self._wait(record.task, ErrorCode.audio_volume_failed,
                                lambda: setattr(record, "expired", True))

    def _volume_settled(self, record: _VolumeTask) -> None:
        task = record.task
        result = None if task.cancelled() else task.result()
        current = self._audio_owned(record.handle, record.context)
        ok = (not record.expired and isinstance(result, Ok) and isinstance(current, Ok)
              and not current.value.audio_closed and current.value.audio_policy.enabled)
        if isinstance(current, Ok) and current.value.volume_task is record:
            current.value.volume_task = None
        event = TransportEvent(
            kind=TransportEventKind.volume_result, receiver_id=record.handle.receiver_id,
            stream=record.handle, endpoint_fingerprint=record.handle.endpoint_fingerprint,
            endpoint_generation=record.handle.endpoint_generation,
            daemon_instance_uuid=record.handle.daemon_instance_uuid, context=record.context,
            payload=EventVolume(level=result.value if ok else record.level.value, ok=ok),
        )
        for callback in tuple(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue

    async def _set_volume(self, record: _VolumeTask) -> Result[int]:
        if not self._features.target_volume:
            return _failure(ErrorCode.unsupported_operation)
        owned = self._audio_owned(record.handle, record.context)
        if isinstance(owned, Err):
            return owned
        stream = owned.value
        if stream.volume_task is not None and not stream.volume_task.task.done():
            return _failure(ErrorCode.audio_volume_failed)
        stream.volume_task = record
        async with stream.lock:
            checked = self._audio_owned(record.handle, record.context)
            if isinstance(checked, Err):
                return checked
            retry = (stream.volume_state == "refused" and stream.has_audio
                     and stream.initial_volume == record.level)
            if (record.expired or stream.audio_closed or not stream.audio_policy.enabled
                    or (stream.audio not in (AudioState.Enabled, AudioState.Muted) and not retry)):
                return _failure(ErrorCode.audio_volume_failed)
            result = await self._request(stream.daemon, BackendOperation.volume, stream.target,
                                         VolumePayload(level=record.level), record.context)
            checked = self._audio_owned(record.handle, record.context)
            if isinstance(checked, Err):
                return checked
            if record.expired or stream.audio_closed or not stream.audio_policy.enabled:
                return _failure(ErrorCode.audio_volume_failed)
            if isinstance(result, Err):
                return result
            if not isinstance(result.value, StatusReply):
                return _failure(ErrorCode.audio_volume_failed)
            entries = [s for s in result.value.streams if s.target == stream.target.canonical_host]
            if len(entries) != 1:
                return _failure(ErrorCode.audio_volume_failed)
            receipt = entries[0]
            if not (receipt.state == "streaming" and receipt.has_audio and receipt.audio_closed is False
                    and receipt.volume_state == "applied" and receipt.volume == record.level.value):
                return _failure(ErrorCode.audio_volume_failed)
            stream.muted = receipt.volume == 0
            stream.audio = AudioState.Muted if stream.muted else AudioState.Enabled
            stream.volume_state = "applied"
            return Ok(value=receipt.volume)

    async def close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]:
        if type(handle) is not StreamHandle or type(context) is not OperationContext:
            return _failure(ErrorCode.invalid_input)
        if not self._features.target_volume:
            return _failure(ErrorCode.unsupported_operation)
        owned = self._audio_owned(handle, context)
        if isinstance(owned, Err):
            return owned
        stream = owned.value
        stream.audio_closed = True
        stream.audio_policy = replace(stream.audio_policy, enabled=False)
        self._retire_volume(stream)
        record = stream.close_task
        if record is None or record.task.done():
            record = _CloseTask(task=self._spawn(self._close_audio(stream, context)))
            stream.close_task = record
        result = await self._wait(record.task, ErrorCode.audio_volume_failed,
                                  lambda: setattr(record, "expired", True))
        return _failure(ErrorCode.audio_volume_failed) if record.expired else result

    async def _close_audio(self, stream: _Stream, context: OperationContext) -> Result[None]:
        # Closing audio must never wait behind the stream's volume lock.
        checked = self._audio_owned(stream.handle, context)
        if isinstance(checked, Err):
            return checked
        result = await self._request(stream.daemon, BackendOperation.audio_off, stream.target,
                                     AudioGatePayload(), context)
        checked = self._audio_owned(stream.handle, context)
        if isinstance(checked, Err):
            return checked
        if isinstance(result, Err):
            return result
        entries = ([s for s in result.value.streams if s.target == stream.target.canonical_host]
                   if isinstance(result.value, StatusReply) else [])
        if len(entries) != 1 or entries[0].audio_closed is not True:
            return _failure(ErrorCode.audio_volume_failed)
        return Ok(value=None)

    async def set_muted(self, handle: StreamHandle, muted: bool, context: OperationContext) -> Result[None]:
        owned = self._owned(handle, context)
        if isinstance(owned, Err):
            return owned
        if type(muted) is not bool:
            return _failure(ErrorCode.invalid_input)
        return await self._wait(self._spawn(self._set_muted(owned.value, muted, context)), ErrorCode.audio_mute_failed)

    async def _set_muted(self, stream: _Stream, muted: bool, context: OperationContext) -> Result[None]:
        async with stream.lock:
            checked = self._owned(stream.handle, context)
            if isinstance(checked, Err):
                return checked
            if stream.audio not in (AudioState.Enabled, AudioState.Muted):
                return _failure(ErrorCode.audio_unavailable)
            if not self._features.target_mute:
                return _failure(ErrorCode.unsupported_operation)
            operation = BackendOperation.mute if muted else BackendOperation.unmute
            result = await self._request(stream.daemon, operation, stream.target, MutePayload(muted=muted), context)
            checked = self._owned(stream.handle, context)
            if isinstance(checked, Err):
                return checked
            confirmed = _ack(result, operation, stream.target) and result.value.muted is muted
            if confirmed:
                stream.muted = muted
                stream.audio = AudioState.Muted if muted else AudioState.Enabled
            self._emit(stream, TransportEventKind.mute_result, EventMute(muted=stream.muted, ok=confirmed), context)
            return Ok(value=None) if confirmed else _failure(ErrorCode.audio_mute_failed)

    def _pairing_current(self, binding: _PairingBinding, *, cleanup=False) -> Result[None]:
        checked = self._targets.require_cleanup(binding.target) if cleanup else self._current(binding.target, binding.context)
        if isinstance(checked, Err):
            return checked
        if binding.daemon is None or not binding.daemon.alive:
            return _failure(ErrorCode.keyring_unavailable)
        if not cleanup:
            fence = self._fences.get(binding.target.receiver_id)
            if fence is None or (
                fence.endpoint_fingerprint != binding.target.endpoint_fingerprint
                or fence.endpoint_generation != binding.target.endpoint_generation
                or fence.session_generation != binding.context.session_generation
                or fence.trust_generation != (binding.context.trust_generation or 0)
            ):
                return _failure(ErrorCode.stale_target)
        return Ok(value=None)

    async def begin_pairing(self, target: ReceiverTarget, context: OperationContext) -> Result[PairingChallenge]:
        checked = self._current(target, context)
        if isinstance(checked, Err):
            return checked
        if self._trust_blocked(target):
            return _failure(ErrorCode.trust_cleanup_required)
        if not all((self._features.target_bound_pairing, self._features.keyring,
                    self._features.provisional_commit, self._features.cancel_pairing, self._features.revoke_pairing)):
            return _failure(ErrorCode.keyring_unavailable)
        fence = self._fences.get(target.receiver_id)
        if target.receiver_id in self._beginnings or (fence is not None and fence.active_challenge_id is not None):
            return _failure(ErrorCode.stale_target)
        self._beginnings.add(target.receiver_id)
        self._pairings[target.receiver_id] = _PairingBinding(target=target, context=context)
        try:
            result = await self._pairing.begin_pairing(target, context)
        finally:
            self._beginnings.discard(target.receiver_id)
        if isinstance(result, Err) and result.error.code in (ErrorCode.stale_event, ErrorCode.stale_target):
            checked = self._current(target, context)
            if isinstance(checked, Err):
                return checked
        return result

    async def submit_pairing(self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult:
        return await self._pairing.submit_pairing(challenge_id, value, context)

    async def cancel_pairing(self, receiver_id: ReceiverId, challenge_id: ChallengeId | None, context: OperationContext) -> Result[None]:
        return await self._pairing.cancel_pairing(receiver_id, challenge_id, context)

    async def forget_trust(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        if any(stream.target.receiver_id == receiver_id for stream in (*self._streams.values(), *self._retired.values())):
            return _failure(ErrorCode.invalid_input)
        return await self._pairing.forget_trust(receiver_id, context)

    async def mark_invalid(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        return await self._pairing.mark_invalid(receiver_id, context)

    async def retry_revocation(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        return await self._pairing.retry_revocation(receiver_id, context)

    async def shutdown(self) -> Result[None]:
        if self._closed:
            return Ok(value=None)
        self._closing = True
        for stream in self._streams.values():
            stream.abandoned = True
        if self._shutdown_task is None or self._shutdown_task.done():
            self._shutdown_task = self._spawn(self._shutdown())
        return await self._wait(self._shutdown_task, ErrorCode.cleanup_timeout, timeout=SHUTDOWN_TIMEOUT)

    async def _shutdown(self) -> Result[None]:
        failures = [
            result for result in await asyncio.gather(*(
                self.cancel_pairing(receiver_id, None, binding.context)
                for receiver_id, binding in tuple(self._pairings.items())
            )) if isinstance(result, Err)
        ]
        await asyncio.gather(*(
            self.stop_stream(stream.handle, stream.context)
            for stream in tuple(self._streams.values()) if stream.handle is not None
        ))
        stopped = await self._wait(self._spawn(_safe(self._registry.shutdown(), ErrorCode.cleanup_timeout)), ErrorCode.cleanup_timeout)
        if isinstance(stopped, Err):
            return stopped
        for stream in (*tuple(self._streams.values()), *tuple(self._retired.values())):
            if stream.handle is not None:
                self._released.add(stream.handle)
                self._emit(stream, TransportEventKind.stopped)
            self._drop(stream)
        for daemon in self._daemons.values():
            daemon.alive = False
        failures.extend(
            result for result in await asyncio.gather(*(
                self._wait(self._spawn(_close(daemon.control)), ErrorCode.cleanup_timeout)
                for daemon in tuple(self._daemons.values())
            )) if isinstance(result, Err)
        )
        self._daemon_subscription.close()
        if failures:
            return failures[0]
        self._closed = True
        return Ok(value=None)


async def _close(control: BackendControl) -> Result[None]:
    try:
        await control.close()
    except Exception:
        return _failure(ErrorCode.cleanup_timeout)
    return Ok(value=None)


__all__ = ["DoubleTakeTransport"]
