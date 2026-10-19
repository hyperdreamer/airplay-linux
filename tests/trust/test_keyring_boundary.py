"""The keyring adapter owns handles, never file credentials or secret text."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from airplay_linux.backend.control import AckReply, ApplicationContractProfile, BackendOperation, StatusReply
from airplay_linux.diagnostics import ActivityLog, diagnostic, safe_activity
from airplay_linux.domain.operations import (
    ChallengeId,
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    PairingChallenge,
    SensitiveText,
)
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, RecoveryAction
from airplay_linux.storage import AtomicFiles, JsonReceiverStore
from airplay_linux.testing.adapters import Journal, SequenceIds
from airplay_linux.testing.builders import context, target
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.trust.coordinator import PairingCoordinator
from airplay_linux.trust.fences import TrustFence, TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore


class Control:
    def __init__(self, kind="pin"):
        self.journal = Journal()
        self.kind = kind
        self.fail_operation = None
        self.raise_operation = None
        self.begin_ack = False
        self.wrong_target = False

    async def request(self, operation, receiver, payload, ctx):
        self.journal.record("control.request", operation=operation, target=receiver, payload=payload, context=ctx)
        if operation is self.raise_operation:
            raise RuntimeError("canary-password backend-correlation-canary")
        if operation is self.fail_operation:
            return Err(error=Diagnostic(code=ErrorCode.keyring_unavailable,
                                        message="canary-password", detail="backend-correlation-canary",
                                        recovery=(RecoveryAction.retry,)))
        if operation is BackendOperation.status or (operation is BackendOperation.begin_pairing and not self.begin_ack):
            return Ok(value=StatusReply(
                state="pin_required", device="Living Room", target="192.0.2.99" if self.wrong_target else target().canonical_host,
                has_audio=False, audio_muted=False, needs_credential=True,
                credential_kind=self.kind, streams=(),
            ))
        return Ok(value=AckReply(operation=operation.value, target=receiver.canonical_host))


def features(**overrides):
    return replace(ApplicationContractProfile(socket_path="/run/user/1000/private.sock").features, **overrides)


def challenge(pairing):
    receiver = target()
    ctx = context(lease=None, trust_generation=3)
    return PairingChallenge(challenge_id=ChallengeId(UUID(int=5)), receiver_id=receiver.receiver_id,
                            endpoint_fingerprint=receiver.endpoint_fingerprint, endpoint_generation=1,
                            operation_id=ctx.operation_id, session_generation=1, trust_generation=3,
                            created_at=20.0, expires_at=140.0, credential_kind=pairing.credential_kind,
                            backend_token=pairing.backend_token)


@pytest.mark.parametrize("missing", ["target_bound_pairing", "keyring", "provisional_commit", "cancel_pairing", "revoke_pairing"])
def test_unaffirmed_transaction_capability_fails_every_method_without_io(missing):
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features(**{missing: False}))
        receiver = target()
        from airplay_linux.trust.keyring import KeyringPairing
        ch = challenge(KeyringPairing(backend_token=OpaqueBackendToken("backend-correlation-canary"), credential_kind="pin"))
        results = [
            await store.begin_pairing_token(receiver, context(lease=None)),
            await store.load_credential_ref(receiver.receiver_id),
            await store.prepare_pairing(ch, ch.backend_token),
            await store.commit_pairing(OpaqueProvisionalCredentialRef("provisional-canary"), 3),
            await store.cancel_pairing(ch),
            await store.revoke_pairing(receiver.receiver_id),
            await store.mark_invalid(receiver.receiver_id),
        ]
        assert all(isinstance(result, Err) and result.error.code is ErrorCode.keyring_unavailable for result in results)
        assert control.journal.entries == []
    asyncio.run(scenario())


def test_missing_provisional_commit_reports_unavailable_without_trust_or_metadata(tmp_path):
    async def scenario():
        control = Control()
        credentials = BackendKeyringStore(control, features(provisional_commit=False))
        receiver_store = JsonReceiverStore(AtomicFiles(root=tmp_path))
        fences = TrustFenceRegistry()
        fences.set(TrustFence(receiver_id=target().receiver_id, endpoint_fingerprint=target().endpoint_fingerprint,
                              endpoint_generation=1, session_generation=1, trust_generation=3))
        clock = FakeClock(20)
        coordinator = PairingCoordinator(control, credentials, receiver_store, fences, clock, FakeScheduler(clock), SequenceIds())
        result = await coordinator.begin_pairing(target(), context(lease=None, trust_generation=3))
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.keyring_unavailable
        assert control.journal.entries == []
        assert list(tmp_path.rglob("*.json")) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("begin_ack", [False, True])
def test_adapter_owns_opaque_prepare_commit_and_receiver_revocation(begin_ack):
    async def scenario():
        control = Control(kind="password")
        control.begin_ack = begin_ack
        store = BackendKeyringStore(control, features())
        assert (await store.load_credential_ref(target().receiver_id)).value is None
        begun = await store.begin_pairing_token(target(), context(lease=None, trust_generation=3))
        assert isinstance(begun, Ok)
        ch = challenge(begun.value)
        assert ch.credential_kind == "password"
        prepared = await store.prepare_pairing(ch, ch.backend_token)
        assert isinstance(prepared, Ok)
        assert isinstance(prepared.value, OpaqueProvisionalCredentialRef)
        committed = await store.commit_pairing(prepared.value, 3)
        assert isinstance(committed, Ok)
        assert isinstance(committed.value, OpaqueCredentialRef)
        assert (await store.load_credential_ref(ch.receiver_id)).value == committed.value
        assert isinstance(await store.revoke_pairing(ch.receiver_id), Ok)
        assert (await store.load_credential_ref(ch.receiver_id)).value is None
        assert isinstance(await store.commit_pairing(prepared.value, 3), Err)
        assert isinstance(await store.revoke_pairing(ch.receiver_id), Ok)  # Confirmed retries are idempotent.
        assert "backend-correlation-canary" not in repr(store)
    asyncio.run(scenario())


def test_foreign_token_or_generation_cannot_prepare_or_commit():
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        begun = await store.begin_pairing_token(target(), context(lease=None, trust_generation=3))
        ch = challenge(begun.value)
        before = len(control.journal.entries)
        assert isinstance(await store.prepare_pairing(ch, OpaqueBackendToken("foreign-token")), Err)
        assert isinstance(await store.prepare_pairing(replace(ch, endpoint_fingerprint="replacement"), ch.backend_token), Err)
        assert len(control.journal.entries) == before
        prepared = await store.prepare_pairing(ch, ch.backend_token)
        before = len(control.journal.entries)
        assert isinstance(await store.commit_pairing(prepared.value, 4), Err)
        assert len(control.journal.entries) == before
        assert isinstance(await store.cancel_pairing(ch), Ok)
        assert isinstance(await store.commit_pairing(prepared.value, 3), Err)
    asyncio.run(scenario())


def test_another_receivers_prompt_cannot_supply_the_credential_kind():
    async def scenario():
        control = Control()
        control.wrong_target = True
        store = BackendKeyringStore(control, features())
        result = await store.begin_pairing_token(target(), context(lease=None, trust_generation=3))
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.keyring_unavailable
        assert (await store.load_credential_ref(target().receiver_id)).value is None
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", [BackendOperation.begin_pairing, BackendOperation.prepare_pairing, BackendOperation.commit_pairing, BackendOperation.cancel_pairing, BackendOperation.revoke_pairing])
@pytest.mark.parametrize("raises", [False, True])
def test_keyring_denials_and_exceptions_are_fixed_and_do_not_leak(operation, raises):
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        if operation is BackendOperation.begin_pairing:
            setattr(control, "raise_operation" if raises else "fail_operation", operation)
            result = await store.begin_pairing_token(target(), context(lease=None, trust_generation=3))
        else:
            begun = await store.begin_pairing_token(target(), context(lease=None, trust_generation=3))
            ch = challenge(begun.value)
            prepared = await store.prepare_pairing(ch, ch.backend_token)
            setattr(control, "raise_operation" if raises else "fail_operation", operation)
            if operation is BackendOperation.prepare_pairing:
                result = await store.prepare_pairing(ch, ch.backend_token)
            elif operation is BackendOperation.commit_pairing:
                result = await store.commit_pairing(prepared.value, 3)
            elif operation is BackendOperation.cancel_pairing:
                result = await store.cancel_pairing(ch)
            else:
                result = await store.revoke_pairing(ch.receiver_id)
        assert isinstance(result, Err)
        assert result.error.code in {ErrorCode.keyring_unavailable, ErrorCode.trust_cleanup_required}
        rendered = repr(result) + repr(store) + repr(control.journal)
        assert "canary-password" not in rendered
        assert "backend-correlation-canary" not in rendered
    asyncio.run(scenario())


def test_confirmed_cancel_allows_a_new_pairing_transaction():
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        ctx = context(lease=None, trust_generation=3)
        begun = await store.begin_pairing_token(target(), ctx)
        ch = challenge(begun.value)
        assert isinstance(await store.cancel_pairing(ch), Ok)
        restarted = await store.begin_pairing_token(target(), ctx)
        assert isinstance(restarted, Ok)
        assert restarted.value.backend_token != ch.backend_token
    asyncio.run(scenario())


def test_current_rotation_provisional_can_compensate_without_a_wire_handle():
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        ctx = context(lease=None, trust_generation=3)
        first = await store.begin_pairing_token(target(), ctx)
        old = challenge(first.value)
        prepared = await store.prepare_pairing(old, old.backend_token)
        assert isinstance(await store.commit_pairing(prepared.value, 3), Ok)
        fresh = await store.begin_pairing_token(target(), ctx)
        ch = replace(challenge(fresh.value), challenge_id=ChallengeId(UUID(int=6)))
        provisional = await store.prepare_pairing(ch, ch.backend_token)
        assert isinstance(await store.cancel_pairing(ch), Ok)
        assert isinstance(await store.revoke_pairing(provisional.value), Ok)
        assert (await store.load_credential_ref(ch.receiver_id)).value is None
        assert isinstance(await store.revoke_pairing(ch.receiver_id), Ok)
    asyncio.run(scenario())


def test_rotation_can_begin_after_a_confirmed_commit():
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        ctx = context(lease=None, trust_generation=3)
        begun = await store.begin_pairing_token(target(), ctx)
        ch = challenge(begun.value)
        prepared = await store.prepare_pairing(ch, ch.backend_token)
        assert isinstance(await store.commit_pairing(prepared.value, 3), Ok)
        rotated = await store.begin_pairing_token(target(), ctx)
        assert isinstance(rotated, Ok)
        assert rotated.value.backend_token != ch.backend_token
        assert isinstance(await store.revoke_pairing(target().receiver_id), Ok)
        assert (await store.load_credential_ref(target().receiver_id)).value is None
    asyncio.run(scenario())


def test_stale_provisional_revoke_never_removes_a_newer_committed_credential():
    async def scenario():
        control = Control()
        store = BackendKeyringStore(control, features())
        ctx = context(lease=None, trust_generation=3)
        begun = await store.begin_pairing_token(target(), ctx)
        ch = challenge(begun.value)
        old_provisional = await store.prepare_pairing(ch, ch.backend_token)
        assert isinstance(await store.commit_pairing(old_provisional.value, 3), Ok)
        rotated = await store.begin_pairing_token(target(), ctx)
        assert isinstance(rotated, Ok)
        fresh = replace(challenge(rotated.value), challenge_id=ChallengeId(UUID(int=6)))
        provisional = await store.prepare_pairing(fresh, fresh.backend_token)
        committed = await store.commit_pairing(provisional.value, 3)
        assert isinstance(committed, Ok)
        # A target-only wire revoke cannot safely address an obsolete handle.
        # Refuse it without contacting or revoking the newer receiver credential.
        before = len(control.journal.entries)
        assert isinstance(await store.prepare_pairing(ch, ch.backend_token), Err)
        assert isinstance(await store.commit_pairing(old_provisional.value, 3), Err)
        assert isinstance(await store.cancel_pairing(ch), Err)
        assert isinstance(await store.revoke_pairing(old_provisional.value), Err)
        assert len(control.journal.entries) == before
        assert (await store.load_credential_ref(target().receiver_id)).value == committed.value
    asyncio.run(scenario())


@pytest.mark.parametrize("stage", [BackendOperation.submit_pairing, BackendOperation.prepare_pairing, BackendOperation.commit_pairing])
@pytest.mark.parametrize("invalidate", ["cancel", "invalid"])
def test_adapter_and_coordinator_compensate_the_late_backend_together(stage, invalidate, tmp_path):
    class HeldControl(Control):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation, receiver, payload, ctx):
            if operation is stage:
                self.entered.set()
                await self.release.wait()
            return await super().request(operation, receiver, payload, ctx)

    class ObservedCredentials(BackendKeyringStore):
        def __init__(self, control):
            super().__init__(control, features())
            self.cancelled = asyncio.Event()
            self.invalidated = asyncio.Event()

        async def cancel_pairing(self, challenge):
            result = await super().cancel_pairing(challenge)
            self.cancelled.set()
            return result

        async def mark_invalid(self, receiver_id):
            result = await super().mark_invalid(receiver_id)
            self.invalidated.set()
            return result

    async def scenario():
        control = HeldControl()
        credentials = ObservedCredentials(control)
        fences = TrustFenceRegistry()
        receiver = target()
        fences.set(TrustFence(receiver_id=receiver.receiver_id, endpoint_fingerprint=receiver.endpoint_fingerprint,
                              endpoint_generation=1, session_generation=1, trust_generation=3))
        clock = FakeClock(20)
        coordinator = PairingCoordinator(control, credentials, JsonReceiverStore(AtomicFiles(root=tmp_path)),
                                         fences, clock, FakeScheduler(clock), SequenceIds())
        ctx = context(lease=None, trust_generation=3)
        begun = await coordinator.begin_pairing(receiver, ctx)
        submission = asyncio.create_task(coordinator.submit_pairing(begun.value.challenge_id, SensitiveText("8496"), ctx))
        await control.entered.wait()
        if invalidate == "cancel":
            cancelling = asyncio.create_task(coordinator.cancel_pairing(receiver.receiver_id, begun.value.challenge_id, ctx))
        else:
            cancelling = asyncio.create_task(coordinator.mark_invalid(receiver.receiver_id, ctx))
        # Observe the invalidation and its early backend effects before allowing
        # the operation to complete. The final revoke must follow completion.
        await (credentials.cancelled if invalidate == "cancel" else credentials.invalidated).wait()
        assert coordinator.pending_cleanup(receiver.receiver_id)
        control.release.set()
        assert (await submission).kind == "Cancelled"
        assert isinstance(await cancelling, Ok)
        assert not coordinator.pending_cleanup(receiver.receiver_id)
        assert (await credentials.load_credential_ref(receiver.receiver_id)).value is None
        operations = [fields["operation"] for _, fields in control.journal.entries]
        assert operations[-1] is BackendOperation.revoke_pairing
        assert operations.index(stage) < len(operations) - 1
        assert list(tmp_path.rglob("*.json")) == []
    asyncio.run(scenario())


def test_pairing_never_persists_or_exports_input_tokens_or_references(tmp_path, monkeypatch):
    monkeypatch.setattr("airplay_linux.trust.keyring.uuid4", lambda: SimpleNamespace(hex="backend-correlation-canary"))

    async def scenario():
        control = Control(kind="password")
        credentials = BackendKeyringStore(control, features())
        receiver = target()
        receiver_store = JsonReceiverStore(AtomicFiles(root=tmp_path))
        assert isinstance(await receiver_store.put(SavedReceiver(
            receiver_id=receiver.receiver_id, service_metadata=receiver.service_records,
            display_name="Living Room", last_endpoint=receiver,
            continuity_fingerprint=receiver.endpoint_fingerprint, credential_ref=None, remembered_resolution=None,
        )), Ok)
        before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        fences = TrustFenceRegistry()
        fences.set(TrustFence(receiver_id=receiver.receiver_id, endpoint_fingerprint=receiver.endpoint_fingerprint,
                              endpoint_generation=1, session_generation=1, trust_generation=3))
        clock = FakeClock(20)
        coordinator = PairingCoordinator(control, credentials, receiver_store, fences, clock, FakeScheduler(clock), SequenceIds())
        ctx = context(lease=None, trust_generation=3)
        begun = await coordinator.begin_pairing(receiver, ctx)
        assert isinstance(begun, Ok)
        value = SensitiveText("canary-password")
        result = await coordinator.submit_pairing(begun.value.challenge_id, value, ctx)
        assert result.kind == "Trusted"
        after = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
        assert after == before
        log = ActivityLog()
        log.emit(safe_activity("pairing.completed", receiver.receiver_id, {}, "2026-10-17T00:00:00Z"))
        rendered = repr(result) + repr(begun) + repr(value) + repr(credentials) + repr(control.journal) + log.export()
        for canary in ("canary-password", "backend-correlation-canary", "8496"):
            assert canary not in rendered
            assert all(canary.encode() not in payload for payload in after.values())
        assert all(path.suffix == ".json" for path in after)
    asyncio.run(scenario())
