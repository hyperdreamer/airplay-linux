"""Trust/audio acceptance with real pairing transactions and secret canaries."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from airplay_linux.backend.control import AckReply, BackendOperation
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    Cancel, FailurePayload, Forget, KeepCleanup, Pair, RetryAudio, RetryRevocation,
    SetMuted, StartVideoOnly, Stop, SubmitPairing, TransportEventKind,
)
from airplay_linux.domain.operations import OpaqueBackendToken, SensitiveText
from airplay_linux.domain.resources import (
    AudioState, DisplayState, Err, ErrorCode, Mode, Ok, SessionPhase,
    TransportState, TrustState,
)
from airplay_linux.storage import AtomicFiles, JsonReceiverStore, JsonSessionStore
from airplay_linux.testing.adapters import Deferred, FakeCredentialStore, Journal
from airplay_linux.testing.builders import target
from airplay_linux.trust.coordinator import PairingCoordinator
from airplay_linux.trust.keyring import KeyringPairing

from test_lifecycle_matrix import (
    AcceptanceTransport, app_factory, event, ordered, resources,
)


async def trust_audio_transition(case, app_factory):
    receiver = target().receiver_id
    if case in {"startup_audio_decision", "runtime_audio_loss"}:
        app = await app_factory()
        if case == "startup_audio_decision":
            app.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_denied)))
        before = await app.start(audio=True)
        assert before.display is DisplayState.Owned
        if case == "startup_audio_decision":
            assert resources(before) == (TransportState.Idle, DisplayState.Owned, AudioState.Unavailable,
                                         TrustState.Unknown, True)
            assert before.phase is SessionPhase.AudioDecisionRequired
            assert not app.journal.calls("transport.start_stream")
            await app.send(StartVideoOnly(receiver_id=receiver))
            assert app.state().audio is AudioState.Disabled
        else:
            assert before.audio is AudioState.Enabled and before.transport is TransportState.Streaming
            app.journal.clear()
            app.transport.emit(event(app, TransportEventKind.audio_failed,
                                     payload=FailurePayload(diagnostic=diagnostic(ErrorCode.audio_unavailable))))
            await app.settle()
            assert resources(app.state()) == (TransportState.Streaming, DisplayState.Owned,
                                              AudioState.Unavailable, TrustState.Unknown, True)
            assert app.state().stream == before.stream
            await app.send(RetryAudio(receiver_id=receiver))
            assert app.state().audio is AudioState.Enabled
            retry, = app.journal.calls("transport.start_audio")
            assert retry["handle"]["lease_id"] == before.transport_lease_id
            assert retry["handle"]["daemon_instance_uuid"] == before.stream.daemon_instance_uuid
            assert retry["context"]["transport_lease_id"] == before.transport_lease_id
            assert not app.journal.calls("transport.stop_stream")
        assert app.state().display_handle == before.display_handle
        assert app.state().transport is TransportState.Streaming
        assert not app.display.removed
        return
    app = await app_factory()
    if case in {"retry_revocation", "keep_trust_blocked"}:
        await app.online()
        app.pairing.queue_result("forget_trust", Err(error=diagnostic(ErrorCode.trust_cleanup_required)))
        await app.send(Forget(receiver_id=receiver))
        assert app.state().trust is TrustState.CleanupRequired
        assert app.state().stream is None and app.state().display_handle is None
        app.journal.clear()
        if case == "retry_revocation":
            await app.send(RetryRevocation(receiver_id=receiver))
            assert app.state().trust is TrustState.Untrusted
            assert len(app.journal.calls("pairing.retry_revocation")) == 1
        else:
            await app.send(KeepCleanup(receiver_id=receiver))
            await app.send(Pair(receiver_id=receiver))
            assert app.state().trust is TrustState.CleanupRequired
            assert not app.journal.calls("pairing.begin_pairing")
        assert not app.transport.streams and not app.display.created
        return
    if case == "forget_no_stream":
        await app.online()
        await app.send(Pair(receiver_id=receiver))
        challenge = app.state().challenge
        await app.send(SubmitPairing(receiver_id=receiver, challenge_id=challenge.challenge_id,
                                     value=SensitiveText("8496")))
        assert app.state().trust is TrustState.Trusted
        assert app.state().stream is None and not app.state().intent_active
        app.journal.clear()
        await app.send(Forget(receiver_id=receiver))
        assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                          TrustState.Untrusted, False)
        assert receiver not in app.pairing.trusted
        assert len(app.journal.calls("pairing.forget_trust")) == 1
        assert not app.journal.calls("transport.start_stream")
        return
    owned = case in {"submit_pair_owned", "cancel_pair_owned", "pairing_during_connect"}
    if owned:
        app.transport.ready = case != "pairing_during_connect"
        before = await app.start()
        assert before.display is DisplayState.Owned
        assert before.transport is (TransportState.Connecting if case == "pairing_during_connect"
                                    else TransportState.Streaming)
        app.transport.emit(event(app, TransportEventKind.pairing_required))
        await app.settle()
        assert app.state().display_handle == before.display_handle
        ordered(app.journal, "transport.start_stream", "pairing.begin_pairing")
        ordered(app.journal, "transport.start_stream", "transport.stop_stream")
        assert not app.display.removed
    else:
        await app.online()
        assert app.state().stream is None and app.state().display_handle is None
        await app.send(Pair(receiver_id=receiver))
    pairing = app.state()
    assert pairing.trust is TrustState.Pairing and pairing.challenge is not None
    assert pairing.stream is None
    app.journal.clear()
    if case.startswith("cancel_pair"):
        await app.send(Cancel(receiver_id=receiver))
        assert resources(app.state()) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                          TrustState.Untrusted, False)
        assert not app.display.outputs and not app.transport.streams
        assert app.journal.calls("pairing.cancel_pairing")
    else:
        app.transport.ready = True
        await app.send(SubmitPairing(receiver_id=receiver, challenge_id=pairing.challenge.challenge_id,
                                     value=SensitiveText("8496")))
        after = app.state()
        assert after.trust is TrustState.Trusted and after.challenge is None
        if owned:
            assert after.transport is TransportState.Streaming and after.display is DisplayState.Owned
            assert after.display_handle == before.display_handle
            assert len(app.display.created) == 1
            ordered(app.journal, "pairing.submit_pairing", "transport.start_stream")
        else:
            assert resources(after) == (TransportState.Idle, DisplayState.None_, AudioState.Disabled,
                                        TrustState.Trusted, False)
            assert not app.journal.calls("transport.start_stream")


PIN_CANARY = "8496"
TOKEN_CANARY = "backend-token-canary-acceptance"
ERROR_CANARY = "provider-exception-canary-acceptance"


class PairingControl:
    def __init__(self):
        self.journal = Journal()
        self.fail = False

    async def request(self, operation, destination, payload, context):
        self.journal.record("control.request", operation=operation, payload=payload,
                            target=destination, context=context)
        if self.fail:
            raise RuntimeError(ERROR_CANARY)
        return Ok(value=AckReply(operation=operation.value, target=destination.canonical_host))


class Credentials(FakeCredentialStore):
    def __init__(self):
        super().__init__()
        self.commit_entered = asyncio.Event()
        self.commit_release = None
        self.coordinator = None

    async def begin_pairing_token(self, destination, context):
        return Ok(value=KeyringPairing(backend_token=OpaqueBackendToken(TOKEN_CANARY),
                                       credential_kind="pin"))

    async def commit_pairing(self, provisional, generation):
        self.commit_entered.set()
        if self.commit_release is not None:
            await self.commit_release.wait()
        return await super().commit_pairing(provisional, generation)


def coordinator_configure(directory, holder, *, hold_commit=False, fail=False):
    def configure(rig):
        files = AtomicFiles(directory / "state")
        rig.sessions, rig.receivers = JsonSessionStore(files), JsonReceiverStore(files)
        control, credentials = PairingControl(), Credentials()
        control.fail = fail
        credentials.journal = rig.journal
        control.journal = rig.journal
        if hold_commit:
            credentials.commit_release = Deferred()
        coordinator = PairingCoordinator(control, credentials, rig.receivers, rig.fences,
                                         rig.clock, rig.scheduler, rig.ids)
        holder.update(control=control, credentials=credentials, coordinator=coordinator)
        rig.pairing = coordinator
        rig.deps = replace(rig.deps, sessions=rig.sessions, receivers=rig.receivers, pairing=coordinator)
    return configure


@pytest.mark.parametrize("invalidate", ["endpoint", "stop", "forget"])
async def test_real_pairing_commit_after_invalidation_is_revoked_without_touching_other_receiver(
    app_factory, tmp_path, invalidate,
):
    holder = {}
    app = await app_factory(configure=coordinator_configure(tmp_path / "pairing", holder, hold_commit=True))
    before_b = await app.start(Mode.MIRROR, destination=target("b", address="192.0.2.11"), audio=True)
    await app.online()
    await app.send(Pair(receiver_id=target().receiver_id))
    challenge = app.state().challenge
    assert challenge is not None
    try:
        await app.send(SubmitPairing(receiver_id=target().receiver_id, challenge_id=challenge.challenge_id,
                                     value=SensitiveText(PIN_CANARY)))
        await asyncio.wait_for(holder["credentials"].commit_entered.wait(), 1)
        if invalidate == "endpoint":
            await app.online(target(endpoint_generation=2, address="192.0.2.99"))
        elif invalidate == "stop":
            await app.send(Stop(receiver_id=target().receiver_id))
        else:
            await app.send(Forget(receiver_id=target().receiver_id))
        holder["credentials"].commit_release.resolve(None)
        await app.settle()
        await app.send(RetryRevocation(receiver_id=target().receiver_id))
        assert app.state().trust is not TrustState.Trusted
        assert app.state().challenge is None
        assert target().receiver_id not in holder["credentials"].stored
        assert not holder["credentials"].provisional
        assert holder["credentials"].revoked
        assert app.state(target("b")) == before_b
        assert len(app.transport.streams) == 1
    finally:
        if not holder["credentials"].commit_release.resolved:
            holder["credentials"].commit_release.resolve(None)
        await app.settle()


@pytest.mark.parametrize("fail", [False, True])
async def test_pairing_canaries_cross_real_coordinator_and_never_enter_exports_or_disk(
    app_factory, tmp_path, fail,
):
    holder, directory = {}, tmp_path / "canaries"
    app = await app_factory(configure=coordinator_configure(directory, holder, fail=fail))
    await app.online()
    await app.send(Pair(receiver_id=target().receiver_id))
    challenge = app.state().challenge
    assert challenge is not None
    receipt = await app.send(SubmitPairing(receiver_id=target().receiver_id,
                                          challenge_id=challenge.challenge_id, value=SensitiveText(PIN_CANARY)))
    assert app.state().trust is (TrustState.Untrusted if fail else TrustState.Trusted)
    assert app.journal.calls("control.request")
    assert isinstance(await app.runtime.persistence.flush(), Ok)
    assert isinstance(await app.runtime.shutdown(), Ok)
    public = [repr(receipt), repr(app.state()), repr(app.journal), app.runtime.deps.activity.export(),
              repr(holder["coordinator"].pending_cleanup(target().receiver_id))]
    public.extend(path.read_text() for path in tmp_path.rglob("*") if path.is_file())
    assert list((directory / "state").rglob("*.json"))
    for canary in (PIN_CANARY, TOKEN_CANARY, ERROR_CANARY):
        assert all(canary not in value for value in public), canary


async def test_audio_retry_completion_for_old_stream_cannot_enable_new_stream(app_factory):
    app = await app_factory()
    before = await app.start(audio=True)
    app.transport.emit(event(app, TransportEventKind.audio_failed))
    await app.settle()
    held = Deferred()
    app.transport.queue_result("start_audio", held)
    try:
        await app.send(RetryAudio(receiver_id=before.receiver_id))
        assert app.state().audio is AudioState.Starting
        await app.send(Stop(receiver_id=before.receiver_id))
        after = await app.start(audio=False)
        held.resolve(Ok(value=None))
        await app.settle()
        assert app.state() == after
        assert after.transport is TransportState.Streaming and after.audio is AudioState.Disabled
        assert after.transport_lease_id != before.transport_lease_id
    finally:
        if not held.resolved:
            held.resolve(Ok(value=None))
        await app.settle()


async def test_new_challenge_rejects_old_prompt_submission_before_pairing_port(app_factory):
    app = await app_factory()
    await app.online()
    await app.send(Pair(receiver_id=target().receiver_id))
    first = app.state().challenge
    await app.send(Cancel(receiver_id=target().receiver_id))
    await app.send(Pair(receiver_id=target().receiver_id))
    current = app.state()
    assert current.challenge.challenge_id != first.challenge_id
    app.journal.clear()
    await app.send(SubmitPairing(receiver_id=target().receiver_id, challenge_id=first.challenge_id,
                                 value=SensitiveText(PIN_CANARY)))
    assert app.state().challenge == current.challenge and app.state().trust is TrustState.Pairing
    assert not app.journal.calls("pairing.submit_pairing")
