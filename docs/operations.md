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
