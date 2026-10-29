# Operations

## Startup and the single-instance guard

One instance owns a runtime root at a time. The guard takes an exclusive lock and records an
authenticated owner (instance UUID plus PID/start-time); a second instance exits with status 1
rather than sharing state. The lock is held for the process lifetime.

## Runtime layout

```
$XDG_RUNTIME_DIR/airplay-linux/
  airplay-linux.lock          exclusive lock
  airplay-linux.lock.owner    authenticated owner record
  instances/<instance_uuid>/  private daemon socket and instance files
```

Metadata lives under the state root (`$XDG_STATE_HOME/pi/airplay-linux` by default). It holds no
secrets: credential references come from the Secret Service, never from application metadata.

## Gated capabilities

`--check-environment` lists them. A gated capability is a **deliberate refusal**, not a bug. The application treats an
unproven backend capability as absent and never infers one from the presence of a command, a version string, or a prior
success.

### What the shipped backend leaves unproven

On this build the startup probe withholds all of `foreground_control`, `owner_handshake`, `multi_target`, `keyring`,
`physical_source` and `virtual_source` for every profile the installed `doubletake` returns. The consequence is concrete:

- **no streaming session can start** through the shipped backend, in either Mirror or Virtual mode;
- discovery, receiver listing and device inspection still work;
- pairing is unavailable, so remembered trust is never established.

This is the specified v1 behaviour, recorded in the design under "Capability-gated delivery (v1 scope)". The refusal is
typed and boundary-appropriate; the structured capability report names the missing admission/start/source flags or the
pairing transaction prerequisite group, while the visible catalog message may not include that detail.

### Prerequisites you must satisfy before this application streams

1. A backend that affirms `foreground_control`, `owner_handshake`, `multi_target`, `keyring`, `physical_source` and
   `virtual_source` — or your chosen subset, if you accept that only the corresponding modes become available.
2. `kscreen-doctor` for display observation (present on this host).
3. PipeWire and xdg-desktop-portal for capture and audio; a Secret Service provider for remembered trust.
4. `krfb-virtualmonitor` for Virtual mode. **It is absent on this host**, so Virtual is unavailable here regardless of
   the backend.

### Live acceptance checklist (for a host that satisfies them)

Each item must be verified on a machine with the dependencies above; none of it is implied by the automated suite.

- physical-only Mirror onto a real AirPlay receiver, with system audio, and a second receiver muted independently;
- PIN/password pairing over a secret-safe channel, with no secret in argv, logs, metadata or the pane;
- a fresh process restores remembered trust from the backend credential authority (currently unproven: the shipped
  `BackendKeyringStore` keeps references in an adapter-local map and implements no cross-process load);
- a Virtual output created at the negotiated resolution, associated by exact owner-derived name, retained across a
  transport-only failure and recreated on an accepted mode change;
- exact-owner teardown with layout restoration, and a whole-application shutdown inside its documented bound.

### Documented residuals

- **Virtual output association is correlation evidence, not compositor attestation.** Publication requires the exact
  owner-derived name from the helper argv, absence from the launch baseline, a fresh observation, one new output with that
  name and size, a matching ownership record, and the live owned helper before and after. A same-user process that copies
  this reservation's exact name while our helper is live can still cause a foreign output to be accepted.
- **Remembered trust does not survive a restart** on the shipped adapter, as above.
- **The one-turn-gap drain** in ordered shutdown is defensive redundancy that no test pins directly; its removal does not
  currently fail the suite.


`--check-environment` lists them. A gated capability is a **deliberate refusal**, not a bug:

- `virtual_display` is gated because creating a virtual output requires proving the output belongs
  to the helper we launched. Without a real association verifier, an output could be adopted from
  another application or a stale helper.
- `remembered_trust` is gated because a keyring transaction issued through a control bound to a
  replaced daemon could target a daemon we no longer own.

Both fail closed. Restoring either requires supplying the missing evidence at the composition
root, not loosening the check.

## Failure behaviour

- An unconfirmed release is reported as *cleanup required*, never as success. Stop is retried, not
  assumed.
- A valid virtual output is preserved across transport-only failure. Stop, helper failure and an
  abandoned challenge are the cases that tear it down.
- Whole-application shutdown is bounded at 15 seconds and escalates rather than hanging.

## Diagnostics

Every message comes from an allowlisted catalog: a fixed code, a fixed message and the recovery
actions that apply. `--diagnostics` prints them without touching any receiver.
