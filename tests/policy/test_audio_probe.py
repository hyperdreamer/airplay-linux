"""Audio capability is proven repeatably before a stream exists.

The probe is introspection, never a permission grant: it checks that PipeWire
publishes its socket and that the portal exposes the ScreenCast interface, but
it does not create a portal session, open a PipeWire stream, or ask the user for
permission. Every test injects both evidence sources, so no test opens a real
socket, contacts D-Bus, or launches desktop tooling.

The start decision is equally explicit. A failed or skipped probe while audio is
enabled is ``decision_required``; only the user's explicit video-only choice (or
an explicitly disabled policy) produces ``video_only``.
"""

from __future__ import annotations

from collections import deque

import pytest

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.resources import (
    AudioPolicy,
    Err,
    ErrorCode,
    Ok,
    RecoveryAction,
)
from airplay_linux.policy.audio import (
    PORTAL_OBJECT_PATH,
    PORTAL_SERVICE,
    REQUIRED_PORTAL_INTERFACES,
    SCREENCAST_INTERFACE,
    PipeWirePortalAudioProbe,
    PipeWireSocket,
    audio_start_policy,
    probe_audio,
)

# --------------------------------------------------------------------------- fakes


def _next(values: deque[object]) -> object:
    """One scripted value: pop while more remain, otherwise repeat the last."""
    return values.popleft() if len(values) > 1 else values[0]


class FakePortal:
    """A D-Bus capability client double: introspection in, interface names out."""

    def __init__(self, *results: frozenset[str] | None) -> None:
        self._results = deque(results) if results else deque([REQUIRED_PORTAL_INTERFACES])
        self.calls: list[tuple[str, str]] = []
        self.failure: Exception | None = None

    async def interfaces(self, service: str, path: str) -> frozenset[str] | None:
        self.calls.append((service, path))
        if self.failure is not None:
            raise self.failure
        return _next(self._results)  # type: ignore[return-value]


class FakePipeWire:
    """The PipeWire socket check, scripted and counted."""

    def __init__(self, *values: bool) -> None:
        self._values = deque(values) if values else deque([True])
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return _next(self._values)  # type: ignore[return-value]


class ScriptedAudioProbe:
    """A scripted :class:`AudioProbe` for the generic probe/start helpers."""

    def __init__(self, *results: object) -> None:
        self._results = deque(results) if results else deque([Ok(value=None)])
        self.calls = 0
        self.failure: Exception | None = None

    async def check(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return _next(self._results)


def audio_probe(
    *, portal: FakePortal | None = None, pipewire: FakePipeWire | None = None,
) -> PipeWirePortalAudioProbe:
    return PipeWirePortalAudioProbe(
        portal=portal if portal is not None else FakePortal(),
        pipewire=pipewire if pipewire is not None else FakePipeWire(),
    )


# ------------------------------------------------------------------ start policy


def test_audio_failure_requires_explicit_choice():
    failure = Err(error=diagnostic(ErrorCode.audio_unavailable))
    assert audio_start_policy(AudioPolicy(), failure, False) == "decision_required"
    assert audio_start_policy(AudioPolicy(), failure, True) == "video_only"


def test_available_audio_starts_audio_unless_video_only_was_explicit():
    assert audio_start_policy(AudioPolicy(), Ok(value=None), False) == "start_audio"
    assert audio_start_policy(AudioPolicy(), Ok(value=None), True) == "video_only"


def test_an_enabled_audio_start_never_implicitly_downgrades_a_missing_result():
    """A caller that skipped the probe did not prove audio; it must ask the user."""
    assert audio_start_policy(AudioPolicy(), None, False) == "decision_required"


def test_disabled_audio_is_a_choice_not_a_failure():
    failure = Err(error=diagnostic(ErrorCode.audio_unavailable))
    assert audio_start_policy(AudioPolicy(enabled=False), failure, False) == "video_only"
    assert audio_start_policy(AudioPolicy(enabled=False), None, False) == "video_only"


async def test_disabled_audio_skips_probing_entirely():
    probe = ScriptedAudioProbe(Err(error=diagnostic(ErrorCode.audio_unavailable)))
    assert await probe_audio(AudioPolicy(enabled=False), probe) is None
    assert probe.calls == 0


async def test_enabled_audio_probes_and_preserves_a_failure():
    failure = Err(error=diagnostic(ErrorCode.audio_unavailable))
    probe = ScriptedAudioProbe(failure)
    assert await probe_audio(AudioPolicy(), probe) is failure
    assert probe.calls == 1


async def test_probe_audio_propagates_an_explicit_denial_unchanged():
    denial = Err(error=diagnostic(ErrorCode.audio_denied))
    probe = ScriptedAudioProbe(denial)
    result = await probe_audio(AudioPolicy(), probe)
    assert result is denial
    assert audio_start_policy(AudioPolicy(), result, False) == "decision_required"


async def test_probe_audio_sanitizes_an_unexpected_adapter_exception():
    probe = ScriptedAudioProbe()
    probe.failure = RuntimeError("portal token=secret /home/user")
    result = await probe_audio(AudioPolicy(), probe)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable
    assert "secret" not in result.error.message
    assert "secret" not in (result.error.detail or "")


def test_audio_failure_diagnostic_offers_retry_video_only_and_stop():
    failure = Err(error=diagnostic(ErrorCode.audio_unavailable))
    assert set(failure.error.recovery) >= {
        RecoveryAction.retry_audio,
        RecoveryAction.video_only,
        RecoveryAction.stop,
    }
    result = audio_start_policy(AudioPolicy(), failure, False)
    assert result == "decision_required"


@pytest.mark.parametrize(
    ("policy", "result", "explicit"),
    [
        (object(), Ok(value=None), False),
        (AudioPolicy(enabled=1), Ok(value=None), False),  # type: ignore[arg-type]
        (AudioPolicy(), object(), False),
        (AudioPolicy(), Ok(value="started"), False),
        (AudioPolicy(), Ok(value=None), 1),
    ],
)
def test_start_policy_rejects_malformed_values(policy, result, explicit):
    with pytest.raises(TypeError):
        audio_start_policy(policy, result, explicit)  # type: ignore[arg-type]


# ------------------------------------------------------------------- probe


async def test_pipewire_portal_probe_reports_ok_when_both_evidence_sources_exist():
    portal = FakePortal()
    pipewire = FakePipeWire(True)
    result = await audio_probe(portal=portal, pipewire=pipewire).check()
    assert result == Ok(value=None)
    assert portal.calls == [(PORTAL_SERVICE, PORTAL_OBJECT_PATH)]
    assert SCREENCAST_INTERFACE in REQUIRED_PORTAL_INTERFACES


async def test_probe_is_repeatable_and_never_caches_a_failure():
    portal = FakePortal(None, REQUIRED_PORTAL_INTERFACES)
    pipewire = FakePipeWire(True)
    probe = audio_probe(portal=portal, pipewire=pipewire)

    first = await probe.check()
    second = await probe.check()

    assert isinstance(first, Err)
    assert second == Ok(value=None)
    assert pipewire.calls == 2
    assert len(portal.calls) == 2


async def test_missing_pipewire_is_audio_unavailable_and_skips_the_portal():
    portal = FakePortal()
    pipewire = FakePipeWire(False)
    result = await audio_probe(portal=portal, pipewire=pipewire).check()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable
    assert "pipewire-0" in (result.error.detail or "")
    assert portal.calls == []


async def test_portal_without_screencast_is_audio_unavailable():
    portal = FakePortal(frozenset({"org.freedesktop.portal.RemoteDesktop"}))
    result = await audio_probe(portal=portal, pipewire=FakePipeWire(True)).check()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable
    assert SCREENCAST_INTERFACE in (result.error.detail or "")


async def test_portal_introspection_failure_is_audio_unavailable_without_backend_text():
    portal = FakePortal()
    portal.failure = RuntimeError("SERVICE NOT FOUND: /run/user/x")
    result = await audio_probe(portal=portal, pipewire=FakePipeWire(True)).check()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable
    assert "SERVICE NOT FOUND" not in result.error.message
    assert "SERVICE NOT FOUND" not in (result.error.detail or "")


async def test_probe_audio_does_not_accept_a_malformed_success():
    class Malformed:
        async def check(self):
            return Ok(value="started")

    result = await probe_audio(AudioPolicy(), Malformed())
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable


def test_the_pipewire_socket_check_requires_an_absolute_runtime_and_socket_name():
    seen: list[str] = []

    def exists(path: str) -> bool:
        seen.append(path)
        return True

    assert PipeWireSocket({"XDG_RUNTIME_DIR": "/run/user/1000"}, exists)() is True
    assert seen == ["/run/user/1000/pipewire-0"]

    seen.clear()
    assert PipeWireSocket({"XDG_RUNTIME_DIR": "relative"}, exists)() is False
    assert PipeWireSocket({}, exists)() is False
    assert seen == []


async def test_probe_never_requests_permission_or_opens_a_stream():
    """The only operation the probe performs is read-only D-Bus introspection."""
    portal = FakePortal()
    await audio_probe(portal=portal, pipewire=FakePipeWire(True)).check()
    # FakePortal exposes exactly one operation; receiving the introspection call
    # is the whole observable interaction. No session/stream verb exists here.
    assert portal.calls == [(PORTAL_SERVICE, PORTAL_OBJECT_PATH)]
