# AirPlay Linux

Mirror a **physical monitor** from a Linux desktop to AirPlay receivers (Apple TV and
AirPlay-enabled smart TVs), with system audio, or give a TV its **own virtual display** so the
desktop layout is not disturbed.

This is the AirPlay **sender**. It does not receive AirPlay.

## Requirements

- Linux with KDE Plasma 6 on Wayland for the guaranteed v1 environment. Other desktops are
  capability-gated: whatever cannot be proven is reported unavailable rather than attempted.
- Python >= 3.12 and PySide6 >= 6.6,<7.
- `doubletake` / `doubletake-ctl` for the AirPlay transport. The application does not implement
  AirPlay authentication itself.
- PipeWire and xdg-desktop-portal for capture; a Secret Service provider for remembered trust;
  `kscreen-doctor` for display observation; optionally `krfb-virtualmonitor` for virtual outputs.

Nothing is installed automatically. `airplay-linux --check-environment` reports what is present.

## Running

```sh
airplay-linux                  # start the dashboard
airplay-linux --diagnostics    # print the diagnostic catalog and exit (read-only)
airplay-linux --check-environment
```

## Two modes

- **Mirror** sends an existing physical monitor. It never creates a virtual output.
- **Virtual display** creates an OS-level output at the receiver's native resolution and sends
  that. The output survives transport-only failures (Wi-Fi blips, receiver reboots, daemon
  restarts); a helper failure invalidates the source and triggers ordered cleanup.

The two modes are mutually exclusive per receiver and independent across receivers.

## What this build can and cannot do

The owned-daemon epoch is active. Production control uses `EvidenceAuthority` to join an
application-owned launch receipt to the current daemon process, private socket entry, kernel socket
row, peer credentials, file-descriptor holder and endpoint generation. Registry, supervisor,
transport and control share this authority. Teardown requires a pidfd, stage-complete evidence and
fresh verification after opening the pidfd; unresolved evidence retains a cleanup obligation.

This is a **cooperative same-user guarantee**. Another process with the same UID can inspect or
modify user-owned files and processes; this is not isolation from a malicious same-user process.
Old, foreign, ambiguous, replaced or unreadable ownership evidence is refused before control or
signals. A same-user process that impersonates the expected entry and argv remains indistinguishable
under this guarantee. Reported geometry does not attest the compositor's actual pixels. A receiver
can later require pairing that this application cannot service, and remembered trust still does not
survive an application restart. A crash requires reconciliation of durable receipts and a new endpoint
generation before replacement.

Mirror requires every activation prerequisite: owned daemon control, durable receipts, endpoint
verification, strict teardown, shared authority, a credential-service witness and the wired source
verifier. Missing prerequisites produce a named `dependency_incompatible` refusal with no backend
launch. The credential witness uses one private network/mount namespace and two isolated backend
legs before registry construction; its file-backed evidence must prove the service, positive and
negative legs. Public admission requires `owned_daemon_control`, `keyring` and `multi_target`.

A full-chain source-reporting build must advertise `capabilities-v1`, `status-source` and
`status-source-complete`. Before publishing a stream, the application matches that target's reported
monitor type, complete size and signed position against one fresh KScreen inventory of the requested
physical output. Window/virtual sources, missing position, a different monitor or ambiguous geometry
produce `unsupported_source`. Source acquisition and credential-challenge polling share one 5.0 s
deadline. A challenge from the connect acknowledgement or status stops only that exact target and
returns `dependency_incompatible`; unconfirmed target cleanup takes `cleanup_timeout` precedence.
No credential prompt or pairing operation is offered by this public profile.

The public backend still withholds `foreground_control`, `owner_handshake`, `target_bound_pairing`,
`provisional_commit`, `cancel_pairing`, `revoke_pairing`, `audio_restart` and `query_modes`. Startup
diagnostics name the selected authority, failed credential leg, missing complete-source or inventory
evidence and withheld flags. The fixture-only application-contract profile retains its original
handshake and pairing prerequisites.

The binaries currently installed in `/home/bin` advertise only the older two-token source contract,
so **Mirror remains gated on this host until a complete build is installed and all runtime evidence
passes**. The activation rebuild and its actual artifact hashes are recorded in
[docs/backend-fork.md](docs/backend-fork.md). Verification used deterministic external boundaries;
no live receiver streaming session was attempted. Virtual display has its own helper, source and
output-association prerequisites and is not enabled by this activation.

`--check-environment` reports capability evidence; [docs/operations.md](docs/operations.md) has the
operator checks and [docs/backend-compatibility.md](docs/backend-compatibility.md) records the backend
surfaces. Discovery, receiver listing and device inspection remain available when streaming is gated.
