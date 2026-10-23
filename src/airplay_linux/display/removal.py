"""Durable removal admission, exact owned teardown, and conditional layout repair.

The frozen DisplayPort has no transport acknowledgement argument. A composition
must inject ``transport_released(record, context)`` from its transport owner; an
Ok(None) is that caller's confirmation, checked for every destructive attempt.
Neither a removing record nor a missing lease in a context proves release.

KScreenObserver deliberately exposes configuration only. There is no documented
output deletion command in that interface, so a helperless orphan is retained
with manual cleanup instructions even after confirmation.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kscreen import KScreenObserver, OutputIdentity, OutputInfo
from airplay_linux.domain.messages import DisplayEvent
from airplay_linux.domain.operations import OperationContext, OwnerId, ProcessIdentity
from airplay_linux.domain.ports import Clock, OwnershipStore, ProcessPort
from airplay_linux.domain.records import LayoutSnapshot, OwnershipRecord
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result, Size
from airplay_linux.process import IdentityEvidence, IdentityState, OwnedProcessScan, read_identity_evidence

from .locking import CreationLock

REMOVAL_TIMEOUT = 5.0
OutputVerifier = Callable[[OwnerId, str, ProcessIdentity, OutputInfo], Awaitable[Result[None]]]
TransportRelease = Callable[[OwnershipRecord, OperationContext], Awaitable[Result[None]]]


def _failure(code: ErrorCode, record: OwnershipRecord | None = None) -> Err:
    return Err(error=diagnostic(code, record.receiver_id if record is not None else None))


def _identity_from_layout(
    connector: str, properties: Mapping[str, str | int | bool],
) -> OutputIdentity | None:
    output_id = properties.get("id")
    width, height = properties.get("width"), properties.get("height")
    x, y = properties.get("x"), properties.get("y")
    if (
        type(output_id) is not str
        or type(width) is not int
        or type(height) is not int
        or type(x) is not int
        or type(y) is not int
    ):
        return None
    try:
        size = Size(width=width, height=height)
    except (TypeError, ValueError):
        return None
    return OutputIdentity(
        output_id=output_id,
        connector=connector,
        size=size,
        position=(x, y),
    )


def eligible_restore(
    baseline: LayoutSnapshot,
    last_applied: Mapping[str, Mapping[str, str | int | bool]],
    current: LayoutSnapshot,
) -> Mapping[str, Mapping[str, str | int | bool]]:
    """Return baseline values for touched properties that still belong to us.

    A created/deleted connector has no baseline/current intersection. An output
    id change fences a surviving connector too. Equality is type-sensitive:
    bools are not layout integers. Untouched, unknown and user-modified values
    confer no restoration permission.
    """
    restore = {}
    for connector, applied in last_applied.items():
        before = baseline.outputs.get(connector)
        now = current.outputs.get(connector)
        if before is None or now is None:
            continue
        if ("id" in before or "id" in now) and before.get("id") != now.get("id"):
            continue
        values = {
            name: before[name] for name, value in applied.items()
            if name != "id" and name in before and name in now
            and type(now[name]) is type(value) and now[name] == value
            and (type(before[name]) is not type(value) or before[name] != value)
        }
        if values:
            restore[connector] = values
    return restore


class _Subscription:
    def __init__(self, callbacks, callback):
        self._callbacks, self._callback = callbacks, callback

    def close(self) -> None:
        if self._callback in self._callbacks:
            self._callbacks.remove(self._callback)


class DisplayRemover:
    """A cleanup attempt never erases uncertain process/compositor evidence."""
    def __init__(
        self, helper: VirtualHelper, observer: KScreenObserver, ownership_store: OwnershipStore,
        creation_lock: CreationLock, clock: Clock, *,
        process: ProcessPort | None = None,
        output_verifier: OutputVerifier | None = None,
        transport_released: TransportRelease | None = None,
        identity_probe: Callable[[int], IdentityEvidence] | None = None,
    ) -> None:
        self.helper, self.observer = helper, observer
        self.store, self.creation_lock, self.clock = ownership_store, creation_lock, clock
        self._process, self._output_verifier = process, output_verifier
        self._transport_released = transport_released
        self._identity_probe = identity_probe
        self._projections: dict[OwnerId, DisplayEvent] = {}
        self._journals: dict[OwnerId, asyncio.Lock] = {}
        self._removing: set[OwnerId] = set()
        self._removed: set[OwnerId] = set()
        self._known: dict[OwnerId, OwnershipRecord] = {}
        self._callbacks: list[Callable[[DisplayEvent], None]] = []
        self._background: set[asyncio.Task] = set()
        self._process_subscription = process.subscribe(self._process_exit) if process is not None else None

    @property
    def known_records(self) -> tuple[OwnershipRecord, ...]:
        return tuple(self._known.values())

    def track(self, record: OwnershipRecord) -> None:
        self._known[record.owner_uuid] = record
        if record.desired_state == "removing" or record.stage == "removing":
            self.fence(record.owner_uuid)

    def fence(self, owner_uuid: OwnerId) -> None:
        """Close active-publication admission before the first suspension."""
        self._removing.add(owner_uuid)

    def is_removing(self, owner_uuid: OwnerId) -> bool:
        return owner_uuid in self._removing

    def journal(self, owner_uuid: OwnerId) -> asyncio.Lock:
        """Recovery and removal serialize active writes/removal admission here."""
        return self._journals.setdefault(owner_uuid, asyncio.Lock())

    def subscribe(self, callback: Callable[[DisplayEvent], None]) -> _Subscription:
        self._callbacks.append(callback)
        return _Subscription(self._callbacks, callback)

    def emit(self, kind: str, record: OwnershipRecord, error: Diagnostic | None = None, handle=None) -> None:
        event = DisplayEvent(
            kind=kind, receiver_id=record.receiver_id,
            creation_operation_id=record.creation_operation_id, owner_uuid=record.owner_uuid,
            connector=record.connector, output_id=record.output_id, helper=record.helper,
            handle=handle, diagnostic=error,
        )
        for callback in tuple(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue

    async def lookup(self, owner_uuid: OwnerId) -> Result[OwnershipRecord | None]:
        try:
            records = await self.store.all()
            if isinstance(records, Err):
                return records
            matches = [r for r in records.value if r.owner_uuid == owner_uuid]
            if len(matches) > 1:
                return _failure(ErrorCode.ownership_mismatch)
            if not matches:
                return Ok(value=None)
            self.track(matches[0])
            return Ok(value=matches[0])
        except Exception:
            return _failure(ErrorCode.persistence_failed)

    def scan(self, record: OwnershipRecord) -> OwnedProcessScan:
        try:
            result = self.helper.owned_scan(record.owner_uuid, record.runtime_directory)
            if type(result) is OwnedProcessScan:
                return result
        except Exception:
            pass
        return OwnedProcessScan(matches=(), complete=False)

    def helper_evidence(self, record: OwnershipRecord) -> tuple[str, ProcessIdentity | None]:
        """Distinguish exact live, proved dead, and unresolvable/reused identities."""
        scan = self.scan(record)
        if not scan.complete or len(scan.matches) > 1:
            return "unknown", None
        if record.helper is None:
            return ("live", scan.matches[0]) if scan.matches else ("dead", None)
        if scan.matches == (record.helper,):
            return "live", record.helper
        if scan.matches or self._process is None:
            return "unknown", None
        try:
            current = self._process.identity(record.helper.pid)
        except Exception:
            return "unknown", None
        # identity() collapses inaccessible and absent. The fake environment
        # table is a complete process world; production additionally needs an
        # explicit procfs ENOENT receipt, just like VirtualHelper.stop().
        if current is not None:
            return "unknown", None
        if self._identity_probe is not None or not callable(getattr(self._process, "environments", None)):
            try:
                evidence = (self._identity_probe or read_identity_evidence)(record.helper.pid)
            except Exception:
                return "unknown", None
            if type(evidence) is not IdentityEvidence or evidence.state is not IdentityState.absent:
                return "unknown", None
        return "dead", None

    async def observe(self) -> Result[tuple[tuple[OutputInfo, ...], LayoutSnapshot]]:
        try:
            layout = await self.observer.snapshot()
            if isinstance(layout, Err):
                return layout
            observed = await self.observer.observation()
            if isinstance(observed, Err):
                return observed
            outputs, latest = observed.value
            if type(layout.value) is not LayoutSnapshot or type(latest) is not LayoutSnapshot or type(outputs) is not tuple:
                return _failure(ErrorCode.configuration_failed)
            # Retain complete evidence from *both* reads. Projection can omit an
            # unreadable output, so it cannot make a contradictory window safe.
            # A fresh coherent observation is needed before any destructive act.
            if layout.value.outputs != latest.outputs:
                return _failure(ErrorCode.ownership_mismatch)
            return Ok(value=(outputs, latest))
        except Exception:
            return _failure(ErrorCode.configuration_failed)

    @staticmethod
    def output_evidence(record: OwnershipRecord, outputs, layout: LayoutSnapshot):
        """Classify full compositor evidence, including outputs without geometry."""
        if any(type(o) is not OutputInfo for o in outputs):
            return "unknown", None
        if len({o.output_id for o in outputs}) != len(outputs) or len({o.connector for o in outputs}) != len(outputs):
            return "unknown", None
        if (record.connector is None) != (record.output_id is None):
            return "unknown", None
        if record.connector is not None:
            same_connector = layout.outputs.get(record.connector)
            same_ids = [c for c, p in layout.outputs.items() if p.get("id") == record.output_id]
            candidates = [
                o for o in outputs
                if o.connector == record.connector or o.output_id == record.output_id
            ]
            if same_connector is None and not same_ids:
                # The two adapter reads are separate observations. A matching
                # output in the second one contradicts an absent full snapshot;
                # absence is not safe until both observations agree.
                return ("unknown", None) if candidates else ("absent", None)
            if same_connector is None or same_connector.get("id") != record.output_id or same_ids != [record.connector]:
                return "unknown", None
            candidates = [
                o for o in outputs
                if o.connector == record.connector and o.output_id == record.output_id
            ]
        else:
            # The baseline is complete; outputs() can omit unknown geometry.
            baseline_connectors = set(record.baseline.outputs)
            baseline_ids = {p.get("id") for p in record.baseline.outputs.values()}
            new = {
                c: p for c, p in layout.outputs.items()
                if c not in baseline_connectors or p.get("id") not in baseline_ids
            }
            new_candidates = [
                o for o in outputs
                if o.connector not in baseline_connectors or o.output_id not in baseline_ids
            ]
            if not new and not new_candidates:
                return "absent", None
            # Any disagreement between the full snapshot and the projected
            # output list is uncertainty. In particular, a newly visible output
            # must not be treated as absent merely because it arrived between
            # the observer's two reads.
            if len(new) != 1 or len(new_candidates) != 1:
                return "unknown", None
            connector, properties = next(iter(new.items()))
            candidate = new_candidates[0]
            if candidate.connector != connector or candidate.output_id != properties.get("id"):
                return "unknown", None
            candidates = [candidate]
        if len(candidates) != 1 or candidates[0].virtual is not True:
            return "unknown", None
        candidate = candidates[0]
        if candidate.connector in record.baseline.outputs or any(
            p.get("id") == candidate.output_id for p in record.baseline.outputs.values()
        ):
            return "unknown", None
        return "exact", candidate

    async def verify_output(self, record: OwnershipRecord, candidate: OutputInfo, *, verifier=None) -> Result[None]:
        authority = verifier if verifier is not None else self._output_verifier
        if authority is None:
            return _failure(ErrorCode.unsupported_operation, record)
        if self.helper_evidence(record) != ("live", record.helper):
            return _failure(ErrorCode.ownership_mismatch, record)
        try:
            records = await self.store.all()
            if isinstance(records, Err):
                return records
            if any(r.owner_uuid != record.owner_uuid and (
                r.connector == candidate.connector or r.output_id == candidate.output_id
            ) for r in records.value):
                return _failure(ErrorCode.ownership_mismatch, record)
            approved = await authority(record.owner_uuid, record.runtime_directory, record.helper, candidate)
        except Exception:
            return _failure(ErrorCode.ownership_mismatch, record)
        if self.helper_evidence(record) != ("live", record.helper):
            return _failure(ErrorCode.ownership_mismatch, record)
        if type(approved) is not Ok or approved.value is not None:
            return approved if isinstance(approved, Err) else _failure(ErrorCode.ownership_mismatch, record)
        return Ok(value=None)

    async def mark_removing(self, owner_uuid: OwnerId) -> Result[None]:
        if type(owner_uuid) is not UUID:
            return _failure(ErrorCode.invalid_input)
        self.fence(owner_uuid)
        async with self.journal(owner_uuid):
            found = await self.lookup(owner_uuid)
            if isinstance(found, Err):
                return found
            if found.value is None:
                return Ok(value=None) if owner_uuid in self._removed else _failure(ErrorCode.ownership_mismatch)
            record = replace(found.value, desired_state="removing", stage="removing", last_configuration_step="removing")
            try:
                # Rewrite even an already-visible removing record: a previous
                # failed directory fsync is not a durable admission receipt.
                written = await self.store.write(record)
            except Exception:
                return _failure(ErrorCode.persistence_failed, record)
            if isinstance(written, Err):
                self.emit("cleanup_required", record, written.error)
                return written
            self.track(record)
        # DisplayEvent has no 'removing' member. CleanupRequired is the typed
        # projection of a resource whose release is still unconfirmed.
        self.emit("cleanup_required", record)
        return Ok(value=None)

    async def remove(self, owner_uuid: OwnerId, context: OperationContext) -> Result[None]:
        if type(context) is not OperationContext:
            return _failure(ErrorCode.invalid_input)
        admitted = await self.mark_removing(owner_uuid)
        if isinstance(admitted, Err):
            return admitted
        if owner_uuid in self._removed:
            return Ok(value=None)
        # Admission is outside this lock, so recovery/configuration cannot delay
        # fencing. The lock serializes actual compositor mutation with creation.
        async with self.creation_lock.acquire():
            found = await self.lookup(owner_uuid)
            if isinstance(found, Err):
                return found
            if found.value is None:
                return Ok(value=None) if owner_uuid in self._removed else _failure(ErrorCode.ownership_mismatch)
            record = found.value
            try:
                result = await self._remove_record(record, context)
            except Exception:
                result = _failure(ErrorCode.cleanup_timeout, record)
            if isinstance(result, Err):
                self.emit("cleanup_required", record, result.error)
            return result

    async def _remove_record(self, record: OwnershipRecord, context: OperationContext) -> Result[None]:
        if record.desired_state != "removing" or record.stage != "removing":
            return _failure(ErrorCode.ownership_mismatch, record)
        if self._transport_released is None:
            return _failure(ErrorCode.cleanup_timeout, record)
        async with asyncio.timeout(REMOVAL_TIMEOUT):
            release = await self._transport_released(record, context)
        if type(release) is not Ok or release.value is not None:
            return release if isinstance(release, Err) else _failure(ErrorCode.cleanup_timeout, record)
        evidence = self.helper_evidence(record)
        if evidence[0] == "unknown":
            return _failure(ErrorCode.ownership_mismatch, record)
        if record.helper is None and evidence[0] == "live":
            record = replace(record, helper=evidence[1])
            async with self.journal(record.owner_uuid):
                written = await self.store.write(record)
                if isinstance(written, Err):
                    return written
                self.track(record)
        observed = await self.observe()
        if isinstance(observed, Err):
            return observed
        outputs, layout = observed.value
        output_state, candidate = self.output_evidence(record, outputs, layout)
        if output_state == "unknown":
            return _failure(ErrorCode.ownership_mismatch, record)
        if candidate is not None:
            if evidence[0] != "live":
                return self._manual_orphan(record)
            verified = await self.verify_output(record, candidate)
            if isinstance(verified, Err):
                return verified
            # Persist launch-window output evidence before any signal.
            if record.connector is None:
                record = replace(record, connector=candidate.connector, output_id=candidate.output_id,
                                 observed_size=candidate.size)
                async with self.journal(record.owner_uuid):
                    written = await self.store.write(record)
                    if isinstance(written, Err):
                        return written
                    self.track(record)
                verified = await self.verify_output(record, candidate)
                if isinstance(verified, Err):
                    return verified
        # Association and record writes may suspend. Revalidate the complete
        # compositor identity at the signal boundary, not just the process PID.
        latest = await self.observe()
        if isinstance(latest, Err):
            return latest
        state, current_output = self.output_evidence(record, *latest.value)
        if state != output_state or (candidate is not None and (
            current_output is None or current_output.connector != candidate.connector
            or current_output.output_id != candidate.output_id
        )):
            return _failure(ErrorCode.ownership_mismatch, record)
        if evidence[0] == "live":
            if self.helper_evidence(record) != ("live", record.helper):
                return _failure(ErrorCode.ownership_mismatch, record)
            stopped = await self.helper.stop(record.helper, record.owner_uuid, record.runtime_directory, REMOVAL_TIMEOUT)
            if isinstance(stopped, Err):
                return stopped
        if self.helper_evidence(record)[0] != "dead":
            return _failure(ErrorCode.cleanup_timeout, record)
        # A filtered output list is insufficient: the full snapshot below also
        # checks the connector/id when geometry became unreadable or was reused.
        disappeared = await self.observer.wait_for(
            lambda current: not any(
                o.connector == record.connector or o.output_id == record.output_id for o in current
            ) if record.connector is not None else all(
                o.connector in record.baseline.outputs
                and record.baseline.outputs[o.connector].get("id") == o.output_id for o in current
            ), self.clock.now() + REMOVAL_TIMEOUT,
        )
        if isinstance(disappeared, Err):
            return disappeared
        current = await self.observer.snapshot()
        if isinstance(current, Err):
            return current
        if not self._gone(record, current.value):
            return _failure(ErrorCode.cleanup_timeout, record)
        restored = await self._restore(record, current.value)
        if isinstance(restored, Err):
            return restored
        async with self.journal(record.owner_uuid):
            # Recheck after every observation/configuration await, immediately at
            # the deletion boundary. A stale success cannot erase a late helper.
            if self.helper_evidence(record)[0] != "dead":
                return _failure(ErrorCode.ownership_mismatch, record)
            final = await self.observer.snapshot()
            if isinstance(final, Err) or not self._gone(record, final.value):
                return _failure(ErrorCode.cleanup_timeout, record)
            if not self._layout_restored(record, final.value):
                return _failure(ErrorCode.configuration_failed, record)
            if self.helper_evidence(record)[0] != "dead":
                return _failure(ErrorCode.ownership_mismatch, record)
            deleted = await self.store.delete(record.owner_uuid)
            if isinstance(deleted, Err):
                return deleted
            self._removed.add(record.owner_uuid)
            self._known.pop(record.owner_uuid, None)
            self._projections.pop(record.owner_uuid, None)
        self.emit("removed", record)
        return Ok(value=None)

    @staticmethod
    def _gone(record: OwnershipRecord, layout: LayoutSnapshot) -> bool:
        if type(layout) is not LayoutSnapshot:
            return False
        if record.connector is not None:
            return record.connector not in layout.outputs and all(
                p.get("id") != record.output_id for p in layout.outputs.values()
            )
        return all(c in record.baseline.outputs and p.get("id") == record.baseline.outputs[c].get("id")
                   for c, p in layout.outputs.items())

    @staticmethod
    def _layout_restored(record: OwnershipRecord, layout: LayoutSnapshot) -> bool:
        """Deletion needs baseline equality, not merely no eligible commands.

        User-modified, missing, or reassigned properties are left alone but still
        require cleanup evidence. Created outputs have no pre-change properties
        to restore; their disappearance is proved separately by _gone.
        """
        for connector, applied in record.last_applied.items():
            before = record.baseline.outputs.get(connector)
            if connector == record.connector or before is None or not applied:
                continue
            actual = layout.outputs.get(connector)
            if actual is None or ("id" in before and actual.get("id") != before["id"]):
                return False
            if any(
                name not in before or name not in actual
                or type(actual[name]) is not type(before[name]) or actual[name] != before[name]
                for name in applied if name != "id"
            ):
                return False
        return True

    async def _restore(self, record: OwnershipRecord, current: LayoutSnapshot) -> Result[None]:
        eligible = eligible_restore(record.baseline, record.last_applied, current)
        for connector in eligible:
            if connector == record.connector:
                continue
            # Another receiver's still-owned output is never part of this
            # owner's restoration, even if a stale journal mentions it.
            owned = await self.store.all()
            if isinstance(owned, Err):
                return owned
            if any(r.owner_uuid != record.owner_uuid and r.connector == connector for r in owned.value):
                continue
            latest = await self.observer.snapshot()
            if isinstance(latest, Err):
                return latest
            values = eligible_restore(record.baseline, record.last_applied, latest.value).get(connector, {})
            if not values:
                # The property permission was lost while the guarded read was
                # in flight. Preserve the user's newer value and keep cleanup
                # unconfirmed so the caller can report that restoration did not
                # occur.
                return _failure(ErrorCode.configuration_failed, record)
            properties = latest.value.outputs[connector]
            target = _identity_from_layout(connector, properties)
            if target is None:
                return _failure(ErrorCode.configuration_failed, record)
            command = {k: v for k, v in values.items() if k in {"enabled", "scale", "mode", "size", "position"}}
            # x/y and width/height are flattened snapshot aliases. Restore a
            # composite command only with the latest untouched companion value.
            if "x" in values or "y" in values:
                x, y = values.get("x", properties.get("x")), values.get("y", properties.get("y"))
                if type(x) is not int or type(y) is not int:
                    return _failure(ErrorCode.configuration_failed, record)
                command["position"] = f"{x},{y}"
            if "width" in values or "height" in values:
                width = values.get("width", properties.get("width"))
                height = values.get("height", properties.get("height"))
                if type(width) is not int or type(height) is not int:
                    return _failure(ErrorCode.configuration_failed, record)
                command["size"] = f"{width}x{height}"
            if not command:
                return _failure(ErrorCode.configuration_failed, record)
            # Freeze all known properties from the eligibility observation.
            # The locked configuration read must still match, including mode,
            # scale and the companions a composite command can overwrite.
            configured = await self.observer.configure(target, command, expected=properties)
            if isinstance(configured, Err):
                return configured
            readback = await self.observer.snapshot()
            if isinstance(readback, Err):
                return readback
            actual = readback.value.outputs.get(connector, {})
            if actual.get("id") != target.output_id or any(actual.get(k) != v for k, v in values.items()):
                return _failure(ErrorCode.configuration_failed, record)
        return Ok(value=None)

    @staticmethod
    def _manual_orphan(record: OwnershipRecord) -> Err:
        base = diagnostic(ErrorCode.ownership_mismatch, record.receiver_id)
        return Err(error=replace(base, message=(
            "This orphan has no verified live helper. Keep it for manual cleanup in KDE Display Configuration; "
            "then retry cleanup to confirm disappearance."
        )))

    async def approve_orphan(self, owner_uuid: OwnerId, confirmed: bool, context: OperationContext) -> Result[None]:
        if type(owner_uuid) is not UUID or type(confirmed) is not bool or type(context) is not OperationContext:
            return _failure(ErrorCode.invalid_input)
        if not confirmed:
            return Ok(value=None)
        async with self.creation_lock.acquire():
            found = await self.lookup(owner_uuid)
            if isinstance(found, Err) or found.value is None:
                return found if isinstance(found, Err) else _failure(ErrorCode.ownership_mismatch)
            record = found.value
            if record.connector is None or record.output_id is None:
                return _failure(ErrorCode.ownership_mismatch, record)
            observed = await self.observe()
            if isinstance(observed, Err):
                return observed
            state, _ = self.output_evidence(record, *observed.value)
            if state != "exact" or self.helper_evidence(record)[0] != "dead":
                return _failure(ErrorCode.ownership_mismatch, record)
            marked = await self.mark_removing(owner_uuid)
            if isinstance(marked, Err):
                return marked
            # No observer method promises output deletion. Never translate
            # confirmation into a fabricated command or a mere disable.
            error = self._manual_orphan(record)
            self.emit("cleanup_required", record, error.error)
            return error

    def track_event(self, event: DisplayEvent) -> None:
        """Retain creation identity even before its projected record is loaded."""
        if event.kind in {"reserved", "helper_started", "output_observed", "published"}:
            self._projections[event.owner_uuid] = replace(event, handle=None)

    def _process_exit(self, event) -> None:
        for record in self.known_records:
            if record.helper == event.identity and not self.is_removing(record.owner_uuid):
                self.invalidate(record, "helper_exit")
        for projection in tuple(self._projections.values()):
            if projection.helper == event.identity and not self.is_removing(projection.owner_uuid):
                self.invalidate_event(projection, "helper_exit")

    def invalidate(self, record: OwnershipRecord, kind: str) -> None:
        self.invalidate_event(DisplayEvent(
            kind="cleanup_required", receiver_id=record.receiver_id,
            creation_operation_id=record.creation_operation_id, owner_uuid=record.owner_uuid,
            connector=record.connector, output_id=record.output_id, helper=record.helper,
            handle=None, diagnostic=None,
        ), kind)

    def invalidate_event(self, event: DisplayEvent, kind: str) -> None:
        self.fence(event.owner_uuid)
        invalid = replace(event, kind=kind, handle=None, diagnostic=diagnostic(ErrorCode.helper_failed, event.receiver_id))
        for callback in tuple(self._callbacks):
            try:
                callback(invalid)
            except Exception:
                continue
        task = asyncio.create_task(self.mark_removing(event.owner_uuid))
        self._background.add(task)
        task.add_done_callback(self._background_done)

    def _background_done(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled():
            task.exception()

    async def flush_events(self) -> None:
        """Await pending durable invalidations before application shutdown."""
        while self._background:
            await asyncio.gather(*tuple(self._background))


__all__ = ["DisplayRemover", "eligible_restore"]
