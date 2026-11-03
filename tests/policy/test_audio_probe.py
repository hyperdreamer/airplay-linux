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

import os
import socket
import stat
from collections import deque
from types import SimpleNamespace

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
    DBusCapabilityClient,
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


async def test_a_successful_probe_is_repeated_when_pipewire_disappears():
    """A cached success must not outlive the daemon that produced it."""
    portal = FakePortal()
    pipewire = FakePipeWire(True, False)
    probe = audio_probe(portal=portal, pipewire=pipewire)

    assert await probe.check() == Ok(value=None)
    second = await probe.check()

    assert isinstance(second, Err)
    assert second.error.code is ErrorCode.audio_unavailable
    assert "pipewire-0" in (second.error.detail or "")
    assert pipewire.calls == 2
    # The second check re-observes PipeWire itself, so the portal is not asked.
    assert len(portal.calls) == 1
    assert audio_start_policy(AudioPolicy(), second, False) == "decision_required"


async def test_a_successful_probe_is_repeated_when_the_portal_disappears():
    portal = FakePortal(REQUIRED_PORTAL_INTERFACES, None)
    pipewire = FakePipeWire(True)
    probe = audio_probe(portal=portal, pipewire=pipewire)

    assert await probe.check() == Ok(value=None)
    second = await probe.check()

    assert isinstance(second, Err)
    assert second.error.code is ErrorCode.audio_unavailable
    assert PORTAL_SERVICE in (second.error.detail or "")
    assert pipewire.calls == 2
    assert len(portal.calls) == 2
    assert audio_start_policy(AudioPolicy(), second, False) == "decision_required"


async def test_repeated_successes_are_probed_each_time_instead_of_replayed():
    portal = FakePortal()
    pipewire = FakePipeWire(True)
    probe = audio_probe(portal=portal, pipewire=pipewire)

    assert await probe.check() == Ok(value=None)
    assert await probe.check() == Ok(value=None)

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

    def reachable(path: str) -> bool:
        seen.append(path)
        return True

    assert PipeWireSocket({"XDG_RUNTIME_DIR": "/run/user/1000"}, reachable)() is True
    assert seen == ["/run/user/1000/pipewire-0"]

    seen.clear()
    assert PipeWireSocket({"XDG_RUNTIME_DIR": "relative"}, reachable)() is False
    assert PipeWireSocket({}, reachable)() is False
    assert seen == []


def test_the_default_pipewire_probe_rejects_a_missing_socket(tmp_path):
    assert PipeWireSocket({"XDG_RUNTIME_DIR": str(tmp_path)})() is False


def test_the_default_pipewire_probe_rejects_an_ordinary_file(tmp_path):
    """Socket identity alone is not reachability, and a file is not a socket."""
    (tmp_path / "pipewire-0").write_text("stale placeholder")

    assert PipeWireSocket({"XDG_RUNTIME_DIR": str(tmp_path)})() is False


def test_the_default_pipewire_probe_rejects_a_stale_socket(tmp_path):
    """A bound-then-closed socket still looks like a socket to metadata checks.

    The endpoint no longer answers, so the probe must report unavailable rather
    than inferring a live PipeWire daemon from the leftover path.
    """
    path = tmp_path / "pipewire-0"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stale.bind(str(path))
    finally:
        stale.close()

    assert stat.S_ISSOCK(os.stat(path).st_mode)
    unreachable = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert unreachable.connect_ex(str(path)) != 0
    finally:
        unreachable.close()

    assert PipeWireSocket({"XDG_RUNTIME_DIR": str(tmp_path)})() is False


def test_the_default_pipewire_probe_accepts_a_listening_socket(tmp_path):
    path = tmp_path / "pipewire-0"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
        assert PipeWireSocket({"XDG_RUNTIME_DIR": str(tmp_path)})() is True
    finally:
        listener.close()


async def test_the_default_probe_reports_a_stale_socket_as_audio_unavailable(tmp_path):
    """The review's counterexample: portal present, PipeWire path stale."""
    path = tmp_path / "pipewire-0"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        stale.bind(str(path))
    finally:
        stale.close()

    portal = FakePortal()
    probe = PipeWirePortalAudioProbe(
        portal=portal, environment={"XDG_RUNTIME_DIR": str(tmp_path)},
    )
    result = await probe.check()

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.audio_unavailable
    assert "pipewire-0" in (result.error.detail or "")
    assert portal.calls == []
    assert audio_start_policy(AudioPolicy(), result, False) == "decision_required"


async def test_probe_never_requests_permission_or_opens_a_stream():
    """The only operation the probe performs is read-only D-Bus introspection."""
    portal = FakePortal()
    await audio_probe(portal=portal, pipewire=FakePipeWire(True)).check()
    # FakePortal exposes exactly one operation; receiving the introspection call
    # is the whole observable interaction. No session/stream verb exists here.
    assert portal.calls == [(PORTAL_SERVICE, PORTAL_OBJECT_PATH)]

# ------------------------------------------------- session-bus introspection

#: A faithful excerpt of what xdg-desktop-portal 1.22 exports.  The
#: ``power-saver-enabled`` property is a real member name that ``dbus-next``
#: refuses to parse, so the document (not the library's object model) is the
#: evidence this client must be able to read.
_PORTAL_DOCUMENT = """<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object
 Introspection 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<!-- GDBus 2.90.1 -->
<node>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg type="s" name="interface_name" direction="in"/>
      <arg type="s" name="property_name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
  </interface>
  <interface name="org.freedesktop.portal.ScreenCast">
    <method name="CreateSession">
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="o" name="handle" direction="out"/>
    </method>
    <property type="u" name="version" access="read"/>
  </interface>
  <interface name="org.freedesktop.portal.PowerProfileMonitor">
    <property type="b" name="power-saver-enabled" access="read"/>
    <property type="u" name="version" access="read"/>
  </interface>
  <node name="request"/>
</node>
"""


class FakeBus:
    """A message bus double: one raw introspection reply, optionally failing."""

    def __init__(self, body: object, *, failure: Exception | None = None) -> None:
        self._body = body
        self._failure = failure
        self.messages: list[object] = []
        self.disconnected = False

    async def call(self, message: object) -> object:
        self.messages.append(message)
        if self._failure is not None:
            raise self._failure
        return SimpleNamespace(body=self._body)

    def disconnect(self) -> None:
        self.disconnected = True


def _connected(bus: object) -> object:
    async def connect() -> object:
        return bus

    return connect


async def test_the_session_bus_client_reads_a_real_introspection_document():
    """The portal publishes hyphenated members; they must not hide interfaces."""
    bus = FakeBus([_PORTAL_DOCUMENT])
    client = DBusCapabilityClient(timeout=0.1, connect=_connected(bus))

    names = await client.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH)

    assert names is not None
    assert SCREENCAST_INTERFACE in names
    assert "org.freedesktop.portal.PowerProfileMonitor" in names
    # Only the object's own interfaces: a child node's are a different object.
    assert bus.messages and bus.messages[0].destination == PORTAL_SERVICE
    assert bus.messages[0].path == PORTAL_OBJECT_PATH
    assert bus.messages[0].interface == "org.freedesktop.DBus.Introspectable"
    assert bus.messages[0].member == "Introspect"
    assert bus.disconnected is True


async def test_the_session_bus_client_requires_a_text_document():
    bus = FakeBus([{"not": "xml"}])
    client = DBusCapabilityClient(timeout=0.1, connect=_connected(bus))

    assert await client.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH) is None
    assert bus.disconnected is True


async def test_the_session_bus_client_reports_a_malformed_document_as_unproven():
    bus = FakeBus(["<node><interface name='unterminated'></node>"])
    client = DBusCapabilityClient(timeout=0.1, connect=_connected(bus))

    assert await client.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH) is None


async def test_the_session_bus_client_disconnects_after_a_failed_call():
    bus = FakeBus([], failure=RuntimeError("bus closed"))
    client = DBusCapabilityClient(timeout=0.1, connect=_connected(bus))

    assert await client.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH) is None
    assert bus.disconnected is True


async def test_the_session_bus_client_refuses_when_the_bus_is_unreachable():
    async def connect() -> object:
        raise RuntimeError("no session bus")

    client = DBusCapabilityClient(timeout=0.1, connect=connect)

    assert await client.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH) is None
