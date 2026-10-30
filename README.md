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

The composition root refuses to fabricate evidence, so what follows is the measured state, not a
list of missing features.

**On the host this was built against, no streaming session starts.** The installed `doubletake`
0.5.0 affirms only `multi_target`, `audio_probe` and `target_mute`; transport admission requires
`foreground_control`, `owner_handshake` and `keyring`, all withheld. Discovery, receiver listing
and device inspection still work.

Three independent prerequisites stand between this code and a working session, and this backend
satisfies none of them:

- **owned daemon control** — a supervised foreground daemon exposes no control socket, and a
  daemonised one is not supervised; owning it needs a launch-receipt model that is specified but
  not implemented;
- **physical source selection** — `doubletake` has no monitor/output selector, so "send this
  monitor" is not expressible; capture is the whole desktop;
- **credential service** — the keyring backend is selectable but unproven, and remembered trust
  does not survive a restart because the adapter implements no cross-process load.

Virtual display is separately blocked: `krfb-virtualmonitor` is absent here, and the composition
supplies no output-association verifier.

Nothing above is worked around by inference — that is the point of the design. `--check-environment`
reports the exact missing affirmations; `docs/operations.md` has the prerequisites and a live
acceptance checklist; `docs/backend-compatibility.md` records why each gate cannot be closed with
this backend.
