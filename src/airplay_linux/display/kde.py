"""DisplayPort composition over the staged KDE creation/recovery/removal owners."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.messages import DisplayEvent
from airplay_linux.domain.operations import DisplayHandle, OperationContext, OperationId, OwnerId
from airplay_linux.domain.records import RecoveryItem
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result, Size

from .creation import CreationProgress, DisplayCreator
from .helper import VirtualHelper
from .kscreen import KScreenObserver
from .recovery import DisplayRecovery
from .removal import DisplayRemover, _Subscription


class KdeVirtualDisplayProvider:
    def __init__(self, creator: DisplayCreator, recovery: DisplayRecovery, remover: DisplayRemover,
                 observer: KScreenObserver, helper: VirtualHelper):
        self.creator, self.recovery, self.remover = creator, recovery, remover
        self.observer, self.helper = observer, helper
        self._callbacks: list[Callable[[DisplayEvent], None]] = []
        self._progress: dict[OwnerId, DisplayEvent] = {}
        self._creation_subscription = creator.subscribe(self._creation_progress)
        self._removal_subscription = remover.subscribe(self._emit)
        self._observer_subscription = observer.subscribe(self._outputs_changed)

    @property
    def last_diagnostic(self) -> Diagnostic | None:
        return self.recovery.last_diagnostic

    def subscribe(self, callback: Callable[[DisplayEvent], None]) -> _Subscription:
        self._callbacks.append(callback)
        return _Subscription(self._callbacks, callback)

    def _emit(self, event: DisplayEvent) -> None:
        if event.kind == "removed":
            self._progress.pop(event.owner_uuid, None)
        for callback in tuple(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue

    def _creation_progress(self, progress: CreationProgress):
        if progress.stage == "removing":
            self.remover.fence(progress.owner_uuid)
            kind = "cleanup_required"
        else:
            kind = progress.stage
        event = DisplayEvent(
            kind=kind, receiver_id=progress.receiver_id, creation_operation_id=progress.creation_operation_id,
            owner_uuid=progress.owner_uuid, connector=progress.connector, output_id=progress.output_id,
            helper=progress.helper, handle=progress.handle if kind == "published" else None, diagnostic=None,
        )
        self._progress[event.owner_uuid] = event
        self.remover.track_event(event)
        self._emit(event)

    async def create(self, receiver_id: ReceiverId, endpoint_fingerprint: str, size: Size,
                     context: OperationContext) -> Result[DisplayHandle]:
        result = await self.creator.create(receiver_id, endpoint_fingerprint, size, context)
        if isinstance(result, Ok):
            # CreationProgress is intentionally a projection, not a disk record.
            # Load the durable record before accepting lifetime observations.
            found = await self.remover.lookup(result.value.owner_uuid)
            error = found.error if isinstance(found, Err) else None
            if isinstance(found, Ok) and found.value is not None:
                record = found.value
                observed = await self.remover.observe()
                if isinstance(observed, Err):
                    error = observed.error
                else:
                    state, output = self.remover.output_evidence(record, *observed.value)
                    if state != "exact" or not self.recovery._configured(record, output):
                        error = diagnostic(ErrorCode.ownership_mismatch, receiver_id)
                    else:
                        verified = await self.remover.verify_output(record, output)
                        if isinstance(verified, Err):
                            error = verified.error
                        else:
                            readback = await self.remover.observe()
                            if isinstance(readback, Err):
                                error = readback.error
                            else:
                                final_state, final = self.remover.output_evidence(record, *readback.value)
                                if final_state != "exact" or final != output or self.remover.helper_evidence(record) != ("live", record.helper):
                                    error = diagnostic(ErrorCode.ownership_mismatch, receiver_id)
                        if self.remover.is_removing(record.owner_uuid):
                            error = diagnostic(ErrorCode.stale_event, receiver_id)
            elif error is None:
                error = diagnostic(ErrorCode.persistence_failed, receiver_id)
            if error is not None:
                # A failed final check must survive restart as cleanup intent,
                # so a later reconciliation cannot revive the failed start.
                admitted = await self.mark_removing(result.value.owner_uuid)
                if isinstance(admitted, Err):
                    error = admitted.error
                projection = self._progress.get(result.value.owner_uuid)
                if projection is not None:
                    self._emit(replace(projection, kind="cleanup_required", handle=None, diagnostic=error))
                return Err(error=error)
        return result

    async def prepare_teardown(self, receiver_id: ReceiverId, superseded_operation_id: OperationId | None,
                               context: OperationContext) -> Result[tuple[OwnerId, ...]]:
        for record in self.remover.known_records:
            if record.receiver_id == receiver_id:
                self.remover.fence(record.owner_uuid)
        return await self.creator.prepare_teardown(receiver_id, superseded_operation_id, context)

    async def mark_removing(self, owner_uuid: OwnerId) -> Result[None]:
        self.remover.fence(owner_uuid)
        found = await self.remover.lookup(owner_uuid)
        if isinstance(found, Err):
            return found
        if found.value is not None:
            # Creator owns a separate per-owner journal and the in-flight start
            # gate. Close both before allowing the remover to signal anything.
            context = OperationContext(operation_id=OperationId(uuid4()), session_generation=0, endpoint_generation=0)
            prepared = await self.creator.prepare_teardown(found.value.receiver_id, None, context)
            if isinstance(prepared, Err):
                return prepared
        return await self.remover.mark_removing(owner_uuid)

    async def remove(self, owner_uuid: OwnerId, context: OperationContext) -> Result[None]:
        marked = await self.mark_removing(owner_uuid)
        if isinstance(marked, Err):
            return marked
        return await self.remover.remove(owner_uuid, context)

    async def reconcile(self) -> tuple[RecoveryItem, ...]:
        return await self.recovery.reconcile()

    async def approve_orphan(self, owner_uuid: OwnerId, confirmed: bool, context: OperationContext) -> Result[None]:
        return await self.remover.approve_orphan(owner_uuid, confirmed, context)

    def _outputs_changed(self, outputs):
        for record in self.remover.known_records:
            if record.output_id is None or record.connector is None or self.remover.is_removing(record.owner_uuid):
                continue
            if not any(o.output_id == record.output_id and o.connector == record.connector for o in outputs):
                self.remover.invalidate(record, "disappeared")
        for event in tuple(self._progress.values()):
            if event.output_id is None or event.connector is None or self.remover.is_removing(event.owner_uuid):
                continue
            if not any(o.output_id == event.output_id and o.connector == event.connector for o in outputs):
                self.remover.invalidate_event(event, "disappeared")

    async def flush_events(self):
        await self.remover.flush_events()


__all__ = ["KdeVirtualDisplayProvider"]
