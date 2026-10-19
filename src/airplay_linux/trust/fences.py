"""Synchronous trust generations, independent of backend completion order."""

from __future__ import annotations

from dataclasses import dataclass, replace

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, _validate_generation
from airplay_linux.domain.operations import ChallengeId, PairingChallenge
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result


@dataclass(frozen=True, kw_only=True)
class TrustFence:
    receiver_id: ReceiverId
    endpoint_fingerprint: str | None
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    active_challenge_id: ChallengeId | None = None

    def __post_init__(self) -> None:
        for name in ("endpoint_generation", "session_generation", "trust_generation"):
            _validate_generation(name, getattr(self, name))


def _same_target(challenge: PairingChallenge, fence: TrustFence) -> bool:
    return (
        challenge.receiver_id == fence.receiver_id
        and challenge.endpoint_fingerprint == fence.endpoint_fingerprint
        and challenge.endpoint_generation == fence.endpoint_generation
        and challenge.session_generation == fence.session_generation
        and challenge.trust_generation == fence.trust_generation
    )


def fence_matches(challenge: PairingChallenge, fence: TrustFence, now: float) -> bool:
    return (
        _same_target(challenge, fence)
        and fence.active_challenge_id == challenge.challenge_id
        and challenge.created_at <= now < challenge.expires_at
    )


class TrustFenceRegistry:
    """All methods finish before suspension; invalidation never waits for I/O."""

    def __init__(self) -> None:
        self._fences: dict[ReceiverId, TrustFence] = {}

    def set(self, fence: TrustFence) -> None:
        previous = self.get(fence.receiver_id)
        if previous is not None and (
            previous.endpoint_fingerprint != fence.endpoint_fingerprint
            or previous.endpoint_generation != fence.endpoint_generation
            or previous.session_generation != fence.session_generation
            or previous.trust_generation != fence.trust_generation
        ):
            fence = replace(fence, active_challenge_id=None)
        self._fences[fence.receiver_id] = fence

    def get(self, receiver_id: ReceiverId) -> TrustFence | None:
        return self._fences.get(receiver_id)

    def activate(self, challenge: PairingChallenge) -> Result[None]:
        current = self.get(challenge.receiver_id)
        if current is None or not _same_target(challenge, current) or current.active_challenge_id not in (None, challenge.challenge_id):
            return Err(error=diagnostic(ErrorCode.stale_event))
        self._fences[challenge.receiver_id] = replace(current, active_challenge_id=challenge.challenge_id)
        return Ok(value=None)

    def clear_challenge(self, receiver_id: ReceiverId, challenge_id: ChallengeId) -> None:
        current = self.get(receiver_id)
        if current is not None and current.active_challenge_id == challenge_id:
            self._fences[receiver_id] = replace(current, active_challenge_id=None)
