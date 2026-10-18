"""Typed control adapter for the published doubletake CLI contract."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, TypeAlias, runtime_checkable
from uuid import UUID

from airplay_linux.backend.contract import (
    AckReply,
    BackendDevice,
    BackendFeatures,
    BackendReply,
    BackendStream,
    DevicesReply,
    StatusReply,
    decode_devices,
    decode_status,
)
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverTarget
from airplay_linux.domain.operations import (
    InstanceId,
    OperationContext,
    ProcessIdentity,
    SensitiveText,
)
from airplay_linux.domain.ports import CommandOutput, CommandSpec, Clock, Subscription
from airplay_linux.domain.resources import (
    CaptureSource,
    Diagnostic,
    Err,
    ErrorCode,
    Ok,
    Result,
)


class BackendOperation(StrEnum):
    status = "status"
    discover = "discover"
    devices = "devices"
    connect = "connect"
    disconnect = "disconnect"
    mute = "mute"
    unmute = "unmute"
    query_modes = "query_modes"
    start_audio = "start_audio"
    restart_audio = "restart_audio"
    begin_pairing = "begin_pairing"
    submit_pairing = "submit_pairing"
    cancel_pairing = "cancel_pairing"
    prepare_pairing = "prepare_pairing"
    commit_pairing = "commit_pairing"
    mark_invalid = "mark_invalid"
    revoke_pairing = "revoke_pairing"
    shutdown = "shutdown"

    STATUS = status
    DISCOVER = discover
    DEVICES = devices
    CONNECT = connect
    DISCONNECT = disconnect
    MUTE = mute
    UNMUTE = unmute
    QUERY_MODES = query_modes
    START_AUDIO = start_audio
    RESTART_AUDIO = restart_audio
    BEGIN_PAIRING = begin_pairing
    SUBMIT_PAIRING = submit_pairing
    CANCEL_PAIRING = cancel_pairing
    PREPARE_PAIRING = prepare_pairing
    COMMIT_PAIRING = commit_pairing
    MARK_INVALID = mark_invalid
    REVOKE_PAIRING = revoke_pairing
    SHUTDOWN = shutdown


@dataclass(frozen=True, kw_only=True)
class EmptyPayload:
    kind: str = "empty"


@dataclass(frozen=True, kw_only=True, repr=False)
class ConnectPayload:
    """Connect intent; credentials are never represented by ``repr``."""

    source: CaptureSource | None = None
    credential: SensitiveText | str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        source = "<present>" if self.source is not None else "None"
        credential = "<redacted>" if self.credential is not None else "None"
        return f"ConnectPayload(source={source}, credential={credential})"


@dataclass(frozen=True, kw_only=True, repr=False)
class CredentialPayload:
    value: SensitiveText | str

    def __repr__(self) -> str:
        return "CredentialPayload(value=<redacted>)"


@dataclass(frozen=True, kw_only=True)
class MutePayload:
    muted: bool


@dataclass(frozen=True, kw_only=True, repr=False)
class SourcePayload:
    source: CaptureSource

    def __repr__(self) -> str:
        return "SourcePayload(source=<redacted>)"


@dataclass(frozen=True, kw_only=True, repr=False)
class PairingPayload:
    credential: SensitiveText | str

    def __repr__(self) -> str:
        return "PairingPayload(credential=<redacted>)"


@dataclass(frozen=True, kw_only=True)
class ModesPayload:
    pass


BackendPayload: TypeAlias = (
    EmptyPayload
    | ConnectPayload
    | CredentialPayload
    | MutePayload
    | SourcePayload
    | PairingPayload
    | ModesPayload
)


class BackendNoticeKind(StrEnum):
    status = "status"
    stream = "stream"
    pairing = "pairing"
    failed = "failed"


@dataclass(frozen=True, kw_only=True)
class BackendNotice:
    """Safe subscription event with a closed set of typed payload fields."""

    kind: BackendNoticeKind
    status: StatusReply | None = None
    target: str | None = None
    credential_kind: str | None = None
    diagnostic: Diagnostic | None = None


@dataclass(frozen=True, kw_only=True, repr=False)
class Handshake:
    """Runtime ownership proof; the owner token is never printable."""

    instance_uuid: InstanceId
    owner_token: SensitiveText | str
    daemon: ProcessIdentity
    protocol_version: str

    def __repr__(self) -> str:
        return (
            "Handshake("
            f"instance_uuid={self.instance_uuid!r}, owner_token=<redacted>, "
            f"daemon={self.daemon!r}, protocol_version={self.protocol_version!r})"
        )


@dataclass(frozen=True, kw_only=True)
class _Subscription:
    _close_action: Callable[[], None] = field(repr=False, compare=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def close(self) -> None:
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        self._close_action()


@runtime_checkable
class BackendControl(Protocol):
    async def request(
        self,
        operation: BackendOperation,
        target: ReceiverTarget | None,
        payload: BackendPayload,
        context: OperationContext | None,
    ) -> Result[BackendReply]: ...

    def subscribe(self, callback: Callable[[BackendNotice], None]) -> Subscription: ...

    async def handshake(
        self, owner_token: SensitiveText | str, instance_uuid: InstanceId,
    ) -> Result[Handshake]: ...

    async def close(self) -> None: ...


def _diag(code: ErrorCode, *, command: str | None = None) -> Diagnostic:
    return diagnostic(code, command=command)


def _err(code: ErrorCode, *, command: str | None = None) -> Err:
    return Err(error=_diag(code, command=command))


def _default_features() -> BackendFeatures:
    # These are the capabilities usable through the documented v0.5 CLI.  The
    # application-owned lifecycle and trust/source gates remain deliberately off.
    return BackendFeatures(
        profile="doubletake-0.5",
        version="0.5.0",
        foreground_control=False,
        owner_handshake=False,
        multi_target=True,
        target_bound_pairing=False,
        physical_source=False,
        virtual_source=False,
        audio_probe=True,
        audio_restart=False,
        target_mute=True,
        keyring=False,
        provisional_commit=False,
        cancel_pairing=False,
        revoke_pairing=False,
        query_modes=False,
    )


@dataclass(frozen=True, kw_only=True, repr=False)
class PublishedDoubletakeProfile:
    """The externally documented v0.5 command mapping.

    ``socket_path`` has no default on purpose: every command is bound to an
    explicitly selected private socket rather than the daemon's ambient default.
    """

    socket_path: str
    name: str = "doubletake-0.5"
    version: str = "0.5.0"
    provenance: str = "published-release"
    live_support_proven: bool = False
    features: BackendFeatures = field(default_factory=_default_features)
    owner_token: SensitiveText | str | None = field(default=None, repr=False)
    instance_uuid: InstanceId | None = None
    protocol_version: str | None = None

    def __repr__(self) -> str:
        return (
            "PublishedDoubletakeProfile("
            f"socket_path={self.socket_path!r}, name={self.name!r}, "
            f"version={self.version!r}, provenance={self.provenance!r}, "
            f"live_support_proven={self.live_support_proven!r}, "
            f"features={self.features!r})"
        )

    def _operation(self, operation: BackendOperation | str) -> BackendOperation | None:
        try:
            return operation if isinstance(operation, BackendOperation) else BackendOperation(operation)
        except (TypeError, ValueError):
            return None

    def try_argv(
        self,
        operation: BackendOperation | str,
        target: ReceiverTarget | None,
        payload: BackendPayload,
    ) -> Result[tuple[str, ...]]:
        op = self._operation(operation)
        if op is None:
            return _err(ErrorCode.unsupported_operation)
        if type(self.socket_path) is not str or not self.socket_path or not self.socket_path.startswith("/") or "\x00" in self.socket_path:
            return _err(ErrorCode.stale_socket)
        if not isinstance(payload, (EmptyPayload, ConnectPayload, CredentialPayload, MutePayload, SourcePayload, PairingPayload, ModesPayload)):
            return _err(ErrorCode.invalid_input)

        target_text: str | None = None
        if target is not None:
            if not isinstance(target, ReceiverTarget) or type(target.canonical_host) is not str:
                return _err(ErrorCode.invalid_input)
            if not target.canonical_host or any(char.isspace() for char in target.canonical_host):
                return _err(ErrorCode.invalid_input)
            target_text = target.canonical_host

        if op in {BackendOperation.status, BackendOperation.discover, BackendOperation.devices}:
            if target is not None:
                return _err(ErrorCode.invalid_input)
            command = op.value
            return Ok(value=("doubletake-ctl", "-socket", self.socket_path, command))

        if op in {BackendOperation.disconnect, BackendOperation.mute, BackendOperation.unmute}:
            if target_text is None:
                return _err(ErrorCode.unsupported_operation)
            if op in {BackendOperation.mute, BackendOperation.unmute} and not isinstance(payload, (EmptyPayload, MutePayload)):
                return _err(ErrorCode.invalid_input)
            if op is BackendOperation.disconnect and not isinstance(payload, EmptyPayload):
                return _err(ErrorCode.invalid_input)
            return Ok(value=("doubletake-ctl", "-socket", self.socket_path, op.value, target_text))

        if op is BackendOperation.connect:
            if target_text is None:
                return _err(ErrorCode.unsupported_operation)
            if not isinstance(payload, (EmptyPayload, ConnectPayload)):
                return _err(ErrorCode.invalid_input)
            if isinstance(payload, ConnectPayload):
                # The published CLI places PIN/password arguments in argv.  The
                # adapter must never put caller secrets into a process command.
                if payload.credential is not None:
                    return _err(ErrorCode.unsupported_operation)
                if payload.source is not None:
                    return _err(ErrorCode.unsupported_source)
            return Ok(value=("doubletake-ctl", "-socket", self.socket_path, "connect", target_text))

        # No other operation has a published v0.5 command mapping.
        return _err(ErrorCode.unsupported_operation)

    def argv(
        self,
        operation: BackendOperation | str,
        target: ReceiverTarget | None,
        payload: BackendPayload,
    ) -> tuple[str, ...]:
        result = self.try_argv(operation, target, payload)
        if isinstance(result, Err):
            raise ValueError(result.error.code.value)
        return result.value


class UnsupportedControl:
    """Fail-closed implementation used when no compatible backend is proven."""

    def __init__(self, *, code: ErrorCode = ErrorCode.unsupported_operation) -> None:
        self._code = code
        self._closed = False

    async def request(
        self,
        operation: BackendOperation,
        target: ReceiverTarget | None,
        payload: BackendPayload,
        context: OperationContext | None,
    ) -> Result[BackendReply]:
        return _err(self._code)

    def subscribe(self, callback: Callable[[BackendNotice], None]) -> Subscription:
        return _Subscription(_close_action=lambda: None)

    async def handshake(
        self, owner_token: SensitiveText | str, instance_uuid: InstanceId,
    ) -> Result[Handshake]:
        return _err(ErrorCode.dependency_incompatible)

    async def close(self) -> None:
        self._closed = True


class PublishedDoubletakeControl:
    """Process-port adapter for the read-only and target-scoped CLI surface."""

    def __init__(
        self,
        process,
        profile: PublishedDoubletakeProfile,
        *,
        timeout: float = 5.0,
        clock: Clock | None = None,
    ) -> None:
        self._process = process
        self._profile = profile
        self._timeout = timeout
        self._clock = clock
        self._closed = False
        self._callbacks: list[Callable[[BackendNotice], None]] = []

    def subscribe(self, callback: Callable[[BackendNotice], None]) -> Subscription:
        self._callbacks.append(callback)

        def remove() -> None:
            if callback in self._callbacks:
                self._callbacks.remove(callback)

        return _Subscription(_close_action=remove)

    def _emit(self, notice: BackendNotice) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback(notice)
            except Exception:
                # A subscriber cannot be allowed to turn a backend result into
                # an untyped or secret-bearing failure.
                continue

    def _available(self, operation: BackendOperation) -> bool:
        features = self._profile.features
        if operation in {BackendOperation.status, BackendOperation.discover, BackendOperation.devices}:
            return True
        if operation is BackendOperation.disconnect:
            return features.multi_target
        if operation in {BackendOperation.mute, BackendOperation.unmute}:
            return features.multi_target and features.target_mute
        if operation is BackendOperation.connect:
            return all(
                (
                    features.foreground_control,
                    features.owner_handshake,
                    features.multi_target,
                    features.target_bound_pairing,
                    features.physical_source or features.virtual_source,
                    features.provisional_commit,
                    features.cancel_pairing,
                    features.revoke_pairing,
                )
            )
        if operation is BackendOperation.query_modes:
            return features.query_modes
        if operation in {BackendOperation.start_audio, BackendOperation.restart_audio}:
            return features.audio_restart
        if operation is BackendOperation.begin_pairing:
            return features.target_bound_pairing and features.cancel_pairing
        if operation is BackendOperation.submit_pairing:
            return features.target_bound_pairing and features.keyring and features.provisional_commit
        if operation is BackendOperation.prepare_pairing:
            return features.target_bound_pairing and features.keyring and features.provisional_commit
        if operation is BackendOperation.commit_pairing:
            return features.keyring and features.provisional_commit
        if operation is BackendOperation.mark_invalid:
            return features.revoke_pairing
        if operation is BackendOperation.cancel_pairing:
            return features.cancel_pairing
        if operation is BackendOperation.revoke_pairing:
            return features.revoke_pairing
        return False

    def _normalize_operation(self, operation: BackendOperation | str) -> BackendOperation | None:
        try:
            return operation if isinstance(operation, BackendOperation) else BackendOperation(operation)
        except (TypeError, ValueError):
            return None

    async def _run(self, argv: tuple[str, ...]) -> Result[CommandOutput]:
        try:
            value = await asyncio.wait_for(
                self._process.run(
                    CommandSpec(argv=argv, env={}),
                    timeout=self._timeout,
                    stdin=None,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            return _err(ErrorCode.daemon_failed)
        except FileNotFoundError:
            return _err(ErrorCode.dependency_missing, command=argv[0])
        except OSError:
            return _err(ErrorCode.dependency_missing, command=argv[0])
        except Exception:
            return _err(ErrorCode.dependency_incompatible)
        if isinstance(value, Err):
            code = getattr(value.error, "code", ErrorCode.dependency_incompatible)
            if code is ErrorCode.dependency_missing:
                return _err(ErrorCode.dependency_missing, command=argv[0])
            if code in {ErrorCode.cleanup_timeout, ErrorCode.daemon_failed, ErrorCode.transport_failed}:
                return _err(ErrorCode.daemon_failed)
            return _err(ErrorCode.dependency_incompatible)
        if not isinstance(value, Ok) or not isinstance(value.value, CommandOutput):
            return _err(ErrorCode.dependency_incompatible)
        if type(value.value.exit_code) is not int or value.value.exit_code != 0:
            return _err(ErrorCode.daemon_failed)
        return value

    def _ownership_check(self, status: StatusReply) -> Result[None]:
        expected_owner = self._profile.owner_token
        expected_instance = self._profile.instance_uuid
        if expected_owner is not None:
            actual_owner = status.owner_token
            if actual_owner is None:
                return _err(ErrorCode.ownership_mismatch)
            expected_text = _secret_text(expected_owner)
            if expected_text is None or actual_owner != expected_text:
                return _err(ErrorCode.ownership_mismatch)
        if expected_instance is not None:
            if status.instance_uuid is None or status.instance_uuid != expected_instance:
                return _err(ErrorCode.ownership_mismatch)
        if status.socket_path is not None and status.socket_path != self._profile.socket_path:
            return _err(ErrorCode.stale_socket)
        if self._profile.protocol_version is not None:
            if status.protocol_version != self._profile.protocol_version:
                return _err(ErrorCode.dependency_incompatible)
        return Ok(value=None)

    @staticmethod
    def _response_object(output: CommandOutput) -> object:
        try:
            return output.stdout.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            return None

    async def request(
        self,
        operation: BackendOperation | str,
        target: ReceiverTarget | None,
        payload: BackendPayload,
        context: OperationContext | None,
    ) -> Result[BackendReply]:
        if self._closed:
            return _err(ErrorCode.daemon_failed)
        op = self._normalize_operation(operation)
        if op is None:
            return _err(ErrorCode.unsupported_operation)
        argv_result = self._profile.try_argv(op, target, payload)
        if isinstance(argv_result, Err):
            return argv_result
        if not self._available(op):
            return _err(ErrorCode.dependency_incompatible)

        process_result = await self._run(argv_result.value)
        if isinstance(process_result, Err):
            return process_result
        raw = self._response_object(process_result.value)
        if raw is None:
            return _err(ErrorCode.dependency_incompatible)

        if op is BackendOperation.status:
            decoded = decode_status(raw)
            if isinstance(decoded, Err):
                return decoded
            ownership = self._ownership_check(decoded)
            if isinstance(ownership, Err):
                return ownership
            self._emit(BackendNotice(kind=BackendNoticeKind.status, status=decoded))
            return Ok(value=decoded)
        if op in {BackendOperation.discover, BackendOperation.devices}:
            decoded = decode_devices(raw)
            if isinstance(decoded, Err):
                return decoded
            return Ok(value=decoded)

        parsed = _parse_ack(raw, op, target)
        if isinstance(parsed, Err):
            return parsed
        self._emit(BackendNotice(kind=BackendNoticeKind.stream, target=parsed.target))
        return Ok(value=parsed)

    async def handshake(
        self, owner_token: SensitiveText | str, instance_uuid: InstanceId,
    ) -> Result[Handshake]:
        if self._closed:
            return _err(ErrorCode.daemon_failed)
        if not self._profile.features.owner_handshake:
            return _err(ErrorCode.dependency_incompatible)
        if _secret_text(owner_token) is None or not isinstance(instance_uuid, UUID):
            return _err(ErrorCode.invalid_input)
        result = await self.request(BackendOperation.status, None, EmptyPayload(), None)
        if isinstance(result, Err):
            return result
        status = result.value
        if status.owner_token != _secret_text(owner_token):
            return _err(ErrorCode.ownership_mismatch)
        if status.instance_uuid != instance_uuid:
            return _err(ErrorCode.ownership_mismatch)
        if status.daemon is None or not status.protocol_version:
            return _err(ErrorCode.dependency_incompatible)
        return Ok(
            value=Handshake(
                instance_uuid=instance_uuid,
                owner_token=owner_token,
                daemon=status.daemon,
                protocol_version=status.protocol_version,
            )
        )

    async def close(self) -> None:
        self._closed = True
        self._callbacks.clear()


def _secret_text(value: SensitiveText | str | None) -> str | None:
    if isinstance(value, SensitiveText):
        try:
            return value.reveal_for_backend()
        except Exception:
            return None
    if type(value) is str:
        return value
    return None


def _parse_ack(
    raw: str,
    operation: BackendOperation,
    target: ReceiverTarget | None,
) -> AckReply | Err:
    try:
        import json

        value = json.loads(raw)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return _err(ErrorCode.dependency_incompatible)
    if type(value) is not dict or type(value.get("ok")) is not bool:
        return _err(ErrorCode.dependency_incompatible)
    if value["ok"] is not True:
        return _err(ErrorCode.receiver_rejected)
    state = value.get("state")
    if state is not None and (type(state) is not str or not state):
        return _err(ErrorCode.dependency_incompatible)
    muted = value.get("audio_muted")
    if muted is not None and type(muted) is not bool:
        return _err(ErrorCode.dependency_incompatible)
    return AckReply(
        operation=operation.value,
        state=state,
        target=target.canonical_host if target is not None else None,
        muted=muted,
    )


__all__ = [
    "AckReply",
    "BackendControl",
    "BackendDevice",
    "BackendFeatures",
    "BackendNotice",
    "BackendNoticeKind",
    "BackendOperation",
    "BackendPayload",
    "BackendReply",
    "BackendStream",
    "ConnectPayload",
    "CredentialPayload",
    "DevicesReply",
    "EmptyPayload",
    "Handshake",
    "ModesPayload",
    "MutePayload",
    "PairingPayload",
    "PublishedDoubletakeControl",
    "PublishedDoubletakeProfile",
    "SourcePayload",
    "StatusReply",
    "UnsupportedControl",
]
