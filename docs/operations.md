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

`--check-environment` reports the available dependencies and their evidence. An unproven
capability stays unavailable. Public help and capability tokens establish the backend's
published surface; production composition separately supplies receipt-backed daemon ownership,
a credential-service witness and fresh physical-source verification. The older release-only
probe could not establish those facts. Its historical observations and the current recognition
rules are in [Backend compatibility](backend-compatibility.md).

Mirror requires complete per-stream source reporting plus a fresh, unambiguous physical-output
match. Portal metadata reports what was selected; it provides no new monitor selector. Owned
control still requires current process/socket receipts and strict teardown. A `target-volume`
token grants none of those ownership, source or trust permissions.

The host also needs KScreen observation, PipeWire, a ScreenCast portal implementation for its
desktop, and a usable Secret Service. Virtual mode additionally requires `krfb-virtualmonitor`
and its ownership association verifier. Availability must be observed on the intended host;
the offline suite supplies controlled boundaries and proves no current host readiness.

Portal-backed Mirror is offered on KDE Plasma 6 X11 as well as KDE Plasma 6 Wayland, and
X11 adds no new capture path: the same backend, physical-monitor inventory and per-stream
source-verification proofs must pass first. The bootstrap `--check-environment` report
cannot establish those proofs, so it never proves production capture. Virtual display
remains KDE Plasma 6 Wayland-only; X11 has no virtual-output provider.

## Receiver volume and session mute

The **Volume** row stays visible below the primary actions, including with no receiver selected
or with an older backend. It controls only the selected receiver. A capable, streaming session
with confirmed audio and resolved trust/cleanup permits changes from 0 to 100. While a change is
pending, the readout keeps the last acknowledged value rather than displaying the requested
value as confirmed.

| Readout | Meaning |
| --- | --- |
| `30%` (or another positive integer) | Last receiver-acknowledged application level |
| `Mute` | Acknowledged level 0; slider 0 and the speaker mute action have the same effect |
| `—` | Level unknown, including a fresh stream with no level receipt |

An acknowledged positive **slider** change saves a per-receiver preference from 1 to 100 after
identity/continuity checks. The next audio-enabled Start reapplies that remembered level when
`target_volume` is proven. A saved level alone cannot enable the feature. Zero is never saved;
mute is session-local. The speaker's unmute action restores the current session's **last audible
level**, then the remembered receiver level, then 100 if neither exists. Speaker actions and
startup reapplication do not overwrite the saved preference.

If saving fails after the receiver acknowledged the change, sound state and readout keep the
applied level. The pane says **"Applied, but could not be saved."** The catalog explains:
"The receiver volume was applied but could not be saved. The previous saved level is kept."
An invalid saved volume is ignored without erasing other receiver preferences or rewriting the
record on read. Structural or identity corruption still refuses the record.

On a legacy backend the row is disabled with **"The installed backend does not support receiver
volume."** Existing target `mute`/`unmute`, connect arguments, startup/retry meanings, ownership,
source checks and timers retain their legacy behavior. Even a remembered positive level sends
no new connect flags without proven support.

### Startup, recovery and video-only decisions

With a requested starting level, only a final RTSP 200 acknowledges that level. A complete
startup rejection keeps video on the same stream and withholds audio. The catalog says:
"The starting volume could not be confirmed. Audio is withheld. Retry audio, start video only,
or stop the session." Retry reissues the requested level on that same handle. A pending or
missing receipt also leaves an explicit audio decision; a Retry samples the existing audio-start
status once. There is no added status-polling owner. An incomplete or expired setup follows the
ordinary failed-setup cleanup path.

A live level failure keeps video and the last confirmed audio state; it does not reconnect the
stream or turn an earlier audible level into a startup refusal. Audio loss clears current mute
confirmation while retaining acknowledged level history. Recovery must confirm audio again;
an applied zero receipt restores `Mute`, and an applied positive receipt restores audible state.
A close reported by another caller cannot confirm this application's audio closure.

Choosing **Start video only** immediately admits the audio-off policy and prevents further
volume/retry work for that stream. `Disabled` is published only after the current targeted close
confirms that audio producers stopped. An unconfirmed close keeps the cleanup obligation and
allows another video-only attempt or Stop. A replacement stream uses the admitted policy even
while the earlier close remains unconfirmed. The backend's audio-off latch is one-way for each
stream; enabling audio requires a new stream.

### What an acknowledgement establishes

The application remembers its own acknowledged commands. It does not sense changes made with a
receiver remote and does not read back the receiver's current gain. An acknowledgement cannot
prove that a receiver honored the requested audible gain. A 50% command maps to **-6.020600 dB**
amplitude (`20 log10(0.5)`), which is not a claim of half perceived loudness.

Implementation proof, a prepared helper build, an installed pair and a live receiver observation
are separate evidence. The [prepared-pair receipt and operator rollback](backend-fork.md#prepared-volume-helper-pair-2026-11-05)
records a build in `/tmp/av-delivery.7OYAi0` from a superseded patch revision; that pair was not
installed. The [installed final-source pair](backend-fork.md#installed-final-source-pair-2026-11-06)
records the four-token pair actually installed in `/home/bin` on 2026-11-06, its observed
help/capability contracts and the two rollback backups. Audible gain and live receiver acceptance
remain unobserved, and an already running daemon keeps its earlier executable. The receipt
contains the separate operator command groups and the two-file revert path.

### Live acceptance and residual guarantees

The automated suite uses offline fakes and private fixtures. Physical-only Mirror with real
system audio, independent receivers, remembered trust across a fresh process, Virtual output
resolution/ownership and bounded teardown still require separately authorized host/receiver
observations. No live receiver acceptance is claimed here.

Virtual output association remains correlation evidence: publication requires the exact
owner-derived name, absence from the launch baseline, a fresh unique output of the expected
size, a matching record and the owned helper alive before and after. A same-user process that
copies the exact name while the helper is live can still defeat that correlation. Daemon
ownership has the same cooperative same-user boundary.

The shipped `BackendKeyringStore` retains credential references in an adapter-local map and has
no cross-process load contract, so remembered trust across restart remains unproven. The
one-turn-gap drain in ordered shutdown is defensive redundancy; a surviving mutation in the
historical selected 62-test set is not whole-suite mutation evidence.

## Failure behaviour

- An unconfirmed release is reported as *cleanup required*, never as success. Stop is retried, not
  assumed.
- A valid virtual output is preserved across transport-only failure. Stop, helper failure and an
  abandoned challenge are the cases that tear it down.
- Whole-application shutdown is bounded at 15 seconds and escalates rather than hanging.

## Diagnostics

Every message comes from an allowlisted catalog: a fixed code, a fixed message and the recovery
actions that apply. `--diagnostics` prints them without touching any receiver.
