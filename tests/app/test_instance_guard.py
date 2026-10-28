"""Exercise real file/process exclusion, including Qt's crash publication window."""
from __future__ import annotations

import json
import os
from pathlib import Path
import select
import subprocess
import sys
from uuid import uuid4

import pytest
from PySide6.QtCore import QLockFile

from airplay_linux.domain.operations import InstanceId
from airplay_linux.domain.resources import Err, Ok
from airplay_linux.process import LinuxProcessRunner
from airplay_linux.process.instance import InstanceGuard


GUARDED_CHILD = """
import sys
from pathlib import Path
from uuid import UUID
from airplay_linux.process import LinuxProcessRunner
from airplay_linux.process.instance import InstanceGuard
from airplay_linux.domain.resources import Ok
g = InstanceGuard(Path(sys.argv[1]), LinuxProcessRunner())
r = g.acquire(UUID(sys.argv[2]))
print('acquired' if isinstance(r, Ok) else 'blocked', flush=True)
sys.stdin.readline()
g.release()
"""
QT_CHILD = """
import sys
from PySide6.QtCore import QLockFile
q = QLockFile(sys.argv[1])
q.setStaleLockTime(0)
print('acquired' if q.tryLock(0) else 'blocked', flush=True)
sys.stdin.readline()
q.unlock()
"""


def child(script, *arguments):
    """Start a child and read the single line it publishes about the lock.

    A child that dies during import publishes nothing. Reporting that as an empty
    outcome makes the test fail as `assert '' == 'acquired'`, which hides the child's
    own traceback; the failure then looks like a scheduling flake in the guard. Surface
    the child's stderr instead so the real cause is always visible.
    """

    proc = subprocess.Popen(
        [sys.executable, '-c', script, *map(str, arguments)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    ready, _, _ = select.select([proc.stdout], [], [], 30)
    if not ready:
        proc.kill()
        proc.communicate(timeout=5)
        pytest.fail('child did not publish lock result within 30s')
    outcome = proc.stdout.readline().strip()
    if not outcome:
        proc.wait(timeout=10)
        detail = (proc.stderr.read() or '<no stderr>').strip()
        pytest.fail(
            'child published no lock result (exit %s); child stderr:\n%s'
            % (proc.returncode, detail)
        )
    return proc, outcome


def finish(proc, *, crash=False):
    if crash:
        proc.kill()
    else:
        proc.stdin.write('\n')
        proc.stdin.flush()
    proc.communicate(timeout=5)


def test_two_children_really_exclude_a_second_instance(tmp_path):
    first, outcome = child(GUARDED_CHILD, tmp_path, uuid4())
    try:
        assert outcome == 'acquired'
        second, outcome = child(GUARDED_CHILD, tmp_path, uuid4())
        finish(second)
        assert outcome == 'blocked'
    finally:
        finish(first)
    third, outcome = child(GUARDED_CHILD, tmp_path, uuid4())
    finish(third)
    assert outcome == 'acquired'


def test_qt_lock_info_remains_compatible_and_owner_evidence_is_private(tmp_path):
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    instance = InstanceId(uuid4())
    assert isinstance(guard.acquire(instance), Ok)
    path = tmp_path / 'airplay-linux.lock'
    try:
        assert QLockFile(str(path)).getLockInfo()[0] == os.getpid()
        assert str(instance).encode() in path.read_bytes()
        owner = tmp_path / 'airplay-linux.lock.owner'
        assert owner.stat().st_mode & 0o777 == 0o600
        evidence = json.loads(owner.read_bytes())
        assert evidence['instance_uuid'] == str(instance)
        assert evidence['start_ticks'] == LinuxProcessRunner().identity(os.getpid()).start_ticks
    finally:
        guard.release()
    assert not path.exists()
    assert not owner.exists()


def test_dead_verified_owner_releases_only_stable_stale_lock(tmp_path):
    first, outcome = child(GUARDED_CHILD, tmp_path, uuid4())
    assert outcome == 'acquired'
    finish(first, crash=True)
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    try:
        assert isinstance(guard.acquire(InstanceId(uuid4())), Ok)
    finally:
        guard.release()


def test_live_qt_owner_without_extension_blocks_even_if_qt_would_check_name(tmp_path):
    path = tmp_path / 'airplay-linux.lock'
    first, outcome = child(QT_CHILD, path)
    assert outcome == 'acquired'
    before = path.read_bytes()
    try:
        guard = InstanceGuard(tmp_path, LinuxProcessRunner())
        assert isinstance(guard.acquire(InstanceId(uuid4())), Err)
        assert path.read_bytes() == before
        guard.release()
        assert path.read_bytes() == before
    finally:
        finish(first)


def test_crash_between_qt_lock_and_extension_can_recover_after_confirmed_death(tmp_path):
    first, outcome = child(QT_CHILD, tmp_path / 'airplay-linux.lock')
    assert outcome == 'acquired'
    finish(first, crash=True)
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    try:
        assert isinstance(guard.acquire(InstanceId(uuid4())), Ok)
    finally:
        guard.release()


@pytest.mark.parametrize('payload', [b'', b'123', b'123\napp\nhost\n'])
def test_truncated_lock_never_becomes_permission_to_remove(tmp_path, payload):
    path = tmp_path / 'airplay-linux.lock'
    path.write_bytes(payload)
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    assert isinstance(guard.acquire(InstanceId(uuid4())), Err)
    assert path.read_bytes() == payload


def test_pid_reuse_or_live_mismatch_blocks_without_touching_evidence(tmp_path):
    first, outcome = child(GUARDED_CHILD, tmp_path, uuid4())
    assert outcome == 'acquired'
    finish(first, crash=True)
    path = tmp_path / 'airplay-linux.lock'
    payload = path.read_bytes().splitlines(keepends=True)
    payload[0] = f'{os.getpid()}\n'.encode()
    path.write_bytes(b''.join(payload))
    before = path.read_bytes()
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    assert isinstance(guard.acquire(InstanceId(uuid4())), Err)
    assert path.read_bytes() == before


def test_unreadable_live_process_is_not_treated_as_dead(tmp_path, monkeypatch):
    import airplay_linux.process.instance as module
    from airplay_linux.process.procfs import IdentityEvidence, IdentityState
    path = tmp_path / 'airplay-linux.lock'
    first, outcome = child(QT_CHILD, path)
    assert outcome == 'acquired'
    try:
        monkeypatch.setattr(module, 'read_identity_evidence', lambda pid: IdentityEvidence(
            pid=pid, state=IdentityState.unreadable,
        ))
        guard = InstanceGuard(tmp_path, LinuxProcessRunner())
        assert isinstance(guard.acquire(InstanceId(uuid4())), Err)
        assert path.exists()
    finally:
        finish(first)


def test_symlink_lock_is_never_followed_or_removed(tmp_path):
    target = tmp_path / 'untouched'
    target.write_text('private')
    (tmp_path / 'airplay-linux.lock').symlink_to(target)
    guard = InstanceGuard(tmp_path, LinuxProcessRunner())
    assert isinstance(guard.acquire(InstanceId(uuid4())), Err)
    assert target.read_text() == 'private'
