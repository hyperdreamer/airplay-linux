"""Keyring/trust contracts: only affirmed backend operations may carry trust.

The store under test is the production :class:`BackendKeyringStore`, the
coordinator is the production :class:`PairingCoordinator`, and the control it
talks to is the production :class:`PublishedDoubletakeControl` spawning a private
fake ``application-contract-v1`` command per operation.  The fake daemon records
only request metadata (never payload values) in its ordinary journal.

The secret boundary is proved by *submitting a real typed secret* through the
real submit boundary (``PairingCoordinator.submit_pairing`` ->
``PublishedDoubletakeControl.request(submit_pairing, PairingPayload)``), not by
inspecting an empty-payload flow.  For that test the fixtures additionally opt in
to private boundary capture: the actual process argv and environment values, and
the exact request bytes the client sends / the daemon receives.  Those captured
files are themselves scanned, so a regression that leaks the credential into any
of those channels fails the test.

The contracts pinned here: a real canary submission never reaches argv, child
environment, private request bytes, the journal, or on-disk state; a
partial/foreign challenge cannot prepare or commit; a denied begin yields no
credential and a refused compensation retains durable cleanup intent; concurrent
prompts stay target-local; an older handle cannot revoke a newer pairing.

Manual commands: ``pytest -q tests/adapter_contracts/test_keyring_contract.py``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from airplay_linux.backend.control import (
    ApplicationContractProfile,
    CredentialPayload,
    PairingPayload,
    PublishedDoubletakeControl,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import (
    ChallengeId,
    InstanceId,
    OperationContext,
    PairingChallenge,
    SensitiveText,
)
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.domain.ports import CommandSpec
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.storage import AtomicFiles, JsonReceiverStore
from airplay_linux.testing.builders import context, target
from airplay_linux.trust.coordinator import PairingCoordinator
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore

PROCESS_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
if str(PROCESS_FIXTURES) not in sys.path:
    sys.path.insert(0, str(PROCESS_FIXTURES))

import fake_doubletake  # noqa: E402

SECRET = "5141-9273"
#: The captured boundary files are scanned for these exact bytes; a random value
#: cannot collide with a path, PID, or request id by accident.
CANARY = f"canary-{uuid4().hex}"


class _Clock:
    now = staticmethod(time.monotonic)


class _Scheduler:
    def call_at(self, when, callback):
        return asyncio.get_running_loop().call_at(when, callback)


class _Ids:
    new = staticmethod(uuid4)


@dataclass
class KeyringWorld:
    runner: LinuxProcessRunner
    daemon: object
    control: object
    store: BackendKeyringStore
    capture: Path

    def journal(self) -> list[dict]:
        return fake_doubletake.journal(self.daemon.journal_path)

    def daemon_operations(self) -> list[str]:
        return [
            entry["operation"] for entry in self.journal()
            if entry["process"] == "daemon" and entry.get("operation")
        ]

    def capture_files(self) -> list[Path]:
        return sorted(
            path for path in self.capture.rglob("*") if path.is_file()
        )

    def capture_leaks(self, needle: bytes) -> list[Path]:
        return [
            path for path in self.capture_files() if needle in path.read_bytes()
        ]


@dataclass
class PairingWorld(KeyringWorld):
    coordinator: PairingCoordinator
    fences: TrustFenceRegistry
    state_root: Path
    refusals: list[str] = field(default_factory=list)

    def fence(self, receiver) -> None:
        self.fences.set(TrustFence(
            receiver_id=receiver.receiver_id,
            endpoint_fingerprint=receiver.endpoint_fingerprint,
            endpoint_generation=1,
            session_generation=1,
            trust_generation=0,
        ))


class _CompensationRefuser:
    """A control proxy that refuses selected trust operations exactly once.

    It is the controlled fault boundary for a refused compensation: every other
    call reaches the real production control unchanged, and each refused call is
    recorded so the test can prove it never reached the backend.
    """

    def __init__(self, inner, refuse: dict[str, int]):
        self._inner = inner
        self._remaining = dict(refuse)
        self.refused: list[str] = []

    async def request(self, operation, target, payload, context, **kwargs):
        name = getattr(operation, "value", operation)
        if self._remaining.get(name, 0) > 0:
            self._remaining[name] -= 1
            self.refused.append(name)
            return Err(error=diagnostic(ErrorCode.keyring_unavailable))
        return await self._inner.request(operation, target, payload, context, **kwargs)

    def subscribe(self, callback):
        return self._inner.subscribe(callback)

    async def handshake(self, owner_token, instance_uuid):
        return await self._inner.handshake(owner_token, instance_uuid)

    async def close(self):
        return await self._inner.close()


async def _start_world(
    tmp_path, monkeypatch, *, capture: bool, daemon_env=None,
) -> KeyringWorld:
    capture_root = tmp_path / "capture"
    capture_root.mkdir(mode=0o700)
    boundary = {
        fake_doubletake.DAEMON_CAPTURE_ENV: str(capture_root),
        fake_doubletake.CTL_CAPTURE_ENV: str(capture_root),
    } if capture else {}
    installed = fake_doubletake.install(
        tmp_path / "bin",
        env={
            "doubletake": {**boundary, **(daemon_env or {})},
            "doubletake-ctl": dict(boundary),
            "application-contract-v1": dict(boundary),
        },
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
    return KeyringWorld(
        runner=runner,
        daemon=daemon,
        control=control,
        store=BackendKeyringStore(control, profile.features),
        capture=capture_root,
    )


async def keyring_world(tmp_path, monkeypatch, *, daemon_env=None) -> KeyringWorld:
    return await _start_world(
        tmp_path, monkeypatch, capture=False, daemon_env=daemon_env,
    )


async def pairing_world(
    tmp_path, monkeypatch, *, capture=True, daemon_env=None, refuse_compensation=None,
) -> PairingWorld:
    """A coordinator over the real store/control with an explicit trust fence."""
    base = await _start_world(
        tmp_path, monkeypatch, capture=capture, daemon_env=daemon_env,
    )
    control = base.control
    if refuse_compensation:
        control = _CompensationRefuser(control, refuse_compensation)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    store = BackendKeyringStore(control, base.control._profile.features)
    fences = TrustFenceRegistry()
    coordinator = PairingCoordinator(
        control,
        store,
        JsonReceiverStore(AtomicFiles(state_root)),
        fences,
        _Clock(),
        _Scheduler(),
        _Ids(),
    )
    return PairingWorld(
        runner=base.runner,
        daemon=base.daemon,
        control=control,
        store=store,
        capture=base.capture,
        coordinator=coordinator,
        fences=fences,
        state_root=state_root,
        refusals=control.refused if isinstance(control, _CompensationRefuser) else [],
    )


async def _close(world: KeyringWorld) -> None:
    await world.control.close()
    await fake_doubletake.stop_daemon(world.runner, world.daemon)


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


def _captured_submissions(capture: Path) -> list[dict]:
    path = capture / "ctl-argv-env.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("operation") == "submit_pairing":
            entries.append(entry)
    return entries


def _captured_requests(capture: Path) -> list[dict]:
    path = capture / "daemon-requests.jsonl"
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


# ---------------------------------------------------------------------------
# Secret boundary: a real typed secret is submitted through the real boundary.
# ---------------------------------------------------------------------------


async def test_pairing_never_places_the_typed_secret_in_a_process_or_request(
    tmp_path, monkeypatch,
):
    """A real canary submission must not reach argv, env, or private bytes.

    This drives ``PairingCoordinator.submit_pairing`` with a unique
    :class:`SensitiveText`.  The fake ctl records its real argv and environment,
    and both the client and daemon record the exact request bytes; every one of
    those files is scanned for the canary.  The submission is required to
    succeed, so the assertions cannot pass by never sending anything.
    """
    world = await pairing_world(
        tmp_path, monkeypatch,
        daemon_env={fake_doubletake.DAEMON_CREDENTIAL_KIND_ENV: "password"},
    )
    receiver = target("a")
    world.fence(receiver)
    ctx = context(generation=1, lease=None)
    canary = CANARY
    try:
        begun = await world.coordinator.begin_pairing(receiver, ctx)
        assert isinstance(begun, Ok), begun
        challenge = begun.value
        assert challenge.credential_kind == "password"

        submitted = await world.coordinator.submit_pairing(
            challenge.challenge_id, SensitiveText(canary), ctx,
        )
        # The secret really crossed the submit boundary and was accepted.
        assert submitted.kind == "Trusted", submitted
        assert submitted.credential_ref is not None

        # Positive controls: the captured boundary really saw this submission.
        submissions = _captured_submissions(world.capture)
        assert [entry["operation"] for entry in submissions] == ["submit_pairing"]
        assert submissions[0]["argv"], "ctl capture has no real argv"
        assert isinstance(submissions[0]["env"], dict)
        requests = _captured_requests(world.capture)
        assert any(
            entry.get("operation") == "submit_pairing" for entry in requests
        ), "the daemon capture has no submit_pairing request"
        assert "submit_pairing" in _daemon_operations(world.daemon)

        # The typed secret is never represented by a payload repr...
        assert canary not in repr(PairingPayload(credential=canary))
        assert canary not in repr(CredentialPayload(value=canary))
        # ...never appears in the captured process argv or environment...
        for entry in submissions:
            assert all(canary not in argument for argument in entry["argv"])
            assert all(canary not in name for name in entry["env"])
            assert all(canary not in value for value in entry["env"].values())
        # ...and never in the exact private request bytes either side observed.
        assert world.capture_leaks(canary.encode()) == []
        assert canary not in world.daemon.journal_path.read_text()
        # Adapter state renders without the secret as well.
        assert canary not in repr(world.store)
        assert canary not in repr(world.coordinator)
        assert canary not in repr(submitted)
    finally:
        await _close(world)


async def test_no_plaintext_secret_is_written_anywhere_under_the_private_root(
    tmp_path, monkeypatch,
):
    world = await pairing_world(
        tmp_path, monkeypatch,
        daemon_env={fake_doubletake.DAEMON_CREDENTIAL_KIND_ENV: "password"},
    )
    receiver = target("a")
    world.fence(receiver)
    ctx = context(generation=1, lease=None)
    canary = f"private-canary-{uuid4().hex}"
    try:
        begun = await world.coordinator.begin_pairing(receiver, ctx)
        assert isinstance(begun, Ok), begun
        submitted = await world.coordinator.submit_pairing(
            begun.value.challenge_id, SensitiveText(canary), ctx,
        )
        assert submitted.kind == "Trusted", submitted
        leaks = [
            path for path in tmp_path.rglob("*")
            if path.is_file() and canary.encode() in path.read_bytes()
        ]
        assert leaks == [], leaks
    finally:
        await _close(world)


# ---------------------------------------------------------------------------
# Store-level trust transactions.
# ---------------------------------------------------------------------------


async def test_denied_pairing_never_yields_a_credential(tmp_path, monkeypatch):
    world = await keyring_world(
        tmp_path, monkeypatch, daemon_env={"AIRPLAY_FAKE_DAEMON_DENY_PAIRING": "1"},
    )
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await world.store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Err)
        assert begun.error.code is ErrorCode.keyring_unavailable
        # The refusal compensated: begin was answered, then cancelled.
        assert [
            op for op in _daemon_operations(world.daemon)
            if op in {"begin_pairing", "cancel_pairing"}
        ] == ["begin_pairing", "cancel_pairing"]
        remembered = await world.store.load_credential_ref(receiver.receiver_id)
        assert isinstance(remembered, Ok)
        assert remembered.value is None
    finally:
        await _close(world)


async def test_a_refused_compensation_retains_exact_cleanup_intent(
    tmp_path, monkeypatch,
):
    """A refused cancel/revoke is not success: the barrier must be retried.

    ``begin`` acquires a prompt whose status is unusable, so the store must
    compensate by cancelling and then revoking.  Both are refused once by the
    controlled proxy, so the coordinator may not report or drop the residual
    trust.  Only an explicit retry that finally reaches the backend clears it.
    """
    world = await pairing_world(
        tmp_path, monkeypatch,
        daemon_env={fake_doubletake.DAEMON_BEGIN_NO_PROMPT_ENV: "1"},
        refuse_compensation={"cancel_pairing": 1, "revoke_pairing": 1},
    )
    receiver = target("a")
    world.fence(receiver)
    ctx = context(generation=1, lease=None)
    try:
        begun = await world.coordinator.begin_pairing(receiver, ctx)
        assert isinstance(begun, Err), begun
        assert begun.error.code is ErrorCode.trust_cleanup_required
        # The refused operations never reached the backend.
        assert world.refusals == ["cancel_pairing", "revoke_pairing"]
        assert "cancel_pairing" not in _daemon_operations(world.daemon)
        assert "revoke_pairing" not in _daemon_operations(world.daemon)
        # Cleanup intent is retained, so no credential is claimed.
        assert world.coordinator.pending_cleanup(receiver.receiver_id) is True
        remembered = await world.store.load_credential_ref(receiver.receiver_id)
        assert isinstance(remembered, Ok) and remembered.value is None

        retried = await world.coordinator.retry_revocation(
            receiver.receiver_id, ctx,
        )
        assert isinstance(retried, Ok), retried
        assert world.coordinator.pending_cleanup(receiver.receiver_id) is False
        # Exactly one receiver revoke reached the backend, for this target.
        revokes = [
            entry for entry in _journal(world.daemon)
            if entry["process"] == "daemon"
            and entry.get("operation") == "revoke_pairing"
        ]
        assert [entry["target"] for entry in revokes] == [receiver.canonical_host]
        remembered = await world.store.load_credential_ref(receiver.receiver_id)
        assert isinstance(remembered, Ok) and remembered.value is None
    finally:
        await _close(world)


async def test_a_foreign_generation_challenge_cannot_prepare(tmp_path, monkeypatch):
    world = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        begun = await world.store.begin_pairing_token(receiver, ctx)
        assert isinstance(begun, Ok), begun
        challenge = await _challenge(begun.value, ctx)
        foreign = PairingChallenge(
            **{
                **challenge.__dict__,
                "session_generation": challenge.session_generation + 1,
            },
        )
        prepared = await world.store.prepare_pairing(foreign, begun.value.backend_token)
        assert isinstance(prepared, Err)
        assert prepared.error.code is ErrorCode.keyring_unavailable
        # The mismatched challenge was rejected locally: no prepare was sent.
        assert "prepare_pairing" not in _daemon_operations(world.daemon)
    finally:
        await _close(world)


async def test_an_older_handle_cannot_revoke_a_newer_pairing(tmp_path, monkeypatch):
    world = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    try:
        first = await world.store.begin_pairing_token(receiver, ctx)
        assert isinstance(first, Ok), first
        first_challenge = await _challenge(first.value, ctx)
        first_prepared = await world.store.prepare_pairing(
            first_challenge, first.value.backend_token,
        )
        assert isinstance(first_prepared, Ok), first_prepared
        first_credential = await world.store.commit_pairing(first_prepared.value, 0)
        assert isinstance(first_credential, Ok), first_credential

        second = await world.store.begin_pairing_token(receiver, ctx)
        assert isinstance(second, Ok), second
        second_challenge = await _challenge(second.value, ctx)
        second_prepared = await world.store.prepare_pairing(
            second_challenge, second.value.backend_token,
        )
        assert isinstance(second_prepared, Ok), second_prepared

        before = _daemon_operations(world.daemon).count("revoke_pairing")
        stale = await world.store.revoke_pairing(first_prepared.value)
        assert isinstance(stale, Err)
        assert stale.error.code is ErrorCode.keyring_unavailable
        assert _daemon_operations(world.daemon).count("revoke_pairing") == before

        current = await world.store.revoke_pairing(second_prepared.value)
        assert isinstance(current, Ok), current
        assert _daemon_operations(world.daemon).count("revoke_pairing") == before + 1
    finally:
        await _close(world)


# ---------------------------------------------------------------------------
# Concurrent prompts: two live prompts stay target-local; one prompt is serial.
# ---------------------------------------------------------------------------


async def test_concurrent_pairing_prompts_never_cross_targets(
    tmp_path, monkeypatch,
):
    world = await pairing_world(
        tmp_path, monkeypatch, capture=False,
        daemon_env={fake_doubletake.DAEMON_CREDENTIAL_KIND_ENV: "password"},
    )
    a, b = target("a"), target("b")
    world.fence(a)
    world.fence(b)
    ctx = context(generation=1, lease=None)
    first, second = f"prompt-{uuid4().hex}", f"prompt-{uuid4().hex}"
    try:
        begun = await asyncio.gather(
            world.coordinator.begin_pairing(a, ctx),
            world.coordinator.begin_pairing(b, ctx),
        )
        assert all(isinstance(item, Ok) for item in begun), begun
        challenge_a, challenge_b = (item.value for item in begun)
        results = await asyncio.gather(
            world.coordinator.submit_pairing(
                challenge_a.challenge_id, SensitiveText(first), ctx,
            ),
            world.coordinator.submit_pairing(
                challenge_b.challenge_id, SensitiveText(second), ctx,
            ),
        )
        assert [item.kind for item in results] == ["Trusted", "Trusted"], results
        assert results[0].credential_ref != results[1].credential_ref
        # Exactly one submit per target, addressed to that target's own host.
        submits = [
            entry for entry in _journal(world.daemon)
            if entry["process"] == "daemon"
            and entry.get("operation") == "submit_pairing"
        ]
        assert sorted(entry["target"] for entry in submits) == sorted(
            [a.canonical_host, b.canonical_host]
        )
        # Each receiver keeps its own remembered credential reference.
        loaded_a = await world.store.load_credential_ref(a.receiver_id)
        loaded_b = await world.store.load_credential_ref(b.receiver_id)
        assert isinstance(loaded_a, Ok) and isinstance(loaded_b, Ok)
        assert loaded_a.value == results[0].credential_ref
        assert loaded_b.value == results[1].credential_ref
        assert first not in world.daemon.journal_path.read_text()
        assert second not in world.daemon.journal_path.read_text()
    finally:
        await _close(world)


async def test_a_second_concurrent_submission_for_one_prompt_is_refused(
    tmp_path, monkeypatch,
):
    world = await pairing_world(
        tmp_path, monkeypatch, capture=False,
        daemon_env={fake_doubletake.DAEMON_CREDENTIAL_KIND_ENV: "password"},
    )
    receiver = target("a")
    world.fence(receiver)
    ctx = context(generation=1, lease=None)
    first, second = f"race-{uuid4().hex}", f"race-{uuid4().hex}"
    try:
        begun = await world.coordinator.begin_pairing(receiver, ctx)
        assert isinstance(begun, Ok), begun
        results = await asyncio.gather(
            world.coordinator.submit_pairing(
                begun.value.challenge_id, SensitiveText(first), ctx,
            ),
            world.coordinator.submit_pairing(
                begun.value.challenge_id, SensitiveText(second), ctx,
            ),
        )
        kinds = sorted(item.kind for item in results)
        assert kinds == ["InvalidInput", "Trusted"], results
        submits = [
            entry for entry in _journal(world.daemon)
            if entry["process"] == "daemon"
            and entry.get("operation") == "submit_pairing"
        ]
        # The duplicate waiter is refused locally: only one prompt is answered.
        assert len(submits) == 1
        assert submits[0]["target"] == receiver.canonical_host
    finally:
        await _close(world)


async def test_the_published_profile_refuses_a_credential_instead_of_arguing_it(
    tmp_path, monkeypatch,
):
    """The documented v0.5 surface refuses credentials before any process exists."""
    from airplay_linux.backend.control import (
        ConnectPayload,
        PublishedDoubletakeProfile,
    )

    world = await keyring_world(tmp_path, monkeypatch)
    ctx = context(generation=1, lease=None)
    receiver = target("a")
    before = len(_journal(world.daemon))
    published = PublishedDoubletakeControl(
        world.runner,
        PublishedDoubletakeProfile(socket_path=world.daemon.socket_path),
    )
    try:
        # The documented v0.5 command surface places PINs in argv, so the adapter
        # refuses the credential-bearing connect instead of spawning that command.
        refused = await published.request(
            "connect",
            receiver,
            ConnectPayload(credential=SensitiveText(CANARY)),
            ctx,
        )
        assert isinstance(refused, Err)
        assert refused.error.code is ErrorCode.unsupported_operation
        assert CANARY not in repr(refused)
        assert len(_journal(world.daemon)) == before
    finally:
        await published.close()
    try:
        assert isinstance(
            await world.runner.run(
                CommandSpec(argv=("application-contract-v1", "--help"), env={}), 5.0,
            ),
            Ok,
        )
        assert SECRET not in repr(
            PairingPayload(credential=SensitiveText(SECRET)),
        )
    finally:
        await _close(world)


__all__: list[str] = []
