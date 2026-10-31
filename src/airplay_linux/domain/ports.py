"""Adapter boundaries; failures return sanitized domain results, never exceptions."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    # Postponed foundation-lane signature; no import of the unfinished implementation.
    from airplay_linux.process.procfs import TerminationEvidence

    from .identity import ReceiverId, ReceiverTarget, ReceiverView
    from .messages import (
        Command,
        CommandReceipt,
        DiscoveryEvent,
        DisplayEvent,
        ResourceEvent,
        TransportEvent,
    )
    from .operations import (
        ChallengeId,
        DisplayHandle,
        InstanceId,
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
    from .records import (
        ActivityEvent,
        DaemonCleanupIntent,
        DaemonRecord,
        LaunchReceipt,
        SupervisorReleaseIntent,
        OwnershipRecord,
        RecoveryItem,
        SavedReceiver,
        SessionSnapshot,
    )
    from .resources import (
        AudioPolicy,
        CaptureSource,
        PhysicalMonitorSource,
        ReceiverCapabilities,
        Result,
        Size,
    )


class Clock(Protocol):
    def now(self) -> float: ...


class CancelHandle(Protocol):
    def cancel(self) -> None: ...


class Scheduler(Protocol):
    def call_at(self, when: float, callback: Callable[[], None]) -> CancelHandle: ...


class IdFactory(Protocol):
    def new(self) -> UUID: ...


class Subscription(Protocol):
    def close(self) -> None: ...


class DiscoveryPort(Protocol):
    async def start(self) -> Result[None]: ...

    async def refresh(self) -> Result[None]: ...

    def current(self, receiver_id: ReceiverId) -> ReceiverView | None: ...

    def subscribe(self, callback: Callable[[DiscoveryEvent], None]) -> Subscription: ...

    async def close(self) -> None: ...


class CapturePort(Protocol):
    async def list_physical(self) -> Result[tuple[PhysicalMonitorSource, ...]]: ...

    async def select(self, connector: str) -> Result[PhysicalMonitorSource]: ...

    async def verify(self, source: PhysicalMonitorSource) -> Result[None]: ...

    async def release(self, source: PhysicalMonitorSource) -> Result[None]:
        """Idempotently close this exact portal session after stream release.

        Stale successful selections also require release.
        """
        ...


class TransportPort(Protocol):
    """Verify target freshness immediately before backend I/O, including retries."""

    def update_target(self, target: ReceiverTarget | None, receiver_id: ReceiverId) -> None: ...

    async def query_modes(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[ReceiverCapabilities | None]: ...

    async def start_stream(
        self, target: ReceiverTarget, source: CaptureSource,
        audio_policy: AudioPolicy, context: OperationContext,
    ) -> Result[StreamHandle]: ...

    async def stop_stream(self, handle: StreamHandle, context: OperationContext) -> Result[None]: ...

    async def set_muted(
        self, handle: StreamHandle, muted: bool, context: OperationContext,
    ) -> Result[None]: ...

    async def start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]: ...

    def subscribe(self, callback: Callable[[TransportEvent], None]) -> Subscription: ...

    async def shutdown(self) -> Result[None]: ...


class PairingPort(Protocol):
    """Implemented by the transport coordinator, the sole pairing authority."""

    async def begin_pairing(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[PairingChallenge]: ...

    async def submit_pairing(
        self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext,
    ) -> PairingResult: ...

    async def cancel_pairing(
        self, receiver_id: ReceiverId, challenge_id: ChallengeId | None,
        context: OperationContext,
    ) -> Result[None]: ...

    async def forget_trust(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]: ...

    async def mark_invalid(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]: ...

    async def retry_revocation(self, receiver_id: ReceiverId, context: OperationContext) -> Result[None]: ...


class CredentialStore(Protocol):
    """Backend keyring transactions; application metadata never holds secrets."""

    async def load_credential_ref(self, receiver_id: ReceiverId) -> Result[OpaqueCredentialRef | None]: ...

    async def prepare_pairing(
        self, challenge: PairingChallenge, backend_result: OpaqueBackendToken,
    ) -> Result[OpaqueProvisionalCredentialRef]: ...

    async def commit_pairing(
        self, provisional_ref: OpaqueProvisionalCredentialRef, trust_generation: int,
    ) -> Result[OpaqueCredentialRef]: ...

    async def cancel_pairing(self, challenge: PairingChallenge) -> Result[None]: ...

    async def revoke_pairing(
        self, ref_or_receiver: OpaqueProvisionalCredentialRef | ReceiverId,
    ) -> Result[None]: ...

    async def mark_invalid(self, receiver_id: ReceiverId) -> Result[None]: ...


class DisplayPort(Protocol):
    """Display events and removal use owner/helper/output identity, not endpoints."""

    async def create(
        self, receiver_id: ReceiverId, endpoint_fingerprint: str,
        size: Size, context: OperationContext,
    ) -> Result[DisplayHandle]: ...

    async def prepare_teardown(
        self, receiver_id: ReceiverId, superseded_operation_id: OperationId | None,
        context: OperationContext,
    ) -> Result[tuple[OwnerId, ...]]:
        """Fence pending creation and durably mark every admitted reservation removing.

        Do not stop helpers here. The gate also covers a deferred create with no
        owner UUID yet, so it cannot later reserve or publish an active output.
        """
        ...

    async def mark_removing(self, owner_uuid: OwnerId) -> Result[None]: ...

    async def remove(self, owner_uuid: OwnerId, context: OperationContext) -> Result[None]: ...

    async def reconcile(self) -> tuple[RecoveryItem, ...]: ...

    async def approve_orphan(
        self, owner_uuid: OwnerId, confirmed: bool, context: OperationContext,
    ) -> Result[None]: ...

    def subscribe(self, callback: Callable[[DisplayEvent], None]) -> Subscription: ...


class SessionStore(Protocol):
    async def save(self, snapshot: SessionSnapshot) -> Result[None]: ...

    async def load_all(self) -> tuple[SessionSnapshot, ...]: ...


class ReceiverStore(Protocol):
    async def get(self, receiver_id: ReceiverId) -> Result[SavedReceiver | None]: ...

    async def all(self) -> Result[tuple[SavedReceiver, ...]]: ...

    async def put(self, saved: SavedReceiver) -> Result[None]: ...

    async def remove_metadata(self, receiver_id: ReceiverId) -> Result[None]: ...


class OwnershipStore(Protocol):
    async def write(self, record: OwnershipRecord) -> Result[None]: ...

    async def all(self) -> Result[tuple[OwnershipRecord, ...]]: ...

    async def delete(self, owner_uuid: OwnerId) -> Result[None]: ...


class DaemonStore(Protocol):
    async def write(self, record: DaemonRecord) -> Result[None]: ...

    async def all(self) -> Result[tuple[DaemonRecord, ...]]: ...

    async def delete(self, instance_uuid: InstanceId) -> Result[None]: ...


class LaunchReceiptStore(Protocol):
    async def write(self, record: LaunchReceipt) -> Result[None]: ...

    async def all(self) -> Result[tuple[LaunchReceipt, ...]]: ...


class DaemonCleanupIntentStore(Protocol):
    async def write(self, record: DaemonCleanupIntent) -> Result[None]: ...

    async def all(self) -> Result[tuple[DaemonCleanupIntent, ...]]: ...


class SupervisorReleaseIntentStore(Protocol):
    async def write(self, record: SupervisorReleaseIntent) -> Result[None]: ...

    async def read(self) -> Result[SupervisorReleaseIntent | None]: ...


class ActivitySink(Protocol):
    def emit(self, event: ActivityEvent) -> None: ...


@dataclass(frozen=True, kw_only=True)
class CommandSpec:
    """Process launch data; callers must never put sensitive input in argv."""

    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None = None
    pass_fds: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


@dataclass(frozen=True, kw_only=True)
class CommandOutput:
    exit_code: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, kw_only=True)
class ProcessExit:
    identity: ProcessIdentity
    exit_code: int


class ProcessPort(Protocol):
    async def run(
        self, spec: CommandSpec, timeout: float, stdin: bytes | None = None,
    ) -> Result[CommandOutput]: ...

    async def spawn(self, spec: CommandSpec) -> Result[ProcessIdentity]: ...

    def identity(self, pid: int) -> ProcessIdentity | None: ...

    def owned(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str) -> bool: ...

    async def terminate(
        self, identity: ProcessIdentity, owner_uuid: UUID,
        runtime_directory: str, deadline: float, *, strict: bool = False,
        evidence: TerminationEvidence | None = None,
        revalidate: Callable[[], Result[TerminationEvidence]] | None = None,
    ) -> Result[None]: ...

    def subscribe(self, callback: Callable[[ProcessExit], None]) -> Subscription: ...


class AudioProbe(Protocol):
    async def check(self) -> Result[None]: ...


class ManagerPort(Protocol):
    async def dispatch(self, command: Command) -> CommandReceipt: ...

    def ingest(self, event: ResourceEvent) -> None: ...

    def snapshot(self, receiver_id: ReceiverId) -> SessionSnapshot: ...

    def subscribe(self, callback: Callable[[SessionSnapshot], None]) -> Subscription: ...

    async def shutdown(self) -> Result[None]: ...
