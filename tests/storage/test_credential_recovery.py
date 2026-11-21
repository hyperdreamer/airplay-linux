"""Recovery markers are durable evidence, never a credential or a status guess."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from airplay_linux.domain import records
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.relay import TerminationProof
from airplay_linux.domain.resources import Err, ErrorCode, Ok, RecoveryAction
from airplay_linux.storage import AtomicFiles, PosixFilesystem
from airplay_linux.storage import codecs, repositories

CANARY = "credential-canary-recovery-7f3da9"
TARGET = "192.0.2.41"
OLD_INSTANCE = UUID("11111111-1111-4111-8111-111111111111")
OLD_LAUNCH = UUID("22222222-2222-4222-8222-222222222222")
START = UUID("33333333-3333-4333-8333-333333333333")
ATTEMPT = UUID("44444444-4444-4444-8444-444444444444")
NEW_INSTANCE = UUID("55555555-5555-4555-8555-555555555555")
NEW_LAUNCH = UUID("66666666-6666-4666-8666-666666666666")


def marker(target=TARGET, generation=1, start=START):
    assert hasattr(records, "CredentialRecoveryMarker"), "missing recovery marker record"
    old = records.OriginalDaemonIdentity(
        launch_uuid=OLD_LAUNCH, instance_uuid=OLD_INSTANCE, endpoint_generation=4,
        daemon_process=ProcessIdentity(pid=101, start_ticks=501),
        runtime_directory="/run/user/1000/old", socket_path="/run/user/1000/old/control.sock",
    )
    return records.CredentialRecoveryMarker(
        schema_version=1, target=target, receiver_id="airplay-device:living-room",
        start_operation_id=start, original_daemon=old, marker_generation=generation,
        state="watching", automatic_status_checks=True, release_attempt=None, retirement=None,
    )


def attempt():
    return records.ReleaseAttempt(
        attempt_id=ATTEMPT, replacement_runtime_directory="/run/user/1000/new",
        replacement_socket_path="/run/user/1000/new/control.sock", status="intended",
        replacement_instance_uuid=None, replacement_endpoint_generation=None,
        readiness=None, terminal_evidence=None,
    )


def readiness():
    return records.OwnedDaemonReadinessEvidence(
        launch_uuid=NEW_LAUNCH, recovery_attempt_id=ATTEMPT, instance_uuid=NEW_INSTANCE,
        endpoint_generation=5, runtime_directory="/run/user/1000/new",
        socket_path="/run/user/1000/new/control.sock", daemon_pid=102,
        daemon_start_ticks=502, socket_device=6, socket_inode=70, verified_at=10.0,
    )


def proof():
    return TerminationProof(
        daemon_instance_uuid=OLD_INSTANCE, daemon=ProcessIdentity(pid=101, start_ticks=501),
        runtime_directory="/run/user/1000/old", launch_uuid=OLD_LAUNCH,
        receipt_state="stopped", cleanup_outcome="stopped", cleanup_completed_at=8.0,
        process_evidence="pidfd_exited", identity_verified_at=7.0, observed_at=9.0,
    )


def release():
    return records.ReleaseEvidence(
        old_daemon_termination=proof(), replacement_instance_uuid=NEW_INSTANCE,
        replacement_endpoint_generation=5, replacement_runtime_directory="/run/user/1000/new",
        replacement_socket_path="/run/user/1000/new/control.sock", readiness=readiness(),
    )


def key(target=TARGET):
    return "credential-recovery/" + sha256(target.encode()).hexdigest() + ".json"


def assert_failure(result, code=ErrorCode.persistence_failed):
    assert isinstance(result, Err)
    assert result.error.code == code
    assert result.error.message == "Recovery state could not be verified."
    assert result.error.recovery == (RecoveryAction.keep_blocked,)
    assert CANARY not in repr(result)


async def test_create_publishes_hashed_secret_free_record(tmp_path):
    item = marker()
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    assert await store.create(item) == Ok(value=item)
    assert await store.for_target(TARGET) == Ok(value=item)
    assert await store.read_all() == Ok(value=(item,))
    payload = (tmp_path / key()).read_bytes()
    assert CANARY.encode() not in payload
    assert codecs.decode_credential_recovery_marker(payload) == item
    assert [entry.name for entry in (tmp_path / "credential-recovery").iterdir()] == [key().split("/")[-1]]


async def test_conflicts_and_atomic_dismissal_preserve_tombstone(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    item = marker()
    assert await store.create(item) == Ok(value=item)
    assert_failure(await store.create(replace(item, start_operation_id=ATTEMPT)), ErrorCode.ownership_mismatch)
    assert_failure(await store.dismiss(item, 2), ErrorCode.ownership_mismatch)
    pending = (await store.mark_release_pending(item, 1)).value
    assert (pending.marker_generation, pending.state, pending.automatic_status_checks) == (2, "release_pending", True)
    dismissed = (await store.dismiss(pending, 2)).value
    assert (dismissed.marker_generation, dismissed.state, dismissed.automatic_status_checks) == (3, "release_pending", False)
    still_pending = (await store.mark_release_pending(dismissed, 3)).value
    assert not still_pending.automatic_status_checks
    evidence = records.NoSpawnRetirement(failure=ErrorCode.daemon_failed, stop_cleanup_settled=True, observed_at=2.0)
    assert_failure(await store.retire_no_spawn(still_pending, 4, evidence), ErrorCode.ownership_mismatch)
    other = marker(target="192.0.2.42")
    assert isinstance(await store.create(other), Ok)
    retired = (await store.retire_no_spawn(other, 1, evidence)).value
    assert retired.marker_generation == 2 and retired.state == "retired"
    assert retired.retirement.no_spawn == evidence and not retired.automatic_status_checks
    assert_failure(await store.create(marker(target=other.target, generation=2)), ErrorCode.ownership_mismatch)
    newer = marker(target=other.target, generation=3, start=ATTEMPT)
    assert await store.create(newer) == Ok(value=newer)


async def test_reply_retirement_requires_terminal_evidence_and_settled_cleanup(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    item = marker()
    assert isinstance(await store.create(item), Ok)
    invalid = records.TerminalReplyRetirement(reply_at=1.0, accepted=True, target_observation="absent", observation_at=2.0, stop_cleanup_settled=False)
    assert_failure(await store.retire_after_reply(item, 1, invalid), ErrorCode.ownership_mismatch)
    valid = replace(invalid, stop_cleanup_settled=True)
    retired = (await store.retire_after_reply(item, 1, valid)).value
    assert retired.retirement.kind == "terminal_reply"
    assert retired.retirement.reply == valid


async def test_replacement_retirement_requires_exact_old_identity_and_ready_attempt(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    item = marker()
    assert isinstance(await store.create(item), Ok)
    pending = (await store.mark_release_pending(item, 1)).value
    intended = (await store.record_release_attempt(pending, 2, attempt())).value
    assert intended.release_attempt == attempt()
    assert_failure(await store.record_replacement_ready(intended, 3, replace(readiness(), recovery_attempt_id=START)), ErrorCode.ownership_mismatch)
    for invalid_ready in (
        replace(readiness(), launch_uuid=OLD_LAUNCH),
        replace(readiness(), daemon_pid=101, daemon_start_ticks=501),
        replace(readiness(), instance_uuid=OLD_INSTANCE),
        replace(readiness(), socket_path="/run/user/1000/old/control.sock"),
    ):
        assert_failure(await store.record_replacement_ready(intended, 3, invalid_ready), ErrorCode.ownership_mismatch)
    ready = (await store.record_replacement_ready(intended, 3, readiness())).value
    assert ready.release_attempt.status == "ready"
    wrong = replace(release(), old_daemon_termination=replace(proof(), daemon=ProcessIdentity(pid=103, start_ticks=501)))
    assert_failure(await store.retire_after_replacement(ready, 4, wrong), ErrorCode.ownership_mismatch)
    retired = (await store.retire_after_replacement(ready, 4, release())).value
    assert retired.state == "retired" and retired.retirement.release == release()


@pytest.mark.parametrize("bad", ["unknown", "mismatch", "malformed", "hidden"])
async def test_invalid_visible_entries_fail_closed(tmp_path, bad):
    files = AtomicFiles(tmp_path)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    directory = tmp_path / "credential-recovery"
    if bad == "unknown":
        (directory / "unknown.txt").write_text(CANARY)
    elif bad == "hidden":
        (directory / ".unexpected").write_text(CANARY)
    elif bad == "mismatch":
        (tmp_path / key()).rename(directory / ("0" * 64 + ".json"))
    else:
        (tmp_path / key()).write_text('{"secret":"' + CANARY + '"}')
    assert_failure(await store.read_all())
    assert_failure(await store.for_target(TARGET))
    assert_failure(await store.create(marker(target="192.0.2.42")))


class FailDirectoryConfirmation(PosixFilesystem):
    def __init__(self):
        self.armed = False
        self.renames = 0
        self.confirmations = 0

    def replace(self, parent, source, destination):
        super().replace(parent, source, destination)
        self.renames += 1

    def fsync_directory(self, descriptor):
        self.confirmations += 1
        if self.armed:
            raise OSError(CANARY)
        super().fsync_directory(descriptor)


async def test_post_rename_retry_requires_fresh_confirmation_and_preserves_bytes(tmp_path):
    fs = FailDirectoryConfirmation()
    files = AtomicFiles(tmp_path, fs)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    fs.armed = True
    assert_failure(await store.dismiss(item, 1))
    visible = files.read(key())
    assert codecs.decode_credential_recovery_marker(visible).state == "release_pending"
    before = (fs.renames, fs.confirmations)
    assert_failure(await store.dismiss(item, 1))
    assert files.read(key()) == visible
    assert fs.renames > before[0] and fs.confirmations > before[1]
    assert_failure(await store.create(marker(generation=3, start=ATTEMPT)), ErrorCode.ownership_mismatch)
    assert_failure(await store.create(marker(target="192.0.2.99", start=ATTEMPT)))
    fs.armed = False
    result = await store.dismiss(item, 1)
    assert isinstance(result, Ok)
    assert files.read(key()) == visible


async def test_restart_confirms_each_active_and_retired_record(tmp_path):
    fs = FailDirectoryConfirmation()
    files = AtomicFiles(tmp_path, fs)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    one = marker()
    two = marker(target="192.0.2.42")
    assert isinstance(await store.create(one), Ok)
    assert isinstance(await store.create(two), Ok)
    retired = (await store.retire_no_spawn(one, 1, records.NoSpawnRetirement(failure=ErrorCode.daemon_failed, stop_cleanup_settled=True, observed_at=3.0))).value
    reopened = repositories.JsonCredentialRecoveryMarkerStore(files)
    assert await reopened.read_all() == Ok(value=tuple(sorted((retired, two), key=lambda m: key(m.target))))
    fs.armed = True
    assert_failure(await reopened.confirm_all_durable())
    fs.armed = False
    before = fs.renames
    assert await reopened.confirm_all_durable() == Ok(value=None)
    assert fs.renames - before == 2


async def test_concurrent_same_target_creation_has_one_owner(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    first = marker()
    second = marker(start=ATTEMPT)
    results = await asyncio.gather(store.create(first), store.create(second))
    assert sum(isinstance(result, Ok) for result in results) == 1
    assert sum(isinstance(result, Err) for result in results) == 1
    assert (await store.for_target(TARGET)).value in (first, second)


async def test_reservation_exists_before_waiting_for_store_lock(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    await store._lock.acquire()
    first = asyncio.create_task(store.create(marker()))
    await asyncio.sleep(0)
    assert store._reservations.get(TARGET) == marker()
    assert_failure(await store.create(marker(start=ATTEMPT)), ErrorCode.ownership_mismatch)
    assert_failure(await asyncio.wait_for(store.create(marker()), timeout=0.1), ErrorCode.ownership_mismatch)
    assert not (tmp_path / key()).exists()
    store._lock.release()
    assert isinstance(await first, Ok)


async def test_retry_after_ambiguous_tombstone_replacement(tmp_path):
    fs = FailDirectoryConfirmation()
    files = AtomicFiles(tmp_path, fs)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    evidence = records.NoSpawnRetirement(failure=ErrorCode.daemon_failed, stop_cleanup_settled=True, observed_at=2.0)
    assert isinstance(await store.retire_no_spawn(item, 1, evidence), Ok)
    replacement = marker(generation=3, start=ATTEMPT)
    fs.armed = True
    assert_failure(await store.create(replacement))
    visible = files.read(key())
    before = fs.confirmations
    assert_failure(await store.create(replacement))
    assert fs.confirmations > before and files.read(key()) == visible
    fs.armed = False
    assert await store.create(replacement) == Ok(value=replacement)
    assert files.read(key()) == visible


async def test_retry_refuses_changed_visible_record(tmp_path):
    fs = FailDirectoryConfirmation()
    files = AtomicFiles(tmp_path, fs)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    fs.armed = True
    assert_failure(await store.dismiss(item, 1))
    visible = codecs.decode_credential_recovery_marker(files.read(key()))
    changed = codecs.encode_credential_recovery_marker(replace(visible, start_operation_id=ATTEMPT))
    (tmp_path / key()).write_bytes(changed)
    fs.armed = False
    assert_failure(await store.dismiss(item, 1))
    assert files.read(key()) == changed
    assert_failure(await store.confirm_all_durable())
    assert files.read(key()) == changed


@pytest.mark.parametrize("failure", ["read", "duplicate"])
async def test_unreadable_and_duplicate_evidence_fail_closed(tmp_path, failure):
    class BadReadFiles(AtomicFiles):
        def names(self, relative, *, include_hidden=False):
            names = super().names(relative, include_hidden=include_hidden)
            return names + names if failure == "duplicate" else names

        def read(self, relative):
            if failure == "read" and relative.startswith("credential-recovery/"):
                from airplay_linux.storage.atomic import StorageError
                raise StorageError("read")
            return super().read(relative)

    regular = AtomicFiles(tmp_path)
    assert isinstance(await repositories.JsonCredentialRecoveryMarkerStore(regular).create(marker()), Ok)
    store = repositories.JsonCredentialRecoveryMarkerStore(BadReadFiles(tmp_path))
    assert_failure(await store.read_all())
    assert_failure(await store.confirm_all_durable())


def test_recovery_codec_rejects_missing_extra_and_contradictory_fields():
    item = marker()
    encoded = codecs.encode_credential_recovery_marker(item)
    document = json.loads(encoded)
    assert codecs.decode_credential_recovery_marker(encoded) == item
    for field in document:
        bad = dict(document)
        del bad[field]
        with pytest.raises(codecs.RecordError):
            codecs.decode_credential_recovery_marker(json.dumps(bad).encode())
    bad_documents = [
        document | {"credential": CANARY},
        document | {"schema_version": True},
        document | {"marker_generation": 0},
        document | {"state": "retired"},
        document | {"automatic_status_checks": False},
        document | {"original_daemon": document["original_daemon"] | {"daemon_process": {"pid": True, "start_ticks": 1}}},
        document | {"original_daemon": document["original_daemon"] | {"socket_path": "../credential"}},
    ]
    for bad in bad_documents:
        with pytest.raises(codecs.RecordError) as error:
            codecs.decode_credential_recovery_marker(json.dumps(bad).encode())
        assert CANARY not in str(error.value)
    duplicated = b'{"schema":"airplay-linux/credential-recovery",' + encoded[1:]
    with pytest.raises(codecs.RecordError):
        codecs.decode_credential_recovery_marker(duplicated)


async def test_release_pending_absent_observation_never_clears_marker(tmp_path):
    files = AtomicFiles(tmp_path)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    pending = (await store.mark_release_pending(item, 1)).value
    before = files.read(key())
    complete_reply = records.TerminalReplyRetirement(
        reply_at=1.0, accepted=False, target_observation="absent", observation_at=2.0,
        stop_cleanup_settled=True,
    )
    assert_failure(await store.retire_after_reply(pending, 2, complete_reply), ErrorCode.ownership_mismatch)
    assert files.read(key()) == before
    assert await store.for_target(TARGET) == Ok(value=pending)


async def test_invalid_target_queries_fail_closed(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    for target in (None, [], "", "a\x00b", "\ud800", "a" * 4097):
        assert_failure(await store.for_target(target))


async def test_identity_and_evidence_mismatches_do_not_write(tmp_path):
    files = AtomicFiles(tmp_path)
    store = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await store.create(item), Ok)
    original = files.read(key())
    for wrong in (
        replace(item, receiver_id="airplay-device:other"),
        replace(item, start_operation_id=ATTEMPT),
        replace(item, original_daemon=replace(item.original_daemon, endpoint_generation=5)),
        replace(item, original_daemon=replace(item.original_daemon, socket_path="/run/user/1000/old/other.sock")),
        replace(item, target=[]),
    ):
        assert_failure(await store.dismiss(wrong, 1), ErrorCode.ownership_mismatch)
        assert files.read(key()) == original
    assert_failure(await store.retire_no_spawn(item, 1, "no_spawn"), ErrorCode.ownership_mismatch)
    bad = records.NoSpawnRetirement(failure=CANARY, stop_cleanup_settled=True, observed_at=2.0)
    assert_failure(await store.retire_no_spawn(item, 1, bad), ErrorCode.ownership_mismatch)
    assert files.read(key()) == original


async def test_reply_observation_must_follow_acknowledgement(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    item = marker()
    assert isinstance(await store.create(item), Ok)
    evidence = records.TerminalReplyRetirement(
        reply_at=3.0, accepted=True, target_observation="streaming", observation_at=2.0,
        stop_cleanup_settled=True,
    )
    assert_failure(await store.retire_after_reply(item, 1, evidence), ErrorCode.ownership_mismatch)
    assert await store.for_target(TARGET) == Ok(value=item)


async def test_dismiss_is_one_publication(tmp_path):
    fs = FailDirectoryConfirmation()
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path, fs))
    item = marker()
    assert isinstance(await store.create(item), Ok)
    before = fs.renames
    result = await store.dismiss(item, 1)
    assert isinstance(result, Ok)
    assert fs.renames == before + 1
    assert result.value.state == "release_pending" and not result.value.automatic_status_checks


async def test_restart_after_ambiguous_retirement_requires_confirmation(tmp_path):
    fs = FailDirectoryConfirmation()
    files = AtomicFiles(tmp_path, fs)
    original = repositories.JsonCredentialRecoveryMarkerStore(files)
    item = marker()
    assert isinstance(await original.create(item), Ok)
    fs.armed = True
    evidence = records.NoSpawnRetirement(failure=ErrorCode.daemon_failed, stop_cleanup_settled=True, observed_at=2.0)
    assert_failure(await original.retire_no_spawn(item, 1, evidence))
    raw = files.read(key())
    assert codecs.decode_credential_recovery_marker(raw).state == "retired"
    restarted = repositories.JsonCredentialRecoveryMarkerStore(files)
    assert_failure(await restarted.confirm_all_durable())
    assert files.read(key()) == raw
    fs.armed = False
    before = fs.confirmations
    assert await restarted.confirm_all_durable() == Ok(value=None)
    assert fs.confirmations > before and files.read(key()) == raw


async def test_startup_confirmation_preserves_waiting_creation_reservation(tmp_path):
    class TrackingReservations(dict):
        prematurely_removed = False

        def clear(self):
            if TARGET in self and not (tmp_path / key()).exists():
                self.prematurely_removed = True
            super().clear()

        def pop(self, target, default=None):
            if target == TARGET and target in self and not (tmp_path / key()).exists():
                self.prematurely_removed = True
            return super().pop(target, default)

    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    reservations = TrackingReservations()
    store._reservations = reservations
    await store._lock.acquire()
    confirmation = asyncio.create_task(store.confirm_all_durable())
    await asyncio.sleep(0)
    creation = asyncio.create_task(store.create(marker()))
    await asyncio.sleep(0)
    assert reservations[TARGET] == marker()
    assert_failure(await store.create(marker(start=ATTEMPT)), ErrorCode.ownership_mismatch)
    store._lock.release()
    assert await confirmation == Ok(value=None)
    assert isinstance(await creation, Ok)
    assert not reservations.prematurely_removed


async def test_release_attempt_terminal_retry_requires_fresh_identity_and_endpoint(tmp_path):
    store = repositories.JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path))
    item = marker()
    assert isinstance(await store.create(item), Ok)
    pending = (await store.dismiss(item, 1)).value
    intended = (await store.record_release_attempt(pending, 2, attempt())).value
    terminal_evidence = records.ReleaseAttemptTerminalEvidence(
        outcome="not_spawned", replacement_launch_uuid=None, process_evidence="no_child", verified_at=2.0,
    )
    terminal_attempt = replace(attempt(), status="terminal", terminal_evidence=terminal_evidence)
    terminal = (await store.record_release_attempt(intended, 3, terminal_attempt)).value
    assert terminal.release_attempt == terminal_attempt and terminal.marker_generation == 4
    reused_id = replace(attempt(), replacement_runtime_directory="/run/user/1000/newer", replacement_socket_path="/run/user/1000/newer/control.sock")
    assert_failure(await store.record_release_attempt(terminal, 4, reused_id), ErrorCode.ownership_mismatch)
    fresh = replace(reused_id, attempt_id=START)
    restarted = (await store.record_release_attempt(terminal, 4, fresh)).value
    assert restarted.release_attempt == fresh and restarted.marker_generation == 5


def test_codec_rejects_retirement_with_conflicting_attempt_and_reply_order():
    no_spawn = records.NoSpawnRetirement(failure=ErrorCode.daemon_failed, stop_cleanup_settled=True, observed_at=2.0)
    retirement = records.MarkerRetirement(kind="no_spawn", no_spawn=no_spawn, reply=None, release=None)
    invalid = replace(marker(), state="retired", automatic_status_checks=False, release_attempt=attempt(), retirement=retirement)
    with pytest.raises(codecs.RecordError):
        codecs.encode_credential_recovery_marker(invalid)
    reply = records.TerminalReplyRetirement(reply_at=3.0, accepted=True, target_observation="streaming", observation_at=2.0, stop_cleanup_settled=True)
    invalid = replace(marker(), state="retired", automatic_status_checks=False,
                      retirement=records.MarkerRetirement(kind="terminal_reply", no_spawn=None, reply=reply, release=None))
    with pytest.raises(codecs.RecordError):
        codecs.encode_credential_recovery_marker(invalid)
