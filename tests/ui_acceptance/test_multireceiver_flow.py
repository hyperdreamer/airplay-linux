"""Two-receiver acceptance: every result belongs to the subject it was issued for.

The recurring defect this module exists to catch is a result applied to the wrong
subject: a pairing commit after the endpoint moved, a stale connection or
reconnect error painting the receiver the user has since selected, a prompt
tracked on the wrong window, or a second manager trying to own a resource the
first one acquired.

Every test drives the assembled application (production presenter, dialog factory
and window over the real session manager) with two receivers, so the assertions
name the receiver and the exact retained resource each time.  A bare "Ok" would
never satisfy them.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

import pytest
from PySide6.QtWidgets import QApplication, QLineEdit

from airplay_linux import app as app_module
from airplay_linux.backend.contract import AckReply, BackendStream, CredentialSubmissionReply, StatusReply
from airplay_linux.backend.control import ApplicationContractProfile, BackendOperation, Handshake
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.messages import (
    ConfirmSwitch,
    CredentialRequestPayload,
    RetryCleanup,
    SubmitPairing,
    Stop,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.operations import (
    InstanceId,
    ProcessIdentity,
    SensitiveText,
    StreamHandle,
)
from airplay_linux.domain.records import (
    DaemonRecord,
    LaunchReceipt,
    OriginalDaemonIdentity,
)
from airplay_linux.domain.relay import CredentialRequest, NoReply, RelayStartOptions, Submitted
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Mode,
    Ok,
    TransportState,
    TrustState,
    VirtualOutputSource,
)
from airplay_linux.runtime import RuntimeConfig
from airplay_linux.session import Route
from airplay_linux.session.recovery import CredentialRecoveryCoordinator
from airplay_linux.session.testing import SessionRig
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage.repositories import JsonCredentialRecoveryMarkerStore
from airplay_linux.testing.adapters import Deferred
from airplay_linux.testing.builders import target
from airplay_linux.transport.doubletake import CONTROL_TIMEOUT, DoubleTakeTransport
from airplay_linux.ui.state import Action

from test_matrix import (
    RECEIVER_A,
    RECEIVER_B,
    AcceptanceTransport,
    build,  # noqa: F401 - re-exported fixture used by these tests
    field_of,
    remembered,
    saved_receiver,
)


def a() -> ReceiverId:
    return ReceiverId(RECEIVER_A)


def b() -> ReceiverId:
    return ReceiverId(RECEIVER_B)


def shiboken_alive(widget: Any) -> bool:
    import shiboken6

    return shiboken6.isValid(widget)


# ---------------------------------------------------------------- assembled credential relay


class _AssemblyRegistry:
    """Durable daemon identity seam for the real recovery coordinator."""

    def __init__(self, runtime_directory: Path, instance_uuid: InstanceId) -> None:
        runtime_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        app = ProcessIdentity(pid=100, start_ticks=1000)
        supervisor = ProcessIdentity(pid=200, start_ticks=2000)
        daemon = ProcessIdentity(pid=300, start_ticks=3000)
        socket_path = str(runtime_directory / "daemon.sock")
        self._record = DaemonRecord(
            schema_version=1,
            instance_uuid=instance_uuid,
            owner_token="fake-owner-token",
            app=app,
            supervisor=supervisor,
            daemon=daemon,
            protocol_version="1",
            socket_path=socket_path,
            lifecycle="running",
            runtime_directory=str(runtime_directory),
        )
        self._ready = True
        self._receipt = LaunchReceipt(
            schema_version=1,
            launch_uuid=UUID(int=77),
            instance_uuid=instance_uuid,
            app=app,
            owner_uid=1000,
            boot_id="fake-boot-id",
            runtime_directory=str(runtime_directory),
            runtime_device=1,
            runtime_inode=2,
            endpoint_generation=1,
            socket_name="daemon.sock",
            protocol_version="1",
            state="ready",
            daemon_instance_uuid=instance_uuid,
            supervisor=supervisor,
            daemon=daemon,
            entry_device=1,
            entry_inode=3,
            socket_inode=3,
            peer_pid=daemon.pid,
            peer_start_ticks=daemon.start_ticks,
            peer_uid=1000,
            status_state="idle",
            credential_service="keyring_proven",
            failure=None,
            updated_at=0.0,
            recovery_attempt_id=None,
        )

    async def _receipt_for(self, record):
        if record != self._record:
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        return Ok(value=self._receipt)

    async def ensure(self):
        return Ok(value=self._record)

    def subscribe(self, callback):
        return _AssemblySubscription()

    async def shutdown(self):
        return Ok(value=None)

    async def authenticate_exact_identity(self, identity):
        expected = OriginalDaemonIdentity(
            launch_uuid=self._receipt.launch_uuid,
            instance_uuid=self._record.instance_uuid,
            endpoint_generation=self._receipt.endpoint_generation,
            daemon_process=self._record.daemon,
            runtime_directory=self._record.runtime_directory,
            socket_path=self._record.socket_path,
        )
        if identity != expected:
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        return Ok(value=self._record)


class _AssemblySubscription:
    def close(self) -> None:
        pass


class _RealCredentialControl:
    """Backend seam for a real DoubleTakeTransport in assembled Stop tests."""

    def __init__(self, registry: _AssemblyRegistry, events: list[str]) -> None:
        self.registry = registry
        self.events = events
        self.coordinator: CredentialRecoveryCoordinator | None = None
        self.target: ReceiverTarget | None = None
        self.status_state = "pin_required"
        self.submission_state = "streaming"
        self.disconnect_status_state = "absent"
        self.outcome: Any = Ok(value=Submitted(
            reply_at=0.0, accepted=True, post_dispatch_error=None,
        ))
        self.status_replies: list[StatusReply | Deferred[Any]] = []
        self.status_read_states: list[str] = []
        self.status_return_states: list[str] = []
        self.status_read_cancelled = 0
        self.status_entered = asyncio.Event()
        self.submit_entered = asyncio.Event()
        self.disconnect_targets: list[str] = []
        self.submission_targets: list[ReceiverTarget] = []
        self.canary_matched = False
        self.hold_credential_status = False
        self.credential_status_entered = asyncio.Event()
        self.release_credential_status = asyncio.Event()

    def status_reply(self, state: str | None = None) -> StatusReply:
        if self.target is None:
            raise AssertionError("the backend has not received the Start target")
        row_state = self.status_state if state is None else state
        rows = [BackendStream(
            device="TV B", target="192.0.2.20", state="pin_required",
            has_audio=False, audio_muted=False, credential_kind="pin",
        )]
        if row_state != "absent":
            rows.append(BackendStream(
                device="TV A", target=self.target.canonical_host, state=row_state,
                has_audio=False, audio_muted=False,
                credential_kind="pin" if row_state == "pin_required" else None,
            ))
        return StatusReply(
            state="pin_required", device="TV B", target="192.0.2.20",
            has_audio=False, audio_muted=False, needs_credential=True,
            credential_kind="pin", streams=tuple(rows),
        )

    def queue_status_reply(self, reply: StatusReply) -> None:
        self.status_replies.append(reply)

    def queue_status_gate(self) -> Deferred[Any]:
        gate: Deferred[Any] = Deferred()
        self.status_replies.append(gate)
        return gate

    async def handshake(self, owner_token, instance_uuid):
        return Ok(value=Handshake(
            instance_uuid=instance_uuid,
            owner_token=owner_token,
            daemon=self.registry._record.daemon,
            protocol_version=self.registry._record.protocol_version,
        ))

    async def request(
        self, operation, target, payload, context, *, dispatch_fence=None,
    ):
        if operation is BackendOperation.status:
            self.status_entered.set()
            self.status_read_states.append(self.status_state)
            scripted = self.status_replies.pop(0) if self.status_replies else None
            if isinstance(scripted, Deferred):
                try:
                    await scripted.wait()
                except asyncio.CancelledError:
                    self.status_read_cancelled += 1
                    raise
            if isinstance(scripted, StatusReply):
                reply = scripted
            else:
                reply = self.status_reply()
            matching = tuple(
                row for row in reply.streams
                if self.target is not None and row.target == self.target.canonical_host
            )
            self.status_return_states.append(matching[0].state if len(matching) == 1 else "absent")
            return Ok(value=reply)

        if target is None:
            return Err(error=diagnostic(ErrorCode.stale_target))
        if dispatch_fence is not None:
            checked = dispatch_fence()
            if isinstance(checked, Err):
                return checked
        if operation is BackendOperation.connect:
            self.target = target
            return Ok(value=AckReply(
                operation="connect", target=target.canonical_host, state="pin_required",
            ))
        if operation is BackendOperation.disconnect:
            self.disconnect_targets.append(target.canonical_host)
            self.status_state = self.disconnect_status_state
            return Ok(value=AckReply(
                operation="disconnect", target=target.canonical_host, state="pin_required",
            ))
        return Err(error=diagnostic(ErrorCode.unsupported_operation))

    async def submit_credential(self, target, payload, context, *, dispatch_fence):
        checked = dispatch_fence()
        if isinstance(checked, Err):
            return checked
        if self.coordinator is None or not self.coordinator.markers:
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        marker = self.coordinator.markers[0]
        if marker.state != "watching":
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        value = payload.value
        self.canary_matched = value.reveal_for_backend() == "route-b-real-transport-canary-3617"
        self.submission_targets.append(target)
        self.events.append("credential_dispatch")
        self.submit_entered.set()
        outcome = self.outcome
        if isinstance(outcome, Deferred):
            outcome = await outcome.wait()
        if not isinstance(outcome, Ok) or not isinstance(outcome.value, Submitted):
            raise AssertionError("Stop acceptance expected a complete credential reply")
        self.status_state = self.submission_state
        return CredentialSubmissionReply(
            reply_at=float(outcome.value.reply_at), accepted=outcome.value.accepted,
        )

    async def close(self) -> None:
        pass


class _DoubleTakeTransportBinding:
    """Bind the production transport to one assembled acceptance rig."""

    def __init__(self, registry: _AssemblyRegistry, control: _RealCredentialControl) -> None:
        self.registry = registry
        self.control = control
        self.real: DoubleTakeTransport | None = None

    def bind(self, rig: SessionRig, coordinator: CredentialRecoveryCoordinator) -> None:
        features = replace(
            ApplicationContractProfile(socket_path=self.registry._record.socket_path).features,
            credential_relay=True,
            target_volume=True,
        )
        self.control.coordinator = coordinator
        self.real = DoubleTakeTransport(
            lambda _record: self.control,
            self.registry,
            rig.capture,
            rig.pairing,
            rig.receivers,
            rig.fences,
            features,
            rig.clock,
            rig.scheduler,
            rig.ids,
            recovery_guard=coordinator.start_dispatch_fence,
        )

    async def read_credential_status(self, target, context):
        if self.control.hold_credential_status:
            self.control.credential_status_entered.set()
            await self.control.release_credential_status.wait()
        return await self.real.read_credential_status(target, context)

    def __getattr__(self, name: str):
        real = self.real
        if real is None:
            raise AttributeError(name)
        return getattr(real, name)


class _AssemblyCredentialTransport(AcceptanceTransport):
    """Target-row fake which publishes a pending request before returning its handle."""

    CANARY = "route-b-assembled-canary-7391"

    def __init__(self, *, outcome=None, observed_state="streaming") -> None:
        super().__init__(ready=False)
        self.coordinator = None
        self.events: list[str] = []
        self.outcome = outcome or Ok(value=Submitted(reply_at=0.0, accepted=True, post_dispatch_error=None))
        self.observed_state = observed_state
        self.request_emitted_before_return = False
        self.request_status: StatusReply | None = None
        self.submission_target: ReceiverTarget | None = None
        self.status_targets: list[ReceiverTarget] = []
        self.canary_matched = False
        self.hold_status = False
        self.status_entered = asyncio.Event()
        self.release_status = asyncio.Event()
        self.submit_entered = asyncio.Event()
        self.request_event: TransportEvent | None = None
        self.request_events: list[TransportEvent] = []
        self.hold_start = False
        self.start_waiting = asyncio.Event()
        self.release_start = asyncio.Event()
        self.start_finished = asyncio.Event()
        self.pending_handle: StreamHandle | None = None
        self.pending_target: ReceiverTarget | None = None
        self.pending_owner = None
        self.pending_start_stopped = False
        self.transferred_handles: list[StreamHandle] = []
        self.submission_targets: list[ReceiverTarget] = []
        self.status_read_states: list[str] = []
        self.status_read_cancelled = 0
        self.cleanup_statuses: list[StatusReply] = []
        self.cleanup_status_sequence: list[StatusReply] = []
        self.cleanup_ack_states: list[str] = []

    async def start_stream(self, destination, source, audio, operation, *, initial_volume=None):
        result = await super().start_stream(
            destination, source, audio, operation, initial_volume=initial_volume,
        )
        if not isinstance(result, Ok):
            return result
        handle = result.value
        self.pending_handle = handle
        self.pending_target = destination
        decoy = BackendStream(
            device="TV B", target="192.0.2.20", state="pin_required",
            has_audio=False, audio_muted=False, credential_kind="pin",
        )
        matching = BackendStream(
            device="TV A", target=destination.canonical_host, state="pin_required",
            has_audio=False, audio_muted=False, credential_kind="pin",
        )
        self.request_status = StatusReply(
            state="pin_required", device="TV B", target="192.0.2.20",
            has_audio=False, audio_muted=False, needs_credential=True,
            credential_kind="pin", streams=(decoy, matching),
        )
        rows = tuple(
            row for row in self.request_status.streams
            if row.target == destination.canonical_host and row.state == "pin_required"
            and row.credential_kind in {"pin", "password"}
        )
        if not isinstance(source, VirtualOutputSource) and len(rows) == 1:
            request = CredentialRequest(
                receiver_id=destination.receiver_id,
                target=destination,
                credential_kind=rows[0].credential_kind,
                start_operation_id=operation.operation_id,
                endpoint_generation=destination.endpoint_generation,
                session_generation=operation.session_generation,
                owner_lease=handle.lease_id,
                daemon_instance_uuid=handle.daemon_instance_uuid,
                generation=1,
                observed_at=0.0,
                start_options=RelayStartOptions(
                    target_volume_proven=False, audio=audio, initial_volume=initial_volume,
                ),
            )
            event = TransportEvent(
                kind=TransportEventKind.credential_request,
                receiver_id=destination.receiver_id,
                stream=handle,
                endpoint_fingerprint=destination.endpoint_fingerprint,
                endpoint_generation=destination.endpoint_generation,
                daemon_instance_uuid=handle.daemon_instance_uuid,
                context=operation,
                payload=CredentialRequestPayload(request=request, lifecycle="live", reason=None),
            )
            self.request_event = event
            self.request_events.append(event)
            self.emit(event)
            self.request_emitted_before_return = True
        try:
            if self.hold_start:
                self.start_waiting.set()
                await self.release_start.wait()
        finally:
            self.start_finished.set()
        if self.pending_start_stopped:
            return Err(error=diagnostic(ErrorCode.stale_event))
        return result

    async def stop_pending_start(self, owner, stop_context):
        self._record("stop_pending_start", owner=owner, stop_context=stop_context)
        self.pending_owner = owner
        self.pending_start_stopped = True
        self.release_start.set()
        await self.start_finished.wait()
        self.transferred_handles.append(self.pending_handle)
        return Ok(value=self.pending_handle)

    async def stop_stream(self, handle, context):
        if self.pending_target is None or not self.cleanup_status_sequence:
            return await super().stop_stream(handle, context)
        self.cleanup_ack_states.append("pin_required")
        for status in self.cleanup_status_sequence:
            self.cleanup_statuses.append(status)
            matches = tuple(
                row for row in status.streams
                if row.target == self.pending_target.canonical_host
            )
            if len(matches) > 1:
                return Err(error=diagnostic(ErrorCode.transport_failed))
            if not matches:
                return await super().stop_stream(handle, context)
        return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    def end_credential_request(self) -> None:
        event = self.request_event
        if event is None:
            raise AssertionError("the exact Start did not publish a credential request")
        self.emit(replace(
            event,
            payload=CredentialRequestPayload(
                request=event.payload.request, lifecycle="ended", reason="row_ended",
            ),
        ))

    async def submit_credential(self, target, value, context, *, dispatch_fence):
        self._record(
            "submit_credential", target=target, value=value, context=context,
            dispatch_fence=dispatch_fence is not None,
        )
        self.submission_target = target
        self.submission_targets.append(target)
        self.canary_matched = value.reveal_for_backend() == self.CANARY
        if not self.coordinator.markers or self.coordinator.markers[0].state != "watching":
            return Err(error=diagnostic(ErrorCode.persistence_failed))
        checked = dispatch_fence()
        if isinstance(checked, Err):
            return checked
        self.events.append("credential_dispatch")
        if isinstance(self.outcome, Deferred):
            self.submit_entered.set()
            return await self.outcome.wait()
        return self.outcome

    async def read_credential_status(self, target, context):
        self._record("read_credential_status", target=target, context=context)
        self.status_targets.append(target)
        self.status_read_states.append(self.observed_state)
        if self.hold_status:
            self.status_entered.set()
            try:
                await self.release_status.wait()
            except asyncio.CancelledError:
                self.status_read_cancelled += 1
                raise
        row = BackendStream(
            device="TV A" if self.observed_state != "absent" else "TV B",
            target=target.canonical_host if self.observed_state != "absent" else "192.0.2.20",
            state=self.observed_state if self.observed_state != "absent" else "pin_required",
            has_audio=False, audio_muted=False,
            credential_kind="pin" if self.observed_state != "streaming" else None,
        )
        other = BackendStream(
            device="TV B", target="192.0.2.20", state="pin_required",
            has_audio=False, audio_muted=False, credential_kind="pin",
        )
        result = StatusReply(
            state="pin_required", device="TV B", target="192.0.2.20",
            has_audio=False, audio_muted=False, needs_credential=True,
            credential_kind="pin", streams=(other, row),
        )
        return Ok(value=result)

    async def record_credential_marker_generation(
        self, target, context, *, submission_id, expected_generation, previous_generation,
    ):
        self._record(
            "record_credential_marker_generation", target=target, context=context,
            submission_id=submission_id, expected_generation=expected_generation,
            previous_generation=previous_generation,
        )
        return Ok(value=None)


def _install_assembly_recovery_rig(monkeypatch, tmp_path, transport) -> None:
    import test_matrix

    parent = test_matrix.SessionRig

    class RecoverySessionRig(parent):
        def __init__(self):
            super().__init__()
            events = transport.events
            registry = _AssemblyRegistry(
                tmp_path / "owned-runtime", transport.daemon_instance_uuid,
            )
            store = JsonCredentialRecoveryMarkerStore(AtomicFiles(tmp_path / "recovery-state"))
            create = store.create

            async def create_and_observe(marker):
                result = await create(marker)
                if isinstance(result, Ok):
                    events.append("marker_published")
                return result

            store.create = create_and_observe
            coordinator = CredentialRecoveryCoordinator(store, registry, fences=self.fences)
            coordinator.events = events
            startup_completed = coordinator._startup_completed

            def record_startup_completion():
                events.append("startup_complete")
                startup_completed()

            coordinator._startup_completed = record_startup_completion
            transport.coordinator = coordinator
            self.deps = replace(
                self.deps, transport=transport, credential_recovery=coordinator,
            )

    monkeypatch.setattr(test_matrix, "SessionRig", RecoverySessionRig)


async def _build_credential_app(
    build, monkeypatch, tmp_path, *, outcome=None, observed_state="streaming", saved=(),
):
    root = tmp_path / "assembled-recovery"
    root.mkdir(mode=0o700)
    transport = _AssemblyCredentialTransport(
        outcome=outcome, observed_state=observed_state,
    )
    _install_assembly_recovery_rig(monkeypatch, root, transport)
    app = await build(transport=transport, saved=saved)
    coordinator = app.manager.deps.credential_recovery
    return app, coordinator, transport


async def test_assembled_runtime_relays_a_matching_target_request_through_the_real_dialog(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(build, monkeypatch, tmp_path)
    app.discover("a")
    app.discover("b", address="192.0.2.20")
    await app.settle()
    app.select("a")
    await app.settle()

    app.click(Action.start)
    await app.settle()

    state = app.state("a")
    prompt_state = state.credential_prompt
    assert transport.request_emitted_before_return is True
    assert isinstance(state.stream, StreamHandle)
    assert prompt_state is not None
    assert state.mode is Mode.MIRROR
    assert len(transport.request_status.streams) == 2
    assert transport.request_status.state == "pin_required"
    assert transport.request_status.streams[0].target == "192.0.2.20"
    assert transport.request_status.streams[0].state == "pin_required"
    assert transport.request_status.streams[1].target == state.target.canonical_host
    assert transport.request_status.streams[1].state == "pin_required"
    assert not coordinator._startup_pending
    assert app.manager.deps.credential_recovery is coordinator
    assert [type(command).__name__ for command in app.dispatched] == ["Start"]

    dialog = await app.prompt("pairingDialog")
    canary = transport.CANARY
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert(canary)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await app.settle()

    assert transport.canary_matched
    assert transport.submission_target == state.target
    assert transport.status_targets == [state.target]
    assert transport.events.index("marker_published") < transport.events.index("credential_dispatch")
    assert coordinator.markers[0].state == "retired"
    stored = await coordinator._store.for_target(state.target.canonical_host)
    assert isinstance(stored, Ok) and stored.value.state == "retired"
    assert app.state("a").credential_prompt.report == "connected"
    app.select("a")
    await app.settle()
    assert app.notice == f"Connected to {app.title}."
    assert app.error == ""
    assert field.text() == ""
    assert canary not in repr(app.state("a"))
    assert canary not in repr(coordinator.markers)
    assert canary not in repr(app.dispatched)
    assert canary not in repr(app.journal)
    assert canary not in app.activity_text


async def test_assembled_matching_row_end_closes_prompt_without_a_result(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(build, monkeypatch, tmp_path)
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()

    assert app.state("a").credential_prompt is not None
    transport.end_credential_request()
    await app.settle()

    assert app.state("a").credential_prompt is None
    assert app.state("a").intent_active
    assert app.rig.journal.calls("transport.submit_credential") == []
    assert coordinator.markers == ()


async def test_assembled_dismissal_after_dispatch_suppresses_late_reporting(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    outcome = Deferred()
    app, coordinator, transport = await _build_credential_app(
        build, monkeypatch, tmp_path, outcome=outcome,
    )
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    dialog.findChild(QLineEdit, "secretField").insert(transport.CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await asyncio.wait_for(transport.submit_entered.wait(), timeout=2)
    await app.settle()

    assert app.state("a").credential_prompt is not None
    app.prompt_buttons(dialog)["pairingCancelButton"].click()
    await app.settle()
    assert app.state("a").credential_prompt is None
    assert coordinator.markers[0].state == "watching"

    outcome.resolve(Ok(value=NoReply(reason="incomplete_reply")))
    await app.settle()
    assert coordinator.markers[0].state == "release_pending"
    assert "outcome is unknown" not in app.notice
    assert "Connected to" not in app.notice


async def _build_real_stop_app(build, monkeypatch, tmp_path, *, outcome=None):
    import test_matrix

    root = tmp_path / "real-stop-recovery"
    root.mkdir(mode=0o700)
    registry = _AssemblyRegistry(root / "owned-runtime", InstanceId(UUID(int=7391)))
    events: list[str] = []
    control = _RealCredentialControl(registry, events)
    if outcome is not None:
        control.outcome = outcome
    binding = _DoubleTakeTransportBinding(registry, control)
    parent = test_matrix.SessionRig

    class RealTransportSessionRig(parent):
        def __init__(self):
            super().__init__()
            store = JsonCredentialRecoveryMarkerStore(AtomicFiles(root / "recovery-state"))
            create = store.create

            async def create_and_observe(marker):
                result = await create(marker)
                if isinstance(result, Ok):
                    events.append("marker_published")
                return result

            store.create = create_and_observe
            coordinator = CredentialRecoveryCoordinator(store, registry, fences=self.fences)
            coordinator.events = events
            startup_completed = coordinator._startup_completed

            def record_startup_completion():
                events.append("startup_complete")
                startup_completed()

            coordinator._startup_completed = record_startup_completion
            binding.bind(self, coordinator)
            self.deps = replace(
                self.deps, transport=binding, credential_recovery=coordinator,
            )

    monkeypatch.setattr(test_matrix, "SessionRig", RealTransportSessionRig)
    app = await build(transport=binding)
    if binding.real is None:
        raise AssertionError("the production transport was not bound to the UI rig")
    return app, app.manager.deps.credential_recovery, control, binding.real


async def _hold_readiness_status_until_stop(app, control):
    control.status_entered.clear()
    gate = control.queue_status_gate()
    app.rig.scheduler.advance(0.1)
    await app.settle()
    await asyncio.wait_for(control.status_entered.wait(), timeout=2)
    return gate


async def _run_real_pre_submit_stop_contract(build, monkeypatch, tmp_path):
    app, coordinator, control, transport = await _build_real_stop_app(build, monkeypatch, tmp_path)
    app.discover("a")
    app.discover("b", address="192.0.2.20")
    await app.settle()
    app.click(Action.start)
    await app.settle()

    state = app.state("a")
    assert state.credential_prompt is not None
    assert state.stream is None
    stream, = transport._streams.values()
    start_handle = stream.handle
    assert start_handle is not None
    assert control.target == state.target

    readiness_gate = await _hold_readiness_status_until_stop(app, control)
    control.disconnect_status_state = "pin_required"
    control.queue_status_reply(control.status_reply("pin_required"))
    handoffs = []
    stop_pending_start = transport.stop_pending_start

    async def observe_handoff(owner, stop_context):
        result = await stop_pending_start(owner, stop_context)
        handoffs.append((owner, stop_context, result))
        return result

    transport.stop_pending_start = observe_handoff
    await app.dispatchem(Stop(receiver_id=a()))

    assert stream.readiness_stop.is_set(), "Stop must signal the real readiness poll"
    assert stream.handoff_ready.is_set(), "the acquisition must signal handoff readiness"
    assert stream.handoff_claimed is True, "Stop must claim the pending handle"
    assert control.status_read_cancelled == 1
    assert len(handoffs) == 1
    owner, stop_context, handed = handoffs[0]
    assert isinstance(handed, Ok) and handed.value == start_handle
    assert owner.credential_submission_id is None
    assert owner.marker_generation is None
    assert coordinator.markers == ()
    assert control.submission_targets == []
    assert control.disconnect_targets == [state.target.canonical_host]

    duplicate_handoff = await stop_pending_start(owner, stop_context)
    assert isinstance(duplicate_handoff, Err), "a pending handle may be transferred only once"

    await app.settle()
    assert app.state("a").cleanup_pending, "target-present status must block cleanup"
    assert start_handle.lease_id in transport._streams
    assert control.status_return_states[-1] == "pin_required"
    return app, coordinator, control, transport, readiness_gate, state


async def test_assembled_pre_submit_stop_uses_real_pending_start_handoff_and_target_cleanup(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, control, transport, readiness_gate, state = (
        await _run_real_pre_submit_stop_contract(build, monkeypatch, tmp_path)
    )
    control.disconnect_status_state = "absent"
    await app.dispatchem(RetryCleanup(receiver_id=a()))

    assert not app.state("a").cleanup_pending
    assert app.state("a").stream is None
    assert control.disconnect_targets == [state.target.canonical_host] * 2
    assert control.status_return_states[-1] == "absent"
    assert not transport._streams
    assert readiness_gate.resolved is False


async def test_assembled_dispatched_stop_keeps_real_status_settlement_and_handoff_owned(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    outcome = Deferred()
    app, coordinator, control, transport = await _build_real_stop_app(
        build, monkeypatch, tmp_path, outcome=outcome,
    )
    control.submission_state = "connecting"
    control.disconnect_status_state = "connecting"
    app.discover("a")
    app.discover("b", address="192.0.2.20")
    await app.settle()
    app.click(Action.start)
    await app.settle()

    start_handle = next(iter(transport._streams.values())).handle
    state = app.state("a")
    assert start_handle is not None
    assert state.credential_prompt is not None
    assert state.stream is None
    dialog = await app.prompt("pairingDialog")
    canary = "route-b-real-transport-canary-3617"
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert(canary)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await asyncio.wait_for(control.submit_entered.wait(), timeout=2)
    assert control.submission_targets == [state.target]

    outcome.resolve(Ok(value=Submitted(
        reply_at=app.rig.clock.now(), accepted=True, post_dispatch_error=None,
    )))
    control.hold_credential_status = True
    await asyncio.wait_for(control.credential_status_entered.wait(), timeout=2)
    assert control.status_state == "connecting"
    assert state.stream is None
    assert app.state("a").stream is None

    stream, = transport._streams.values()
    handoffs = []
    stop_pending_start = transport.stop_pending_start

    async def observe_handoff(owner, stop_context):
        result = await stop_pending_start(owner, stop_context)
        handoffs.append((owner, stop_context, result))
        return result

    transport.stop_pending_start = observe_handoff
    await app.dispatchem(Stop(receiver_id=a()))

    assert stream.readiness_stop.is_set()
    assert stream.handoff_ready.is_set()
    assert stream.handoff_claimed is True
    assert len(handoffs) == 1
    assert handoffs[0][0].credential_submission_id is not None
    assert handoffs[0][0].marker_generation is not None
    assert isinstance(handoffs[0][2], Ok) and handoffs[0][2].value == start_handle
    assert control.disconnect_targets == []
    assert coordinator.markers[0].state == "watching"
    assert app.state("a").stream is None
    assert not control.release_credential_status.is_set()

    control.status_state = "streaming"
    control.release_credential_status.set()
    await app.settle()

    assert "streaming" in control.status_return_states
    assert control.submission_targets == [state.target]
    assert control.canary_matched
    assert control.events.index("marker_published") < control.events.index("credential_dispatch")
    assert control.disconnect_targets == [state.target.canonical_host]
    assert app.state("a").cleanup_pending
    assert start_handle.lease_id in transport._streams
    assert app.state("a").stream is None

    control.disconnect_status_state = "absent"
    await app.dispatchem(RetryCleanup(receiver_id=a()))

    assert control.disconnect_targets == [state.target.canonical_host] * 2
    assert not app.state("a").cleanup_pending
    assert not transport._streams
    assert app.state("a").stream is None
    assert field.text() == ""
    assert canary not in repr(app.state("a"))
    assert canary not in repr(coordinator.markers)
    assert canary not in repr(app.journal)
    assert canary not in app.activity_text


async def test_assembled_dispatched_stop_keeps_the_original_command_deadline(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    outcome = Deferred()
    app, coordinator, control, transport = await _build_real_stop_app(
        build, monkeypatch, tmp_path, outcome=outcome,
    )
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()

    state = app.state("a")
    assert state.credential_prompt is not None
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    canary = "route-b-deadline-canary-4286"
    field.insert(canary)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await asyncio.wait_for(control.submit_entered.wait(), timeout=2)
    assert control.disconnect_targets == []

    app.rig.scheduler.advance(CONTROL_TIMEOUT - 0.01)
    await app.settle()
    await app.dispatchem(Stop(receiver_id=a()))

    assert app.state("a").cleanup_pending
    assert control.disconnect_targets == []
    assert app.state("a").stream is None

    app.rig.scheduler.advance(0.01)
    await app.settle()

    assert not app.state("a").cleanup_pending
    assert control.disconnect_targets == [state.target.canonical_host]
    assert app.state("a").stream is None
    assert coordinator.markers[0].state == "release_pending"
    assert field.text() == ""
    assert canary not in repr(app.state("a"))
    assert canary not in repr(coordinator.markers)
    assert canary not in repr(app.journal)
    assert canary not in app.activity_text


async def test_assembled_no_reply_reports_unknown_and_keeps_the_target_marker(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(
        build, monkeypatch, tmp_path, outcome=Ok(value=NoReply(reason="service_shutdown")),
    )
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    dialog.findChild(QLineEdit, "secretField").insert(transport.CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await app.settle()

    marker, = coordinator.markers
    stored = await coordinator._store.for_target(marker.target)
    assert isinstance(stored, Ok) and stored.value == marker
    assert marker.state == "release_pending"
    assert marker.automatic_status_checks is True
    assert app.state("a").credential_prompt.report == "outcome_unknown"
    assert app.notice.split("\n\n", 1)[0] == "No complete command reply was received; the outcome is unknown."
    assert "A delayed connection may still appear." in app.notice
    assert transport.status_targets == []
    assert transport.CANARY not in repr(marker)
    assert transport.CANARY not in repr(app.journal)


async def test_assembled_negative_reply_still_uses_target_status_observation(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    outcome = Ok(value=Submitted(reply_at=0.0, accepted=False, post_dispatch_error=None))
    app, coordinator, transport = await _build_credential_app(
        build, monkeypatch, tmp_path, outcome=outcome, observed_state="absent",
    )
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    dialog.findChild(QLineEdit, "secretField").insert(transport.CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await app.settle()

    assert transport.status_targets == [app.state("a").target]
    assert app.state("a").credential_prompt.report == "not_observed_streaming"
    assert app.notice.split("\n\n", 1)[0] == "The target was not observed streaming."
    assert coordinator.markers[0].state == "retired"
    assert "rejected" not in app.notice.lower()


async def test_assembled_submitted_prompt_stays_read_only_until_status_arrives(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(build, monkeypatch, tmp_path)
    transport.hold_status = True
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert(transport.CANARY)
    app.prompt_buttons(dialog)["pairingSubmitButton"].click()
    await asyncio.wait_for(transport.status_entered.wait(), timeout=2)
    await app.settle()

    prompt = app.state("a").credential_prompt
    assert prompt is not None and prompt.state == "observing" and prompt.report is None
    assert field.isReadOnly()
    assert coordinator.markers[0].state == "watching"
    transport.release_status.set()
    await app.settle()

    assert app.state("a").credential_prompt.report == "connected"
    assert coordinator.markers[0].state == "retired"


async def test_assembled_cancel_sends_no_credential_command_or_later_report(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(build, monkeypatch, tmp_path)
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    app.prompt_buttons(dialog)["pairingCancelButton"].click()
    await app.settle()

    assert app.state("a").credential_prompt is None
    assert coordinator.markers == ()
    assert not app.rig.journal.calls("transport.submit_credential")
    assert transport.status_targets == []
    assert app.notice == ""


async def test_virtual_display_start_cannot_open_a_credential_prompt(
    build: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    app, coordinator, transport = await _build_credential_app(
        build, monkeypatch, tmp_path, saved=(saved_receiver("a", resolution=remembered()),),
    )
    app.discover("a")
    await app.settle()
    await app.start_virtual()

    assert app.state("a").mode is Mode.VIRTUAL
    assert transport.request_events == []
    assert app.state("a").credential_prompt is None
    assert coordinator.markers == ()
    assert app.presenter._relay_prompt_pending == {}
    assert not app.rig.journal.calls("transport.submit_credential")


# --------------------------------------------------------------------- two sessions


async def test_two_receivers_render_their_own_rows_and_receive_their_own_commands(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("a")
    app.discover("b", address="192.0.2.20")
    await app.settle()

    assert [row.receiver_id for row in app.rows()] == [a(), b()]
    assert app.summary == "2 TVs available  0 sessions"

    # A: physical mirror.  Selecting a receiver is a real table-row selection.
    app.select("a")
    await app.settle()
    assert app.title == "Living Room 192.0.2.10"
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    a_stream = app.state("a").stream
    assert a_stream is not None

    # B: virtual display at the remembered explicit size, concurrently.
    app.select("b")
    await app.settle()
    assert app.phase == "Available"
    assert app.title == "Living Room 192.0.2.20"
    await app.start_virtual()
    assert app.phase == "Virtual display"
    b_stream = app.state("b").stream
    b_display = app.state("b").display_handle
    assert b_stream is not None and b_stream != a_stream
    assert b_display is not None

    assert app.summary == "2 TVs available  2 sessions"
    assert app.row("a").mode_label == "Mirror"
    assert app.row("b").mode_label == "Virtual display"
    assert app.row("b").size_label == "1920x1080"
    assert app.row("a").size_label == ""

    started = app.calls("transport.start_stream")
    assert [field_of(call["target"], "receiver_id") for call in started] == [a(), b()]
    created = app.calls("display.create")
    assert [call["receiver_id"] for call in created] == [b()]
    assert app.rig.display.created == [b_display]

    # Stopping B releases exactly B's stream and leaves A's untouched.
    app.click(Action.stop)
    await app.settle()
    assert app.phase == "Available"
    assert app.state("b").stream is None
    assert app.state("a").stream == a_stream
    assert set(app.rig.transport.streams) == {a_stream.lease_id}
    assert app.rig.display.removed == [b_display.owner_uuid]

    stopped = app.calls("transport.stop_stream")
    assert [field_of(call["handle"], "lease_id") for call in stopped] == [
        b_stream.lease_id
    ]
    assert app.row("a").mode_label == "Mirror"
    assert app.row("b").mode_label == ""


async def test_audio_failure_and_mute_stay_receiver_local(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    app.select("b")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    b_stream = app.state("b").stream
    assert b_stream is not None

    # A loses audio; B keeps both its stream and its mute control.
    app.select("a")
    await app.settle()
    a_stream = app.state("a").stream
    assert a_stream is not None
    app.rig.transport.emit(app.transport_event(TransportEventKind.audio_failed, "a"))
    await app.settle()
    assert app.offered() == {Action.retry_audio, Action.stop}
    assert app.button(Action.mute).isEnabled() is False
    app.select("b")
    await app.settle()
    assert app.phase == "Virtual display"
    assert app.button(Action.mute).isEnabled()
    app.button(Action.mute).click()
    await app.settle()
    assert app.rig.transport.muted == {b_stream.lease_id: True}
    assert app.state("a").stream == a_stream

    # Retrying A's audio re-targets A's exact stream and never B's.
    app.select("a")
    await app.settle()
    app.click(Action.retry_audio)
    await app.settle()
    audio = [
        field_of(call["handle"], "lease_id") for call in app.calls("transport.start_audio")
    ]
    # A's start, B's start, then A's retry: each audio control names its own lease.
    assert audio == [a_stream.lease_id, b_stream.lease_id, a_stream.lease_id]
    assert app.badges["audio"] == "Audio on"
    app.select("b")
    await app.settle()
    assert app.badges["audio"] == "Muted"


async def test_a_confirmed_switch_stops_before_it_starts_the_new_mode(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    first = app.state("a").stream
    assert first is not None

    # The pane's control is locked during a session: it can never present two
    # active modes.  The presenter's documented confirmation path is what a
    # request (keyboard/API) reaches, and it must stop before starting.
    assert app.mode_buttons() == (True, False, False)
    app.window.mode_requested.emit(Mode.VIRTUAL.value)
    await app.settle()
    dialog = await app.prompt("confirmStopDialog")
    assert app.phase == "Mirroring", "a pending confirmation must not stop anything"
    assert app.mode_buttons() == (True, False, False)
    assert any("Virtual display" in text for text in app.prompt_texts(dialog))
    app.prompt_buttons(dialog)["keepSessionButton"].click()
    await app.settle()
    assert [type(command).__name__ for command in app.dispatched] == ["Start"]
    assert app.phase == "Mirroring"
    assert app.state("a").stream == first

    app.window.mode_requested.emit(Mode.VIRTUAL.value)
    await app.settle()
    dialog = await app.prompt("confirmStopDialog")
    app.prompt_buttons(dialog)["confirmStopButton"].click()
    await app.settle()

    assert [type(command).__name__ for command in app.dispatched] == [
        "Start", "ConfirmSwitch",
    ]
    switch = app.dispatched[-1]
    assert isinstance(switch, ConfirmSwitch)
    assert switch.receiver_id == a()
    assert switch.confirmed is True
    assert switch.request.mode is Mode.VIRTUAL
    assert app.phase == "Virtual display"
    after = app.state("a").stream
    assert after is not None and after != first
    assert app.state("a").display_handle is not None
    assert set(app.rig.transport.streams) == {after.lease_id}

    names = app.journal.names()
    stop_at = names.index("transport.stop_stream")
    create_at = names.index("display.create")
    start_at = names.index("transport.start_stream", stop_at)
    assert stop_at < create_at < start_at
    stopped = app.calls("transport.stop_stream")
    assert [field_of(call["handle"], "lease_id") for call in stopped][-1] == first.lease_id


# --------------------------------------------------------------------- prompts


async def test_a_pairing_prompt_is_tracked_for_the_receiver_that_earned_it(
    build: Callable[..., Any],
) -> None:
    app = await build()
    app.discover("a")
    app.discover("b")
    await app.settle()

    app.select("a")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    a_dialog = await app.prompt("pairingDialog")
    a_field = a_dialog.findChild(QLineEdit, "secretField")
    a_field.insert("1111")
    a_challenge = app.state("a").challenge
    assert a_challenge is not None

    # Opening B's prompt adopts it and dismisses (wiping) A's open prompt.
    app.select("b")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    b_dialog = await app.prompt("pairingDialog")
    assert b_dialog is not a_dialog
    # The superseded prompt was closed before the new one took over; Qt may have
    # destroyed it already, which also destroys any typed secret.  If it is still
    # alive, the field must already be folded, wiped and un-undoable.
    if shiboken_alive(a_dialog):
        assert a_dialog.isVisible() is False
        assert a_field.text() == "" and a_field.isUndoAvailable() is False
    assert app.window._tracked_prompt is b_dialog
    b_challenge = app.state("b").challenge
    assert b_challenge is not None and b_challenge != a_challenge

    b_field = b_dialog.findChild(QLineEdit, "secretField")
    b_field.insert("2222")
    app.prompt_buttons(b_dialog)["pairingSubmitButton"].click()
    await app.settle()

    submits = app.calls("pairing.submit_pairing")
    assert [call["challenge_id"] for call in submits] == [b_challenge.challenge_id]
    assert app.state("b").trust is TrustState.Trusted
    # A was not submitted by B's prompt: its challenge is still live and its
    # snapshot still reports the pairing attempt, not trust.
    assert app.state("a").challenge == a_challenge
    assert app.state("a").trust is TrustState.Pairing
    assert "1111" not in app.activity_text and "2222" not in app.activity_text

    # A's challenge survived B's prompt; its own prompt still works afterwards.
    app.select("a")
    await app.settle()
    assert app.phase == "Pairing"
    assert Action.submit_pairing in app.offered()
    app.click(Action.submit_pairing)
    await app.settle()
    fresh = await app.prompt("pairingDialog")
    field = fresh.findChild(QLineEdit, "secretField")
    field.insert("3333")
    app.prompt_buttons(fresh)["pairingSubmitButton"].click()
    await app.settle()
    assert app.badges["trust"] == "Paired"
    assert a() in app.rig.pairing.trusted
    assert len(app.calls("pairing.submit_pairing")) == 2
    assert [call["challenge_id"] for call in app.calls("pairing.submit_pairing")] == [
        b_challenge.challenge_id, a_challenge.challenge_id,
    ]


async def test_a_pairing_submit_after_the_endpoint_moved_is_never_a_trust_commit(
    build: Callable[..., Any],
) -> None:
    app = await build()
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("b")
    await app.settle()
    app.click(Action.pair)
    await app.settle()
    dialog = await app.prompt("pairingDialog")
    field = dialog.findChild(QLineEdit, "secretField")
    field.insert("pinned-pin")
    challenge = app.state("b").challenge
    assert challenge is not None
    assert challenge.endpoint_generation == 1
    trust_generation = app.state("b").trust_generation

    # The receiver answers from a replaced endpoint: the challenge is retired and
    # the open prompt is invalidated (closed and wiped) before any secret leaves.
    app.rig.discovery.add_target(target("b", 2))
    await app.settle()

    assert app.state("b").challenge is None
    assert app.state("b").trust is not TrustState.Trusted
    assert app.state("b").trust_generation == trust_generation + 1
    if shiboken_alive(dialog):
        assert field.text() == "" and field.isUndoAvailable() is False
        assert dialog.isVisible() is False
        # A user can still press the stale prompt's submit button.  The field is
        # wiped and the control disabled, so no stale secret can be submitted.
        buttons = app.prompt_buttons(dialog)
        if "pairingSubmitButton" in buttons:
            buttons["pairingSubmitButton"].click()
            await app.settle()
    assert app.calls("pairing.submit_pairing") == []
    assert b() not in app.rig.pairing.trusted

    # A late submit for the retired challenge is refused by the pairing authority
    # itself; it never commits trust for B, and it never touches A.
    await app.dispatchem(
        SubmitPairing(
            receiver_id=b(),
            challenge_id=challenge.challenge_id,
            value=SensitiveText("late-pin"),
        )
    )
    assert b() not in app.rig.pairing.trusted
    assert app.rig.pairing.journal.calls("pairing.submit_pairing") == []
    assert app.state("a").trust is TrustState.Unknown
    assert app.state("a").challenge is None
    assert app.state("b").transport is not TransportState.Streaming


# ------------------------------------------------------------- late completions


async def test_a_late_reconnect_failure_only_diagnoses_its_own_receiver(
    build: Callable[..., Any],
) -> None:
    app = await build(
        transport=AcceptanceTransport(),
        saved=(saved_receiver("b", resolution=remembered("b")),),
    )
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("b")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    retained = app.state("b").display_handle
    assert retained is not None

    app.rig.transport.emit(app.transport_event(TransportEventKind.network_lost, "b"))
    await app.settle()
    assert app.phase == "Reconnecting"

    # Hold B's retry, select A, and complete A's own start while B is suspended.
    deferred = Deferred()
    app.rig.transport.queue_result("start_stream", deferred)
    app.click(Action.retry)
    await app.settle()
    # The retry is in flight and fenced to B; nothing about A has changed yet.
    assert app.state("b").transport is TransportState.Connecting
    app.select("a")
    await app.settle()
    app.click(Action.start)
    await app.settle()
    assert app.phase == "Mirroring"
    assert app.error == ""

    deferred.resolve(Err(error=diagnostic(ErrorCode.stale_target)))
    await app.settle()

    # A keeps streaming and shows no error; the failure belongs to B alone.
    assert app.state("a").transport is TransportState.Streaming
    assert app.error == ""
    assert app.state("b").display_handle == retained
    app.select("b")
    await app.settle()
    assert app.state("b").transport is TransportState.Reconnecting
    assert app.error == diagnostic(ErrorCode.stale_target, b()).message
    assert app.state("b").display_handle == retained
    assert app.rig.display.removed == []


# ------------------------------------------------------------------- ownership


async def test_the_presenter_commands_the_one_composed_manager(
    build: Callable[..., Any],
) -> None:
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    await app.settle()

    assert isinstance(app.rig, SessionRig)
    assert app.presenter._manager is app.runtime.manager
    assert app.runtime.manager is not app.rig.manager
    assert set(app.runtime.manager.handlers) == set(Route)

    await app.start_virtual()
    assert len(app.calls("display.create")) == 1
    assert len(app.calls("transport.start_stream")) == 1
    # The disabled rig's manager owns nothing at all.
    assert app.rig.manager.snapshot(a()).display_handle is None
    assert app.rig.manager.snapshot(a()).stream is None

    app.click(Action.stop)
    await app.settle()
    assert len(app.calls("transport.stop_stream")) == 1
    assert len(app.calls("display.remove")) == 1
    assert app.rig.display.outputs == {}


async def test_a_released_output_never_leaves_phantom_cleanup_actions(
    build: Callable[..., Any],
) -> None:
    """A confirmed release must read as released on every later render.

    The presenter refreshes durable ownership evidence when the selection moves
    (a normal two-receiver action).  When the session then *releases* its output,
    the cached evidence is no longer the store's answer, and the pane must not
    keep describing a resource that no longer exists as "Ownership unconfirmed"
    with Remove/Retry controls while the phase says the receiver is Available.
    """
    app = await build(saved=(saved_receiver("a", resolution=remembered()),))
    app.discover("a")
    app.discover("b")
    await app.settle()
    app.select("a")
    await app.settle()
    await app.start_virtual()
    assert app.phase == "Virtual display"
    owned = app.state("a").display_handle
    assert owned is not None

    # A two-receiver user action: look at B, then come back to A.
    app.select("b")
    await app.settle()
    app.select("a")
    await app.settle()
    assert app.presenter._records.get(owned.owner_uuid) is not None

    app.click(Action.stop)
    await app.settle()
    assert app.state("a").display_handle is None
    assert app.rig.ownerships.records == {}

    assert app.phase == "Available"
    assert app.badges["display"] == "No output"
    assert app.error == ""
    assert Action.start in app.offered()
    assert not (
        app.offered()
        & {
            Action.retry_cleanup,
            Action.keep_cleanup,
            Action.confirm_orphan,
            Action.keep_orphan,
        }
    )


# ----------------------------------------------------------------- app lifetime


async def test_closing_the_window_during_creation_ends_in_a_truthful_cleanup(
    build: Callable[..., Any],
) -> None:
    """Closing during an in-flight creation never lies about the cleanup.

    Two things must hold.  While the reservation is genuinely unconfirmed the
    dashboard stays open with bounded progress instead of disappearing.  And the
    final result must be truthful: either cleanup is confirmed (``Ok``, window
    gone, nothing owned) or it is reported as unconfirmed (``Err``) with a
    resource that really is still unconfirmed.  A false "could not be confirmed"
    for resources that were in fact released is a shutdown defect.
    """
    app = await build(saved=(saved_receiver("b", resolution=remembered("b")),))
    app.discover("b")
    await app.settle()
    publish = Deferred()
    store = app.rig.ownerships
    write = store.write

    async def held(record):
        if record.stage == "published":
            await publish.wait()
        return await write(record)

    store.write = held
    try:
        await app.start_virtual()
        assert app.phase == "Creating display"
        assert app.window.close() is False
        await app.settle()
        assert app.window.isVisible(), "cleanup is still unconfirmed"
        assert "closing" in app.window.windowTitle().lower()
        assert app.presenter._shutdown_task is not None
        assert not app.presenter._shutdown_task.done()

        # The pending publication may now finish; cleanup owns its release.
        publish.resolve(None)
        outcome = await asyncio.wait_for(app.runtime.shutdown(), 8)
    finally:
        if not publish.resolved:
            publish.resolve(None)
        await app.settle(40)
        shutdown_task = app.presenter._shutdown_task
        if shutdown_task is not None and not shutdown_task.done():
            await asyncio.wait_for(shutdown_task, 8)

    if isinstance(outcome, Err):
        # A cleanup failure must name a resource that is genuinely unresolved.
        remaining = (
            dict(app.rig.ownerships.records)
            or dict(app.rig.transport.streams)
            or dict(app.rig.display.outputs)
            or list(app.rig.display.process.environments())
        )
        assert remaining, (
            "shutdown reported unconfirmed cleanup although every owned resource "
            f"was released: {outcome.error.code}"
        )
        assert app.window.isVisible()
        return

    assert isinstance(outcome, Ok)
    assert app.window.isVisible() is False
    assert app.runtime._closed
    assert app.rig.transport.streams == {}
    assert app.rig.display.outputs == {}
    assert app.rig.ownerships.records == {}
    assert app.rig.display.removed, "the late creation was released, not leaked"
    assert not app.manager.pending_resources(b())


async def test_a_second_instance_is_rejected_before_a_second_window_exists(
    build: Callable[..., Any], tmp_path: Any, capsys: pytest.CaptureFixture[str],
) -> None:
    from uuid import uuid4

    from airplay_linux.domain.operations import InstanceId
    from airplay_linux.process.instance import InstanceGuard
    from airplay_linux.process.linux import LinuxProcessRunner

    app = await build()
    window_class = app.window.__class__
    windows = [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.isVisible() and isinstance(widget, window_class)
    ]
    assert len(windows) == 1

    # The operator's entry point owns the single-instance lock for its lifetime.
    guard = InstanceGuard(app.runtime.config.runtime_root, LinuxProcessRunner())
    assert isinstance(guard.acquire(InstanceId(uuid4())), Ok)
    try:
        second = app_module._run_guarded(
            RuntimeConfig(
                state_root=tmp_path / "second" / "state",
                runtime_root=app.runtime.config.runtime_root,
                live=True,
            )
        )
    finally:
        guard.release()

    assert second == 1
    assert "another instance already owns this runtime root" in capsys.readouterr().err
    still = [
        widget
        for widget in QApplication.topLevelWidgets()
        if widget.isVisible() and isinstance(widget, window_class)
    ]
    assert still == windows
