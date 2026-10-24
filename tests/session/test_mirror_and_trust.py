from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import (
    EffectEvent,
    Forget,
    Pair,
    RetryAudio,
    RetryRevocation,
    SetMuted,
    Start,
    Stop,
    SubmitPairing,
)
from airplay_linux.domain.operations import (
    ChallengeId,
    InstanceId,
    OperationContext,
    OperationId,
    PairingResult,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.resources import AudioPolicy, Err, ErrorCode, Mode, Ok, SessionPhase, StartRequest, TrustState
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import physical, target


async def _online_rig() -> tuple[SessionRig, object]:
    rig = SessionRig(build_lifecycle_handlers)
    destination = target()
    rig.discovery.add_target(destination)
    rig.capture.add_physical(physical())
    await rig.settle()
    return rig, destination


async def _start_mirror(
    rig: SessionRig, destination: object, *, audio: AudioPolicy | None = None,
) -> None:
    completion: Deferred[object] = Deferred()
    rig.transport.queue_result("start_stream", completion)
    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(
                mode=Mode.MIRROR,
                source=physical(),
                audio=audio if audio is not None else AudioPolicy(),
            ),
        )
    )
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.transport_lease_id is not None
    assert state.operation_id is not None
    completion.resolve(
        Ok(
            value=StreamHandle(
                receiver_id=destination.receiver_id,
                endpoint_generation=destination.endpoint_generation,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                backend_stream_id="stream-test",
                daemon_instance_uuid=InstanceId(UUID(int=9)),
                lease_id=state.transport_lease_id,
                start_operation_id=state.operation_id,
            )
        )
    )
    await rig.settle()


@pytest.mark.asyncio
async def test_retry_audio_can_resolve_pre_video_audio_decision() -> None:
    rig, destination = await _online_rig()
    rig.audio.queue_result("check", Err(error=diagnostic(ErrorCode.audio_unavailable)))
    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(mode=Mode.MIRROR, source=physical()),
        )
    )
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).intent_stage.value == "audio_decision"

    completion: Deferred[object] = Deferred()
    rig.audio.queue_result("check", Ok(value=None))
    rig.transport.queue_result("start_stream", completion)
    await rig.manager.dispatch(RetryAudio(receiver_id=destination.receiver_id))
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert state.transport is not None
    assert state.operation_id is not None
    assert state.transport_lease_id is not None
    completion.resolve(
        Ok(
            value=StreamHandle(
                receiver_id=destination.receiver_id,
                endpoint_generation=destination.endpoint_generation,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                backend_stream_id="stream-retry-audio",
                daemon_instance_uuid=InstanceId(UUID(int=9)),
                lease_id=state.transport_lease_id,
                start_operation_id=state.operation_id,
            )
        )
    )
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).stream is not None


@pytest.mark.asyncio
async def test_disabled_audio_skips_probe_and_starts_video_only() -> None:
    rig, destination = await _online_rig()
    completion: Deferred[object] = Deferred()
    rig.transport.queue_result("start_stream", completion)
    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(
                mode=Mode.MIRROR,
                source=physical(),
                audio=AudioPolicy(enabled=False),
            ),
        )
    )
    await rig.settle()

    assert "audio_probe.check" not in rig.audio.journal.names()
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.operation_id is not None
    completion.resolve(
        Ok(
            value=StreamHandle(
                receiver_id=destination.receiver_id,
                endpoint_generation=destination.endpoint_generation,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                backend_stream_id="stream-no-audio",
                daemon_instance_uuid=InstanceId(UUID(int=9)),
                lease_id=state.transport_lease_id,
                start_operation_id=state.operation_id,
            )
        )
    )
    await rig.settle()

    assert rig.manager.snapshot(destination.receiver_id).stream is not None


@pytest.mark.asyncio
async def test_mirror_rejects_a_substituted_physical_connector() -> None:
    rig, destination = await _online_rig()
    substituted = physical("HDMI-1")
    rig.capture.queue_result("select", Ok(value=substituted))
    rig.capture.queue_result("verify", Ok(value=None))

    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(mode=Mode.MIRROR, source=physical()),
        )
    )
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert "transport.start_stream" not in rig.transport.journal.names()
    assert rig.capture.released == [substituted]
    assert state.stream is None
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.unsupported_source


@pytest.mark.asyncio
async def test_mirror_uses_only_physical_capture_and_does_not_create_a_display() -> None:
    rig, destination = await _online_rig()
    await _start_mirror(rig, destination)

    state = rig.manager.snapshot(destination.receiver_id)
    assert "display.create" not in rig.display.journal.names()
    assert state.physical_source == physical()
    assert state.stream is not None
    assert state.phase is SessionPhase.Mirroring


@pytest.mark.asyncio
async def test_pairing_can_complete_without_an_output_and_keeps_session_generation() -> None:
    rig, destination = await _online_rig()

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    pairing = rig.manager.snapshot(destination.receiver_id)
    assert pairing.challenge is not None
    assert pairing.trust is TrustState.Pairing
    generation = pairing.session_generation

    await rig.manager.dispatch(
        SubmitPairing(
            receiver_id=destination.receiver_id,
            challenge_id=pairing.challenge.challenge_id,
            value=SensitiveText("1234"),
        )
    )
    await rig.settle()

    trusted = rig.manager.snapshot(destination.receiver_id)
    assert trusted.challenge is None
    assert trusted.trust is TrustState.Trusted
    assert trusted.session_generation == generation
    assert trusted.display_handle is None
    assert trusted.stream is None


@pytest.mark.asyncio
async def test_stale_trusted_completion_cannot_commit_trust() -> None:
    rig, destination = await _online_rig()

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    challenge = rig.manager.snapshot(destination.receiver_id).challenge
    assert challenge is not None

    rig.manager.ingest(
        EffectEvent(
            receiver_id=destination.receiver_id,
            operation_id=challenge.operation_id,
            session_generation=challenge.session_generation + 1,
            kind="trust:pairing_submit",
            result=Ok(value=PairingResult(kind="Trusted", credential_ref=None, diagnostic=None)),
            resource=None,
        )
    )
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.CleanupRequired
    assert state.challenge is None


@pytest.mark.asyncio
async def test_muted_state_changes_only_after_transport_confirmation() -> None:
    rig, destination = await _online_rig()
    await _start_mirror(rig, destination)
    stream = rig.manager.snapshot(destination.receiver_id).stream
    assert stream is not None

    await rig.manager.dispatch(SetMuted(receiver_id=destination.receiver_id, muted=True))
    await rig.settle()

    assert rig.transport.muted[stream.lease_id] is True
    assert rig.manager.snapshot(destination.receiver_id).muted is True


@pytest.mark.asyncio
async def test_stale_challenge_submission_is_ignored() -> None:
    rig, destination = await _online_rig()

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    before = rig.manager.snapshot(destination.receiver_id)
    assert before.challenge is not None

    await rig.manager.dispatch(
        SubmitPairing(
            receiver_id=destination.receiver_id,
            challenge_id=ChallengeId(UUID(int=999999)),
            value=SensitiveText("1234"),
        )
    )
    await rig.settle()

    after = rig.manager.snapshot(destination.receiver_id)
    assert after.challenge == before.challenge
    assert "pairing.submit_pairing" not in rig.pairing.journal.names()


@pytest.mark.asyncio
async def test_forget_is_refused_while_an_active_stream_exists() -> None:
    rig, destination = await _online_rig()
    await _start_mirror(rig, destination)
    before = rig.manager.snapshot(destination.receiver_id)

    await rig.manager.dispatch(Forget(receiver_id=destination.receiver_id))
    await rig.settle()

    after = rig.manager.snapshot(destination.receiver_id)
    assert after.stream == before.stream
    assert "pairing.forget_trust" not in rig.pairing.journal.names()


@pytest.mark.asyncio
async def test_cancel_during_portal_selection_releases_late_source() -> None:
    rig, destination = await _online_rig()
    selected: Deferred[object] = Deferred()
    rig.capture.queue_result("select", selected)

    await rig.manager.dispatch(
        Start(
            receiver_id=destination.receiver_id,
            request=StartRequest(mode=Mode.MIRROR, source=physical()),
        )
    )
    await rig.settle()
    assert not selected.resolved

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    selected.resolve(Ok(value=physical()))
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert rig.capture.released == [physical()]
    assert state.stream is None
    assert "transport.start_stream" not in rig.transport.journal.names()


@pytest.mark.asyncio
async def test_stop_cancels_pairing_begin_that_has_not_returned_a_challenge() -> None:
    rig, destination = await _online_rig()
    begin: Deferred[object] = Deferred()
    rig.pairing.queue_result("begin_pairing", begin)

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Pairing

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert "pairing.cancel_pairing" in rig.pairing.journal.names()
    cancel = next(
        entry for name, entry in rig.pairing.journal.entries
        if name == "pairing.cancel_pairing"
    )
    assert cancel["challenge_id"] is None

    logged_context = rig.pairing.journal.entries[0][1]["context"]
    begin_context = OperationContext(
        operation_id=OperationId(logged_context["operation_id"]),
        session_generation=logged_context["session_generation"],
        endpoint_generation=logged_context["endpoint_generation"],
        transport_lease_id=None,
        trust_generation=logged_context["trust_generation"],
    )
    late_challenge = rig.pairing.script_challenge(destination, begin_context)
    begin.resolve(Ok(value=late_challenge))
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert state.challenge is None
    assert state.trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_pair_is_rejected_while_ordered_cleanup_is_pending() -> None:
    rig, destination = await _online_rig()
    await _start_mirror(rig, destination)
    release: Deferred[object] = Deferred()
    rig.capture.queue_result("release", release)

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).cleanup_pending

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()

    assert "pairing.begin_pairing" not in rig.pairing.journal.names()
    assert rig.manager.snapshot(destination.receiver_id).trust is not TrustState.Pairing

    release.resolve(Ok(value=None))
    await rig.settle()


@pytest.mark.asyncio
async def test_stop_cancels_pairing_before_reporting_available() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).challenge is not None

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.Untrusted
    assert state.challenge is None
    assert state.phase is SessionPhase.Available
    assert "pairing.cancel_pairing" in rig.pairing.journal.names()


@pytest.mark.asyncio
async def test_failed_pairing_cancellation_locks_trust_until_revocation_retry() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    rig.pairing.queue_result(
        "cancel_pairing", Err(error=diagnostic(ErrorCode.cleanup_timeout)),
    )

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    blocked = rig.manager.snapshot(destination.receiver_id)
    assert blocked.trust is TrustState.CleanupRequired
    assert blocked.phase is SessionPhase.TrustCleanupRequired

    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    still_blocked = rig.manager.snapshot(destination.receiver_id)
    assert still_blocked.session_generation == blocked.session_generation
    assert still_blocked.trust is TrustState.CleanupRequired

    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.pairing.journal.names().count("pairing.begin_pairing") == 1

    await rig.manager.dispatch(RetryRevocation(receiver_id=destination.receiver_id))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_pairing_expiry_waits_for_backend_cancellation() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    cancellation: Deferred[object] = Deferred()
    rig.pairing.queue_result("cancel_pairing", cancellation)

    rig.scheduler.advance(120.0)
    await rig.settle()
    expired = rig.manager.snapshot(destination.receiver_id)
    assert expired.trust is TrustState.RevocationPending
    assert expired.challenge is None
    assert not cancellation.resolved

    cancellation.resolve(Ok(value=None))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_pairing_rejects_three_invalid_submissions() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()

    for _ in range(3):
        challenge = rig.manager.snapshot(destination.receiver_id).challenge
        assert challenge is not None
        await rig.manager.dispatch(
            SubmitPairing(
                receiver_id=destination.receiver_id,
                challenge_id=challenge.challenge_id,
                value=SensitiveText(""),
            )
        )
        await rig.settle()

    state = rig.manager.snapshot(destination.receiver_id)
    assert state.challenge is None
    assert state.trust is TrustState.Untrusted
    assert state.last_error is not None
    assert state.last_error.code is ErrorCode.receiver_rejected


@pytest.mark.asyncio
async def test_forget_retires_pairing_challenge_before_backend_confirmation() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    forgotten: Deferred[object] = Deferred()
    rig.pairing.queue_result("forget_trust", forgotten)

    await rig.manager.dispatch(Forget(receiver_id=destination.receiver_id))
    await rig.settle()
    pending = rig.manager.snapshot(destination.receiver_id)
    assert pending.challenge is None
    assert pending.trust is TrustState.RevocationPending

    forgotten.resolve(Ok(value=None))
    await rig.settle()
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_late_trusted_pairing_with_failed_compensation_blocks_trust() -> None:
    rig, destination = await _online_rig()
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await rig.settle()
    challenge = rig.manager.snapshot(destination.receiver_id).challenge
    assert challenge is not None
    submission: Deferred[object] = Deferred()
    rig.pairing.queue_result("submit_pairing", submission)

    await rig.manager.dispatch(
        SubmitPairing(
            receiver_id=destination.receiver_id,
            challenge_id=challenge.challenge_id,
            value=SensitiveText("1234"),
        )
    )
    await rig.settle()
    await rig.manager.dispatch(Stop(receiver_id=destination.receiver_id))
    await rig.settle()
    rig.pairing.queue_result(
        "revoke_trust", Err(error=diagnostic(ErrorCode.cleanup_timeout)),
    )

    submission.resolve(
        PairingResult(kind="Trusted", credential_ref=None, diagnostic=None),
    )
    await rig.settle()
    state = rig.manager.snapshot(destination.receiver_id)
    assert state.trust is TrustState.CleanupRequired
    assert state.phase is SessionPhase.TrustCleanupRequired
