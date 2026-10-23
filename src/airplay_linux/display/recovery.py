"""Recover durable ownership stages without reconnecting removing resources."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import DisplayHandle, OperationContext, OperationId
from airplay_linux.domain.records import OwnershipRecord, RecoveryItem
from airplay_linux.domain.resources import Err, ErrorCode, Ok, VirtualOutputSource

from .removal import DisplayRemover, OutputVerifier, REMOVAL_TIMEOUT, _failure


class DisplayRecovery:
    """Inspect all stages, repair only proved ownership, and publish last.

    The frozen tuple-returning port cannot carry a global store failure without
    an OwnershipRecord. ``last_diagnostic`` retains that error for composition;
    it must not interpret an empty tuple as successful reconciliation when this
    property is set. Unknown/no-record outputs are outside our authority.
    """
    def __init__(self, helper, observer, ownership_store, creation_lock, clock, remover: DisplayRemover,
                 *, output_verifier: OutputVerifier | None = None):
        self.helper, self.observer, self.store = helper, observer, ownership_store
        self.creation_lock, self.clock, self.remover = creation_lock, clock, remover
        self._output_verifier = output_verifier
        self.last_diagnostic = None

    async def reconcile(self) -> tuple[RecoveryItem, ...]:
        self.last_diagnostic = None
        try:
            loaded = await self.store.all()
        except Exception:
            loaded = _failure(ErrorCode.persistence_failed)
        if isinstance(loaded, Err):
            self.last_diagnostic = loaded.error
            return ()
        items = []
        for initial in loaded.value:
            cleanup = False
            try:
                async with self.creation_lock.acquire():
                    found = await self.remover.lookup(initial.owner_uuid)
                    if isinstance(found, Err):
                        item = self._item("cleanup_required", initial, found.error)
                    elif found.value is None:
                        continue
                    else:
                        record = found.value
                        if record.desired_state == "removing" or record.stage == "removing" or self.remover.is_removing(record.owner_uuid):
                            item = self._item("removing", record)
                            cleanup = True
                        else:
                            item, cleanup = await self._active(record)
                # Never recursively take the provider lock. Removal first writes
                # admission, then takes that lock for helper/layout mutations.
                if cleanup:
                    result = await self.remover.remove(initial.owner_uuid, OperationContext(
                        operation_id=OperationId(uuid4()), session_generation=0, endpoint_generation=0,
                    ))
                    if isinstance(result, Err):
                        item = replace(item, diagnostic=result.error)
            except Exception:
                item = self._item("cleanup_required", initial, diagnostic(ErrorCode.ownership_mismatch, initial.receiver_id))
            if item.kind in {"cleanup_required", "orphan"}:
                self.remover.emit("cleanup_required", item.record, item.diagnostic)
            items.append(item)
        return tuple(items)

    @staticmethod
    def _item(kind, record, error=None, handle=None):
        return RecoveryItem(kind=kind, record=record, handle=handle, diagnostic=error)

    async def _active(self, record: OwnershipRecord):
        helper_state, identity = self.remover.helper_evidence(record)
        if helper_state == "unknown":
            return self._uncertain(record, ErrorCode.ownership_mismatch)
        observed = await self.remover.observe()
        if isinstance(observed, Err):
            return self._item("cleanup_required", record, observed.error), False
        output_state, candidate = self.remover.output_evidence(record, *observed.value)
        if output_state == "unknown":
            return self._uncertain(record, ErrorCode.ownership_mismatch)
        # Owner environment covers the spawn-to-PID-persistence crash window.
        # It authorizes a process identity, not a compositor output association.
        if record.helper is None and helper_state == "live":
            record = replace(record, stage="helper_started", helper=identity, last_configuration_step="helper_started")
            result = await self._write_active(record)
            if isinstance(result, Err):
                return self._item("cleanup_required", record, result.error), False
            self.remover.emit("helper_started", record)
        if helper_state == "dead":
            if output_state == "absent" and record.stage == "reserved":
                async with self.remover.journal(record.owner_uuid):
                    active = await self._check_active(record)
                    if isinstance(active, Err):
                        return self._item("cleanup_required", record, active.error), False
                    final = await self.remover.observe()
                    if isinstance(final, Err):
                        return self._item("cleanup_required", record, final.error), False
                    if self.remover.is_removing(record.owner_uuid) or self.remover.helper_evidence(record)[0] != "dead" or self.remover.output_evidence(record, *final.value)[0] != "absent":
                        return self._uncertain(record, ErrorCode.ownership_mismatch)
                    deleted = await self.store.delete(record.owner_uuid)
                    if isinstance(deleted, Err):
                        return self._item("cleanup_required", record, deleted.error), False
                return self._item("discarded", record), False
            if candidate is not None and record.helper is not None and record.connector is not None:
                return self._item("orphan", record, diagnostic(ErrorCode.ownership_mismatch, record.receiver_id)), False
            return self._uncertain(record, ErrorCode.ownership_mismatch)
        if output_state == "absent":
            waited = await self.observer.wait_for(
                lambda outputs: self._new_candidates(record, outputs),
                self.clock.now() + REMOVAL_TIMEOUT,
            )
            if isinstance(waited, Err):
                if waited.error.code != ErrorCode.cleanup_timeout:
                    return self._item("cleanup_required", record, waited.error), False
                final = await self.remover.observe()
                if isinstance(final, Err):
                    return self._item("cleanup_required", record, final.error), False
                # An ambiguous new output is never an excuse to stop a helper.
                if self.remover.output_evidence(record, *final.value)[0] != "absent":
                    return self._uncertain(record, ErrorCode.ownership_mismatch)
                if self.remover.helper_evidence(record) != ("live", record.helper):
                    return self._uncertain(record, ErrorCode.ownership_mismatch)
                return self._item("cleanup_required", record, diagnostic(ErrorCode.helper_failed, record.receiver_id)), True
            observed = await self.remover.observe()
            if isinstance(observed, Err):
                return self._item("cleanup_required", record, observed.error), False
            output_state, candidate = self.remover.output_evidence(record, *observed.value)
            if output_state != "exact":
                return self._uncertain(record, ErrorCode.ownership_mismatch)
        verified = await self._verify(record, candidate)
        if isinstance(verified, Err):
            return self._item("cleanup_required", record, verified.error), False
        if record.stage in {"reserved", "helper_started"}:
            record = replace(record, stage="output_observed", connector=candidate.connector,
                             output_id=candidate.output_id, observed_size=candidate.size,
                             last_configuration_step="output_observed")
            written = await self._write_active(record)
            if isinstance(written, Err):
                return self._item("cleanup_required", record, written.error), False
            self.remover.emit("output_observed", record)
        # No command before its output association and removal fence are checked.
        verified = await self._verify(record, candidate)
        if isinstance(verified, Err):
            return self._item("cleanup_required", record, verified.error), False
        if not self._configured(record, candidate):
            configured = await self.observer.configure(candidate.output_id, {
                "enabled": True, "size": record.requested_size, "position": "0,0",
            })
            if isinstance(configured, Err):
                return self._item("cleanup_required", record, configured.error), False
        final = await self.remover.observe()
        if isinstance(final, Err):
            return self._item("cleanup_required", record, final.error), False
        state, actual = self.remover.output_evidence(record, *final.value)
        if state != "exact" or not self._configured(record, actual):
            return self._uncertain(record, ErrorCode.configuration_failed)
        verified = await self._verify(record, actual)
        if isinstance(verified, Err):
            return self._item("cleanup_required", record, verified.error), False
        applied = {c: dict(p) for c, p in record.last_applied.items()}
        applied.setdefault(actual.connector, {}).update({
            "enabled": True, "width": record.requested_size.width, "height": record.requested_size.height,
            "size": f"{record.requested_size.width}x{record.requested_size.height}",
            "x": 0, "y": 0, "position": "0,0",
        })
        record = replace(record, stage="published", observed_size=actual.size, last_applied=applied,
                         last_configuration_step="published")
        written = await self._write_active(record)
        if isinstance(written, Err):
            return self._item("cleanup_required", record, written.error), False
        # Persistence may suspend. Re-prove the resource before exposing it.
        verified = await self._verify(record, actual)
        if isinstance(verified, Err):
            return self._item("cleanup_required", record, verified.error), False
        handle = DisplayHandle(owner_uuid=record.owner_uuid, helper=record.helper, source=VirtualOutputSource(
            connector=actual.connector, kscreen_output_id=actual.output_id, size=actual.size,
        ))
        self.remover.emit("published", record, handle=handle)
        # Subscriber code is synchronous but may close admission itself.
        if self.remover.is_removing(record.owner_uuid):
            return self._uncertain(record, ErrorCode.stale_event)
        return self._item("recovering", record, handle=handle), False

    @staticmethod
    def _new_candidates(record, outputs):
        if record.connector is not None:
            return any(o.connector == record.connector or o.output_id == record.output_id for o in outputs)
        baseline_ids = {p.get("id") for p in record.baseline.outputs.values()}
        return any(o.connector not in record.baseline.outputs or o.output_id not in baseline_ids for o in outputs)

    @staticmethod
    def _configured(record, output):
        return output is not None and output.enabled and output.size == record.requested_size and output.position == (0, 0)

    def _uncertain(self, record, code):
        return self._item("cleanup_required", record, diagnostic(code, record.receiver_id)), False

    async def _check_active(self, record):
        if self.remover.is_removing(record.owner_uuid):
            return _failure(ErrorCode.stale_event, record)
        found = await self.remover.lookup(record.owner_uuid)
        if isinstance(found, Err):
            return found
        if found.value is None or found.value.desired_state != "active" or found.value.stage == "removing" or self.remover.is_removing(record.owner_uuid):
            return _failure(ErrorCode.stale_event, record)
        return Ok(value=None)

    async def _write_active(self, record):
        async with self.remover.journal(record.owner_uuid):
            active = await self._check_active(record)
            if isinstance(active, Err):
                return active
            if self.remover.helper_evidence(record) != ("live", record.helper):
                return _failure(ErrorCode.ownership_mismatch, record)
            result = await self.store.write(record)
            if isinstance(result, Err):
                return result
            self.remover.track(record)
            if self.remover.is_removing(record.owner_uuid):
                return _failure(ErrorCode.stale_event, record)
            return Ok(value=None)

    async def _verify(self, record, candidate):
        active = await self._check_active(record)
        if isinstance(active, Err):
            return active
        verified = await self.remover.verify_output(record, candidate, verifier=self._output_verifier)
        if isinstance(verified, Err):
            return verified
        # Association verification can itself suspend; refresh both the full
        # compositor snapshot and the process identity before the next mutation.
        observed = await self.remover.observe()
        if isinstance(observed, Err):
            return observed
        state, current = self.remover.output_evidence(record, *observed.value)
        if state != "exact" or current != candidate:
            return _failure(ErrorCode.ownership_mismatch, record)
        active = await self._check_active(record)
        if isinstance(active, Err):
            return active
        if self.remover.helper_evidence(record) != ("live", record.helper):
            return _failure(ErrorCode.ownership_mismatch, record)
        return Ok(value=None)


__all__ = ["DisplayRecovery"]
