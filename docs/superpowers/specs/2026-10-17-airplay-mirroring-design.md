# AirPlay Linux Desktop Controller Design

Date: 2026-10-17

## Background

AirPlay Linux is a desktop GUI for sending a Linux desktop to AirPlay-capable receivers on the local network. The initial target is KDE Plasma 6 Wayland, while the application keeps the core discovery and mirroring model portable to X11 and other desktop environments when their capture or display providers are available.

Linux-to-AirPlay sending is not implemented as a mature, general-purpose library. The design therefore treats `doubletake` as an external transport backend and keeps AirPlay protocol details outside the application. The application owns device discovery, pairing workflow, session state, mode transitions, virtual-display lifecycle, recovery, and user-facing diagnostics.

The current development environment is KDE Plasma 6 Wayland. `doubletake`, `doubletake-ctl`, `kscreen-doctor`, Avahi, and PySide6 are available. `krfb-virtualmonitor` is currently absent and must be detected as an optional dependency required for Virtual display mode.

## Goals

- Discover Apple TVs and AirPlay-enabled smart TVs advertised on the LAN.
- Support multiple receiver sessions at the same time.
- Provide two mutually exclusive modes per receiver:
  - Mirror: stream one physical monitor to the TV.
  - Virtual display: create a KDE virtual monitor at the receiver's resolved mode, use it as an ordinary desktop monitor, and stream that output to the TV.
- Include system audio with per-session mute controls.
- Support PIN/password pairing and remembered trusted receivers.
- Support KDE Plasma 6 Wayland as a guaranteed v1 environment.
- Keep the core session model testable without a TV, LAN, compositor, or privileged display operation.
- Report missing dependencies, unsupported capabilities, and recovery actions directly in the GUI.

## Non-goals

- Reimplementing AirPlay, FairPlay, RTSP, or receiver authentication inside this application.
- Making every Linux compositor support virtual outputs in v1.
- Treating an application window as a Mirror source.
- Running Mirror and Virtual display modes concurrently for the same receiver.
- Guessing a TV resolution when the receiver does not provide reliable information.

## User Model

A receiver is a stable device identity discovered from mDNS. A receiver can be available, offline, paired, pairing-required, or in an active session. Each receiver owns one `DeviceSession` and one active mode at most.

Different receivers may use different modes concurrently. Switching a receiver from one mode to the other requires an explicit stop and teardown of the current mode first.

### Receiver identity

The discovery layer first aggregates all records belonging to one receiver into a `ReceiverRecordSet`; it derives `ReceiverId` only after this cross-service merge. Records are matched conservatively in this order:

1. Records with equal normalized AirPlay `deviceid` or MAC-like identifiers across `_airplay._tcp` and `_raop._tcp` may merge.
2. Records with different non-empty device identifiers never merge, even when their service-instance labels, models, hosts, or ports match. They remain separate replacement/conflict candidates.
3. If exactly one record has a device identifier and its companion record has none, they may merge only when the normalized service-instance label and model match and their current canonical address matches while the service port is ignored.
4. If neither record has a device identifier, an AirPlay and RAOP record may merge only when the normalized service-instance label and model match and their current canonical address matches. A port change is compatible; a different current address is replacement evidence, not an automatic merge.
5. If more than one candidate satisfies a fallback key, or if fallback records at different addresses overlap, the records remain ambiguous and are never combined.

For a merged record set, `ReceiverId` is derived in this order:

1. `airplay-device:<normalized-device-id>` when a compatible device identifier exists.
2. `airplay-fingerprint:<sha256(normalized-instance-label|normalized-model)>` for an unambiguous stable service-instance fallback.
3. `airplay-endpoint:<sha256(normalized-host|normalized-model)>` for the weaker endpoint-sensitive fallback.

An ambiguous candidate receives an ephemeral `airplay-ambiguous:<sha256(record-set)>` identifier that is not persisted and cannot inherit trust or remembered resolution. A service-name or port change does not change an ID when a device identifier or stable service-instance label remains compatible. A hostname change with the same canonical address is compatible. A new endpoint for a fallback-only identity is a possible replacement: it requires explicit re-pairing and does not inherit trust or remembered resolution. A new device identifier at the same endpoint is always a new receiver. Discovery fixtures cover equal and conflicting IDs, both service types, identical fallback labels/models, port and hostname changes, replacement advertisements arriving in either order, collisions, and trust non-inheritance.

The application stores the receiver ID, merged service metadata, display name, last endpoint, pairing/trust metadata, and non-secret preferences. It never stores a PIN or credential in this record.

### Mirror mode

Mirror mode accepts a physical monitor source only. It creates no virtual output. Starting the mode starts a `doubletake` transport session with system audio enabled by default. The user may stop the session or mute its audio without affecting other receiver sessions.

On Wayland, physical-monitor selection is subject to the compositor's screen-capture portal. The domain contract uses a typed `PhysicalMonitorSource` containing a connector identity and, where required, a portal restore token. The transport adapter must expose only physical monitor sources to the session manager and must reject virtual-output or application-window sources. If the active backend cannot accept or verify a requested physical monitor descriptor, Mirror start enters an actionable unsupported state rather than silently capturing another source.

### Virtual display mode

Virtual display mode creates a KDE virtual output at the receiver's resolved resolution, configures it as a usable desktop monitor, and streams that output to the receiver. The output remains available to applications while the mode is active and during a transient network reconnect.

The domain contract uses a typed `VirtualOutputSource` containing the connector identity, KScreen output ID, and configured size. The output is owned by the session that created it.

Stopping the mode stops transport first and then removes the output owned by that session. A transport-only network failure preserves the output. A virtual-display helper failure invalidates the display source and enters display cleanup/reconciliation rather than pretending the display remains usable. A stale output found on a later startup is identified and offered for cleanup rather than being deleted without user consent.

## UI Direction

The main window uses the operations-dashboard layout, revised so Mirror and Virtual display are not shown as simultaneous controls for a single receiver.

```text
+ AirPlay Linux ------------------------------------------------+
| [Scan]  3 TVs available   2 sessions   KDE Wayland     [cfg] |
+------------------+-------------------------------------------+
| TVs              | Selected receiver                         |
|                  |                                           |
| * Living Room    | Living Room  Apple TV 4K  Paired           |
|   Mirror         | [Mirror | Virtual display]                 |
|   Streaming      |                                           |
|                  | Physical monitor: [Monitor 1          v]  |
| * Office TV      | Audio: [on]             [Start]           |
|   Virtual        |                                           |
|   3840x2160      |                                           |
|                  |                                           |
| o Bedroom TV     |                                           |
|   Available      |                                           |
+------------------+-------------------------------------------+
| Activity: pairing completed; receiver is ready              |
+--------------------------------------------------------------+
```

The device list shows the active mode and state. The selected receiver pane contains a segmented mode control and renders only the selected mode's controls. The layout is dense and operational: it supports scanning, comparison, repeated start/stop actions, and simultaneous sessions without presenting two conflicting renderers for one receiver.

Unfamiliar icon-only controls must have tooltips. Text buttons remain for clear commands such as `Start`, `Stop`, `Pair`, and `Create display`; audio uses a toggle or mute icon with an accessible label.

### UI state matrix

The primary session label is rendered together with compact transport, display, and audio resource badges. This makes retained resources explicit instead of implying that one coarse label describes every side effect.

| Domain/resource state | Selected-pane presentation | Allowed primary actions |
|---|---|---|
| Available (`Idle`, no output) | Mode selector, physical source or resolution controls, Pair if needed | Start, Pair, Scan |
| Offline (`Idle`, no output) | Last-known receiver identity and offline badge | Scan, Remove saved metadata |
| Recovering (`Output owned`, transport reconnecting) | Recovered output, receiver identity, reconnect status | Retry, Stop |
| Pairing (`No output`) | PIN/password prompt, receiver identity, Cancel | Submit PIN, Cancel, Retry |
| Pairing (`Output owned`) | Pairing prompt plus retained output badge and cleanup-on-cancel notice | Submit PIN, Cancel and remove output |
| Resolution required | Supported-mode list or explicit picker, remembered-choice indicator | Choose, Cancel |
| Creating output | Progress, reservation stage, and Cancel; no mode switch | Cancel |
| Connecting | Progress, selected source, audio policy and current display badge | Stop |
| Mirroring | Physical source, active audio/mute control, Stop | Mute/Unmute, Stop |
| Virtual display | Output size, active audio/mute control, Stop | Mute/Unmute, Stop, Change resolution |
| Reconnecting (`Output owned` or no output) | `Reconnecting`, last error, endpoint status, next retry | Retry, Stop |
| Reconfiguring | Old output removal/new output creation progress and layout warning | Cancel |
| Resolution mismatch | Stream stopped, current output details, accepted mode and layout warning | Recreate at accepted mode, Stop |
| Audio decision required | Audio unavailable before video is ready; show Retry audio, Start video only, and Cancel | Retry audio, explicit video-only start, or Cancel |
| Audio unavailable (video active) | Video remains active, audio error, disabled mute, Retry audio | Retry audio, Stop |
| Dependency or capability failure | Exact missing capability and recovery hint | Retry after fix, Back |
| Orphan candidate | Connector/output details, ownership evidence, and helper status | Remove after confirmation, Keep |
| Cleanup required | Resource that could not be confirmed removed, timeout detail | Retry cleanup, Keep for manual cleanup, Stop |
| Trust cleanup required | Backend credential cancellation/revocation is unconfirmed; no new pairing | Retry revocation, Keep blocked, Remove saved metadata |
| Failed | Error detail plus transport/display/audio resource badges and retained intent | Retry, Stop |

The mode selector is disabled while a session is active, creating an output, reconfiguring, stopping, or `TrustState` is `RevocationPending`/`CleanupRequired`. A requested mode switch is queued only after the user confirms Stop; the next mode starts after teardown completes. The application never starts the next mode concurrently with cleanup from the previous one. Runtime audio failure leaves video active, disables mute while audio is unavailable, and exposes Retry audio or Stop.

## Architecture

The application is a PySide6 desktop shell over a small domain core.

### Application shell

The PySide6 layer owns the main window, Qt models, view state, user prompts, notifications, and activity-log presentation. It does not know how AirPlay transport or KDE virtual outputs work.

### Session manager

The session manager is the central deep module. Its interface accepts domain commands and emits state changes and diagnostic events. It enforces:

- one active mode per receiver;
- physical-monitor-only sources for Mirror;
- resolution selection before virtual-output creation;
- transport-before-teardown ordering;
- reconnect preservation for Virtual display mode after transport-only network failure;
- cleanup ownership for child processes and outputs;
- serialized commands per receiver;
- independent resource state for transport, display, and audio.

Each command allocates a unique `operation_id`; commands do not all advance one shared generation. A `session_generation` identifies the lifetime of the current receiver mode intent and increments only when a new start supersedes the prior intent, a user cancellation abandons the current start, Stop/mode switch begins, resolution reconfiguration begins, or shutdown begins. A retry within the same intent keeps the session generation but receives a new operation ID.

Transport has a separate `transport_lease_id` for the current stream/daemon/endpoint binding. It changes on a new connection, endpoint generation change, or daemon restart. Transport events must match the current lease for state updates; stop/cleanup acknowledgements may instead match the exact stream handle being released. Display events use the persisted `display_owner_uuid`, connector identity, and helper PID/start time, so a network endpoint change or transport restart cannot invalidate a valid display-helper event. Audio retry has its own operation ID and does not invalidate the transport or display lease.

Pairing uses a `challenge_id` and `trust_generation` in addition to the session and endpoint generations. Submitting a still-active challenge does not increment the session generation. Cancel, expiry, Forget, endpoint replacement, or credential invalidation increments the trust generation and invalidates outstanding challenges. A late pairing result may never promote trust unless all of its challenge, session, endpoint, and trust-generation checks still match.

The session manager injects a monotonic `Clock` and `Scheduler`, so retry timers and stop timeouts are deterministic in tests. It also receives injected process, transport-event, capture-source, display-observer, and credential interfaces rather than calling OS tools directly.

#### Resource state model

The user-visible phase is derived from three independent resources:

- `TransportState`: `Idle`, `Pairing`, `Connecting`, `Streaming`, `Reconnecting`, `Stopping`, or `Failed`.
- `DisplayState`: `None`, `Resolving`, `Creating`, `Owned`, `Reconfiguring`, `Removing`, `Failed`, or `CleanupRequired`.
- `AudioState`: `Disabled`, `Starting`, `Enabled`, `Muted`, or `Unavailable`.
- `TrustState`: `Unknown`, `Untrusted`, `Trusted`, `Pairing`, `RevocationPending`, or `CleanupRequired`.

A Virtual display session may therefore be `Pairing + DisplayState.Owned`, `Reconnecting + DisplayState.Owned`, or `Failed + DisplayState.CleanupRequired` without contradictory resource claims. Trust cleanup is independently represented as `TrustState.CleanupRequired` even when no video or display resource remains. `SessionPhase` is one of `Available`, `Offline`, `ResolutionRequired`, `AudioDecisionRequired`, `CreatingDisplay`, `Pairing`, `Connecting`, `Mirroring`, `VirtualDisplay`, `Reconnecting`, `Reconfiguring`, `Recovering`, `Stopping`, `CleanupRequired`, `TrustCleanupRequired`, or `Failed`. The phase is a projection for the UI; resource and trust states are authoritative for cleanup.

An operation that owns a display always records whether the display is absent, being created, owned and usable, being removed, or cleanup-uncertain. A provider failure never reports `DisplayState.None` until the provider confirms that no owned output remains. A transport-only failure does not change `DisplayState.Owned`.

#### Lifecycle transition contract

Commands are serialized per receiver. A Stop command is accepted from every non-idle state and transitions to `Stopping`; repeated Stop commands are idempotent. Before any cancellation or superseding command, the manager increments the session generation, cancels timers, invalidates only the affected operation/lease/challenge, and stops accepting state events for the superseded intent. If a display is owned or being created, it first atomically publishes the display record's `desired_state=removing` intent; this records teardown without stopping the helper yet. It then runs cleanup in this order: stop/release transport, stop audio control, remove the owned display, wait for provider confirmation, then restore only eligible layout changes. A cleanup event belongs to the current stopping operation or exact resource handle and is not rejected merely because the superseded start operation is old.

| State and resources | Event or command | Transition and side effect |
|---|---|---|
| Available (`Idle`, `None`) | Start Mirror | Select a physical source and audio policy, then Pairing or Connecting |
| Available (`Idle`, `None`) | Start Virtual | Resolve `ResolutionDecision`, then Pairing or Creating display |
| Offline (`Idle`, `None`) | Receiver reappears | Update target generation, then Available and retry only after user intent permits |
| Offline (`Idle`, `None`) | Start | Refresh discovery/target; do not send to the stale endpoint |
| Resolution required | Choose | Begin Creating display for Virtual, or resolve Mirror source |
| Resolution required | Cancel | Available or Offline |
| Pairing (`None`) | Submit valid input | Transport pairing, then Connecting or Creating display |
| Pairing (`None`) | Cancel, expiry, invalid input budget exhausted | Available/Offline when backend cancellation is confirmed; otherwise TrustCleanupRequired, with no output |
| Pairing (`Owned`) | Submit valid input | Continue Connecting with the retained output |
| Pairing (`Owned`) | Cancel or expiry | Stopping; stop transport if any, remove the output, then Available/Offline or CleanupRequired; any failed trust cancellation also sets TrustState.CleanupRequired |
| Creating (`Creating` or partial) | Output published | Display becomes Owned, then Connecting |
| No active stream | Forget trust | Invalidate challenges, revoke/remove backend credential, then Available/Offline or TrustCleanupRequired |
| Creating (`Creating` or partial) | Cancel or provider failure | Stopping/Removing; only after confirmed removal may the phase become Available/Failed |
| Connecting (`None` or `Owned`) | Stream ready | Mirroring or VirtualDisplay; audio becomes Enabled/Muted or remains Unavailable |
| Connecting | Pairing required | Pairing with the current display resource preserved |
| Connecting | Network failure | Reconnecting; Virtual keeps Owned, Mirror keeps None |
| Connecting | Audio startup failure | AudioDecisionRequired; continue only after Retry audio, explicit video-only choice, or Cancel |
| Mirroring/VirtualDisplay | Network failure or receiver removal | Reconnecting; increment endpoint wait state, retain Virtual output |
| Mirroring/VirtualDisplay | Runtime audio failure | Keep video active, set AudioState.Unavailable, disable mute, offer Retry audio or Stop |
| Active state | Stop or confirmed mode switch | Stopping; no next mode starts before cleanup completes |
| Reconnecting | Network recovery | New operation ID and transport lease, refresh target, then Connecting without changing the session generation |
| Reconnecting | Stop | Stopping, then Available/Offline or CleanupRequired |
| Reconnecting | Retry budget for local recovery exhausted | Failed; preserve a valid Virtual output, or enter CleanupRequired if display ownership is uncertain |
| Any transport state | Daemon exit | Reconcile daemon instance, restart within the local restart budget, then reconnect each target independently or enter Failed |
| Any virtual state | Helper exit or KScreen disappearance | Set DisplayState.Failed, stop transport using the invalid source, then Remove or CleanupRequired |
| VirtualDisplay | Change resolution | Reconfiguring: stop stream, remove old output, create the new output, then reconnect |
| Reconfiguring | Cancel | Continue ordered removal/cleanup; then Available/Offline or CleanupRequired, never revive the old stream |
| Resolution mismatch | Recreate at accepted mode | Reconfiguring using the same ordered destroy/recreate transaction |
| Resolution mismatch | Stop/Cancel | Stopping; remove the retained output |
| Recovering | Matching helper/output adopted | Reconstruct the Virtual session with DisplayState.Owned and Reconnecting/Connecting transport |
| Recovering | Receiver unavailable | Offline/Reconnecting with the output retained and an explicit Stop action |
| Any state | Shutdown | Increment session generation, invalidate operation/lease/challenges, stop transport, remove owned outputs with bounded timeouts, then exit |
| Stopping | All resources confirmed released | Available, Offline, or the queued next mode |
| Stopping | Timeout or ambiguous ownership | CleanupRequired; never report false Available |
| CleanupRequired | Retry cleanup succeeds | Available/Offline |
| CleanupRequired | Keep/manual cleanup | Retain an explicit cleanup badge and do not claim ownership is released |
| TrustCleanupRequired | Retry revocation succeeds | Set TrustState.Untrusted, then Available/Offline |
| TrustCleanupRequired | Keep blocked/manual cleanup | Retain TrustState.CleanupRequired and prohibit pairing or stream start |

Network reconnect uses exponential delays capped at a fixed maximum. It does not become terminal merely because a TV is offline; it remains Reconnecting until Stop or an explicit unrecoverable classification. Bounded retry budgets apply to daemon restart and other local recovery attempts. When such a budget is exhausted, the session becomes Failed with its intent retained and an explicit Retry action. A Virtual display output is not removed by a transport-only Failed state; it is removed on Stop, valid helper-failure cleanup, session end, or user-approved orphan cleanup.

### Discovery adapter

The discovery adapter watches `_airplay._tcp` and `_raop._tcp` through mDNS, first aggregates related service records into a `ReceiverRecordSet`, then derives `ReceiverId` and emits added/updated/removed events. It normalizes display name, host/address, model, feature bits, service port, pairing hints, endpoint fingerprint, and last-seen state. Endpoint changes increment the record set's endpoint generation without changing a stable device identity.

Avahi or a Python zeroconf implementation may provide the first adapter. The domain interface must not expose raw Avahi or zeroconf records. Discovery does not claim a panel resolution unless the receiver information is trustworthy. Removal of a discovered record does not immediately stop an active session; active sessions enter Reconnecting, while an idle receiver becomes Offline.

### Transport adapter

`DoubleTakeTransport` owns one private `doubletake` daemon for the application and all receiver targets. It starts the daemon with a private socket under `$XDG_RUNTIME_DIR/airplay-linux/instances/<app_instance_uuid>/`, rather than attaching to an unrelated user daemon at the default socket. The app owns daemon startup, health monitoring, restart, and shutdown. The daemon's native multi-target support is used for concurrent streams.

The daemon is launched through an app-owned `DaemonSupervisor`, not directly from the GUI. On Linux the supervisor is a long-lived child process that sets `PR_SET_PDEATHSIG=SIGTERM` before launching `doubletake` in the foreground (the app never uses `-daemonize`), forwards termination, and escalates to `SIGKILL` after a bounded deadline. The supervisor records the owner UUID and child PID/start time. The runtime record is written atomically as `starting` before launch, then `running` only after the owner-token and control-socket handshake succeeds; normal shutdown writes `stopping` before requesting termination. A daemon that cannot be proven to belong to the current supervisor is never reused.

The GUI is single-instance. A `QLockFile` at `$XDG_RUNTIME_DIR/airplay-linux/airplay-linux.lock` contains an application instance UUID. Startup verifies the lock owner using PID liveness and `/proc/<pid>/stat` start time before reporting that another application instance is active. A stale lock is removed only after those checks. Each daemon instance has an owner token, supervisor PID/start time, daemon PID/start time when known, protocol version, private socket path, lifecycle state, and runtime directory record. A socket is accepted only after its path, owner record, PID/start time, and control handshake match the current app instance.

When a new GUI observes an old lock or runtime record, it first authenticates the exact prior owner UUID and supervisor/daemon PID-start pairs, requests shutdown, and waits for the private daemon and all of its streams to terminate before creating replacement streams. If the recorded owner is dead but a matching supervisor or daemon remains, it terminates that exact owned process using the owner token and verifies disappearance. A live mismatch or unresolvable prior owner is an ownership `CleanupRequired` condition; the new GUI does not attach, adopt streams, or start replacement streams. Records in `starting` or `stopping` are reconciled using the same process and socket evidence, including crashes around record publication. A stale socket or mismatched handshake is removed only after the recorded process is proven dead.

`ReceiverTarget` binds every command to the current endpoint:

```text
ReceiverTarget(
    receiver_id,
    service_records,
    canonical_host,
    service_port,
    protocol,
    endpoint_fingerprint,
    endpoint_generation,
)
```

The target is built from the current merged `ReceiverRecordSet`. An endpoint change increments `endpoint_generation`; commands already sent to the old generation may finish only for cleanup, while new start, pair, mute, and reconnect commands require the current target. The adapter rejects a target whose receiver ID, endpoint fingerprint, or generation is stale. A new device ID at the same endpoint is not allowed to receive commands through the old target. Display-helper events are not target events: they are accepted or rejected using the display ownership record and helper identity described below.

The typed transport interface is conceptually:

```text
pair(target, pairing_input, operation_context) -> PairingResult
query_modes(target, operation_context) -> ReceiverCapabilities or Unknown
start_stream(target, source, audio_policy, operation_context) -> StreamHandle
stop_stream(stream_handle, operation_context) -> Result
set_muted(stream_handle, muted, operation_context) -> Result
start_audio(stream_handle, operation_context) -> Result
subscribe() -> TransportEvent
```

`source` is either `PhysicalMonitorSource(connector, portal_restore_token)` or `VirtualOutputSource(connector, kscreen_output_id, size)`. `StreamHandle` contains the receiver ID, target endpoint generation, backend target/stream ID, and daemon instance UUID. `OperationContext` contains the operation ID, session generation, transport lease ID when applicable, and trust generation when pairing is involved. Every transport event contains the receiver ID, stream ID when applicable, target endpoint fingerprint/generation, daemon instance UUID, operation ID, and the applicable session/lease or challenge token. Display-helper events instead contain the display owner UUID, connector/output ID, helper PID/start time, and typed display classification. Old transport events are ignored for current state, but exact-handle cleanup acknowledgements remain consumable by the stopping transaction.

The adapter uses the published `doubletake`/`doubletake-ctl` command and socket contract. Startup verifies that required commands and control operations are present; an incompatible backend is an actionable dependency error. Human-oriented output parsing is confined to this adapter. The adapter must not claim a physical source succeeded unless the backend accepted or verified the physical source descriptor. If the installed backend cannot do this, Mirror is explicitly unsupported while other modes remain available.

A target-level backend failure removes only that target stream. A daemon process failure affects all streams because they share the daemon; the session manager preserves each session's intent, restarts the daemon within its restart budget, and reconnects streams independently against current `ReceiverTarget` values. Other sessions remain represented and do not get torn down merely because one target failed. A second application instance never attaches to or shares this daemon.

Pairing is owned by this transport coordinator. The session manager submits one `PairingChallenge` or pairing input to `pair`; it does not call a separate credential adapter to perform a second pairing operation. Credential storage is an implementation dependency of the coordinator and is described below.

### KDE virtual-display adapter

`KdeVirtualDisplayProvider` is the required v1 provider for KDE Plasma 6 Wayland. It:

1. verifies that the current session is KDE Wayland;
2. verifies that `krfb-virtualmonitor` and `kscreen-doctor` are available;
3. acquires a provider-wide creation lock so virtual-output creation is serialized across receivers;
4. snapshots the relevant KScreen layout and atomically reserves an ownership record before launching a helper;
5. launches one owned helper process for the requested virtual output;
6. atomically records the helper PID and process start time, then observes KScreen output data against the pre-create baseline;
7. accepts exactly one new connector/output matching the helper's requested size and reservation; ambiguity is a failure, never an arbitrary claim;
8. atomically records the observed output and configures it through `kscreen-doctor`;
9. publishes the completed ownership record and returns a `VirtualOutputSource`;
10. removes the output by stopping the owned helper and waiting for KScreen removal;
11. restores only the provider-owned layout changes when possible.

The reservation protocol uses durable atomic writes (temporary file, `fsync`, rename, and directory `fsync`) with these stages: `reserved`, `helper_started`, `output_observed`, `published`, and `removing`. Every record also has a durable `desired_state`: `active` or `removing`. The record is reserved before helper launch, so a crash cannot create an output without at least a durable ownership intent. Before any Stop, cancellation, failed reconfiguration, or cleanup, the provider atomically changes `desired_state` to `removing` and stage to `removing` before stopping transport or the helper. A restart never reconnects a record whose desired state is `removing`.

The helper is launched with `AIRPLAY_LINUX_OWNER_UUID` and the exact app-instance runtime directory in its environment; startup reconciliation may match a live helper by that owner UUID plus PID/start time, never by command name alone. The provider also scans the owner UUID in the helper environment/process record for the crash window between launch and PID persistence. If any stage update fails, the provider stops the exact helper it launched, waits for output removal, and reports `CleanupRequired` if removal cannot be confirmed. Recovery does not treat `output_observed` as usable until the requested KScreen configuration is verified and the record is republished.

The persisted non-secret ownership record contains:

- `owner_uuid` generated at reservation;
- `desired_state`, either `active` or `removing`;
- `receiver_id` and endpoint fingerprint;
- connector name, KScreen output ID when known, and configured size;
- requested and observed resolution;
- helper PID and process start time, when persisted;
- creation timestamp, reservation stage, and last completed configuration step;
- pre-create layout snapshot and baseline hash;
- last provider-applied layout values;
- provider version/capability fingerprint.

The provider-wide lock covers baseline capture, helper launch, output observation, and publication, so two receivers cannot both claim the next new output. It supports one helper/output per virtual session and verifies distinct output IDs for concurrent virtual displays. If the installed helper cannot provide a distinct second output, the provider reports that capability failure for the new request without disturbing existing sessions.

Startup reconciliation handles every stage and evidence combination:

- `reserved` with no matching helper or output is discarded only after owner/process liveness checks; a live helper found by owner UUID is promoted to `helper_started` and its PID/start time is persisted before further action;
- `helper_started` with a live exact helper and no output is waited on briefly; if the output does not appear, the record enters `CleanupRequired` and the helper is stopped rather than being published;
- `helper_started` or `output_observed` with a live exact helper and a matching output completes and verifies the requested KScreen configuration under the provider lock, then atomically publishes; it is not usable during this repair;
- `output_observed` with an output but incomplete or mismatched configuration is repaired only if the exact helper and ownership evidence still match; otherwise it becomes an orphan candidate or `CleanupRequired` and is never adopted as usable;
- `published` with `desired_state=active`, a live exact helper, and an exact configured output enters `Recovering`; the session manager reconstructs the Virtual session with `DisplayState.Owned` and reconnects transport only after the display source is verified;
- any stage with `desired_state=removing`, including `removing`, resumes ordered removal and never reconnects transport, even if the helper and output are both still live;
- a matching output with no live helper becomes an `Orphan candidate` and requires user approval before removal; it is not silently adopted;
- a helper with no output and no usable ownership record is stopped after exact owner checks; an output with no ownership record is never removed automatically and remains an unrelated output unless the user explicitly chooses cleanup through a separate system tool;
- helper/output evidence found during the launch-to-PID-persistence crash window is reconciled by owner UUID, exact process start evidence, and connector/size matching, not by command name or newest-output heuristics.

A recovered active session can be paired or reconnected independently of display ownership. If its receiver is not currently discovered, it displays `Recovering/Offline`, retains the output, and retries only when the receiver reappears or the user selects Retry. If trust is invalid, the output remains owned while the session enters Pairing with the output-retained variant. A recovered session can always be stopped through the same ordered cleanup path.

Layout restoration is constrained by the saved baseline. On teardown, the provider restores only output properties that it changed and only when those properties still equal the provider's last-applied values. User changes made after creation, and changes to unrelated outputs, are preserved. The provider owns only outputs represented by its exact record and observed connector/output identity. It does not remove unrelated physical or virtual outputs.

A helper or KScreen timeout records `CleanupRequired`, never claims success, and leaves the user an actionable cleanup/retry path.

### Capture-source adapter

A `CaptureSourceProvider` enumerates physical monitor connectors and requests the Wayland portal source when a restore token is not available. It filters out application windows and virtual outputs before the session manager sees the choices. Portal permission denial or cancellation is a typed recoverable event. The provider is also the seam used to verify that a source passed to `DoubleTakeTransport` is physical.

### Credential adapter

The transport coordinator is the sole pairing authority. The credential adapter is storage and backend-transaction plumbing only; it exposes no independent pairing challenge API. It must provide a trust transaction with an explicit commit fence:

```text
load_credential_ref(receiver_id) -> OpaqueCredentialRef or Missing
prepare_pairing(challenge, backend_result) -> OpaqueProvisionalCredentialRef
commit_pairing(provisional_ref, trust_generation) -> Result
cancel_pairing(challenge) -> Result
revoke_pairing(provisional_ref_or_receiver_id) -> Result
mark_invalid(receiver_id) -> Result
```

The backend owns secret reads, writes, rotation, and revocation through its keyring mode. `submit_pairing` may produce an opaque provisional backend result, but the coordinator promotes it to trusted only at the commit point after `commit_pairing` succeeds while the challenge is active and its `trust_generation`, session generation, endpoint generation, and receiver ID still match. A one-shot backend that may persist before the commit fence must support cancellation or compensating revocation; if neither is available, pairing is reported unsupported rather than claiming a cancellable trust flow. Secrets and provisional credentials remain inside the backend/keyring adapter and are never copied into application metadata.

The coordinator exposes the pairing flow to the session manager:

```text
begin_pairing(target, operation_context) -> PairingChallenge
submit_pairing(challenge, pin_or_password, operation_context) -> PairingResult
forget_trust(receiver_id) -> Result
```

A `PairingChallenge` contains a challenge ID, receiver ID, endpoint fingerprint, operation ID, session generation, trust generation, creation time, expiry time, and an opaque backend correlation token. The session manager accepts user input only through this coordinator. The coordinator rejects expired, cancelled, wrong-target, and stale-generation submissions before contacting the backend. Pairing results are typed as `Trusted`, `InvalidInput`, `ReceiverRejected`, `Cancelled`, `CredentialUnavailable`, `TrustCleanupRequired`, or `TransportFailure`.

Cancel or expiry invalidates the challenge and increments the trust generation. Forget first invalidates every outstanding challenge for that receiver, requests backend cancellation, and waits for each in-flight operation to finish or be compensatingly revoked. Forget is allowed when no stream is active, including while pairing is pending, but it is not reported successful until backend credential removal is confirmed; only then are the opaque local trust reference and `TrustState` cleared. A late backend success can never promote trust after invalidation; the coordinator revokes it using the opaque provisional reference. Failed cancellation or revocation enters `Trust cleanup required`, blocks new pairing for that receiver, and leaves an explicit retry/revocation action. Forget while a stream is active still requires Stop to complete first. Invalid input has a bounded prompt-attempt budget.

A backend credential-invalid event clears the opaque trust reference, increments the trust generation, transitions the transport resource to Pairing, and preserves a valid Virtual output while awaiting new trust. Rotation is represented as a new pairing challenge and never silently reuses the old credential. PINs, passwords, opaque backend secrets, correlation tokens, and provisional credential contents are never written to application metadata, the activity log, test snapshots, or exception messages.

### Audio policy

The default `AudioPolicy` is enabled. Startup probes PipeWire and portal capability before a normal audio-enabled start. If audio is unavailable or permission is denied before video is connected, the normal start is blocked in `AudioDecisionRequired`; the user may Retry audio, explicitly choose `Start video only`, or Cancel. That choice is recorded in the session state and activity log; it is never an implicit downgrade.

Once video is active, an audio failure is video-preserving. The transport resource remains `Streaming`, the audio resource becomes `Unavailable`, mute controls are disabled, and the selected pane shows `Audio unavailable` with Retry audio and Stop. Retry audio creates a new operation context for the audio sub-operation only; success moves audio to Enabled or Muted without interrupting video. A failed retry leaves video and the Unavailable state intact. Audio recovery from a backend event follows the same state update and never changes another receiver session.

Mute/unmute is a target-local command and is session-only. A mute failure leaves the prior known state unchanged and reports the backend error. Audio capability, permission, startup decision, runtime failure, and mute failure use typed transport events.

## State and Data Flow

The primary user-visible flow is a projection of independent resources:

```text
Available
  | start Mirror
  v
Pairing/Connecting ---> Mirroring ---> Reconnecting --+
  |                         |                         |
  +-------------------------+------ retry ------------+

Available
  | start Virtual display
  v
Resolve mode -> Create output -> Connecting -> Virtual display
                         |             ^             |
                         v             |             v
                   Pairing +       Reconnecting (output retained)
                   output owned

Virtual display --change resolution--> Reconfiguring
                                      -> new output -> Connecting
```

Important rules:

- Pairing may occur before output creation or after a backend challenge during connection. The display resource is explicitly `None` or `Owned`; cancellation with `Owned` always removes it through `Stopping`.
- Missing dependencies and unsupported capabilities are explicit terminal states for the attempted command, not global application failures.
- A failed receiver session does not stop or alter another receiver session.
- Virtual display reconnects preserve the output and window layout after transport-only failure.
- Mirror reconnects have no output to preserve; transport may retry without creating a display.
- Resolution reconfiguration destroys and recreates the output in an ordered transaction; it does not run alongside the old stream.
- Runtime audio failure preserves video and changes only the audio resource.
- Stop is idempotent and always releases transport and display resources owned by the session.
- Every asynchronous event is checked against the token for its resource: receiver ID plus endpoint/transport lease and daemon instance for transport, display owner UUID plus helper/output identity for display, and challenge/trust generation for pairing. A stale event cannot change current state, while an exact-handle cleanup acknowledgement can finish the stopping transaction.

A persisted session record contains at least:

- stable receiver ID, merged service metadata, and current `ReceiverTarget` endpoint generation;
- current mode, intent, `SessionPhase`, `TransportState`, `DisplayState`, `AudioState`, and `TrustState`;
- selected physical monitor or virtual-output handle;
- `ResolutionDecision` and observed resolution;
- audio policy and muted state;
- owned daemon instance and backend stream identifiers;
- owned virtual-display process/output identifiers and ownership stage;
- last error code, user-facing message, and recovery action;
- reconnect attempt and next retry time;
- current operation ID, session generation, transport lease ID, and trust generation when applicable;
- queued next mode only while a confirmed Stop/reconfiguration transaction is active.

The persisted ownership record is authoritative for virtual-output recovery; the session record references it but never treats an unconfirmed output as released.

## Resolution Policy

A `ResolutionDecision` contains a width, height, source, and evidence. Its source is one of `stored`, `discovery`, or `user`.

The app resolves the mode before creating a virtual output:

1. use a remembered mode only when it is associated with the same stable receiver identity;
2. otherwise use a reliable receiver mode from discovery or the transport adapter's pre-stream `query_modes` result;
3. if no reliable pre-stream value exists, require the user to choose from an explicit mode picker;
4. remember the chosen or verified mode per receiver after successful setup;
5. never guess a mode from a display name, stale endpoint, or generic TV category.

Changing a virtual display resolution is a `Reconfiguring` transaction. The UI warns that windows may need repositioning. The session manager increments the session generation, invalidates the active transport lease and pairing challenges, publishes the old display's `desired_state=removing` intent, stops transport, waits for stop confirmation, marks the old display `Removing`, stops the helper, waits for KScreen removal, reserves and creates the new output, then reconnects using a new operation ID and transport lease. The old and new outputs are never owned as active alternatives by the same session, and cancellation after teardown ends in Available/Offline or CleanupRequired rather than reviving the old stream.

If the transport reports a reliable accepted mode after setup and it differs from the decision, the session stops the stream and enters `Resolution mismatch` with the current output retained but not streamed. The user can explicitly choose `Recreate at accepted mode`, which performs the same destroy/recreate transaction, or Stop, which removes the retained output. The application never changes the output silently. A failed recreation reports the exact display/transport resource states and offers Retry or cleanup.

## Error Handling and Recovery

- No discovered receivers is a normal empty state; a saved receiver with no current record is `Offline`.
- Avahi or network errors appear as retryable discovery errors while known devices remain visible.
- Removal of a receiver record does not stop an active session; it invalidates the endpoint generation and moves transport to Reconnecting while a Virtual output remains owned.
- Pairing prompts appear only when requested by the receiver. Pairing cancellation, expiry, wrong-target input, and stale-generation input do not mutate trust; late backend completion is cancelled or compensatingly revoked under the trust-generation fence.
- Transport errors remain local to the receiver and expose retry/stop actions.
- Missing `krfb-virtualmonitor` disables only Virtual display mode.
- Virtual-display creation and reconfiguration are transactional. Failure or cancellation stops the exact helper, waits for output removal, and records CleanupRequired if the timeout expires.
- Network loss in Virtual display mode shows `Reconnecting`, keeps the output, and retries with bounded exponential backoff.
- A transport daemon exit reconciles the private daemon instance and restarts within a bounded local-recovery budget; each target then reconnects independently against its current endpoint.
- A helper exit or KScreen disappearance invalidates the virtual source, stops transport, and enters display removal or CleanupRequired. It never claims that the old output is still usable.
- Normal shutdown increments session generations, invalidates transport leases and pairing challenges, stops transports, removes owned virtual outputs with bounded cleanup timeouts, and records unresolved resources before exit.
- Runtime audio failure keeps an established video stream active, sets AudioState.Unavailable, disables mute, and offers Retry audio or Stop. Audio failure before video readiness requires an explicit Retry audio, Start video only, or Cancel decision.
- Startup scans ownership records and exact helper/output evidence. Matching records enter Recovering; output-without-helper records become orphan candidates; unowned outputs are never removed automatically.
- Portal cancellation, keyring failure, unsupported source, unsupported mode, audio denial, receiver rejection, stale target, stale socket, helper failure, and cleanup timeout are distinct actionable errors.

The activity log records timestamped lifecycle and resource events without secrets. The selected receiver pane contains the current actionable error, resource badges, and recovery action, not just a raw process exit code.

## Dependencies and Environment Checks

At startup the application checks:

- PySide6 runtime;
- Avahi/mDNS availability or the selected discovery implementation;
- `doubletake` and `doubletake-ctl` compatibility with required control operations;
- `kscreen-doctor` for KDE display management;
- `krfb-virtualmonitor` for Virtual display mode;
- Wayland/X11 session information;
- PipeWire and the desktop portal for Wayland capture and audio;
- a usable keyring backend for remembered pairing;
- support for the required number of concurrent virtual outputs.

The app reports exact missing commands and keeps unrelated functionality enabled where possible. It does not install system packages automatically.

## Testing Strategy

### Deterministic seams

The session manager injects:

- a monotonic clock and scheduler;
- process runner and process-exit event source;
- discovery event source and merged-record identity resolver;
- transport command/event source and daemon-instance registry;
- portal/capture-source provider;
- KScreen observer, provider-wide creation lock, ownership store, and command runner;
- credential/keyring storage provider;
- persistent session and ownership records.

Each seam has a fake implementation with controllable events, delays, failures, process start times, endpoint changes, and late/stale callbacks. The fake KScreen provider can interleave two receiver creation attempts and inject a crash at every ownership-record stage.

### Unit tests

- mDNS record aggregation across AirPlay and RAOP, identity derivation, port/hostname changes, identity replacement, collision handling, trust non-inheritance, and stale-device handling;
- capability and `ResolutionDecision` selection without circular negotiation or guessed modes;
- mode exclusivity per receiver and concurrent sessions across receivers;
- command validation for physical-only Mirror sources;
- state/resource transitions for every row in the lifecycle table, including Pairing with an owned output, Offline, Recovering, Reconfiguring, AudioDecisionRequired, Audio unavailable, CleanupRequired, TrustCleanupRequired, and stop timeout;
- command and lease validation rejects stale callbacks, stale target endpoint events, stale daemon events, and stale pairing submissions;
- cancellation and idempotent Stop from every asynchronous state, with transport-before-display cleanup ordering;
- reconnect scheduling, cap, retry classification, receiver disappearance, endpoint replacement, and cancellation;
- ownership reservation, every-stage crash-point recovery including launch-before-PID-persistence and `removing`, atomic publication, desired teardown intent, layout restoration guards, and orphan-reconciliation decisions;
- keyring metadata boundaries, trust-generation fences, challenge expiry, in-flight cancel/late completion, forget/revoke/rotation, invalid credential recovery, and secret non-logging;
- audio policy, explicit video-only fallback, runtime audio failure with video retention, audio retry, mute failure, and state retention;
- stale daemon lock/socket handling, owner-death supervisor termination, PID start-time verification, crash recovery around record publication, and single-instance rejection.

### Adapter contract tests

- fake discovery emits deterministic cross-service merged records and endpoint generations;
- fake transport verifies sole-authority pairing, trust commit/cancel/revoke fences, target binding, typed source validation, start/stop, mute, runtime audio recovery, reconnect, wrong-target rejection, daemon restart, and two concurrent receiver targets;
- fake process runner covers missing commands, non-zero exit, timeout, malformed output, abrupt termination, daemon restart, stale PID reuse, owner-token mismatch, owner-death supervisor termination, and GUI crash with surviving daemon streams;
- fake capture provider rejects windows and virtual outputs and supplies only physical monitor descriptors;
- fake KDE provider verifies provider-wide creation serialization, reservation stages, helper-to-output correlation, distinct concurrent output IDs, crash adoption, reconnect retention, transactional rollback, layout restoration guards, timeout, and teardown ordering;
- fake keyring covers load, prepare/commit, cancel, revoke, forget, rotation, permission failure, challenge invalidation, and no-plaintext guarantees.


### Environment integration tests

- run the installed `doubletake` and `doubletake-ctl` help and control-socket contract checks;
- run KScreen JSON parsing against a fixture modeled on `kscreen-doctor -j`;
- run a fake `krfb-virtualmonitor` process through the real provider, including owner UUID and process-start-time reconciliation;
- run a KDE Plasma Wayland smoke test when the helper and a test-safe virtual output are available;
- verify startup orphan reconciliation never removes an unowned output and adopts only an exact live owner record;
- verify the app remains usable in the current environment where `krfb-virtualmonitor` is absent;
- exercise two concurrent targets, target-local failure isolation, system audio/mute, runtime audio loss with video retention, reconnect retention, resolution recreation, shutdown, endpoint replacement, and source enforcement against a supported backend contract;
- verify a second application instance cannot attach to the first instance's daemon or socket and startup terminates or blocks on a surviving prior owner;

### UI tests

Qt tests cover every row in the UI matrix: pairing before and after output creation, mode-selector lockout, explicit stop-before-switch, resolution picker, portal cancellation, Offline and Recovering, reconnect presentation with retained output, Reconfiguring, resolution mismatch, dependency errors, orphan cleanup confirmation, CleanupRequired, audio-unavailable video retention and retry, stale pairing submission, forget/revoke flow, and per-session mute. Tests assert that text and controls never expose a conflicting mode action while resources are stopping or uncertain.

Acceptance requires multiple independent receiver sessions, strict per-receiver mode exclusivity, physical-only Mirror source validation, serialized and recoverable virtual-output creation, exact crash adoption/cleanup behavior, virtual-output creation and cleanup, remembered pairing without plaintext metadata, actionable dependency errors, deterministic cancellation behavior, runtime audio failure without video loss, and isolation of one receiver's failure from another.

## Risks and Boundaries

- AirPlay receiver compatibility varies by vendor firmware even when the receiver advertises AirPlay services. The GUI must surface backend rejection details.
- KDE's virtual-output support is helper-backed and version-sensitive. The provider must use capability detection and avoid private KWin protocol assumptions where a supported helper is available.
- Wayland capture is mediated by the desktop portal. A backend limitation that prevents selecting or verifying a requested physical monitor must be surfaced as unsupported rather than silently selecting a different source.
- AirPlay resolution may be negotiated rather than advertised. The explicit pre-stream fallback picker is required for correctness.
- The application cannot guarantee recovery from a desktop session crash; persisted ownership records and startup reconciliation are the recovery boundary.
- mDNS identity and remembered trust are security-sensitive; spoofed or replaced receivers require explicit identity-change handling.
- Real hardware and real network tests remain necessary for receiver-specific pairing, negotiated resolutions, reconnect behavior, and system-audio compatibility.

## Verification Checklist

Before implementation planning, the design is internally consistent:

- Mirror and Virtual display are separate per-receiver modes.
- Only Mirror uses physical-monitor sources.
- Virtual display owns its KDE output lifecycle and preserves it during transport-only reconnect.
- Transport, display, and audio resources have independent authoritative states, including output-retained pairing and video-preserving audio failure.
- KDE Plasma 6 Wayland is guaranteed for v1; other environments are capability-gated.
- Receiver identity is derived after conservative cross-service aggregation, and endpoint/transport lease tokens reject stale commands without invalidating display ownership events.
- Provider-wide virtual-output reservation, atomic ownership publication, every-stage crash recovery, durable teardown intent, and conservative orphan cleanup are explicit.
- Daemon owner-death supervision and previous-instance reconciliation prevent competing private daemons.
- Pairing has an explicit trust commit fence, cancellation/revocation compensation, and TrustCleanupRequired outcome.
- Resolution is resolved before output creation and selected explicitly when no reliable value exists.
- Stale asynchronous events cannot revive a stopped or superseded operation.

- Daemon and helper ownership, failure classification, and cleanup timeouts are explicit.
- Pairing, trust, identity replacement, audio fallback, and mute semantics are explicit.

- External AirPlay protocol work remains inside the transport adapter.
- All side effects have fakeable adapter seams and corresponding tests.
