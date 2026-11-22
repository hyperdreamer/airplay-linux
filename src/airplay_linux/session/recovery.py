"""Rebuild intent only after the daemon and display owners prove resources.

Session metadata is never liveness evidence. The display provider owns its
stage machine, creation lock, exact removal and layout restoration. This module
only projects its reconciliation results into actors and rejoins ordered cleanup.
"""
from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from airplay_linux.backend.control import DispatchFence, OwnershipProof

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel, ConfirmOrphanRemoval, ConfirmSwitch, DismissCredentialRecovery, EffectEvent, KeepCleanup,
    Pair, RestartReceiverToRelease, Retry, RetryCleanup, RetryRevocation, Start, Stop,
    StopCredentialRecovery,
)
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.operations import OpaqueCredentialRef, OperationId
from airplay_linux.domain.ports import CredentialRecoveryMarkerStore
from airplay_linux.domain.records import (
    CredentialRecoveryMarker, DaemonRecord, LaunchReceipt, NoSpawnRetirement,
    OriginalDaemonIdentity, OwnedDaemonReadinessEvidence, RecoveryItem, ReleaseAttempt,
    ReleaseAttemptTerminalEvidence, ReleaseEvidence, SessionSnapshot, TerminalReplyRetirement,
)
from airplay_linux.domain.resources import (
    Diagnostic, DisplayState, Err, ErrorCode, IntentStage, Mode, Ok, RecoveryAction, Result, TrustState,
)
from airplay_linux.process.daemon_registry import OwnedDaemonRecoveryRestart
from airplay_linux.process.ownership import DaemonRecoveryExemption
from airplay_linux.storage.codecs import encode_credential_recovery_marker, is_ambiguous_identity
from airplay_linux.storage.repositories import PreferenceAccess
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry

from .engine import Handler, ManagerDeps, Route, SessionContext, SessionManager
from .reconnect import _runtime as reconnect_runtime


def _recovery_refusal(code: ErrorCode = ErrorCode.persistence_failed) -> Err:
    return Err(error=Diagnostic(
        code=code, message="Recovery state could not be verified.",
        recovery=(RecoveryAction.keep_blocked,),
    ))


def _marker_identity(marker: CredentialRecoveryMarker) -> tuple:
    return (marker.target, marker.receiver_id, marker.start_operation_id, marker.original_daemon)


class CredentialRecoveryCoordinator:
    """Publish verified marker state and synchronous target-admission fences."""

    def __init__(
        self, store: CredentialRecoveryMarkerStore, registry,
        *, fences: TrustFenceRegistry | None = None,
    ) -> None:
        self._store, self._registry, self._fences = store, registry, fences
        self._markers: dict[str, CredentialRecoveryMarker] = {}
        self._lock = asyncio.Lock()
        self._release_lock = asyncio.Lock()
        self._release_proposals: dict[str, ReleaseAttempt] = {}
        self._release_results: dict[str, OwnedDaemonRecoveryRestart] = {}
        self._factory_reservations: dict[str, OperationId] = {}
        self._reservations: dict[str, CredentialRecoveryMarker] = {}
        self._inflight: set[str] = set()
        self._failed_targets: set[str] = set()
        self._loaded = False
        self._store_blocked = True
        self._startup_pending = True

    @property
    def markers(self) -> tuple[CredentialRecoveryMarker, ...]:
        return tuple(self._markers[target] for target in sorted(self._markers))

    async def load_before_startup(self) -> Result[None]:
        self._store_blocked = True
        try:
            loaded = await self._store.read_all()
            if not isinstance(loaded, Ok) or type(loaded.value) is not tuple:
                return _recovery_refusal()
            markers = {}
            for marker in loaded.value:
                encode_credential_recovery_marker(marker)
                if marker.target in markers:
                    return _recovery_refusal()
                markers[marker.target] = marker
            confirmed = await self._store.confirm_all_durable()
            if not isinstance(confirmed, Ok) or confirmed.value is not None:
                return _recovery_refusal()
            readback = await self._store.read_all()
            if not isinstance(readback, Ok) or readback.value != loaded.value:
                return _recovery_refusal()
        except Exception:
            return _recovery_refusal()
        self._markers = markers
        self._loaded = True
        self._store_blocked = False
        return Ok(value=None)

    async def recover_before_registry_reconciliation(self) -> Result[tuple[DaemonRecoveryExemption, ...]]:
        if not self._loaded or self._store_blocked:
            return _recovery_refusal()
        exemptions = []
        authenticate = getattr(self._registry, "authenticate_recovery", None)
        for marker in self.markers:
            if marker.state == 'release_pending' and marker.release_attempt is not None:
                attempt = marker.release_attempt
                if attempt.status != 'terminal':
                    inspect_attempt = getattr(self._registry, 'inspect_recovery_attempt', None)
                    authorize = getattr(self._registry, 'authorize_recovery_attempt', None)
                    if not callable(inspect_attempt) or not callable(authorize):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)

                    def fence(marker=marker):
                        if self._store_blocked or self._markers.get(marker.target) != marker:
                            return _recovery_refusal(ErrorCode.ownership_mismatch)
                        return Ok(value=None)

                    try:
                        if authorize(marker, fence) != Ok(value=None):
                            return _recovery_refusal(ErrorCode.ownership_mismatch)
                        inspected = await inspect_attempt(marker)
                    except Exception:
                        return _recovery_refusal()
                    if not isinstance(inspected, Ok):
                        return _recovery_refusal()
                    if type(inspected.value) is ReleaseAttemptTerminalEvidence:
                        terminal = replace(attempt, status='terminal', terminal_evidence=inspected.value)
                        updated = await self._transition_for(
                            marker, marker.marker_generation, self._store.record_release_attempt, terminal,
                        )
                        if isinstance(updated, Err):
                            return updated
                        marker = updated.value
                    elif inspected.value != 'ready':
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                released = await self.restart_receiver_to_release(marker, marker.marker_generation)
                if isinstance(released, Err):
                    return released
                marker = released.value
            if (marker.state == 'retired' and marker.retirement is not None
                    and marker.retirement.kind == 'daemon_replaced'):
                authenticate_replacement = getattr(self._registry, 'authenticate_release_replacement', None)
                if not callable(authenticate_replacement):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                try:
                    result = await authenticate_replacement(marker)
                except Exception:
                    return _recovery_refusal()
                if not isinstance(result, Ok):
                    return _recovery_refusal()
                exemption = result.value
                if exemption is not None:
                    if (type(exemption) is not DaemonRecoveryExemption
                            or exemption.marker_identity != marker
                            or exemption.marker_generation != marker.marker_generation
                            or exemption.kind != 'release_replacement'
                            or exemption.daemon_identity != marker.release_attempt.readiness):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    exemptions.append(exemption)
                continue
            if marker.state != "watching" or not marker.automatic_status_checks:
                continue
            if not callable(authenticate):
                continue
            try:
                result = await authenticate(marker)
            except Exception:
                self._store_blocked = True
                return _recovery_refusal()
            if not isinstance(result, Ok):
                self._store_blocked = True
                return _recovery_refusal()
            exemption = result.value
            if exemption is None:
                continue
            if (
                type(exemption) is not DaemonRecoveryExemption
                or exemption.marker_identity != marker
                or exemption.marker_generation != marker.marker_generation
                or exemption.kind != "watching_original"
                or exemption.daemon_identity != marker.original_daemon
            ):
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            exemptions.append(exemption)
        return Ok(value=tuple(exemptions))

    def _startup_completed(self) -> None:
        self._startup_pending = False

    def require_start_allowed(self, target, start_operation_id: OperationId) -> Result[None]:
        if self._store_blocked:
            return _recovery_refusal()
        if self._startup_pending:
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        canonical = target.canonical_host if isinstance(target, ReceiverTarget) else target
        reserved_start = self._factory_reservations.get(canonical)
        if reserved_start is not None and reserved_start != start_operation_id:
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        reserved = self._reservations.get(canonical)
        marker = reserved if reserved is not None else self._markers.get(canonical)
        if marker is not None and marker.state != "retired" and (
            marker.state != "watching" or marker.start_operation_id != start_operation_id
        ):
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        return Ok(value=None)

    def start_dispatch_fence(self, target, start_operation_id: OperationId) -> DispatchFence:
        expected = (
            self._fences.get(target.receiver_id)
            if self._fences is not None and isinstance(target, ReceiverTarget) else None
        )

        def check() -> Result[None]:
            allowed = self.require_start_allowed(target, start_operation_id)
            if isinstance(allowed, Err):
                return allowed
            if self._fences is not None and isinstance(target, ReceiverTarget) and (
                expected is None or self._fences.get(target.receiver_id) != expected
                or expected.endpoint_fingerprint != target.endpoint_fingerprint
                or expected.endpoint_generation != target.endpoint_generation
            ):
                return Err(error=diagnostic(ErrorCode.stale_target, target.receiver_id))
            return Ok(value=None)

        return check

    async def begin_submission_for(
        self, receiver_id: ReceiverId, target: ReceiverTarget, start_operation_id: OperationId,
    ) -> Result[tuple[CredentialRecoveryMarker, OriginalDaemonIdentity]]:
        if type(target) is not ReceiverTarget or target.receiver_id != receiver_id:
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        canonical = target.canonical_host
        if canonical in self._factory_reservations:
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        allowed = self.require_start_allowed(target, start_operation_id)
        current = self._markers.get(canonical)
        if isinstance(allowed, Err):
            return allowed
        if current is not None and current.state != "retired":
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        fence = self.start_dispatch_fence(target, start_operation_id)
        self._factory_reservations[canonical] = start_operation_id
        try:
            async with self._lock:
                # The registry's durable receipt supplies launch and endpoint
                # identity. Authentication then repeats its full durable join.
                record = getattr(self._registry, "_record", None)
                if type(record) is not DaemonRecord or not getattr(self._registry, "_ready", False):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                receipt_result = await self._registry._receipt_for(record)
                if not isinstance(receipt_result, Ok) or type(receipt_result.value) is not LaunchReceipt:
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                receipt = receipt_result.value
                if (
                    receipt.state != "ready" or record.lifecycle != "running"
                    or record.daemon is None or receipt.daemon != record.daemon
                    or receipt.daemon_instance_uuid != record.instance_uuid
                    or receipt.runtime_directory != record.runtime_directory
                    or str(Path(receipt.runtime_directory) / receipt.socket_name) != record.socket_path
                ):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                identity = OriginalDaemonIdentity(
                    launch_uuid=receipt.launch_uuid, instance_uuid=record.instance_uuid,
                    endpoint_generation=receipt.endpoint_generation,
                    daemon_process=record.daemon, runtime_directory=record.runtime_directory,
                    socket_path=record.socket_path,
                )
                authenticated = await self._registry.authenticate_exact_identity(identity)
                if (
                    not isinstance(authenticated, Ok) or authenticated.value != record
                    or self._registry._record != record or not self._registry._ready
                ):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                allowed = fence()
                if isinstance(allowed, Err):
                    return allowed
                marker = CredentialRecoveryMarker(
                    schema_version=1, target=canonical, receiver_id=receiver_id,
                    start_operation_id=start_operation_id, original_daemon=identity,
                    marker_generation=1 if current is None else current.marker_generation + 1,
                    state="watching", automatic_status_checks=True,
                    release_attempt=None, retirement=None,
                )
                created = await self._begin_submission_locked(marker)
                if isinstance(created, Err):
                    return created
                return Ok(value=(created.value, identity))
        except Exception:
            return _recovery_refusal()
        finally:
            self._factory_reservations.pop(canonical, None)
            if canonical in self._failed_targets:
                self._reservations.pop(canonical, None)

    async def begin_submission(self, marker: CredentialRecoveryMarker) -> Result[CredentialRecoveryMarker]:
        if type(marker) is CredentialRecoveryMarker:
            reserved = self._reservations.get(marker.target)
            if reserved is not None and reserved != marker:
                return _recovery_refusal(ErrorCode.ownership_mismatch)
        async with self._lock:
            return await self._begin_submission_locked(marker)

    async def _begin_submission_locked(self, marker: CredentialRecoveryMarker) -> Result[CredentialRecoveryMarker]:
        try:
            encode_credential_recovery_marker(marker)
        except Exception:
            return _recovery_refusal()
        if not self._loaded or self._startup_pending:
            return _recovery_refusal()
        if self._store_blocked and self._reservations.get(marker.target) != marker:
            return _recovery_refusal()
        current = self._markers.get(marker.target)
        reserved = self._reservations.get(marker.target)
        if (
            marker.state != "watching" or not marker.automatic_status_checks
            or marker.target in self._inflight
            or (reserved is not None and reserved != marker)
            or (current is not None and (
                current.state != "retired" or marker.marker_generation <= current.marker_generation
            ))
        ):
            return _recovery_refusal(ErrorCode.ownership_mismatch)
        self._reservations[marker.target] = marker
        self._inflight.add(marker.target)
        try:
            result = await self._store.create(marker)
            if not isinstance(result, Ok) or result.value != marker:
                self._failed_targets.add(marker.target)
                self._store_blocked = True
                return _recovery_refusal()
            self._markers[marker.target] = marker
            self._reservations.pop(marker.target, None)
            self._failed_targets.discard(marker.target)
            self._store_blocked = bool(self._failed_targets)
            return result
        except asyncio.CancelledError:
            self._failed_targets.add(marker.target)
            self._store_blocked = True
            raise
        except Exception:
            self._failed_targets.add(marker.target)
            self._store_blocked = True
            return _recovery_refusal()
        finally:
            self._inflight.discard(marker.target)

    async def _transition_for(
        self, identity, expected_generation: int, method, *evidence,
    ) -> Result[CredentialRecoveryMarker]:
        async with self._lock:
            if type(identity) is not CredentialRecoveryMarker or type(expected_generation) is not int:
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            current = self._markers.get(identity.target)
            if (
                current is None or _marker_identity(current) != _marker_identity(identity)
                or current.marker_generation != expected_generation
                or identity.target in self._reservations
                or identity.target in self._factory_reservations
            ):
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            try:
                result = await method(identity, expected_generation, *evidence)
                if not isinstance(result, Ok):
                    return _recovery_refusal(
                        result.error.code if isinstance(result, Err) else ErrorCode.persistence_failed,
                    )
                updated = result.value
                encode_credential_recovery_marker(updated)
                if (
                    _marker_identity(updated) != _marker_identity(current)
                    or updated.marker_generation != expected_generation + 1
                ):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
            except Exception:
                return _recovery_refusal()
            self._markers[identity.target] = updated
            return Ok(value=updated)

    async def mark_release_pending_for(self, identity, expected_generation: int) -> Result[CredentialRecoveryMarker]:
        return await self._transition_for(identity, expected_generation, self._store.mark_release_pending)

    async def dismiss_for(self, identity, expected_generation: int) -> Result[CredentialRecoveryMarker]:
        return await self._transition_for(identity, expected_generation, self._store.dismiss)

    async def retire_no_spawn_for(
        self, identity, expected_generation: int, evidence: NoSpawnRetirement,
    ) -> Result[CredentialRecoveryMarker]:
        return await self._transition_for(identity, expected_generation, self._store.retire_no_spawn, evidence)

    async def retire_after_reply_for(
        self, identity, expected_generation: int, evidence: TerminalReplyRetirement,
    ) -> Result[CredentialRecoveryMarker]:
        return await self._transition_for(identity, expected_generation, self._store.retire_after_reply, evidence)

    async def restart_receiver_to_release(
        self, identity: CredentialRecoveryMarker, expected_generation: int,
        *, still_current: Callable[[], bool] | None = None,
    ) -> Result[CredentialRecoveryMarker]:
        """Durably reserve, verify and retire one endpoint-changing release."""
        def current_operation() -> bool:
            try:
                return still_current is None or still_current() is True
            except Exception:
                return False

        async with self._release_lock:
            current = self._markers.get(identity.target) if type(identity) is CredentialRecoveryMarker else None
            if (not self._loaded or self._store_blocked or current is None or not current_operation()
                    or _marker_identity(current) != _marker_identity(identity)
                    or type(expected_generation) is not int or current.marker_generation != expected_generation
                    or current.state != 'release_pending'):
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            attempt = current.release_attempt
            if attempt is None or attempt.status == 'terminal':
                attempt = self._release_proposals.get(current.target)
                if attempt is None:
                    attempt_id = OperationId(uuid4())
                    runtime = Path(self._registry._configured_runtime) / attempt_id.hex
                    attempt = ReleaseAttempt(
                        attempt_id=attempt_id, replacement_runtime_directory=str(runtime),
                        replacement_socket_path=str(runtime / 'daemon.sock'), status='intended',
                        replacement_instance_uuid=None, replacement_endpoint_generation=None,
                        readiness=None, terminal_evidence=None,
                    )
                    self._release_proposals[current.target] = attempt
                recorded = await self._transition_for(
                    current, current.marker_generation, self._store.record_release_attempt, attempt,
                )
                if isinstance(recorded, Err):
                    return recorded
                current = recorded.value
                self._release_proposals.pop(current.target, None)
                self._release_results.pop(current.target, None)
            attempt = current.release_attempt
            if attempt is None or attempt.status not in {'intended', 'ready'}:
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            async with self._lock:
                def fence() -> Result[None]:
                    if (self._store_blocked or self._markers.get(current.target) != current
                            or not current_operation()):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    return Ok(value=None)

                try:
                    if fence() != Ok(value=None):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    authorized = self._registry.authorize_recovery_attempt(current, fence)
                    if not isinstance(authorized, Ok):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    result = await self._registry.restart_with_new_endpoint(
                        current.original_daemon, attempt.attempt_id,
                        attempt.replacement_runtime_directory, attempt.replacement_socket_path,
                    )
                    if not isinstance(result, Ok) or type(result.value) is not OwnedDaemonRecoveryRestart:
                        return _recovery_refusal(
                            result.error.code if isinstance(result, Err) else ErrorCode.ownership_mismatch,
                        )
                    restart = result.value
                    ready = restart.readiness
                    receipt, record, proof = restart.receipt, restart.record, restart.old_daemon_termination
                    ownership = restart.ownership_proof
                    if (
                        type(ownership) is not OwnershipProof or ownership.kind != 'evidence'
                        or ownership.daemon != receipt.daemon
                        or ownership.generation != receipt.endpoint_generation
                        or ownership.socket_entry != (receipt.entry_device, receipt.entry_inode)
                        or type(ready) is not OwnedDaemonReadinessEvidence
                        or (ready.recovery_attempt_id, ready.runtime_directory, ready.socket_path) != (
                            attempt.attempt_id, attempt.replacement_runtime_directory, attempt.replacement_socket_path,
                        )
                        or receipt.recovery_attempt_id != attempt.attempt_id or receipt.state != 'ready'
                        or (ready.launch_uuid, ready.instance_uuid, ready.endpoint_generation) != (
                            receipt.launch_uuid, receipt.daemon_instance_uuid, receipt.endpoint_generation,
                        )
                        or (ready.daemon_pid, ready.daemon_start_ticks) != (receipt.daemon.pid, receipt.daemon.start_ticks)
                        or (ready.socket_device, ready.socket_inode) != (receipt.entry_device, receipt.entry_inode)
                        or record.instance_uuid != ready.instance_uuid or record.runtime_directory != ready.runtime_directory
                        or record.socket_path != ready.socket_path or record.daemon != receipt.daemon
                        or (proof.launch_uuid, proof.daemon_instance_uuid, proof.daemon, proof.runtime_directory) != (
                            current.original_daemon.launch_uuid, current.original_daemon.instance_uuid,
                            current.original_daemon.daemon_process, current.original_daemon.runtime_directory,
                        )
                        or fence() != Ok(value=None)
                    ):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    cached = self._release_results.get(current.target)
                    if cached is not None and (
                        cached.receipt != receipt or cached.record != record
                        or replace(ready, verified_at=cached.readiness.verified_at) != cached.readiness
                    ):
                        return _recovery_refusal(ErrorCode.ownership_mismatch)
                    if cached is None:
                        self._release_results[current.target] = restart
                        cached = restart
                except Exception:
                    return _recovery_refusal()
            if not current_operation():
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            if attempt.status == 'intended':
                recorded = await self._transition_for(
                    current, current.marker_generation, self._store.record_replacement_ready, cached.readiness,
                )
                if isinstance(recorded, Err):
                    return recorded
                current = recorded.value
                attempt = current.release_attempt
            if attempt is None or attempt.readiness is None or (
                replace(cached.readiness, verified_at=attempt.readiness.verified_at) != attempt.readiness
            ):
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            evidence = ReleaseEvidence(
                old_daemon_termination=cached.old_daemon_termination,
                replacement_instance_uuid=attempt.readiness.instance_uuid,
                replacement_endpoint_generation=attempt.readiness.endpoint_generation,
                replacement_runtime_directory=attempt.readiness.runtime_directory,
                replacement_socket_path=attempt.readiness.socket_path, readiness=attempt.readiness,
            )
            if not current_operation():
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            retired = await self._transition_for(
                current, current.marker_generation, self._store.retire_after_replacement, evidence,
            )
            if isinstance(retired, Ok):
                self._release_results.pop(current.target, None)
            return retired

    def credential_dispatch_fence(
        self, marker_identity: CredentialRecoveryMarker, marker_generation: int,
    ) -> DispatchFence:
        def check() -> Result[None]:
            if self._store_blocked:
                return _recovery_refusal()
            if type(marker_identity) is not CredentialRecoveryMarker or type(marker_generation) is not int:
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            current = self._markers.get(marker_identity.target)
            if (
                self._startup_pending or current is None or current.state != "watching"
                or current.marker_generation != marker_generation
                or _marker_identity(current) != _marker_identity(marker_identity)
                or current.target in self._reservations
            ):
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            return Ok(value=None)

        return check


@dataclass(frozen=True)
class _CredentialRestore:
    expected_fence: TrustFence
    target: ReceiverTarget | None
    result: Result[OpaqueCredentialRef | None]


class RecoveryHandlers:
    def __init__(self, deps: ManagerDeps) -> None:
        self.deps = deps
        self.items: dict[object, tuple[RecoveryItem, ...]] = {}
        self.approving: set[object] = set()

    def __call__(self, ctx: SessionContext, message: object) -> bool:
        if isinstance(message, StopCredentialRecovery):
            coordinator = self.deps.credential_recovery
            matches = tuple(marker for marker in coordinator.markers if (
                marker.receiver_id == message.receiver_id
                and marker.marker_generation == message.marker_generation
                and marker.state != 'retired'
            )) if coordinator is not None else ()
            target = ctx.state.target
            if len(matches) != 1 or (
                target is not None and target.canonical_host != matches[0].target
            ):
                ctx.apply(last_error=diagnostic(ErrorCode.stale_event, ctx.state.receiver_id))
                return True
            marker = matches[0]
            owner_operation_id = ctx.state.operation_id
            owner_session_generation = ctx.state.session_generation
            owner_trust_generation = ctx.state.trust_generation
            owner_state_target = target
            if target is None:
                target = ReceiverTarget(
                    receiver_id=marker.receiver_id, service_records=(), canonical_host=marker.target,
                    service_port=7000, protocol='airplay',
                    endpoint_fingerprint=f'recovery:{marker.original_daemon.launch_uuid.hex}',
                    endpoint_generation=0,
                )
            owner_target = target
            registry = getattr(coordinator, '_registry', None)
            stop_target = getattr(registry, 'stop_recovery_target', None)
            if not callable(stop_target):
                ctx.apply(last_error=diagnostic(ErrorCode.dependency_incompatible, ctx.state.receiver_id))
                return True
            operation = ctx.operation()
            if owner_operation_id is not None:
                operation = replace(operation, operation_id=owner_operation_id)
            operation = replace(
                operation,
                marker_generation=marker.marker_generation,
                owner_daemon=marker.original_daemon,
            )

            def still_current() -> Result[None]:
                current = next((item for item in coordinator.markers if item.target == marker.target), None)
                if (
                    not coordinator._loaded or coordinator._store_blocked
                    or current != marker
                    or ctx.state.operation_id != owner_operation_id
                    or ctx.state.session_generation != owner_session_generation
                    or ctx.state.trust_generation != owner_trust_generation
                    or ctx.state.target != owner_state_target
                    or (owner_state_target is not None
                        and owner_state_target.canonical_host != marker.target)
                ):
                    return _recovery_refusal(ErrorCode.ownership_mismatch)
                return Ok(value=None)

            ctx.launch(
                Route.recovery, 'credential_stop', operation,
                lambda: stop_target(marker, owner_target, operation, still_current),
            )
            return True
        if isinstance(message, DismissCredentialRecovery):
            coordinator = self.deps.credential_recovery
            matches = tuple(marker for marker in coordinator.markers if (
                marker.receiver_id == message.receiver_id
                and marker.marker_generation == message.marker_generation
                and marker.state != 'retired' and marker.automatic_status_checks
            )) if coordinator is not None else ()
            if len(matches) != 1:
                ctx.apply(last_error=diagnostic(ErrorCode.stale_event, ctx.state.receiver_id))
                return True
            marker = matches[0]
            operation = ctx.operation()
            if ctx.state.operation_id is not None:
                operation = replace(operation, operation_id=ctx.state.operation_id)
            ctx.launch(
                Route.recovery, 'credential_dismiss', operation,
                lambda: coordinator.dismiss_for(marker, marker.marker_generation),
            )
            return True
        if isinstance(message, RestartReceiverToRelease):
            coordinator = self.deps.credential_recovery
            matches = tuple(marker for marker in coordinator.markers if (
                marker.receiver_id == message.receiver_id
                and marker.marker_generation == message.marker_generation
                and marker.state == 'release_pending'
            )) if coordinator is not None else ()
            if len(matches) != 1:
                ctx.apply(last_error=diagnostic(ErrorCode.stale_event, ctx.state.receiver_id))
                return True
            marker = matches[0]
            owner_operation_id = ctx.state.operation_id
            owner_target = ctx.state.target
            owner_trust_generation = ctx.state.trust_generation
            owner_lease = ctx.state.transport_lease_id
            operation = ctx.operation()
            if owner_operation_id is not None:
                operation = replace(operation, operation_id=owner_operation_id)

            def still_current() -> bool:
                return (ctx.state.operation_id == owner_operation_id
                        and ctx.state.session_generation == operation.session_generation
                        and ctx.state.target == owner_target
                        and ctx.state.trust_generation == owner_trust_generation
                        and ctx.state.transport_lease_id == owner_lease)

            ctx.launch(
                Route.recovery, 'credential_release', operation,
                lambda: coordinator.restart_receiver_to_release(
                    marker, marker.marker_generation, still_current=still_current,
                ),
            )
            return True
        if not isinstance(message, EffectEvent):
            return False
        kind = message.kind.split(":", 1)[-1]
        if kind == 'credential_stop':
            effect = ctx._manager._completed_effects.get((message.receiver_id, message.operation_id, message.kind))
            if effect is None or effect.context.owner_daemon is None or effect.context.marker_generation is None:
                return True
            current = next((item for item in coordinator.markers if (
                item.receiver_id == ctx.state.receiver_id
                and item.original_daemon == effect.context.owner_daemon
                and item.marker_generation == effect.context.marker_generation
                and (
                    ctx.state.target is None
                    or item.target == ctx.state.target.canonical_host
                )
            )), None) if (coordinator := self.deps.credential_recovery) is not None else None
            if current is None or (
                ctx.state.target is not None
                and ctx.state.target.canonical_host != current.target
            ):
                return True
            ctx.apply(last_error=message.result.error if isinstance(message.result, Err) else None)
            return True
        if kind in {'credential_release', 'credential_dismiss'}:
            ctx.apply(last_error=message.result.error if isinstance(message.result, Err) else None)
            return True
        if kind == "startup":
            runtime = reconnect_runtime(ctx)
            runtime.known, runtime.target = True, ctx.state.target
            ctx.invalidate_trust()
            if ctx.state.trust is TrustState.CleanupRequired:
                handler = ctx._manager.handlers.get(Route.trust)
                if handler is not None:
                    handler(ctx, RetryRevocation(receiver_id=ctx.state.receiver_id))
            if ctx.state.intent_stage is IntentStage.stopping:
                handler = ctx._manager.handlers.get(Route.cleanup)
                if handler is not None:
                    handler(ctx, Stop(receiver_id=ctx.state.receiver_id))
            return True
        if kind == "trust_restore":
            if not isinstance(message.result, Ok) or type(message.result.value) is not _CredentialRestore:
                return True
            restore = message.result.value
            state = ctx.state
            target = state.target if state.online else None
            if (
                self.deps.fences.get(state.receiver_id) != restore.expected_fence
                or message.session_generation != state.session_generation
                or state.session_generation != restore.expected_fence.session_generation
                or state.trust_generation != restore.expected_fence.trust_generation
                or state.challenge is not None
                or state.trust is not TrustState.Unknown
                or target != restore.target
                or restore.expected_fence.receiver_id != state.receiver_id
            ):
                if state.trust is TrustState.Unknown and state.challenge is None:
                    ctx.apply(last_error=diagnostic(ErrorCode.stale_target, state.receiver_id))
                return True
            if isinstance(restore.result, Ok) and type(restore.result.value) is OpaqueCredentialRef:
                ctx.apply(trust=TrustState.Trusted, last_error=None)
            else:
                error = (
                    restore.result.error if isinstance(restore.result, Err)
                    else diagnostic(ErrorCode.keyring_unavailable, state.receiver_id)
                )
                ctx.apply(last_error=error)
            return True
        if kind == "shutdown_pending":
            ctx.cancel_timers()
            trust = ctx.state.trust
            if trust in (TrustState.Pairing, TrustState.RevocationPending):
                trust = TrustState.CleanupRequired
            has_display = (ctx.state.display_handle is not None or ctx.state.ownership_uuid is not None
                           or ctx.state.display is not DisplayState.None_)
            from .cleanup import cleanup_complete
            pending = not cleanup_complete(ctx.state, ctx.outstanding_acquisitions(),
                                           ctx._manager.pending_resources(ctx.state.receiver_id))
            ctx.apply(intent_active=False, queued_start=None, next_retry_at=None,
                      challenge=None, trust=trust, cleanup_pending=pending,
                      intent_stage=IntentStage.cleanup if pending else IntentStage.idle,
                      display=DisplayState.CleanupRequired if has_display else ctx.state.display,
                      last_error=diagnostic(ErrorCode.cleanup_timeout, ctx.state.receiver_id))
            return True
        if kind == "persistence_failed":
            # Failure is a separate diagnostic; preserve every independent
            # resource fact (especially a still-owned stream or display).
            ctx.apply(last_error=diagnostic(ErrorCode.persistence_failed, ctx.state.receiver_id))
            return True
        if kind.startswith("orphan_"):
            owner = next((item.record.owner_uuid for item in self.items.get(ctx.state.receiver_id, ())
                          if f"orphan_{item.record.owner_uuid}" == kind), None)
            self.approving.discard(owner)
            if isinstance(message.result, Err):
                ctx.apply(last_error=message.result.error, cleanup_pending=True)
            else:
                remaining = tuple(item for item in self.items.get(ctx.state.receiver_id, ())
                                  if item.record.owner_uuid != owner)
                self.items[ctx.state.receiver_id] = remaining
                if not remaining:
                    ctx.apply(display=DisplayState.None_, display_handle=None, ownership_uuid=None,
                              intent_active=False, intent_stage=IntentStage.idle,
                              cleanup_pending=False, last_error=None)
            return True
        return False

    def cleanup(self, previous: Handler, ctx: SessionContext, message: object) -> bool:
        items = self.items.get(ctx.state.receiver_id, ())
        blocked = any(item.kind in {"orphan", "cleanup_required"}
                      and item.record.desired_state != "removing" and item.record.stage != "removing"
                      for item in items)
        if isinstance(message, ConfirmOrphanRemoval):
            item = next((item for item in items if item.kind == "orphan"
                         and item.record.owner_uuid == message.owner_uuid), None)
            if item is None or not message.confirmed or message.owner_uuid in self.approving:
                return True
            self.approving.add(message.owner_uuid)
            operation = ctx.operation()
            ctx.apply(operation_id=operation.operation_id)
            ctx.launch(Route.recovery, f"orphan_{message.owner_uuid}", operation,
                       lambda: self.deps.display.approve_orphan(message.owner_uuid, True, operation))
            return True
        if blocked and isinstance(message, (Stop, Cancel, RetryCleanup, KeepCleanup, ConfirmSwitch)):
            # A UUID is journal evidence, not proof of a live output. In
            # particular shutdown must never convert an orphan into auto-remove.
            if isinstance(message, (Stop, Cancel)):
                if ctx.state.intent_active:
                    ctx.supersede("stop")
                ctx.cancel_timers()
            ctx.apply(intent_active=False, cleanup_pending=True, intent_stage=IntentStage.cleanup,
                      display=DisplayState.CleanupRequired,
                      last_error=diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
            return True
        return previous(ctx, message)


def build_recovery_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    return {Route.recovery: RecoveryHandlers(deps)}


class StartupRecovery:
    """Run once before UI commands; install all actors before any continuation.

    The cleanup route needs one small adapter for orphan commands, since the
    frozen manager routes those commands to cleanup rather than recovery.
    """
    def __init__(
        self,
        manager: SessionManager,
        deps: ManagerDeps,
        registry,
        *,
        credential_loader: Callable[[ReceiverId], Awaitable[Result[OpaqueCredentialRef | None]]] | None = None,
    ) -> None:
        self.manager, self.deps, self.registry = manager, deps, registry
        self._credential_loader = credential_loader
        handler = manager.handlers.get(Route.recovery)
        if handler is None:
            handler = RecoveryHandlers(deps)
            manager.handlers[Route.recovery] = handler
        if not isinstance(handler, RecoveryHandlers):
            raise TypeError("recovery route must come from build_recovery_handlers")
        self.handler = handler
        self._result = None
        self._blocked = True
        for route, previous in tuple(manager.handlers.items()):
            if route is Route.recovery:
                continue

            def guarded(ctx, message, previous=previous, route=route):
                if self._blocked and isinstance(message, (Start, Retry, Pair, ConfirmSwitch)):
                    ctx.apply(last_error=diagnostic(ErrorCode.ownership_mismatch, ctx.state.receiver_id))
                    return True
                if route is Route.cleanup:
                    return self.handler.cleanup(previous, ctx, message)
                return previous(ctx, message)

            manager.handlers[route] = guarded

    async def run(self) -> Result[tuple[RecoveryItem, ...]]:
        if self._result is not None:
            return self._result
        try:
            self._result = await self._restore()
        except Exception:  # never retain adapter exception strings
            self._result = Err(error=diagnostic(ErrorCode.persistence_failed))
        return self._result

    async def _restore(self):
        coordinator = self.deps.credential_recovery
        if coordinator is None:
            reconciled = await self.registry.reconcile_previous()
        else:
            loaded = await coordinator.load_before_startup()
            if isinstance(loaded, Err):
                return loaded
            exemptions = await coordinator.recover_before_registry_reconciliation()
            if isinstance(exemptions, Err):
                return exemptions
            reconcile = self.registry.reconcile_previous
            parameters = inspect.signature(reconcile).parameters
            accepts_exemptions = "recovery_exemptions" in parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            )
            if accepts_exemptions:
                reconciled = await reconcile(recovery_exemptions=exemptions.value)
            elif exemptions.value:
                return _recovery_refusal(ErrorCode.ownership_mismatch)
            else:
                reconciled = await reconcile()
        if not isinstance(reconciled, Ok):
            return reconciled if isinstance(reconciled, Err) else Err(error=diagnostic(ErrorCode.ownership_mismatch))
        snapshots = await self.deps.sessions.load_all()
        load_error = getattr(self.deps.sessions, "load_error", None)
        if isinstance(load_error, Err):
            return load_error
        quarantined = getattr(self.deps.sessions, "quarantined", None)
        failures = quarantined() if callable(quarantined) else ()
        if failures:
            # The tuple-only SessionStore contract uses this side channel for
            # unreadable metadata. Missing trust/intent is unknown, not idle.
            return Err(error=failures[0])
        preferences = await self.deps.receivers.all()
        if isinstance(preferences, Err):
            return preferences
        items = await self.deps.display.reconcile()
        failure = getattr(self.deps.display, "last_diagnostic", None)
        if failure is not None:
            return Err(error=failure)
        ownerships = await self.deps.ownerships.all()
        if isinstance(ownerships, Err):
            return ownerships
        durable = {record.owner_uuid: record for record in ownerships.value}
        grouped = defaultdict(list)
        for item in items:
            # Removed/discarded records are no longer resources, even when the
            # provider returns their historical reconciliation action.
            if item.record.owner_uuid in durable:
                grouped[item.record.receiver_id].append(item)
        # An incomplete provider response cannot make a durable reservation
        # disappear. Surface it conservatively without manufacturing a handle.
        seen = {item.record.owner_uuid for item in items}
        for record in durable.values():
            if record.owner_uuid not in seen:
                grouped[record.receiver_id].append(RecoveryItem(
                    kind="cleanup_required", record=record, handle=None,
                    diagnostic=diagnostic(ErrorCode.ownership_mismatch, record.receiver_id),
                ))
        removing_receivers = {item.record.receiver_id for item in items
                              if item.record.desired_state == "removing" or item.record.stage == "removing"}
        saved = {record.receiver_id: record for record in preferences.value}
        sessions = {state.receiver_id: state for state in snapshots}
        receivers = set(sessions) | set(saved) | set(grouped) | removing_receivers
        for receiver_id in receivers:
            old = sessions.get(receiver_id, SessionSnapshot.idle(receiver_id))
            candidates = tuple(grouped[receiver_id])
            self.handler.items[receiver_id] = candidates
            view = self.deps.discovery.current(receiver_id)
            online = view is not None and view.online
            destination = self.manager._target_from_view(view) if online else None
            preference = PreferenceAccess.for_record_set(view.record_set, saved.get(receiver_id)) if view else None
            continuous = not is_ambiguous_identity(receiver_id) and (
                view is None or (view.record_set.persistable and not view.record_set.ambiguous
                                 and view.record_set.replacement_of is None
                                 and (view.record_set.identity_kind == "device" or view.record_set.continuity_verified))
            )
            tombstone = old.trust in (
                TrustState.Pairing, TrustState.RevocationPending, TrustState.CleanupRequired,
            )
            trust_intent_safe = (
                not old.cleanup_pending
                and old.intent_stage not in (IntentStage.stopping, IntentStage.cleanup)
            )
            should_restore_trust = (
                not tombstone
                and receiver_id not in removing_receivers
                and trust_intent_safe
                and not candidates
            )
            trust_error = diagnostic(ErrorCode.stale_target, receiver_id) if not continuous and should_restore_trust else None
            # Construct from the idle defaults: no stale capture/stream/audio
            # token, challenge, mute, backoff or queued mode switch survives.
            state = replace(
                SessionSnapshot.idle(receiver_id, online=online), target=destination, mode=old.mode,
                intent_active=old.intent_active,
                intent_stage=IntentStage.recovering if old.intent_active else IntentStage.idle,
                audio_policy=old.audio_policy,
                remembered_volume=preference.remembered_volume if continuous and preference else None,
                session_generation=old.session_generation, trust_generation=old.trust_generation + 1,
                trust=TrustState.CleanupRequired if tombstone else TrustState.Unknown,
                last_error=trust_error,
                resolution=(preference.remembered_resolution if preference else old.resolution) if continuous else None,
            )
            if len(candidates) == 1:
                item = candidates[0]
                record = durable[item.record.owner_uuid]
                removing = record.desired_state == "removing" or record.stage == "removing"
                exact = (item.kind == "recovering" and record.stage == "published"
                         and not removing and item.handle is not None
                         and item.handle.owner_uuid == record.owner_uuid
                         and item.handle.helper == record.helper
                         and item.handle.source.connector == record.connector
                         and item.handle.source.kscreen_output_id == record.output_id
                         and item.handle.source.size == record.observed_size == record.requested_size)
                should_restore_trust = (
                    should_restore_trust or (exact and not tombstone and trust_intent_safe)
                )
                state = replace(state, mode=Mode.VIRTUAL, ownership_uuid=record.owner_uuid,
                                display_handle=item.handle if exact else None,
                                display=DisplayState.Owned if exact else DisplayState.CleanupRequired,
                                intent_active=old.intent_active and not removing,
                                intent_stage=IntentStage.stopping if removing else (
                                    IntentStage.recovering if exact else IntentStage.cleanup),
                                cleanup_pending=not exact,
                                observed_resolution=record.observed_size,
                                last_error=item.diagnostic if not exact else trust_error)
            elif candidates:
                state = replace(state, mode=Mode.VIRTUAL, intent_stage=IntentStage.cleanup,
                                display=DisplayState.CleanupRequired, cleanup_pending=True,
                                last_error=diagnostic(ErrorCode.ownership_mismatch, receiver_id))
            elif receiver_id in removing_receivers or old.cleanup_pending or old.intent_stage in (IntentStage.stopping, IntentStage.cleanup):
                # Prior daemon release and the provider's empty durable set are
                # both confirmed, so stale stream/display references can clear.
                state = replace(state, intent_active=False, intent_stage=IntentStage.idle)
            if trust_error is not None and state.last_error is None:
                state = replace(state, last_error=trust_error)
            self.manager.install(state)
            self.manager._fence_receiver(state)
            self.deps.transport.update_target(destination, receiver_id)
            if should_restore_trust and continuous:
                credential_loader = self._credential_loader
                if credential_loader is None:
                    credential_loader = getattr(self.deps.pairing, "load_credential_ref", None)
                expected_fence = self.deps.fences.get(receiver_id)
                if expected_fence is None:
                    expected_fence = TrustFence(
                        receiver_id=receiver_id,
                        endpoint_fingerprint=destination.endpoint_fingerprint if destination else None,
                        endpoint_generation=destination.endpoint_generation if destination else 0,
                        session_generation=state.session_generation,
                        trust_generation=state.trust_generation,
                        active_challenge_id=None,
                    )
                    loaded = Err(error=diagnostic(ErrorCode.stale_target, receiver_id))
                elif not callable(credential_loader):
                    loaded = Err(error=diagnostic(ErrorCode.keyring_unavailable, receiver_id))
                else:
                    try:
                        loaded = await credential_loader(receiver_id)
                    except Exception:
                        loaded = Err(error=diagnostic(ErrorCode.keyring_unavailable, receiver_id))
                self.manager.ingest(EffectEvent(
                    receiver_id=receiver_id,
                    operation_id=OperationId(self.deps.ids.new()),
                    session_generation=state.session_generation,
                    kind="recovery:trust_restore",
                    result=Ok(value=_CredentialRestore(
                        expected_fence=expected_fence,
                        target=destination,
                        result=loaded,
                    )),
                    resource=None,
                ))
        self._blocked = False
        if coordinator is not None:
            coordinator._startup_completed()
        for receiver_id in receivers:
            state = self.manager.snapshot(receiver_id)
            self.manager.ingest(EffectEvent(
                receiver_id=receiver_id, operation_id=OperationId(self.deps.ids.new()),
                session_generation=state.session_generation, kind="recovery:startup",
                result=Ok(value=None), resource=None,
            ))
        await self.manager.drain()
        return Ok(value=items)


__all__ = ["CredentialRecoveryCoordinator", "build_recovery_handlers", "StartupRecovery"]
