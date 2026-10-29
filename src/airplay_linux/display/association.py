"""Conservative process-to-output association for the KScreen-backed provider.

KScreen does not expose a helper PID on an output. The strongest available
production evidence is therefore temporal and fail-closed: the output must be a
newly observed virtual connector selected after the exact owned helper launched,
and both the helper identity and connector/output identity must remain current
through an independent full observation. This does not claim an upstream
``krfb-virtualmonitor`` association API that the platform does not provide.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import OwnerId, ProcessIdentity
from airplay_linux.domain.ports import OwnershipStore
from airplay_linux.domain.records import LayoutSnapshot, OwnershipRecord
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kscreen import KScreenObserver, OutputInfo


def _refused() -> Err:
    return Err(error=diagnostic(ErrorCode.ownership_mismatch))


@dataclass(frozen=True, slots=True)
class _LaunchFence:
    observation_subject: str
    observation_sequence: int
    baseline: LayoutSnapshot


def _observation_stamp(value: object) -> tuple[str, int] | None:
    subject = getattr(value, "observation_subject", None)
    sequence = getattr(value, "observation_sequence", None)
    if (
        type(subject) is not str
        or not subject
        or type(sequence) is not int
        or sequence <= 0
    ):
        return None
    return subject, sequence


def _after_fence(value: object, fence: _LaunchFence) -> bool:
    stamp = _observation_stamp(value)
    return stamp is not None and stamp[0] == fence.observation_subject and stamp[1] > fence.observation_sequence


class KScreenOutputVerifier:
    """Verify a candidate against current helper ownership and KScreen evidence.

    This deliberately verifies identity, not the candidate's old geometry: the
    creator calls it before each KScreen configuration step, during which size,
    enabled state, and position are expected to change. Requested geometry is
    separately checked by the creator's exact readback contract.
    """

    def __init__(
        self, helper: VirtualHelper, observer: KScreenObserver, ownerships: OwnershipStore,
    ) -> None:
        self._helper = helper
        self._observer = observer
        self._ownerships = ownerships
        self._launches: dict[OwnerId, _LaunchFence] = {}

    def register_launch(self, owner_uuid: OwnerId, baseline: LayoutSnapshot) -> bool:
        """Bind one fresh observer fence to the helper launch about to occur."""
        if type(owner_uuid) is not UUID or type(baseline) is not LayoutSnapshot:
            return False
        stamp = _observation_stamp(baseline)
        if stamp is None or owner_uuid in self._launches:
            return False
        self._launches[owner_uuid] = _LaunchFence(
            observation_subject=stamp[0],
            observation_sequence=stamp[1],
            baseline=baseline,
        )
        return True

    async def __call__(
        self,
        owner_uuid: OwnerId,
        runtime_directory: str,
        identity: ProcessIdentity,
        candidate: OutputInfo,
    ) -> Result[None]:
        try:
            return await self._verify(owner_uuid, runtime_directory, identity, candidate)
        except Exception:
            return _refused()

    async def _verify(
        self,
        owner_uuid: OwnerId,
        runtime_directory: str,
        identity: ProcessIdentity,
        candidate: OutputInfo,
    ) -> Result[None]:
        if (
            not isinstance(owner_uuid, UUID)
            or type(runtime_directory) is not str
            or not runtime_directory
            or type(identity) is not ProcessIdentity
            or type(candidate) is not OutputInfo
            or candidate.virtual is not True
        ):
            return _refused()

        try:
            ownership_result = await self._ownerships.all()
        except Exception:
            return _refused()
        if (
            not isinstance(ownership_result, Ok)
            or type(ownership_result.value) is not tuple
        ):
            return _refused()
        records = [item for item in ownership_result.value if item.owner_uuid == owner_uuid]
        if len(records) != 1 or type(records[0]) is not OwnershipRecord:
            return _refused()
        record = records[0]
        fence = self._launches.get(owner_uuid)
        if (
            fence is None
            or record.runtime_directory != runtime_directory
            or record.helper != identity
            or record.stage == "reserved"
            or candidate.size != record.requested_size
            or (record.connector is not None and record.connector != candidate.connector)
            or (record.output_id is not None and record.output_id != candidate.output_id)
            or type(record.baseline) is not LayoutSnapshot
            or record.baseline.outputs != fence.baseline.outputs
            or not _after_fence(candidate, fence)
        ):
            return _refused()

        baseline_ids = {
            properties.get("id")
            for properties in fence.baseline.outputs.values()
            if type(properties.get("id")) is str
        }
        if (
            candidate.connector in fence.baseline.outputs
            or candidate.output_id in baseline_ids
        ):
            return _refused()

        before = self._owned_exactly(owner_uuid, runtime_directory, identity)
        if not before:
            return _refused()
        try:
            observed = await self._observer.observation()
        except Exception:
            return _refused()
        if not isinstance(observed, Ok) or type(observed.value) is not tuple or len(observed.value) != 2:
            return _refused()
        outputs, layout = observed.value
        if (
            type(outputs) is not tuple
            or type(layout) is not LayoutSnapshot
            or not isinstance(layout.outputs, Mapping)
            or not _after_fence(layout, fence)
        ):
            return _refused()
        if any(type(output) is not OutputInfo for output in outputs):
            return _refused()

        current_by_connector = {output.connector: output for output in outputs}
        if len(current_by_connector) != len(outputs) or len(outputs) != len(layout.outputs):
            return _refused()
        if any(
            output.output_id != layout.outputs.get(output.connector, {}).get("id")
            or output.size.width != layout.outputs.get(output.connector, {}).get("width")
            or output.size.height != layout.outputs.get(output.connector, {}).get("height")
            for output in outputs
        ):
            return _refused()
        by_connector = [output for output in outputs if output.connector == candidate.connector]
        by_id = [output for output in outputs if output.output_id == candidate.output_id]
        if (
            len(by_connector) != 1
            or len(by_id) != 1
            or by_connector[0] != by_id[0]
            or by_connector[0].output_id != candidate.output_id
            or by_connector[0].virtual is not True
            or by_connector[0].size != record.requested_size
        ):
            return _refused()
        new_virtual_matches = [
            output for output in outputs
            if output.virtual is True
            and output.size == record.requested_size
            and output.connector not in fence.baseline.outputs
            and output.output_id not in baseline_ids
        ]
        if len(new_virtual_matches) != 1 or new_virtual_matches[0] != by_connector[0]:
            return _refused()
        properties = layout.outputs.get(candidate.connector)
        if (
            not properties
            or properties.get("id") != candidate.output_id
            or sum(
                1 for current in layout.outputs.values()
                if current.get("id") == candidate.output_id
            ) != 1
        ):
            return _refused()

        after = self._owned_exactly(owner_uuid, runtime_directory, identity)
        if not after:
            return _refused()
        return Ok(value=None)

    def _owned_exactly(
        self, owner_uuid: OwnerId, runtime_directory: str, identity: ProcessIdentity,
    ) -> bool:
        try:
            verify = getattr(self._helper, "verifies_owned_identity", None)
            return callable(verify) and verify(identity, owner_uuid, runtime_directory) is True
        except Exception:
            return False


__all__ = ["KScreenOutputVerifier"]
