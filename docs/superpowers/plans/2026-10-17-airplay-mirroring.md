# AirPlay Linux Mirroring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Build a PySide6 desktop sender supporting concurrent AirPlay receivers, physical-monitor Mirror, recoverable KDE Virtual display, system audio, and fenced remembered pairing.

**Architecture:** A Qt shell projects independent domain resources managed by one serialized actor per receiver. Inject discovery, capture, transport, credentials, process supervision, persistence, and KDE display adapters; keep blocking work outside actor and GUI queues. Freeze domain interfaces first, develop disjoint adapter lanes against deterministic fakes, then compose the lifecycle handlers and application.

**Tech Stack:** Python >=3.12, PySide6 >=6.6,<7, qasync >=0.27,<1, zeroconf >=0.140,<1, dbus-next >=0.2.3,<1, pytest >=8,<10, pytest-asyncio >=0.24,<2, pytest-qt >=4.4,<5; external doubletake/doubletake-ctl, PipeWire, xdg-desktop-portal, Secret Service, kscreen-doctor, and optional krfb-virtualmonitor.

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design. The initial repository contains this document only; every application path below is new.
- Product requirement: "Support KDE Plasma 6 Wayland as a guaranteed v1 environment." Other environments are capability-gated; missing dependencies fail the attempted capability and keep unrelated functionality usable. Do not install system packages automatically.
- Product requirement: "Support multiple receiver sessions at the same time." "Running Mirror and Virtual display modes concurrently for the same receiver" is a non-goal. Confirm Stop before switching, and start the queued mode only after confirmed cleanup.
- Product requirement: "Mirror mode accepts a physical monitor source only. It creates no virtual output." Never capture an application window, virtual output, or silently substituted physical monitor for Mirror.
- Product requirement: "Guessing a TV resolution when the receiver does not provide reliable information" is a non-goal. Resolve a typed decision before reserving a virtual output; remember it only for the same stable, compatible identity after successful setup.
- Keep `TransportState`, `DisplayState`, `AudioState`, and `TrustState` independently authoritative. Derive `SessionPhase` and UI actions from them; an unknown or unconfirmed resource is never reported released.
- Preserve a valid Virtual output across transport-only failure, receiver absence, local transport retry exhaustion, pairing, and daemon restart. Helper failure invalidates the source and triggers ordered cleanup.
- Each command gets a new operation UUID. Session generation advances only for a new superseding start, abandoned start, Stop/confirmed mode switch, resolution reconfiguration, or shutdown; same-intent retries and active challenge submission keep it. Transport leases, audio operation IDs, trust generations/challenges, and display owner identities fence their own resources.
- Transport state events require current receiver, endpoint fingerprint/generation, session generation, lease, stream, and daemon instance as applicable. Exact-handle cleanup acknowledgements can finish teardown after generation changes. Display events use owner UUID, connector/output, and helper PID/start time, independently of network generations. Stale successful acquisitions must still be released or durably recorded as cleanup uncertain.
- Teardown order: durably publish `desired_state=removing`/stage `removing` for any reserved/owned display, stop transport and confirm its release, stop audio control, stop/remove the exact owned helper/output and confirm disappearance, then conditionally restore eligible layout properties. A failure to publish removal intent blocks destructive teardown and exposes cleanup retry.
- Display reservation stages are exactly `reserved`, `helper_started`, `output_observed`, `published`, `removing`; desired state is exactly `active` or `removing`. Use temporary write, file fsync, atomic rename, and parent-directory fsync. The provider-wide creation lock spans baseline through verified publication. Never remove an unowned output automatically.
- Own one private foreground doubletake daemon through a Linux owner-death supervisor; never use `-daemonize`, the default socket, or an unrelated daemon. Runtime path is `$XDG_RUNTIME_DIR/airplay-linux/instances/<app_instance_uuid>/`; single-instance lock is `$XDG_RUNTIME_DIR/airplay-linux/airplay-linux.lock`. Authenticate owner token, UUID, socket, and PID/start-time pairs before use or termination.
- The transport coordinator is the only pairing authority. Commit trust only while challenge, receiver, endpoint, session generation, and trust generation still match. Cancel/expiry/Forget/replacement invalidate trust; compensate late success with revocation. Unconfirmed cancellation/revocation blocks new pairing and start as `TrustState.CleanupRequired`.
- Use backend keyring mode exclusively. PINs, passwords, opaque correlation tokens, provisional credentials, and backend secrets never enter metadata, exception text, process argv, activity logs, diagnostic exports, or test snapshots. Sensitive input is transient and has a redacted representation. Application code never implements AirPlay authentication or stores secret bytes in its own records.
- Audio defaults enabled. Before video readiness, failed/denied audio requires explicit Retry audio, Start video only, or Cancel. After video readiness, audio failure preserves video and disables mute; audio retry does not replace transport/display leases. Mute is receiver-local and session-only; failed mute preserves the last confirmed state.
- Network backoff is `min(1 * 2**attempt, 30)` seconds, with no offline retry limit and no jitter in v1. Local daemon restart budget is 3 attempts in 60 monotonic seconds. Pairing expires after 120 monotonic seconds and allows 3 invalid submissions. Control calls time out after 5 seconds, stream stop after 5 seconds, helper appearance/removal after 5 seconds, supervisor TERM escalation after 3 seconds, and whole-app shutdown after 15 seconds. These are implementation constants, not new receiver capability claims.
- Planning evidence: installed `doubletake-bin 0.5.0-1` help lists `-socket`, `-cred-backend`, `-daemonize`, `-target`, and `-no-audio`; ctl lists status/discover/devices/connect/pin/disconnect/mute/unmute, but does not establish foreground multi-target control, source verification, transactional trust/revocation, audio restart, or owner handshake. Treat each unverified operation as unsupported. No invented backend flags, socket messages, or simulated implementation may be presented as live capability.
- All paths are relative to the integration worktree. Task 1 freezes shared DTOs/protocols. Later tasks may create only their declared files; consume earlier interfaces without editing their owners. An interface conflict is a concrete integration finding, not permission to change an undeclared path. No task modifies the approved design or this plan during execution.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, and `LC_ALL=C.UTF-8`. Normal tests use no LAN, real keyring, TV, desktop mutation, or privileged operation. Live tests are separately marked and require explicit test-environment selection.
- File map: `domain/` owns immutable DTOs and protocols; `testing/` owns deterministic adapters; `diagnostics/` owns safe messages; `storage/` owns atomic metadata; `discovery/`, `process/`, `backend/`, `trust/`, `transport/`, `capture/`, `display/`, and `policy/` own their adapters; `session/` owns actor internals; `ui/` owns projection/widgets/presenter; `app.py` is the composition root. Tests mirror these directories.
- PM lane map: `foundation` Task 1 then Task 2; `diagnostics` Task 3; `storage` Task 4; `discovery` Tasks 5–6; `process` Tasks 7–9 with Task 8 also producing the backend wire contract; `trust` Task 10; `transport` Task 11; `capture` Task 12; `display` Tasks 13–15; `policy` Tasks 16–17; `session` Tasks 18–23; `ui` Tasks 24–26; `assembly` Task 27; `adapter-acceptance` Task 28; `lifecycle-acceptance` Task 29; `ui-acceptance` Task 30. File ownership is disjoint between lanes. Explicit per-task dependencies, rather than numeric adjacency, define scheduling.

## Task 1: Establish packaging and freeze typed domain interfaces

**Implementer tier:** Capable
**Lane/category:** foundation / shared-contract; prerequisite for every lane.
**Depends on:** none.

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `src/airplay_linux/__init__.py`
- Create: `src/airplay_linux/domain/__init__.py`
- Create: `src/airplay_linux/domain/identity.py`
- Create: `src/airplay_linux/domain/resources.py`
- Create: `src/airplay_linux/domain/operations.py`
- Create: `src/airplay_linux/domain/records.py`
- Create: `src/airplay_linux/domain/messages.py`
- Create: `src/airplay_linux/domain/ports.py`
- Test: `tests/domain/test_contracts.py`

**Interfaces:**
- Consumes: no application code; the approved design supplies invariants.
- Produces immutable, keyword-only dataclasses and string enums with the fields below. IDs are `NewType` wrappers around `str` (`ReceiverId`) or UUID (`OperationId`, `LeaseId`, `ChallengeId`, `OwnerId`, `InstanceId`); generations are nonnegative `int`; times are monotonic `float`, with separate UTC ISO timestamps in disk records.
- Produces `Result[T] = Ok[T](value: T) | Err(error: Diagnostic)`, `Diagnostic(code: ErrorCode, message: str, recovery: tuple[RecoveryAction, ...], receiver_id: ReceiverId | None = None, detail: str | None = None)`; `ErrorCode` includes portal_cancelled/denied, keyring_unavailable, unsupported_source/mode/operation, dependency_missing/incompatible, audio_denied/unavailable/mute_failed, receiver_rejected, stale_target/event, stale_socket, ownership_mismatch, helper_failed, configuration_failed, cleanup_timeout, trust_cleanup_required, resolution_mismatch, discovery_failed, transport_failed, daemon_failed, persistence_failed, invalid_input, pairing_expired. RecoveryAction contains retry, stop, back, choose_mode, video_only, retry_audio, retry_cleanup, retry_revocation, keep_blocked, remove_metadata, recreate, confirm_remove, keep.
- Produces identity DTOs: `ServiceRecord(key: str, service_type: Literal['_airplay._tcp','_raop._tcp'], instance: str, model: str, device_id: str | None, host: str, addresses: tuple[str,...], port: int, txt: Mapping[str,str], seen_at: float)`; `ReceiverRecordSet(records: tuple[ServiceRecord,...], receiver_id: ReceiverId, identity_kind: Literal['device','fingerprint','endpoint','ambiguous'], canonical_address: str, endpoint_fingerprint: str, endpoint_generation: int, persistable: bool, continuity_verified: bool, replacement_of: ReceiverId | None, ambiguous: bool)`; `ReceiverTarget(receiver_id, service_records: tuple[ServiceRecord,...], canonical_host: str, service_port: int, protocol: str, endpoint_fingerprint: str, endpoint_generation: int)`; `ReceiverView(record_set: ReceiverRecordSet, display_name: str, online: bool, pairing_hint: str | None)`.
- Produces resource enums exactly as designed: `Mode(MIRROR='mirror', VIRTUAL='virtual')`; TransportState Idle/Pairing/Connecting/Streaming/Reconnecting/Stopping/Failed; DisplayState None/Resolving/Creating/Owned/Reconfiguring/Removing/Failed/CleanupRequired; AudioState Disabled/Starting/Enabled/Muted/Unavailable; TrustState Unknown/Untrusted/Trusted/Pairing/RevocationPending/CleanupRequired; SessionPhase Available/Offline/ResolutionRequired/AudioDecisionRequired/CreatingDisplay/Pairing/Connecting/Mirroring/VirtualDisplay/Reconnecting/Reconfiguring/Recovering/Stopping/CleanupRequired/TrustCleanupRequired/Failed. Use Python member `DisplayState.None_` for serialized value `'None'` and the listed PascalCase members for the other states. `IntentStage` values idle/starting/active/resolution/audio_decision/reconfiguring/recovering/stopping/cleanup describe pending procedural intent; they do not replace resource state. `StartRequest` is defined in `domain/resources.py`; ErrorCode, RecoveryAction, Diagnostic, Ok, Err, and Result also live there, while secret wrappers and operation identities live in `domain/operations.py`.
- Produces `Size(width: int, height: int)` (positive, maximum 16384 each); `PhysicalMonitorSource(connector: str, portal_restore_token: str | None, capture_lease_id: LeaseId | None = None)`; `VirtualOutputSource(connector: str, kscreen_output_id: str, size: Size)`; `CaptureSource = PhysicalMonitorSource | VirtualOutputSource`; `AudioPolicy(enabled: bool = True)`; `ResolutionEvidence(origin: str, reliable: bool, receiver_id: ReceiverId, endpoint_fingerprint: str | None)`; `ResolutionDecision(size: Size, source: Literal['stored','discovery','user'], evidence: ResolutionEvidence)`; `ReceiverCapabilities(modes: tuple[Size,...], reliable: bool, evidence: str)`; unknown modes are `None`.
- Produces operation DTOs: `OperationContext(operation_id: OperationId, session_generation: int, endpoint_generation: int, transport_lease_id: LeaseId | None = None, trust_generation: int | None = None, audio_operation_id: OperationId | None = None)`; `ProcessIdentity(pid: int, start_ticks: int)`; `StreamHandle(receiver_id: ReceiverId, endpoint_generation: int, endpoint_fingerprint: str, backend_stream_id: str, daemon_instance_uuid: InstanceId, lease_id: LeaseId, start_operation_id: OperationId)`; `DisplayHandle(owner_uuid: OwnerId, source: VirtualOutputSource, helper: ProcessIdentity)`.
- Produces transient `SensitiveText` with `reveal_for_backend() -> str` and redacted repr/str; `OpaqueCredentialRef`, `OpaqueProvisionalCredentialRef`, and `OpaqueBackendToken` with redacted repr, equality by opaque ID, and no generic serialization. `PairingChallenge(challenge_id, receiver_id, endpoint_fingerprint, endpoint_generation, operation_id, session_generation, trust_generation, created_at, expires_at, credential_kind: Literal['pin','password'], backend_token: OpaqueBackendToken)` is coordinator-private. `ChallengeView` has the same public identity/time/kind fields and excludes the token. `PairingResult(kind: Literal['Trusted','InvalidInput','ReceiverRejected','Cancelled','CredentialUnavailable','TrustCleanupRequired','TransportFailure'], credential_ref: OpaqueCredentialRef | None, diagnostic: Diagnostic | None)`.
- Produces disk DTOs: `LayoutSnapshot(outputs: Mapping[str,Mapping[str,str|int|bool]], baseline_hash: str)`; `OwnershipRecord(schema_version: int, owner_uuid: OwnerId, app_instance_uuid: InstanceId, runtime_directory: str, creation_operation_id: OperationId, receiver_id: ReceiverId, endpoint_fingerprint: str, desired_state: Literal['active','removing'], stage: Literal['reserved','helper_started','output_observed','published','removing'], requested_size: Size, observed_size: Size | None, helper: ProcessIdentity | None, connector: str | None, output_id: str | None, created_utc: str, last_configuration_step: str, baseline: LayoutSnapshot, last_applied: Mapping[str,Mapping[str,str|int|bool]], provider_version: str, capability_fingerprint: str)`; `DaemonRecord(schema_version: int, instance_uuid: InstanceId, owner_token: str, app: ProcessIdentity, supervisor: ProcessIdentity | None, daemon: ProcessIdentity | None, protocol_version: str, socket_path: str, lifecycle: Literal['starting','running','stopping'], runtime_directory: str)`. Owner tokens are process authentication data confined to runtime records, never exported. Recovered helpers retain their original record's runtime_directory, not the new GUI's directory.
- Produces `SessionSnapshot(receiver_id: ReceiverId, target: ReceiverTarget | None, online: bool, mode: Mode | None, intent_active: bool, intent_stage: IntentStage, phase: SessionPhase, transport: TransportState, display: DisplayState, audio: AudioState, trust: TrustState, physical_source: PhysicalMonitorSource | None, display_handle: DisplayHandle | None, ownership_uuid: OwnerId | None, resolution: ResolutionDecision | None, observed_resolution: Size | None, accepted_resolution: Size | None, audio_policy: AudioPolicy, muted: bool, stream: StreamHandle | None, challenge: ChallengeView | None, last_error: Diagnostic | None, reconnect_attempt: int, next_retry_at: float | None, operation_id: OperationId | None, session_generation: int, transport_lease_id: LeaseId | None, trust_generation: int, audio_operation_id: OperationId | None, queued_start: StartRequest | None, cleanup_pending: bool)` with an `idle(receiver_id: ReceiverId, online: bool = False) -> SessionSnapshot` factory. `SavedReceiver(receiver_id: ReceiverId, service_metadata: tuple[ServiceRecord,...], display_name: str, last_endpoint: ReceiverTarget | None, continuity_fingerprint: str, credential_ref: OpaqueCredentialRef | None, remembered_resolution: ResolutionDecision | None)` is non-secret and unavailable for ambiguous identities. A new start resets muted=False; recovered same-intent sessions may restore session mute.
- Produces commands in `domain/messages.py`: Start(receiver_id: ReceiverId, request: StartRequest), Stop(receiver_id: ReceiverId), Cancel(receiver_id: ReceiverId), Retry(receiver_id: ReceiverId), SubmitPairing(receiver_id: ReceiverId, challenge_id: ChallengeId, value: SensitiveText), Pair(receiver_id: ReceiverId), Forget(receiver_id: ReceiverId), ChooseResolution(receiver_id: ReceiverId, decision: ResolutionDecision), ChangeResolution(receiver_id: ReceiverId, decision: ResolutionDecision), RecreateAccepted(receiver_id: ReceiverId), SetMuted(receiver_id: ReceiverId, muted: bool), RetryAudio(receiver_id: ReceiverId), StartVideoOnly(receiver_id: ReceiverId), RetryCleanup(receiver_id: ReceiverId), RetryRevocation(receiver_id: ReceiverId), KeepCleanup(receiver_id: ReceiverId), ConfirmSwitch(receiver_id: ReceiverId, request: StartRequest, confirmed: bool), ConfirmOrphanRemoval(receiver_id: ReceiverId, owner_uuid: OwnerId, confirmed: bool); `Command` is this tagged union. `StartRequest(mode: Mode, source: PhysicalMonitorSource | None = None, resolution: ResolutionDecision | None = None, audio: AudioPolicy = AudioPolicy())` lives in resources.py. `CommandReceipt(operation_id: OperationId, accepted: bool, diagnostic: Diagnostic | None)` acknowledges acceptance rather than completion.
- Produces typed events in messages.py: `TransportEvent(kind: TransportEventKind, receiver_id: ReceiverId, stream: StreamHandle | None, endpoint_fingerprint: str, endpoint_generation: int, daemon_instance_uuid: InstanceId, context: OperationContext, payload: TransportPayload)`; kinds ready/stopped/network_lost/pairing_required/credential_invalid/audio_start_failed/audio_failed/audio_recovered/mute_result/accepted_mode/failed/daemon_exit. Payload is a tagged union of `EmptyPayload`, `FailurePayload(diagnostic: Diagnostic)`, `ModePayload(size: Size, reliable: bool)`, `MutePayload(muted: bool, ok: bool)`, `ChallengePayload(challenge: ChallengeView)`; no raw backend message. `DisplayEvent(kind: Literal['reserved','helper_started','output_observed','published','removed','helper_exit','disappeared','cleanup_required'], receiver_id: ReceiverId, creation_operation_id: OperationId | None, owner_uuid: OwnerId, connector: str | None, output_id: str | None, helper: ProcessIdentity | None, handle: DisplayHandle | None, diagnostic: Diagnostic | None)`; the creation operation binds an initial reservation once, and is not an endpoint-dependent event fence. `DiscoveryEvent(kind: Literal['added','updated','removed','error'], receiver: ReceiverView | None, diagnostic: Diagnostic | None)`; `TimerEvent(receiver_id: ReceiverId, kind: Literal['reconnect','pairing_expiry','cleanup_deadline','local_retry'], context: OperationContext)`; `EffectEvent(receiver_id: ReceiverId, operation_id: OperationId, session_generation: int, kind: str, result: Result[object], resource: StreamHandle | DisplayHandle | PhysicalMonitorSource | None)` is internal and never persisted. `ResourceEvent` is this event union.
- Produces protocols in `domain/ports.py`; asynchronous operations that can fail return Result and sanitize exception text; pure lookups, subscriptions and lifecycle close methods use their explicit types below. `Clock.now() -> float`; `Scheduler.call_at(when: float, callback: Callable[[],None]) -> CancelHandle` with `cancel() -> None`; `IdFactory.new() -> UUID`; `Subscription.close() -> None`; each event source exposes `subscribe(callback: Callable[[EventType],None]) -> Subscription`.
- Produces `DiscoveryPort.start() -> Awaitable[Result[None]]`, `.refresh() -> Awaitable[Result[None]]`, `.current(receiver_id: ReceiverId) -> ReceiverView | None`, `.subscribe(callback: Callable[[DiscoveryEvent],None]) -> Subscription`, `.close() -> Awaitable[None]`; `CapturePort.list_physical() -> Awaitable[Result[tuple[PhysicalMonitorSource,...]]]`, `.select(connector: str) -> Awaitable[Result[PhysicalMonitorSource]]`, `.verify(source: PhysicalMonitorSource) -> Awaitable[Result[None]]`, `.release(source: PhysicalMonitorSource) -> Awaitable[Result[None]]`. Release closes exactly that portal session after stream release; it is idempotent and must also be invoked for a stale successful selection.
- Produces `TransportPort.update_target(target: ReceiverTarget | None, receiver_id: ReceiverId) -> None`; `.query_modes(target: ReceiverTarget, context: OperationContext) -> Awaitable[Result[ReceiverCapabilities | None]]`; `.start_stream(target: ReceiverTarget, source: CaptureSource, audio_policy: AudioPolicy, context: OperationContext) -> Awaitable[Result[StreamHandle]]`; `.stop_stream(handle: StreamHandle, context: OperationContext) -> Awaitable[Result[None]]`; `.set_muted(handle: StreamHandle, muted: bool, context: OperationContext) -> Awaitable[Result[None]]`; `.start_audio(handle: StreamHandle, context: OperationContext) -> Awaitable[Result[None]]`; `.subscribe(callback: Callable[[TransportEvent],None]) -> Subscription`; `.shutdown() -> Awaitable[Result[None]]`. Target freshness is checked immediately before backend I/O, not only at enqueue time.
- Produces `PairingPort.begin_pairing(target: ReceiverTarget, context: OperationContext) -> Awaitable[Result[PairingChallenge]]`; `.submit_pairing(challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> Awaitable[PairingResult]`; `.cancel_pairing(receiver_id: ReceiverId, challenge_id: ChallengeId | None, context: OperationContext) -> Awaitable[Result[None]]`; `.forget_trust(receiver_id: ReceiverId, context: OperationContext) -> Awaitable[Result[None]]`; `.mark_invalid(receiver_id: ReceiverId, context: OperationContext) -> Awaitable[Result[None]]`; `.retry_revocation(receiver_id: ReceiverId, context: OperationContext) -> Awaitable[Result[None]]`. Display and transport depend on these protocols, never a second pairing implementation.
- Produces `CredentialStore.load_credential_ref(receiver_id: ReceiverId) -> Awaitable[Result[OpaqueCredentialRef | None]]`; `.prepare_pairing(challenge: PairingChallenge, backend_result: OpaqueBackendToken) -> Awaitable[Result[OpaqueProvisionalCredentialRef]]`; `.commit_pairing(provisional_ref: OpaqueProvisionalCredentialRef, trust_generation: int) -> Awaitable[Result[OpaqueCredentialRef]]`; `.cancel_pairing(challenge: PairingChallenge) -> Awaitable[Result[None]]`, `.revoke_pairing(ref_or_receiver: OpaqueProvisionalCredentialRef | ReceiverId) -> Awaitable[Result[None]]`, `.mark_invalid(receiver_id: ReceiverId) -> Awaitable[Result[None]]`.
- Produces `DisplayPort.create(receiver_id: ReceiverId, endpoint_fingerprint: str, size: Size, context: OperationContext) -> Awaitable[Result[DisplayHandle]]`; `.prepare_teardown(receiver_id: ReceiverId, superseded_operation_id: OperationId | None, context: OperationContext) -> Awaitable[Result[tuple[OwnerId,...]]]` first fences an admitted pending create and durably marks every existing reservation for that intent removing without stopping helpers; `.mark_removing(owner_uuid: OwnerId) -> Awaitable[Result[None]]`; `.remove(owner_uuid: OwnerId, context: OperationContext) -> Awaitable[Result[None]]`; `.reconcile() -> Awaitable[tuple[RecoveryItem,...]]`; `.approve_orphan(owner_uuid: OwnerId, confirmed: bool, context: OperationContext) -> Awaitable[Result[None]]`; `.subscribe(callback: Callable[[DisplayEvent],None]) -> Subscription`. `RecoveryItem(kind: Literal['recovering','removing','orphan','cleanup_required','discarded'], record: OwnershipRecord, handle: DisplayHandle | None, diagnostic: Diagnostic | None)`. Stage progress is emitted before helper launch and at each durable update. A teardown gate prevents a deferred admitted create from reserving/publishing active after cancellation even if no owner UUID existed when Stop arrived.
- Produces `SessionStore.save(snapshot: SessionSnapshot) -> Awaitable[Result[None]]`, `.load_all() -> Awaitable[tuple[SessionSnapshot,...]]`; `ReceiverStore.get(receiver_id: ReceiverId) -> Awaitable[Result[SavedReceiver | None]]`, `.all() -> Awaitable[Result[tuple[SavedReceiver,...]]]`, `.put(saved: SavedReceiver) -> Awaitable[Result[None]]`, `.remove_metadata(receiver_id: ReceiverId) -> Awaitable[Result[None]]`; `OwnershipStore.write(record: OwnershipRecord) -> Awaitable[Result[None]]`, `.all() -> Awaitable[Result[tuple[OwnershipRecord,...]]]`, `.delete(owner_uuid: OwnerId) -> Awaitable[Result[None]]`; `DaemonStore.write(record: DaemonRecord) -> Awaitable[Result[None]]`, `.all() -> Awaitable[Result[tuple[DaemonRecord,...]]]`, `.delete(instance_uuid: InstanceId) -> Awaitable[Result[None]]`. `ActivitySink.emit(event: ActivityEvent) -> None`; `ActivityEvent(utc: str, code: str, receiver_id: ReceiverId | None, message: str, fields: Mapping[str,str|int|bool])` accepts allowlisted fields only.
- Produces process protocols: `CommandSpec(argv: tuple[str,...], env: Mapping[str,str], cwd: str | None = None, pass_fds: tuple[int,...] = ())` (no sensitive argv); `CommandOutput(exit_code: int, stdout: bytes, stderr: bytes)`; `ProcessPort.run(spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Awaitable[Result[CommandOutput]]`, `.spawn(spec: CommandSpec) -> Awaitable[Result[ProcessIdentity]]`, `.identity(pid: int) -> ProcessIdentity | None`, `.owned(identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str) -> bool`, `.terminate(identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float) -> Awaitable[Result[None]]`, `.subscribe(callback: Callable[[ProcessExit],None]) -> Subscription`; `ProcessExit(identity: ProcessIdentity, exit_code: int)`.
- Produces `CapabilityReport(environment: str, discovery: bool, mirror: bool, virtual_display: bool, audio: bool, remembered_pairing: bool, additional_virtual_outputs: bool, diagnostics: tuple[Diagnostic,...])`; `AudioProbe.check() -> Awaitable[Result[None]]`; `ManagerPort.dispatch(command: Command) -> Awaitable[CommandReceipt]`, `.ingest(event: ResourceEvent) -> None`, `.snapshot(receiver_id: ReceiverId) -> SessionSnapshot`, `.subscribe(callback: Callable[[SessionSnapshot],None]) -> Subscription`, `.shutdown() -> Awaitable[Result[None]]`.

- [ ] **Step 1: Write failing contract tests.** Use enum-value parameterization, invalid sizes, frozen mutation, typed source discrimination, safe sensitive repr, and a snapshot with independent retained resources. Include this executable seed:

```python
from dataclasses import replace
import pytest
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.resources import AudioState, DisplayState, TransportState
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.operations import SensitiveText

def test_resources_are_independent():
    state = SessionSnapshot.idle(ReceiverId('airplay-device:001122334455'))
    state = replace(state, transport=TransportState.Reconnecting,
                    display=DisplayState.Owned, audio=AudioState.Unavailable)
    assert state.display is DisplayState.Owned
    assert state.transport is TransportState.Reconnecting

def test_sensitive_input_has_no_printable_value():
    secret = SensitiveText('canary-pin-8496')
    assert '8496' not in repr(secret)
    assert '8496' not in str(secret)
    with pytest.raises(TypeError):
        import json
        json.dumps(secret)
```

- [ ] **Step 2: Configure and demonstrate red.** Create pyproject build metadata using setuptools src discovery, package name `airplay-linux`, version `0.1.0`, the dependencies in the header, optional `test` dependencies, pytest `asyncio_mode='auto'`, testpaths `tests`, markers `live_backend`, `live_kde`, `live_hardware`, and script `airplay-linux = airplay_linux.app:main`. Set Python path in pytest configuration and `[tool.setuptools.package-data]` to `airplay_linux = ['resources/*']` so Task 27's desktop resource needs no packaging-file edit. Add setuptools>=68 and wheel to the test/build environment, with `[build-system] requires=['setuptools>=68','wheel']`. Run `PYTHONPATH=src python -m pytest -q tests/domain/test_contracts.py`; expect collection failure for missing domain modules. Do not invoke the not-yet-created entry point.
- [ ] **Step 3: Implement the frozen DTOs and protocols.** Use `@dataclass(frozen=True, kw_only=True)` and `StrEnum`; give snapshot optional resources safe idle defaults. Set `.gitignore` to `.venv/`, `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `*.egg-info/`, `build/`, `dist/`, `.coverage`. Do not serialize DTOs with unrestricted `asdict`; storage owns explicit projections. All secret wrapper constructors reject pickling and expose no `__dict__` serializer. Group records/types by the declared files and re-export nothing implicitly from package init. Protocol annotations use forward imports under `TYPE_CHECKING` to prevent circular imports.
- [ ] **Step 4: Verify.** Run `PYTHONPATH=src python -m pytest -q tests/domain`; expect no failures. Use `python -m pip install -e '.[test]'` in the execution environment before later tasks; installation is setup, not a test, and no system packages are installed by the app.
- [ ] **Step 5: Commit.** `git add pyproject.toml .gitignore src/airplay_linux/__init__.py src/airplay_linux/domain tests/domain/test_contracts.py` then `git commit -m "feat(domain): establish typed resource and adapter contracts"`.

## Task 2: Supply deterministic adapters and a controllable event laboratory

**Implementer tier:** Advanced
**Lane/category:** foundation / test-seams.
**Depends on:** Task 1.

**Files:**
- Create: `src/airplay_linux/testing/__init__.py`
- Create: `src/airplay_linux/testing/time.py`
- Create: `src/airplay_linux/testing/adapters.py`
- Create: `src/airplay_linux/testing/builders.py`
- Test: `tests/testing/test_fakes.py`

**Interfaces:**
- Consumes Task 1 `Clock.now() -> float`, `Scheduler.call_at(float, Callable[[],None]) -> CancelHandle`, `IdFactory.new() -> UUID`, and all adapter protocols and DTOs listed in `domain/ports.py`; Result is `Ok(value) | Err(Diagnostic)`, events are the typed ResourceEvent union.
- Produces `FakeClock(start: float = 0).now() -> float`; `FakeScheduler(clock).advance(seconds: float) -> None`, `.pending() -> tuple[float,...]`, stable FIFO for equal deadlines; `SequenceIds(seed: int = 1).new() -> UUID`.
- Produces `Deferred[T].resolve(value: T) -> None`, `.wait() -> Awaitable[T]`; `Journal.entries: list[tuple[str,object]]`, `.names() -> list[str]`; `FakeTransport`, `FakePairing`, `FakeCapture`, `FakeDisplay`, `FakeDiscovery`, `FakeProcess`, `MemorySessionStore`, `MemoryReceiverStore`, `MemoryOwnershipStore`, `MemoryDaemonStore`, `FakeCredentialStore`, `MemoryActivity`, `FakeAudioProbe`, `FakeManager`, each satisfying its Task 1 protocol. Every async method has a FIFO scripted `queue_result(method: str, result: Result[object] | Deferred[object]) -> None`; event sources expose `.emit(typed_event) -> None`. `FakeDiscovery.add_target(target: ReceiverTarget) -> None` builds and emits its matching ReceiverView; `.remove_target(receiver_id: ReceiverId) -> None` emits removed while retaining last-known view offline. `FakeManager.commands: list[Command]`, `.emit_snapshot(snapshot: SessionSnapshot) -> None`, `.snapshot(receiver_id: ReceiverId) -> SessionSnapshot`, and async `.dispatch(command: Command) -> CommandReceipt` support presenter tests. Default fakes return Ok for verified operations, audio available, None for queried modes, and fake trusted keyring refs; pairing_required is emitted only when a test scripts the receiver challenge.
- Produces builders `target(receiver: str = 'a', endpoint_generation: int = 1, address: str = '192.0.2.10') -> ReceiverTarget`, `physical(connector: str = 'DP-1') -> PhysicalMonitorSource`, `virtual_handle(owner: int = 1, output: str = 'v1') -> DisplayHandle`, `context(generation: int = 1, endpoint_generation: int = 1, lease: int | None = 1, trust_generation: int = 0) -> OperationContext`, `service(kind: str = 'airplay', **overrides) -> ServiceRecord`; builder override keys must be real dataclass fields and reject unknown names.

- [ ] **Step 1: Write failing fake tests.** Pin cancellation, equal-time FIFO, late completion after caller cancellation, independently controlled receiver results, exact PID/start matching, stage-write failures, and event unsubscription.

```python
from airplay_linux.testing.time import FakeClock, FakeScheduler

def test_equal_deadlines_and_cancel():
    clock = FakeClock()
    scheduler = FakeScheduler(clock)
    seen = []
    scheduler.call_at(1, lambda: seen.append('first'))
    cancelled = scheduler.call_at(1, lambda: seen.append('cancelled'))
    scheduler.call_at(1, lambda: seen.append('last'))
    cancelled.cancel()
    scheduler.advance(1)
    assert seen == ['first', 'last']
    assert scheduler.pending() == ()
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/testing/test_fakes.py`; expect missing testing modules.
- [ ] **Step 3: Implement.** Use a heap of `(deadline, sequence, callback)`; never wall-clock sleeps. Scripted fakes validate target freshness and exact handles just like their protocols, but do not precompute expected session transitions. Support independent transport/audio/display/pairing deferrals, arbitrary stale events, owner-token/PID reuse, KScreen snapshots, owner environment scans, and atomic-write failure at write/fsync/rename/directory-fsync. The Journal stores secret-free typed public arguments; SensitiveText and backend tokens are replaced by redacted sentinels. Keep deferred backend success observable after cancellation using a separately owned completion future.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/testing`; expect no failures. Tests must prove advancing fake time alone triggers retry callbacks and no real process is spawned.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/testing tests/testing/test_fakes.py` then `git commit -m "test(core): add deterministic time process and resource fakes"`.

## Task 3: Define safe diagnostics and user recovery messages

**Implementer tier:** Standard
**Lane/category:** diagnostics / observable-errors.
**Depends on:** Tasks 1–2.

**Files:**
- Create: `src/airplay_linux/diagnostics/__init__.py`
- Create: `src/airplay_linux/diagnostics/catalog.py`
- Create: `src/airplay_linux/diagnostics/activity.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes Task 1 `Diagnostic(code: ErrorCode, message: str, recovery: tuple[RecoveryAction,...], receiver_id: ReceiverId | None, detail: str | None)`, `ActivityEvent(utc, code, receiver_id, message, fields: Mapping[str,str|int|bool])`, `ActivitySink.emit(event) -> None`; Task 2 `MemoryActivity` and `SequenceIds.new() -> UUID` for deterministic fixtures.
- Produces `diagnostic(code: ErrorCode, receiver_id: ReceiverId | None = None, *, command: str | None = None) -> Diagnostic` from an allowlisted catalog; `ActivityLog(max_entries: int = 1000).emit(event) -> None`, `.entries() -> tuple[ActivityEvent,...]`, `.subscribe(callback) -> Subscription`, `.export() -> str`; `safe_activity(code: str, receiver_id: ReceiverId | None, fields: Mapping[str,str|int|bool], utc: str) -> ActivityEvent`.

- [ ] **Step 1: Write failing tests.** Cover a distinct helpful message/recovery for every ErrorCode, bounded log ordering, subscriber removal, and rejected fields/values for PIN, password, credential, correlation token, owner token, restore token, raw argv/stdout/stderr/exception. Test the actual output channel:

```python
import pytest
from airplay_linux.diagnostics.activity import safe_activity

def test_secret_field_cannot_enter_activity():
    with pytest.raises(ValueError, match='field'):
        safe_activity('pairing', None, {'password': 'canary-8496'}, '2026-10-17T00:00:00Z')
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect missing diagnostics modules.
- [ ] **Step 3: Implement.** Use fixed catalog messages, e.g. `unsupported_source: 'The backend cannot verify this physical monitor. Select a supported source or update doubletake.'`, `cleanup_timeout: 'Removal could not be confirmed. Retry cleanup or keep this resource for manual cleanup.'`, and trust cleanup instructions that explicitly block pairing. Allow fields receiver_name, phase, transport, display, audio, trust, command_name, retry_seconds, resolution, connector, output_id, reservation_stage, endpoint_generation, error_code, explicit_video_only only. Values come from sanitized DTO fields, never backend text; bound text lengths and escape controls. Exports include counts/capabilities and safe activity only, never runtime owner records or raw stored snapshots. Diagnostics construction accepts an allowlisted executable name, not arbitrary error strings.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics`; expect no failures, and canary strings absent from emitted entries/export/error representations.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics tests/diagnostics/test_safety.py` then `git commit -m "feat(diagnostics): add safe actionable resource diagnostics"`.

## Task 4: Implement durable non-secret records and continuity-aware preferences

**Implementer tier:** Advanced
**Lane/category:** storage / durable-metadata.
**Depends on:** Tasks 1–3.

**Files:**
- Create: `src/airplay_linux/storage/__init__.py`
- Create: `src/airplay_linux/storage/atomic.py`
- Create: `src/airplay_linux/storage/codecs.py`
- Create: `src/airplay_linux/storage/repositories.py`
- Test: `tests/storage/test_atomic_records.py`
- Test: `tests/storage/test_secret_boundaries.py`

**Interfaces:**
- Consumes Task 1 `SessionStore.save(SessionSnapshot) -> Awaitable[Result[None]]`, `.load_all() -> Awaitable[tuple[SessionSnapshot,...]]`, ReceiverStore/OwnershipStore/DaemonStore protocols, OwnershipRecord/DaemonRecord/SavedReceiver schemas, OpaqueCredentialRef; Task 2 fake stores and failure scripting; Task 3 `diagnostic(ErrorCode, receiver_id=None, *, command=None) -> Diagnostic`.
- Produces `AtomicFiles(root: Path).write(relative: str, payload: bytes) -> None`, `.read(relative: str) -> bytes | None`, `.delete(relative: str) -> None`, with injected filesystem operations for failure tests; `JsonSessionStore`, `JsonReceiverStore`, `JsonOwnershipStore`, `JsonDaemonStore` implementing their ports; constructors accept `AtomicFiles`.
- Produces `encode_session(snapshot) -> bytes`, `decode_session(bytes) -> SessionSnapshot`, corresponding `encode/decode_ownership`, `encode/decode_daemon`, `encode/decode_receiver`; version is 1. `PreferenceAccess.for_record_set(record_set: ReceiverRecordSet, saved: SavedReceiver | None) -> SavedReceiver | None` returns nothing for ambiguous/replaced/unverified-continuity fallback identities even if their hashed ID is unchanged.

- [ ] **Step 1: Write failing tests.** Parameterize failures before temp write, file fsync, rename, and directory fsync; old or new complete records are readable and uncertainty is surfaced. Cover malformed/truncated/unknown-version JSON quarantined as diagnostics without deletion, path traversal/symlink rejection, 0700 directories/0600 files, and secret rejection:

```python
from dataclasses import replace
from airplay_linux.storage.codecs import encode_session
from airplay_linux.testing.builders import target
from airplay_linux.domain.records import SessionSnapshot

def test_session_disk_shape_has_no_challenge_or_secret():
    state = replace(SessionSnapshot.idle(target().receiver_id), target=target())
    payload = encode_session(state)
    assert b'backend_token' not in payload
    assert b'portal_restore_token' not in payload
    assert b'password' not in payload
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/storage`; expect missing storage modules.
- [ ] **Step 3: Implement.** Write explicit field allowlists, JSON schema/version checks and enum decoding, no pickle/asdict. Keep receiver/session/ownership data under `$XDG_STATE_HOME/airplay-linux` (fallback `~/.local/state/airplay-linux`); daemon runtime records under their private runtime directories. Persist physical connector choice but omit portal restore tokens and capture lease IDs from general metadata; keep those tokens/leases in the protected capture adapter's own private runtime memory. Persist an opaque keyring reference identifier only, never provisional refs or correlation tokens. Disk session state is evidence of intent, not proof of live streams/displays; ownership records are authoritative. Never persist ambiguous receivers. A failed directory fsync is an uncertain durable write and returns persistence_failed, not silent success. Delete via unlink plus directory fsync only after the caller has confirmed resource release.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/storage`; expect no failures. Check every encoded DTO against canary secret input, restore-token input, backend correlation, and opaque provisional values; no snapshots containing those values are written.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/storage tests/storage` then `git commit -m "feat(storage): persist atomic resource records without secrets"`.

## Task 5: Derive conservative receiver identities after cross-service aggregation

**Implementer tier:** Capable
**Lane/category:** discovery / identity.
**Depends on:** Tasks 1–2.

**Files:**
- Create: `src/airplay_linux/discovery/__init__.py`
- Create: `src/airplay_linux/discovery/identity.py`
- Test: `tests/discovery/test_identity.py`

**Interfaces:**
- Consumes Task 1 ServiceRecord, ReceiverRecordSet, ReceiverTarget, ReceiverId; Task 2 `service(kind='airplay', **overrides) -> ServiceRecord` and `FakeClock.now() -> float`.
- Produces `normalize_device_id(value: str | None) -> str | None` (MAC-like values become 12 lowercase hex digits); `IdentityResolver.seed_saved(saved: tuple[SavedReceiver,...]) -> None` loads only prior non-secret endpoint/identity evidence before browsing and never asserts credential validity; `.update(records: tuple[ServiceRecord,...]) -> tuple[ReceiverRecordSet,...]`, retaining previous sets to detect continuity/replacement and increment endpoint generations; `target_for(record_set: ReceiverRecordSet) -> Result[ReceiverTarget]` rejects ambiguous/offline sets. Device IDs use `airplay-device:`, stable label/model hashes use `airplay-fingerprint:`, host/model hashes use `airplay-endpoint:`, ambiguous record-set hashes use `airplay-ambiguous:`.

- [ ] **Step 1: Write failing parameterized tests.** Cases must include equal IDs across services, conflicting nonempty IDs at the same host, one-ID and no-ID fallback with equal label/model/current canonical address, normalized RAOP MAC prefixes, empty labels, port changes, same-address hostname changes, changed-address fallback replacement, identical labels at two addresses, multiple fallback matches, IPv4/IPv6 canonicalization, service removal, and both advertisement arrival orders.

```python
from airplay_linux.discovery.identity import IdentityResolver
from airplay_linux.testing.builders import service

def test_conflicting_device_ids_never_merge():
    records = (service(device_id='00:11:22:33:44:55'),
               service(kind='raop', device_id='66:77:88:99:aa:bb'))
    merged = IdentityResolver().update(records)
    assert len(merged) == 2
    assert {r.receiver_id for r in merged} == {
        'airplay-device:001122334455', 'airplay-device:66778899aabb'}
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/discovery/test_identity.py`; expect missing resolver.
- [ ] **Step 3: Implement the ordered matching algorithm.** Normalize labels with Unicode NFC, casefold, trim terminal service suffix, remove only a syntactically valid RAOP MAC prefix; normalize models with NFC/casefold/trim; normalize hosts with IDNA, casefold, trailing-dot removal; canonicalize addresses using `ipaddress` including IPv6 scope. Select a stable canonical address preferring sorted advertised IPv4 then sorted scoped IPv6, with no network I/O in the resolver; use address overlap to avoid treating harmless DNS ordering as replacement. First form equal-ID groups; never bridge conflicting ID groups through an ID-less record. Apply fallback edges only for an unambiguous one-to-one AirPlay/RAOP match at the same current canonical address with equal label/model, ignoring port. Ambiguity is resolved across the entire current graph, never first-match-wins. Hash normalized label + `|` + model for fingerprint, host + `|` + model for endpoint, and sorted canonical complete records for ephemeral ambiguity. Keep fallback endpoint-continuity evidence separately from stable hash: a new address invalidates saved trust/resolution even if a label hash repeats. Stable device IDs survive compatible service/port/host changes; new device IDs always yield new receivers. Target creation prefers the AirPlay service's advertised port/protocol rather than a RAOP audio port. Endpoint generation increments on address/port/protocol changes and removal, and never grants old targets validity again.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/discovery/test_identity.py`; expect no failures and identical merge classifications for all input permutations. Verify seeded cold-start evidence grants fallback preference access only for an exact compatible current endpoint, and rejects changed-address or ambiguous first advertisements in both arrival orders. Assert ambiguous candidates are nonpersistable and replacement candidates have `continuity_verified=False`.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/discovery tests/discovery/test_identity.py` then `git commit -m "feat(discovery): merge receiver records conservatively before identity"`.

## Task 6: Watch mDNS and expose domain discovery events

**Implementer tier:** Advanced
**Lane/category:** discovery / zeroconf-adapter.
**Depends on:** Tasks 1–3, 5.

**Files:**
- Create: `src/airplay_linux/discovery/zeroconf_adapter.py`
- Create: `tests/fixtures/discovery/services.json`
- Test: `tests/discovery/test_zeroconf_adapter.py`

**Interfaces:**
- Consumes Task 1 DiscoveryPort (`start/refresh/close`, `current(receiver_id) -> ReceiverView | None`, `subscribe(DiscoveryEvent callback)`), ServiceRecord and DiscoveryEvent; Task 5 `IdentityResolver.update(tuple[ServiceRecord,...]) -> tuple[ReceiverRecordSet,...]`, `target_for(set) -> Result[ReceiverTarget]`; Task 2 fake clock/event controls; Task 3 safe diagnostics/activity.
- Produces `ZeroconfDiscovery(resolver: IdentityResolver, clock: Clock, mdns: MdnsClient)` implementing DiscoveryPort; private `MdnsClient.browse(types: tuple[str,...], callback) -> Awaitable[Result[Subscription]]`, `.resolve(key: str) -> Awaitable[Result[ServiceRecord]]`, `.close() -> Awaitable[None]`, implemented using AsyncZeroconf and fakeable at tests.

- [ ] **Step 1: Write failing tests with an injected in-memory MdnsClient.** Verify both service subscriptions, raw TXT bytes normalization including invalid UTF-8, out-of-order resolve/removal, refresh without duplicates, display-name changes, offline retention, discovery error retaining known receivers, and separate replacement events.

```python
import pytest
from airplay_linux.discovery.zeroconf_adapter import ZeroconfDiscovery
from airplay_linux.discovery.identity import IdentityResolver
from airplay_linux.testing.time import FakeClock

@pytest.mark.asyncio
async def test_discovery_can_close_without_network():
    class Client:
        async def close(self):
            self.closed = True
    client = Client()
    adapter = ZeroconfDiscovery(IdentityResolver(), FakeClock(), client)
    await adapter.close()
    assert client.closed
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/discovery/test_zeroconf_adapter.py`; expect missing adapter.
- [ ] **Step 3: Implement.** Browse `_airplay._tcp.local.` and `_raop._tcp.local.`; strip `.local.` only at the raw-service seam, never change service instance meaning. Track per-record revision so a resolve result after removal cannot restore a vanished endpoint. Feed the complete live record set to the resolver before emitting added/updated/removed; keep last-known saved identities visible offline. Service removal invalidates the current target rather than commanding transport directly. Feature bits, pairing hints, and trustworthy mode evidence are normalized; model/display names alone never produce a resolution. Surface selected mDNS implementation failures as retryable discovery diagnostics, with working known-device UI. Implement scan as a bounded refresh of browsers, not an unbounded subprocess.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/discovery`; expect no failures, no multicast sockets in unit tests.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/discovery/zeroconf_adapter.py tests/fixtures/discovery/services.json tests/discovery/test_zeroconf_adapter.py` then `git commit -m "feat(discovery): adapt zeroconf events into receiver views"`.

## Task 7: Implement bounded Linux process operations and exact ownership evidence

**Implementer tier:** Advanced
**Lane/category:** process / process-runtime.
**Depends on:** Tasks 1–3.

**Files:**
- Create: `src/airplay_linux/process/__init__.py`
- Create: `src/airplay_linux/process/linux.py`
- Create: `src/airplay_linux/process/procfs.py`
- Test: `tests/process/test_linux_runner.py`
- Test: `tests/process/test_procfs.py`

**Interfaces:**
- Consumes Task 1 ProcessPort, ProcessIdentity(pid, start_ticks), CommandSpec(argv, env, cwd), CommandOutput(exit_code, stdout, stderr), ProcessExit; Task 2 FakeProcess and deterministic clock; Task 3 `diagnostic(...) -> Diagnostic`.
- Produces `LinuxProcessRunner(proc_root: Path = Path('/proc'))` implementing ProcessPort; `read_identity(pid: int, proc_root: Path) -> ProcessIdentity | None`; `owned_processes(owner_uuid: UUID, runtime_directory: str, proc_root: Path) -> tuple[ProcessIdentity,...]`; all signals revalidate start ticks and exact owner/runtime evidence immediately before use. When supported use pidfd for race-resistant waiting/signaling after initial identity validation.

- [ ] **Step 1: Write failing tests.** Use synthetic procfs files for PID reuse, `stat` comm containing spaces/parentheses, inaccessible environ, wrong runtime path, matching UUID with wrong start time, and nonmatching names. Use a real harmless Python child for timeout/output limits and SIGTERM-to-SIGKILL escalation; never signal an unrelated PID.

```python
from pathlib import Path
from airplay_linux.process.procfs import read_identity

def test_unknown_pid_is_absent(tmp_path: Path):
    assert read_identity(200, tmp_path) is None
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/process/test_linux_runner.py tests/process/test_procfs.py`; expect missing process modules.
- [ ] **Step 3: Implement.** Use `asyncio.create_subprocess_exec`, never shell=True; bound stdout/stderr to 1 MiB each and deadlines with monotonic scheduling; drain pipes while waiting to avoid deadlock. Read `/proc/<pid>/stat` field 22 after correctly locating the final comm delimiter; compare PID and start ticks, UID, owner UUID and exact runtime-directory environment. Missing/inaccessible evidence yields ownership_mismatch rather than a guessed kill. Env is explicitly inherited/overlaid with a minimal allowlist; never log env or stdin. Async cancellation terminates only a verified owned child and awaits bounded reap. Exact successful exit is separately observable for cleanup; command timeout has a typed diagnostic without raw stderr. Expose subscriptions for child exit, including abrupt exits and spawn failure.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/process/test_linux_runner.py tests/process/test_procfs.py`; expect no failures and no surviving owned test children. All real child tests have a finally block that reaps the exact child.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/process tests/process/test_linux_runner.py tests/process/test_procfs.py` then `git commit -m "feat(process): add bounded execution and exact Linux ownership checks"`.

## Task 8: Pin the published backend contract and capability negotiation

**Implementer tier:** Capable
**Lane/category:** process / backend-contract.
**Depends on:** Tasks 1–3, 7.

**Files:**
- Create: `src/airplay_linux/backend/__init__.py`
- Create: `src/airplay_linux/backend/contract.py`
- Create: `src/airplay_linux/backend/control.py`
- Create: `tests/fixtures/backend/doubletake-0.5-help.txt`
- Create: `tests/fixtures/backend/doubletake-ctl-0.5-help.txt`
- Create: `tests/fixtures/backend/status-0.5.json`
- Create: `tests/fixtures/backend/contract-v1.json`
- Create: `docs/backend-compatibility.md`
- Test: `tests/backend/test_contract.py`
- Test: `tests/backend/test_control.py`

**Interfaces:**
- Consumes Task 1 Result/Diagnostic, ReceiverTarget, OperationContext, CaptureSource, ProcessPort.run(CommandSpec, timeout, stdin=None), Clock; Task 2 scripted process fakes; Task 3 safe diagnostics.
- Produces `features_from_help(sender_help: str, ctl_help: str) -> BackendFeatures`, a conservative pure decoder; `BackendFeatures(profile: str, version: str, foreground_control: bool, owner_handshake: bool, multi_target: bool, target_bound_pairing: bool, physical_source: bool, virtual_source: bool, audio_probe: bool, audio_restart: bool, target_mute: bool, keyring: bool, provisional_commit: bool, cancel_pairing: bool, revoke_pairing: bool, query_modes: bool)`; `probe_backend(process: ProcessPort) -> Awaitable[Result[BackendFeatures]]` performs read-only help/version/contract probes only. The contract-v1 fixture has `provenance='application-contract-fixture'` and `live_support_proven=false`.
- Produces `BackendControl.request(operation: BackendOperation, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None) -> Awaitable[Result[BackendReply]]`, `.subscribe(callback: Callable[[BackendNotice],None]) -> Subscription`, `.handshake(owner_token: str, instance_uuid: InstanceId) -> Awaitable[Result[Handshake]]`, `.close() -> Awaitable[None]`; `Handshake(instance_uuid, owner_token, daemon: ProcessIdentity, protocol_version: str)` is runtime-private. BackendOperation enum includes status, connect, disconnect, mute, unmute, query_modes, start_audio, begin_pairing, submit_pairing, cancel_pairing, prepare_pairing, commit_pairing, revoke_pairing, mark_invalid, shutdown. BackendPayload/Reply/Notice are explicit tagged dataclasses mapped to domain types; secret-bearing variants have redacted repr and never reach normal diagnostics.
- Produces `PublishedDoubletakeProfile` mapping only externally documented operations to actual ctl argv or socket requests, with proof provenance in `docs/backend-compatibility.md`; `UnsupportedControl` implements BackendControl and rejects unavailable operations. A protocol-v1 fixture is an application adapter-contract fixture, explicitly not proof that an installed doubletake implements it.

- [ ] **Step 1: Write failing contract tests.** Capture the installed read-only help (ctl --help may exit 2), and a status fixture consistent with documented `streams`, `needs_credential`, `credential_kind`, and legacy `needs_pin`. Test malformed JSON, wrong owner, mismatched socket, timeout, old status formats, missing tools, and per-operation capability checks. Seed:

```python
from airplay_linux.backend.contract import features_from_help

def test_legacy_help_does_not_prove_safe_sources_or_pairing():
    features = features_from_help('-socket path\n-cred-backend string\n-daemonize',
                                  'connect [target] [PIN-or-password]\npin <PIN-or-password>')
    assert not features.physical_source
    assert not features.provisional_commit
    assert not features.owner_handshake
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/backend`; expect missing backend contract. Read the installed man pages and the exact published release source/contract for the detected version; record URL/version and operation evidence in the compatibility document. Do not launch a live sender during contract discovery.
- [ ] **Step 3: Implement the codec and gates.** Whitelist verified ctl status/discover/devices and target-specific connect/disconnect/mute/unmute with an explicit private `-socket` argument. Never issue unqualified disconnect/mute/unmute or positional PIN/password arguments. Use a published secret-safe socket channel only when verified; otherwise pairing is unsupported. Do not infer foreground control from `-daemonize` documentation, keyring transactions from `-cred-backend`, or physical selection from generic portal support. A compatible published version must independently prove foreground control, authenticated owner handshake, typed source verification, target scoping, and safe trust cancellation/revocation before those starts become available; absent proof yields dependency_incompatible or unsupported_operation with exact remediation. Decoder maps backend messages to safe typed fields, never passes free-form errors to GUI. An implemented profile must map concrete published wire fields and have captured conformance fixtures; no invented live wire schema. Keep all AirPlay protocol work external. Profile-v1 fake messages exercise the full required adapter semantics even when the local release fails capability checks.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/backend`; expect no failures. Current 0.5 help must fail closed for unsupported obligations while discovery and the application shell remain usable. `docs/backend-compatibility.md` must distinguish verified supported operations, absent operations, and optional live verification; unknown values cannot enable a capability.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/backend tests/backend tests/fixtures/backend docs/backend-compatibility.md` then `git commit -m "feat(backend): pin published control contract and fail closed on gaps"`.

## Task 9: Own a foreground daemon through supervisor and runtime reconciliation

**Implementer tier:** Capable
**Lane/category:** process / daemon-ownership.
**Depends on:** Tasks 1–4, 7–8.

**Files:**
- Create: `src/airplay_linux/process/supervisor.py`
- Create: `src/airplay_linux/process/daemon_registry.py`
- Test: `tests/process/test_supervisor.py`
- Test: `tests/process/test_daemon_registry.py`

**Interfaces:**
- Consumes Task 1 DaemonRecord and DaemonStore (`write/all/delete`), ProcessPort, ProcessIdentity, Clock/Scheduler/IdFactory; Task 4 AtomicFiles and JsonDaemonStore; Task 7 exact owned-process checks; Task 8 BackendFeatures and `BackendControl.handshake(owner_token, instance_uuid) -> Awaitable[Result[Handshake]]`.
- Produces `DaemonSupervisor` executable via `python -m airplay_linux.process.supervisor`, accepting app PID/start, instance UUID, runtime directory, and a private inherited config FD; `.run() -> int` owns one foreground child. `DaemonRegistry.ensure() -> Awaitable[Result[DaemonRecord]]`, `.reconcile_previous() -> Awaitable[Result[None]]`, `.restart() -> Awaitable[Result[DaemonRecord]]`, `.shutdown() -> Awaitable[Result[None]]`, `.subscribe(callback: Callable[[DaemonNotice],None]) -> Subscription`; `DaemonNotice(kind: Literal['ready','exit','cleanup_required'], instance_uuid, diagnostic)`.

- [ ] **Step 1: Write failing tests.** Pin durable starting-before-spawn, running-only-after-handshake, stopping-before-TERM, PID reuse, mismatched owner/socket, stale socket with live process, starting/stopping crash windows, parent death, direct-child death, surviving previous streams, and restart budget accounting. Use a fake daemon executable in tmp_path and real private Unix socket only in a disposable temporary runtime directory.

```python
import pytest
from airplay_linux.process.supervisor import foreground_argv

def test_foreground_command_never_daemonizes():
    argv = foreground_argv('/usr/bin/doubletake', '/run/test/private.sock')
    assert '-daemonize' not in argv
    assert '-socket' in argv
    assert argv[argv.index('-socket') + 1] == '/run/test/private.sock'
    assert argv[argv.index('-cred-backend') + 1] == 'keyring'
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/process/test_supervisor.py tests/process/test_daemon_registry.py`; expect missing supervisor/registry.
- [ ] **Step 3: Implement supervision.** Supervisor sets `prctl(PR_SET_PDEATHSIG, SIGTERM)` before spawning, then rechecks recorded app PID/start to close the parent-death race. Child installs its own parent-death signal in a minimal launcher before exec so supervisor death cannot leave a free daemon. Avoid threaded preexec_fn; use a dedicated launcher process. Forward termination, request target release, wait up to 3 seconds then KILL exact child and reap. Bind the config pipe using CommandSpec.pass_fds, close the GUI's copies after spawn, and pass authentication config through that protected FD/runtime record, never a printable token argument. Runtime dir 0700/socket path owned by current UID, no symlink traversal. Write starting before launch, record supervisor/daemon PID-start pairs as known, accept socket only after path/record/process/owner-token/protocol handshake, then publish running. Never launch an incompatible foreground backend merely to see whether it works.
- [ ] **Step 4: Implement prior-instance reconciliation and verify.** Authenticate each old record by UUID, owner token and exact process pairs; request shutdown and confirm both supervisor/daemon and streams gone before replacement. If app is dead and matching owned children survive, terminate only those exact children; if evidence is live-mismatched or inaccessible return cleanup_required and prohibit replacement streams. A stale socket is unlinked only after recorded processes are proven dead. Apply identical rules to starting/stopping records and failed atomic updates. Run `PYTHONPATH=src python -m pytest -q tests/process`; expect no failures, including real parent-death probe and bounded escalation.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/process/supervisor.py src/airplay_linux/process/daemon_registry.py tests/process/test_supervisor.py tests/process/test_daemon_registry.py` then `git commit -m "feat(process): supervise and reconcile private foreground daemon ownership"`.

## Task 10: Fence keyring trust transactions and compensate late pairing completion

**Implementer tier:** Capable
**Lane/category:** trust / pairing-transaction.
**Depends on:** Tasks 1–4, 8.

**Files:**
- Create: `src/airplay_linux/trust/__init__.py`
- Create: `src/airplay_linux/trust/fences.py`
- Create: `src/airplay_linux/trust/keyring.py`
- Create: `src/airplay_linux/trust/coordinator.py`
- Test: `tests/trust/test_pairing_transactions.py`
- Test: `tests/trust/test_keyring_boundary.py`

**Interfaces:**
- Consumes Task 1 PairingPort: `begin_pairing(target, context) -> Awaitable[Result[PairingChallenge]]`, `submit_pairing(challenge_id, SensitiveText, context) -> Awaitable[PairingResult]`, `cancel_pairing(receiver_id, challenge_id | None, context)`, `forget_trust(receiver_id, context)`, `mark_invalid(receiver_id, context)`, `retry_revocation(receiver_id, context)` each `Awaitable[Result[None]]`; Task 1 CredentialStore load/prepare/commit/cancel/revoke/mark_invalid methods; Task 4 ReceiverStore; Task 8 BackendControl.request(operation, target, payload, context), BackendFeatures; Task 2 Deferred and FakeCredentialStore.
- Produces `TrustFence(receiver_id: ReceiverId, endpoint_fingerprint: str | None, endpoint_generation: int, session_generation: int, trust_generation: int, active_challenge_id: ChallengeId | None = None)`; `TrustFenceRegistry.set(fence: TrustFence) -> None`, `.get(receiver_id: ReceiverId) -> TrustFence | None`, `.activate(challenge: PairingChallenge) -> Result[None]` compares all generation/target fields and atomically installs its active ID, `.clear_challenge(receiver_id: ReceiverId, challenge_id: ChallengeId) -> None` clears only that exact ID. Generation/target invalidation is synchronous and clears the active ID before backend effects. `fence_matches(challenge: PairingChallenge, fence: TrustFence, now: float) -> bool` checks identity, endpoint, session, trust, active_challenge_id, and expiry.
- Produces `BackendKeyringStore(control: BackendControl, features: BackendFeatures)` implementing CredentialStore; it forwards backend-owned opaque transactions in keyring mode, never reads keyring secret bytes itself. `PairingCoordinator(control, credentials: CredentialStore, receiver_store: ReceiverStore, fences: TrustFenceRegistry, clock: Clock, scheduler: Scheduler, ids: IdFactory)` implements PairingPort; `.pending_cleanup(receiver_id) -> bool` identifies blocked trust transactions. It is constructed only inside DoubleTakeTransport in Task 11.

- [ ] **Step 1: Write failing transaction tests.** Hold backend completion at submit, prepare, commit, cancellation, and revocation. Invalidate by Cancel, expiry, Forget, endpoint replacement, credential-invalid, Stop, and shutdown, then complete the old success. Assert no trust promotion or persistence, compensating revocation, bounded 3 invalid-input attempts, no contact for wrong-target/expired input, and a blocked receiver on failed compensation. Include:

```python
from airplay_linux.trust.coordinator import fence_matches
from airplay_linux.trust.fences import TrustFence

def test_changed_trust_generation_rejects_active_time_window(challenge):
    fence = TrustFence(receiver_id=challenge.receiver_id,
                       endpoint_fingerprint=challenge.endpoint_fingerprint,
                       endpoint_generation=challenge.endpoint_generation,
                       session_generation=challenge.session_generation,
                       trust_generation=challenge.trust_generation + 1)
    assert not fence_matches(challenge, fence, challenge.created_at + 1)
```

Define the local `challenge` fixture with the Task 1 dataclass, fixed UUIDs and a redacted opaque token; never use a secret-bearing snapshot.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/trust`; expect missing coordinator and keyring adapter.
- [ ] **Step 3: Implement the commit fence.** Reject unsupported transactional flow before prompting: prepare+commit must exist, or the backend's one-shot store must support a verified cancel/compensating revoke path. Validate current fence immediately before backend submission, before commit, and after every await including successful commit. Do not hold an actor/receiver serialization lock across backend I/O; fence invalidation is immediate. If commit completed after invalidation, revoke the exact provisional or committed receiver credential and await confirmation before unblocking. Only a still-valid successful commit yields Trusted and an opaque reference. Use a per-receiver barrier for in-flight operations; Forget invalidates all challenges first, cancels operations, waits for completion/compensation, revokes backend credentials, then clears local opaque metadata. An active stream prevents Forget until Stop has completed. Cancel/expiry keep TrustState blocked until confirmation, even without video resources. Rotation is a new challenge; mark_invalid clears local trust and invalidates the old generation. Input validation distinguishes visible PIN from masked unrestricted password; do not impose four digits on password mode.
- [ ] **Step 4: Verify all failure surfaces.** `PYTHONPATH=src python -m pytest -q tests/trust`; expect no failures. Inject keyring unavailable/denied, invalid input exhaustion, cancellation timeout, revoke failure, and repeated retry. Assert canary PIN/password/token absent from metadata bytes, Journal, exceptions, repr, and safe activity export. Ensure no app file-credential fallback is created.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/trust tests/trust` then `git commit -m "feat(trust): fence keyring commits and revoke stale pairing results"`.

## Task 11: Coordinate target-bound concurrent transport and sole-authority pairing

**Implementer tier:** Capable
**Lane/category:** transport / doubletake-adapter.
**Depends on:** Tasks 1–3, 8–10. Physical verification consumes the already-frozen CapturePort and Task 2 FakeCapture; Task 12 supplies the independent live adapter at assembly.

**Files:**
- Create: `src/airplay_linux/transport/__init__.py`
- Create: `src/airplay_linux/transport/targets.py`
- Create: `src/airplay_linux/transport/doubletake.py`
- Test: `tests/transport/test_doubletake.py`

**Interfaces:**
- Consumes Task 1 TransportPort `update_target`, query_modes/start_stream/stop_stream/set_muted/start_audio/subscribe/shutdown signatures, StreamHandle, ReceiverTarget, OperationContext, CapturePort.verify(PhysicalMonitorSource) -> Awaitable[Result[None]], PairingPort; Task 8 BackendControl and BackendFeatures; Task 9 DaemonRegistry.ensure/restart/shutdown and DaemonNotice; Task 10 PairingCoordinator and TrustFenceRegistry.
- Produces `TargetRegistry.update(target: ReceiverTarget | None, receiver_id: ReceiverId) -> None`, `.require_current(target: ReceiverTarget) -> Result[None]`; `DoubleTakeTransport(control_factory: Callable[[DaemonRecord],BackendControl], registry: DaemonRegistry, capture: CapturePort, credentials: CredentialStore, receivers: ReceiverStore, fences: TrustFenceRegistry, features: BackendFeatures, clock: Clock, scheduler: Scheduler, ids: IdFactory)` implementing both TransportPort and PairingPort. Its sole PairingCoordinator is private; manager receives this same object under both protocols.

- [ ] **Step 1: Write failing adapter tests.** Two receivers connect concurrently, mute and stop affect one exact stream only, device replacement at same address cannot receive old commands, stale target rejection occurs after deferred I/O admission, old daemon notices do not affect current streams, unsupported physical source fails without connect, and a single PairingCoordinator performs one backend pairing. Pin exact-handle cleanup after endpoint change:

```python
from airplay_linux.transport.targets import TargetRegistry
from airplay_linux.testing.builders import target
from airplay_linux.domain.resources import Err

def test_old_endpoint_is_not_a_current_target():
    registry = TargetRegistry()
    old = target(endpoint_generation=1)
    registry.update(target(endpoint_generation=2), old.receiver_id)
    assert isinstance(registry.require_current(old), Err)
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/transport`; expect missing transport modules.
- [ ] **Step 3: Implement.** Require current receiver ID/fingerprint/generation for query/start/pair/mute/audio-retry; bind stream IDs to daemon instance, target, and lease. Mirror additionally requires CapturePort verification and published backend acceptance of the exact connector/portal descriptor. Virtual start requires a typed verified VirtualOutputSource; never accept windows or arbitrary strings. Stop uses the exact tracked stream handle and the owning daemon, even if target generation is obsolete; if the backend cannot distinguish an old stream from a replacement at the same address, return cleanup uncertainty rather than disconnecting a replacement. Backend events are enriched only from exact request/stream correlation; an ambiguous notice cannot be attributed to the currently selected receiver. A target failure releases only its stream. Daemon exit invalidates all its transport handles and emits notices for each receiver; no display manipulation occurs here. Only the manager applies restart budget and reconnect scheduling. Query modes returns None for unsupported/unknown pre-stream information. `start_audio` is independently gated; mute changes known state only on confirmed success. No application credential/pairing calls bypass the sole coordinator.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/transport tests/trust`; expect no failures. Script overlapping challenges for A/B, target-local errors, late old-daemon ready/stopped, helper-independent display tokens, and wrong-stream mute notices.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/transport tests/transport` then `git commit -m "feat(transport): bind concurrent streams and pairing to exact targets"`.

## Task 12: Enforce physical-monitor capture through portal and backend verification

**Implementer tier:** Advanced
**Lane/category:** capture / physical-monitor-selection.
**Depends on:** Tasks 1–3, 7–8.

**Files:**
- Create: `src/airplay_linux/capture/__init__.py`
- Create: `src/airplay_linux/capture/portal.py`
- Create: `src/airplay_linux/capture/physical.py`
- Test: `tests/capture/test_physical_sources.py`
- Test: `tests/capture/test_portal.py`

**Interfaces:**
- Consumes Task 1 CapturePort list_physical/select/verify/release signatures and PhysicalMonitorSource(connector, portal_restore_token, capture_lease_id), Result/Diagnostic; Task 7 ProcessPort for read-only connector probes; Task 8 BackendFeatures.physical_source; Task 2 FakeCapture and fake clock.
- Produces `is_physical_descriptor(source: object, connectors: tuple[str,...]) -> bool`; `PortalClient.select_monitor(connector: str, restore_token: str | None) -> Awaitable[Result[PortalMonitor]]`, `.close() -> Awaitable[None]`; `PortalMonitor(connector: str, source_type: Literal['physical_monitor'], restore_token: str | None, portal_session_id: str)` is adapter-private. `PhysicalCaptureProvider(portal: PortalClient, connectors: Callable[[],Awaitable[tuple[str,...]]], features: BackendFeatures)` implements CapturePort. Connector inventory is separately obtained from verified physical outputs/sysfs and never labels an unknown virtual connector physical.

- [ ] **Step 1: Write failing tests.** Feed a window, helper-created virtual output, unknown connector, valid physical monitor, denied portal, cancelled portal, expired restore token, and a portal response unable to bind the requested connector. Test backend inability without starting a portal session:

```python
from airplay_linux.capture.physical import is_physical_descriptor
from airplay_linux.domain.resources import PhysicalMonitorSource, Size, VirtualOutputSource

def test_virtual_output_is_never_a_mirror_descriptor():
    source = VirtualOutputSource(connector='Virtual-1', kscreen_output_id='8',
                                 size=Size(width=1920, height=1080))
    assert not is_physical_descriptor(source, ('DP-1',))
    assert is_physical_descriptor(PhysicalMonitorSource(connector='DP-1',
                                   portal_restore_token=None), ('DP-1',))
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/capture`; expect missing capture modules.
- [ ] **Step 3: Implement.** Use dbus-next ScreenCast CreateSession/SelectSources/Start with monitor source type only, single source, explicit response correlation and request cancellation. A generic portal monitor selection without reliable connector/type verification is unsupported_source; do not treat a selected window/virtual monitor as physical. Revalidate connector liveness and backend selection support at start. Keep restore tokens out of general persistence/logging, erase on cancellation/expiry, reacquire through portal as needed. Portal denial/cancellation is recoverable and returns the receiver to a selectable state after ordered cleanup. Gate X11 or other compositor providers on proven monitor mapping and backend source acceptance; do not implement window capture as a fallback. Close request/session resources through CapturePort.release on Stop and stale completion; assign a unique capture_lease_id to each acquired portal session. Unacquired list/choice descriptors have no lease; two receivers selecting the same connector get distinct leases and releasing one cannot close the other's session. A stale selected source cannot silently restart capture.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/capture`; expect no failures using a fake D-Bus peer, including delayed response after Cancel and absence of token values in diagnostics.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/capture tests/capture` then `git commit -m "feat(capture): verify physical-only portal monitor sources"`.

## Task 13: Observe KScreen and launch version-checked owned virtual-monitor helpers

**Implementer tier:** Advanced
**Lane/category:** display / helper-and-kscreen.
**Depends on:** Tasks 1–3, 7.

**Files:**
- Create: `src/airplay_linux/display/__init__.py`
- Create: `src/airplay_linux/display/kscreen.py`
- Create: `src/airplay_linux/display/helper.py`
- Create: `tests/fixtures/display/kscreen-baseline.json`
- Create: `tests/fixtures/display/kscreen-two-virtual.json`
- Test: `tests/display/test_kscreen.py`
- Test: `tests/display/test_helper.py`

**Interfaces:**
- Consumes Task 1 Size, LayoutSnapshot, ProcessIdentity, OwnershipRecord, ProcessPort, Clock/Scheduler and Result; Task 2 scripted KScreen/process evidence; Task 7 ownership checks and bounded execution.
- Produces `OutputInfo(output_id: str, connector: str, size: Size, enabled: bool, position: tuple[int,int], scale: float, virtual: bool | None)`; `KScreenObserver.snapshot() -> Awaitable[Result[LayoutSnapshot]]`, `.outputs() -> Awaitable[Result[tuple[OutputInfo,...]]]`, `.wait_for(predicate: Callable[[tuple[OutputInfo,...]],bool], deadline: float) -> Awaitable[Result[tuple[OutputInfo,...]]]`, `.configure(output_id: str, values: Mapping[str,str|int|bool]) -> Awaitable[Result[None]]`, `.subscribe(callback) -> Subscription`.
- Produces `VirtualHelper.probe() -> Awaitable[Result[HelperFeatures]]`, `.launch(owner_uuid: OwnerId, runtime_directory: str, size: Size) -> Awaitable[Result[ProcessIdentity]]`, `.find_owned(owner_uuid, runtime_directory) -> tuple[ProcessIdentity,...]`, `.stop(identity, owner_uuid, runtime_directory, deadline) -> Awaitable[Result[None]]`; `HelperFeatures(version: str, distinct_outputs: bool, capability_fingerprint: str)` only claims capabilities demonstrated by a supported helper profile.

- [ ] **Step 1: Write failing tests.** Parse representative `kscreen-doctor -j` JSON including malformed modes/IDs, disabled outputs, two newly appearing outputs, unavailable size, supported-mode ID selection, and preexisting unrelated virtual output. Verify helper environment carries `AIRPLAY_LINUX_OWNER_UUID` and `AIRPLAY_LINUX_RUNTIME_DIR`; wrong owner/PID-start is never stopped.

```python
import json
from pathlib import Path
from airplay_linux.display.kscreen import parse_outputs

def test_baseline_fixture_has_unique_output_ids():
    raw = json.loads(Path('tests/fixtures/display/kscreen-baseline.json').read_text())
    outputs = parse_outputs(raw)
    assert len({o.output_id for o in outputs}) == len(outputs)
    assert any(o.connector == 'DP-1' for o in outputs)
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/display/test_kscreen.py tests/display/test_helper.py`; expect missing modules. Create baseline fixture with physical DP-1 at 1920x1080 and the second fixture retaining DP-1 plus distinct owned candidate connectors Virtual-1/Virtual-2.
- [ ] **Step 3: Implement.** Confine JSON parsing and `kscreen-doctor` argv construction to KScreen adapter; use supported `output.<id>.enable`, `output.<id>.mode.<reported-mode-id>`, position and scale commands verified by the installed contract, and read back resulting layout. Never assume width×height text is a valid mode ID. Baseline hash uses canonical sorted JSON. Poll asynchronously with injected scheduler or subscribe to change events; no blocking sleep on Qt/actor thread. Helper profile must be based on installed `krfb-virtualmonitor --help`/published documented arguments; absent helper returns dependency_missing with exact command name. Launch with both ownership environment values, one helper per reservation, and exact process-start evidence. Detect distinct-output support conservatively through profile and actual observed identity; failure for a second output leaves existing helpers untouched.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/display/test_kscreen.py tests/display/test_helper.py`; expect no failures. Fake helper launch is exercised through ProcessPort; the live helper is never started in this suite.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/display tests/display/test_kscreen.py tests/display/test_helper.py tests/fixtures/display` then `git commit -m "feat(display): add checked KScreen and owned helper adapters"`.

## Task 14: Reserve and publish serialized KDE virtual-output creation

**Implementer tier:** Capable
**Lane/category:** display / creation-transaction.
**Depends on:** Tasks 1–4, 13.

**Files:**
- Create: `src/airplay_linux/display/creation.py`
- Create: `src/airplay_linux/display/locking.py`
- Test: `tests/display/test_creation_transaction.py`

**Interfaces:**
- Consumes Task 1 OwnershipStore.write/all/delete, OwnershipRecord (five stages and desired state), DisplayHandle and OperationContext; Task 4 durable AtomicFiles; Task 13 `KScreenObserver.snapshot/outputs/wait_for/configure` and `VirtualHelper.launch/find_owned/stop/probe` signatures; Task 2 fake clock, KScreen evidence, deferred helper and failure controls.
- Produces `select_new_output(baseline: tuple[OutputInfo,...], observed: tuple[OutputInfo,...], size: Size) -> Result[OutputInfo]`; `CreationLock.acquire() -> AsyncContextManager[None]`, implemented with an asyncio lock plus an exact runtime provider lock for process reconciliation; `DisplayCreator.create(receiver_id: ReceiverId, endpoint_fingerprint: str, size: Size, context: OperationContext) -> Awaitable[Result[DisplayHandle]]`; constructor takes helper, observer, OwnershipStore, CreationLock, runtime_directory, app_instance_uuid, Clock/Scheduler/IdFactory and safe ActivitySink.
- Produces `CreationProgress(receiver_id: ReceiverId, creation_operation_id: OperationId, owner_uuid: OwnerId, stage: str, desired_state: str, helper: ProcessIdentity | None, connector: str | None, output_id: str | None, handle: DisplayHandle | None)` subscription; progress contains no portal/backend secret. `DisplayCreator.prepare_teardown(receiver_id: ReceiverId, superseded_operation_id: OperationId | None, context: OperationContext) -> Awaitable[Result[tuple[OwnerId,...]]]` synchronously fences admitted creation before its first await, durably marks existing records removing, and does not signal helpers. It uses a per-record journal lock/CAS so final publication cannot overwrite removing intent; it does not wait behind a helper-appearance await under the provider creation lock. Final publication rechecks this gate and desired state. Provider-owned rollback is safe because initial/new-output creation has no stream until publication.

- [ ] **Step 1: Write failing creation tests.** Interleave two receiver create requests and prove the lock spans baseline-to-publication; reserve-before-launch; capture every stage, helper env, distinct output claims, multiple-output ambiguity, mismatched resolution, write failure at each stage, cancellation at each stage, and late helper launch after cancellation. Seed:

```python
from airplay_linux.display.creation import select_new_output
from airplay_linux.display.kscreen import OutputInfo
from airplay_linux.domain.resources import Size, Err

def test_two_matching_new_outputs_are_ambiguous():
    size = Size(width=1920, height=1080)
    candidates = tuple(OutputInfo(output_id=str(i), connector=f'Virtual-{i}',
                       size=size, enabled=True, position=(0, 0), scale=1.0,
                       virtual=True) for i in (7, 8))
    assert isinstance(select_new_output((), candidates, size), Err)
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/display/test_creation_transaction.py`; expect missing creator.
- [ ] **Step 3: Implement the transaction.** Under provider lock verify KDE Plasma 6 Wayland and helper capabilities, snapshot baseline, generate owner UUID, atomically write reserved/active with requested size and complete baseline before spawn. Launch, record PID/start and helper_started, observe exactly one new output absent from baseline matching reservation and size; persist output_observed with connector/output/observed size. Set usable mode/enabled/position using reported KScreen mode ID, record each completed configuration step/last-applied values durably, read back configuration, then write published/active. Return only after final fsync. No stream starts before published. Detect cancellation/removing intent between every await, never publish active over a durable removing record. On any write/configuration/observation failure, attempt durable removing intent, stop the exact just-launched helper (including in-memory PID before persistence), wait for disappearance, and retain CleanupRequired evidence if confirmation or intent publication fails. A second helper that cannot yield a distinct output fails only the new reservation.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/display/test_creation_transaction.py`; expect no failures, including launch-before-PID-persistence failure and two concurrent callers. Assert no published handle for unverified output_observed state and no deletion of an unrelated connector.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/display/creation.py src/airplay_linux/display/locking.py tests/display/test_creation_transaction.py` then `git commit -m "feat(display): reserve and publish serialized owned virtual outputs"`.

## Task 15: Reconcile every ownership stage and remove outputs conservatively

**Implementer tier:** Capable
**Lane/category:** display / recovery-and-removal.
**Depends on:** Tasks 1–4, 7, 13–14.

**Files:**
- Create: `src/airplay_linux/display/recovery.py`
- Create: `src/airplay_linux/display/removal.py`
- Create: `src/airplay_linux/display/kde.py`
- Test: `tests/display/test_recovery_matrix.py`
- Test: `tests/display/test_removal_and_layout.py`

**Interfaces:**
- Consumes Task 1 DisplayPort create/mark_removing/remove/reconcile/approve_orphan/subscribe signatures, RecoveryItem, OwnershipRecord, DisplayEvent; Task 13 KScreenObserver and VirtualHelper; Task 14 DisplayCreator.create and CreationLock.acquire; Task 4 OwnershipStore atomic semantics; Task 7 exact process ownership.
- Produces `DisplayRecovery.reconcile() -> Awaitable[tuple[RecoveryItem,...]]`; `DisplayRemover.mark_removing(owner_uuid: OwnerId) -> Awaitable[Result[None]]`, `.remove(owner_uuid: OwnerId, context: OperationContext) -> Awaitable[Result[None]]`, `.approve_orphan(owner_uuid: OwnerId, confirmed: bool, context: OperationContext) -> Awaitable[Result[None]]`; `KdeVirtualDisplayProvider(creator: DisplayCreator, recovery: DisplayRecovery, remover: DisplayRemover, observer: KScreenObserver, helper: VirtualHelper)` implements DisplayPort and delegates prepare_teardown to creator. It translates CreationProgress into typed DisplayEvent and emits each durable stage, including the initial reservation. Removal requires caller-confirmed transport release; mark_removing/prepare_teardown alone do not touch the helper/output.
- Produces `eligible_restore(baseline: LayoutSnapshot, last_applied: Mapping[str,Mapping[str,str|int|bool]], current: LayoutSnapshot) -> Mapping[str,Mapping[str,str|int|bool]]`: restore only provider-touched properties still equal to last-applied values, excluding the deleted output and unrelated properties.

- [ ] **Step 1: Write failing table tests.** Cross all five stages × desired active/removing × exact helper alive/dead/PID-reused/inaccessible × matching output absent/exact/misconfigured/ambiguous. Include owner-env launch-to-PID crash, no record/no helper, helper/no output, output/no helper, and output/no record. Test user-modified layout preservation:

```python
from airplay_linux.display.removal import eligible_restore
from airplay_linux.domain.records import LayoutSnapshot

def test_user_change_is_not_restored():
    before = LayoutSnapshot(outputs={'DP-1': {'x': 0, 'scale': 1}}, baseline_hash='a')
    current = LayoutSnapshot(outputs={'DP-1': {'x': 900, 'scale': 1}}, baseline_hash='b')
    applied = {'DP-1': {'x': 1920}}
    assert eligible_restore(before, applied, current) == {}
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/display/test_recovery_matrix.py tests/display/test_removal_and_layout.py`; expect missing recovery/removal/provider.
- [ ] **Step 3: Implement the recovery decision table.** Reserved with no evidence is discarded only after owner/process checks; owner-env matching live helper is first persisted as helper_started. Helper_started with no output waits 5 seconds, then marks cleanup and stops only the exact helper. Helper_started/output_observed with exact helper/output repair requested configuration under creation lock, verify and republish before yielding recovering. Output_observed without exact ownership is orphan/cleanup uncertainty, never usable. Published/active with exact configured output and helper yields recovering with a verified handle. Every removing-intent record resumes removal and never reconnects, irrespective of stage. A matching output with dead helper becomes an orphan candidate requiring explicit confirmation; no-record output is unrelated and excluded from automatic cleanup. Exact owned helper without a usable record/output is stopped only after complete owner evidence. Ambiguous or inaccessible ownership is CleanupRequired. Scan process owner environment for the launch window and persist exact start evidence; never infer ownership from command name or newest connector.
- [ ] **Step 4: Implement removal and verify.** Persist removing intent before signals; stop exact owned helper, await exact connector/output disappearance, then restore only eligible properties and confirm readback before deleting record. Helper/KScreen timeout retains record and CleanupRequired; Keep never means released. Orphan removal requires `confirmed=True` plus exact persisted output identity revalidated at action time; no helper means only an explicit, documented KScreen removal operation may run, otherwise give manual cleanup instructions rather than inventing a command. Helper exit/output disappearance emits DisplayEvent with persisted owner/helper identity and never claims a usable source. Run `PYTHONPATH=src python -m pytest -q tests/display`; expect no failures, including refusal to affect unknown outputs and two-receiver layout isolation.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/display/recovery.py src/airplay_linux/display/removal.py src/airplay_linux/display/kde.py tests/display/test_recovery_matrix.py tests/display/test_removal_and_layout.py` then `git commit -m "feat(display): reconcile every reservation stage and confirm owned cleanup"`.

## Task 16: Resolve receiver modes with reliable evidence and identity continuity

**Implementer tier:** Standard
**Lane/category:** policy / resolution.
**Depends on:** Tasks 1–2, 4–5.

**Files:**
- Create: `src/airplay_linux/policy/__init__.py`
- Create: `src/airplay_linux/policy/resolution.py`
- Test: `tests/policy/test_resolution.py`

**Interfaces:**
- Consumes Task 1 ResolutionDecision(size, source stored/discovery/user, evidence), ResolutionEvidence(origin, reliable, receiver_id, endpoint_fingerprint), ReceiverCapabilities(modes, reliable, evidence), ReceiverRecordSet, SavedReceiver; Task 4 `PreferenceAccess.for_record_set(record_set, saved) -> SavedReceiver | None`; Task 5 identity continuity classification.
- Produces `ResolutionRequired(choices: tuple[Size,...], remembered_rejected: bool)`; `resolve_resolution(record_set: ReceiverRecordSet, saved: SavedReceiver | None, discovered: ReceiverCapabilities | None, queried: ReceiverCapabilities | None, user: Size | None = None) -> ResolutionDecision | ResolutionRequired`; `remember_after_success(record_set, decision: ResolutionDecision, observed: Size | None) -> ResolutionDecision | None`; `accepted_mode_mismatch(decision, accepted: Size, reliable: bool) -> bool`.

- [ ] **Step 1: Write failing precedence tests.** Same compatible stable identity stored mode wins; otherwise reliable discovery then reliable pre-stream query; unknown/unreliable information requires explicit picker; explicit positive dimensions accepted; ambiguous/replacement identity gets no saved value. Include no TV-category inference:

```python
from airplay_linux.policy.resolution import accepted_mode_mismatch
from airplay_linux.domain.resources import ResolutionDecision, ResolutionEvidence, Size
from airplay_linux.domain.identity import ReceiverId

def test_unreliable_accepted_size_cannot_reconfigure():
    decision = ResolutionDecision(size=Size(width=1920, height=1080), source='user',
        evidence=ResolutionEvidence(origin='picker', reliable=True,
                 receiver_id=ReceiverId('airplay-device:a'), endpoint_fingerprint=None))
    assert not accepted_mode_mismatch(decision, Size(width=3840, height=2160), False)
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/policy/test_resolution.py`; expect missing policy.
- [ ] **Step 3: Implement.** Filter remembered data with continuity verification; do not equate label-hash equality with replacement trust. Reliable single preferred advertised/query mode yields discovery source with exact evidence; multiple equally reliable supported modes require explicit selection rather than guessing a panel preference. Picker choices may be reported modes or explicit positive custom dimensions validated by Size; no preselected generic 1080p fallback. Remember only after confirmed setup/verification, and never for ambiguous IDs. A reliable accepted-size difference returns mismatch; policy never changes a live output itself.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/policy/test_resolution.py`; expect no failures including endpoint replacement in both discovery arrival orders and failed setup not writing a preference.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/policy tests/policy/test_resolution.py` then `git commit -m "feat(policy): require reliable or explicit virtual display resolution"`.

## Task 17: Probe environment, audio, and per-mode capability gates

**Implementer tier:** Advanced
**Lane/category:** policy / startup-capabilities.
**Depends on:** Tasks 1–3, 7–8, 12–13.

**Files:**
- Create: `src/airplay_linux/policy/capabilities.py`
- Create: `src/airplay_linux/policy/audio.py`
- Test: `tests/policy/test_capabilities.py`
- Test: `tests/policy/test_audio_probe.py`

**Interfaces:**
- Consumes Task 1 CapabilityReport(environment, discovery, mirror, virtual_display, audio, remembered_pairing, additional_virtual_outputs, diagnostics), AudioProbe.check() -> Awaitable[Result[None]], AudioPolicy(enabled=True); Task 8 probe_backend -> BackendFeatures; Task 12 PhysicalCaptureProvider verification capabilities; Task 13 VirtualHelper.probe -> HelperFeatures; Task 7 ProcessPort; Task 3 diagnostic catalog.
- Produces `EnvironmentProbe.check() -> Awaitable[CapabilityReport]`, `.refresh() -> Awaitable[CapabilityReport]`, injected environment mapping, command finder, D-Bus capability client, backend/helper probes; `PipeWirePortalAudioProbe` implements AudioProbe. `audio_start_policy(policy: AudioPolicy, probe: Result[None], explicit_video_only: bool) -> Literal['start_audio','decision_required','video_only']`; `can_start(report, mode: Mode, trust: TrustState) -> Result[None]` gates only the attempted capability.

- [ ] **Step 1: Write failing matrix tests.** KDE Plasma 6 Wayland with complete compatible dependencies enables modes; missing krfb disables Virtual alone; missing kscreen, transport incompatibility, unavailable discovery, PipeWire/portal denial, unusable keyring, second-output limitation, X11, GNOME Wayland, and unknown desktop are separately represented. Audio policy seed:

```python
from airplay_linux.policy.audio import audio_start_policy
from airplay_linux.domain.resources import AudioPolicy, Err
from airplay_linux.diagnostics.catalog import diagnostic
from airplay_linux.domain.resources import ErrorCode

def test_audio_failure_requires_explicit_choice():
    failure = Err(error=diagnostic(ErrorCode.audio_unavailable))
    assert audio_start_policy(AudioPolicy(), failure, False) == 'decision_required'
    assert audio_start_policy(AudioPolicy(), failure, True) == 'video_only'
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/policy/test_capabilities.py tests/policy/test_audio_probe.py`; expect missing capability/audio policy.
- [ ] **Step 3: Implement.** Probe PySide6 runtime, selected zeroconf availability, doubletake/ctl feature profile, kscreen-doctor, krfb-virtualmonitor, XDG_SESSION_TYPE/XDG_CURRENT_DESKTOP and Plasma version, PipeWire connection and portal interfaces, Secret Service/keyring availability and known concurrent-output support. Distinguish introspection capability from an actual user permission grant; normal start obtains permission and can still enter AudioDecisionRequired. Read-only startup probes never create a monitor or prompt for PIN/audio permission. Missing PySide6 is reported by bootstrap stderr before importing UI. Report exact missing executable/interface and retry-after-fix hint. KDE v1 has a concrete provider path; another environment is enabled only if its capture/display provider proves the requested operation. Do not disable discovery/saved-device inspection because transport or helper is unavailable. Second-output inability blocks only an additional Virtual creation. Audio probe is separately repeatable without a stream; explicitly disabled audio skips probing and is logged as a session choice.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/policy`; expect no failures and a current-environment fixture with absent krfb that keeps shell/discovery usable.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/policy/capabilities.py src/airplay_linux/policy/audio.py tests/policy/test_capabilities.py tests/policy/test_audio_probe.py` then `git commit -m "feat(policy): gate environment capabilities and explicit audio fallback"`.

## Task 18: Serialize receiver actors and fence events by their actual resource

**Implementer tier:** Capable
**Lane/category:** session / actor-and-effect-engine.
**Depends on:** Tasks 1–3, 10.

**Files:**
- Create: `src/airplay_linux/session/__init__.py`
- Create: `src/airplay_linux/session/engine.py`
- Create: `src/airplay_linux/session/fencing.py`
- Create: `src/airplay_linux/session/phase.py`
- Create: `src/airplay_linux/session/testing.py`
- Test: `tests/session/test_engine.py`
- Test: `tests/session/test_fencing.py`

**Interfaces:**
- Consumes Task 1 ManagerPort, Command/ResourceEvent unions, SessionSnapshot, all resource ports, Clock/Scheduler/IdFactory, Result, ActivitySink; Task 2 fakes and Journal; Task 3 diagnostics; Task 10 `TrustFenceRegistry.set(TrustFence) -> None` and `.get(receiver_id) -> TrustFence | None`.
- Produces `Route` enum mirror/virtual/trust/cleanup/resolution/reconnect/audio/recovery; `Handler = Callable[[SessionContext, Command | ResourceEvent], bool]`, synchronous and nonblocking. `ManagerDeps(transport: TransportPort, pairing: PairingPort, display: DisplayPort, capture: CapturePort, discovery: DiscoveryPort, audio_probe: AudioProbe, sessions: SessionStore, receivers: ReceiverStore, ownerships: OwnershipStore, fences: TrustFenceRegistry, clock: Clock, scheduler: Scheduler, ids: IdFactory, activity: ActivitySink, capabilities: Callable[[],CapabilityReport], restart_daemon: Callable[[],Awaitable[Result[DaemonRecord]]])`.
- Produces `SessionManager(deps: ManagerDeps, handlers: Mapping[Route,Handler])` implementing dispatch/ingest/snapshot/subscribe/shutdown; `.drain() -> Awaitable[None]` yields until queued state events and completed effects are applied, without waiting for deliberately unresolved backend work; `.install(snapshot: SessionSnapshot) -> None` is recovery-only, before commands are accepted; `.pending_resources(receiver_id: ReceiverId) -> tuple[StreamHandle | DisplayHandle | PhysicalMonitorSource,...]`; `.set_shutdown_hook(callback: Callable[[],Awaitable[Result[None]]]) -> None`.
- Produces `SessionContext.state: SessionSnapshot`, `.deps: ManagerDeps`, `.apply(**changes) -> SessionSnapshot` which computes `derive_phase(state: SessionSnapshot) -> SessionPhase` from independent resources plus IntentStage, never accepts a contradictory manual phase; `.operation() -> OperationContext` allocating a fresh operation ID, `.supersede(reason: Literal['start','cancel','stop','switch','reconfigure','shutdown']) -> OperationContext`, `.new_transport_lease(context: OperationContext) -> OperationContext`, `.invalidate_trust() -> None`, `.cancel_timers() -> None`, `.cancel_audio() -> None` clearing the audio-operation token and local callbacks only, `.launch(route: Route, name: str, context: OperationContext, work: Callable[[],Awaitable[Result[object]]], acquisition: bool = False) -> None`, `.enqueue(event: ResourceEvent) -> None`, `.schedule(kind: str, when: float, context: OperationContext) -> None`, `.outstanding_acquisitions() -> int`. Effects return a tagged EffectEvent routed by `kind = '<route>:<name>'`; state mutation occurs only in handlers during actor dispatch. Cleanup routes take precedence for superseded acquisitions. Derive phase precedence is unresolved display cleanup, active stopping/reconfiguration, unresolved trust cleanup, pending resolution/audio decision, recovery, pairing, display creation, connecting/reconnecting/streaming, failed, then online Available/offline Offline; badges always preserve every resource.
- Produces `accept_transport(state: SessionSnapshot, event: TransportEvent) -> bool`, `accept_display(state: SessionSnapshot, event: DisplayEvent) -> bool`, `accept_challenge(state: SessionSnapshot, challenge: ChallengeView) -> bool`, `accept_cleanup(exact_owned_handles: tuple[StreamHandle | DisplayHandle | PhysicalMonitorSource,...], event: ResourceEvent) -> bool`. `SessionRig(handlers_factory: Callable[[ManagerDeps],Mapping[Route,Handler]] | None = None)` builds Task 2 fakes, fences and manager, with `.manager`, `.deps`, `.journal`, `.clock`, `.scheduler`, `.transport`, `.display`, `.pairing`, `.discovery`, `.audio`, `.sessions`, `.receiver_id: ReceiverId` (the default target ID), and `.settle() -> Awaitable[None]` using manager.drain. Test-local handlers can be supplied without requiring later tasks. SessionRig defaults to complete fake capabilities, available audio, a trusted backend ref and no reliable query_modes value; each test opts into pairing/failure explicitly. Engine always maintains discovered online targets, even when reconnect policy is not installed.

- [ ] **Step 1: Write failing engine tests.** Use probe handlers to hold one receiver's effect indefinitely while accepting Stop for it and a command for another receiver. Verify FIFO state changes, distinct operation IDs, allowed generation increments only, retries/challenge submission/mute preserving session generation, separate audio IDs, and display events surviving endpoint/daemon changes. Event test:

```python
from dataclasses import replace
from airplay_linux.session.fencing import accept_transport
from airplay_linux.testing.builders import context, target

def test_stale_transport_lease_cannot_update_current_state(streaming_state, ready_event):
    stale = replace(ready_event, context=context(generation=streaming_state.session_generation,
                    endpoint_generation=streaming_state.target.endpoint_generation, lease=999))
    assert not accept_transport(streaming_state, stale)
```

Define local streaming_state/ready_event fixtures from Task 1 DTOs and Task 2 builders; no future application fixtures are assumed.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_engine.py tests/session/test_fencing.py`; expect missing engine/fencing.
- [ ] **Step 3: Implement actors and the effect ledger.** One queue per receiver drains without awaiting OS/network work; effects run separately and enqueue typed completion. Commands allocate operation UUID even if rejected; acceptance validation reports a diagnostic, not an exception. Do not increment a shared generation on every command. `supersede` updates session generation, cancels timers and affected operations, publishes trust fence synchronously, and retains exact owned-resource cleanup entries. A request cancellation is advisory: unresolved acquisitions remain in the ledger until confirmed absent or released. Stale successful stream/display/portal acquisition routes to cleanup, never discarded without release. Do not declare teardown complete or start a queued mode while an acquisition may still create a resource; a deadline yields cleanup uncertainty. Avoid asyncio task cancellation losing a successful backend result: observe the owned completion future and pass it to cleanup. Event acceptance checks only applicable resource tokens; compare a stream notice's operation to StreamHandle.start_operation_id rather than the most recent command receipt, while audio/mute results use their own pending operation token. A new mute/audio command cannot make current stream notices stale. Stopped events may match exact old stream handles regardless of intent generation. Bootstrap reservation progress is correlated to its registered creation operation once; subsequent display events use owner/helper/output evidence only. Unknown route commands are rejected with unsupported_operation without mutating resources.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/session/test_engine.py tests/session/test_fencing.py`; expect no failures. Test adversarial ordering of ready/stop, old daemon exit/new ready, stale audio recovery, late output publication after Cancel, and old challenge completion. Demonstrate Stop is processed before a blocked effect resolves.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session tests/session/test_engine.py tests/session/test_fencing.py` then `git commit -m "feat(session): serialize actors and fence resource-specific effects"`.

## Task 19: Implement Mirror, pairing, mode confirmation, and universal ordered Stop

**Implementer tier:** Capable
**Lane/category:** session / mirror-trust-cleanup.
**Depends on:** Tasks 1–3, 10–12, 17–18; use Task 2 FakeDisplay rather than requiring a compositor.

**Files:**
- Create: `src/airplay_linux/session/mirror.py`
- Create: `src/airplay_linux/session/pairing.py`
- Create: `src/airplay_linux/session/cleanup.py`
- Test: `tests/session/test_mirror_and_trust.py`
- Test: `tests/session/test_stop_matrix.py`

**Interfaces:**
- Consumes Task 18 Handler/Route/SessionContext methods and ManagerDeps, SessionRig; Task 1 Start/Stop/Cancel/Pair/SubmitPairing/Forget/RetryCleanup/RetryRevocation/KeepCleanup/ConfirmSwitch commands, SessionSnapshot, TransportPort start_stream/stop_stream, PairingPort begin/submit/cancel/forget/retry_revocation, CapturePort select/verify/release, DisplayPort mark_removing/remove and prepare_teardown, AudioProbe.check; Task 17 `can_start(report, mode, trust) -> Result[None]`, `audio_start_policy(policy, probe, explicit_video_only) -> Literal[...]`.
- Produces `build_lifecycle_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` assigning mirror/trust/cleanup routes; `cleanup_complete(state: SessionSnapshot, outstanding_acquisitions: int, pending_handles: tuple[StreamHandle | DisplayHandle | PhysicalMonitorSource,...]) -> bool`. Cleanup completes only when transport/audio/display release is confirmed and no possible acquisition remains; trust uncertainty remains independently blocking.
- Uses DisplayPort extension frozen in Task 1: `prepare_teardown(receiver_id: ReceiverId, superseded_operation_id: OperationId | None, context: OperationContext) -> Awaitable[Result[tuple[OwnerId,...]]]` fences a pending creation and durably marks all that receiver's reservations removing without stopping helpers. This covers Stop before a creator has returned its owner UUID.

- [ ] **Step 1: Write failing lifecycle tests using SessionRig(build_lifecycle_handlers).** Mirror needs an online current target, typed physical source and explicit audio policy; missing capability is command-local. Include Pairing with no output, cancellation/expiry/invalid-input exhaustion, unchanged generation on valid SubmitPairing, Forget while pairing, refused active-stream Forget, trust_cleanup lockout, and stale challenge rejection. Parameterize Stop across every TransportState/DisplayState/TrustState reachable combination with resource-backed fixtures, repeated Stop, cancellation during portal selection/connecting/pairing/create, and a late acquisition. Core test:

```python
import pytest
from airplay_linux.session.testing import SessionRig
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.domain.messages import Start, Stop
from airplay_linux.domain.resources import Mode, StartRequest
from airplay_linux.testing.builders import physical, target

@pytest.mark.asyncio
async def test_mirror_does_not_create_a_display():
    rig = SessionRig(build_lifecycle_handlers)
    rig.discovery.add_target(target())
    await rig.settle()
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id,
        request=StartRequest(mode=Mode.MIRROR, source=physical())))
    await rig.settle()
    assert 'display.create' not in rig.journal.names()
    await rig.manager.dispatch(Stop(receiver_id=target().receiver_id))
    await rig.settle()
```

The engine's built-in discovery bookkeeping and Task 2 FakeDiscovery.add_target supply the online/current target for this test; no reconnect policy is needed.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_mirror_and_trust.py tests/session/test_stop_matrix.py`; expect missing lifecycle handlers.
- [ ] **Step 3: Implement start and trust flow.** Reject a second active mode, online-stale target, unsupported physical source, cleanup/trust-blocked receiver, and unconfirmed switch. Start supersedes the idle intent once, selects/verifies requested physical source, probes audio, starts only with accepted policy, and enters Pairing only on receiver request/backend challenge. Track ChallengeView while the coordinator retains secret correlation. Handle credential_invalid by synchronously advancing trust generation/clearing the active challenge and opaque ref, calling PairingPort.mark_invalid, preserving any valid owned display, and entering Pairing only when the current backend requests it; failed invalidation remains trust-blocked. Submit active challenge without session-generation increment; update trust only on a still-current Trusted result. Explicit Pair while idle can pair without starting video. Cancel/expiry with no output returns Available/Offline only after backend cancellation; failed cancellation leaves TrustCleanupRequired. Forget invalidates trust fences before backend await, waits/compensates, then clears metadata reference only after confirmed removal. Remove saved metadata can delete non-secret preferences while retaining an in-memory/persisted blocked-trust tombstone until revocation is resolved.
- [ ] **Step 4: Implement ordered cleanup and verify.** On first Stop/cancel/switch increment the intent generation, cancel retries, invalidate affected challenge/lease, and call prepare_teardown before transport stop. If publishing removing fails, record CleanupRequired and do not stop transport/helper under an unrecorded intent. Confirm exact stream release, stop audio controls, release the exact physical portal source if any, then remove all owned/pending outputs and restore eligible layout. Pairing cancellation can run concurrently with resource cleanup but trust remains blocked until confirmed. A stop timeout exposes exact pending resources; repeated Stop retries the same transaction without a new output or repeated generation increment. ConfirmSwitch(confirmed=False) preserves session; confirmed=True queues StartRequest and starts it only after every resource/acquisition is released and trust is safe. No output exists in normal Mirror flow. `PYTHONPATH=src python -m pytest -q tests/session/test_mirror_and_trust.py tests/session/test_stop_matrix.py`; expect no failures and Journal ordering `display.prepare_teardown < transport.stop < audio.stop_control < display.remove` when those resources exist.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session/mirror.py src/airplay_linux/session/pairing.py src/airplay_linux/session/cleanup.py tests/session/test_mirror_and_trust.py tests/session/test_stop_matrix.py` then `git commit -m "feat(session): add mirror pairing and ordered idempotent cleanup"`.

## Task 20: Implement Virtual display, retained-output pairing, and resolution recreation

**Implementer tier:** Capable
**Lane/category:** session / virtual-transaction.
**Depends on:** Tasks 14–19.

**Files:**
- Create: `src/airplay_linux/session/virtual.py`
- Create: `tests/session/virtual_scenarios.py`
- Test: `tests/session/test_virtual_and_resolution.py`

**Interfaces:**
- Consumes Task 18 Handler, SessionContext, SessionRig; Task 19 `build_lifecycle_handlers(deps) -> Mapping[Route,Handler]` and its cleanup route; Task 16 resolve_resolution -> ResolutionDecision | ResolutionRequired, remember_after_success and accepted_mode_mismatch; Task 1 DisplayPort.create/prepare_teardown/mark_removing/remove and DisplayEvent, query_modes/start_stream/stop_stream, ChooseResolution/ChangeResolution/RecreateAccepted commands; Task 15 KdeVirtualDisplayProvider.
- Produces `build_virtual_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` assigning virtual/resolution routes; `virtual_handlers(deps)` in the test helper merges lifecycle and virtual mappings while rejecting duplicate routes. `Resolution mismatch` is represented by `last_error.code=resolution_mismatch`, `accepted_resolution`, stopped transport, and an owned display, not an invented SessionPhase enum.

- [ ] **Step 1: Write failing tests.** Pin unknown mode picker before reservation, remembered mode continuity, pre-output pairing, post-output receiver challenge, retained-output pairing Cancel, provider failure during create, distinct outputs for A/B, and reconfiguration order. Assert transport-only failure does not remove a display; helper failure does stop invalid transport and remove/mark cleanup. Test unknown resolution:

```python
import pytest
from airplay_linux.session.testing import SessionRig
from virtual_scenarios import virtual_handlers
from airplay_linux.domain.messages import Start
from airplay_linux.domain.resources import Mode, StartRequest, SessionPhase
from airplay_linux.testing.builders import target

@pytest.mark.asyncio
async def test_unknown_mode_precedes_any_output():
    rig = SessionRig(virtual_handlers)
    rig.discovery.add_target(target())
    await rig.settle()
    await rig.manager.dispatch(Start(receiver_id=target().receiver_id,
                                    request=StartRequest(mode=Mode.VIRTUAL)))
    await rig.settle()
    assert rig.manager.snapshot(target().receiver_id).phase is SessionPhase.ResolutionRequired
    assert 'display.create' not in rig.journal.names()
```

Use pytest's default prepend import mode so the declared local helper is importable from this test directory.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_virtual_and_resolution.py`; expect missing virtual handlers.
- [ ] **Step 3: Implement initial setup.** Resolve decision before create; if Pair is requested by backend before create, resume creation only after trusted result; if requested during connection preserve Owned output while prompting. Subscribe to reservation progress to retain exact owner identity and stage; create result becomes Owned only after verified published handle. When stream ready, remember chosen/verified resolution for stable compatible identity. Failures retain exact output uncertainty; no phase reports Available until cleanup confirms absence. A display helper exit or disappearance is validated by owner/helper identity, sets DisplayState.Failed, marks removing intent, stops the invalid-source stream, then uses ordered cleanup; a stale helper from an earlier owner cannot affect the current output.
- [ ] **Step 4: Implement recreation and verify.** ChangeResolution/RecreateAccepted supersedes generation once, invalidates lease/challenges, durably marks old output removing before stop, waits exact stream release, removes/confirms old output, then reserves/configures/publishes new output and starts with a new operation/lease. Do not own old and new active alternatives concurrently. Cancel during any recreation stage continues cleanup and never resurrects old stream/output. Reliable post-setup accepted mismatch stops stream only, retains output with accepted_resolution and explicit Recreate/Stop; never changes mode silently. Failure in recreation preserves exact display/transport/error states. `PYTHONPATH=src python -m pytest -q tests/session/test_virtual_and_resolution.py tests/session/test_stop_matrix.py`; expect no failures including two simultaneous receivers, Cancel at every await, and mismatched mode after a previous successful setup.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session/virtual.py tests/session/virtual_scenarios.py tests/session/test_virtual_and_resolution.py` then `git commit -m "feat(session): transact virtual outputs pairing and resolution recreation"`.

## Task 21: Reconnect current targets and coordinate shared daemon recovery

**Implementer tier:** Capable
**Lane/category:** session / reconnect-and-daemon-recovery.
**Depends on:** Tasks 9, 11, 18–20.

**Files:**
- Create: `src/airplay_linux/session/reconnect.py`
- Test: `tests/session/test_reconnect.py`
- Test: `tests/session/test_daemon_recovery.py`

**Interfaces:**
- Consumes Task 18 ManagerDeps.restart_daemon() -> Awaitable[Result[DaemonRecord]], clock/scheduler and SessionContext operations/lease/timers; Task 1 DiscoveryEvent, TransportEvent, TimerEvent, current ReceiverTarget, Retry/Stop commands; Task 11 TransportPort.update_target/start_stream/stop_stream and daemon notices; Task 19 cleanup/trust routes and Task 20 virtual route.
- Produces `build_reconnect_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` assigning reconnect; `network_delay(attempt: int) -> float`, `RestartBudget(clock: Clock, limit: int = 3, window: float = 60).claim() -> bool`; one shared `DaemonRecovery` per manager with `.request_restart() -> Awaitable[Result[DaemonRecord]]` coalescing simultaneous receiver requests.

- [ ] **Step 1: Write failing tests.** Assert delays 1,2,4,8,16,30,30 with injected scheduler, indefinite offline state, current endpoint refresh before each attempt, same-intent generation retained, fresh operation/lease, Stop timer cancellation, old ready rejection, offline idle Start refresh without stale send, and trust invalidation only when identity continuity is lost. Pin cap:

```python
from airplay_linux.session.reconnect import network_delay

def test_network_backoff_is_capped_without_becoming_terminal():
    assert [network_delay(i) for i in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_reconnect.py tests/session/test_daemon_recovery.py`; expect missing reconnect implementation.
- [ ] **Step 3: Implement endpoint and network recovery.** Discovery removal updates current target to None, invalidates transport lease/endpoint commands, and makes active transport Reconnecting while preserving a valid display and intent. An idle receiver becomes Offline and reappearance does not create user intent. Same stable device ID endpoint updates preserve allowed saved trust but invalidate outstanding challenge endpoint fences; fallback replacement discards trust/resolution inheritance and requires explicit pairing. Retry waits for a fresh record if absent; no start/pair/mute is sent to last-known endpoint. Network attempts remain Reconnecting indefinitely at capped backoff; no local-retry counter is reused as network exhaustion. New connection creates a lease while keeping session generation. A failed target does not alter another target or output.
- [ ] **Step 4: Implement daemon failure classification and verify.** Daemon exit affects streams from that exact instance only; preserve intent, source and independent output state, coalesce one registry reconciliation/restart within the 3-in-60-second budget, and reconnect each receiver independently against its current target. On local budget exhaustion show Failed plus retained intent/output and explicit Retry; do not remove a valid display. Restart failure ownership_mismatch is CleanupRequired, not repeated launches. Retry from explicit Failed retries local recovery without silently replacing display. Run `PYTHONPATH=src python -m pytest -q tests/session/test_reconnect.py tests/session/test_daemon_recovery.py`; expect no failures for one network loss, one target rejection, and shared daemon failure with both receivers represented.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session/reconnect.py tests/session/test_reconnect.py tests/session/test_daemon_recovery.py` then `git commit -m "feat(session): reconnect current targets and bound shared daemon recovery"`.

## Task 22: Preserve video through runtime audio loss and implement local mute

**Implementer tier:** Advanced
**Lane/category:** session / audio-lifecycle.
**Depends on:** Tasks 11, 17–21.

**Files:**
- Create: `src/airplay_linux/session/audio.py`
- Test: `tests/session/test_audio_lifecycle.py`

**Interfaces:**
- Consumes Task 1 AudioState/AudioPolicy, RetryAudio/StartVideoOnly/SetMuted/Cancel commands, TransportPort.start_audio(handle, context), set_muted(handle, muted, context) and typed audio events; Task 17 AudioProbe.check/audio_start_policy; Task 18 SessionContext.launch, operation, state.audio_operation_id and resource-fencing functions; Task 19 startup/cleanup continuation; Task 21 reconnect leases.
- Produces `build_audio_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` assigning audio. It consumes Task 18 `SessionContext.cancel_audio() -> None` for receiver-local control shutdown after stream release; this produces the safe Journal entry `audio.stop_control` and never creates a second transport stream or changes display ownership.

- [ ] **Step 1: Write failing tests.** Enabled-by-default startup, explicit disabled choice, denied/unavailable startup before any ready video, RetryAudio, explicit StartVideoOnly, Cancel, runtime failure with video retained, successful/failed retry, stale audio retry completion, mute failure preserving prior state, and A/B isolation. Add a direct resource assertion:

```python
from dataclasses import replace
from airplay_linux.domain.resources import AudioState, TransportState

def test_audio_unavailable_projection_retains_stream(streaming_snapshot):
    updated = replace(streaming_snapshot, audio=AudioState.Unavailable)
    assert updated.transport is TransportState.Streaming
    assert updated.stream == streaming_snapshot.stream
    assert updated.display_handle == streaming_snapshot.display_handle
```

The surrounding handler tests must deliver an actual audio_failed event and check calls, so the dataclass assertion alone is insufficient.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_audio_lifecycle.py`; expect missing audio handlers.
- [ ] **Step 3: Implement decisions and retry.** Probe before normal start; a pre-ready backend audio_start_failed holds AudioDecisionRequired and prevents promotion to Mirroring/VirtualDisplay until the user chooses. If a partial backend stream exists, retain/stop it explicitly according to supported control semantics; no implicit video-only continuation. RetryAudio gets a fresh audio operation ID, retains session generation/display owner/transport lease, and uses start_audio for an existing handle or reprobes the not-yet-started intent. Success restores desired Enabled/Muted without interrupting established video; failure retains Unavailable/video. If the backend cannot independently retry audio, report unsupported_operation while preserving video and Stop; never emulate audio retry by tearing down live video. StartVideoOnly is explicit, updates audio policy and logs explicit_video_only; if replacing an unready partial stream, confirm its release before a new lease starts. Runtime audio loss keeps transport Streaming and output Owned, records typed diagnostic and disables mute. Stop invalidates audio operation, cancels audio controller, then proceeds with display cleanup.
- [ ] **Step 4: Implement mute and verify.** Allow mute only for Streaming with audio Enabled/Muted and current exact handle/target. On success update desired mute and confirmed AudioState; on failure keep prior values. Backend audio_recovered events must match stream/lease and applicable audio operation; stale retries cannot revive stopped audio. Per-session mute is never written into SavedReceiver preferences or inherited by a fresh start. `PYTHONPATH=src python -m pytest -q tests/session/test_audio_lifecycle.py`; expect no failures and no transport.stop/display.remove during runtime audio loss/retry.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session/audio.py tests/session/test_audio_lifecycle.py` then `git commit -m "feat(session): preserve video through audio loss and isolate mute"`.

## Task 23: Restore persisted intent, reconcile outputs, and bound shutdown

**Implementer tier:** Capable
**Lane/category:** session / persistence-recovery-shutdown.
**Depends on:** Tasks 4, 9–11, 15, 18–22.

**Files:**
- Create: `src/airplay_linux/session/recovery.py`
- Create: `src/airplay_linux/session/persistence.py`
- Create: `src/airplay_linux/session/shutdown.py`
- Test: `tests/session/test_startup_recovery.py`
- Test: `tests/session/test_shutdown.py`

**Interfaces:**
- Consumes Task 1 SessionStore.load_all/save, ReceiverStore, OwnershipStore, DisplayPort.reconcile -> tuple[RecoveryItem,...], approve_orphan, SessionSnapshot/TrustState; Task 9 DaemonRegistry.reconcile_previous/shutdown; Task 18 ManagerPort and SessionManager.install/set_shutdown_hook; Tasks 19–22 handler factories and cleanup continuations.
- Produces `build_recovery_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` assigning recovery; `SessionPersistence(manager: ManagerPort, store: SessionStore).start() -> None`, `.flush() -> Awaitable[Result[None]]`, `.close() -> Awaitable[Result[None]]`, serializes writes per receiver; `StartupRecovery(manager: SessionManager, deps: ManagerDeps, registry: DaemonRegistry).run() -> Awaitable[Result[tuple[RecoveryItem,...]]]`; `ShutdownCoordinator(manager, deps, registry, persistence, deadline_seconds: float = 15).run() -> Awaitable[Result[None]]`.

- [ ] **Step 1: Write failing tests.** Published/active/exact output reconstructs Virtual Owned before reconnect; no current receiver retains output in Recovering/Offline; reappearance permits same-intent reconnect; invalid trust retains output and enters pairing only on receiver challenge; removing intent always resumes cleanup even if a session snapshot says streaming. Test stale stored stream IDs are not adopted and old daemon is resolved before replacement. Shutdown seed:

```python
import pytest
from airplay_linux.session.testing import SessionRig

@pytest.mark.asyncio
async def test_idle_shutdown_has_no_unresolved_owned_resource():
    rig = SessionRig()
    result = await rig.manager.shutdown()
    assert not rig.manager.pending_resources(rig.receiver_id)
```

Expose `SessionRig.receiver_id` as its default target receiver ID; full tests construct the ShutdownCoordinator and assert its Result as well as disk evidence.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/session/test_startup_recovery.py tests/session/test_shutdown.py`; expect missing recovery/persistence/shutdown.
- [ ] **Step 3: Implement startup reconstruction.** Reconcile prior daemon ownership first, then display records under provider lock. Session record provides intent/preferences only; use exact published ownership evidence to install Owned/Recovering with verified DisplayHandle. Never restore a stored stream handle as live. Removing/uncertain records block reconnect and feed cleanup. Orphan candidates are surfaced for explicit ConfirmOrphanRemoval; Keep leaves evidence intact. Unknown outputs remain untouched. For an active recovered output absent discovery, retain output and wait for receiver reappearance or explicit Retry. A trust-cleanup tombstone survives restart, blocks pairing/start, and retries backend revocation by receiver ID; do not reconstruct secret-bearing challenges from disk. Nonpersistable identities can be shown in memory but inherit no trust/resolution. Persistence writes complete safe snapshots in receiver order; failure adds a persistence diagnostic without claiming the resource is gone.
- [ ] **Step 4: Implement bounded shutdown and verify.** Close UI command acceptance, supersede each active intent once, cancel timers/challenges, atomically publish display removal intent, stop streams/audio, remove owned outputs and flush unresolved evidence. Run receiver cleanups concurrently but each in required order; shared daemon registry shuts down once. If the 15-second deadline expires, record pending exact resources and trust uncertainty, terminate exact owned daemon through supervisor as required, preserve unresolved ownership records and return cleanup error before GUI exit. Never wait unboundedly for provider or keyring. Verify `PYTHONPATH=src python -m pytest -q tests/session/test_startup_recovery.py tests/session/test_shutdown.py tests/session`; expect no failures, with fake clock deadlines and one real harmless supervisor probe already covered by process tests.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/session/recovery.py src/airplay_linux/session/persistence.py src/airplay_linux/session/shutdown.py tests/session/test_startup_recovery.py tests/session/test_shutdown.py` then `git commit -m "feat(session): reconcile durable intent and bound ordered shutdown"`.

## Task 24: Project every resource combination into safe UI actions

**Implementer tier:** Advanced
**Lane/category:** ui / pure-state-projection.
**Depends on:** Tasks 1–3.

**Files:**
- Create: `src/airplay_linux/ui/__init__.py`
- Create: `src/airplay_linux/ui/state.py`
- Test: `tests/ui/test_state_projection.py`

**Interfaces:**
- Consumes Task 1 SessionSnapshot, CapabilityReport, ReceiverView/SavedReceiver, OwnershipRecord, RecoveryItem, Size and resource enums; Task 3 safe Diagnostic and recovery catalog. No Qt, session implementation, OS adapter, or live probe is consumed.
- Produces `ViewInputs(receiver: ReceiverView | None, saved: SavedReceiver | None, capabilities: CapabilityReport, selected_mode: Mode, physical_sources: tuple[PhysicalMonitorSource,...] = (), resolution_choices: tuple[Size,...] = (), ownership: OwnershipRecord | None = None, orphans: tuple[RecoveryItem,...] = ())`; `Action` enum start/stop/pair/submit_pairing/cancel/scan/remove_metadata/retry/choose_resolution/change_resolution/recreate/retry_audio/video_only/mute/retry_cleanup/keep_cleanup/retry_revocation/keep_blocked/confirm_orphan/keep_orphan; `Badge(resource: str, label: str, severity: str)`; `PaneState(title: str, phase_label: str, badges: tuple[Badge,...], actions: frozenset[Action], mode_enabled: bool, visible_mode: Mode, primary_label: str, source_enabled: bool, resolution_enabled: bool, mute_enabled: bool, muted: bool, challenge: ChallengeView | None, notice: str | None, diagnostic: Diagnostic | None)`; `project(snapshot: SessionSnapshot, inputs: ViewInputs) -> PaneState`; `ReceiverRow(receiver_id, name: str, mode_label: str, phase_label: str, size_label: str, online: bool)` and `project_row(snapshot, inputs) -> ReceiverRow`.

- [ ] **Step 1: Write failing parameterized matrix tests.** Build DTOs locally for all rows: available, offline, recovering-owned, pairing-none, pairing-owned, resolution-required, creating, connecting, mirroring, virtual, reconnecting, reconfiguring, resolution-mismatch, audio-decision, audio-unavailable-video-active, capability failure, orphan, cleanup-required, trust-cleanup-required, failed. Check resource badges, exact allowed actions and selector lockout.

```python
from dataclasses import replace
from airplay_linux.ui.state import project, Action
from airplay_linux.domain.resources import TrustState

def test_uncertain_trust_disables_mode_and_start(idle_snapshot, view_inputs):
    pane = project(replace(idle_snapshot, trust=TrustState.CleanupRequired), view_inputs)
    assert not pane.mode_enabled
    assert Action.start not in pane.actions
    assert Action.retry_revocation in pane.actions
```

Define fixtures in this test file using Task 1 dataclasses and Task 2 builders; view_inputs includes a complete compatible CapabilityReport.
- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/ui/test_state_projection.py`; expect missing projection.
- [ ] **Step 3: Implement the full projection.** Trust cleanup outranks start actions independently of transport/display. Preserve retained-output badges in Pairing/Reconnecting/Failed/Recovering; Pairing owned says cancellation removes output. Primary labels use Start for Mirror and Create display for Virtual; active states expose Stop. ResolutionMismatch uses explicit error/accepted_resolution with Recreate at accepted mode and Stop. AudioDecision offers Retry audio, Start video only, Cancel; runtime unavailable keeps phase reflecting active video, disables mute and offers Retry audio/Stop. Creating/Reconfiguring show stage/layout warning/Cancel; active or uncertain cleanup never enables a conflicting mode action. Offline displays last known identity and permits Scan/Remove saved metadata; orphan cleanup is never an unconditional Remove. Capability failure shows exact missing command and Retry after fix/Back while other receivers stay actionable. All four resource/trust badges derive from authoritative state even when phase is Failed.
- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/ui/test_state_projection.py`; expect no failures. Assert each matrix row has at least one explicit expected-action assertion and absence checks for unsafe controls, not only snapshot comparisons.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/ui tests/ui/test_state_projection.py` then `git commit -m "feat(ui): project resource states into explicit safe actions"`.

## Task 25: Build the dense PySide6 dashboard and accessible prompts

**Implementer tier:** Advanced
**Lane/category:** ui / widgets-and-models.
**Depends on:** Tasks 1–3, 24.

**Files:**
- Create: `src/airplay_linux/ui/models.py`
- Create: `src/airplay_linux/ui/window.py`
- Create: `src/airplay_linux/ui/dialogs.py`
- Test: `tests/ui/test_dashboard_widgets.py`

**Interfaces:**
- Consumes Task 24 PaneState, ReceiverRow, Action, project/project_row types and Task 1 public DTOs only. Widgets do not call transport, processes, keyring, display commands or discovery themselves.
- Produces `ReceiverListModel(QAbstractTableModel).replace(rows: tuple[ReceiverRow,...]) -> None`, `.receiver_id(index: QModelIndex) -> ReceiverId | None`; columns Device/Mode/State/Size. `MainWindow(QMainWindow).render_receivers(rows) -> None`, `.render_selected(pane: PaneState) -> None`, `.set_activity(events: tuple[ActivityEvent,...]) -> None`, `.set_summary(available: int, sessions: int, environment: str) -> None`.
- Produces Qt signals `receiver_selected = Signal(str)`, `action_requested = Signal(str)`, `mode_requested = Signal(str)`, `source_selected = Signal(str)`, `resolution_selected = Signal(int,int)`, `mute_requested = Signal(bool)`, `closing_requested = Signal()`; receiver selection is converted back to ReceiverId by presenter. `PairingDialog(challenge: ChallengeView).take_input() -> SensitiveText | None`; `ConfirmStopDialog(next_mode: Mode).confirmed() -> bool`; `OrphanDialog(item: RecoveryItem).confirmed() -> bool`; `ResolutionDialog(choices: tuple[Size,...]).decision() -> Size | None`.

- [ ] **Step 1: Write failing Qt tests.** Use pytest-qt `qtbot`, ensure receiver list and pane coexist, only selected mode's controls are visible, empty/offline device states are readable, and keyboard/accessibility labels/tooltips are populated. Seed:

```python
from PySide6.QtWidgets import QPushButton
from airplay_linux.ui.window import MainWindow

def test_dashboard_has_text_commands(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.findChild(QPushButton, 'scanButton').text() == 'Scan'
    assert window.findChild(QPushButton, 'startButton').text() == 'Start'
    assert window.findChild(QPushButton, 'stopButton').text() == 'Stop'
```

- [ ] **Step 2: Run red.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/ui/test_dashboard_widgets.py`; expect missing widgets.
- [ ] **Step 3: Implement.** Use a main toolbar with Scan/counts/environment/settings, splitter with receiver table and selected pane, and bottom activity view. Exclusive QButtonGroup segments `modeMirror`/`modeVirtual`; one stacked source/resolution editor, never simultaneous start renderers. Controls use object names scanButton/startButton/stopButton/pairButton/muteToggle/sourceCombo/resolutionCombo/audioRetryButton/videoOnlyButton/cancelButton/resourceBadges/errorLabel/activityView. Text buttons identify commands; mute has accessible name 'Mute receiver audio' and an explanatory tooltip. Every unfamiliar icon-only control has tooltip and accessible name; settings can be a text button. Password input is masked and unrestricted; PIN prompt follows receiver kind. Clear secret widget contents immediately after returning transient SensitiveText or Cancel, and never store it as Qt item data/history. Dialogs render retained-output cancellation notice, resolution layout warning, explicit stop-before-switch, and orphan ownership/helper details. closeEvent emits closing_requested and defers actual close to presenter after cleanup.
- [ ] **Step 4: Verify.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/ui/test_dashboard_widgets.py`; expect no failures including tab navigation, no double mode selection, secret-field clearing and every Action's corresponding visibility/disabled state.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/ui/models.py src/airplay_linux/ui/window.py src/airplay_linux/ui/dialogs.py tests/ui/test_dashboard_widgets.py` then `git commit -m "feat(ui): add accessible multi-receiver operations dashboard"`.

## Task 26: Bind GUI intent to domain commands and safe asynchronous updates

**Implementer tier:** Advanced
**Lane/category:** ui / presenter.
**Depends on:** Tasks 1–3, 24–25; consume ManagerPort, not the concrete session implementation.

**Files:**
- Create: `src/airplay_linux/ui/presenter.py`
- Test: `tests/ui/test_presenter.py`

**Interfaces:**
- Consumes Task 1 ManagerPort.dispatch/snapshot/subscribe/shutdown, DiscoveryPort.refresh/current/subscribe, CapturePort.list_physical, ReceiverStore.remove_metadata, public commands; Task 24 ViewInputs/project/project_row; Task 25 MainWindow signals/render methods and dialogs; Task 3 ActivityLog.entries/subscribe/export; Task 2 FakeManager/FakeDiscovery/FakeCapture.
- Produces `Presenter(window: MainWindow, manager: ManagerPort, discovery: DiscoveryPort, capture: CapturePort, receivers: ReceiverStore, capabilities: Callable[[],CapabilityReport], activity: ActivityLog, ownerships: OwnershipStore, dialogs: DialogFactory, request_shutdown: Callable[[],Awaitable[Result[None]]])`; `.start() -> Awaitable[None]`, `.close() -> Awaitable[None]`. `DialogFactory.pairing(challenge: ChallengeView) -> Awaitable[SensitiveText | None]`, `.confirm_stop(mode: Mode) -> Awaitable[bool]`, `.resolution(choices: tuple[Size,...]) -> Awaitable[Size | None]`, `.orphan(item: RecoveryItem) -> Awaitable[bool]` use nonmodal QDialog.open/finished continuations. Dismissing or invalidating a prompt resolves its future and clears secret input; no nested blocking dialog loop is used.

- [ ] **Step 1: Write failing presenter tests with fake ports/dialog answers.** Assert only domain commands cross into manager, stale selected receiver/pairing prompt cannot target a new selection, Scan updates all visible receivers, source/resolution choices are receiver-local, audio actions affect selected receiver, and a rejected action yields safe diagnostic. Include confirmed-switch testing:

```python
from airplay_linux.domain.messages import ConfirmSwitch
import pytest

@pytest.mark.asyncio
async def test_unconfirmed_switch_never_queues_next_start(presenter_rig):
    presenter_rig.dialogs.stop_answer = False
    presenter_rig.window.mode_requested.emit('virtual')
    await presenter_rig.settle()
    assert not any(isinstance(c, ConfirmSwitch) and c.confirmed
                   for c in presenter_rig.manager.commands)
```

Define presenter_rig in the test from MainWindow, FakeManager, FakeDiscovery, FakeCapture and an asynchronous fake DialogFactory with preprogrammed answers. Its settle awaits only scheduled ready work and processes Qt events without wall-clock sleeps; do not import the composition root.
- [ ] **Step 2: Run red.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/ui/test_presenter.py`; expect missing presenter.
- [ ] **Step 3: Implement.** Capture receiver ID, challenge ID and public fence values when opening each prompt; submit to that receiver only and let coordinator revalidate. A mode click while active opens explicit Stop confirmation, dispatches ConfirmSwitch only after approval, and keeps old mode until cleanup completes. Render manager snapshots and discovery/capability changes through Qt queued delivery on GUI thread; qasync tasks perform async port calls without blocking. The receiver list is the union of live discovery, saved metadata, active intent and uncertain owned/trust resources; an idle offline record with removed metadata is hidden only when no cleanup barrier remains. Maintain per-receiver idle selection separately from active mode. Dispatch audio retry/video-only/cleanup/revocation/recreate actions explicitly; never infer video-only from a disabled audio probe. Orphan removal needs positive dialog confirmation and exact owner ID. Removing saved metadata must not bypass trust cleanup or delete display records. Keep blocked/manual cleanup retains badges. Closing starts one shutdown future and keeps bounded progress visible; repeat close is idempotent. Present activity/catalog diagnostics without exposing raw backend strings. Resolve stale delayed source lists/ownership loads using selected-receiver request tokens.
- [ ] **Step 4: Verify.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/ui/test_presenter.py`; expect no failures including two receivers with overlapping prompts and events, unavailable audio mute lockout, and live mode switching disabled until cleanup confirmation.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/ui/presenter.py tests/ui/test_presenter.py` then `git commit -m "feat(ui): bind dashboard intent to fenced domain commands"`.

## Task 27: Compose application startup, single-instance guard, and operator diagnostics

**Implementer tier:** Capable
**Lane/category:** assembly / runnable-application.
**Depends on:** Tasks 3–26.

**Files:**
- Create: `src/airplay_linux/app.py`
- Create: `src/airplay_linux/runtime.py`
- Create: `src/airplay_linux/process/instance.py`
- Create: `src/airplay_linux/resources/airplay-linux.desktop`
- Create: `README.md`
- Create: `docs/operations.md`
- Test: `tests/app/test_composition.py`
- Test: `tests/app/test_instance_guard.py`

**Interfaces:**
- Consumes Tasks 19–23 handler factories (`build_lifecycle_handlers`, `build_virtual_handlers`, `build_reconnect_handlers`, `build_audio_handlers`, `build_recovery_handlers`) returning disjoint Mapping[Route,Handler]; Task 18 SessionManager/ManagerDeps; Task 23 StartupRecovery/SessionPersistence/ShutdownCoordinator; Tasks 4–17 concrete adapters; Task 26 Presenter; Task 7 process ownership; Task 9 DaemonRegistry reconciliation; Task 17 EnvironmentProbe.check/refresh.
- Produces `compose_handlers(deps: ManagerDeps) -> Mapping[Route,Handler]` rejecting duplicate/missing routes; `compose_manager(deps: ManagerDeps) -> SessionManager`; `RuntimeConfig(state_root: Path, runtime_root: Path, live: bool = True)`; `build_runtime(config: RuntimeConfig, overrides: Mapping[str,object] | None = None) -> Awaitable[Result[ApplicationRuntime]]`, with explicit typed override-name validation for tests and no production hidden fake fallback. `ApplicationRuntime.start() -> Awaitable[Result[None]]`, `.shutdown() -> Awaitable[Result[None]]`, `.diagnostics() -> str`, `.manager`, `.presenter`.
- Produces `parse_args(argv: Sequence[str]) -> argparse.Namespace` with boolean diagnostics/check_environment fields; `InstanceGuard(runtime_root: Path, process: ProcessPort).acquire(instance_uuid: InstanceId) -> Result[None]`, `.release() -> None`, QLockFile plus authenticated UUID/start-time extension; `main(argv: Sequence[str] | None = None) -> int` supporting normal GUI, `--check-environment` and `--diagnostics`. Diagnostic commands perform read-only probes/export and never create streams/outputs or expose runtime tokens.

- [ ] **Step 1: Write failing composition and instance tests.** Use two harmless child processes/temporary runtime dirs to prove one instance, live mismatch blocking, dead verified owner release, PID reuse, truncated lock, crash between lock acquisition and owner extension, and previous-daemon reconciliation before any start. Fake complete capabilities should compose the same manager used by GUI; missing krfb fixture still launches usable shell/discovery. CLI safe-path seed:

```python
from airplay_linux.app import parse_args

def test_diagnostics_is_an_explicit_read_only_command():
    args = parse_args(['--diagnostics'])
    assert args.diagnostics
    assert not args.check_environment
```

- [ ] **Step 2: Run red.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/app`; expect missing app/runtime/instance modules.
- [ ] **Step 3: Implement instance guard and assembly.** Use QLockFile at the exact global runtime path, disable time-age-based stale deletion, and inspect an existing lock before invoking a method that could auto-remove it. Verify PID liveness/start ticks against protected ownership evidence; live/inaccessible mismatch blocks. Store UUID and start ticks as a versioned trailing extension to the Qt lock file while holding the lock, fsync it, and test QLockFile.getLockInfo compatibility; a companion owner record binds the extension to exact process evidence. A dead PID with stable unchanged lock evidence permits stale removal; a live PID without start evidence does not. Never use age/name alone. Acquire app lock before backend ownership reconciliation; load ReceiverStore.all and seed IdentityResolver with prior non-secret endpoint evidence before DiscoveryPort.start, preserving cold-start continuity checks. Instantiate real providers with optional unsupported adapters where appropriate, reconcile old private daemon/outputs before enabling stream commands, install recovered intent, subscribe persistence/diagnostics and connect presenter. Use QApplication + qasync QEventLoop; import Qt lazily so missing-runtime CLI error is readable. Runtime getters do not start a daemon until a permitted operation needs it. Set SIGTERM/INT to the same bounded shutdown path, and release app lock after unresolved records are flushed and owned daemon termination is attempted.
- [ ] **Step 4: Document operation and verify.** README gives editable install, `airplay-linux`, KDE Plasma 6 Wayland target, two exclusive per-receiver modes, supported concurrency, exact optional helper requirement, and capability-gated backend compatibility link. operations.md covers missing commands/backend operations, portal permissions, keyring restrictions, explicit resolution/audio choices, PIN/password flow, stop-before-switch, retained output on reconnect, orphan confirmation, CleanupRequired/TrustCleanupRequired retry/manual paths, and safe diagnostic export. Include the installed 0.5 compatibility limitation candidly; no claim of a successful real TV test. Desktop entry uses Type=Application, Name=AirPlay Linux, Exec=airplay-linux, Terminal=false, Categories=AudioVideo; ship it as a package resource using Task 1 package-data configuration. Run `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/app tests/ui` and `python -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/airplay-linux-wheel .`; expect no failures and a wheel with entry point/package resource. Use a unique task-specific temporary wheel directory when running concurrently.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/app.py src/airplay_linux/runtime.py src/airplay_linux/process/instance.py src/airplay_linux/resources/airplay-linux.desktop README.md docs/operations.md tests/app` then `git commit -m "feat(app): compose single-instance desktop runtime and recovery diagnostics"`.

## Task 28: Exercise real adapter plumbing with private fake processes and opt-in environment checks

**Implementer tier:** Advanced
**Lane/category:** adapter-acceptance / contract-and-environment-tests.
**Depends on:** Tasks 4–17, 27.

**Files:**
- Create: `tests/adapter_contracts/test_transport_contract.py`
- Create: `tests/adapter_contracts/test_keyring_contract.py`
- Create: `tests/adapter_contracts/test_capture_contract.py`
- Create: `tests/adapter_contracts/test_kde_contract.py`
- Create: `tests/adapter_contracts/test_private_processes.py`
- Create: `tests/fixtures/processes/fake_doubletake.py`
- Create: `tests/fixtures/processes/fake_virtualmonitor.py`
- Create: `tests/fixtures/processes/fake_kscreen.py`
- Create: `tests/environment/test_installed_contracts.py`
- Create: `tests/environment/test_live_kde.py`
- Create: `tests/environment/test_live_receivers.py`

**Interfaces:**
- Consumes Task 8 BackendControl and verified profile/fixture distinction; Task 11 DoubleTakeTransport (TransportPort and PairingPort), Task 10 BackendKeyringStore, Task 12 PhysicalCaptureProvider, Tasks 13–15 KdeVirtualDisplayProvider and observers/helpers, Task 7 LinuxProcessRunner, Task 9 DaemonRegistry, Task 27 build_runtime and diagnostic commands. Reuse Task 2 deterministic fakes and Task 4 stores; tests only create the files listed here.
- Produces reusable contract test parameterization applying identical invariants to fake and real adapter implementations. Fake process protocol is explicitly test-only: private Unix socket, newline-delimited JSON with request ID, owner token, instance UUID, operation and target/stream identifiers; responses correlate exact IDs. It never advertises itself as a compatible installed doubletake. Fake helper exposes owner env, persists simulated KScreen output in a tmp_path file, supports controlled crash-before-PID-persistence and removal; fake_kscreen changes only that file.

- [ ] **Step 1: Add adversarial contract assertions before wiring fixtures.** Each adapter contract must reject wrong receiver/endpoint/owner/source, stop exactly one resource, survive stale replies, and preserve secret boundaries. Missing test fixture executables or deliberately malformed first responses should make the initial run fail. Seed a provenance assertion:

```python
import json
from pathlib import Path

def test_contract_fixture_cannot_claim_live_doubletake_support():
    fixture = json.loads(Path('tests/fixtures/backend/contract-v1.json').read_text())
    assert fixture['provenance'] == 'application-contract-fixture'
    assert fixture['live_support_proven'] is False
```

- [ ] **Step 2: Run red.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/adapter_contracts`; expect failures naming absent fake process fixtures/unconfigured private handshake, not an arbitrary unrelated assertion. Task 8 must have supplied the provenance fields used above.
- [ ] **Step 3: Implement real-process fixtures and contract tests.** Run actual LinuxProcessRunner/supervisor/registry over private fake processes; test owner death and GUI crash with surviving old daemon streams, PID/start mismatch, socket token mismatch, starting/stopping publication crashes, target-local disconnect/mute, concurrent pair prompts, compensation failure, malformed/timeout process output, bounded reap, and second-app rejection. Run real KDE provider with fake helper/kscreen commands through all stage crashes and two creations; use process owner environment/start ticks to recover launch-to-PID window. Apply fake D-Bus portal/keyring peers to verify permission and no-plaintext guarantees. All cleanup is finally-guarded and confined to exact temporary resources.
- [ ] **Step 4: Add environment checks and verify.** `tests/environment/test_installed_contracts.py` read-only checks help and private-socket contract compatibility, recording unsupported operations rather than inventing support; mark live_backend and require `AIRPLAY_LINUX_LIVE_BACKEND=1`. Live KDE requires `AIRPLAY_LINUX_LIVE_KDE=1` plus KDE Wayland/helper/support and creates/removes only its exact owned test output after explicit test configuration. Live receivers require `AIRPLAY_LINUX_LIVE_HARDWARE=1` and non-secret config path `AIRPLAY_LINUX_LIVE_CONFIG`; verify two targets, isolation, physical source enforcement, startup/runtime audio and mute, reconnect output retention, accepted-mode recreation, endpoint replacement and shutdown. Secret input comes interactively or backend keyring, never checked-in config. Missing opt-in skips with a precise reason; incompatible selected live backend fails its compatibility gate, and no operational pass is inferred from skipped hardware. Run `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/adapter_contracts tests/environment -m 'not live_backend and not live_kde and not live_hardware'`; expect no failures. Document available manual commands in test module docstrings; do not run live mutations in ordinary CI.
- [ ] **Step 5: Commit.** `git add tests/adapter_contracts tests/fixtures/processes tests/environment` then `git commit -m "test(adapters): cover private process contracts and opt-in live environments"`.

## Task 29: Verify every lifecycle transition and crash boundary through assembled sessions

**Implementer tier:** Capable
**Lane/category:** lifecycle-acceptance / end-to-end-domain-tests.
**Depends on:** Tasks 1–23, 27.

**Files:**
- Create: `tests/integration/test_lifecycle_matrix.py`
- Create: `tests/integration/test_concurrent_sessions.py`
- Create: `tests/integration/test_crash_recovery.py`
- Create: `tests/integration/test_trust_and_audio.py`

**Interfaces:**
- Consumes Task 27 `compose_handlers(ManagerDeps) -> Mapping[Route,Handler]` and `compose_manager(ManagerDeps) -> SessionManager`; Task 18 SessionRig(handlers_factory), snapshot/dispatch/ingest/drain; Task 2 all scripted fakes, fake Clock/Scheduler and Journal; Task 4 actual JSON stores where disk recovery is tested; Task 23 StartupRecovery/ShutdownCoordinator and Task 15 recovery table. No new production interface is produced.
- Produces a requirement-keyed test inventory with these named cases: idle_mirror, idle_virtual, offline_reappearance, offline_start, choose_resolution, cancel_resolution, submit_pair_none, cancel_pair_none, submit_pair_owned, cancel_pair_owned, publish_output, forget_no_stream, cancel_partial_create, stream_ready, pairing_during_connect, network_during_connect, startup_audio_decision, active_network_loss, runtime_audio_loss, stop_or_confirm_switch, reconnect_success, reconnect_stop, local_budget_exhausted, daemon_exit, helper_or_output_loss, change_resolution, cancel_reconfigure, recreate_accepted, stop_mismatch, adopt_recovering, recovery_offline, shutdown_any, stopped_all_released, stop_uncertain, retry_cleanup, keep_cleanup, retry_revocation, keep_trust_blocked. Each has explicit initial resources, command/event, expected resource tuple, and ordered side effects.

- [ ] **Step 1: Add full-path tests and demonstrate their sensitivity.** Start with one assembled virtual session and a delayed stop; assert removing intent is durable before transport stop, transport acknowledgement precedes helper stop, and no next mode starts while uncertainty remains. For an initial red run, inject a test-local wrapper that drops the mark-removing write and assert the new test fails; remove the wrapper, leaving production unchanged. Seed:

```python
import pytest
from airplay_linux.session.testing import SessionRig
from airplay_linux.runtime import compose_handlers
from airplay_linux.domain.messages import Stop

@pytest.mark.asyncio
async def test_stop_empty_receiver_is_idempotent():
    rig = SessionRig(compose_handlers)
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    await rig.manager.dispatch(Stop(receiver_id=rig.receiver_id))
    await rig.settle()
    assert not rig.manager.pending_resources(rig.receiver_id)
    assert 'display.create' not in rig.journal.names()
```

- [ ] **Step 2: Run scoped red/sensitivity probe.** `PYTHONPATH=src python -m pytest -q tests/integration/test_lifecycle_matrix.py`; observe the intentional test-local mutation failure, then remove that mutation and run the same command. Do not weaken expected states to make a transition pass. These acceptance tasks are test additions: a mutation-based red phase avoids manufacturing a production regression.
- [ ] **Step 3: Complete the lifecycle matrix and two-receiver acceptance.** Cover every named case with actual command/event delivery rather than dataclass replacement alone. For A Mirror/B Virtual and A Virtual/B Virtual, prove mode exclusivity, unique helper/output identities, unrelated-target failure isolation, provider creation serialization and independent mute/audio recovery. Inject stale session/lease/endpoint/daemon/challenge/audio/display events after Stop/new start and verify no revival, while exact old-handle cleanup still completes. Inject secret canaries through real coordinator boundaries and inspect all disk files/activity/exception exports. Crash-recovery tests cover each ownership stage, each atomic-write failure, launch-before-PID persistence, removed-intent republish prevention, missing helper orphan confirmation, no-record outputs, and guarded layout restoration. Recovery respects absent/changed endpoints and pending trust cleanup. Shutdown tests assert bounded completion and retained uncertain records.
- [ ] **Step 4: Verify.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/integration tests/session tests/display tests/trust`; expect no failures. No test uses real elapsed sleeps for retry/state transitions or depends on a TV/LAN/compositor. Every inventory key must map to at least one collected test node, checked by a static list within test_lifecycle_matrix.py against its parameter IDs.
- [ ] **Step 5: Commit.** `git add tests/integration` then `git commit -m "test(session): verify full lifecycle concurrency and crash recovery acceptance"`.

## Task 30: Verify the full Qt state matrix and final runnable acceptance

**Implementer tier:** Advanced
**Lane/category:** ui-acceptance / final-behavior-tests.
**Depends on:** Tasks 24–29.

**Files:**
- Create: `tests/ui_acceptance/test_matrix.py`
- Create: `tests/ui_acceptance/test_multireceiver_flow.py`
- Create: `tests/ui_acceptance/test_diagnostics.py`

**Interfaces:**
- Consumes Task 27 build_runtime(RuntimeConfig, overrides), compose_handlers and ApplicationRuntime; Task 18 SessionRig; Task 24 projection/ViewInputs/Action; Tasks 25–26 widgets/presenter/dialog seams; Task 3 safe diagnostic export. All adapters are deterministic overrides and the GUI runs offscreen. No new production interface is produced.
- Produces Qt tests for all 20 UI table rows and the mode/audio/trust variants described below, plus an assembled two-receiver walkthrough; the suite is the final acceptance evidence, not a substitute for separately selected live hardware verification.

- [ ] **Step 1: Add UI matrix tests and prove a real assertion fails under a local mutation.** Parameterize available/offline/recovering-owned/pairing-none/pairing-owned/resolution-required/creating/connecting/mirroring/virtual/reconnecting/reconfiguring/resolution-mismatch/audio-decision/audio-unavailable/dependency-failure/orphan/cleanup-required/trust-cleanup-required/failed. Render the actual MainWindow through Presenter and assert visible text, badges, enabled controls, and dispatched commands. Temporarily make a test-local projection wrapper enable Start during TrustCleanupRequired and confirm the control lockout test fails, then remove the wrapper. Seed:

```python
from PySide6.QtWidgets import QPushButton

def test_audio_decision_has_three_explicit_choices(audio_decision_window):
    window = audio_decision_window
    assert window.findChild(QPushButton, 'audioRetryButton').isEnabled()
    assert window.findChild(QPushButton, 'videoOnlyButton').isEnabled()
    assert window.findChild(QPushButton, 'cancelButton').isEnabled()
```

Define audio_decision_window in this test module by building SessionRig(compose_handlers), rendering its startup AudioDecisionRequired snapshot through the real Presenter, and registering widgets with qtbot; no external fixture is assumed.
- [ ] **Step 2: Run scoped red/sensitivity probe.** `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python -m pytest -q tests/ui_acceptance/test_matrix.py`; observe the intentional lockout mutation failure, remove the wrapper, and rerun until the real projection passes. Do not edit earlier production files in this task; a production failure is an integration finding for its owning task.
- [ ] **Step 3: Exercise complete user flows.** Scan empty/error/success, show saved Offline receiver, start A Mirror, B Virtual with explicit resolution, receiver-demanded PIN/password, cancel pairing while output owned, stale submit after selection/endpoint change, confirmed stop-before-switch, reconnect with retained output, runtime audio loss/mute disabled/retry, accepted-mode recreation with warning, cleanup timeout/Keep/Retry, trust revocation failure/block/retry, orphan confirm/Keep, missing krfb/backend operations with unrelated UI usable, two app instance rejection, and window close during creation. Verify text never presents both modes as active controls for one receiver and a stopped/uncertain resource cannot be described as available. Diagnostics export includes exact recovery messages and excludes secrets, raw process text and owner/portal tokens.
- [ ] **Step 4: Run final deterministic verification.** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 PYTHONPATH=src python -m pytest -q -m 'not live_backend and not live_kde and not live_hardware'`; expect no failures. Run `python -m compileall -q src` and the wheel build command from Task 27; expect success. Inspect `git diff --check` and `git status --short` for unintended paths. Report selected/skip reasons for live suites explicitly; do not claim TV, system-audio, or real KDE mutation acceptance when those opt-in tests were not executed. A deterministic test failure is repaired through the controller's task review/fix workflow rather than by widening this task's allowlist.
- [ ] **Step 5: Commit.** `git add tests/ui_acceptance` then `git commit -m "test(ui): verify state matrix diagnostics and concurrent receiver flows"`.
