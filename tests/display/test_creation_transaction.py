from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from airplay_linux.display.creation import CreationProgress, DisplayCreator, select_new_output
from airplay_linux.display.kscreen import OutputInfo
from airplay_linux.display.locking import CreationLock
from airplay_linux.domain.operations import (
    DisplayHandle,
    InstanceId,
    OperationContext,
    OperationId,
    OwnerId,
    ProcessIdentity,
)
from airplay_linux.domain.records import LayoutSnapshot
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.testing.adapters import MemoryActivity, MemoryOwnershipStore
from airplay_linux.testing.time import FakeClock, FakeScheduler


SIZE = Size(width=1920, height=1080)
APP_INSTANCE = InstanceId(UUID(int=41))
RUNTIME_DIRECTORY = "/run/user/1000/airplay-linux/instances/41"


def output(
    output_id: str,
    connector: str,
    *,
    size: Size = SIZE,
    virtual: bool | None = True,
    enabled: bool = True,
    position: tuple[int, int] = (0, 0),
) -> OutputInfo:
    return OutputInfo(
        output_id=output_id,
        connector=connector,
        size=size,
        enabled=enabled,
        position=position,
        scale=1.0,
        virtual=virtual,
    )


BASELINE_OUTPUTS = (output("1", "DP-1", virtual=False),)


def snapshot(outputs: tuple[OutputInfo, ...]) -> LayoutSnapshot:
    return LayoutSnapshot(
        outputs={
            item.connector: {
                "id": item.output_id,
                "width": item.size.width,
                "height": item.size.height,
                "enabled": item.enabled,
                "x": item.position[0],
                "y": item.position[1],
                "scale": item.scale,
            }
            for item in outputs
        },
        baseline_hash="baseline",
    )


class ImmediateHelper:
    def __init__(self, observer: "Observer") -> None:
        self.observer = observer
        self.launched: list[tuple[OwnerId, str, Size]] = []
        self.stopped: list[ProcessIdentity] = []
        self.create_output = True
        self._next_pid = 7000

    async def probe(self):
        from airplay_linux.display.helper import HelperFeatures

        return Ok(
            value=HelperFeatures(
                version="test",
                distinct_outputs=True,
                capability_fingerprint="test-capability",
            ),
        )

    async def launch(self, owner_uuid, runtime_directory: str, size: Size):
        self._next_pid += 1
        identity = ProcessIdentity(pid=self._next_pid, start_ticks=identity_ticks(self._next_pid))
        self.launched.append((owner_uuid, runtime_directory, size))
        if self.create_output:
            self.observer.current = self.observer.current + (
                output(str(self._next_pid), f"Virtual-{self._next_pid % 1000}"),
            )
        return Ok(value=identity)

    async def stop(self, identity, owner_uuid, runtime_directory: str, deadline: float):
        self.stopped.append(identity)
        self.observer.current = tuple(
            item for item in self.observer.current if item.output_id != str(identity.pid)
        )
        return Ok(value=None)


def identity_ticks(pid: int) -> int:
    return pid * 11


class LateHelper(ImmediateHelper):
    def __init__(self, observer: "Observer") -> None:
        super().__init__(observer)
        self.started = asyncio.Event()
        self.completion: asyncio.Future[object] = asyncio.get_running_loop().create_future()

    async def launch(self, owner_uuid, runtime_directory: str, size: Size):
        self._next_pid += 1
        identity = ProcessIdentity(pid=self._next_pid, start_ticks=identity_ticks(self._next_pid))
        self.launched.append((owner_uuid, runtime_directory, size))
        self.started.set()
        await asyncio.shield(self.completion)
        self.observer.current = self.observer.current + (
            output(str(self._next_pid), f"Virtual-{self._next_pid % 1000}"),
        )
        return Ok(value=identity)


class ObserveStopLateHelper(LateHelper):
    def __init__(self, observer: "Observer", store: MemoryOwnershipStore) -> None:
        super().__init__(observer)
        self.store = store
        self.record_at_stop = None

    async def stop(self, identity, owner_uuid, runtime_directory: str, deadline: float):
        self.record_at_stop = self.store.records.get(owner_uuid)
        return await super().stop(identity, owner_uuid, runtime_directory, deadline)


class ReusedOutputHelper(ImmediateHelper):
    async def stop(self, identity, owner_uuid, runtime_directory: str, deadline: float):
        self.stopped.append(identity)
        self.observer.current = tuple(
            replace(
                item,
                connector="HDMI-99",
                virtual=False,
            ) if item.output_id == str(identity.pid) else item
            for item in self.observer.current
        )
        return Ok(value=None)


class Observer:
    def __init__(self, baseline: tuple[OutputInfo, ...] = BASELINE_OUTPUTS) -> None:
        self.baseline = baseline
        self.current = baseline
        self.configure_calls: list[tuple[str, dict[str, object]]] = []
        self.snapshot_calls = 0
        self.wait_calls = 0

    async def snapshot(self):
        self.snapshot_calls += 1
        return Ok(value=snapshot(self.baseline))

    async def outputs(self):
        return Ok(value=self.current)

    async def wait_for(self, predicate, deadline: float):
        self.wait_calls += 1
        if predicate(self.current):
            return Ok(value=self.current)
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    async def configure(self, output_id: str, values):
        self.configure_calls.append((output_id, dict(values)))
        changed = []
        for item in self.current:
            if item.output_id == output_id:
                requested_size = values.get("size", item.size)
                if isinstance(requested_size, str):
                    width, height = (int(part) for part in requested_size.split("x"))
                    requested_size = Size(width=width, height=height)
                requested_position = values.get("position", item.position)
                if isinstance(requested_position, str):
                    x, y = (int(part) for part in requested_position.split(","))
                    requested_position = (x, y)
                changed.append(
                    replace(
                        item,
                        size=requested_size,
                        enabled=values.get("enabled", item.enabled),
                        position=requested_position,
                    ),
                )
            else:
                changed.append(item)
        self.current = tuple(changed)
        return Ok(value=None)


class FailingPositionObserver(Observer):
    async def configure(self, output_id: str, values):
        if "position" in values:
            return Err(error=diagnostic(ErrorCode.configuration_failed))
        return await super().configure(output_id, values)


class BlockingObserver(Observer):
    def __init__(self, baseline: tuple[OutputInfo, ...] = BASELINE_OUTPUTS) -> None:
        super().__init__(baseline)
        self.wait_started = asyncio.Event()
        self.wait_completion: asyncio.Future[object] = asyncio.get_running_loop().create_future()

    async def wait_for(self, predicate, deadline: float):
        self.wait_calls += 1
        self.wait_started.set()
        await asyncio.shield(self.wait_completion)
        if predicate(self.current):
            return Ok(value=self.current)
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))


def diagnostic(code: ErrorCode):
    from airplay_linux.diagnostics.catalog import diagnostic as make_diagnostic

    return make_diagnostic(code)


def context(number: int = 1) -> OperationContext:
    return OperationContext(
        operation_id=OperationId(UUID(int=100 + number)),
        session_generation=number,
        endpoint_generation=1,
        transport_lease_id=None,
        trust_generation=0,
        audio_operation_id=None,
    )


def make_creator(tmp_path: Path, observer, helper, store=None, activity=None) -> DisplayCreator:
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    return DisplayCreator(
        helper=helper,
        observer=observer,
        ownership_store=store or MemoryOwnershipStore(),
        creation_lock=CreationLock(tmp_path / "runtime"),
        runtime_directory=RUNTIME_DIRECTORY,
        app_instance_uuid=APP_INSTANCE,
        clock=clock,
        scheduler=scheduler,
        id_factory=__import__("airplay_linux.testing.adapters", fromlist=["SequenceIds"]).SequenceIds(500),
        activity_sink=activity or MemoryActivity(),
    )


@pytest.mark.asyncio
async def test_creator_lock_spans_baseline_through_verified_publication(tmp_path: Path):
    first_observer = BlockingObserver()
    first_helper = ImmediateHelper(first_observer)
    first_store = MemoryOwnershipStore()
    first = make_creator(tmp_path, first_observer, first_helper, first_store)

    second_observer = Observer()
    second_helper = ImmediateHelper(second_observer)
    second_store = MemoryOwnershipStore()
    second = make_creator(tmp_path, second_observer, second_helper, second_store)

    first_task = asyncio.create_task(
        first.create(
            receiver_id="airplay-device:a",
            endpoint_fingerprint="endpoint-a",
            size=SIZE,
            context=context(),
        ),
    )
    await first_observer.wait_started.wait()

    second_task = asyncio.create_task(
        second.create(
            receiver_id="airplay-device:b",
            endpoint_fingerprint="endpoint-b",
            size=SIZE,
            context=context(2),
        ),
    )
    await asyncio.sleep(0.02)
    assert second_observer.snapshot_calls == 0

    first_observer.wait_completion.set_result(None)
    first_result, second_result = await asyncio.gather(first_task, second_task)
    assert isinstance(first_result, Ok)
    assert isinstance(second_result, Ok)
    assert second_observer.snapshot_calls == 1


@pytest.mark.asyncio
async def test_each_atomic_write_failure_rolls_back_owned_output(tmp_path: Path):
    for step in MemoryOwnershipStore.STEPS:
        observer = Observer()
        helper = ImmediateHelper(observer)
        store = MemoryOwnershipStore()
        store.inject_failure(step, occurrence=3)
        creator = make_creator(tmp_path / step, observer, helper, store)

        result = await creator.create(
            receiver_id="airplay-device:a",
            endpoint_fingerprint=f"endpoint-{step}",
            size=SIZE,
            context=context(),
        )

        assert isinstance(result, Err), step
        assert helper.stopped, step
        assert observer.current == BASELINE_OUTPUTS, step
        assert not store.records, step


def test_selects_one_matching_new_virtual_output_and_ignores_baseline():
    preexisting = output("7", "Virtual-old")
    candidate = output("8", "Virtual-new")

    result = select_new_output((BASELINE_OUTPUTS[0], preexisting), (BASELINE_OUTPUTS[0], preexisting, candidate), SIZE)

    assert isinstance(result, Ok)
    assert result.value == candidate


def test_two_matching_new_outputs_are_ambiguous():
    candidates = tuple(
        output(str(index), f"Virtual-{index}") for index in (7, 8)
    )

    result = select_new_output((), candidates, SIZE)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.configuration_failed


def test_mismatched_or_unproven_output_is_not_claimed():
    observed = (
        output("7", "Virtual-wrong-size", size=Size(width=1280, height=720)),
        output("8", "DP-2", virtual=False),
    )

    result = select_new_output((), observed, SIZE)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.configuration_failed


@pytest.mark.asyncio
async def test_creation_lock_serializes_distinct_instances_on_same_runtime(tmp_path: Path):
    first = CreationLock(tmp_path / "runtime")
    second = CreationLock(tmp_path / "runtime")
    entered: list[str] = []
    release = asyncio.Event()

    async def worker(lock: CreationLock, name: str):
        async with lock.acquire():
            entered.append(name)
            await release.wait()

    first_task = asyncio.create_task(worker(first, "first"))
    await asyncio.sleep(0)
    second_task = asyncio.create_task(worker(second, "second"))
    await asyncio.sleep(0.02)
    assert entered == ["first"]

    release.set()
    await asyncio.gather(first_task, second_task)
    assert entered == ["first", "second"]


@pytest.mark.asyncio
async def test_create_publishes_each_durable_stage_and_returns_verified_handle(tmp_path: Path):
    observer = Observer()
    helper = ImmediateHelper(observer)
    store = MemoryOwnershipStore()
    activity = MemoryActivity()
    creator = make_creator(tmp_path, observer, helper, store, activity)
    progress: list[CreationProgress] = []
    creator.subscribe(progress.append)

    result = await creator.create(
        receiver_id="airplay-device:a",
        endpoint_fingerprint="endpoint-a",
        size=SIZE,
        context=context(),
    )

    assert isinstance(result, Ok)
    assert result.value.source.connector.startswith("Virtual-")
    assert [item.stage for item in progress] == [
        "reserved", "helper_started", "output_observed", "published",
    ]
    record = next(iter(store.records.values()))
    assert record.stage == "published"
    assert record.desired_state == "active"
    assert record.helper == result.value.helper
    assert record.connector == result.value.source.connector
    assert record.output_id == result.value.source.kscreen_output_id
    assert helper.launched[0][1] == RUNTIME_DIRECTORY
    assert any(values.get("enabled") is True for _, values in observer.configure_calls)
    assert any(values.get("position") == "0,0" for _, values in observer.configure_calls)
    assert [event.code for event in activity.events] == [
        "display.reserved", "display.helper_started",
        "display.output_observed", "display.published",
    ]
    assert all("secret" not in repr(event) for event in activity.events)


@pytest.mark.asyncio
async def test_atomic_publication_failure_never_returns_published_handle(tmp_path: Path):
    observer = Observer()
    helper = ImmediateHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure("directory_fsync", occurrence=7)
    creator = make_creator(tmp_path, observer, helper, store)

    result = await creator.create(
        receiver_id="airplay-device:a",
        endpoint_fingerprint="endpoint-a",
        size=SIZE,
        context=context(),
    )

    assert isinstance(result, Err)
    assert result.error.code in {ErrorCode.persistence_failed, ErrorCode.cleanup_timeout}
    assert not any(record.stage == "published" and record.desired_state == "active" for record in store.records.values())
    assert helper.stopped


@pytest.mark.asyncio
async def test_cancellation_during_late_helper_launch_cleans_exact_helper(tmp_path: Path):
    observer = Observer()
    store = MemoryOwnershipStore()
    helper = ObserveStopLateHelper(observer, store)
    creator = make_creator(tmp_path, observer, helper, store)

    task = asyncio.create_task(
        creator.create(
            receiver_id="airplay-device:a",
            endpoint_fingerprint="endpoint-a",
            size=SIZE,
            context=context(),
        ),
    )
    await helper.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    helper.completion.set_result(None)
    for _ in range(20):
        if helper.stopped:
            break
        await asyncio.sleep(0)

    assert helper.stopped
    assert helper.record_at_stop is not None
    assert helper.record_at_stop.helper is not None
    assert not store.records
    assert not any(item.stage == "published" for item in store.records.values())


@pytest.mark.asyncio
async def test_prepare_teardown_fences_pending_creation_and_marks_record_removing(tmp_path: Path):
    observer = Observer()
    helper = LateHelper(observer)
    store = MemoryOwnershipStore()
    creator = make_creator(tmp_path, observer, helper, store)

    create_task = asyncio.create_task(
        creator.create(
            receiver_id="airplay-device:a",
            endpoint_fingerprint="endpoint-a",
            size=SIZE,
            context=context(),
        ),
    )
    await helper.started.wait()
    teardown = await creator.prepare_teardown(
        receiver_id="airplay-device:a",
        superseded_operation_id=context().operation_id,
        context=context(2),
    )

    assert isinstance(teardown, Ok)
    assert teardown.value
    assert next(iter(store.records.values())).desired_state == "removing"
    assert next(iter(store.records.values())).stage == "removing"
    assert not helper.stopped

    helper.completion.set_result(None)
    with pytest.raises(asyncio.CancelledError):
        create_task.cancel()
        await create_task
    for _ in range(20):
        if helper.stopped:
            break
        await asyncio.sleep(0)
    assert helper.stopped




@pytest.mark.asyncio
async def test_reused_output_identity_keeps_removal_record_and_never_deletes_new_output(tmp_path: Path):
    observer = FailingPositionObserver()
    helper = ReusedOutputHelper(observer)
    store = MemoryOwnershipStore()
    creator = make_creator(tmp_path, observer, helper, store)

    result = await creator.create(
        receiver_id="airplay-device:a",
        endpoint_fingerprint="endpoint-a",
        size=SIZE,
        context=context(),
    )

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.ownership_mismatch
    assert helper.stopped
    assert any(item.connector == "HDMI-99" for item in observer.current)
    record = next(iter(store.records.values()))
    assert record.stage == "removing"
    assert record.desired_state == "removing"


@pytest.mark.asyncio
async def test_existing_output_is_never_removed_when_creation_cannot_prove_new_output(tmp_path: Path):
    existing = output("7", "Virtual-existing")
    observer = Observer((BASELINE_OUTPUTS[0], existing))
    helper = ImmediateHelper(observer)
    helper.create_output = False
    store = MemoryOwnershipStore()
    creator = make_creator(tmp_path, observer, helper, store)

    result = await creator.create(
        receiver_id="airplay-device:a",
        endpoint_fingerprint="endpoint-a",
        size=SIZE,
        context=context(),
    )

    assert isinstance(result, Err)
    assert existing in observer.current
    assert any(item.output_id == existing.output_id for item in observer.current)
    assert not store.records
