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

`ReceiverId` is derived in this order:

1. Prefer the normalized AirPlay `deviceid` or MAC-like identifier from service TXT records.
2. If no device identifier exists, use `airplay-fingerprint:<sha256>` over the canonical service type, normalized host name, model, and service port.

A service-name change does not change an ID when the device identifier is present. A new device identifier at the same host is a new receiver and cannot silently inherit trust. For a fingerprint-only receiver, a changed canonical fingerprint is shown as a possible receiver replacement and requires explicit re-pairing. Collisions are handled by retaining the full canonical record and requiring user confirmation before merging.

The application stores the receiver ID, display name, last endpoint, pairing/trust metadata, and non-secret preferences. It never stores a PIN or credential in this record.

### Mirror mode

Mirror mode accepts a physical monitor source only. It creates no virtual output. Starting the mode starts a `doubletake` transport session with system audio enabled by default. The user may stop the session or mute its audio without affecting other receiver sessions.

On Wayland, physical-monitor selection is subject to the compositor's screen-capture portal. The domain contract uses a typed `PhysicalMonitorSource` containing a connector identity and, where required, a portal restore token. The transport adapter must expose only physical monitor sources to the session manager and must reject virtual-output or application-window sources. If the active backend cannot accept or verify a requested physical monitor descriptor, Mirror start enters an actionable unsupported state rather than silently capturing another source.

### Virtual display mode

Virtual display mode creates a KDE virtual output at the receiver's resolved resolution, configures it as a usable desktop monitor, and streams that output to the receiver. The output remains available to applications while the mode is active and during a transient network reconnect.

The domain contract uses a typed `VirtualOutputSource` containing the connector identity, KScreen output ID, and configured size. The output is owned by the session that created it.

Stopping the mode stops transport first and then removes the output owned by that session. A transport-only network failure preserves the output. A virtual-display helper failure is different: because the output may no longer exist, the session records a display failure and performs cleanup/reconciliation rather than pretending the display remains usable. A stale output found on a later startup is identified and offered for cleanup rather than being deleted without user consent.

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

| Domain state | Selected-pane presentation | Allowed primary actions |
|---|---|---|
| Available | Mode selector, source or resolution controls, Pair if needed | Start, Pair, Scan |
| Pairing required | PIN/password prompt, receiver identity, Cancel | Submit PIN, Cancel, Retry |
| Resolution required | Supported-mode list or explicit picker, remembered choice indicator | Choose, Cancel |
| Creating output | Progress and cancel action; no mode switch | Cancel |
| Connecting | Progress, selected source, audio policy | Stop |
| Mirroring | Physical source, active audio/mute control, Stop | Mute/Unmute, Stop |
| Virtual display | Output size, active audio/mute control, Stop | Mute/Unmute, Stop, Change resolution |
| Reconnecting | `Reconnecting` state, last error, next retry, Retry and Stop | Retry, Stop |
| Resolution mismatch | Reported accepted mode, window-layout warning | Recreate at accepted mode, Cancel |
| Dependency or capability failure | Exact missing capability and recovery hint | Retry after fix, Back |
| Orphan candidate | Connector/output details and ownership evidence | Remove after confirmation, Keep |
| Failed | Error detail, retained session intent, Retry and Stop | Retry, Stop |

The mode selector is disabled while a session is active, creating an output, or stopping. A requested mode switch first requires Stop and waits for teardown completion; the application never starts the next mode concurrently with cleanup from the previous one.

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
- serialized commands per receiver.

Each command allocates an `operation_id` and increments the receiver's `operation_generation`. Adapter events carry the operation ID and receiver ID. Events from older generations are ignored, so a late connect or process-exit event cannot revive a session after Stop or mode switch.

The session manager injects a monotonic `Clock` and `Scheduler`, so retry timers and stop timeouts are deterministic in tests. It also receives injected process, transport-event, capture-source, display-observer, and credential interfaces rather than calling OS tools directly.

### Lifecycle transition contract

Commands are serialized per receiver. A Stop command is accepted from every non-idle state and transitions to `Stopping`; repeated Stop commands are idempotent. The following table defines the important transitions:

| State | Event or command | Transition and side effect |
|---|---|---|
| Available | Start Mirror | Resolve physical source and audio, then Pairing or Connecting |
| Available | Start Virtual | Resolve `ResolutionDecision`, then Creating output |
| Pairing | Submit PIN | Pair through transport, then Connecting |
| Pairing | Cancel or receiver disappears | Available or Offline; no output exists |
| Resolution required | Choose or Cancel | Creating output, or Available |
| Creating output | Output ready | Connecting with `VirtualOutputSource` |
| Creating output | Cancel or provider failure | Stopping/cleanup, or Failed with no owned output |
| Connecting | Stream ready | Mirroring or Virtual display |
| Connecting | Pairing required | Pairing; preserve virtual output if already created |
| Connecting | Network failure | Reconnecting; preserve virtual output |
| Connecting | Stop | Stopping |
| Active Mirror | Network failure | Reconnecting; no display cleanup needed |
| Active Virtual display | Network failure | Reconnecting; keep the output and layout |
| Active state | Stop or mode switch | Stopping, then Available or next requested mode |
| Reconnecting | Network recovery | Connecting with a new operation generation |
| Reconnecting | Stop | Stopping, then Available |
| Reconnecting | Unrecoverable error | Failed; retain virtual output only if it remains valid |
| Any transport state | Daemon exit | Restart app-owned daemon within a bounded restart budget, otherwise Failed |
| Any virtual state | Helper exit | Display failure; reconcile output and move to Failed or cleanup-required |
| Any state | Shutdown | Stopping with bounded cleanup, then process exit |
| Stopping | Cleanup complete | Available, Offline, or next mode |
| Stopping | Timeout | Force cleanup and record Cleanup required |

Network reconnect uses exponential delays capped at a fixed maximum. It does not become terminal merely because a TV is offline; it remains Reconnecting until Stop or an explicit unrecoverable classification. Bounded retry budgets apply to daemon restart and other local recovery attempts. When such a budget is exhausted, the session becomes Failed with its intent retained and an explicit Retry action. A Virtual display output is not removed by a transport-only Failed state; it is removed on Stop, valid helper failure cleanup, session end, or user-approved orphan cleanup.

### Discovery adapter

The discovery adapter watches `_airplay._tcp` and `_raop._tcp` through mDNS, merges records by `ReceiverId`, and emits added/updated/removed events. It normalizes display name, host/address, model, feature bits, service port, pairing hints, and last-seen state.

Avahi or a Python zeroconf implementation may provide the first adapter. The domain interface must not expose raw Avahi or zeroconf records. Discovery does not claim a panel resolution unless the receiver information is trustworthy. Removal of a discovered record does not immediately stop an active session; active sessions enter Reconnecting, while an idle receiver becomes Offline.

### Transport adapter

`DoubleTakeTransport` owns one private `doubletake` daemon for the application and all receiver targets. It starts the daemon with a private socket below `$XDG_RUNTIME_DIR/airplay-linux/`, rather than attaching to an unrelated user daemon at the default socket. The app owns daemon startup, health monitoring, restart, and shutdown. The daemon's native multi-target support is used for concurrent streams.

The typed transport interface is conceptually:

```text
pair(receiver_id, pairing_input) -> PairingResult
query_modes(receiver_id) -> ReceiverCapabilities or Unknown
start_stream(receiver_id, source, audio_policy) -> StreamHandle
stop_stream(stream_handle) -> Result
set_muted(stream_handle, muted) -> Result
subscribe() -> TransportEvent
```

`source` is either `PhysicalMonitorSource(connector, portal_restore_token)` or `VirtualOutputSource(connector, kscreen_output_id, size)`. `StreamHandle` contains the receiver ID and backend target/stream ID. Every event contains the receiver ID, stream ID when applicable, operation ID, and a typed classification such as `pairing_required`, `network_failure`, `audio_unavailable`, `unsupported_source`, `daemon_failure`, or `receiver_rejected`.

The adapter uses the published `doubletake`/`doubletake-ctl` command and socket contract. Startup verifies that required commands and control operations are present; an incompatible backend is an actionable dependency error. Human-oriented output parsing is confined to this adapter. The adapter must not claim a physical source succeeded unless the backend accepted or verified the physical source descriptor. If the installed backend cannot do this, Mirror is explicitly unsupported while other modes remain available.

A target-level backend failure removes only that target stream. A daemon process failure affects all streams because they share the daemon; the session manager preserves each session's intent, restarts the daemon within its restart budget, and reconnects streams independently. Other sessions remain represented and do not get torn down merely because one target failed.

### KDE virtual-display adapter

`KdeVirtualDisplayProvider` is the required v1 provider for KDE Plasma 6 Wayland. It:

1. verifies that the current session is KDE Wayland;
2. verifies that `krfb-virtualmonitor` and `kscreen-doctor` are available;
3. snapshots the relevant KScreen layout before creation;
4. launches one owned helper process for the requested virtual output;
5. observes KScreen output data until a new output appears;
6. enables and configures the new connector through `kscreen-doctor`;
7. returns a `VirtualOutputSource` and persisted ownership record;
8. removes the output by stopping the owned helper and waiting for KScreen removal;
9. restores the pre-create physical-output layout when possible.

The provider must observe an output not already owned by another active session. It supports one helper/output per virtual session and verifies distinct output IDs for concurrent virtual displays. If the installed helper cannot provide a distinct second output, the provider reports that capability failure for the new request without disturbing existing sessions.

The persisted non-secret ownership record contains:

- `owner_uuid` generated at creation;
- `receiver_id`;
- connector name and KScreen output ID;
- requested and observed resolution;
- helper PID and process start time;
- creation timestamp;
- pre-create layout snapshot;
- provider version/capability fingerprint.

Startup reconciliation follows these rules:

- matching record, output, and live helper: adopt the existing output;
- matching record and output but no helper: show an orphan candidate and ask before removal;
- record with no output: discard the stale record after logging it;
- output with no ownership record: never remove automatically; leave it alone and report it only when relevant.

A helper or KScreen timeout records `Cleanup required`, never claims success, and leaves the user an actionable cleanup/retry path. The provider owns only outputs represented by its record and exact observed connector/output identity. It does not remove unrelated physical or virtual outputs.

### Capture-source adapter

A `CaptureSourceProvider` enumerates physical monitor connectors and requests the Wayland portal source when a restore token is not available. It filters out application windows and virtual outputs before the session manager sees the choices. Portal permission denial or cancellation is a typed recoverable event. The provider is also the seam used to verify that a source passed to `DoubleTakeTransport` is physical.

### Credential adapter

The transport backend's keyring mode is the preferred secret store. The application-facing credential interface exposes trust state and lifecycle operations without exposing stored credentials:

```text
has_trust(receiver_id) -> bool
begin_pairing(receiver_id) -> PairingChallenge
submit_pairing(challenge, pin_or_password) -> PairingResult
forget(receiver_id) -> Result
```

The backend owns secret reads, writes, rotation, and revocation. The app stores only whether a receiver is trusted and the backend's opaque credential reference. Pairing cancellation returns to Available without recording a failure. Invalid credentials are retryable with a bounded prompt-attempt budget; keyring unavailability is a dependency error and never falls back to plaintext.

### Audio policy

The default `AudioPolicy` is enabled. Startup probes PipeWire and portal capability before a normal audio-enabled start. If audio is unavailable or permission is denied, the normal start is blocked with an actionable reason and the user may explicitly choose `Start video only`. That choice is recorded in the session state and activity log; it is never an implicit downgrade.

Mute/unmute is a target-local command and is session-only. A mute failure leaves the prior known state unchanged and reports the backend error. Audio capability, permission, and mute failures use typed transport events and do not stop unrelated receiver sessions.

## State and Data Flow

The primary state flow is:

```text
Available
  | start Mirror
  v
Pairing/Connecting ---> Mirroring ---> Reconnecting --+
  |                                                   |
  +------------------------- retry -------------------+

Available
  | start Virtual display
  v
Resolve mode -> Create KDE output -> Connecting -> Virtual display
                                      ^              |
                                      |              v
                                      +-------- Reconnecting
```

Important rules:

- Pairing-required is a recoverable state with a PIN/password prompt.
- Missing dependencies and unsupported capabilities are explicit terminal states for the attempted command, not global application failures.
- A failed receiver session does not stop or alter another receiver session.
- Virtual display reconnects preserve the output and window layout after transport-only failure.
- Mirror reconnects have no output to preserve; transport may retry without creating a display.
- Stop is idempotent and always releases transport and display resources owned by the session.
- Every asynchronous event is checked against receiver ID and operation generation before it can change state.

A session record contains at least:

- stable receiver ID and current network endpoint;
- current mode and lifecycle state;
- selected physical monitor or virtual-output handle;
- `ResolutionDecision` and observed resolution;
- audio policy and muted state;
- owned transport process/session identifiers;
- owned virtual-display process/output identifiers;
- last error code, user-facing message, and recovery action;
- reconnect attempt and next retry time;
- current operation ID and generation.

## Resolution Policy

A `ResolutionDecision` contains a width, height, source, and evidence. Its source is one of `stored`, `discovery`, or `user`.

The app resolves the mode before creating a virtual output:

1. use a remembered mode only when it is associated with the same stable receiver identity;
2. otherwise use a reliable receiver mode from discovery or the transport adapter's pre-stream `query_modes` result;
3. if no reliable pre-stream value exists, require the user to choose from an explicit mode picker;
4. remember the chosen or verified mode per receiver after successful setup;
5. never guess a mode from a display name, stale endpoint, or generic TV category.

Changing a virtual display resolution destroys and recreates the KDE output. The UI warns that windows may need repositioning. If the transport reports a reliable accepted mode after setup and it differs from the decision, the session stops the stream and enters `Resolution mismatch` with the current output retained. The user can explicitly choose `Recreate at accepted mode`, which tears down and recreates the output, or Cancel, which stops Virtual display mode. The application never changes the output silently.

## Error Handling and Recovery

- No discovered receivers is a normal empty state.
- Avahi or network errors appear as retryable discovery errors while known devices remain visible.
- Pairing prompts appear only when requested by the receiver.
- Pairing cancellation returns to Available without changing trust metadata.
- Transport errors remain local to the receiver and expose retry/stop actions.
- Missing `krfb-virtualmonitor` disables only Virtual display mode.
- Virtual-display creation is transactional: failure or cancellation stops the helper, waits for output removal, and records cleanup if the timeout expires.
- Network loss in Virtual display mode shows `Reconnecting`, keeps the output, and retries with bounded exponential backoff.
- A transport daemon exit restarts within a bounded local-recovery budget; each target then reconnects independently.
- A helper exit or KScreen disappearance invalidates the virtual source and enters display cleanup/reconciliation.
- Normal shutdown stops transports and removes owned virtual outputs with bounded cleanup timeouts.
- Startup scans for orphaned outputs using persisted ownership records and offers explicit cleanup.
- Portal cancellation, keyring failure, unsupported source, unsupported mode, audio denial, and receiver rejection are distinct actionable errors.

The activity log records timestamped events and diagnostic detail. The selected receiver pane contains the current actionable error, not just a raw process exit code.

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
- discovery event source;
- transport command/event source;
- portal/capture-source provider;
- KScreen observer and command runner;
- credential/keyring provider;
- persistent ownership store.

Each seam has a fake implementation with controllable events, delays, failures, and late/stale callbacks.

### Unit tests

- mDNS identity derivation, identity replacement, collision handling, and stale-device handling;
- capability and `ResolutionDecision` selection;
- mode exclusivity per receiver;
- command validation for physical-only Mirror sources;
- state transitions for every row in the lifecycle table;
- operation-generation rejection of stale callbacks;
- cancellation and idempotent Stop from every asynchronous state;
- reconnect scheduling, cap, retry classification, and cancellation;
- ownership, transaction rollback, and orphan-reconciliation decisions;
- keyring metadata boundaries and pairing cancellation;
- audio policy, explicit video-only fallback, mute failure, and state retention.

### Adapter contract tests

- fake discovery emits deterministic device events;
- fake transport verifies pairing, start/stop, typed source validation, mute, reconnect, and two concurrent receiver targets;
- fake process runner covers missing commands, non-zero exit, timeout, malformed output, abrupt termination, and daemon restart;
- fake capture provider rejects windows and virtual outputs and supplies only physical monitor descriptors;
- fake KDE provider verifies create, wait-for-output, configure, distinct concurrent output IDs, reconnect retention, transactional rollback, layout restoration, timeout, and teardown ordering;
- fake keyring covers store, read, forget, rotation, permission failure, and no-plaintext guarantees.

### Environment integration tests

- run the installed `doubletake` and `doubletake-ctl` help and control-socket contract checks;
- run KScreen JSON parsing against a fixture modeled on `kscreen-doctor -j`;
- run a fake `krfb-virtualmonitor` process through the real provider;
- run a KDE Plasma Wayland smoke test when the helper and a test-safe virtual output are available;
- verify that startup orphan reconciliation never removes an unowned output;
- verify the app remains usable in the current environment where `krfb-virtualmonitor` is absent;
- exercise two concurrent targets, target-local failure isolation, system audio/mute, reconnect retention, shutdown, and source enforcement against a supported backend contract.

### UI tests

Qt tests cover the state matrix, including pairing prompts, mode-selector lockout, explicit stop-before-switch, resolution picker, portal cancellation, reconnect presentation, dependency errors, resolution mismatch, orphan cleanup confirmation, audio-unavailable video-only choice, and per-session mute.

Acceptance requires multiple independent receiver sessions, strict per-receiver mode exclusivity, physical-only Mirror source validation, virtual-output creation and cleanup, remembered pairing without plaintext metadata, actionable dependency errors, deterministic cancellation behavior, and isolation of one receiver's failure from another.

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
- KDE Plasma 6 Wayland is guaranteed for v1; other environments are capability-gated.
- Resolution is resolved before output creation and selected explicitly when no reliable value exists.
- Stale asynchronous events cannot revive a stopped or superseded operation.
- Daemon and helper ownership, failure classification, and cleanup timeouts are explicit.
- Pairing, trust, identity replacement, audio fallback, and mute semantics are explicit.
- External AirPlay protocol work remains inside the transport adapter.
- All side effects have fakeable adapter seams and corresponding tests.
