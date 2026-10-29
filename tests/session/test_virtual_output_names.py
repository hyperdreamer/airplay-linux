"""Authorship regressions over the production provider; no live display I/O."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from airplay_linux.display.association import KScreenOutputVerifier
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.display.removal import DisplayRemover
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.storage import AtomicFiles, JsonOwnershipStore
from airplay_linux.testing.builders import context, target
from test_virtual_review_regressions import _audit_foreign_output
from virtual_scenarios import HD, production_display

pytestmark = pytest.mark.asyncio

# Hand-derived SHA-256 prefix for the fixture's first reservation (UUID(int=2500)).
OWNER_NAME = "Virtual-airplay-da1f0aef916590efb0292edbd28be5cd"
OTHER_OWNER_NAME = "Virtual-airplay-71603021792cbe70abfa7c1a431f24d3"


@pytest.fixture
def named_display(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    return production_display(tmp_path, verifier=True, create_output=False)


async def _create_with_postlaunch_output(rig, name):
    provider, desktop, store = rig
    original_launch = provider.helper.launch
    writes, events, verdicts = [], [], []
    original_write = store.write
    original_verify = desktop.output_verifier._verify

    async def record_write(record):
        writes.append(record.stage)
        return await original_write(record)

    async def record_verify(*args):
        result = await original_verify(*args)
        verdicts.append(result)
        return result

    async def launch_then_inject(*args):
        launched = await original_launch(*args)
        assert isinstance(launched, Ok)
        foreign = _audit_foreign_output()
        foreign["name"] = name
        desktop.entries.append(foreign)
        return launched

    store.write = record_write
    desktop.output_verifier._verify = record_verify
    provider.helper.launch = launch_then_inject
    provider.subscribe(events.append)
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    return result, writes, events, verdicts


async def test_foreign_output_after_launch_with_different_name_is_never_published(named_display):
    # Removing the verifier's exact name requirement reopens the audited residual.
    result, writes, events, verdicts = await _create_with_postlaunch_output(
        named_display, "Virtual-foreign",
    )
    assert isinstance(result, Err), result
    assert verdicts and isinstance(verdicts[0], Err), verdicts
    assert verdicts[0].error.code is ErrorCode.ownership_mismatch
    assert "published" not in writes
    assert all(event.kind != "published" for event in events)
    assert named_display[1].associations == {}
    assert named_display[1].entries[0]["name"] == "Virtual-foreign"
    assert all(record.stage != "published" for record in named_display[2].records.values())


async def test_foreign_output_with_another_owners_name_is_never_published(named_display):
    # Checking only a name prefix instead of the entire owner-derived name is unsafe.
    result, writes, events, verdicts = await _create_with_postlaunch_output(
        named_display, OTHER_OWNER_NAME,
    )
    assert isinstance(result, Err), result
    assert verdicts and isinstance(verdicts[0], Err), verdicts
    assert verdicts[0].error.code is ErrorCode.ownership_mismatch
    assert "published" not in writes
    assert all(event.kind != "published" for event in events)
    assert named_display[1].associations == {}
    assert named_display[1].entries[0]["name"] == OTHER_OWNER_NAME


async def test_exact_owner_name_from_helper_argv_is_accepted(tmp_path, monkeypatch):
    # Omitting --name, deriving from another owner, or refusing every name breaks this control.
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    provider, desktop, store = production_display(tmp_path, verifier=True)
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    assert isinstance(result, Ok), result
    (spec,) = desktop.spawned
    assert "--name" in spec.argv
    assert spec.argv[spec.argv.index("--name") + 1] == OWNER_NAME
    assert result.value.source.connector == OWNER_NAME
    assert store.records[result.value.owner_uuid].stage == "published"
    assert desktop.entries[0]["name"] == OWNER_NAME
    assert str(result.value.owner_uuid) not in " ".join(spec.argv)
    assert result.value.owner_uuid.hex not in " ".join(spec.argv)


async def test_name_in_fresh_observation_must_still_match_owner(named_display):
    provider, desktop, _store = named_display
    original_observation = desktop.observer.observation
    original_verify = desktop.output_verifier._verify
    checking = False
    verdicts = []

    async def observation_with_name_swap():
        if checking:
            desktop.entries[0]["name"] = OTHER_OWNER_NAME
        return await original_observation()

    async def record_verify(*args):
        nonlocal checking
        checking = True
        result = await original_verify(*args)
        verdicts.append(result)
        return result

    desktop.observer.observation = observation_with_name_swap
    desktop.output_verifier._verify = record_verify
    result, writes, _events, _verdicts = await _create_with_postlaunch_output(named_display, OWNER_NAME)
    assert isinstance(result, Err), result
    assert verdicts and isinstance(verdicts[0], Err), verdicts
    assert "published" not in writes


async def test_fresh_observation_without_name_explains_unproven_authorship(named_display):
    desktop = named_display[1]
    original_observation = desktop.observer.observation
    original_verify = desktop.output_verifier._verify
    checking = False
    verdicts = []

    async def observation_without_name():
        if checking:
            desktop.entries[0].pop("name", None)
        return await original_observation()

    async def record_verify(*args):
        nonlocal checking
        checking = True
        result = await original_verify(*args)
        verdicts.append(result)
        return result

    desktop.observer.observation = observation_without_name
    desktop.output_verifier._verify = record_verify
    result, writes, _events, _verdicts = await _create_with_postlaunch_output(named_display, OWNER_NAME)
    assert isinstance(result, Err), result
    assert verdicts and isinstance(verdicts[0], Err), verdicts
    assert verdicts[0].error.code is ErrorCode.ownership_mismatch
    assert "kscreen-doctor" in verdicts[0].error.message
    assert "'name'" in verdicts[0].error.message
    assert "authorship" in verdicts[0].error.message.lower()
    assert "published" not in writes


async def test_duplicate_exact_owner_names_are_refused(named_display):
    provider, desktop, _store = named_display
    original_launch = provider.helper.launch

    async def launch_then_inject_duplicate(*args):
        launched = await original_launch(*args)
        for output_id in (19, 20):
            entry = deepcopy(_audit_foreign_output())
            entry.update(id=output_id, name=OWNER_NAME)
            desktop.entries.append(entry)
        return launched

    provider.helper.launch = launch_then_inject_duplicate
    events = []
    provider.subscribe(events.append)
    result = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    assert isinstance(result, Err), result
    assert all(event.kind != "published" for event in events)
    assert not desktop.associations


async def _reload_ownership_for_teardown(provider, desktop, state_dir, *, keep_fence=True):
    # Reload durable UUIDs and connector names; retain the runtime launch fence
    # explicitly. A full process restart without that fence must still fail closed.
    store = JsonOwnershipStore(AtomicFiles(state_dir))
    verifier = desktop.output_verifier
    if keep_fence:
        verifier._ownerships = store
    else:
        verifier = KScreenOutputVerifier(provider.helper, desktop.observer, store)
    provider.remover._process_subscription.close()

    async def transport_released(*args):
        return Ok(value=None)

    remover = DisplayRemover(
        provider.helper, desktop.observer, store,
        provider.remover.creation_lock, provider.remover.clock,
        process=desktop, output_verifier=verifier, transport_released=transport_released,
    )
    recovery = DisplayRecovery(
        provider.helper, desktop.observer, store,
        remover.creation_lock, remover.clock, remover, output_verifier=verifier,
    )
    return store, recovery, remover


async def test_reloaded_owner_rederives_exact_name_for_recovery_and_removal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    state_dir = tmp_path / "state"
    store = JsonOwnershipStore(AtomicFiles(state_dir))
    provider, desktop, _ = production_display(tmp_path, verifier=True, ownerships=store)
    created = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    assert isinstance(created, Ok), created
    handle = created.value
    assert handle.source.connector == OWNER_NAME
    reloaded, recovery, remover = await _reload_ownership_for_teardown(provider, desktop, state_dir)
    records = await reloaded.all()
    assert isinstance(records, Ok)
    assert records.value[0].owner_uuid == handle.owner_uuid
    assert records.value[0].owner_uuid is not handle.owner_uuid
    assert records.value[0].connector == OWNER_NAME
    items = await recovery.reconcile()
    assert len(items) == 1 and items[0].kind == "recovering", items
    assert items[0].handle.source.connector == OWNER_NAME
    removed = await remover.remove(handle.owner_uuid, context())
    assert isinstance(removed, Ok), removed
    assert not desktop.entries
    assert not desktop.alive(handle.helper)
    assert (await reloaded.all()).value == ()


@pytest.mark.parametrize("operation", ["recovery", "removal"])
async def test_reloaded_record_with_other_owner_name_cannot_authorize_teardown(
    tmp_path, monkeypatch, operation,
):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    state_dir = tmp_path / "state"
    store = JsonOwnershipStore(AtomicFiles(state_dir))
    provider, desktop, _ = production_display(tmp_path, verifier=True, ownerships=store)
    created = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    assert isinstance(created, Ok), created
    handle = created.value
    original = (await store.all()).value[0]
    # Corrupt both the observed and saved connector consistently. Trusting the
    # persisted string instead of re-deriving from owner would accept this output.
    desktop.entries[0]["name"] = OTHER_OWNER_NAME
    tampered = replace(original, connector=OTHER_OWNER_NAME, last_applied={
        OTHER_OWNER_NAME: original.last_applied[OWNER_NAME],
    })
    assert isinstance(await store.write(tampered), Ok)
    reloaded, recovery, remover = await _reload_ownership_for_teardown(provider, desktop, state_dir)
    before = deepcopy(desktop.entries)
    configured_before = len(desktop.runs)
    if operation == "recovery":
        items = await recovery.reconcile()
        assert len(items) == 1 and items[0].kind == "cleanup_required", items
        error = items[0].diagnostic
        assert items[0].handle is None
    else:
        result = await remover.remove(handle.owner_uuid, context())
        assert isinstance(result, Err), result
        error = result.error
    assert error.code is ErrorCode.ownership_mismatch
    assert "'name'" in error.message
    assert desktop.entries == before
    assert desktop.alive(handle.helper)
    assert not [spec for spec, _ in desktop.runs[configured_before:] if spec.argv[1:] != ("-j",)]
    assert len((await reloaded.all()).value) == 1


async def test_exact_name_without_launch_fence_cannot_authorize_restart_adoption(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    state_dir = tmp_path / "state"
    store = JsonOwnershipStore(AtomicFiles(state_dir))
    provider, desktop, _ = production_display(tmp_path, verifier=True, ownerships=store)
    created = await provider.create(target().receiver_id, target().endpoint_fingerprint, HD, context())
    assert isinstance(created, Ok), created
    _reloaded, recovery, remover = await _reload_ownership_for_teardown(
        provider, desktop, state_dir, keep_fence=False,
    )
    items = await recovery.reconcile()
    assert len(items) == 1 and items[0].kind == "cleanup_required", items
    assert items[0].handle is None
    assert items[0].diagnostic.code is ErrorCode.ownership_mismatch
    refused = await remover.remove(created.value.owner_uuid, context())
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.ownership_mismatch
    assert desktop.alive(created.value.helper)
    assert desktop.entries[0]["name"] == OWNER_NAME
