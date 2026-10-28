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

## Capabilities this build leaves gated

The composition root refuses to fabricate evidence. In this build:

- `virtual_display` — gated: no helper/output association verifier is supplied, and display
  creation refuses to publish without one.
- `remembered_trust` — gated: the keyring control is not rebound to the owned daemon.

They are reported at startup and by `--check-environment`. See `docs/operations.md`.
