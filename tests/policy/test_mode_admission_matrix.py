"""Policy mode permission never exceeds the real transport's admission.

``EnvironmentProbe`` answers "may this profile start this mode?". The integrated
:class:`~airplay_linux.transport.doubletake.DoubleTakeTransport` is what finally
admits or refuses that start, and it re-checks backend prerequisites the policy
must not omit: ``_admit`` needs proven foreground control, an owner handshake and
the backend keyring operation, and ``start_stream`` needs the multi-target
operation. A policy gate narrower than that advertises starts the transport
refuses; a gate wider than it hides capability the transport would accept.

This matrix drives the real transport (over deterministic doubles, with zero real
backend I/O) and the policy probe with the *same* ``BackendFeatures`` object, so
neither kind of drift can ship unnoticed.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.backend.contract import AckReply
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    Handshake,
)
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.records import DaemonRecord
from airplay_linux.domain.resources import (
    AudioPolicy,
    Err,
    ErrorCode,
    Mode,
    Ok,
    Size,
    TrustState,
    VirtualOutputSource,
)
from airplay_linux.display.helper import HelperFeatures
from airplay_linux.policy.capabilities import EnvironmentProbe, can_start
from airplay_linux.storage import AtomicFiles, JsonReceiverStore
from airplay_linux.testing.adapters import FakeCapture, Journal, SequenceIds
from airplay_linux.testing.builders import context, physical, target
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore

PLASMA = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "KDE",
    "KDE_SESSION_VERSION": "6",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}
FINGERPRINT = "a" * 64
VIRTUAL = VirtualOutputSource(
    connector="Virtual-1", kscreen_output_id="9", size=Size(width=1920, height=1080),
)
#: The profile the transport integration tests use: every operation proven.
CAPABILITIES = ApplicationContractProfile(socket_path="/run/private/daemon.sock").features
BOOL_FLAGS = tuple(
    name for name in CAPABILITIES.__dataclass_fields__ if name not in {"profile", "version"}
)


def _daemon(number: int = 9) -> DaemonRecord:
    return DaemonRecord(
        schema_version=1, instance_uuid=InstanceId(UUID(int=number)),
        owner_token="private-owner-canary", app=ProcessIdentity(pid=100, start_ticks=1),
        supervisor=ProcessIdentity(pid=200 + number, start_ticks=2),
        daemon=ProcessIdentity(pid=300 + number, start_ticks=3), protocol_version="v1",
        socket_path=f"/run/private/{number}/daemon.sock", lifecycle="running",
        runtime_directory=f"/run/private/{number}",
    )


class _Subscription:
    def close(self) -> None:
        pass


class _Registry:
    """Daemon admission double that counts every ``ensure`` (backend I/O)."""

    def __init__(self, record: DaemonRecord) -> None:
        self.record = record
        self.ensure_calls = 0

    def subscribe(self, callback) -> _Subscription:
        return _Subscription()

    async def ensure(self):
        self.ensure_calls += 1
        return Ok(value=self.record)


class _Control:
    """Typed backend double: ownership proof plus one connect acknowledgement."""

    def __init__(self, record: DaemonRecord) -> None:
        self.record = record
        self.journal = Journal()

    def subscribe(self, callback) -> _Subscription:
        return _Subscription()

    async def handshake(self, owner_token, instance_uuid):
        return Ok(value=Handshake(
            instance_uuid=instance_uuid, owner_token=owner_token,
            daemon=self.record.daemon, protocol_version="v1",
        ))

    async def request(self, operation, receiver, payload, ctx, *, dispatch_fence=None):
        # Mirror the published adapter: evaluate the fence before any effect.
        if dispatch_fence is not None:
            fenced = dispatch_fence()
            if isinstance(fenced, Err):
                return fenced
        self.journal.record(
            "request", operation=operation, target=receiver, payload=payload,
            context=ctx,
        )
        return Ok(value=AckReply(
            operation=operation.value,
            target=receiver.canonical_host if receiver is not None else None,
            state="streaming" if operation is BackendOperation.connect else None,
        ))

    async def close(self) -> None:
        pass


class _Audio:
    async def check(self):
        return Ok(value=None)


class _Keyring:
    async def __call__(self) -> bool:
        return True


async def _ready(value):
    return Ok(value=value)


class Harness:
    """The real transport plus the policy probe over the same capabilities."""

    def __init__(self, root, capabilities, source) -> None:
        self.capabilities = capabilities
        self.source = source
        self.record = _daemon()
        self.registry = _Registry(self.record)
        self.control = _Control(self.record)
        self.capture = FakeCapture()
        self.capture.add_physical(physical())
        clock = FakeClock(20)
        self.transport = DoubleTakeTransport(
            lambda record: self.control, self.registry, self.capture,
            BackendKeyringStore(self.control, capabilities),
            JsonReceiverStore(AtomicFiles(root=root)),
            TrustFenceRegistry(), capabilities, clock, FakeScheduler(clock),
            SequenceIds(100),
        )
        self.receiver = target()
        self.transport.update_target(self.receiver, self.receiver.receiver_id)

    async def report(self):
        builder = EnvironmentProbe(
            environment=PLASMA,
            command_finder=lambda name: f"/usr/bin/{name}",
            module_finder=lambda name: True,
            backend_probe=lambda: _ready(self.capabilities),
            helper_probe=lambda: _ready(HelperFeatures(
                version="24.08.2", distinct_outputs=True,
                capability_fingerprint=FINGERPRINT,
            )),
            capture=self.capture,
            audio_probe=_Audio(),
            keyring_probe=_Keyring(),
        )
        return await builder.check()

    async def start(self):
        return await self.transport.start_stream(
            self.receiver, self.source, AudioPolicy(enabled=False), context(),
        )


def _source(mode: Mode):
    return physical() if mode is Mode.MIRROR else VIRTUAL


def _assert_refused_before_backend_io(harness: Harness) -> None:
    assert harness.registry.ensure_calls == 0
    assert harness.control.journal.names() == []


@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
@pytest.mark.parametrize("flag", BOOL_FLAGS)
async def test_policy_mode_permission_matches_the_real_transport(flag, mode, tmp_path):
    """For every proven backend operation, policy and transport must agree.

    Flipping one flag proves both directions: policy must refuse what the real
    transport refuses (no over-claiming) and must keep permitting what the real
    transport still accepts (no over-gating). Refusals must also happen before
    the daemon is ensured or a request is dispatched.
    """
    capabilities = replace(CAPABILITIES, **{flag: False})
    harness = Harness(tmp_path, capabilities, _source(mode))

    report = await harness.report()
    permitted = can_start(report, mode, TrustState.Trusted)
    started = await harness.start()

    policy_started = isinstance(permitted, Ok)
    transport_started = isinstance(started, Ok)
    assert policy_started == transport_started, (
        f"{mode.value} with {flag}=False: policy={permitted!r} transport={started!r}"
    )
    if transport_started:
        assert harness.registry.ensure_calls == 1
        assert harness.control.journal.names() == ["request"]
    else:
        _assert_refused_before_backend_io(harness)


@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_missing_backend_keyring_is_refused_by_both_policy_and_transport(
    mode, tmp_path,
):
    """``DoubleTakeTransport._admit`` refuses every start without this proof."""
    harness = Harness(tmp_path, replace(CAPABILITIES, keyring=False), _source(mode))

    report = await harness.report()
    permitted = can_start(report, mode, TrustState.Trusted)
    started = await harness.start()

    assert isinstance(permitted, Err)
    assert permitted.error.code is ErrorCode.unsupported_operation
    assert isinstance(started, Err)
    assert started.error.code is ErrorCode.dependency_incompatible
    _assert_refused_before_backend_io(harness)
    assert not report.mirror and not report.virtual_display


@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_missing_multi_target_is_refused_by_both_policy_and_transport(
    mode, tmp_path,
):
    """``start_stream`` refuses with ``unsupported_operation`` without it."""
    harness = Harness(tmp_path, replace(CAPABILITIES, multi_target=False), _source(mode))

    report = await harness.report()
    permitted = can_start(report, mode, TrustState.Trusted)
    started = await harness.start()

    assert isinstance(permitted, Err)
    assert permitted.error.code is ErrorCode.unsupported_operation
    assert isinstance(started, Err)
    assert started.error.code is ErrorCode.unsupported_operation
    _assert_refused_before_backend_io(harness)
    assert not report.mirror and not report.virtual_display


@pytest.mark.parametrize("mode", [Mode.MIRROR, Mode.VIRTUAL])
async def test_the_fully_proven_profile_starts_through_the_real_transport(mode, tmp_path):
    """The positive control: the matrix would otherwise pass vacuously."""
    harness = Harness(tmp_path, CAPABILITIES, _source(mode))

    report = await harness.report()
    assert can_start(report, mode, TrustState.Trusted) == Ok(value=None)

    started = await harness.start()
    assert isinstance(started, Ok)
    assert harness.registry.ensure_calls == 1
    assert [call["operation"] for call in harness.control.journal.calls("request")] == [
        BackendOperation.connect,
    ]
