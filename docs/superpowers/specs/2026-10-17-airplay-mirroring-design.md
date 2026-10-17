# AirPlay Linux Desktop Controller Design

Date: 2026-10-17

## Background

AirPlay Linux is a desktop GUI for sending a Linux desktop to AirPlay-capable receivers on the local network. The initial target is KDE Plasma 6 Wayland, while the application should keep the core discovery and mirroring model portable to X11 and other desktop environments when their capture or display providers are available.

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

### Mirror mode

Mirror mode accepts a physical monitor source only. It creates no virtual output. Starting the mode starts a `doubletake` transport session with system audio enabled by default. The user may stop the session or mute its audio without affecting other receiver sessions.

On Wayland, physical-monitor selection is subject to the compositor's screen-capture portal. The transport adapter must expose only physical monitor sources to the session manager and must reject virtual-output or application-window sources. If the active backend cannot select a requested physical monitor, the session enters an actionable unsupported state rather than silently capturing another source.

### Virtual display mode

Virtual display mode creates a KDE virtual output at the receiver's resolved resolution, configures it as a usable desktop monitor, and streams that output to the receiver. The output remains available to applications while the mode is active and during a transient network reconnect.

Stopping the mode stops transport first and then removes the output owned by that session. A hard helper failure or desktop-session end also removes owned outputs when possible. A stale output found on a later startup is identified and offered for cleanup rather than being deleted without user consent.

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
- reconnect preservation for Virtual display mode;
- cleanup ownership for child processes and outputs.

The session manager depends on injected adapters and can be tested with deterministic fakes.

### Discovery adapter

The discovery adapter watches `_airplay._tcp` and `_raop._tcp` through mDNS, merges records by stable device identity, and emits added/updated/removed events. It normalizes display name, host/address, model, feature bits, service port, pairing hints, and last-seen state.

Avahi or a Python zeroconf implementation may provide the first adapter. The domain interface must not expose raw Avahi or zeroconf records. Discovery does not claim a panel resolution unless the receiver information is trustworthy.

### Transport adapter

`DoubleTakeTransport` supervises the external `doubletake` daemon and its Unix-socket control interface. It is responsible for:

- starting and validating the daemon;
- discovery enrichment when useful;
- pairing and credential prompts;
- starting and stopping target streams;
- enabling system audio;
- per-target mute/unmute;
- transport health and reconnect events;
- mapping backend diagnostics into domain errors.

The adapter must use keyring-backed credentials where the backend supports it. Application metadata stores only a receiver identifier, display name, selected mode, and non-secret preferences.

The external command contract is validated at startup and in adapter tests. A fake executable and fake socket are used for deterministic tests; production code does not parse arbitrary human-oriented output outside the adapter.

### KDE virtual-display adapter

`KdeVirtualDisplayProvider` is the required v1 provider for KDE Plasma 6 Wayland. It:

1. verifies that the current session is KDE Wayland;
2. verifies that `krfb-virtualmonitor` and `kscreen-doctor` are available;
3. launches and retains the helper process with the requested resolution;
4. observes KScreen output data until the owned virtual connector appears;
5. enables and configures the connector through `kscreen-doctor`;
6. returns a stable output handle and capture source descriptor;
7. removes the output by stopping the owned helper process.

The adapter owns only outputs it created. It must never remove an unrelated virtual or physical output. KScreen output discovery is treated as eventual: creation is not complete until the expected connector is visible and configured.

Other desktop environments use optional provider adapters. If no provider is available, the GUI keeps Mirror mode available and marks Virtual display mode unavailable with the detected reason.

### Credential adapter

The transport backend's keyring mode is the preferred secret store. The application never writes pairing PINs or receiver credentials to its own JSON metadata. If no keyring is available, pairing is a user-visible dependency error rather than a silent plaintext fallback.

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
- Virtual display reconnects preserve the output and window layout.
- Mirror reconnects have no output to preserve; transport may retry without creating a display.
- Stop is idempotent and always releases transport and display resources owned by the session.

A session record contains at least:

- stable receiver ID and current network endpoint;
- current mode and lifecycle state;
- selected physical monitor or virtual-output handle;
- requested and observed resolution;
- audio enabled/muted state;
- owned transport process/session identifiers;
- owned virtual-display process/output identifiers;
- last error code, user-facing message, and recovery action;
- reconnect attempt and next retry time.

## Resolution Policy

The app uses receiver-specific information when it is trustworthy:

1. use a known model/capability mode when it is reliable;
2. prefer the canvas accepted or reported during AirPlay setup;
3. if no reliable value exists, require the user to choose a mode before creating the virtual output;
4. remember the chosen mode per receiver;
5. verify the observed mode after setup and report a mismatch rather than silently pretending it matches.

Changing a virtual display resolution destroys and recreates the KDE output. The UI must warn that windows may need repositioning for this operation.

## Error Handling and Recovery

- No discovered receivers is a normal empty state.
- Avahi or network errors appear as retryable discovery errors while known devices remain visible.
- Pairing prompts appear only when requested by the receiver.
- Transport errors remain local to the receiver and expose retry/stop actions.
- Missing `krfb-virtualmonitor` disables only Virtual display mode.
- Virtual-display creation is rolled back if the expected output is not observed or configured.
- Network loss in Virtual display mode shows `Reconnecting`, keeps the output, and retries with bounded exponential backoff.
- Transport or helper process exit marks the session failed and triggers cleanup of resources owned by that session.
- Normal shutdown stops transports and removes owned virtual outputs.
- Startup scans for orphaned outputs and offers explicit cleanup.

The activity log records timestamped events and diagnostic detail. The selected receiver pane contains the current actionable error, not just a raw process exit code.

## Dependencies and Environment Checks

At startup the application checks:

- PySide6 runtime;
- Avahi/mDNS availability or the selected discovery implementation;
- `doubletake` and `doubletake-ctl` compatibility;
- `kscreen-doctor` for KDE display management;
- `krfb-virtualmonitor` for Virtual display mode;
- Wayland/X11 session information;
- PipeWire and the desktop portal for Wayland capture;
- a usable keyring backend for remembered pairing.

The app reports exact missing commands and keeps unrelated functionality enabled where possible. It does not install system packages automatically.

## Testing Strategy

### Unit tests

- mDNS identity merge and stale-device handling;
- capability and resolution selection;
- mode exclusivity per receiver;
- command validation for physical-only Mirror sources;
- state transitions and idempotent stop;
- reconnect backoff and cancellation;
- ownership and orphan-cleanup decisions;
- keyring metadata boundaries.

### Adapter contract tests

- fake discovery emits deterministic device events;
- fake transport verifies pairing, start/stop, mute, reconnect, and multi-target behavior;
- fake KDE provider verifies create, wait-for-output, configure, reconnect retention, and teardown ordering;
- fake processes cover missing commands, non-zero exit, timeout, malformed output, and abrupt termination.

### Environment integration tests

- run the installed `doubletake` and `doubletake-ctl` help and control-socket contract checks;
- run KScreen JSON parsing against a fixture modeled on `kscreen-doctor -j`;
- run a fake `krfb-virtualmonitor` process through the real provider;
- run a KDE Plasma Wayland smoke test when the helper and a test-safe virtual output are available;
- verify the app remains usable in the current environment where `krfb-virtualmonitor` is absent.

Acceptance requires multiple independent receiver sessions, strict per-receiver mode exclusivity, physical-only Mirror source validation, virtual-output creation and cleanup, remembered pairing without plaintext metadata, actionable dependency errors, and isolation of one receiver's failure from another.

## Risks and Boundaries

- AirPlay receiver compatibility varies by vendor firmware even when the receiver advertises AirPlay services. The GUI must surface backend rejection details.
- KDE's virtual-output support is helper-backed and version-sensitive. The provider must use capability detection and avoid private KWin protocol assumptions where a supported helper is available.
- Wayland capture is mediated by the desktop portal. A backend limitation that prevents selecting a requested physical monitor must be surfaced as unsupported rather than silently selecting a different source.
- AirPlay resolution may be negotiated rather than advertised. The explicit fallback picker is required for correctness.
- The application cannot guarantee recovery from a desktop session crash; startup orphan detection is the recovery boundary.

## Verification Checklist

Before implementation planning, the design is internally consistent:

- Mirror and Virtual display are separate per-receiver modes.
- Only Mirror uses physical-monitor sources.
- Virtual display owns its KDE output lifecycle and preserves it during reconnect.
- KDE Plasma 6 Wayland is guaranteed for v1; other environments are capability-gated.
- Resolution is detected when reliable and selected explicitly otherwise.
- External AirPlay protocol work remains inside the transport adapter.
- All side effects have fakeable adapter seams and corresponding tests.
