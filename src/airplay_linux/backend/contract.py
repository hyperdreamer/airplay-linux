"""Published doubletake capability negotiation and typed response decoding.

The installed doubletake command exposes a small, documented control surface.  This
module records only evidence that can be derived from that surface.  In particular,
flags that merely mention daemon mode, keyring storage, or screen capture do not
turn on application-owned lifecycle, trust, or source capabilities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, TypeAlias
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

# Exact published control surface of doubletake 0.5.0, transcribed from the
# pinned source at e18f85d07f74a3b9d49f448762d28f3f7feb3938:
#   cmd/doubletake/main.go      every flag.String/Bool/Int declaration
#   cmd/doubletake-ctl/main.go  the usage() command table
#
# That release prints no version banner and has no --version flag: the sender's
# --help is Go's flag package usage block, and the ctl's --help is a hand-written
# usage string.  Capability proof therefore cannot rest on a version line the
# release never emits; it rests on this exact, auditable fingerprint instead.  A
# help block that matches it exactly is the pinned release's own output, and any
# other block -- later release, foreign build, or explanatory prose -- proves
# nothing and leaves every gate closed.
_PINNED_050_SENDER_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "bitrate", "code", "cred-backend", "creds", "daemonize", "debug",
        "direct-key", "fps", "hwaccel", "no-audio", "no-cursor", "no-encrypt",
        "pair", "port", "port-range", "socket", "target", "target-latency-ms",
        "test", "video-codec", "x11-window-id", "x11-window-name",
    }
)
# Argument signature -> number of receivers each documented command addresses.
_PINNED_050_CTL_COMMANDS: Final[dict[str, str]] = {
    "status": "",
    "discover": "",
    "devices": "",
    "connect": "[target] [PIN-or-password]",
    "pin": "<PIN-or-password>",
    "disconnect": "[target]",
    "mute": "[target]",
    "unmute": "[target]",
}
_PINNED_050_PROFILE: Final[str] = "doubletake-0.5"
_PINNED_050_VERSION: Final[str] = "0.5.0"
# The published ctl usage documents that a target-scoped command addresses only
# that receiver; without this statement a [target] argument alone would not prove
# that other concurrent streams stay untouched.
_PINNED_050_TARGET_SCOPE_STATEMENT: Final[str] = "only the given ip"

#: The vendored patch (docs/backend-fork.md) adds ``-capabilities`` and reports the
#: captured display in ``status``.  That surface is recognised only together with a
#: matching machine-readable advertisement, never from the extra flag alone.
_PINNED_050_SOURCE_SENDER_FLAGS: Final[frozenset[str]] = _PINNED_050_SENDER_FLAGS | frozenset({"capabilities"})
_PINNED_050_SOURCE_PROFILE: Final[str] = "doubletake-0.5+source"
_CAPABILITY_SCHEMA_TOKEN: Final[str] = "capabilities-v1"
_STATUS_SOURCE_TOKEN: Final[str] = "status-source"
_STATUS_SOURCE_COMPLETE_TOKEN: Final[str] = "status-source-complete"


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

    #: The build advertises (and this application has verified) that ``status``
    #: reports which display each capture selected.  It says nothing about whether
    #: the source is physical or owned: those remain separate, unproven gates.
    source_reporting: bool = False

    #: Ownership comes only from production composition, never public tokens.
    owned_daemon_control: bool = False
    #: Exact complete-source recognition is installed by the source lane.
    source_reporting_complete: bool = False

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
class CredentialProofVerdict:
    """Transient, sanitized outcome of the three independent credential legs."""

    service_leg: Literal["passed", "failed", "not_run"]
    backend_positive_leg: Literal["passed", "failed", "not_run"]
    backend_negative_leg: Literal["passed", "failed", "not_run"]
    failure: ErrorCode | None

    def __post_init__(self) -> None:
        legs = (self.service_leg, self.backend_positive_leg, self.backend_negative_leg)
        if any(type(leg) is not str or leg not in {"passed", "failed", "not_run"} for leg in legs):
            raise ValueError("invalid credential proof leg")
        if self.failure is not None and type(self.failure) is not ErrorCode:
            raise TypeError("invalid credential proof failure")
        if "failed" in legs and self.failure is None:
            raise ValueError("credential proof failed without a failure")
        if all(leg == "passed" for leg in legs) and self.failure is not None:
            raise ValueError("credential proof passed with a failure")

    @property
    def proven(self) -> bool:
        return self.failure is None and all(leg == "passed" for leg in (
            self.service_leg, self.backend_positive_leg, self.backend_negative_leg,
        ))


@dataclass(frozen=True, kw_only=True)
class BackendSource:
    """The backend's typed portal source geometry, without raw JSON."""

    type: int
    width: int
    height: int
    x: int
    y: int

    def __post_init__(self) -> None:
        for name in ("type", "width", "height", "x", "y"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"source {name} must be an integer")


@dataclass(frozen=True, kw_only=True)
class BackendStream:
    """The safe subset of one published status stream."""

    device: str
    target: str
    state: str
    has_audio: bool
    audio_muted: bool
    source: BackendSource | None = None
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
        if self.source is not None and not isinstance(self.source, BackendSource):
            raise TypeError("source must be a BackendSource or None")

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


def _decode_source(value: object) -> BackendSource | Err:
    """Project the complete source object and reject ambiguous evidence."""
    data = _exact_dict(value)
    fields = {"Type", "Width", "Height", "X", "Y"}
    if data is None or set(data) != fields:
        return _incompatible()
    if any(type(data[name]) is not int for name in fields):
        return _incompatible()
    if not 1 <= data["Type"] <= 2**32 - 1 or data["Width"] <= 0 or data["Height"] <= 0:
        return _incompatible()
    try:
        return BackendSource(
            type=data["Type"], width=data["Width"], height=data["Height"],
            x=data["X"], y=data["Y"],
        )
    except (TypeError, ValueError):
        return _incompatible()


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
    source: BackendSource | None = None
    if "source" in data:
        decoded_source = _decode_source(data["source"])
        if isinstance(decoded_source, Err):
            return None
        source = decoded_source
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
            source=source,
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
    # The published daemon emits credentials only for waiting streams.  A
    # credential attached to a streaming/idle entry is not a harmless extra;
    # accepting it would let a stale prompt masquerade as live state.
    if any(stream.credential_kind is not None and stream.state != "pin_required" for stream in streams):
        return _incompatible()
    if credential is not None and state != "pin_required":
        return _incompatible()
    stream_credentials = {stream.credential_kind for stream in streams if stream.credential_kind}
    if len(stream_credentials) > 1 and state != "pin_required":
        return _incompatible()
    if credential is None and needs and legacy_pin:
        credential = "pin"
    if credential is None and needs and len(stream_credentials) == 1:
        credential = next(iter(stream_credentials))
    if credential is None and state == "pin_required":
        pending_credentials = [
            stream.credential_kind
            for stream in streams
            if stream.state == "pin_required" and stream.credential_kind is not None
        ]
        if pending_credentials:
            credential = pending_credentials[0]
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

    # Reproduce statusResponseLocked's legacy projection rather than trusting
    # aggregate fields independently of the stream list: a waiting stream wins
    # for device/target, while audio always comes from the first streaming
    # stream.  This also makes impossible PIN/password combinations fail closed.
    waiting = [stream for stream in streams if stream.state == "pin_required"]
    streaming = [stream for stream in streams if stream.state == "streaming"]
    connecting = [stream for stream in streams if stream.state == "connecting"]
    if waiting:
        projected_device, projected_target = waiting[0].device, waiting[0].target
        projected_credential = waiting[0].credential_kind
        if state != "pin_required":
            return _incompatible()
    elif streaming:
        projected_device, projected_target = streaming[0].device, streaming[0].target
        projected_credential = None
        if state != "streaming":
            return _incompatible()
    else:
        projected_device = projected_target = projected_credential = None
        if connecting and state != "connecting":
            return _incompatible()
        if not connecting and state not in {"idle", "discovering"}:
            return _incompatible()
    streaming_audio = streaming[0] if streaming else None
    if top_has_audio != (streaming_audio.has_audio if streaming_audio else False):
        return _incompatible()
    if top_audio_muted != (streaming_audio.audio_muted if streaming_audio else False):
        return _incompatible()
    if device != projected_device or target != projected_target:
        return _incompatible()
    if credential != projected_credential:
        return _incompatible()
    if needs != bool(waiting) or legacy_pin != (projected_credential == "pin"):
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
    # DeviceInfo is tagged omitempty in the published Response type.  A valid
    # empty discovery therefore has no devices member; an explicitly present
    # non-list value is still malformed and must not be treated as empty.
    raw_devices = data.get("devices", [])
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


def _version_banner(text: str) -> str:
    """Return a version declared by a standalone ``--version`` run, or ``""``.

    Only the first line participates: Go's flag package prints
    ``flag provided but not defined: -version`` before the usage block when a
    build rejects the flag, and that banner must yield no version at all.
    """
    first = text.splitlines()[0] if text.splitlines() else ""
    version = _version(first)
    return "" if version == "unknown" else version


_GO_USAGE_LINE: Final[re.Pattern[str]] = re.compile(r"^Usage of \S+:$|^Usage of \S+:")
_GO_FLAG: Final[re.Pattern[str]] = re.compile(r"^  -([A-Za-z0-9][A-Za-z0-9-]*)(?: [A-Za-z0-9_.]+)?$")
# "  connect [target] [PIN-or-password]  Start mirroring (to target IP, ...)"
_CTL_ROW: Final[re.Pattern[str]] = re.compile(
    r"^  (?P<command>[a-z]+)(?P<arguments> [^ ].*?)?  +(?P<summary>\S.*)$"
)


@dataclass(frozen=True, kw_only=True)
class _CtlCommand:
    arguments: str
    summary: str


def _sender_surface(sender_help: str) -> frozenset[str] | None:
    """Return the exact flag set of a Go ``flag`` usage block, or None.

    Go's package prints ``Usage of <argv0>:`` followed by ``  -name type`` rows and
    tab-indented descriptions.  Only a block that parses cleanly under those
    rules is evidence; anything else is prose and proves nothing.
    """
    lines = sender_help.splitlines()
    if not lines or _GO_USAGE_LINE.match(lines[0]) is None:
        return None
    flags: set[str] = set()
    expecting_description = False
    for line in lines[1:]:
        if not line.strip():
            continue
        flag = _GO_FLAG.match(line)
        if flag is not None:
            name = flag.group(1)
            if name in flags:
                return None
            flags.add(name)
            expecting_description = True
            continue
        if expecting_description and line.startswith("    \t"):
            expecting_description = False
            continue
        # A duplicated flag, a stray description, or non-Go indentation means
        # this is not a verbatim capture of a Go flag usage block.
        return None
    if expecting_description:
        return None
    return frozenset(flags)


def _ctl_surface(ctl_help: str) -> dict[str, _CtlCommand] | None:
    """Return each command's argument signature and summary from the usage table.

    The ctl's ``usage()`` prints a fixed ``Commands:`` table with two spaces
    before each command, a padded argument signature, and a summary.  Only rows
    inside that table count: a command named in prose, a section heading such as
    ``Unsupported commands:``, or a flag row is not a declaration.  Keeping the
    summary attached to its row prevents one command's scope claim from proving
    another command's semantics.
    """
    lines = ctl_help.splitlines()
    if not lines or not lines[0].startswith("Usage: doubletake-ctl "):
        return None
    commands: dict[str, _CtlCommand] = {}
    in_commands = False
    for line in lines[1:]:
        if line.strip() == "Commands:":
            in_commands = True
            continue
        if line.strip() in {"Flags:", "Other:"}:
            in_commands = False
            continue
        if not in_commands or not line.strip():
            continue
        row = _CTL_ROW.match(line)
        if row is None:
            # Any unparseable row inside the table makes the whole table
            # unverified rather than partially trusted.
            return None
        command = row.group("command")
        if command in commands:
            return None
        commands[command] = _CtlCommand(
            arguments=(row.group("arguments") or "").strip(),
            summary=row.group("summary").strip(),
        )
    if not commands:
        return None
    return commands


def _target_scope_proven(command: _CtlCommand | None) -> bool:
    """Accept only an affirmative, non-contradictory scope claim on this row."""
    if command is None:
        return False
    summary = " ".join(command.summary.lower().split())
    if _PINNED_050_TARGET_SCOPE_STATEMENT not in summary:
        return False
    # Any negation is explicit contrary evidence, even if another phrase in the
    # same row happens to mention the pinned wording.  Failing closed is safer
    # than trying to infer which clause a future wording change intended.
    return re.search(r"\b(?:not|never|ignor\w*|unsupported|unavailable)\b", summary) is None


def features_from_help(
    sender_help: str, ctl_help: str, *, advertised: frozenset[str] = frozenset(),
) -> BackendFeatures:
    """Decode the pinned release's published help contract conservatively.

    Evidence is an exact match against the control surface transcribed from the
    pinned source, not keyword presence.  Help text cannot prove ownership,
    source typing, pairing transactions, or any other application-private
    obligation, so those capabilities are never inferred at all -- neither from a
    keyword nor from a negation, and never from a version banner the release does
    not print.
    """
    if type(sender_help) is not str or type(ctl_help) is not str:
        sender_help = ""
        ctl_help = ""
    combined = f"{sender_help}\n{ctl_help}"
    version = _version(combined)
    profile = "unknown" if not combined.strip() else (
        f"doubletake-{version.rsplit('.', 1)[0]}" if version != "unknown" else "doubletake-unknown"
    )

    sender_flags = _sender_surface(sender_help)
    ctl_commands = _ctl_surface(ctl_help)
    ctl_matches = (
        ctl_commands is not None
        and {
            name: command.arguments for name, command in ctl_commands.items()
        } == _PINNED_050_CTL_COMMANDS
    )
    patched_surface = (
        sender_flags == _PINNED_050_SOURCE_SENDER_FLAGS and ctl_matches
    )
    verified = (
        sender_flags is not None
        and ctl_commands is not None
        and (
            sender_flags == _PINNED_050_SENDER_FLAGS or sender_flags == _PINNED_050_SOURCE_SENDER_FLAGS
        )
        and ctl_matches
    )
    source_complete = patched_surface and {
        _CAPABILITY_SCHEMA_TOKEN,
        _STATUS_SOURCE_TOKEN,
        _STATUS_SOURCE_COMPLETE_TOKEN,
    } <= set(advertised)
    if patched_surface and not {
        _CAPABILITY_SCHEMA_TOKEN,
        _STATUS_SOURCE_TOKEN,
    } <= set(advertised):
        # An extra flag is not an advertisement.  Without the machine-readable
        # tokens this surface proves nothing, so fall through to the refusal below.
        verified = False
    if not verified:
        # Unknown, partial, later, foreign, or merely-similar help text proves no
        # public capability and no private one.  Report the observed version text
        # if the block carried one, but do not derive profile support from it.
        return BackendFeatures(
            profile="doubletake-unknown",
            version=version,
            foreground_control=False,
            owner_handshake=False,
            multi_target=False,
            target_bound_pairing=False,
            physical_source=False,
            virtual_source=False,
            audio_probe=False,
            audio_restart=False,
            target_mute=False,
            keyring=False,
            provisional_commit=False,
            cancel_pairing=False,
            revoke_pairing=False,
            query_modes=False,
        )

    # Exactly the pinned surface.  Enable only what each operation's own usage
    # row declares; a scope phrase in ``disconnect`` cannot prove mute semantics.
    target_scoped = all(
        ctl_commands.get(command) is not None
        and ctl_commands[command].arguments == "[target]"
        and _target_scope_proven(ctl_commands[command])
        for command in ("disconnect", "mute", "unmute")
    )
    return BackendFeatures(
        profile=_PINNED_050_SOURCE_PROFILE if patched_surface else _PINNED_050_PROFILE,
        version=_PINNED_050_VERSION,
        foreground_control=False,
        owner_handshake=False,
        multi_target=target_scoped,
        target_bound_pairing=False,
        physical_source=False,
        virtual_source=False,
        audio_probe=True,
        audio_restart=False,
        target_mute=target_scoped,
        keyring=False,
        provisional_commit=False,
        cancel_pairing=False,
        revoke_pairing=False,
        query_modes=False,
        source_reporting=patched_surface,
        source_reporting_complete=source_complete,
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
                text = value.decode("utf-8", errors="replace")
            except Exception:
                continue
            if text.strip():
                chunks.append(text)
    return "\n".join(chunks)


def _surface_text(output: ports.CommandOutput) -> str:
    """Return the single stream that carries the usage block, verbatim.

    Go's flag package writes ``--help`` to stderr and leaves stdout empty, so a
    naive concatenation would put an empty line in front of ``Usage of ...`` and
    the fingerprint would never match.  When a stream holds the usage block it is
    returned alone, unpadded; otherwise the combined evidence is returned so an
    unrecognized surface still fails closed.
    """
    texts: list[str] = []
    for value in (output.stdout, output.stderr):
        if type(value) is not bytes:
            continue
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            continue
        if text.strip():
            texts.append(text)
    for text in texts:
        if text.lstrip().startswith("Usage"):
            return text
    return "\n".join(texts)


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

    capabilities_result = await _probe_run(process, ("doubletake", "-capabilities"), timeout=3.0)
    advertised: frozenset[str] = frozenset()
    if isinstance(capabilities_result, Ok) and isinstance(capabilities_result.value, ports.CommandOutput):
        if _probe_exit_ok(capabilities_result.value, frozenset({0})):
            advertised = frozenset(
                line.strip()
                for line in _output_text(capabilities_result.value).splitlines()
                if line.strip()
            )

    version_result = await _probe_run(process, ("doubletake", "--version"), timeout=3.0)
    version_text = ""
    if isinstance(version_result, Ok) and isinstance(version_result.value, ports.CommandOutput):
        if not _probe_exit_ok(version_result.value, frozenset({0, 2})):
            return _incompatible()
        version_text = _version_banner(_output_text(version_result.value))
    elif isinstance(version_result, Err) and version_result.error.code is ErrorCode.dependency_missing:
        return Err(error=_diagnostic(ErrorCode.dependency_missing, command="doubletake"))

    sender_help = _surface_text(sender_result.value)
    ctl_help = _surface_text(ctl_result.value)
    if not sender_help.strip() or not ctl_help.strip():
        return _incompatible()
    # The fingerprint is the sender's ``--help`` block alone.  ``--version`` is
    # optional evidence: the pinned release rejects it with exit 2, so splicing
    # its error banner in front of the usage block would mask the very surface
    # the fingerprint has to match.
    features = features_from_help(sender_help, ctl_help, advertised=advertised)
    if version_text:
        if features.profile == _PINNED_050_PROFILE and version_text != _PINNED_050_VERSION:
            # A recognized help surface cannot override affirmative evidence from
            # a successful version probe.  No compatibility matrix exists for a
            # different version, so reject the contradictory binary instead of
            # granting the pinned release's capabilities.
            return _incompatible()
        if features.version == "unknown":
            features = replace(features, version=version_text)
    return Ok(value=features)


__all__ = [
    "BackendDevice",
    "BackendFeatures",
    "BackendReply",
    "BackendSource",
    "BackendStream",
    "CredentialProofVerdict",
    "DevicesReply",
    "StatusReply",
    "decode_devices",
    "decode_status",
    "features_from_help",
    "probe_backend",
]
