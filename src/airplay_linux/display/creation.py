"""Transactional creation and publication of one owned virtual display."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from airplay_linux.diagnostics.activity import safe_activity
from airplay_linux.diagnostics.catalog import diagnostic
from airplay_linux.diagnostics import boundary
from airplay_linux.display.helper import HELPER_COMMAND, HelperFeatures
from airplay_linux.display.kscreen import OutputInfo
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    DisplayHandle,
    InstanceId,
    OperationContext,
    OperationId,
    OwnerId,
    ProcessIdentity,
)
from airplay_linux.domain.ports import ActivitySink, Clock, IdFactory, OwnershipStore, Scheduler
from airplay_linux.domain.records import LayoutSnapshot, OwnershipRecord
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Ok,
    Result,
    Size,
    VirtualOutputSource,
)
from airplay_linux.process import OwnedProcessScan

from .locking import CreationLock


CREATION_TIMEOUT = 5.0
HELPER_STOP_TIMEOUT = 5.0


@dataclass(frozen=True, kw_only=True)
class CreationProgress:
    """Safe progress projection for one creation reservation.

    It intentionally contains ownership and layout evidence only.  Receiver
    credentials, helper command output, and endpoint fingerprints never enter
    this event.
    """

    receiver_id: ReceiverId
    creation_operation_id: OperationId
    owner_uuid: OwnerId
    stage: str
    desired_state: str
    helper: ProcessIdentity | None
    connector: str | None
    output_id: str | None
    handle: DisplayHandle | None


@dataclass(eq=False)
class _Creation:
    receiver_id: ReceiverId
    endpoint_fingerprint: str
    size: Size
    context: OperationContext
    owner_uuid: OwnerId | None = None
    record: OwnershipRecord | None = None
    helper: ProcessIdentity | None = None
    output: OutputInfo | None = None
    launch_attempted: bool = False
    cleanup_complete: bool = False
    fenced: bool = False


class _Subscription:
    def __init__(self, callbacks: list[Callable[[CreationProgress], None]], callback):
        self._callbacks = callbacks
        self._callback = callback
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._callbacks.remove(self._callback)
            except ValueError:
                pass


def _failure(code: ErrorCode, receiver_id: ReceiverId | None = None) -> Err:
    return Err(error=diagnostic(code, receiver_id))


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_new_output(
    baseline: tuple[OutputInfo, ...],
    observed: tuple[OutputInfo, ...],
    size: Size,
) -> Result[OutputInfo]:
    """Select exactly one newly appeared, virtual output of ``size``.

    Output identity is established against both connector and compositor output
    id.  A pre-existing virtual connector is therefore never adopted merely
    because it has the requested dimensions.
    """

    if type(size) is not Size or type(baseline) is not tuple or type(observed) is not tuple:
        return _failure(ErrorCode.invalid_input)

    baseline_ids: set[str] = set()
    baseline_connectors: set[str] = set()
    for item in baseline:
        if type(item) is not OutputInfo:
            return _failure(ErrorCode.configuration_failed)
        if item.output_id in baseline_ids or item.connector in baseline_connectors:
            return _failure(ErrorCode.configuration_failed)
        baseline_ids.add(item.output_id)
        baseline_connectors.add(item.connector)
    matches: list[OutputInfo] = []
    seen_ids: set[str] = set()
    seen_connectors: set[str] = set()
    for item in observed:
        if type(item) is not OutputInfo:
            return _failure(ErrorCode.configuration_failed)
        if item.output_id in seen_ids or item.connector in seen_connectors:
            return _failure(ErrorCode.configuration_failed)
        seen_ids.add(item.output_id)
        seen_connectors.add(item.connector)
        if item.output_id in baseline_ids or item.connector in baseline_connectors:
            continue
        if item.virtual is not True or item.size != size:
            continue
        matches.append(item)

    if not baseline_ids.issubset(seen_ids) or not baseline_connectors.issubset(seen_connectors):
        return _failure(ErrorCode.configuration_failed)
    if len(matches) != 1:
        return _failure(ErrorCode.configuration_failed)
    return Ok(value=matches[0])


class DisplayCreator:
    """Create a helper-owned output and publish it only after verified readback."""

    def __init__(
        self,
        helper: Any,
        observer: Any,
        ownership_store: OwnershipStore | None = None,
        creation_lock: CreationLock | None = None,
        runtime_directory: str | Path | None = None,
        app_instance_uuid: InstanceId | UUID | None = None,
        clock: Clock | None = None,
        scheduler: Scheduler | None = None,
        id_factory: IdFactory | None = None,
        activity_sink: ActivitySink | None = None,
        *,
        ownership: OwnershipStore | None = None,
        lock: CreationLock | None = None,
        ids: IdFactory | None = None,
        activity: ActivitySink | None = None,
        output_verifier: Callable[
            [OwnerId, str, ProcessIdentity, OutputInfo], Awaitable[Result[None]]
        ] | None = None,
    ) -> None:
        self._helper = helper
        self._observer = observer
        # The documented helper/observer contract has no process-to-output
        # evidence. A provider must supply a supported association authority;
        # without one, even a new same-size output cannot be published.
        self._output_verifier = output_verifier
        self._ownership = ownership_store if ownership_store is not None else ownership
        self._creation_lock = creation_lock if creation_lock is not None else lock
        self._runtime_directory = str(runtime_directory) if runtime_directory is not None else ""
        self._app_instance_uuid = app_instance_uuid
        self._clock = clock
        self._scheduler = scheduler
        self._ids = id_factory if id_factory is not None else ids
        self._activity = activity_sink if activity_sink is not None else activity
        if (
            self._ownership is None
            or self._creation_lock is None
            or self._clock is None
            or self._scheduler is None
            or self._ids is None
            or type(self._runtime_directory) is not str
            or not self._runtime_directory.startswith("/")
            or type(self._app_instance_uuid) not in (UUID,)
        ):
            raise TypeError("DisplayCreator dependencies are incomplete")

        self._callbacks: list[Callable[[CreationProgress], None]] = []
        self._creations: list[_Creation] = []
        self._teardown_gates: set[ReceiverId] = set()
        self._removing_owners: set[OwnerId] = set()
        self._journals: dict[OwnerId, asyncio.Lock] = {}
        self._background: set[asyncio.Task] = set()

    def subscribe(self, callback: Callable[[CreationProgress], None]) -> _Subscription:
        self._callbacks.append(callback)
        return _Subscription(self._callbacks, callback)

    async def create(
        self,
        receiver_id: ReceiverId,
        endpoint_fingerprint: str,
        size: Size,
        context: OperationContext,
    ) -> Result[DisplayHandle]:
        """Start a fenced transaction and return only after active publication."""

        try:
            canonical_receiver_id = boundary.exact_receiver_id(receiver_id)
        except (TypeError, ValueError):
            return _failure(ErrorCode.invalid_input)
        if canonical_receiver_id is None:
            return _failure(ErrorCode.invalid_input)
        receiver_id = canonical_receiver_id
        if (
            type(endpoint_fingerprint) is not str
            or not endpoint_fingerprint
            or type(size) is not Size
            or type(context) is not OperationContext
        ):
            return _failure(ErrorCode.invalid_input)

        state = _Creation(
            receiver_id=receiver_id,
            endpoint_fingerprint=endpoint_fingerprint,
            size=size,
            context=context,
            fenced=receiver_id in self._teardown_gates,
        )
        self._creations.append(state)
        transaction = asyncio.create_task(self._run_transaction(state))
        self._background.add(transaction)
        transaction.add_done_callback(self._discard_background)
        try:
            return await asyncio.shield(transaction)
        except asyncio.CancelledError:
            state.fenced = True
            # Admission runs independently of a held launch/observation and of
            # the provider lock. Repeated cancellation cannot bypass durability.
            admission = asyncio.create_task(self._mark_removing(state))
            self._background.add(admission)
            admission.add_done_callback(self._discard_background)
            while True:
                try:
                    admitted = await asyncio.shield(admission)
                    break
                except asyncio.CancelledError:
                    continue
            if transaction.done():
                self._schedule_cancelled_result_cleanup(state, transaction)
            if isinstance(admitted, Err):
                return admitted
            raise

    def _discard_background(self, task: asyncio.Task) -> None:
        self._background.discard(task)
        if not task.cancelled():
            try:
                task.exception()
            except (asyncio.CancelledError, Exception):
                pass

    def _schedule_cancelled_result_cleanup(self, state: _Creation, transaction: asyncio.Task) -> None:
        try:
            result = transaction.result()
        except BaseException:
            return
        if isinstance(result, Ok) and isinstance(result.value, DisplayHandle):
            cleanup = asyncio.create_task(self._cleanup(state))
            self._background.add(cleanup)
            cleanup.add_done_callback(self._discard_background)

    def _journal(self, owner_uuid: OwnerId) -> asyncio.Lock:
        journal = self._journals.get(owner_uuid)
        if journal is None:
            journal = asyncio.Lock()
            self._journals[owner_uuid] = journal
        return journal

    def _is_fenced(self, state: _Creation) -> bool:
        return (
            state.fenced
            or state.receiver_id in self._teardown_gates
            or (state.owner_uuid is not None and state.owner_uuid in self._removing_owners)
        )

    async def _run_transaction(self, state: _Creation) -> Result[DisplayHandle]:
        if self._is_fenced(state):
            return _failure(ErrorCode.stale_event, state.receiver_id)

        try:
            async with self._creation_lock.acquire():
                if self._is_fenced(state):
                    return _failure(ErrorCode.stale_event, state.receiver_id)

                if not self._session_is_kde_wayland():
                    return _failure(ErrorCode.dependency_incompatible, state.receiver_id)
                features_result = await self._probe_helper()
                if isinstance(features_result, Err):
                    return features_result
                features = features_result.value

                baseline_result = await self._observer.snapshot()
                if isinstance(baseline_result, Err):
                    return baseline_result
                baseline = baseline_result.value
                if type(baseline) is not LayoutSnapshot:
                    return _failure(ErrorCode.configuration_failed, state.receiver_id)

                baseline_outputs_result = await self._observer.outputs()
                if isinstance(baseline_outputs_result, Err):
                    return baseline_outputs_result
                baseline_outputs = baseline_outputs_result.value
                if type(baseline_outputs) is not tuple:
                    return _failure(ErrorCode.configuration_failed, state.receiver_id)

                existing_result = await self._ownership.all()
                if isinstance(existing_result, Err):
                    return existing_result
                if not features.distinct_outputs and any(
                    record.desired_state == "active" and record.stage != "removing"
                    for record in existing_result.value
                ):
                    return _failure(ErrorCode.unsupported_operation, state.receiver_id)

                if self._is_fenced(state):
                    return _failure(ErrorCode.stale_event, state.receiver_id)

                state.owner_uuid = OwnerId(self._ids.new())
                if any(record.owner_uuid == state.owner_uuid for record in existing_result.value):
                    return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
                record = OwnershipRecord(
                    schema_version=1,
                    owner_uuid=state.owner_uuid,
                    app_instance_uuid=InstanceId(self._app_instance_uuid),
                    runtime_directory=self._runtime_directory,
                    creation_operation_id=state.context.operation_id,
                    receiver_id=state.receiver_id,
                    endpoint_fingerprint=state.endpoint_fingerprint,
                    desired_state="active",
                    stage="reserved",
                    requested_size=state.size,
                    observed_size=None,
                    helper=None,
                    connector=None,
                    output_id=None,
                    created_utc=_utc_now(),
                    last_configuration_step="reserved",
                    baseline=baseline,
                    last_applied={},
                    provider_version=features.version,
                    capability_fingerprint=features.capability_fingerprint,
                )
                written = await self._persist(state, record)
                if isinstance(written, Err):
                    return await self._finish_failure(state, written)
                self._emit_progress(state, record)

                if self._is_fenced(state):
                    return await self._finish_failure(state, _failure(ErrorCode.stale_event, state.receiver_id))

                state.launch_attempted = True
                launch_result = await self._helper.launch(
                    state.owner_uuid, self._runtime_directory, state.size,
                )
                if isinstance(launch_result, Err):
                    return await self._finish_failure(state, launch_result)
                if type(launch_result.value) is not ProcessIdentity:
                    return await self._finish_failure(
                        state, _failure(ErrorCode.helper_failed, state.receiver_id),
                    )
                state.helper = launch_result.value
                helper_record = replace(
                    record,
                    stage="helper_started",
                    helper=state.helper,
                    last_configuration_step="helper_started",
                )
                written = await self._persist(state, helper_record)
                if isinstance(written, Err):
                    return await self._finish_failure(state, written)
                record = helper_record
                self._emit_progress(state, record)

                if self._is_fenced(state):
                    return await self._finish_failure(state, _failure(ErrorCode.stale_event, state.receiver_id))

                observed_result = await self._observe_new_output(
                    baseline_outputs, state.size, baseline,
                )
                if isinstance(observed_result, Err):
                    return await self._finish_failure(state, observed_result)
                selected = observed_result.value
                verified = await self._verify_output(state, selected)
                if isinstance(verified, Err):
                    return await self._finish_failure(state, verified)
                state.output = selected
                observed_record = replace(
                    record,
                    stage="output_observed",
                    observed_size=selected.size,
                    connector=selected.connector,
                    output_id=selected.output_id,
                    last_configuration_step="output_observed",
                )
                written = await self._persist(state, observed_record)
                if isinstance(written, Err):
                    return await self._finish_failure(state, written)
                record = observed_record
                self._emit_progress(state, record)

                if self._is_fenced(state):
                    return await self._finish_failure(state, _failure(ErrorCode.stale_event, state.receiver_id))

                applied: dict[str, dict[str, str | int | bool]] = {selected.connector: {}}
                configuration_steps = (
                    (
                        "mode",
                        {"size": f"{state.size.width}x{state.size.height}"},
                        {
                            "width": state.size.width,
                            "height": state.size.height,
                            "size": f"{state.size.width}x{state.size.height}",
                        },
                    ),
                    ("enabled", {"enabled": True}, {"enabled": True}),
                    ("position", {"position": "0,0"}, {"x": 0, "y": 0, "position": "0,0"}),
                )
                for step, values, applied_values in configuration_steps:
                    if self._is_fenced(state):
                        return await self._finish_failure(
                            state, _failure(ErrorCode.stale_event, state.receiver_id),
                        )
                    verified = await self._verify_output(state, selected)
                    if isinstance(verified, Err):
                        return await self._finish_failure(state, verified)
                    if self._is_fenced(state):
                        return await self._finish_failure(
                            state, _failure(ErrorCode.stale_event, state.receiver_id),
                        )
                    configured = await self._observer.configure(selected, values)
                    if isinstance(configured, Err):
                        return await self._finish_failure(state, configured)
                    if self._is_fenced(state):
                        return await self._finish_failure(
                            state, _failure(ErrorCode.stale_event, state.receiver_id),
                        )
                    applied[selected.connector].update(applied_values)
                    configured_record = replace(
                        record,
                        last_configuration_step=step,
                        last_applied=applied,
                    )
                    written = await self._persist(state, configured_record)
                    if isinstance(written, Err):
                        return await self._finish_failure(state, written)
                    record = configured_record

                if self._is_fenced(state):
                    return await self._finish_failure(
                        state, _failure(ErrorCode.stale_event, state.receiver_id),
                    )
                readback_result = await self._observer.outputs()
                if isinstance(readback_result, Err):
                    return await self._finish_failure(state, readback_result)
                if self._is_fenced(state) or not self._readback_matches(
                    readback_result.value, selected, state.size,
                ):
                    return await self._finish_failure(
                        state, _failure(ErrorCode.configuration_failed, state.receiver_id),
                    )

                if self._is_fenced(state):
                    return await self._finish_failure(
                        state, _failure(ErrorCode.stale_event, state.receiver_id),
                    )

                verified = await self._verify_output(state, selected)
                if isinstance(verified, Err):
                    return await self._finish_failure(state, verified)
                published = await self._publish(state, record)
                if isinstance(published, Err):
                    return await self._finish_failure(state, published)
                if self._is_fenced(state):
                    return await self._finish_failure(
                        state, _failure(ErrorCode.stale_event, state.receiver_id),
                    )
                record = published.value
                handle = DisplayHandle(
                    owner_uuid=state.owner_uuid,
                    source=VirtualOutputSource(
                        connector=selected.connector,
                        kscreen_output_id=selected.output_id,
                        size=state.size,
                    ),
                    helper=state.helper,
                )
                self._emit_progress(state, record, handle)
                return Ok(value=handle)
        except Exception:
            if state.owner_uuid is not None:
                return await self._finish_failure(
                    state, _failure(ErrorCode.helper_failed, state.receiver_id),
                )
            return _failure(ErrorCode.helper_failed, state.receiver_id)
        finally:
            try:
                self._creations.remove(state)
            except ValueError:
                pass

    @staticmethod
    def _session_is_kde_wayland() -> bool:
        session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
        desktops = " ".join(
            value.strip().lower()
            for value in (
                os.environ.get("XDG_CURRENT_DESKTOP", ""),
                os.environ.get("XDG_SESSION_DESKTOP", ""),
            )
            if value.strip()
        )
        if session and session != "wayland":
            return False
        if desktops and "kde" not in desktops:
            return False
        return True

    async def _probe_helper(self) -> Result[HelperFeatures]:
        probe = getattr(self._helper, "probe", None)
        if not callable(probe):
            return Err(error=diagnostic(ErrorCode.dependency_missing, command=HELPER_COMMAND))
        try:
            result = await probe()
        except Exception:
            return _failure(ErrorCode.helper_failed)
        if isinstance(result, Err):
            return result
        if type(result) is not Ok or type(result.value) is not HelperFeatures:
            return _failure(ErrorCode.dependency_incompatible)
        return result

    async def _persist(
        self,
        state: _Creation,
        record: OwnershipRecord,
        *,
        allow_removing: bool = False,
    ) -> Result[None]:
        state.record = record
        try:
            async with self._journal(record.owner_uuid):
                if not allow_removing and record.desired_state == "active":
                    if self._is_fenced(state):
                        return _failure(ErrorCode.stale_event, state.receiver_id)
                    current_result = await self._ownership.all()
                    if isinstance(current_result, Err):
                        return current_result
                    current = next(
                        (
                            item for item in current_result.value
                            if item.owner_uuid == record.owner_uuid
                        ),
                        None,
                    )
                    if current is not None and (
                        current.desired_state == "removing" or current.stage == "removing"
                    ):
                        state.fenced = True
                        self._removing_owners.add(record.owner_uuid)
                        return _failure(ErrorCode.stale_event, state.receiver_id)
                    if self._is_fenced(state):
                        return _failure(ErrorCode.stale_event, state.receiver_id)
                result = await self._ownership.write(record)
        except Exception:
            return _failure(ErrorCode.persistence_failed, state.receiver_id)
        if isinstance(result, Ok):
            state.record = record
        return result

    async def _publish(
        self, state: _Creation, record: OwnershipRecord,
    ) -> Result[OwnershipRecord]:
        assert state.owner_uuid is not None
        async with self._journal(state.owner_uuid):
            if self._is_fenced(state):
                return _failure(ErrorCode.stale_event, state.receiver_id)
            current_result = await self._ownership.all()
            if isinstance(current_result, Err):
                return current_result
            current = next(
                (item for item in current_result.value if item.owner_uuid == state.owner_uuid),
                None,
            )
            if current is not None and (
                current.desired_state == "removing" or current.stage == "removing"
            ):
                state.fenced = True
                self._removing_owners.add(state.owner_uuid)
                return _failure(ErrorCode.stale_event, state.receiver_id)
            if self._is_fenced(state):
                return _failure(ErrorCode.stale_event, state.receiver_id)
            if state.output is None:
                return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
            verified = await self._verify_output(state, state.output)
            if isinstance(verified, Err):
                return verified
            published = replace(
                current or record,
                desired_state="active",
                stage="published",
                last_configuration_step="published",
            )
            written = await self._ownership.write(published)
            if isinstance(written, Err):
                return written
            state.record = published
            if self._is_fenced(state):
                return _failure(ErrorCode.stale_event, state.receiver_id)
            verified = await self._verify_output(state, state.output)
            if isinstance(verified, Err):
                return verified
            return Ok(value=published)

    @staticmethod
    def _absent_from_baseline(candidate: OutputInfo, baseline: LayoutSnapshot) -> bool:
        return candidate.connector not in baseline.outputs and all(
            properties.get("id") != candidate.output_id
            for properties in baseline.outputs.values()
        )

    def _owned_scan(self, state: _Creation) -> OwnedProcessScan:
        scan = getattr(self._helper, "owned_scan", None)
        if not callable(scan) or state.owner_uuid is None:
            return OwnedProcessScan(matches=(), complete=False)
        try:
            result = scan(state.owner_uuid, self._runtime_directory)
        except Exception:
            return OwnedProcessScan(matches=(), complete=False)
        if type(result) is not OwnedProcessScan:
            return OwnedProcessScan(matches=(), complete=False)
        return result

    async def _verify_output(self, state: _Creation, candidate: OutputInfo) -> Result[None]:
        if self._is_fenced(state):
            return _failure(ErrorCode.stale_event, state.receiver_id)
        if state.record is None or not self._absent_from_baseline(candidate, state.record.baseline):
            return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
        scan = self._owned_scan(state)
        if state.helper is None or not scan.complete or scan.matches != (state.helper,):
            return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
        if self._output_verifier is None:
            return _failure(ErrorCode.unsupported_operation, state.receiver_id)
        try:
            result = await self._output_verifier(
                state.owner_uuid, self._runtime_directory, state.helper, candidate,
            )
        except Exception:
            return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
        # Recheck after the authority's await: PID reuse or removal admission
        # during verification cannot authorise a configuration/publication.
        scan = self._owned_scan(state)
        if self._is_fenced(state) or not scan.complete or scan.matches != (state.helper,):
            return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
        if type(result) is not Ok or result.value is not None:
            return result if isinstance(result, Err) else _failure(ErrorCode.ownership_mismatch, state.receiver_id)
        return Ok(value=None)

    async def _observe_new_output(
        self, baseline: tuple[OutputInfo, ...], size: Size, complete_baseline: LayoutSnapshot,
    ) -> Result[OutputInfo]:
        deadline = self._clock.now() + CREATION_TIMEOUT
        try:
            waited = await self._observer.wait_for(
                lambda outputs: isinstance(outputs, tuple)
                and isinstance(select_new_output(baseline, outputs, size), Ok),
                deadline,
            )
        except Exception:
            return _failure(ErrorCode.cleanup_timeout)
        if isinstance(waited, Err):
            return waited
        current_result = await self._observer.outputs()
        if isinstance(current_result, Err):
            return current_result
        selected = select_new_output(baseline, current_result.value, size)
        if isinstance(selected, Err):
            return selected
        if not self._absent_from_baseline(selected.value, complete_baseline):
            return _failure(ErrorCode.ownership_mismatch)
        return selected

    @staticmethod
    def _readback_matches(
        outputs: object, selected: OutputInfo, size: Size,
    ) -> bool:
        if type(outputs) is not tuple:
            return False
        matches = [
            item for item in outputs
            if type(item) is OutputInfo
            and item.output_id == selected.output_id
            and item.connector == selected.connector
        ]
        return (
            len(matches) == 1
            and matches[0].virtual is True
            and matches[0].size == size
            and matches[0].enabled is True
            and matches[0].position == (0, 0)
        )

    async def _finish_failure(self, state: _Creation, failure: Err) -> Result[DisplayHandle]:
        cleanup = await self._cleanup(state)
        if isinstance(cleanup, Err):
            return cleanup
        return failure

    async def _mark_removing(self, state: _Creation) -> Result[None]:
        owner_uuid = state.owner_uuid
        state.fenced = True
        if owner_uuid is None:
            # No reservation exists; the transaction checks the fence before
            # reserving or crossing the launch boundary.
            return Ok(value=None)
        self._removing_owners.add(owner_uuid)
        try:
            async with self._journal(owner_uuid):
                if state.cleanup_complete:
                    return Ok(value=None)
                records = await self._ownership.all()
                if isinstance(records, Err):
                    return records
                current = next((r for r in records.value if r.owner_uuid == owner_uuid), None)
                record = current or state.record
                if record is None:
                    return _failure(ErrorCode.persistence_failed, state.receiver_id)
                removing = replace(
                    record,
                    desired_state="removing",
                    stage="removing",
                    helper=state.helper or record.helper,
                    connector=state.output.connector if state.output is not None else record.connector,
                    output_id=state.output.output_id if state.output is not None else record.output_id,
                    observed_size=state.output.size if state.output is not None else record.observed_size,
                    last_configuration_step="removing",
                )
                written = await self._ownership.write(removing)
                if isinstance(written, Err):
                    return written
                state.record = removing
        except Exception:
            return _failure(ErrorCode.persistence_failed, state.receiver_id)
        self._emit_progress(state, removing)
        return Ok(value=None)

    async def _cleanup(self, state: _Creation) -> Result[None]:
        owner_uuid = state.owner_uuid
        if owner_uuid is None or state.record is None:
            return Ok(value=None)
        written = await self._mark_removing(state)
        if isinstance(written, Err):
            return written
        if state.cleanup_complete:
            return Ok(value=None)

        if state.launch_attempted:
            scan = self._owned_scan(state)
            identities = (state.helper,) if state.helper is not None else scan.matches
            if not identities:
                # A failed launch may have spawned an unprovable child. Even a
                # scan with no owned matches cannot certify that child absent.
                return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
            if state.helper is None:
                if not scan.complete or len(identities) != 1:
                    return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
                state.helper = identities[0]
                written = await self._mark_removing(state)
                if isinstance(written, Err):
                    return written
            stop = getattr(self._helper, "stop", None)
            if not callable(stop):
                return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
            for identity in identities:
                try:
                    stopped = await stop(
                        identity, owner_uuid, self._runtime_directory, HELPER_STOP_TIMEOUT,
                    )
                except Exception:
                    return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
                if isinstance(stopped, Err):
                    return stopped
            scan = self._owned_scan(state)
            if not scan.complete or scan.matches:
                return _failure(ErrorCode.ownership_mismatch, state.receiver_id)

            # Stop is process evidence only. Wait even if observation or the
            # helper_started journal failed before an output identity was saved.
            baseline = state.record.baseline
            if state.output is not None:
                predicate = lambda outputs: not self._contains_output(outputs, state.output)
            else:
                predicate = lambda outputs: type(outputs) is tuple and all(
                    type(item) is OutputInfo
                    and item.connector in baseline.outputs
                    and baseline.outputs[item.connector].get("id") == item.output_id
                    for item in outputs
                )
            try:
                disappeared = await self._observer.wait_for(
                    predicate, self._clock.now() + HELPER_STOP_TIMEOUT,
                )
            except Exception:
                return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
            if isinstance(disappeared, Err):
                return disappeared
            # outputs() omits unknown geometry; only the complete snapshot can
            # prove no unobserved output or reused identity remains.
            current = await self._observer.snapshot()
            if isinstance(current, Err) or type(current.value) is not LayoutSnapshot:
                return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
            if state.output is not None:
                if state.output.connector in current.value.outputs or any(
                    p.get("id") == state.output.output_id for p in current.value.outputs.values()
                ):
                    return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
            elif not self._only_baseline_identities(current.value, baseline):
                return _failure(ErrorCode.cleanup_timeout, state.receiver_id)
        try:
            async with self._journal(owner_uuid):
                # Observation and journal admission can suspend. Process
                # absence must still hold at the evidence deletion boundary.
                if state.launch_attempted:
                    scan = self._owned_scan(state)
                    if not scan.complete or scan.matches:
                        return _failure(ErrorCode.ownership_mismatch, state.receiver_id)
                deleted = await self._ownership.delete(owner_uuid)
                if isinstance(deleted, Ok):
                    state.cleanup_complete = True
                return deleted
        except Exception:
            return _failure(ErrorCode.persistence_failed, state.receiver_id)

    @staticmethod
    def _only_baseline_identities(current: LayoutSnapshot, baseline: LayoutSnapshot) -> bool:
        return all(
            connector in baseline.outputs
            and properties.get("id") == baseline.outputs[connector].get("id")
            for connector, properties in current.outputs.items()
        )

    @staticmethod
    def _contains_output(outputs: object, expected: OutputInfo) -> bool:
        return type(outputs) is tuple and any(
            type(item) is OutputInfo
            and item.output_id == expected.output_id
            and item.connector == expected.connector
            for item in outputs
        )

    async def prepare_teardown(
        self,
        receiver_id: ReceiverId,
        superseded_operation_id: Any,
        context: OperationContext,
    ) -> Result[tuple[OwnerId, ...]]:
        """Close admission before awaiting, then publish removal intent per owner."""

        try:
            canonical_receiver_id = boundary.exact_receiver_id(receiver_id)
        except (TypeError, ValueError):
            return _failure(ErrorCode.invalid_input)
        if canonical_receiver_id is None or type(context) is not OperationContext:
            return _failure(ErrorCode.invalid_input)
        receiver_id = canonical_receiver_id
        self._teardown_gates.add(receiver_id)
        for state in self._creations:
            if state.receiver_id == receiver_id:
                state.fenced = True

        try:
            records_result = await self._ownership.all()
            if isinstance(records_result, Err):
                return records_result
            owners: list[OwnerId] = []
            for record in records_result.value:
                if record.receiver_id != receiver_id:
                    continue
                owners.append(record.owner_uuid)
                self._removing_owners.add(record.owner_uuid)
                removing = replace(
                    record,
                    desired_state="removing",
                    stage="removing",
                    last_configuration_step="removing",
                )
                async with self._journal(record.owner_uuid):
                    current_result = await self._ownership.all()
                    if isinstance(current_result, Err):
                        return current_result
                    current = next(
                        (item for item in current_result.value if item.owner_uuid == record.owner_uuid),
                        record,
                    )
                    if current.desired_state == "removing" and current.stage == "removing":
                        removing = current
                    else:
                        written = await self._ownership.write(removing)
                        if isinstance(written, Err):
                            return written
                self._emit_record_progress(removing)
                for state in self._creations:
                    if state.owner_uuid == record.owner_uuid:
                        state.record = removing
            return Ok(value=tuple(owners))
        finally:
            self._teardown_gates.discard(receiver_id)

    def _emit_record_progress(self, record: OwnershipRecord) -> None:
        event = CreationProgress(
            receiver_id=record.receiver_id,
            creation_operation_id=record.creation_operation_id,
            owner_uuid=record.owner_uuid,
            stage=record.stage,
            desired_state=record.desired_state,
            helper=record.helper,
            connector=record.connector,
            output_id=record.output_id,
            handle=None,
        )
        self._notify(event, record.requested_size)

    def _emit_progress(
        self,
        state: _Creation,
        record: OwnershipRecord,
        handle: DisplayHandle | None = None,
    ) -> None:
        event = CreationProgress(
            receiver_id=state.receiver_id,
            creation_operation_id=state.context.operation_id,
            owner_uuid=record.owner_uuid,
            stage=record.stage,
            desired_state=record.desired_state,
            helper=record.helper,
            connector=record.connector,
            output_id=record.output_id,
            handle=handle,
        )
        self._notify(event, record.requested_size)

    def _notify(
        self,
        event: CreationProgress,
        resolution_size: Size | None = None,
    ) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback(event)
            except Exception:
                continue
        if self._activity is None:
            return
        fields: dict[str, str | int | bool] = {
            "reservation_stage": event.stage,
        }
        if event.connector is not None:
            fields["connector"] = event.connector
        if event.output_id is not None:
            fields["output_id"] = event.output_id
        if resolution_size is not None:
            fields["resolution"] = f"{resolution_size.width}x{resolution_size.height}"
        try:
            activity = safe_activity(
                f"display.{event.stage}",
                event.receiver_id,
                fields,
                _utc_now(),
            )
            self._activity.emit(activity)
        except Exception:
            # The safe activity boundary is deliberately non-authoritative for
            # ownership.  A bad sink cannot interrupt the transaction.
            return


__all__ = ["CreationProgress", "DisplayCreator", "select_new_output"]
