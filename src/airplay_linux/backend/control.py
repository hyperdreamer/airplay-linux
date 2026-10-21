"""Typed control adapter for the published doubletake CLI contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, TypeAlias, runtime_checkable
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

# A caller-owned predicate the adapter re-evaluates at the dispatch boundary:
# after every adapter-owned await and immediately before the mutating child is
# spawned.  It returns the typed failure to report when the exact target or the
# cleanup ownership is no longer valid.
DispatchFence: TypeAlias = Callable[[], Result[None]]

# Operations that address a receiver target and change its state.  These are the
# commands whose dispatch must be fenced: a target can be replaced at the same
# address while an adapter-owned await is in flight, so the check has to happen
# at the spawn boundary rather than at entry.  Pairing/keyring operations are
# fenced by the trust coordinator's own generation fences and are not listed.
FENCE_REQUIRED_OPERATIONS: Final[frozenset[BackendOperation]] = frozenset(
    {
        BackendOperation.connect,
        BackendOperation.disconnect,
        BackendOperation.mute,
        BackendOperation.unmute,
        BackendOperation.start_audio,
        BackendOperation.restart_audio,
    }
)


def requires_dispatch_fence(operation: BackendOperation) -> bool:
    """True when this operation must be fenced at the dispatch boundary."""

    return operation in FENCE_REQUIRED_OPERATIONS


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


def _application_contract_features() -> BackendFeatures:
    """Capabilities of the deterministic, non-live adapter contract fixture."""
    return BackendFeatures(
        profile="application-contract-v1",
        version="v1",
        foreground_control=True,
        owner_handshake=True,
        multi_target=True,
        target_bound_pairing=True,
        physical_source=True,
        virtual_source=True,
        audio_probe=True,
        audio_restart=True,
        target_mute=True,
        keyring=True,
        provisional_commit=True,
        cancel_pairing=True,
        revoke_pairing=True,
        query_modes=True,
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


@dataclass(frozen=True, kw_only=True, repr=False)
class ApplicationContractProfile(PublishedDoubletakeProfile):
    """Explicit v1 application fixture profile; never an installed backend claim.

    Its argv is a deterministic test transport name, not a doubletake command.
    Keeping this profile separate from :class:`PublishedDoubletakeProfile` makes
    it impossible for help probing to silently advertise the application schema.
    """

    name: str = "application-contract-v1"
    version: str = "v1"
    provenance: str = "application-contract-fixture"
    live_support_proven: bool = False
    features: BackendFeatures = field(default_factory=_application_contract_features)
    protocol_version: str | None = "v1"

    def __post_init__(self) -> None:
        if self.provenance != "application-contract-fixture":
            raise ValueError("application contract provenance is fixed")
        if self.live_support_proven:
            raise ValueError("application contract cannot claim live support")
        if self.protocol_version != "v1":
            raise ValueError("application contract protocol must be v1")
        if self.features.profile != "application-contract-v1":
            raise ValueError("application contract feature profile mismatch")

    def try_argv(
        self,
        operation: BackendOperation | str,
        target: ReceiverTarget | None,
        payload: BackendPayload,
    ) -> Result[tuple[str, ...]]:
        op = self._operation(operation)
        if op is None:
            return _err(ErrorCode.unsupported_operation)
        if (
            type(self.socket_path) is not str
            or not self.socket_path
            or not self.socket_path.startswith("/")
            or "\x00" in self.socket_path
        ):
            return _err(ErrorCode.stale_socket)
        if not isinstance(
            payload,
            (EmptyPayload, ConnectPayload, CredentialPayload, MutePayload,
             SourcePayload, PairingPayload, ModesPayload),
        ):
            return _err(ErrorCode.invalid_input)
        target_text: str | None = None
        if target is not None:
            if not isinstance(target, ReceiverTarget) or type(target.canonical_host) is not str:
                return _err(ErrorCode.invalid_input)
            if not target.canonical_host or any(char.isspace() for char in target.canonical_host):
                return _err(ErrorCode.invalid_input)
            target_text = target.canonical_host
        reads = {BackendOperation.status, BackendOperation.discover, BackendOperation.devices}
        target_operations = set(BackendOperation) - reads - {BackendOperation.shutdown}
        if op in reads or op is BackendOperation.shutdown:
            if target is not None:
                return _err(ErrorCode.invalid_input)
        elif op in target_operations and target_text is None:
            return _err(ErrorCode.unsupported_operation)
        # Payload values, including credentials, are consumed by the fake
        # socket/process harness and are intentionally never placed in argv.
        argv = ("application-contract-v1", "-socket", self.socket_path, op.value)
        return Ok(value=argv + ((target_text,) if target_text is not None else ()))


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
        self._handshake: Handshake | None = None

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
        # A socket path is routing information, not an ownership proof.  Every
        # operation that can mutate daemon state requires the independently
        # authenticated handshake recorded by ``handshake``.
        if not features.owner_handshake:
            return False
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
        if operation is BackendOperation.shutdown:
            return features.owner_handshake
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
        # doubletake-ctl serializes a valid {"ok": false, ...} response and
        # exits 1 for a receiver-level rejection.  Preserve the output for the
        # typed decoder; launch/transport failures have already returned above.
        if type(value.value.exit_code) is not int:
            return _err(ErrorCode.dependency_incompatible)
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
            raw = output.stdout.decode("utf-8")
            return json.loads(raw)
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _is_mutation(operation: BackendOperation) -> bool:
        return operation not in {
            BackendOperation.status,
            BackendOperation.discover,
            BackendOperation.devices,
        }

    async def _revalidate_ownership(self) -> Result[None]:
        """Recheck the exact authenticated daemon before each mutation."""
        handshake = self._handshake
        if handshake is None:
            return _err(ErrorCode.dependency_incompatible)
        argv_result = self._profile.try_argv(
            BackendOperation.status, None, EmptyPayload(),
        )
        if isinstance(argv_result, Err):
            return argv_result
        process_result = await self._run(argv_result.value)
        if isinstance(process_result, Err):
            return process_result
        output = process_result.value
        response = self._response_object(output)
        if not isinstance(response, dict):
            return _err(
                ErrorCode.daemon_failed
                if output.exit_code != 0
                else ErrorCode.dependency_incompatible
            )
        if response.get("ok") is not True:
            return _err(ErrorCode.ownership_mismatch)
        if output.exit_code != 0:
            return _err(ErrorCode.daemon_failed)
        status = decode_status(response)
        if isinstance(status, Err):
            return status
        ownership = self._ownership_check(status)
        if isinstance(ownership, Err):
            self._handshake = None
            return ownership
        expected_owner = _secret_text(handshake.owner_token)
        if (
            status.socket_path != self._profile.socket_path
            or status.instance_uuid != handshake.instance_uuid
            or status.owner_token != expected_owner
            or status.daemon != handshake.daemon
            or status.protocol_version != handshake.protocol_version
        ):
            self._handshake = None
            if status.socket_path != self._profile.socket_path:
                return _err(ErrorCode.stale_socket)
            return _err(ErrorCode.ownership_mismatch)
        return Ok(value=None)

    async def request(
        self,
        operation: BackendOperation | str,
        target: ReceiverTarget | None,
        payload: BackendPayload,
        context: OperationContext | None,
        *,
        dispatch_fence: DispatchFence | None = None,
    ) -> Result[BackendReply]:
        """Dispatch one typed command against an already admitted target.

        ``dispatch_fence`` is the caller's exact-target (or cleanup-ownership)
        freshness predicate.  Mutating commands re-evaluate it after this
        adapter's own awaited revalidation and before the mutating child is
        spawned; a failing predicate returns its typed failure with no spawn.
        Read-only commands never consult it.
        """
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

        if self._is_mutation(op):
            ownership = await self._revalidate_ownership()
            if isinstance(ownership, Err):
                return ownership
            # Closing the dispatch window: no await may separate this
            # re-evaluation from the spawn below, so a replacement admitted
            # during the ownership query can never receive this command.
            #
            # A target-addressed state change is never dispatched unfenced.
            # Enforcing this here, in the adapter that owns the spawn, is what
            # makes the guarantee hold even when a wrapper accepts
            # ``dispatch_fence`` and drops it: the wrapper reaches this method
            # with ``dispatch_fence=None`` and the command does not spawn.
            if requires_dispatch_fence(op):
                if dispatch_fence is None:
                    return _err(ErrorCode.dependency_incompatible)
                fenced = dispatch_fence()
                if isinstance(fenced, Err):
                    return fenced
            elif dispatch_fence is not None:
                # Other mutations (pairing/keyring) may still pass a fence; honour
                # it consistently rather than silently ignoring the argument.
                fenced = dispatch_fence()
                if isinstance(fenced, Err):
                    return fenced

        process_result = await self._run(argv_result.value)
        if isinstance(process_result, Err):
            return process_result
        output = process_result.value
        response = self._response_object(output)
        if not isinstance(response, dict) or type(response.get("ok")) is not bool:
            return _err(
                ErrorCode.daemon_failed
                if output.exit_code != 0
                else ErrorCode.dependency_incompatible
            )
        if response["ok"] is False:
            # The published CLI uses exit 1 for this valid rejection envelope.
            # Keep rejection fixed and operation-specific; never copy its free
            # form ``error`` field into a diagnostic.
            if output.exit_code not in {0, 1}:
                return _err(ErrorCode.daemon_failed)
            return _err(_rejection_code(op))
        if output.exit_code != 0:
            return _err(ErrorCode.daemon_failed)

        if op is BackendOperation.status:
            decoded = decode_status(response)
            if isinstance(decoded, Err):
                return decoded
            ownership = self._ownership_check(decoded)
            if isinstance(ownership, Err):
                return ownership
            self._emit(BackendNotice(kind=BackendNoticeKind.status, status=decoded))
            return Ok(value=decoded)
        if op in {BackendOperation.discover, BackendOperation.devices}:
            decoded = decode_devices(response)
            if isinstance(decoded, Err):
                return decoded
            return Ok(value=decoded)

        if op in {BackendOperation.mute, BackendOperation.unmute}:
            parsed = _parse_mute_ack(response, op, target)
        else:
            parsed = _parse_ack(response, op, target)
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
        expected_owner = _secret_text(owner_token)
        if expected_owner is None:
            return _err(ErrorCode.invalid_input)
        configured_owner = _secret_text(self._profile.owner_token)
        if configured_owner is not None and configured_owner != expected_owner:
            return _err(ErrorCode.ownership_mismatch)
        if self._profile.instance_uuid is not None and self._profile.instance_uuid != instance_uuid:
            return _err(ErrorCode.ownership_mismatch)
        result = await self.request(BackendOperation.status, None, EmptyPayload(), None)
        if isinstance(result, Err):
            return result
        status = result.value
        if (
            status.owner_token != expected_owner
            or status.instance_uuid != instance_uuid
            or status.socket_path != self._profile.socket_path
            or status.daemon is None
            or not status.protocol_version
        ):
            return _err(
                ErrorCode.stale_socket
                if status.socket_path != self._profile.socket_path
                else ErrorCode.ownership_mismatch
            )
        if (
            self._profile.protocol_version is not None
            and status.protocol_version != self._profile.protocol_version
        ):
            return _err(ErrorCode.dependency_incompatible)
        handshake = Handshake(
            instance_uuid=instance_uuid,
            owner_token=owner_token,
            daemon=status.daemon,
            protocol_version=status.protocol_version,
        )
        self._handshake = handshake
        return Ok(value=handshake)

    async def close(self) -> None:
        self._closed = True
        self._handshake = None
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


def _rejection_code(operation: BackendOperation) -> ErrorCode:
    if operation in {BackendOperation.mute, BackendOperation.unmute}:
        return ErrorCode.audio_mute_failed
    if operation in {BackendOperation.start_audio, BackendOperation.restart_audio}:
        return ErrorCode.audio_unavailable
    return ErrorCode.receiver_rejected


def _parse_ack(
    value: object,
    operation: BackendOperation,
    target: ReceiverTarget | None,
) -> AckReply | Err:
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


def _parse_mute_ack(
    value: object,
    operation: BackendOperation,
    target: ReceiverTarget | None,
) -> AckReply | Err:
    """Confirm mute from exactly one matching published stream entry."""
    if target is None:
        return _err(ErrorCode.audio_mute_failed)
    status = decode_status(value)
    if isinstance(status, Err):
        return _err(ErrorCode.audio_mute_failed)
    matches = [stream for stream in status.streams if stream.target == target.canonical_host]
    if len(matches) != 1:
        return _err(ErrorCode.audio_mute_failed)
    expected = operation is BackendOperation.mute
    if matches[0].audio_muted is not expected:
        return _err(ErrorCode.audio_mute_failed)
    return AckReply(
        operation=operation.value,
        state=status.state,
        target=target.canonical_host,
        muted=matches[0].audio_muted,
    )


__all__ = [
    "AckReply",
    "ApplicationContractProfile",
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
    "DispatchFence",
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
