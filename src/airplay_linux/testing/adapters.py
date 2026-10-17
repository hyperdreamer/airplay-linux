"""Deterministic adapter implementations for every domain port.

These fakes stand in for hardware, the network, the keyring, the desktop, and the
daemon. Each async operation has a FIFO scripted-result queue so a test can hand
out independent successes, failures, or suspensions per receiver, and each event
source exposes ``emit`` so stale and out-of-order events can be replayed. Nothing
here spawns a process, opens a socket, reads a real clock, or stores a secret.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from types import MappingProxyType
from typing import Any, Generic, TypeVar
from uuid import UUID

from airplay_linux.domain import messages, ports, resources
from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverRecordSet,
    ReceiverTarget,
    ReceiverView,
    ServiceRecord,
)
from airplay_linux.domain.operations import (
    ChallengeId,
    DisplayHandle,
    InstanceId,
    LeaseId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OperationContext,
    OperationId,
    OwnerId,
    PairingChallenge,
    PairingResult,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import (
    ActivityEvent,
    DaemonRecord,
    LayoutSnapshot,
    OwnershipRecord,
    RecoveryItem,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    Diagnostic,
    Err,
    ErrorCode,
    Ok,
    PhysicalMonitorSource,
    RecoveryAction,
    ReceiverCapabilities,
    Result,
    Size,
    VirtualOutputSource,
)

from .time import FakeClock

T = TypeVar("T")

_UNSET = object()

PRIVATE_TYPES: tuple[type, ...] = (
    SensitiveText,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OpaqueBackendToken,
)

APP_INSTANCE = InstanceId(UUID(int=1))
DEFAULT_DAEMON_INSTANCE = InstanceId(UUID(int=9))
PAIRING_LIFETIME = 120.0
CONTROL_TIMEOUT = 5.0


def _failure(code: ErrorCode, message: str, detail: str | None = None) -> Err:
    return Err(
        error=Diagnostic(
            code=code, message=message, recovery=(RecoveryAction.retry,), detail=detail,
        )
    )


def _ok() -> Result[None]:
    return Ok(value=None)


# --------------------------------------------------------------------------- journal


@dataclass(frozen=True, kw_only=True)
class PatternScan:
    """One simulated owner-environment scan keyed by an observed owner token."""

    pid: int
    environment: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True, kw_only=True)
class Redacted:
    """Sentinel standing in for one transient private value."""

    kind: str

    def __repr__(self) -> str:
        return f"Redacted({self.kind})"


@dataclass(frozen=True, kw_only=True)
class RedactedRecord:
    """A record whose non-``repr`` fields are dropped rather than merely hidden."""

    kind: str
    fields: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def __repr__(self) -> str:
        rendered = ", ".join(f"{name}={value!r}" for name, value in self.fields.items())
        return f"{self.kind}({rendered})"


def redact(value: Any) -> Any:
    """Project a value onto a secret-free shape that is safe to print or compare."""
    if isinstance(value, PRIVATE_TYPES):
        return Redacted(kind=type(value).__name__)
    if is_dataclass(value) and not isinstance(value, type):
        declared = fields(value)
        if any(not item.repr for item in declared):
            public = {item.name: redact(getattr(value, item.name)) for item in declared if item.repr}
            return RedactedRecord(kind=type(value).__name__, fields=public)
        return {item.name: redact(getattr(value, item.name)) for item in declared}
    if isinstance(value, Mapping):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return type(value)(redact(item) for item in value)
    return value


class Journal:
    """An ordered record of typed public arguments with secrets replaced."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, object]] = []

    def record(self, name: str, **arguments: Any) -> None:
        self.entries.append((name, redact(arguments)))

    def names(self) -> list[str]:
        return [name for name, _ in self.entries]

    def calls(self, name: str) -> list[dict[str, Any]]:
        return [dict(arguments) for entry, arguments in self.entries if entry == name]

    def clear(self) -> None:
        self.entries.clear()

    def __repr__(self) -> str:
        return f"Journal(entries={self.entries!r})"


# --------------------------------------------------------------------------- async seams


class Deferred(Generic[T]):
    """A completion owned by the test; observable even after its waiter cancelled."""

    def __init__(self) -> None:
        self._future: asyncio.Future[Any] | None = None
        self._value: Any = _UNSET
        self._callbacks: list[Callable[[Any], None]] = []

    @property
    def resolved(self) -> bool:
        return self._value is not _UNSET

    @property
    def value(self) -> T:
        if self._value is _UNSET:
            raise RuntimeError("deferred value is not resolved yet")
        return self._value

    def resolve(self, value: T) -> None:
        if self._value is not _UNSET:
            raise RuntimeError("deferred completion is single use")
        self._value = value
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(value)
        if self._future is not None and not self._future.done():
            self._future.set_result(value)

    def add_done_callback(self, callback: Callable[[T], None]) -> None:
        if self._value is not _UNSET:
            callback(self._value)
        else:
            self._callbacks.append(callback)

    async def wait(self) -> T:
        if self._value is not _UNSET:
            return self._value
        if self._future is None:
            self._future = asyncio.get_running_loop().create_future()
        # Shield the completion future: a cancelled caller must not cancel the
        # backend outcome it no longer listens for.
        await asyncio.shield(self._future)
        return self._value


class _Subscription:
    __slots__ = ("_action", "_closed")

    def __init__(self, action: Callable[[], None]) -> None:
        self._action: Callable[[], None] | None = action
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        action, self._action = self._action, None
        if action is not None:
            action()


class _EventSource:
    def __init__(self) -> None:
        self._subscribers: list[Callable[[Any], None]] = []

    def subscribe(self, callback: Callable[[Any], None]) -> ports.Subscription:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _Subscription(unsubscribe)

    def emit(self, event: Any) -> None:
        for callback in tuple(self._subscribers):
            callback(event)


class _Scripted:
    """FIFO per-method scripted results plus a secret-free journal."""

    journal_prefix = "fake"

    def __init__(self) -> None:
        self.journal = Journal()
        self._scripted: dict[str, deque[Any]] = {}

    def queue_result(self, method: str, result: Any) -> None:
        """Queue one scripted ``Result``, ``PairingResult``, or ``Deferred``."""
        self._scripted.setdefault(method, deque()).append(result)

    def pending(self, method: str) -> int:
        return len(self._scripted.get(method, ()))

    def _record(self, method: str, **arguments: Any) -> None:
        self.journal.record(f"{self.journal_prefix}.{method}", **arguments)

    async def _take(
        self, method: str, on_late: Callable[[Any], None] | None = None,
    ) -> Any:
        """Return the next scripted outcome; ``on_late`` observes a deferred one.

        A deferred outcome is owned by the test, so a caller that was cancelled
        leaves it intact: ``on_late`` still sees the late backend result and can
        keep its resource releasable.
        """
        queued = self._scripted.get(method)
        if not queued:
            return _UNSET
        item = queued.popleft()
        if isinstance(item, Deferred):
            if on_late is not None:
                item.add_done_callback(on_late)
            return await item.wait()
        return item

    async def _call(self, method: str, default: Callable[[], Any], **arguments: Any) -> Any:
        self._record(method, **arguments)
        scripted = await self._take(method)
        return await default() if scripted is _UNSET else scripted


# --------------------------------------------------------------------------- time seams


class SequenceIds:
    """Deterministic UUID source; the n-th call yields ``UUID(int=seed + n - 1)``."""

    def __init__(self, seed: int = 1) -> None:
        self._next = int(seed)

    def new(self) -> UUID:
        value = UUID(int=self._next)
        self._next += 1
        return value


# --------------------------------------------------------------------------- transport


class FakeTransport(_Scripted, _EventSource):
    """Transport double that fences targets, daemon instances, and exact streams."""

    journal_prefix = "transport"

    def __init__(self) -> None:
        _Scripted.__init__(self)
        _EventSource.__init__(self)
        self.daemon_instance_uuid = DEFAULT_DAEMON_INSTANCE
        self.targets: dict[ReceiverId, ReceiverTarget] = {}
        self.streams: dict[LeaseId, StreamHandle] = {}
        self.muted: dict[LeaseId, bool] = {}
        self.audio_started: dict[LeaseId, bool] = {}
        self.shutdown_called = False
        self._ids = SequenceIds(seed=500)

    def update_target(self, target: ReceiverTarget | None, receiver_id: ReceiverId) -> None:
        self._record("update_target", target=target, receiver_id=receiver_id)
        if target is None:
            self.targets.pop(receiver_id, None)
        else:
            self.targets[receiver_id] = target

    def _fresh(self, target: ReceiverTarget) -> bool:
        return self.targets.get(target.receiver_id) == target

    def _stale_stream(self, handle: StreamHandle) -> Err | None:
        if self.streams.get(handle.lease_id) != handle:
            return _failure(ErrorCode.stale_event, "Stream handle is not owned by this transport")
        if handle.daemon_instance_uuid != self.daemon_instance_uuid:
            return _failure(ErrorCode.stale_event, "Stream belongs to a previous daemon instance")
        return None

    def _adopt_stream(self, result: Any) -> None:
        """Track a late backend success so it can still be released by its exact handle."""
        if isinstance(result, Ok) and isinstance(result.value, StreamHandle):
            self.streams[result.value.lease_id] = result.value

    def _new_handle(self, target: ReceiverTarget, context: OperationContext) -> StreamHandle:
        handle = StreamHandle(
            receiver_id=target.receiver_id,
            endpoint_generation=target.endpoint_generation,
            endpoint_fingerprint=target.endpoint_fingerprint,
            backend_stream_id=f"stream-{self._ids.new()}",
            daemon_instance_uuid=self.daemon_instance_uuid,
            lease_id=LeaseId(self._ids.new()),
            start_operation_id=context.operation_id,
        )
        self.streams[handle.lease_id] = handle
        return handle

    async def query_modes(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[ReceiverCapabilities | None]:
        self._record("query_modes", target=target, context=context)
        if not self._fresh(target):
            return _failure(ErrorCode.stale_target, "Receiver endpoint is no longer current")
        scripted = await self._take("query_modes")
        if scripted is not _UNSET:
            return scripted
        return Ok(value=None)

    async def start_stream(
        self, target: ReceiverTarget, source: resources.CaptureSource,
        audio_policy: AudioPolicy, context: OperationContext,
    ) -> Result[StreamHandle]:
        self._record(
            "start_stream", target=target, source=source,
            audio_policy=audio_policy, context=context,
        )
        if not self._fresh(target):
            return _failure(ErrorCode.stale_target, "Receiver endpoint is no longer current")
        if not isinstance(source, (PhysicalMonitorSource, VirtualOutputSource)):
            return _failure(ErrorCode.unsupported_source, "Capture source is not a monitor")
        scripted = await self._take("start_stream", on_late=self._adopt_stream)
        if scripted is _UNSET:
            return Ok(value=self._new_handle(target, context))
        return scripted

    async def stop_stream(self, handle: StreamHandle, context: OperationContext) -> Result[None]:
        self._record("stop_stream", handle=handle, context=context)
        stale = self._stale_stream(handle)
        if stale is not None:
            return stale
        scripted = await self._take("stop_stream")
        result = _ok() if scripted is _UNSET else scripted
        if isinstance(result, Ok):
            self.streams.pop(handle.lease_id, None)
            self.muted.pop(handle.lease_id, None)
            self.audio_started.pop(handle.lease_id, None)
        return result

    async def set_muted(
        self, handle: StreamHandle, muted: bool, context: OperationContext,
    ) -> Result[None]:
        self._record("set_muted", handle=handle, muted=muted, context=context)
        stale = self._stale_stream(handle)
        if stale is not None:
            return stale
        scripted = await self._take("set_muted")
        result = _ok() if scripted is _UNSET else scripted
        if isinstance(result, Ok):
            self.muted[handle.lease_id] = muted
        return result

    async def start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]:
        self._record("start_audio", handle=handle, context=context)
        stale = self._stale_stream(handle)
        if stale is not None:
            return stale
        scripted = await self._take("start_audio")
        result = _ok() if scripted is _UNSET else scripted
        if isinstance(result, Ok):
            self.audio_started[handle.lease_id] = True
        return result

    async def shutdown(self) -> Result[None]:
        async def default() -> Result[None]:
            self.shutdown_called = True
            return Ok(value=None)

        return await self._call("shutdown", default)


# --------------------------------------------------------------------------- pairing


class FakePairing(_Scripted):
    """The sole pairing authority double, with generation and expiry fences."""

    journal_prefix = "pairing"

    def __init__(self, clock: FakeClock | None = None) -> None:
        super().__init__()
        self.clock = clock if clock is not None else FakeClock()
        self.trusted: dict[ReceiverId, OpaqueCredentialRef] = {}
        self.challenges: dict[ChallengeId, PairingChallenge] = {}
        self.invalid_submissions: dict[ChallengeId, int] = {}
        self._ids = SequenceIds(seed=700)

    def script_challenge(self, target: ReceiverTarget, context: OperationContext) -> PairingChallenge:
        """Publish a receiver-requested challenge without inventing a mode."""
        created_at = self.clock.now()
        challenge = PairingChallenge(
            challenge_id=ChallengeId(self._ids.new()),
            receiver_id=target.receiver_id,
            endpoint_fingerprint=target.endpoint_fingerprint,
            endpoint_generation=target.endpoint_generation,
            operation_id=context.operation_id,
            session_generation=context.session_generation,
            trust_generation=context.trust_generation or 0,
            created_at=created_at,
            expires_at=created_at + PAIRING_LIFETIME,
            credential_kind="pin",
            backend_token=OpaqueBackendToken(f"fake-pairing-token-{self._ids.new()}"),
        )
        self.challenges[challenge.challenge_id] = challenge
        return challenge

    async def begin_pairing(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[PairingChallenge]:
        async def default() -> Result[PairingChallenge]:
            return Ok(value=self.script_challenge(target, context))

        return await self._call("begin_pairing", default, target=target, context=context)

    async def submit_pairing(
        self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext,
    ) -> PairingResult:
        self._record(
            "submit_pairing", challenge_id=challenge_id, value=value, context=context,
        )
        challenge = self.challenges.get(challenge_id)
        if challenge is None:
            return _cancelled(ErrorCode.stale_event, "Pairing challenge is not active")
        superseded = (
            challenge.session_generation != context.session_generation
            or challenge.endpoint_generation != context.endpoint_generation
            or challenge.trust_generation != (context.trust_generation or 0)
        )
        if superseded:
            return _cancelled(ErrorCode.stale_event, "Pairing challenge was superseded")
        if self.clock.now() >= challenge.expires_at:
            del self.challenges[challenge_id]
            return _cancelled(ErrorCode.pairing_expired, "Pairing challenge expired")
        scripted = await self._take("submit_pairing")
        if scripted is not _UNSET:
            return scripted
        if value.reveal_for_backend() == "":
            return self._reject_submission(challenge_id)
        del self.challenges[challenge_id]
        credential = OpaqueCredentialRef(f"fake-credential-{challenge.receiver_id}")
        self.trusted[challenge.receiver_id] = credential
        return PairingResult(kind="Trusted", credential_ref=credential, diagnostic=None)

    def _reject_submission(self, challenge_id: ChallengeId) -> PairingResult:
        invalid = self.invalid_submissions.get(challenge_id, 0) + 1
        self.invalid_submissions[challenge_id] = invalid
        if invalid >= 3:
            del self.challenges[challenge_id]
            return PairingResult(
                kind="ReceiverRejected",
                credential_ref=None,
                diagnostic=Diagnostic(
                    code=ErrorCode.receiver_rejected,
                    message="Too many invalid pairing submissions",
                    recovery=(RecoveryAction.stop,),
                ),
            )
        return PairingResult(
            kind="InvalidInput",
            credential_ref=None,
            diagnostic=Diagnostic(
                code=ErrorCode.invalid_input,
                message="Pairing value was rejected",
                recovery=(RecoveryAction.retry,),
            ),
        )

    async def cancel_pairing(
        self, receiver_id: ReceiverId, challenge_id: ChallengeId | None,
        context: OperationContext,
    ) -> Result[None]:
        self._record(
            "cancel_pairing", receiver_id=receiver_id,
            challenge_id=challenge_id, context=context,
        )
        if challenge_id is not None:
            self.challenges.pop(challenge_id, None)
        else:
            for identifier, challenge in tuple(self.challenges.items()):
                if challenge.receiver_id == receiver_id:
                    del self.challenges[identifier]
        scripted = await self._take("cancel_pairing")
        return _ok() if scripted is _UNSET else scripted

    async def forget_trust(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        self._record("forget_trust", receiver_id=receiver_id, context=context)
        self.trusted.pop(receiver_id, None)
        scripted = await self._take("forget_trust")
        return _ok() if scripted is _UNSET else scripted

    async def mark_invalid(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]:
        async def default() -> Result[None]:
            self.trusted.pop(receiver_id, None)
            return Ok(value=None)

        return await self._call("mark_invalid", default, receiver_id=receiver_id, context=context)

    async def retry_revocation(
        self, receiver_id: ReceiverId, context: OperationContext,
    ) -> Result[None]:
        async def default() -> Result[None]:
            return Ok(value=None)

        return await self._call("retry_revocation", default, receiver_id=receiver_id, context=context)


def _cancelled(code: ErrorCode, message: str) -> PairingResult:
    return PairingResult(
        kind="Cancelled",
        credential_ref=None,
        diagnostic=Diagnostic(code=code, message=message, recovery=(RecoveryAction.back,)),
    )


# --------------------------------------------------------------------------- credentials


class FakeCredentialStore(_Scripted):
    """Backend keyring double; only opaque references ever leave this seam."""

    journal_prefix = "credential_store"

    def __init__(self) -> None:
        super().__init__()
        self.stored: dict[ReceiverId, OpaqueCredentialRef] = {}
        self.provisional: dict[ChallengeId, OpaqueProvisionalCredentialRef] = {}
        self.committed: dict[OpaqueCredentialRef, int] = {}
        self.revoked: list[Any] = []
        self.invalid: list[ReceiverId] = []
        self._ids = SequenceIds(seed=900)

    def seed_trusted(self, receiver_id: ReceiverId) -> OpaqueCredentialRef:
        credential = OpaqueCredentialRef(f"fake-trusted-credential-{receiver_id}")
        self.stored[receiver_id] = credential
        return credential

    async def load_credential_ref(
        self, receiver_id: ReceiverId,
    ) -> Result[OpaqueCredentialRef | None]:
        async def default() -> Result[OpaqueCredentialRef | None]:
            # The default fixture is a trusted keyring entry, as the backend would
            # return for a remembered pairing.
            return Ok(value=self.stored.get(receiver_id) or self.seed_trusted(receiver_id))

        return await self._call("load_credential_ref", default, receiver_id=receiver_id)

    async def prepare_pairing(
        self, challenge: PairingChallenge, backend_result: OpaqueBackendToken,
    ) -> Result[OpaqueProvisionalCredentialRef]:
        async def default() -> Result[OpaqueProvisionalCredentialRef]:
            provisional = OpaqueProvisionalCredentialRef(f"fake-provisional-{self._ids.new()}")
            self.provisional[challenge.challenge_id] = provisional
            return Ok(value=provisional)

        return await self._call(
            "prepare_pairing", default, challenge=challenge, backend_result=backend_result,
        )

    async def commit_pairing(
        self, provisional_ref: OpaqueProvisionalCredentialRef, trust_generation: int,
    ) -> Result[OpaqueCredentialRef]:
        async def default() -> Result[OpaqueCredentialRef]:
            credential = OpaqueCredentialRef(f"fake-credential-{self._ids.new()}")
            self.committed[credential] = trust_generation
            return Ok(value=credential)

        return await self._call(
            "commit_pairing", default, provisional_ref=provisional_ref,
            trust_generation=trust_generation,
        )

    async def cancel_pairing(self, challenge: PairingChallenge) -> Result[None]:
        async def default() -> Result[None]:
            self.provisional.pop(challenge.challenge_id, None)
            return Ok(value=None)

        return await self._call("cancel_pairing", default, challenge=challenge)

    async def revoke_pairing(
        self, ref_or_receiver: OpaqueProvisionalCredentialRef | ReceiverId,
    ) -> Result[None]:
        async def default() -> Result[None]:
            self.revoked.append(ref_or_receiver)
            if isinstance(ref_or_receiver, ReceiverId):
                self.stored.pop(ref_or_receiver, None)
            else:
                self.stored = {
                    key: value for key, value in self.stored.items()
                    if value != ref_or_receiver
                }
            return Ok(value=None)

        return await self._call("revoke_pairing", default, ref_or_receiver=ref_or_receiver)

    async def mark_invalid(self, receiver_id: ReceiverId) -> Result[None]:
        async def default() -> Result[None]:
            self.invalid.append(receiver_id)
            self.stored.pop(receiver_id, None)
            return Ok(value=None)

        return await self._call("mark_invalid", default, receiver_id=receiver_id)


# --------------------------------------------------------------------------- capture


class FakeCapture(_Scripted):
    """Portal double: physical monitors only, never a window or virtual output."""

    journal_prefix = "capture"
    NON_PHYSICAL_MARKERS = ("window", "screen-cast", "screencast", "virtual")

    def __init__(self) -> None:
        super().__init__()
        self.monitors: dict[str, PhysicalMonitorSource] = {}
        self.rejected: list[str] = []
        self.selections: list[str] = []
        self.released: list[PhysicalMonitorSource] = []

    def add_physical(self, source: PhysicalMonitorSource) -> None:
        self.monitors[source.connector] = source

    @classmethod
    def is_physical_connector(cls, connector: str) -> bool:
        lowered = connector.lower()
        return not any(marker in lowered for marker in cls.NON_PHYSICAL_MARKERS)

    async def list_physical(self) -> Result[tuple[PhysicalMonitorSource, ...]]:
        async def default() -> Result[tuple[PhysicalMonitorSource, ...]]:
            return Ok(value=tuple(self.monitors.values()))

        return await self._call("list_physical", default)

    async def select(self, connector: str) -> Result[PhysicalMonitorSource]:
        async def default() -> Result[PhysicalMonitorSource]:
            self.selections.append(connector)
            source = self.monitors.get(connector)
            if source is None or not self.is_physical_connector(connector):
                self.rejected.append(connector)
                return _failure(
                    ErrorCode.unsupported_source,
                    "Only a physical monitor source can be captured",
                )
            return Ok(value=source)

        return await self._call("select", default, connector=connector)

    async def verify(self, source: PhysicalMonitorSource) -> Result[None]:
        async def default() -> Result[None]:
            known = self.monitors.get(source.connector)
            if known is None or known != source:
                return _failure(
                    ErrorCode.unsupported_source,
                    "Capture source is not an available physical monitor",
                )
            return Ok(value=None)

        return await self._call("verify", default, source=source)

    async def release(self, source: PhysicalMonitorSource) -> Result[None]:
        async def default() -> Result[None]:
            if source not in self.released:
                self.released.append(source)
            return Ok(value=None)

        return await self._call("release", default, source=source)


# --------------------------------------------------------------------------- process


class FakeProcess(_Scripted, _EventSource):
    """Process double with exact PID/start-time and owner-environment matching."""

    journal_prefix = "process"
    OWNER_TOKEN_ENV = "AIRPLAY_LINUX_OWNER_UUID"
    RUNTIME_DIRECTORY_ENV = "AIRPLAY_LINUX_RUNTIME_DIRECTORY"

    def __init__(self) -> None:
        _Scripted.__init__(self)
        _EventSource.__init__(self)
        self.spawned: list[ports.CommandSpec] = []
        self.runs: list[tuple[ports.CommandSpec, float]] = []
        self.terminated: list[ProcessIdentity] = []
        self._identities: dict[int, ProcessIdentity] = {}
        self._environments: dict[int, Mapping[str, str]] = {}
        self._next_pid = 4000
        self._next_ticks = 90000

    @classmethod
    def owner_environment(cls, owner_uuid: UUID, runtime_directory: str) -> dict[str, str]:
        return {
            cls.OWNER_TOKEN_ENV: str(owner_uuid),
            cls.RUNTIME_DIRECTORY_ENV: runtime_directory,
        }

    def register(self, identity: ProcessIdentity, env: Mapping[str, str]) -> None:
        """Publish an identity directly, to model PID reuse by an unrelated process."""
        self._identities[identity.pid] = identity
        self._environments[identity.pid] = MappingProxyType(dict(env))

    async def run(
        self, spec: ports.CommandSpec, timeout: float, stdin: bytes | None = None,
    ) -> Result[ports.CommandOutput]:
        self.runs.append((spec, timeout))

        async def default() -> Result[ports.CommandOutput]:
            return Ok(value=ports.CommandOutput(exit_code=0, stdout=b"", stderr=b""))

        return await self._call("run", default, spec=spec, timeout=timeout, stdin=stdin)

    async def spawn(self, spec: ports.CommandSpec) -> Result[ProcessIdentity]:
        async def default() -> Result[ProcessIdentity]:
            self.spawned.append(spec)
            self._next_pid += 1
            self._next_ticks += 7
            identity = ProcessIdentity(pid=self._next_pid, start_ticks=self._next_ticks)
            self.register(identity, spec.env)
            return Ok(value=identity)

        return await self._call("spawn", default, spec=spec)

    def identity(self, pid: int) -> ProcessIdentity | None:
        return self._identities.get(pid)

    def environments(self) -> dict[int, Mapping[str, str]]:
        """The simulated ``/proc`` environment table for owner scans."""
        return dict(self._environments)

    def owned(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str) -> bool:
        if self._identities.get(identity.pid) != identity:
            return False
        environment = self._environments.get(identity.pid, {})
        return (
            environment.get(self.OWNER_TOKEN_ENV) == str(owner_uuid)
            and environment.get(self.RUNTIME_DIRECTORY_ENV) == runtime_directory
        )

    def alive(self, identity: ProcessIdentity) -> bool:
        return self._identities.get(identity.pid) == identity

    async def terminate(
        self, identity: ProcessIdentity, owner_uuid: UUID,
        runtime_directory: str, deadline: float,
    ) -> Result[None]:
        self._record(
            "terminate", identity=identity, owner_uuid=owner_uuid,
            runtime_directory=runtime_directory, deadline=deadline,
        )
        if not self.owned(identity, owner_uuid, runtime_directory):
            return _failure(
                ErrorCode.ownership_mismatch,
                "Process identity is not owned by this runtime directory",
            )
        scripted = await self._take("terminate")
        if scripted is not _UNSET:
            return scripted
        self.terminated.append(identity)
        self._forget(identity)
        self.emit(ports.ProcessExit(identity=identity, exit_code=-15))
        return _ok()

    def exit(self, identity: ProcessIdentity, exit_code: int = 0) -> None:
        """Model a helper or daemon dying on its own."""
        self._forget(identity)
        self.emit(ports.ProcessExit(identity=identity, exit_code=exit_code))

    def _forget(self, identity: ProcessIdentity) -> None:
        if self._identities.get(identity.pid) == identity:
            del self._identities[identity.pid]
            self._environments.pop(identity.pid, None)


# --------------------------------------------------------------------------- stores


class MemorySessionStore(_Scripted):
    journal_prefix = "session_store"

    def __init__(self) -> None:
        super().__init__()
        self.saved: dict[ReceiverId, SessionSnapshot] = {}

    async def save(self, snapshot: SessionSnapshot) -> Result[None]:
        async def default() -> Result[None]:
            self.saved[snapshot.receiver_id] = snapshot
            return Ok(value=None)

        return await self._call("save", default, snapshot=snapshot)

    async def load_all(self) -> Result[tuple[SessionSnapshot, ...]]:
        async def default() -> Result[tuple[SessionSnapshot, ...]]:
            return Ok(value=tuple(self.saved.values()))

        return await self._call("load_all", default)


class MemoryReceiverStore(_Scripted):
    journal_prefix = "receiver_store"

    def __init__(self) -> None:
        super().__init__()
        self.receivers: dict[ReceiverId, SavedReceiver] = {}

    async def get(self, receiver_id: ReceiverId) -> Result[SavedReceiver | None]:
        async def default() -> Result[SavedReceiver | None]:
            return Ok(value=self.receivers.get(receiver_id))

        return await self._call("get", default, receiver_id=receiver_id)

    async def all(self) -> Result[tuple[SavedReceiver, ...]]:
        async def default() -> Result[tuple[SavedReceiver, ...]]:
            return Ok(value=tuple(self.receivers.values()))

        return await self._call("all", default)

    async def put(self, saved: SavedReceiver) -> Result[None]:
        async def default() -> Result[None]:
            self.receivers[saved.receiver_id] = saved
            return Ok(value=None)

        return await self._call("put", default, saved=saved)

    async def remove_metadata(self, receiver_id: ReceiverId) -> Result[None]:
        async def default() -> Result[None]:
            self.receivers.pop(receiver_id, None)
            return Ok(value=None)

        return await self._call("remove_metadata", default, receiver_id=receiver_id)


class MemoryOwnershipStore(_Scripted):
    """Atomic-write double with an injectable failure at every durable step."""

    journal_prefix = "ownership_store"
    STEPS = ("write", "fsync", "rename", "directory_fsync")

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[OwnerId, OwnershipRecord] = {}
        self.stage_log: list[str] = []
        self._next_failure: tuple[str, int] | None = None

    def inject_failure(self, step: str, occurrence: int = 1) -> None:
        """Fail the ``occurrence``-th write from now at ``step``."""
        if step not in self.STEPS:
            raise ValueError(f"unknown atomic write step: {step}")
        if occurrence < 1:
            raise ValueError("occurrence must be at least 1")
        self._next_failure = (step, int(occurrence))

    async def write(self, record: OwnershipRecord) -> Result[None]:
        self._record("write", record=record)
        scripted = await self._take("write")
        if scripted is not _UNSET:
            return scripted
        failure: str | None = None
        if self._next_failure is not None:
            step, remaining = self._next_failure
            remaining -= 1
            if remaining == 0:
                failure = step
                self._next_failure = None
            else:
                self._next_failure = (step, remaining)
        self.stage_log = []
        for step in self.STEPS:
            self.stage_log.append(step)
            if step == "rename" and failure != "rename":
                # Rename is the publication point; a failed rename stays invisible.
                self.records[record.owner_uuid] = record
            if failure == step:
                return _failure(
                    ErrorCode.persistence_failed,
                    "Ownership record could not be published atomically",
                    detail=step,
                )
        return _ok()

    async def all(self) -> Result[tuple[OwnershipRecord, ...]]:
        async def default() -> Result[tuple[OwnershipRecord, ...]]:
            return Ok(value=tuple(self.records.values()))

        return await self._call("all", default)

    async def delete(self, owner_uuid: OwnerId) -> Result[None]:
        async def default() -> Result[None]:
            self.records.pop(owner_uuid, None)
            return Ok(value=None)

        return await self._call("delete", default, owner_uuid=owner_uuid)


class MemoryDaemonStore(_Scripted):
    journal_prefix = "daemon_store"

    def __init__(self) -> None:
        super().__init__()
        self.records: dict[InstanceId, DaemonRecord] = {}

    async def write(self, record: DaemonRecord) -> Result[None]:
        async def default() -> Result[None]:
            self.records[record.instance_uuid] = record
            return Ok(value=None)

        return await self._call("write", default, record=record)

    async def all(self) -> Result[tuple[DaemonRecord, ...]]:
        async def default() -> Result[tuple[DaemonRecord, ...]]:
            return Ok(value=tuple(self.records.values()))

        return await self._call("all", default)

    async def delete(self, instance_uuid: InstanceId) -> Result[None]:
        async def default() -> Result[None]:
            self.records.pop(instance_uuid, None)
            return Ok(value=None)

        return await self._call("delete", default, instance_uuid=instance_uuid)


# --------------------------------------------------------------------------- activity


class MemoryActivity(_Scripted):
    journal_prefix = "activity"

    def __init__(self) -> None:
        super().__init__()
        self.events: list[ActivityEvent] = []

    def emit(self, event: ActivityEvent) -> None:
        self._record("emit", event=event)
        self.events.append(event)


# --------------------------------------------------------------------------- audio


class FakeAudioProbe(_Scripted):
    journal_prefix = "audio_probe"

    async def check(self) -> Result[None]:
        async def default() -> Result[None]:
            return Ok(value=None)

        return await self._call("check", default)


# --------------------------------------------------------------------------- manager


class FakeManager(_Scripted, _EventSource):
    """Session-manager double: records commands, precomputes no transitions."""

    journal_prefix = "manager"

    def __init__(self) -> None:
        _Scripted.__init__(self)
        _EventSource.__init__(self)
        self.commands: list[messages.Command] = []
        self.events: list[messages.ResourceEvent] = []
        self.snapshots: dict[ReceiverId, SessionSnapshot] = {}
        self.shutdown_called = False
        self._ids = SequenceIds(seed=1200)

    async def dispatch(self, command: messages.Command) -> messages.CommandReceipt:
        self._record("dispatch", command=command)
        self.commands.append(command)

        async def default() -> messages.CommandReceipt:
            return messages.CommandReceipt(
                operation_id=OperationId(self._ids.new()), accepted=True, diagnostic=None,
            )

        return await self._call("dispatch", default)

    def ingest(self, event: messages.ResourceEvent) -> None:
        self._record("ingest", event=event)
        self.events.append(event)

    def snapshot(self, receiver_id: ReceiverId) -> SessionSnapshot:
        return self.snapshots.get(receiver_id) or SessionSnapshot.idle(receiver_id)

    def emit_snapshot(self, snapshot: SessionSnapshot) -> None:
        self.snapshots[snapshot.receiver_id] = snapshot
        self.emit(snapshot)

    def subscribe(self, callback: Callable[[SessionSnapshot], None]) -> ports.Subscription:
        return _EventSource.subscribe(self, callback)

    async def shutdown(self) -> Result[None]:
        async def default() -> Result[None]:
            self.shutdown_called = True
            return Ok(value=None)

        return await self._call("shutdown", default)


# --------------------------------------------------------------------------- discovery


class FakeDiscovery(_Scripted, _EventSource):
    """Discovery double with deterministic merged records and endpoint generations."""

    journal_prefix = "discovery"

    def __init__(self) -> None:
        _Scripted.__init__(self)
        _EventSource.__init__(self)
        self.targets: dict[ReceiverId, ReceiverTarget] = {}
        self.views: dict[ReceiverId, ReceiverView] = {}
        self.started = False
        self.closed = False

    @staticmethod
    def build_view(target: ReceiverTarget, online: bool) -> ReceiverView:
        record_set = ReceiverRecordSet(
            records=target.service_records,
            receiver_id=target.receiver_id,
            identity_kind="device",
            canonical_address=target.canonical_host,
            endpoint_fingerprint=target.endpoint_fingerprint,
            endpoint_generation=target.endpoint_generation,
            persistable=True,
            continuity_verified=True,
            replacement_of=None,
            ambiguous=False,
        )
        name = (
            target.service_records[0].instance
            if target.service_records
            else target.receiver_id
        )
        return ReceiverView(
            record_set=record_set, display_name=name, online=online, pairing_hint=None,
        )

    def add_target(self, target: ReceiverTarget) -> None:
        """Publish (or refresh) a target and emit its matching view."""
        known = target.receiver_id in self.targets
        self.targets[target.receiver_id] = target
        view = self.build_view(target, online=True)
        self.views[target.receiver_id] = view
        self.emit(messages.DiscoveryEvent(kind="updated" if known else "added", receiver=view, diagnostic=None))

    def remove_target(self, receiver_id: ReceiverId) -> None:
        """Emit removal while retaining the last-known view as offline."""
        target = self.targets.get(receiver_id)
        if target is None:
            return
        view = self.build_view(target, online=False)
        self.views[receiver_id] = view
        self.emit(messages.DiscoveryEvent(kind="removed", receiver=view, diagnostic=None))

    async def start(self) -> Result[None]:
        async def default() -> Result[None]:
            self.started = True
            return Ok(value=None)

        return await self._call("start", default)

    async def refresh(self) -> Result[None]:
        async def default() -> Result[None]:
            return Ok(value=None)

        return await self._call("refresh", default)

    def current(self, receiver_id: ReceiverId) -> ReceiverView | None:
        return self.views.get(receiver_id)

    async def close(self) -> None:
        self._record("close")
        self.closed = True


# --------------------------------------------------------------------------- display


class FakeDisplay(_Scripted, _EventSource):
    """KScreen/krfb double: serialized reservation, exact helper, explicit teardown."""

    journal_prefix = "display"

    def __init__(self) -> None:
        _Scripted.__init__(self)
        _EventSource.__init__(self)
        self.ownership = MemoryOwnershipStore()
        self.process = FakeProcess()
        self.outputs: dict[str, str] = {}
        self.handles: dict[OwnerId, DisplayHandle] = {}
        self.stage_log: list[tuple[OwnerId, str]] = []
        self.created: list[DisplayHandle] = []
        self.removed: list[OwnerId] = []
        self.max_concurrent_creations = 0
        self.crashes: list[tuple[OwnerId, str]] = []
        self._creation_lock = asyncio.Lock()
        self._in_flight = 0
        self._crash_at: str | None = None
        self._ids = SequenceIds(seed=1300)
        self._output_counter = 0

    # -- observability ------------------------------------------------------

    def kscreen_snapshot(self) -> LayoutSnapshot:
        """The current simulated KScreen layout, as a baseline would see it."""
        outputs = {
            output_id: {"connector": connector, "enabled": True}
            for output_id, connector in self.outputs.items()
        }
        return LayoutSnapshot(outputs=outputs, baseline_hash=f"layout-{len(outputs)}")

    def scan_owners(self) -> dict[str, PatternScan]:
        """Scan the simulated process table for children owned by this app instance."""
        scans: dict[str, PatternScan] = {}
        for pid, environment in self.process.environments().items():
            owner = environment.get(self.process.OWNER_TOKEN_ENV)
            if owner is not None:
                scans[owner] = PatternScan(pid=pid, environment=dict(environment))
        return scans

    def crash_at(self, stage: str) -> None:
        """Abort the next create at ``stage`` without cleanup, as a hard crash would."""
        if stage not in ("helper_started", "output_observed", "published"):
            raise ValueError(f"unknown crash point: {stage}")
        self._crash_at = stage

    def _maybe_crash(self, stage: str, record: OwnershipRecord) -> bool:
        if self._crash_at != stage:
            return False
        self._crash_at = None
        self.crashes.append((record.owner_uuid, stage))
        return True

    # -- record bookkeeping -------------------------------------------------

    @staticmethod
    def _ownership_record(
        owner_uuid: OwnerId, receiver_id: ReceiverId, endpoint_fingerprint: str,
        size: Size, operation_id: OperationId, stage: str, desired_state: str,
        helper: ProcessIdentity | None = None, connector: str | None = None,
        output_id: str | None = None, observed: Size | None = None,
    ) -> OwnershipRecord:
        return OwnershipRecord(
            schema_version=1,
            owner_uuid=owner_uuid,
            app_instance_uuid=APP_INSTANCE,
            runtime_directory=f"/run/user/1000/airplay-linux/instances/{APP_INSTANCE}",
            creation_operation_id=operation_id,
            receiver_id=receiver_id,
            endpoint_fingerprint=endpoint_fingerprint,
            desired_state=desired_state,
            stage=stage,
            requested_size=size,
            observed_size=observed,
            helper=helper,
            connector=connector,
            output_id=output_id,
            created_utc="1970-01-01T00:00:00Z",
            last_configuration_step=stage,
            baseline=LayoutSnapshot(outputs={}, baseline_hash="baseline"),
            last_applied={},
            provider_version="fake-kscreen-1",
            capability_fingerprint="fake-capability",
        )

    def _next_record(self, previous: OwnershipRecord, stage: str, desired_state: str) -> OwnershipRecord:
        return self._ownership_record(
            previous.owner_uuid, previous.receiver_id, previous.endpoint_fingerprint,
            previous.requested_size, previous.creation_operation_id, stage, desired_state,
            helper=previous.helper, connector=previous.connector,
            output_id=previous.output_id, observed=previous.observed_size,
        )

    def _handle(self, record: OwnershipRecord) -> DisplayHandle:
        return DisplayHandle(
            owner_uuid=record.owner_uuid,
            source=VirtualOutputSource(
                connector=record.connector or "Virtual-unknown",
                kscreen_output_id=record.output_id or "0",
                size=record.observed_size or record.requested_size,
            ),
            helper=record.helper or ProcessIdentity(pid=0, start_ticks=0),
        )

    def _emit_stage(self, kind: str, record: OwnershipRecord, handle: DisplayHandle | None = None) -> None:
        self.stage_log.append((record.owner_uuid, kind))
        self.emit(
            messages.DisplayEvent(
                kind=kind,
                receiver_id=record.receiver_id,
                creation_operation_id=record.creation_operation_id,
                owner_uuid=record.owner_uuid,
                connector=record.connector,
                output_id=record.output_id,
                helper=record.helper,
                handle=handle,
                diagnostic=None,
            ),
        )

    def _helper_spec(self, record: OwnershipRecord) -> ports.CommandSpec:
        return ports.CommandSpec(
            argv=(
                "krfb-virtualmonitor",
                "--owner", str(record.owner_uuid),
                "--width", str(record.requested_size.width),
                "--height", str(record.requested_size.height),
            ),
            env=self.process.owner_environment(record.owner_uuid, record.runtime_directory),
        )

    async def _discard(self, record: OwnershipRecord) -> None:
        """Return a partially created reservation to a clean, unowned state."""
        if record.helper is not None and self.process.owned(
            record.helper, record.owner_uuid, record.runtime_directory,
        ):
            await self.process.terminate(
                record.helper, record.owner_uuid, record.runtime_directory,
                deadline=CONTROL_TIMEOUT,
            )
        if record.output_id is not None:
            self.outputs.pop(record.output_id, None)
        self.handles.pop(record.owner_uuid, None)
        await self.ownership.delete(record.owner_uuid)

    # -- DisplayPort --------------------------------------------------------

    async def create(
        self, receiver_id: ReceiverId, endpoint_fingerprint: str,
        size: Size, context: OperationContext,
    ) -> Result[DisplayHandle]:
        self._record(
            "create", receiver_id=receiver_id, endpoint_fingerprint=endpoint_fingerprint,
            size=size, context=context,
        )
        scripted = await self._take("create")
        if scripted is not _UNSET:
            return scripted
        async with self._creation_lock:  # provider-wide; spans baseline to publication
            self._in_flight += 1
            self.max_concurrent_creations = max(self.max_concurrent_creations, self._in_flight)
            try:
                return await self._create_locked(
                    receiver_id, endpoint_fingerprint, size, context,
                )
            finally:
                self._in_flight -= 1

    async def _create_locked(
        self, receiver_id: ReceiverId, endpoint_fingerprint: str,
        size: Size, context: OperationContext,
    ) -> Result[DisplayHandle]:
        owner_uuid = OwnerId(self._ids.new())
        reserved = self._ownership_record(
            owner_uuid, receiver_id, endpoint_fingerprint, size,
            context.operation_id, "reserved", "active",
        )
        written = await self.ownership.write(reserved)
        if isinstance(written, Err):
            return written
        self._emit_stage("reserved", reserved)
        spawned = await self.process.spawn(self._helper_spec(reserved))
        if isinstance(spawned, Err):
            await self._discard(reserved)
            return spawned
        started = self._ownership_record(
            owner_uuid, receiver_id, endpoint_fingerprint, size,
            context.operation_id, "helper_started", "active", helper=spawned.value,
        )
        written = await self.ownership.write(started)
        if isinstance(written, Err):
            await self._discard(started)
            return written
        self._emit_stage("helper_started", started)
        if self._maybe_crash("helper_started", started):
            return _failure(ErrorCode.helper_failed, "Provider crashed after helper start")
        self._output_counter += 1
        output_id = f"v{self._output_counter}"
        connector = f"Virtual-{output_id}"
        self.outputs[output_id] = connector
        observed = self._ownership_record(
            owner_uuid, receiver_id, endpoint_fingerprint, size,
            context.operation_id, "output_observed", "active", helper=spawned.value,
            connector=connector, output_id=output_id, observed=size,
        )
        written = await self.ownership.write(observed)
        if isinstance(written, Err):
            await self._discard(observed)
            return written
        self._emit_stage("output_observed", observed)
        if self._maybe_crash("output_observed", observed):
            return _failure(ErrorCode.helper_failed, "Provider crashed after output observation")
        published = self._ownership_record(
            owner_uuid, receiver_id, endpoint_fingerprint, size,
            context.operation_id, "published", "active", helper=spawned.value,
            connector=connector, output_id=output_id, observed=size,
        )
        written = await self.ownership.write(published)
        if isinstance(written, Err):
            await self._discard(published)
            return written
        handle = self._handle(published)
        self.handles[owner_uuid] = handle
        self.created.append(handle)
        self._emit_stage("published", published, handle=handle)
        if self._maybe_crash("published", published):
            return _failure(ErrorCode.helper_failed, "Provider crashed after publication")
        return Ok(value=handle)

    async def prepare_teardown(
        self, receiver_id: ReceiverId, superseded_operation_id: OperationId | None,
        context: OperationContext,
    ) -> Result[tuple[OwnerId, ...]]:
        self._record(
            "prepare_teardown", receiver_id=receiver_id,
            superseded_operation_id=superseded_operation_id, context=context,
        )
        scripted = await self._take("prepare_teardown")
        if scripted is not _UNSET:
            return scripted
        admitted: list[OwnerId] = []
        for record in list(self.ownership.records.values()):
            if record.receiver_id != receiver_id or record.desired_state != "active":
                continue
            removing = self._next_record(record, "removing", "removing")
            written = await self.ownership.write(removing)
            if isinstance(written, Err):
                return written
            self._emit_stage("removing", removing)
            admitted.append(record.owner_uuid)
        return Ok(value=tuple(admitted))

    async def mark_removing(self, owner_uuid: OwnerId) -> Result[None]:
        self._record("mark_removing", owner_uuid=owner_uuid)
        record = self.ownership.records.get(owner_uuid)
        if record is None:
            return _failure(ErrorCode.ownership_mismatch, "No ownership record for this output")
        scripted = await self._take("mark_removing")
        if scripted is not _UNSET:
            return scripted
        return await self.ownership.write(self._next_record(record, "removing", "removing"))

    async def remove(self, owner_uuid: OwnerId, context: OperationContext) -> Result[None]:
        self._record("remove", owner_uuid=owner_uuid, context=context)
        scripted = await self._take("remove")
        if scripted is not _UNSET:
            return scripted
        record = self.ownership.records.get(owner_uuid)
        if record is None:
            return _failure(ErrorCode.ownership_mismatch, "No ownership record for this output")
        if record.desired_state != "removing" or record.stage != "removing":
            return _failure(
                ErrorCode.ownership_mismatch,
                "Removal intent must be published before teardown",
            )
        helper = record.helper
        # Only ever signal a process whose identity matches exactly; a reused PID
        # belongs to someone else and is never terminated.
        if helper is not None and self.process.owned(
            helper, record.owner_uuid, record.runtime_directory,
        ):
            terminated = await self.process.terminate(
                helper, record.owner_uuid, record.runtime_directory,
                deadline=CONTROL_TIMEOUT,
            )
            if isinstance(terminated, Err):
                return terminated
        if record.output_id is not None:
            self.outputs.pop(record.output_id, None)
        self.handles.pop(owner_uuid, None)
        self.removed.append(owner_uuid)
        deleted = await self.ownership.delete(owner_uuid)
        if isinstance(deleted, Err):
            return deleted
        self._emit_stage("removed", self._next_record(record, "removing", "removing"))
        return _ok()

    def reconcile(self) -> tuple[RecoveryItem, ...]:
        items: list[RecoveryItem] = []
        for record in self.ownership.records.values():
            if record.desired_state == "removing":
                kind = "removing"
            elif record.helper is not None and self.process.alive(record.helper):
                kind = "recovering"
            else:
                kind = "orphan"
            items.append(
                RecoveryItem(
                    kind=kind,
                    record=record,
                    handle=self.handles.get(record.owner_uuid),
                    diagnostic=None,
                ),
            )
        return tuple(items)

    async def approve_orphan(
        self, owner_uuid: OwnerId, confirmed: bool, context: OperationContext,
    ) -> Result[None]:
        self._record(
            "approve_orphan", owner_uuid=owner_uuid, confirmed=confirmed, context=context,
        )
        scripted = await self._take("approve_orphan")
        if scripted is not _UNSET:
            return scripted
        if not confirmed:
            return _ok()
        marked = await self.mark_removing(owner_uuid)
        if isinstance(marked, Err):
            return marked
        return await self.remove(owner_uuid, context)
