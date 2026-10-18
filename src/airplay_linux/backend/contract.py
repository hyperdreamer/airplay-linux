"""Published doubletake capability negotiation and typed response decoding.

The installed doubletake command exposes a small, documented control surface.  This
module records only evidence that can be derived from that surface.  In particular,
flags that merely mention daemon mode, keyring storage, or screen capture do not
turn on application-owned lifecycle, trust, or source capabilities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final, TypeAlias
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain import ports
from airplay_linux.domain.operations import InstanceId, ProcessIdentity
from airplay_linux.domain.resources import (
    Diagnostic,
    Err,
    ErrorCode,
    Ok,
    Result,
)


_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"(?i)(?:doubletake(?:-ctl)?|version|release|package)[^0-9]{0,24}"
    r"(?:v)?(\d+\.\d+(?:\.\d+)?)"
)
_KNOWN_STATES: Final[frozenset[str]] = frozenset(
    {"idle", "discovering", "connecting", "streaming", "pin_required"}
)
_CREDENTIAL_KINDS: Final[frozenset[str]] = frozenset({"pin", "password"})


def _diagnostic(code: ErrorCode, *, command: str | None = None) -> Diagnostic:
    """Build a catalog diagnostic without carrying backend-owned text."""
    return diagnostic(code, command=command)


def _incompatible() -> Err:
    return Err(error=_diagnostic(ErrorCode.dependency_incompatible))


@dataclass(frozen=True, kw_only=True)
class BackendFeatures:
    """Capabilities proven by a published backend's own public contract."""

    profile: str
    version: str
    foreground_control: bool
    owner_handshake: bool
    multi_target: bool
    target_bound_pairing: bool
    physical_source: bool
    virtual_source: bool
    audio_probe: bool
    audio_restart: bool
    target_mute: bool
    keyring: bool
    provisional_commit: bool
    cancel_pairing: bool
    revoke_pairing: bool
    query_modes: bool

    def __post_init__(self) -> None:
        if type(self.profile) is not str or type(self.version) is not str:
            raise TypeError("backend profile and version must be strings")
        for name in self.__dataclass_fields__:
            if name in {"profile", "version"}:
                continue
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"backend capability {name} must be a boolean")

    def supports(self, capability: str) -> bool:
        """Return one named capability without accepting unknown truthy values."""
        if type(capability) is not str or capability not in self.__dataclass_fields__:
            return False
        if capability in {"profile", "version"}:
            return False
        return getattr(self, capability) is True


@dataclass(frozen=True, kw_only=True)
class BackendStream:
    """The safe subset of one published status stream."""

    device: str
    target: str
    state: str
    has_audio: bool
    audio_muted: bool
    credential_kind: str | None = None

    def __post_init__(self) -> None:
        for name in ("device", "target", "state"):
            if type(getattr(self, name)) is not str or not getattr(self, name):
                raise TypeError(f"{name} must be non-empty text")
        if self.state not in _KNOWN_STATES:
            raise ValueError("unknown backend stream state")
        for name in ("has_audio", "audio_muted"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if self.credential_kind is not None and self.credential_kind not in _CREDENTIAL_KINDS:
            raise ValueError("unknown credential kind")

    @property
    def device_ip(self) -> str:
        return self.target


@dataclass(frozen=True, kw_only=True, repr=False)
class StatusReply:
    """A typed status snapshot; raw backend errors are intentionally absent."""

    state: str
    device: str | None
    target: str | None
    has_audio: bool
    audio_muted: bool
    needs_credential: bool
    credential_kind: str | None
    streams: tuple[BackendStream, ...]
    error: None = None
    owner_token: str | None = field(default=None, repr=False)
    instance_uuid: InstanceId | None = field(default=None, repr=False)
    socket_path: str | None = field(default=None, repr=False)
    protocol_version: str | None = field(default=None, repr=False)
    daemon: ProcessIdentity | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.state not in _KNOWN_STATES:
            raise ValueError("unknown backend state")
        if self.device is not None and type(self.device) is not str:
            raise TypeError("device must be text or None")
        if self.target is not None and type(self.target) is not str:
            raise TypeError("target must be text or None")
        for name in ("has_audio", "audio_muted", "needs_credential"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if self.credential_kind is not None and self.credential_kind not in _CREDENTIAL_KINDS:
            raise ValueError("unknown credential kind")
        if self.needs_credential is False and self.credential_kind is not None:
            raise ValueError("credential kind without a pending credential")
        if self.needs_credential and self.credential_kind is None:
            raise ValueError("pending credential has no typed kind")
        if type(self.streams) is not tuple or not all(
            isinstance(stream, BackendStream) for stream in self.streams
        ):
            raise TypeError("streams must be a tuple of BackendStream")

    def __repr__(self) -> str:
        return (
            "StatusReply("
            f"state={self.state!r}, device={self.device!r}, target={self.target!r}, "
            f"has_audio={self.has_audio!r}, audio_muted={self.audio_muted!r}, "
            f"needs_credential={self.needs_credential!r}, "
            f"credential_kind={self.credential_kind!r}, streams={self.streams!r})"
        )

    @property
    def ok(self) -> bool:
        return True

    @property
    def device_ip(self) -> str | None:
        return self.target

    @property
    def needs_pin(self) -> bool:
        return self.credential_kind == "pin"



@dataclass(frozen=True, kw_only=True)
class BackendDevice:
    """A discovered device field set documented by doubletake-ctl."""

    name: str
    model: str
    target: str
    port: int
    device_id: str

    @property
    def ip(self) -> str:
        return self.target


@dataclass(frozen=True, kw_only=True)
class DevicesReply:
    devices: tuple[BackendDevice, ...]
    kind: str = "devices"


@dataclass(frozen=True, kw_only=True)
class AckReply:
    """A successful command acknowledgement with no free-form backend text."""

    operation: str
    state: str | None = None
    target: str | None = None
    muted: bool | None = None


BackendReply: TypeAlias = StatusReply | DevicesReply | AckReply


def _exact_dict(value: object) -> dict[str, Any] | None:
    if type(value) is dict:
        return value
    return None


def _json_value(value: object) -> object | None:
    if type(value) is bytes or type(value) is bytearray:
        try:
            return json.loads(bytes(value).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
    if type(value) is str:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    if type(value) is dict:
        return value
    return None


def _required_text(data: dict[str, Any], name: str) -> str | None:
    value = data.get(name)
    if type(value) is not str or not value:
        return None
    return value


def _optional_text(data: dict[str, Any], name: str) -> tuple[bool, str | None]:
    if name not in data:
        return True, None
    value = data[name]
    if value is None:
        return True, None
    if type(value) is not str:
        return False, None
    return True, value


def _required_bool(data: dict[str, Any], name: str) -> bool | None:
    value = data.get(name)
    return value if type(value) is bool else None


def _optional_bool(data: dict[str, Any], name: str, default: bool) -> bool | None:
    if name not in data:
        return default
    value = data[name]
    return value if type(value) is bool else None


def _credential(data: dict[str, Any], name: str = "credential_kind") -> str | None | object:
    if name not in data or data[name] is None:
        return None
    value = data[name]
    if type(value) is str and value == "":
        return None
    if type(value) is not str or value not in _CREDENTIAL_KINDS:
        return _INVALID
    return value


_INVALID = object()


def _decode_stream(value: object) -> BackendStream | None:
    data = _exact_dict(value)
    if data is None:
        return None
    device = _required_text(data, "device")
    target = _required_text(data, "device_ip")
    if target is None:
        target = _required_text(data, "target")
    state = _required_text(data, "state")
    has_audio = _required_bool(data, "has_audio")
    audio_muted = _required_bool(data, "audio_muted")
    credential = _credential(data)
    if (
        device is None
        or target is None
        or state is None
        or has_audio is None
        or audio_muted is None
        or credential is _INVALID
    ):
        return None
    try:
        return BackendStream(
            device=device,
            target=target,
            state=state,
            has_audio=has_audio,
            audio_muted=audio_muted,
            credential_kind=credential,
        )
    except (TypeError, ValueError):
        return None


def _metadata(data: dict[str, Any]) -> tuple[bool, dict[str, object]]:
    """Decode optional ownership evidence without retaining arbitrary fields."""
    result: dict[str, object] = {}
    for name in ("owner_token", "socket_path", "protocol_version"):
        valid, value = _optional_text(data, name)
        if not valid or (value == "" and name != "protocol_version"):
            return False, {}
        result[name] = value

    if "instance_uuid" in data:
        raw_uuid = data["instance_uuid"]
        if type(raw_uuid) is not str:
            return False, {}
        try:
            result["instance_uuid"] = InstanceId(UUID(raw_uuid))
        except (ValueError, AttributeError):
            return False, {}
    else:
        result["instance_uuid"] = None

    pid = data.get("daemon_pid")
    ticks = data.get("daemon_start_ticks")
    if pid is None and ticks is None:
        result["daemon"] = None
    elif type(pid) is int and type(ticks) is int and pid > 0 and ticks >= 0:
        result["daemon"] = ProcessIdentity(pid=pid, start_ticks=ticks)
    else:
        return False, {}
    return True, result


def decode_status(value: object) -> StatusReply | Err:
    """Decode the published status shape, rejecting old or ambiguous shapes."""
    parsed = _json_value(value)
    data = _exact_dict(parsed)
    if data is None:
        return _incompatible()
    if _required_bool(data, "ok") is not True:
        return _incompatible()

    state = _required_text(data, "state")
    top_has_audio = _required_bool(data, "has_audio")
    top_audio_muted = _required_bool(data, "audio_muted")
    if state is None or top_has_audio is None or top_audio_muted is None:
        return _incompatible()

    if "streams" in data:
        raw_streams = data["streams"]
        if type(raw_streams) is not list:
            return _incompatible()
        streams: list[BackendStream] = []
        for raw_stream in raw_streams:
            stream = _decode_stream(raw_stream)
            if stream is None:
                return _incompatible()
            streams.append(stream)
    else:
        # Empty current responses may omit an omitempty streams field.  The
        # aggregate fields above are still required, which rejects pre-v0.5
        # status objects that cannot describe audio or credential state.
        streams = []

    needs = _optional_bool(data, "needs_credential", False)
    legacy_pin = _optional_bool(data, "needs_pin", False)
    if needs is None or legacy_pin is None:
        return _incompatible()
    credential = _credential(data)
    if credential is _INVALID:
        return _incompatible()
    if legacy_pin and credential not in (None, "pin"):
        return _incompatible()
    stream_credentials = {stream.credential_kind for stream in streams if stream.credential_kind}
    if len(stream_credentials) > 1 and state != "pin_required":
        return _incompatible()
    if credential is None and needs and legacy_pin:
        credential = "pin"
    if credential is None and needs and len(stream_credentials) == 1:
        credential = next(iter(stream_credentials))
    if credential is None and stream_credentials and state == "pin_required":
        credential = sorted(stream_credentials)[0]
    if stream_credentials:
        needs = True
    if needs and credential is None:
        return _incompatible()
    if credential is not None:
        needs = True
    if state == "pin_required" and not needs:
        return _incompatible()

    valid, device = _optional_text(data, "device")
    if not valid:
        return _incompatible()
    valid, target = _optional_text(data, "device_ip")
    if not valid:
        return _incompatible()
    valid, metadata = _metadata(data)
    if not valid:
        return _incompatible()
    try:
        return StatusReply(
            state=state,
            device=device,
            target=target,
            has_audio=top_has_audio,
            audio_muted=top_audio_muted,
            needs_credential=needs,
            credential_kind=credential,
            streams=tuple(streams),
            owner_token=metadata["owner_token"],
            instance_uuid=metadata["instance_uuid"],
            socket_path=metadata["socket_path"],
            protocol_version=metadata["protocol_version"],
            daemon=metadata["daemon"],
        )
    except (TypeError, ValueError):
        return _incompatible()


def decode_devices(value: object) -> DevicesReply | Err:
    """Decode discover/devices responses without preserving arbitrary strings."""
    parsed = _json_value(value)
    data = _exact_dict(parsed)
    if data is None or _required_bool(data, "ok") is not True:
        return _incompatible()
    raw_devices = data.get("devices")
    if type(raw_devices) is not list:
        return _incompatible()
    devices: list[BackendDevice] = []
    for raw_device in raw_devices:
        item = _exact_dict(raw_device)
        if item is None:
            return _incompatible()
        name = _required_text(item, "name")
        model = _required_text(item, "model")
        target = _required_text(item, "ip")
        device_id = _required_text(item, "device_id")
        port = item.get("port")
        if (
            name is None
            or model is None
            or target is None
            or device_id is None
            or type(port) is not int
            or not 1 <= port <= 65535
        ):
            return _incompatible()
        devices.append(
            BackendDevice(
                name=name,
                model=model,
                target=target,
                port=port,
                device_id=device_id,
            )
        )
    return DevicesReply(devices=tuple(devices))


def _version(text: str) -> str:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else "unknown"


def _has_line(text: str, word: str) -> bool:
    return re.search(rf"(?im)^\s*(?:[-*]\s*)?{re.escape(word)}(?:\s|\[|<|$)", text) is not None


def _has_target_command(text: str, word: str) -> bool:
    return re.search(
        rf"(?im)^\s*(?:[-*]\s*)?{re.escape(word)}\s+\[target\]",
        text,
    ) is not None


def features_from_help(sender_help: str, ctl_help: str) -> BackendFeatures:
    """Decode only explicit, independently named evidence from help text."""
    if type(sender_help) is not str or type(ctl_help) is not str:
        sender_help = ""
        ctl_help = ""
    combined = f"{sender_help}\n{ctl_help}"
    version = _version(combined)
    profile = "unknown" if not combined.strip() else (
        f"doubletake-{version.rsplit('.', 1)[0]}" if version != "unknown" else "doubletake-unknown"
    )

    documented_controls = all(
        _has_line(ctl_help, command) for command in ("status", "discover", "devices", "connect")
    )
    target_controls = all(
        _has_target_command(ctl_help, command)
        for command in ("disconnect", "mute", "unmute")
    )
    multi_target = documented_controls and target_controls and bool(
        re.search(r"(?i)multi[- ]target|multiple|concurrent|all active", ctl_help)
    )
    if not multi_target:
        # A line documenting both a target argument and the all-target behaviour
        # is sufficient; a bare command name is deliberately not.
        multi_target = documented_controls and target_controls and "target" in ctl_help.lower()

    return BackendFeatures(
        profile=profile,
        version=version,
        foreground_control=(
            bool(re.search(r"(?i)foreground", sender_help))
            and bool(re.search(r"(?i)owner[- ]token", combined))
            and bool(re.search(r"(?i)handshake", combined))
        ),
        owner_handshake=(
            bool(re.search(r"(?i)owner[- ]token", combined))
            and bool(re.search(r"(?i)instance[- ]uuid", combined))
            and bool(re.search(r"(?i)handshake", combined))
        ),
        multi_target=multi_target,
        target_bound_pairing=(
            bool(re.search(r"(?i)target[- ]bound", combined))
            and bool(re.search(r"(?i)credential", combined))
            and bool(re.search(r"(?i)pair", combined))
        ),
        physical_source=(
            bool(re.search(r"(?i)physical (?:monitor|source)", combined))
            and bool(re.search(r"(?i)connector", combined))
            and bool(re.search(r"(?i)verify", combined))
        ),
        virtual_source=(
            bool(re.search(r"(?i)virtual source", combined))
            and bool(re.search(r"(?i)connector", combined))
            and bool(re.search(r"(?i)verify", combined))
        ),
        audio_probe=(
            _has_line(ctl_help, "status")
            and bool(re.search(r"(?i)status|active streams", ctl_help))
        ),
        audio_restart=bool(re.search(r"(?i)(?:start|restart)[_ -]audio", combined)),
        target_mute=target_controls,
        keyring=bool(re.search(r"(?i)(?:keyring|secret service).*(?:transaction|prepare|commit)", combined)),
        provisional_commit=bool(
            re.search(r"(?i)provisional", combined)
            and re.search(r"(?i)commit", combined)
            and re.search(r"(?i)pair", combined)
        ),
        cancel_pairing=bool(re.search(r"(?i)cancel[_ -]pairing", combined)),
        revoke_pairing=bool(re.search(r"(?i)revoke[_ -]pairing", combined)),
        query_modes=bool(re.search(r"(?i)(?:query[_ -]modes|supported modes)", combined)),
    )


async def _probe_run(
    process: ports.ProcessPort,
    argv: tuple[str, ...],
    *,
    timeout: float,
) -> Result[ports.CommandOutput]:
    try:
        return await process.run(ports.CommandSpec(argv=argv, env={}), timeout=timeout, stdin=None)
    except Exception:
        return Err(error=_diagnostic(ErrorCode.dependency_incompatible))


def _probe_exit_ok(output: ports.CommandOutput, allowed: frozenset[int]) -> bool:
    return type(output.exit_code) is int and output.exit_code in allowed



def _output_text(output: ports.CommandOutput) -> str:
    chunks: list[str] = []
    for value in (output.stdout, output.stderr):
        if type(value) is bytes:
            try:
                chunks.append(value.decode("utf-8", errors="replace"))
            except Exception:
                pass
    return "\n".join(chunks)


async def probe_backend(process: ports.ProcessPort) -> Result[BackendFeatures]:
    """Run only bounded help/version probes and decode their evidence."""
    sender_result = await _probe_run(process, ("doubletake", "--help"), timeout=3.0)
    if isinstance(sender_result, Err):
        if sender_result.error.code is ErrorCode.dependency_missing:
            return Err(error=_diagnostic(ErrorCode.dependency_missing, command="doubletake"))
        return Err(error=_diagnostic(ErrorCode.dependency_incompatible))
    if not isinstance(sender_result.value, ports.CommandOutput):
        return _incompatible()
    if not _probe_exit_ok(sender_result.value, frozenset({0, 2})):
        return _incompatible()

    ctl_result = await _probe_run(process, ("doubletake-ctl", "--help"), timeout=3.0)
    if isinstance(ctl_result, Err):
        if ctl_result.error.code is ErrorCode.dependency_missing:
            return Err(error=_diagnostic(ErrorCode.dependency_missing, command="doubletake-ctl"))
        return Err(error=_diagnostic(ErrorCode.dependency_incompatible))
    if not isinstance(ctl_result.value, ports.CommandOutput):
        return _incompatible()
    if not _probe_exit_ok(ctl_result.value, frozenset({0, 2})):
        return _incompatible()

    version_result = await _probe_run(process, ("doubletake", "--version"), timeout=3.0)
    version_text = ""
    if isinstance(version_result, Ok) and isinstance(version_result.value, ports.CommandOutput):
        if not _probe_exit_ok(version_result.value, frozenset({0, 2})):
            return _incompatible()
        version_text = _output_text(version_result.value)
    elif isinstance(version_result, Err) and version_result.error.code is ErrorCode.dependency_missing:
        return Err(error=_diagnostic(ErrorCode.dependency_missing, command="doubletake"))

    sender_help = _output_text(sender_result.value)
    ctl_help = _output_text(ctl_result.value)
    if not sender_help.strip() or not ctl_help.strip():
        return _incompatible()
    return Ok(value=features_from_help(f"{version_text}\n{sender_help}", ctl_help))


__all__ = [
    "BackendDevice",
    "BackendFeatures",
    "BackendReply",
    "BackendStream",
    "DevicesReply",
    "StatusReply",
    "decode_devices",
    "decode_status",
    "features_from_help",
    "probe_backend",
]
