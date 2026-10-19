"""Transaction fences and late backend outcomes, without hardware or a keyring."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.backend.control import AckReply, BackendOperation
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.operations import (
    ChallengeId,
    OpaqueBackendToken,
    OperationId,
    PairingChallenge,
    SensitiveText,
)
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.testing.adapters import (
    Deferred,
    FakeCredentialStore,
    Journal,
    MemoryReceiverStore,
    Redacted,
    SequenceIds,
)
from airplay_linux.testing.builders import context, target
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.trust.coordinator import PairingCoordinator, fence_matches
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry
from airplay_linux.trust.keyring import KeyringPairing


@pytest.fixture
def challenge():
    return PairingChallenge(
        challenge_id=ChallengeId(UUID(int=10)),
        receiver_id=target().receiver_id,
        endpoint_fingerprint="endpoint-a-v1",
        endpoint_generation=1,
        operation_id=OperationId(UUID(int=11)),
        session_generation=2,
        trust_generation=3,
        created_at=20.0,
        expires_at=140.0,
        credential_kind="pin",
        backend_token=OpaqueBackendToken("opaque-token-canary"),
    )


def active_fence(challenge):
    return TrustFence(
        receiver_id=challenge.receiver_id,
        endpoint_fingerprint=challenge.endpoint_fingerprint,
        endpoint_generation=challenge.endpoint_generation,
        session_generation=challenge.session_generation,
        trust_generation=challenge.trust_generation,
        active_challenge_id=challenge.challenge_id,
    )


def test_changed_trust_generation_rejects_active_time_window(challenge):
    fence = TrustFence(
        receiver_id=challenge.receiver_id,
        endpoint_fingerprint=challenge.endpoint_fingerprint,
        endpoint_generation=challenge.endpoint_generation,
        session_generation=challenge.session_generation,
        trust_generation=challenge.trust_generation + 1,
    )
    assert not fence_matches(challenge, fence, challenge.created_at + 1)


@pytest.mark.parametrize(
    "change",
    [
        {"receiver_id": target("b").receiver_id},
        {"endpoint_fingerprint": "replacement"},
        {"endpoint_generation": 2},
        {"session_generation": 3},
        {"trust_generation": 4},
        {"active_challenge_id": ChallengeId(UUID(int=99))},
        {"active_challenge_id": None},
    ],
)
def test_every_fence_dimension_is_required(challenge, change):
    assert not fence_matches(challenge, replace(active_fence(challenge), **change), 21.0)


def test_expiry_is_exclusive_and_future_challenges_are_rejected(challenge):
    fence = active_fence(challenge)
    assert fence_matches(challenge, fence, 20.0)
    assert fence_matches(challenge, fence, 139.99)
    assert not fence_matches(challenge, fence, 140.0)
    assert not fence_matches(challenge, fence, 19.99)


def test_registry_activation_and_exact_clear_do_not_replace_another_challenge(challenge):
    registry = TrustFenceRegistry()
    registry.set(replace(active_fence(challenge), active_challenge_id=None))
    assert isinstance(registry.activate(challenge), Ok)
    other = replace(challenge, challenge_id=ChallengeId(UUID(int=12)))
    assert isinstance(registry.activate(other), Err)
    registry.clear_challenge(challenge.receiver_id, other.challenge_id)
    assert registry.get(challenge.receiver_id).active_challenge_id == challenge.challenge_id
    registry.clear_challenge(challenge.receiver_id, challenge.challenge_id)
    assert registry.get(challenge.receiver_id).active_challenge_id is None


@pytest.mark.parametrize("field", ["endpoint_fingerprint", "endpoint_generation", "session_generation", "trust_generation"])
def test_registry_invalidation_clears_even_an_inherited_active_id(challenge, field):
    registry = TrustFenceRegistry()
    fence = active_fence(challenge)
    registry.set(fence)
    replacement = "replacement" if field == "endpoint_fingerprint" else getattr(fence, field) + 1
    registry.set(replace(fence, **{field: replacement}))
    assert registry.get(challenge.receiver_id).active_challenge_id is None
    assert isinstance(registry.activate(challenge), Err)


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.completion = Deferred()

    async def wait(self):
        self.entered.set()
        await self.completion.wait()

    def release(self):
        self.completion.resolve(None)


class Control:
    def __init__(self):
        self.journal = Journal()
        self.submit_gate = None
        self.rejections = 0

    async def request(self, operation, target, payload, context):
        self.journal.record("control.request", operation=operation, target=target, payload=payload, context=context)
        if operation is BackendOperation.submit_pairing:
            if self.submit_gate is not None:
                await self.submit_gate.wait()
            if self.rejections:
                self.rejections -= 1
                return Err(error=diagnostic(ErrorCode.receiver_rejected))
        return Ok(value=AckReply(operation=operation.value, target=target.canonical_host))


class Credentials(FakeCredentialStore):
    """Add only the adjudicated begin extension and pre-effect suspension seams."""

    def __init__(self):
        super().__init__()
        self.kind = "pin"
        self.gates = {}

    def hold(self, method):
        gate = Gate()
        self.gates[method] = gate
        return gate

    async def pause(self, method):
        gate = self.gates.pop(method, None)
        if gate is not None:
            await gate.wait()

    async def begin_pairing_token(self, target, context):
        await self.pause("begin")
        return Ok(value=KeyringPairing(
            backend_token=OpaqueBackendToken("backend-correlation-canary"),
            credential_kind=self.kind,
        ))

    async def prepare_pairing(self, challenge, backend_result):
        await self.pause("prepare")
        return await super().prepare_pairing(challenge, backend_result)

    async def commit_pairing(self, provisional_ref, trust_generation):
        await self.pause("commit")
        return await super().commit_pairing(provisional_ref, trust_generation)

    async def cancel_pairing(self, challenge):
        await self.pause("cancel")
        return await super().cancel_pairing(challenge)

    async def revoke_pairing(self, ref_or_receiver):
        await self.pause("revoke")
        return await super().revoke_pairing(ref_or_receiver)


class Harness:
    def __init__(self, credentials=None, store=None):
        self.target = target()
        self.context = context(lease=None, trust_generation=3)
        self.clock = FakeClock(20)
        self.scheduler = FakeScheduler(self.clock)
        self.control = Control()
        self.credentials = credentials if credentials is not None else Credentials()
        self.store = store if store is not None else MemoryReceiverStore()
        self.fences = TrustFenceRegistry()
        self.fences.set(TrustFence(
            receiver_id=self.target.receiver_id,
            endpoint_fingerprint=self.target.endpoint_fingerprint,
            endpoint_generation=1,
            session_generation=1,
            trust_generation=3,
        ))
        self.coordinator = PairingCoordinator(
            self.control, self.credentials, self.store, self.fences,
            self.clock, self.scheduler, SequenceIds(100),
        )

    async def begin(self):
        result = await self.coordinator.begin_pairing(self.target, self.context)
        assert isinstance(result, Ok), result
        return result.value

    def current_context(self):
        fence = self.fences.get(self.target.receiver_id)
        return replace(self.context, session_generation=fence.session_generation,
                       endpoint_generation=fence.endpoint_generation, trust_generation=fence.trust_generation)

    async def submit(self, challenge, text="8496"):
        return await self.coordinator.submit_pairing(challenge.challenge_id, SensitiveText(text), self.context)


def test_valid_commit_is_the_only_trust_promotion():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        result = await h.submit(challenge)
        assert result.kind == "Trusted"
        assert result.credential_ref == h.credentials.stored[h.target.receiver_id]
        assert h.credentials.committed[result.credential_ref] == 3
        assert not h.coordinator.pending_cleanup(h.target.receiver_id)
        assert h.fences.get(h.target.receiver_id).active_challenge_id is None
        assert h.scheduler.pending() == ()
        assert h.store.receivers == {}  # Opaque references stay in the keyring boundary.
        assert (await h.submit(challenge)).kind == "Cancelled"
    asyncio.run(scenario())


def test_store_without_begin_extension_fails_before_prompt_or_contact():
    async def scenario():
        h = Harness(FakeCredentialStore())
        result = await h.coordinator.begin_pairing(h.target, h.context)
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.keyring_unavailable
        assert h.control.journal.entries == []
        assert h.store.receivers == {}
        assert h.credentials.stored == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["submit", "prepare", "commit"])
@pytest.mark.parametrize("invalidation", ["cancel", "expiry", "forget", "endpoint", "invalid", "stop", "shutdown"])
def test_late_success_is_revoked_after_every_invalidation(stage, invalidation):
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        if stage == "submit":
            gate = h.control.submit_gate = Gate()
        else:
            gate = h.credentials.hold(stage)
        submission = asyncio.create_task(h.submit(challenge))
        await gate.entered.wait()
        cancellation = None
        if invalidation == "expiry":
            h.scheduler.advance(120)
        elif invalidation in {"endpoint", "stop", "shutdown"}:
            fence = h.fences.get(h.target.receiver_id)
            changed = {"endpoint_generation": 2, "endpoint_fingerprint": "replacement"} if invalidation == "endpoint" else {"session_generation": 2}
            h.fences.set(replace(fence, **changed))
        else:
            method = {"cancel": h.coordinator.cancel_pairing,
                      "forget": h.coordinator.forget_trust,
                      "invalid": h.coordinator.mark_invalid}[invalidation]
            args = (h.target.receiver_id, challenge.challenge_id, h.context) if invalidation == "cancel" else (h.target.receiver_id, h.context)
            cancellation = asyncio.create_task(method(*args))
            await asyncio.sleep(0)
        assert h.fences.get(h.target.receiver_id).active_challenge_id is None
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        result = await submission
        if cancellation is not None:
            assert isinstance(await cancellation, Ok)
        assert result.kind != "Trusted"
        assert result.credential_ref is None
        # Wait for confirmed cleanup even when the submit deadline fired at expiry.
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert h.target.receiver_id not in h.credentials.stored
        assert h.credentials.provisional == {}
        assert h.credentials.revoked
        assert not h.coordinator.pending_cleanup(h.target.receiver_id)
        assert not any(saved.credential_ref for saved in h.store.receivers.values())
    asyncio.run(scenario())


def test_begin_completion_after_cancel_cannot_publish_a_challenge():
    async def scenario():
        h = Harness()
        gate = h.credentials.hold("begin")
        beginning = asyncio.create_task(h.coordinator.begin_pairing(h.target, h.context))
        await gate.entered.wait()
        cancelling = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, None, h.context))
        await asyncio.sleep(0)
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert isinstance(await beginning, Err)
        assert isinstance(await cancelling, Ok)
        assert h.fences.get(h.target.receiver_id).active_challenge_id is None
        assert h.credentials.journal.calls("credential_store.cancel_pairing")
    asyncio.run(scenario())


@pytest.mark.parametrize("backend_rejection", [False, True])
def test_only_three_invalid_attempts_can_reach_an_active_challenge(backend_rejection):
    async def scenario():
        h = Harness()
        h.control.rejections = 3 if backend_rejection else 0
        challenge = await h.begin()
        results = [await h.submit(challenge, "8496" if backend_rejection else "bad") for _ in range(4)]
        assert [result.kind for result in results] == ["InvalidInput", "InvalidInput", "ReceiverRejected", "Cancelled"]
        assert len(h.control.journal.entries) == (3 if backend_rejection else 0)
        assert h.credentials.stored == {}
        assert h.fences.get(h.target.receiver_id).active_challenge_id is None
    asyncio.run(scenario())


@pytest.mark.parametrize("password", ["not-four-digits", "  spaces and Unicode ♥\n", "7"])
def test_password_mode_accepts_unrestricted_nonempty_text(password):
    async def scenario():
        h = Harness()
        h.credentials.kind = "password"
        challenge = await h.begin()
        assert challenge.credential_kind == "password"
        assert (await h.submit(challenge, password)).kind == "Trusted"
        assert h.control.journal.entries[0][1]["payload"]["credential"] == Redacted(kind="SensitiveText")
    asyncio.run(scenario())


@pytest.mark.parametrize("change", [{"endpoint_generation": 2}, {"session_generation": 2}, {"trust_generation": 4}])
def test_wrong_context_is_rejected_before_backend_contact(change):
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        result = await h.coordinator.submit_pairing(challenge.challenge_id, SensitiveText("8496"), replace(h.context, **change))
        assert result.kind == "Cancelled"
        assert h.control.journal.entries == []
        assert h.credentials.stored == {}
        assert (await h.submit(challenge)).kind == "Trusted"
    asyncio.run(scenario())


def test_expired_input_never_reaches_the_backend():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        h.clock.advance(120)  # Also reject expiry before the scheduler has drained.
        result = await h.submit(challenge)
        assert result.kind == "Cancelled"
        assert result.diagnostic.code is ErrorCode.pairing_expired
        assert h.control.journal.entries == []
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
    asyncio.run(scenario())


def test_compensation_failure_blocks_new_pairing_until_confirmed_retry():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        gate = h.credentials.hold("commit")
        submission = asyncio.create_task(h.submit(challenge))
        await gate.entered.wait()
        h.credentials.queue_result("revoke_pairing", Err(error=diagnostic(ErrorCode.keyring_unavailable)))
        cancelling = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, None, h.context))
        await asyncio.sleep(0)
        gate.release()
        assert (await submission).kind == "TrustCleanupRequired"
        assert isinstance(await cancelling, Err)
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        blocked = await h.coordinator.begin_pairing(h.target, h.current_context())
        assert isinstance(blocked, Err)
        assert blocked.error.code is ErrorCode.trust_cleanup_required
        h.credentials.queue_result("revoke_pairing", Err(error=diagnostic(ErrorCode.keyring_unavailable)))
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Err)
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert not h.coordinator.pending_cleanup(h.target.receiver_id)
        assert h.target.receiver_id not in h.credentials.stored
    asyncio.run(scenario())


def test_cancellation_deadline_blocks_even_without_video_resources():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        gate = h.credentials.hold("cancel")
        cancelling = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, challenge.challenge_id, h.context))
        await gate.entered.wait()
        h.scheduler.advance(5)
        result = await cancelling
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.trust_cleanup_required
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        assert isinstance(await h.coordinator.begin_pairing(h.target, h.current_context()), Err)
        gate.release()
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert not h.coordinator.pending_cleanup(h.target.receiver_id)
    asyncio.run(scenario())


def test_cancelled_submit_waiter_keeps_late_backend_success_observable():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        gate = h.control.submit_gate = Gate()
        submission = asyncio.create_task(h.submit(challenge))
        await gate.entered.wait()
        submission.cancel()
        with pytest.raises(asyncio.CancelledError):
            await submission
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert h.credentials.stored == {}
        assert h.credentials.revoked
    asyncio.run(scenario())


def saved(h):
    return SavedReceiver(receiver_id=h.target.receiver_id, service_metadata=h.target.service_records,
                         display_name="Living Room", last_endpoint=h.target,
                         continuity_fingerprint=h.target.endpoint_fingerprint,
                         credential_ref=None, remembered_resolution=None)


def test_forget_requires_confirmed_stream_stop_and_preserves_metadata_until_revoke():
    async def scenario():
        h = Harness()
        h.store.receivers[h.target.receiver_id] = saved(h)
        before = h.fences.get(h.target.receiver_id)
        blocked = await h.coordinator.forget_trust(h.target.receiver_id, replace(h.context, transport_lease_id=context().transport_lease_id))
        assert isinstance(blocked, Err)
        assert h.fences.get(h.target.receiver_id) == before
        assert h.target.receiver_id in h.store.receivers
        h.credentials.seed_trusted(h.target.receiver_id)
        gate = h.credentials.hold("revoke")
        forgetting = asyncio.create_task(h.coordinator.forget_trust(h.target.receiver_id, h.context))
        await gate.entered.wait()
        assert h.target.receiver_id in h.store.receivers
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert isinstance(await forgetting, Ok)
        assert h.target.receiver_id not in h.store.receivers
        assert h.target.receiver_id not in h.credentials.stored
    asyncio.run(scenario())


def test_forget_revoke_failure_keeps_metadata_for_retry():
    async def scenario():
        h = Harness()
        h.store.receivers[h.target.receiver_id] = saved(h)
        h.credentials.queue_result("revoke_pairing", Err(error=diagnostic(ErrorCode.keyring_unavailable)))
        assert isinstance(await h.coordinator.forget_trust(h.target.receiver_id, h.context), Err)
        assert h.target.receiver_id in h.store.receivers
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert h.target.receiver_id not in h.store.receivers
    asyncio.run(scenario())


def test_rotation_uses_a_new_challenge_and_never_reuses_the_old_input():
    async def scenario():
        h = Harness()
        old = await h.begin()
        first = await h.submit(old)
        assert first.kind == "Trusted"
        fresh = await h.begin()
        assert fresh.challenge_id != old.challenge_id
        assert (await h.submit(old)).kind == "Cancelled"
        assert (await h.submit(fresh)).kind == "Trusted"
    asyncio.run(scenario())


def test_unreadable_sensitive_input_returns_a_fixed_invalid_result():
    class Unreadable(SensitiveText):
        def reveal_for_backend(self):
            raise RuntimeError("password-canary")

    async def scenario():
        h = Harness()
        ch = await h.begin()
        result = await h.coordinator.submit_pairing(ch.challenge_id, Unreadable("password-canary"), h.context)
        assert result.kind == "InvalidInput"
        assert "password-canary" not in repr(result)
        assert h.control.journal.entries == []
    asyncio.run(scenario())


def test_backend_deadline_keeps_the_late_result_behind_the_cleanup_barrier():
    async def scenario():
        h = Harness()
        ch = await h.begin()
        gate = h.control.submit_gate = Gate()
        submission = asyncio.create_task(h.submit(ch))
        await gate.entered.wait()
        h.scheduler.advance(5)
        result = await submission
        assert result.kind == "TrustCleanupRequired"
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert h.credentials.stored == {}
        assert h.credentials.revoked
    asyncio.run(scenario())


def test_invalidation_while_revocation_is_pending_still_fences_the_old_success():
    async def scenario():
        h = Harness()
        ch = await h.begin()
        submitting_gate = h.credentials.hold("commit")
        revoke_gate = h.credentials.hold("revoke")
        submission = asyncio.create_task(h.submit(ch))
        await submitting_gate.entered.wait()
        cancellation = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, None, h.context))
        await asyncio.sleep(0)
        submitting_gate.release()
        await revoke_gate.entered.wait()
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        blocked = await h.coordinator.begin_pairing(h.target, h.current_context())
        assert isinstance(blocked, Err)
        assert blocked.error.code is ErrorCode.trust_cleanup_required
        revoke_gate.release()
        assert isinstance(await cancellation, Ok)
        assert (await submission).kind == "Cancelled"
        assert h.credentials.stored == {}
    asyncio.run(scenario())


def test_metadata_failure_after_forget_revocation_keeps_the_cleanup_retryable():
    async def scenario():
        h = Harness()
        h.store.receivers[h.target.receiver_id] = saved(h)
        h.credentials.seed_trusted(h.target.receiver_id)
        h.store.queue_result("remove_metadata", Err(error=diagnostic(ErrorCode.persistence_failed)))
        assert isinstance(await h.coordinator.forget_trust(h.target.receiver_id, h.context), Err)
        assert h.target.receiver_id in h.store.receivers
        assert h.target.receiver_id not in h.credentials.stored
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        assert isinstance(await h.coordinator.retry_revocation(h.target.receiver_id, h.current_context()), Ok)
        assert h.target.receiver_id not in h.store.receivers
    asyncio.run(scenario())


def test_mark_invalid_clears_local_trust_before_a_held_pairing_can_commit():
    async def scenario():
        h = Harness()
        h.credentials.seed_trusted(h.target.receiver_id)
        h.store.receivers[h.target.receiver_id] = replace(saved(h), credential_ref=h.credentials.stored[h.target.receiver_id])
        ch = await h.begin()
        gate = h.credentials.hold("prepare")
        submission = asyncio.create_task(h.submit(ch))
        await gate.entered.wait()
        invalidation = asyncio.create_task(h.coordinator.mark_invalid(h.target.receiver_id, h.context))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert h.target.receiver_id not in h.credentials.stored
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert (await submission).kind == "Cancelled"
        assert isinstance(await invalidation, Ok)
        assert h.store.receivers[h.target.receiver_id].credential_ref is None
    asyncio.run(scenario())


def test_foreign_challenge_cannot_cancel_another_receiver():
    async def scenario():
        h = Harness()
        ch = await h.begin()
        result = await h.coordinator.cancel_pairing(target("b").receiver_id, ch.challenge_id, h.context)
        assert isinstance(result, Err)
        assert (await h.submit(ch)).kind == "Trusted"
    asyncio.run(scenario())


def test_mark_invalid_drops_local_trust_before_a_slow_cancellation():
    async def scenario():
        h = Harness()
        h.credentials.seed_trusted(h.target.receiver_id)
        await h.begin()
        gate = h.credentials.hold("cancel")
        invalidation = asyncio.create_task(h.coordinator.mark_invalid(h.target.receiver_id, h.context))
        await gate.entered.wait()
        assert h.target.receiver_id not in h.credentials.stored
        assert h.coordinator.pending_cleanup(h.target.receiver_id)
        gate.release()
        assert isinstance(await invalidation, Ok)
    asyncio.run(scenario())


def test_a_retired_submit_waiter_cannot_invalidate_a_fresh_challenge(monkeypatch):
    async def scenario():
        h = Harness()
        old = await h.begin()
        backend = h.control.submit_gate = Gate()
        waiter = Gate()
        original_wait = h.coordinator._wait

        async def delayed_delivery(task, receiver_id):
            result = await original_wait(task, receiver_id)
            if getattr(result, "kind", None) == "Cancelled":
                await waiter.wait()
            return result

        # Exercise a legal scheduling delay after the acquisition task completes
        # but before its public waiter resumes. Cleanup may finish in that gap.
        monkeypatch.setattr(h.coordinator, "_wait", delayed_delivery)
        submitting = asyncio.create_task(h.submit(old))
        await backend.entered.wait()
        cancelling = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, old.challenge_id, h.context))
        await asyncio.sleep(0)
        backend.release()
        await waiter.entered.wait()
        assert isinstance(await cancelling, Ok)
        ctx = h.current_context()
        begun = await h.coordinator.begin_pairing(h.target, ctx)
        assert isinstance(begun, Ok)
        waiter.release()
        assert (await submitting).kind == "Cancelled"
        fresh = await h.coordinator.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), ctx)
        assert fresh.kind == "Trusted"
    asyncio.run(scenario())


def test_forget_arriving_during_invalid_metadata_cleanup_is_not_lost():
    class HeldMetadata(MemoryReceiverStore):
        def __init__(self):
            super().__init__()
            self.gate = Gate()

        async def get(self, receiver_id):
            result = await super().get(receiver_id)
            await self.gate.wait()
            return result

    async def scenario():
        store = HeldMetadata()
        h = Harness(store=store)
        ref = h.credentials.seed_trusted(h.target.receiver_id)
        store.receivers[h.target.receiver_id] = replace(saved(h), credential_ref=ref)
        invalidation = asyncio.create_task(h.coordinator.mark_invalid(h.target.receiver_id, h.context))
        await store.gate.entered.wait()
        forgetting = asyncio.create_task(h.coordinator.forget_trust(h.target.receiver_id, h.current_context()))
        await asyncio.sleep(0)
        store.gate.release()
        assert isinstance(await invalidation, Ok)
        assert isinstance(await forgetting, Ok)
        assert h.target.receiver_id not in store.receivers
        assert not h.coordinator.pending_cleanup(h.target.receiver_id)
    asyncio.run(scenario())


def test_pending_receiver_does_not_serialize_another_receivers_pairing():
    async def scenario():
        h = Harness()
        challenge = await h.begin()
        gate = h.credentials.hold("cancel")
        cancellation = asyncio.create_task(h.coordinator.cancel_pairing(h.target.receiver_id, None, h.context))
        await gate.entered.wait()
        receiver_b = target("b", address="192.0.2.11")
        h.fences.set(TrustFence(receiver_id=receiver_b.receiver_id,
                               endpoint_fingerprint=receiver_b.endpoint_fingerprint,
                               endpoint_generation=1, session_generation=1, trust_generation=3))
        result = await h.coordinator.begin_pairing(receiver_b, h.context)
        assert isinstance(result, Ok)
        assert (await h.coordinator.submit_pairing(result.value.challenge_id, SensitiveText("8496"), h.context)).kind == "Trusted"
        assert h.coordinator.pending_cleanup(challenge.receiver_id)
        gate.release()
        assert isinstance(await cancellation, Ok)
    asyncio.run(scenario())
