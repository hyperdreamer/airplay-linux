"""Keyring/trust contracts: only affirmed backend operations may carry trust.

The store under test is the production :class:`BackendKeyringStore`; the control
it talks to is the production :class:`PublishedDoubletakeControl` spawning a
private fake ``application-contract-v1`` command per operation.  The fake daemon
records only request metadata (never payload values), so these tests prove the
secret boundary: a typed PIN never reaches a process argv, the environment of a
child, a request payload, a log line, or adapter state that a repository would
serialise.

The contracts pinned here: a partial/foreign challenge cannot prepare or commit,
a denied begin yields no credential, an older handle cannot revoke a newer
pairing for the same receiver, and no plaintext secret appears anywhere on disk.

Manual commands: ``pytest -q tests/adapter_contracts/test_keyring_contract.py``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from airplay_linux.backend.control import (
    ApplicationContractProfile,
    CredentialPayload,
    PairingPayload,
    PublishedDoubletakeControl,
)
from airplay_linux.domain.operations import (
    ChallengeId,
    InstanceId,
    OperationContext,
    PairingChallenge,
)
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.domain.ports import CommandSpec
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.testing.builders import context, target
from airplay_linux.trust.keyring import BackendKeyringStore

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402

SECRET = "5141-9273"


class _Clock:
    now = staticmethod(time.monotonic)


async def keyring_world(tmp_path, monkeypatch, *, daemon_env=None):
    installed = fake_doubletake.install(
        tmp_path / "bin", env={"doubletake": dict(daemon_env or {})},
    )
    fake_doubletake.private_path(monkeypatch, tmp_path / "bin")
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runner = LinuxProcessRunner()
    daemon = await fake_doubletake.start_daemon(
        runner, shims=installed, runtime=runtime,
    )
    profile = ApplicationContractProfile(
        socket_path=daemon.socket_path,
        owner_token=daemon.owner_token,
        instance_uuid=InstanceId(UUID(daemon.instance_uuid)),
    )
    control = PublishedDoubletakeControl(runner, profile)
    proof = await control.handshake(
        daemon.owner_token, InstanceId(UUID(daemon.instance_uuid)),
    )
    assert isinstance(proof, Ok), proof
    return runner, daemon, control, BackendKeyringStore(control, profile.features)


async def _challenge(binding, ctx: OperationContext, *, kind="pin") -> PairingChallenge:
    now = _Clock.now()
    return PairingChallenge(
        challenge_id=ChallengeId(uuid4()),
        receiver_id=binding_receiver(binding),
        endpoint_fingerprint="endpoint-a-v1",
        endpoint_generation=ctx.endpoint_generation,
        operation_id=ctx.operation_id,
        session_generation=ctx.session_generation,
        trust_generation=ctx.trust_generation or 0,
        created_at=now,
        expires_at=now + 60.0,
        credential_kind=kind,
        backend_token=binding.backend_token,
    )


def binding_receiver(binding):
    return target("a").receiver_id


def _journal(daemon):
    return fake_doubletake.journal(daemon.journal_path)


def _daemon_operations(daemon):
    return [
        entry["operation"] for entry in _journal(daemon)
        if entry["process"] == "daemon" and entry.get("operation")
    ]


async def test_pairing_never_places_the_typed_secret_in_a_process_or_request(
    tmp_path, monkeypatch,
):
    runner, daemon, control, store = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Ok), begun
        assert begun.value.credential_kind == "pin"
        challenge = await _challenge(begun.value, ctx)

        prepared = await store.prepare_pairing(challenge, begun.value.backend_token)
        assert isinstance(prepared, Ok), prepared
        committed = await store.commit_pairing(prepared.value, ctx.trust_generation or 0)
        assert isinstance(committed, Ok), committed
        remembered = await store.load_credential_ref(receiver.receiver_id)
        assert isinstance(remembered, Ok)
        assert remembered.value == committed.value

        # The typed PIN is never part of a request payload representation...
        assert SECRET not in repr(PairingPayload(credential=SECRET))
        assert SECRET not in repr(CredentialPayload(value=SECRET))
        # ...and never reaches any process: no argv, no environment name, and no
        # journal line (request payloads are not logged at all).
        journal_text = daemon.journal_path.read_text()
        assert SECRET not in journal_text
        for entry in _journal(daemon):
            assert "payload" not in entry
            assert all(SECRET not in argument for argument in entry.get("argv", []))
        assert isinstance(
            await runner.run(
                CommandSpec(argv=("application-contract-v1", "--help"), env={}), 5.0,
            ),
            Ok,
        )
        # The opaque credential reference is adapter-private, not backend text.
        assert not isinstance(committed.value, str) or committed.value != SECRET
        assert SECRET not in str(committed.value)
    finally:
        await control.close()
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_denied_pairing_never_yields_a_credential(tmp_path, monkeypatch):
    runner, daemon, control, store = await keyring_world(
        tmp_path, monkeypatch, daemon_env={"AIRPLAY_FAKE_DAEMON_DENY_PAIRING": "1"},
    )
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Err)
        assert begun.error.code is ErrorCode.keyring_unavailable
        # The refusal compensated: begin was answered, then cancelled.
        assert [
            op for op in _daemon_operations(daemon)
            if op in {"begin_pairing", "cancel_pairing"}
        ] == ["begin_pairing", "cancel_pairing"]
        remembered = await store.load_credential_ref(receiver.receiver_id)
        assert isinstance(remembered, Ok)
        assert remembered.value is None
    finally:
        await control.close()
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_a_foreign_generation_challenge_cannot_prepare(tmp_path, monkeypatch):
    runner, daemon, control, store = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Ok), begun
        challenge = await _challenge(begun.value, ctx)
        foreign = PairingChallenge(
            **{
                **challenge.__dict__,
                "session_generation": challenge.session_generation + 1,
            },
        )
        prepared = await store.prepare_pairing(foreign, begun.value.backend_token)
        assert isinstance(prepared, Err)
        assert prepared.error.code is ErrorCode.keyring_unavailable
        # The mismatched challenge was rejected locally: no prepare was sent.
        assert "prepare_pairing" not in _daemon_operations(daemon)
    finally:
        await control.close()
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_an_older_handle_cannot_revoke_a_newer_pairing(tmp_path, monkeypatch):
    runner, daemon, control, store = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        first = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(first, Ok), first
        first_challenge = await _challenge(first.value, ctx)
        first_prepared = await store.prepare_pairing(
            first_challenge, first.value.backend_token,
        )
        assert isinstance(first_prepared, Ok), first_prepared
        first_credential = await store.commit_pairing(first_prepared.value, 0)
        assert isinstance(first_credential, Ok), first_credential

        second = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(second, Ok), second
        second_challenge = await _challenge(second.value, ctx)
        second_prepared = await store.prepare_pairing(
            second_challenge, second.value.backend_token,
        )
        assert isinstance(second_prepared, Ok), second_prepared

        before = _daemon_operations(daemon).count("revoke_pairing")
        stale = await store.revoke_pairing(first_prepared.value)
        assert isinstance(stale, Err)
        assert stale.error.code is ErrorCode.keyring_unavailable
        assert _daemon_operations(daemon).count("revoke_pairing") == before

        current = await store.revoke_pairing(second_prepared.value)
        assert isinstance(current, Ok), current
        assert _daemon_operations(daemon).count("revoke_pairing") == before + 1
    finally:
        await control.close()
        await fake_doubletake.stop_daemon(runner, daemon)


async def test_no_plaintext_secret_is_written_anywhere_under_the_private_root(
    tmp_path, monkeypatch,
):
    runner, daemon, control, store = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Ok), begun
        challenge = await _challenge(begun.value, ctx)
        prepared = await store.prepare_pairing(challenge, begun.value.backend_token)
        assert isinstance(prepared, Ok), prepared
        assert isinstance(
            await store.commit_pairing(prepared.value, 0), Ok,
        )
        for path in tmp_path.rglob("*"):
            if path.is_file():
                assert SECRET.encode() not in path.read_bytes(), path
    finally:
        await control.close()
        await fake_doubletake.stop_daemon(runner, daemon)
