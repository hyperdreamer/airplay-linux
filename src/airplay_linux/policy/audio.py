"""Audio capability evidence and the explicit pre-ready audio decision.

Audio is enabled by default, so a start with audio may not silently continue when
the audio path is unproven. This module keeps the two questions separate:

* :class:`PipeWirePortalAudioProbe` proves *capability* by introspection only: a
  PipeWire socket **accepts a connection** and the desktop portal exposes the
  ScreenCast interface the capture path uses. Existence of the socket path is not
  proof; a stale socket from a stopped daemon is reported unavailable. That is
  still not a permission grant: a normal start still asks the portal, and that
  request can be denied or cancelled later.
* :func:`audio_start_policy` turns the probe result into one of exactly three
  outcomes. While audio is enabled, an absent or failed probe is
  ``decision_required``; only the user's explicit video-only choice produces
  ``video_only``. An explicitly disabled policy skips probing entirely and is a
  recorded session choice, not an inferred downgrade.

The probe is read-only, repeatable, and uncached: retrying after fixing PipeWire
or installing the portal must be able to observe the new evidence. It never
creates a portal session, opens a stream, or prompts for permission.
"""

from __future__ import annotations

import asyncio
import os
import socket
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Final, Literal, Protocol
from xml.parsers import expat

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import AudioProbe
from airplay_linux.domain.resources import (
    AudioPolicy,
    Err,
    ErrorCode,
    Ok,
    Result,
)

#: The desktop portal service and object every capability probe addresses.
PORTAL_SERVICE: Final[str] = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH: Final[str] = "/org/freedesktop/portal/desktop"

#: The portal interface that carries the audio-bearing screen-cast request.
SCREENCAST_INTERFACE: Final[str] = "org.freedesktop.portal.ScreenCast"

#: Every portal interface the audio path requires. Presence is proven by D-Bus
#: introspection; absence is reported by its exact name.
REQUIRED_PORTAL_INTERFACES: Final[frozenset[str]] = frozenset({SCREENCAST_INTERFACE})

#: The PipeWire daemon socket inside ``XDG_RUNTIME_DIR``.
PIPEWIRE_SOCKET_NAME: Final[str] = "pipewire-0"

#: Bound on the read-only connection probe; a live local daemon answers at once.
PIPEWIRE_PROBE_TIMEOUT: Final[float] = 0.25

#: The Secret Service interface a usable remembered-pairing keyring exposes.
SECRET_SERVICE: Final[str] = "org.freedesktop.secrets"
SECRET_SERVICE_PATH: Final[str] = "/org/freedesktop/secrets"
SECRET_SERVICE_INTERFACE: Final[str] = "org.freedesktop.Secret.Service"

#: The D-Bus object operation that returns an object's interfaces as XML.
INTROSPECTABLE_INTERFACE: Final[str] = "org.freedesktop.DBus.Introspectable"

#: D-Bus introspection is a control call; it uses the design's 5 second bound.
INTROSPECTION_TIMEOUT: Final[float] = 5.0

#: The only three start outcomes; there is no implicit fourth state.
AudioStartDecision = Literal["start_audio", "decision_required", "video_only"]


class InterfaceCapabilityClient(Protocol):
    """Read-only portal introspection.

    ``None`` means the service or object could not be introspected. The client
    must not create a session, select a source, start a stream, or prompt.
    """

    async def interfaces(self, service: str, path: str) -> frozenset[str] | None: ...


class PipeWireSocket:
    """Affirmative PipeWire evidence: the daemon's socket accepts a connection.

    A socket file's presence is *not* availability: a crashed or stopped daemon
    leaves the path behind, so a stale socket looks exactly like a live one to
    every metadata check. The probe therefore connects to the endpoint and
    closes it again, which only succeeds while a process is accepting on that
    path. It never speaks the protocol, creates a stream, or asks permission;
    a successful connect claims availability only, not that the portal will
    grant audio to a particular request.
    """

    def __init__(
        self,
        environment: Mapping[str, str] | None = None,
        reachable: Callable[[str], bool] | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._reachable = _socket_reachable if reachable is None else reachable

    def __call__(self) -> bool:
        runtime = self._environment.get("XDG_RUNTIME_DIR")
        if (
            type(runtime) is not str
            or not runtime.startswith("/")
            or "\0" in runtime
            or len(runtime) > 4096
        ):
            return False
        path = os.path.join(runtime, PIPEWIRE_SOCKET_NAME)
        try:
            return bool(self._reachable(path))
        except Exception:  # noqa: BLE001 - an unreachable socket proves nothing
            return False


def _socket_reachable(path: str) -> bool:
    """Whether some process is accepting connections on this Unix socket.

    ``connect`` is bounded: a local PipeWire daemon answers immediately, and the
    timeout only stops a wedged listener from stalling startup. Every failure
    mode (missing path, ordinary file, stale socket, refused or timed-out
    connect) returns ``False`` so the audio capability is reported unavailable.
    """
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return False
    except OSError:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(PIPEWIRE_PROBE_TIMEOUT)
        sock.connect(path)
    except OSError:
        return False
    finally:
        sock.close()
    return True


def _external_uid_auth(uid: int | None) -> object | None:
    """An EXTERNAL authenticator naming ``uid``, or ``None`` for the default.

    ``dbus-next`` states ``os.getuid()`` in its EXTERNAL handshake. That value is
    the namespace-local identity, so inside ``unshare -r`` it reads as ``0``
    while the bus still sees the spawning user's kernel uid and rejects the
    mismatch. A credential witness therefore names the uid it inherited from its
    spawner; an ordinary caller keeps the library default.
    """
    if uid is None:
        return None
    from dbus_next.auth import AuthExternal

    class _ExternalUid(AuthExternal):
        def _authentication_start(self, negotiate_unix_fd: bool = False) -> str:
            self.negotiate_unix_fd = negotiate_unix_fd
            return f"AUTH EXTERNAL {str(uid).encode().hex()}"

    return _ExternalUid()


async def _connect_session_bus(uid: int | None = None) -> object:
    """Open the session bus through ``dbus-next``, imported only when needed."""
    from dbus_next import BusType
    from dbus_next.aio import MessageBus

    return await MessageBus(
        bus_type=BusType.SESSION, auth=_external_uid_auth(uid),
    ).connect()


def _interface_names(document: object) -> frozenset[str] | None:
    """Interface names from an introspection document, tolerantly.

    ``dbus-next`` parses introspection into typed objects and validates every
    member name against the D-Bus grammar. A real service may still publish a
    member the library rejects: xdg-desktop-portal exports
    ``org.freedesktop.portal.PowerProfileMonitor`` with a
    ``power-saver-enabled`` property, and introspection then raises instead of
    describing the object. Treating that parse failure as "the portal is
    unreachable" denied audio on every host, so this reads the document itself:
    only element names and ``name`` attributes are evidence here, and a member
    the server chose to publish cannot hide an interface we need.

    Parameter entities are not parsed, so a hostile DTD cannot be expanded, and
    only the object's own interfaces count - a child node describes another
    object.
    """
    if type(document) is not str:
        return None
    names: list[str] = []
    depth = 0
    parser = expat.ParserCreate()
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)

    def start(tag: str, attributes: Mapping[str, str]) -> None:
        nonlocal depth
        if depth <= 1 and tag == "interface":
            name = attributes.get("name")
            if type(name) is str and name:
                names.append(name)
        depth += 1

    def end(_tag: str) -> None:
        nonlocal depth
        depth -= 1

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(document, True)
    except expat.ExpatError:
        return None
    return frozenset(names) or None


class DBusCapabilityClient:
    """Session-bus introspection through ``dbus-next``.

    The bus connection is opened only for this call and closed again. Every
    failure is reported as ``None`` so the audio probe can name the missing
    capability without copying a backend exception into a diagnostic. The
    document is requested as raw text instead of through the library's typed
    object model, because that model rejects member names the real portal
    publishes (see :func:`_interface_names`).
    """

    def __init__(
        self,
        timeout: float = INTROSPECTION_TIMEOUT,
        *,
        uid: int | None = None,
        connect: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        if uid is not None and (type(uid) is not int or uid < 0):
            raise ValueError("session bus uid must be a non-negative integer")
        self._timeout = float(timeout)
        self._uid = uid
        self._connect = (
            (lambda: _connect_session_bus(uid)) if connect is None else connect
        )

    async def interfaces(self, service: str, path: str) -> frozenset[str] | None:
        try:
            from dbus_next import Message
        except Exception:  # noqa: BLE001 - an absent client library proves nothing
            return None
        bus = None
        try:
            bus = await asyncio.wait_for(self._connect(), self._timeout)
            reply = await asyncio.wait_for(
                bus.call(
                    Message(
                        destination=service, path=path,
                        interface=INTROSPECTABLE_INTERFACE, member="Introspect",
                    )
                ),
                self._timeout,
            )
            body = getattr(reply, "body", None)
            if not body or type(body[0]) is not str:
                return None
            return _interface_names(body[0])
        except Exception:  # noqa: BLE001 - an unreachable portal proves nothing
            return None
        finally:
            disconnect = getattr(bus, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:  # noqa: BLE001 - the result is already decided
                    pass


def _audio_failure(detail: str) -> Err:
    """An audio-capability failure naming the exact missing evidence."""
    return Err(error=replace(diagnostic(ErrorCode.audio_unavailable), detail=detail))


def _valid_policy(policy: object) -> AudioPolicy:
    if not isinstance(policy, AudioPolicy):
        raise TypeError("audio policy must be an AudioPolicy")
    if type(policy.enabled) is not bool:
        raise TypeError("audio policy enabled must be a boolean")
    return policy


def _valid_probe(probe: object) -> Result[None] | None:
    if probe is None or isinstance(probe, Err):
        return probe
    if isinstance(probe, Ok):
        if probe.value is not None:
            raise TypeError("an Ok audio probe result must carry None")
        return probe
    raise TypeError("an audio probe result must be Ok, Err, or None")


async def secret_service_available(client: InterfaceCapabilityClient) -> bool:
    """Whether a Secret Service that can hold remembered trust is introspectable.

    This is capability evidence only. Unlocking the keyring, storing a PIN, and
    committing a credential remain later, explicit operations.
    """
    try:
        interfaces = await client.interfaces(SECRET_SERVICE, SECRET_SERVICE_PATH)
    except Exception:  # noqa: BLE001 - an unreachable keyring proves nothing
        return False
    return type(interfaces) is frozenset and SECRET_SERVICE_INTERFACE in interfaces


class PipeWirePortalAudioProbe:
    """Repeatable, pre-stream audio capability evidence.

    Both evidence sources are injected so tests never touch a real socket or the
    session bus. The default client is read-only introspection, and the default
    PipeWire check proves reachability with a bounded connect and close; neither
    starts a process or creates a stream.
    """

    def __init__(
        self,
        *,
        portal: InterfaceCapabilityClient | None = None,
        pipewire: Callable[[], bool] | None = None,
        environment: Mapping[str, str] | None = None,
        timeout: float = INTROSPECTION_TIMEOUT,
    ) -> None:
        self._portal = DBusCapabilityClient() if portal is None else portal
        self._pipewire = (
            PipeWireSocket(environment) if pipewire is None else pipewire
        )
        self._timeout = float(timeout)

    async def check(self) -> Result[None]:
        try:
            available = self._pipewire() is True
        except Exception:  # noqa: BLE001 - an unreadable probe proves nothing
            available = False
        if not available:
            return _audio_failure(PIPEWIRE_SOCKET_NAME)

        try:
            interfaces = await asyncio.wait_for(
                self._portal.interfaces(PORTAL_SERVICE, PORTAL_OBJECT_PATH),
                self._timeout,
            )
        except Exception:  # noqa: BLE001 - an unreachable portal proves nothing
            interfaces = None
        if type(interfaces) is not frozenset:
            return _audio_failure(PORTAL_SERVICE)

        missing = REQUIRED_PORTAL_INTERFACES - interfaces
        if missing:
            return _audio_failure(", ".join(sorted(missing)))
        return Ok(value=None)


def audio_start_policy(
    policy: AudioPolicy,
    probe: Result[None] | None,
    explicit_video_only: bool,
) -> AudioStartDecision:
    """The one start decision for the current audio evidence.

    ``explicit_video_only`` is the user's recorded choice and always wins. An
    explicitly disabled policy also starts video without audio. Otherwise a
    successful probe starts audio, and every failed or absent result requires an
    explicit decision; it is never treated as video-only.
    """
    checked = _valid_policy(policy)
    if type(explicit_video_only) is not bool:
        raise TypeError("explicit_video_only must be a boolean")
    _valid_probe(probe)

    if explicit_video_only:
        return "video_only"
    if not checked.enabled:
        return "video_only"
    if isinstance(probe, Ok):
        return "start_audio"
    return "decision_required"


async def probe_audio(policy: AudioPolicy, probe: AudioProbe) -> Result[None] | None:
    """Probe only when audio is enabled; ``None`` records the disabled choice.

    The caller logs the ``None`` outcome as an explicit session choice and starts
    video only. An enabled policy always yields a typed result, so a caller
    cannot mistake a skipped probe for a successful one.
    """
    checked = _valid_policy(policy)
    if not checked.enabled:
        return None
    check = getattr(probe, "check", None)
    if not callable(check):
        raise TypeError("an audio probe must expose an async check()")
    try:
        result = await check()
    except Exception:  # noqa: BLE001 - an adapter failure is unproven audio
        return Err(error=diagnostic(ErrorCode.audio_unavailable))
    if isinstance(result, Err):
        return result
    if isinstance(result, Ok) and result.value is None:
        return result
    # A malformed success is not proof that audio started; keep it unproven.
    return Err(error=diagnostic(ErrorCode.audio_unavailable))


__all__ = [
    "AudioStartDecision",
    "DBusCapabilityClient",
    "INTROSPECTABLE_INTERFACE",
    "INTROSPECTION_TIMEOUT",
    "InterfaceCapabilityClient",
    "PIPEWIRE_PROBE_TIMEOUT",
    "PIPEWIRE_SOCKET_NAME",
    "PORTAL_OBJECT_PATH",
    "PORTAL_SERVICE",
    "PipeWirePortalAudioProbe",
    "PipeWireSocket",
    "REQUIRED_PORTAL_INTERFACES",
    "SCREENCAST_INTERFACE",
    "SECRET_SERVICE",
    "SECRET_SERVICE_INTERFACE",
    "SECRET_SERVICE_PATH",
    "audio_start_policy",
    "probe_audio",
    "secret_service_available",
]
