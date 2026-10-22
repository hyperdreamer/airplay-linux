"""Ownership regressions using real adapters over deterministic process evidence."""
from __future__ import annotations
import asyncio
from dataclasses import replace
import json
from uuid import UUID

import pytest

from test_creation_transaction import (
    BASELINE_OUTPUTS, SIZE, RUNTIME_DIRECTORY, APP_INSTANCE,
    BlockingObserver, FailingPositionObserver, ImmediateHelper, LateHelper,
    Observer, context, diagnostic, make_creator, output,
)
from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kscreen import KScreenObserver, layout_hash
from airplay_linux.display.locking import CreationLock
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.storage import AtomicFiles, JsonOwnershipStore, PosixFilesystem
from airplay_linux.testing.adapters import FakeProcess, MemoryOwnershipStore, SequenceIds
from airplay_linux.testing.time import FakeClock, FakeScheduler


class ValidSnapshot:
    async def snapshot(self):
        result = await super().snapshot()
        fields = {connector: {name: int(value) if type(value) is float and value.is_integer() else value
                              for name,value in properties.items()}
                  for connector,properties in result.value.outputs.items()}
        return Ok(value=replace(result.value,outputs=fields,baseline_hash=layout_hash(fields)))


class DurableObserver(ValidSnapshot,Observer):
    pass


class DurableBlockingObserver(ValidSnapshot,BlockingObserver):
    pass


def creator_for(tmp_path, helper, observer, store):
    clock = FakeClock()
    return DisplayCreator(helper, observer, store, CreationLock(tmp_path/'runtime'),
                          RUNTIME_DIRECTORY, APP_INSTANCE, clock, FakeScheduler(clock), SequenceIds(500),
                          output_verifier=getattr(helper, 'verify_output', None))


async def create(creator):
    return await creator.create('airplay-device:a', 'endpoint-a', SIZE, context())


async def settle(creator):
    for _ in range(50):
        await asyncio.sleep(0)
        tasks = tuple(creator._background)
        if not tasks:
            return
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    raise AssertionError('background transaction never settled')


class DesktopProcess(FakeProcess):
    """Actual helper/KScreen adapters over an in-memory process/compositor world."""
    def __init__(self, scenario):
        super().__init__()
        self.scenario = scenario
        self.outputs = [self.entry('1','DP-1', physical=True)]
        if scenario == 'preexisting-unknown':
            self.outputs.append(self.entry('7','Virtual-foreign', unknown=True))
        self.configuration = []
        self.created_identity = None
        self.after_launch = False
        self.associations = {}

    async def verify_output(self, owner_uuid, runtime_directory, identity, candidate):
        if not self.owned(identity, owner_uuid, runtime_directory) or self.associations.get(identity) != (
            candidate.output_id, candidate.connector,
        ):
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        return Ok(value=None)

    def _forget(self, identity):
        association = self.associations.pop(identity, None)
        if association is not None:
            self.outputs = [item for item in self.outputs if (str(item['id']), item['name']) != association]
        super()._forget(identity)

    @staticmethod
    def entry(oid, name, physical=False, unknown=False):
        item = dict(id=int(oid), name=name, type='DisplayPort' if physical else 'Virtual',
                    enabled=True, pos={'x':0,'y':0}, scale=1.0,
                    currentModeId='0' if unknown else '1',
                    modes=[{'id':'1','size':{'width':1920,'height':1080},'refreshRate':60}])
        if not unknown:
            item['size']={'width':1920,'height':1080}
        return item

    async def run(self, spec, timeout, stdin=None):
        self.runs.append((spec,timeout))
        if spec.argv[0] == 'krfb-virtualmonitor':
            text = '--resolution <WxH>' if '--help' in spec.argv else 'krfb-virtualmonitor 24.08.2'
            return Ok(value=CommandOutput(exit_code=0, stdout=text.encode(), stderr=b''))
        assert spec.argv[0] == 'kscreen-doctor'
        if spec.argv[1:] == ('-j',):
            if self.after_launch and self.scenario in {'dead-helper-foreign-new','reused-helper-foreign-new','live-helper-foreign-new'}:
                if self.scenario != 'live-helper-foreign-new':
                    self.exit(self.created_identity)
                if self.scenario == 'reused-helper-foreign-new':
                    self.register(replace(self.created_identity,start_ticks=self.created_identity.start_ticks+1),
                                  self.owner_environment(UUID(int=987),'/tmp/foreign-instance'))
                self.outputs.append(self.entry('7','Virtual-foreign'))
                self.after_launch = False
            return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps({'outputs': self.outputs}).encode(), stderr=b''))
        self.configuration.extend(spec.argv[1:])
        # All requested properties are already true; exact mode ID 1 is supported.
        return Ok(value=CommandOutput(exit_code=0, stdout=b'', stderr=b''))

    async def spawn(self, spec):
        result = await super().spawn(spec)
        self.created_identity = result.value
        self.after_launch = True
        if self.scenario == 'preexisting-unknown':
            self.outputs[1] = self.entry('7','Virtual-foreign')
        elif self.scenario == 'owned-new':
            self.outputs.append(self.entry('7','Virtual-owned'))
            self.associations[result.value] = ('7', 'Virtual-owned')
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['preexisting-unknown', 'dead-helper-foreign-new',
                                      'reused-helper-foreign-new', 'live-helper-foreign-new'])
async def test_foreign_output_is_never_configured_or_published(tmp_path, scenario):
    process = DesktopProcess(scenario)
    clock = FakeClock()
    observer = KScreenObserver(process, clock, FakeScheduler(clock))
    helper = VirtualHelper(process, clock)
    store = JsonOwnershipStore(AtomicFiles(tmp_path/'state'))
    creator = DisplayCreator(helper, observer, store, CreationLock(tmp_path/'runtime'),
                             RUNTIME_DIRECTORY, APP_INSTANCE, clock, FakeScheduler(clock), SequenceIds(500),
                             output_verifier=process.verify_output)
    if scenario == 'preexisting-unknown':
        baseline = (await observer.snapshot()).value
        described = (await observer.outputs()).value
        assert 'Virtual-foreign' in baseline.outputs
        assert all(o.connector != 'Virtual-foreign' for o in described)
    result = await create(creator)
    records = (await store.all()).value
    evidence = dict(result=type(result).__name__, configured=process.configuration,
                    records=[(r.stage,r.connector) for r in records],
                    helper_live=process.alive(process.created_identity))
    assert isinstance(result, Err), evidence
    assert not process.configuration, evidence
    assert all(r.stage != 'published' for r in records), evidence


class UnprovableLaunchProcess(FakeProcess):
    def __init__(self, observer):
        super().__init__()
        self.observer = observer
    def owned(self, identity, owner_uuid, runtime_directory):
        return False
    async def spawn(self, spec):
        result = await super().spawn(spec)
        self.observer.current += (output('7','Virtual-live-unproven'),)
        return result
    async def run(self, spec, timeout, stdin=None):
        text = '--resolution <WxH>' if '--help' in spec.argv else 'krfb-virtualmonitor 24.08.2'
        return Ok(value=CommandOutput(exit_code=0,stdout=text.encode(),stderr=b''))


@pytest.mark.asyncio
async def test_unconfirmed_real_helper_launch_failure_retains_reservation(tmp_path):
    observer = DurableObserver()
    process = UnprovableLaunchProcess(observer)
    helper = VirtualHelper(process, FakeClock())
    store = JsonOwnershipStore(AtomicFiles(tmp_path/'state'))
    creator = creator_for(tmp_path,helper,observer,store)
    result = await create(creator)
    assert isinstance(result, Err)
    assert process.spawned and process.environments()
    assert not process.terminated
    assert len(observer.current) == 2
    records = (await store.all()).value
    assert records and all(r.stage == 'removing' for r in records), dict(
        result=result.error.code, live_helpers=list(process.environments()),
        remaining_outputs=[o.connector for o in observer.current], records=records)


class StickyHelper(ImmediateHelper):
    async def stop(self, identity, owner_uuid, runtime_directory, deadline):
        self.resources.pop(identity)
        self.stopped.append(identity)
        # The process has exited but compositor disappearance has not arrived.
        return Ok(value=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_write',[2,3,4,5,6,7])
async def test_failed_write_retains_cleanup_evidence_until_output_disappears(tmp_path, fail_write):
    observer = Observer()
    helper = StickyHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure('write', occurrence=fail_write)
    creator = make_creator(tmp_path, observer, helper, store)
    result = await create(creator)
    assert isinstance(result,Err)
    assert helper.stopped and len(observer.current) == 2
    assert store.records and all(r.stage == 'removing' for r in store.records.values()), dict(
        failed_write=fail_write, result=result.error.code, waits=observer.wait_calls,
        remaining_outputs=[o.connector for o in observer.current], records=store.records)


class PauseWrites(MemoryOwnershipStore):
    def __init__(self, occurrence):
        super().__init__()
        self.occurrence = occurrence
        self.count = 0
        self.paused = asyncio.Event()
        self.release = asyncio.Event()
    async def write(self, record):
        self.count += 1
        result = await super().write(record)
        if self.count == self.occurrence:
            self.paused.set()
            await self.release.wait()
        return result


@pytest.mark.asyncio
@pytest.mark.parametrize('occurrence',[1,2,3,4,5,6,7])
async def test_cancellation_at_every_creation_write_eventually_cleans(tmp_path, occurrence):
    observer = Observer()
    helper = ImmediateHelper(observer)
    store = PauseWrites(occurrence)
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    await asyncio.wait_for(store.paused.wait(),1)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    # A held store write also holds the per-owner journal. Cancellation cannot
    # acknowledge settlement until it can durably publish removal intent.
    assert not task.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(creator)
    assert observer.current == BASELINE_OUTPUTS
    assert not store.records


@pytest.mark.asyncio
@pytest.mark.parametrize('stage',['launch','observation'])
async def test_cancel_publishes_removing_intent_before_returning(tmp_path, stage):
    observer = DurableBlockingObserver() if stage == 'observation' else DurableObserver()
    helper = ImmediateHelper(observer) if stage == 'observation' else LateHelper(observer)
    store = JsonOwnershipStore(AtomicFiles(tmp_path/'state'))
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    ready = observer.wait_started if stage == 'observation' else helper.started
    await asyncio.wait_for(ready.wait(),1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    records = (await store.all()).value
    # Settle all held work even when the assertion is expected to fail.
    if stage == 'observation':
        observer.wait_completion.set_result(None)
    else:
        helper.completion.set_result(None)
    await settle(creator)
    assert records and all(r.desired_state == 'removing' and r.stage == 'removing' for r in records), [
        (r.stage,r.desired_state) for r in records]


@pytest.mark.asyncio
async def test_lock_spans_pending_final_publication(tmp_path):
    observer = Observer()
    store = PauseWrites(7)
    first = make_creator(tmp_path, observer, ImmediateHelper(observer), store)
    other_observer = Observer()
    second = make_creator(tmp_path, other_observer, ImmediateHelper(other_observer), MemoryOwnershipStore())
    t1 = asyncio.create_task(create(first))
    await asyncio.wait_for(store.paused.wait(),1)
    t2 = asyncio.create_task(second.create('airplay-device:b','endpoint-b',SIZE,context(2)))
    for _ in range(50):
        await asyncio.sleep(0)
    blocked = other_observer.snapshot_calls == 0
    store.release.set()
    results = await asyncio.wait_for(asyncio.gather(t1,t2),1)
    assert all(isinstance(r,Ok) for r in results)
    assert blocked, 'second baseline interleaved with pending published-stage write'


class TraceFilesystem(PosixFilesystem):
    def __init__(self):
        self.events = []
    def write(self,parent,name,payload):
        self.events.append(('write',name))
        return super().write(parent,name,payload)
    def fsync_file(self,parent,name):
        self.events.append(('fsync',name))
        return super().fsync_file(parent,name)
    def replace(self,parent,source,destination):
        self.events.append(('rename',destination))
        return super().replace(parent,source,destination)
    def fsync_directory(self,descriptor):
        self.events.append(('directory_fsync',None))
        return super().fsync_directory(descriptor)


@pytest.mark.asyncio
async def test_real_store_publication_has_ordered_durable_steps(tmp_path):
    observer = DurableObserver()
    helper = ImmediateHelper(observer)
    trace = TraceFilesystem()
    store = JsonOwnershipStore(AtomicFiles(tmp_path/'state',trace))
    creator = make_creator(tmp_path,observer,helper,store)
    result = await create(creator)
    assert isinstance(result,Ok)
    records = (await store.all()).value
    assert records[0].stage == 'published'
    starts = [i for i,event in enumerate(trace.events) if event[0]=='write']
    assert len(starts) == 7
    for begin,end in zip(starts,starts[1:]+[len(trace.events)]):
        names = [name for name,_ in trace.events[begin:end]]
        assert names[:3] == ['write','fsync','rename'],names
        assert len(names)>3 and names[3] == 'directory_fsync',names


class PausedPublicationStore(PauseWrites):
    def __init__(self):
        super().__init__(7)
    async def all(self):
        return await super().all()


@pytest.mark.asyncio
async def test_teardown_during_publication_cannot_return_active_handle(tmp_path):
    observer=Observer()
    helper=ImmediateHelper(observer)
    store=PausedPublicationStore()
    creator=make_creator(tmp_path,observer,helper,store)
    task=asyncio.create_task(create(creator))
    await asyncio.wait_for(store.paused.wait(),1)
    teardown=asyncio.create_task(creator.prepare_teardown('airplay-device:a',context().operation_id,context(2)))
    for _ in range(20):
        await asyncio.sleep(0)
    store.release.set()
    result, removed=await asyncio.wait_for(asyncio.gather(task,teardown),1)
    assert isinstance(result,Err)
    assert isinstance(removed,Ok)
    assert observer.current == BASELINE_OUTPUTS
    assert not store.records or all(r.stage == 'removing' for r in store.records.values())


class StageStore(MemoryOwnershipStore):
    def __init__(self):
        super().__init__()
        self.history = []
    async def write(self,record):
        self.history.append(record.stage)
        return await super().write(record)


@pytest.mark.asyncio
async def test_every_persisted_stage_is_legal_and_ordered(tmp_path):
    observer=Observer()
    helper=ImmediateHelper(observer)
    store=StageStore()
    result=await create(make_creator(tmp_path,observer,helper,store))
    assert isinstance(result,Ok)
    assert store.history == ['reserved','helper_started','output_observed','output_observed','output_observed','output_observed','published']


@pytest.mark.asyncio
async def test_real_adapters_bind_published_handle_to_exact_process(tmp_path):
    process=DesktopProcess('owned-new')
    clock=FakeClock()
    observer=KScreenObserver(process,clock,FakeScheduler(clock))
    helper=VirtualHelper(process,clock)
    store=JsonOwnershipStore(AtomicFiles(tmp_path/'state'))
    creator=DisplayCreator(helper,observer,store,CreationLock(tmp_path/'runtime'),
                           RUNTIME_DIRECTORY,APP_INSTANCE,clock,FakeScheduler(clock),SequenceIds(500),
                           output_verifier=process.verify_output)
    result=await create(creator)
    assert isinstance(result,Ok)
    assert result.value.helper == process.created_identity
    assert process.owned(result.value.helper,result.value.owner_uuid,RUNTIME_DIRECTORY)
    assert helper.find_owned(result.value.owner_uuid,RUNTIME_DIRECTORY) == (result.value.helper,)
    record=(await store.all()).value[0]
    assert record.helper == result.value.helper
    assert record.owner_uuid == result.value.owner_uuid


@pytest.mark.asyncio
async def test_reserved_record_is_durable_when_helper_is_invoked(tmp_path):
    observer=DurableObserver()
    store=JsonOwnershipStore(AtomicFiles(tmp_path/'state'))
    class InspectReservation(ImmediateHelper):
        async def launch(self,owner_uuid,runtime_directory,size):
            records=(await store.all()).value
            assert len(records)==1
            assert records[0].stage=='reserved' and records[0].owner_uuid==owner_uuid
            return await super().launch(owner_uuid,runtime_directory,size)
    result=await create(make_creator(tmp_path,observer,InspectReservation(observer),store))
    assert isinstance(result,Ok)


@pytest.mark.asyncio
async def test_cancellation_while_removing_retains_or_completes_cleanup(tmp_path):
    observer=FailingPositionObserver()
    helper=ImmediateHelper(observer)
    store=PauseWrites(6)
    creator=make_creator(tmp_path,observer,helper,store)
    task=asyncio.create_task(create(creator))
    await asyncio.wait_for(store.paused.wait(),1)
    assert next(iter(store.records.values())).stage=='removing'
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(creator)
    assert observer.current==BASELINE_OUTPUTS
    assert not store.records


@pytest.mark.asyncio
async def test_cancellation_on_published_progress_cleans_return_race(tmp_path):
    observer=Observer()
    helper=ImmediateHelper(observer)
    store=MemoryOwnershipStore()
    creator=make_creator(tmp_path,observer,helper,store)
    task=None
    def cancel_on_published(event):
        if event.stage=='published':
            task.cancel()
    creator.subscribe(cancel_on_published)
    task=asyncio.create_task(create(creator))
    with pytest.raises(asyncio.CancelledError):
        await task
    await settle(creator)
    assert observer.current==BASELINE_OUTPUTS
    assert not store.records



@pytest.mark.asyncio
async def test_documented_adapters_refuse_publication_without_output_association(tmp_path):
    process = DesktopProcess('owned-new')
    clock = FakeClock()
    observer = KScreenObserver(process, clock, FakeScheduler(clock))
    helper = VirtualHelper(process, clock)
    store = JsonOwnershipStore(AtomicFiles(tmp_path / 'state'))
    creator = creator_for(tmp_path, helper, observer, store)
    result = await create(creator)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_operation
    assert not process.configuration
    assert not (await store.all()).value
    assert not process.environments()


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse', ['connector', 'id'])
async def test_complete_baseline_excludes_reused_identity_despite_association_claim(tmp_path, reuse):
    class ReusedBaselineProcess(DesktopProcess):
        async def spawn(self, spec):
            result = await super().spawn(spec)
            candidate = self.entry('8', 'Virtual-foreign') if reuse == 'connector' else self.entry('7', 'Virtual-other')
            self.outputs[1] = candidate
            self.associations[result.value] = (str(candidate['id']), candidate['name'])
            return result

    process = ReusedBaselineProcess('preexisting-unknown')
    clock = FakeClock()
    observer = KScreenObserver(process, clock, FakeScheduler(clock))
    helper = VirtualHelper(process, clock)
    store = JsonOwnershipStore(AtomicFiles(tmp_path / 'state'))
    creator = DisplayCreator(
        helper, observer, store, CreationLock(tmp_path / 'runtime'),
        RUNTIME_DIRECTORY, APP_INSTANCE, clock, FakeScheduler(clock), SequenceIds(500),
        output_verifier=process.verify_output,
    )
    result = await create(creator)
    assert isinstance(result, Err)
    assert not process.configuration


@pytest.mark.asyncio
async def test_helper_identity_is_revalidated_after_final_write(tmp_path):
    observer = Observer()
    helper = ImmediateHelper(observer)
    store = PauseWrites(7)
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    await asyncio.wait_for(store.paused.wait(), 1)
    helper.resources.clear()
    store.release.set()
    result = await asyncio.wait_for(task, 1)
    assert isinstance(result, Err)
    assert store.records and all(r.stage == 'removing' for r in store.records.values())


@pytest.mark.asyncio
async def test_helper_identity_is_revalidated_after_publication_journal_read(tmp_path):
    class HeldReadStore(StageStore):
        def __init__(self):
            super().__init__()
            self.paused = asyncio.Event()
            self.release = asyncio.Event()
        async def all(self):
            if len(self.history) == 6 and not self.paused.is_set():
                self.paused.set()
                await self.release.wait()
            return await super().all()

    observer = Observer()
    helper = ImmediateHelper(observer)
    store = HeldReadStore()
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    await asyncio.wait_for(store.paused.wait(), 1)
    helper.resources.clear()
    store.release.set()
    result = await asyncio.wait_for(task, 1)
    assert isinstance(result, Err)
    assert 'published' not in store.history


@pytest.mark.asyncio
async def test_cancellation_reports_failed_removal_intent_without_signalling(tmp_path):
    observer = Observer()
    helper = LateHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure('fsync', occurrence=2)
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    await asyncio.wait_for(helper.started.wait(), 1)
    task.cancel()
    result = await asyncio.wait_for(task, 1)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    assert not helper.stopped
    assert next(iter(store.records.values())).stage == 'reserved'
    helper.completion.set_result(None)
    await settle(creator)
    assert not store.records
    assert observer.current == BASELINE_OUTPUTS


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_durable_removal_write(tmp_path):
    observer = Observer()
    helper = LateHelper(observer)
    store = PauseWrites(2)
    creator = make_creator(tmp_path, observer, helper, store)
    task = asyncio.create_task(create(creator))
    await asyncio.wait_for(helper.started.wait(), 1)
    task.cancel()
    await asyncio.wait_for(store.paused.wait(), 1)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert next(iter(store.records.values())).desired_state == 'removing'
    assert not helper.stopped
    helper.completion.set_result(None)
    await settle(creator)
    assert not store.records


@pytest.mark.asyncio
async def test_unobserved_output_with_unknown_geometry_keeps_removal_evidence(tmp_path):
    class UnknownOutputObserver(DurableObserver):
        async def snapshot(self):
            result = await super().snapshot()
            if helper.stopped:
                return Ok(value=replace(result.value, outputs={
                    **result.value.outputs, 'Virtual-unknown': {'id': '88'},
                }))
            return result

    observer = UnknownOutputObserver()
    helper = ImmediateHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure('write', occurrence=2)
    creator = make_creator(tmp_path, observer, helper, store)
    result = await create(creator)
    assert isinstance(result, Err)
    assert store.records and all(r.stage == 'removing' for r in store.records.values())


@pytest.mark.asyncio
@pytest.mark.parametrize('occurrence', range(1, 8))
@pytest.mark.parametrize('step', MemoryOwnershipStore.STEPS)
async def test_every_atomic_creation_write_failure_confirms_resource_absence(tmp_path, occurrence, step):
    observer = Observer()
    helper = ImmediateHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure(step, occurrence=occurrence)
    creator = make_creator(tmp_path, observer, helper, store)
    result = await create(creator)
    assert isinstance(result, Err)
    assert observer.current == BASELINE_OUTPUTS
    assert not helper.resources
    assert not store.records
    if occurrence > 1:
        assert helper.stopped
        assert observer.wait_calls > 0


@pytest.mark.asyncio
async def test_stop_acknowledgement_with_live_owned_helper_retains_evidence(tmp_path):
    class LiveAfterStop(ImmediateHelper):
        async def stop(self, identity, owner_uuid, runtime_directory, deadline):
            self.stopped.append(identity)
            return Ok(value=None)

    observer = Observer()
    helper = LiveAfterStop(observer)
    store = MemoryOwnershipStore()
    store.inject_failure('write', occurrence=2)
    result = await create(make_creator(tmp_path, observer, helper, store))
    assert isinstance(result, Err)
    assert helper.resources
    assert store.records and all(r.stage == 'removing' for r in store.records.values())


@pytest.mark.asyncio
async def test_helper_absence_is_rechecked_after_output_observation(tmp_path):
    class LateOwnedProcessObserver(Observer):
        async def snapshot(self):
            result = await super().snapshot()
            if helper.stopped:
                helper.resources[helper.stopped[0]] = (
                    next(iter(store.records)), RUNTIME_DIRECTORY, None,
                )
            return result

    observer = LateOwnedProcessObserver()
    helper = ImmediateHelper(observer)
    store = MemoryOwnershipStore()
    store.inject_failure('write', occurrence=2)
    result = await create(make_creator(tmp_path, observer, helper, store))
    assert isinstance(result, Err)
    assert helper.resources
    assert store.records and all(r.stage == 'removing' for r in store.records.values())
