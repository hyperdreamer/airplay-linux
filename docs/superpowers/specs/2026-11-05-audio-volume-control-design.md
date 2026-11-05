# Audio volume control — design

- **Status:** design agreed with the operator; awaiting independent review, then the execution gate.
- **Date:** 2026-11-05
- **Delivery target:** `/data/home/guest/Development/airplay-linux`, branch `master`, no remote.
- **Mockup record:** `docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`

## 1. Background

The application can mute a receiver and nothing else. That is not a UI omission; it is the
whole surface the backend offers. `doubletake-ctl` publishes exactly nine commands
(`status`, `discover`, `devices`, `connect`, `pin`, `disconnect`, `mute`, `unmute`) and the
application proves that surface before it enables any audio action.

The consequence is audible and is what the operator reported:

- the daemon sends `SET_PARAMETER volume=0` (0 dB, the receiver's **maximum**) while setting
  up a stream, so every new session starts at full volume;
- `unmute` is the same 0 dB value, so unmuting also lands at full volume;
- the only other level the chain can express is `-144 dB` (silence), which `mute` sends.

So the chain has two levels, maximum and silence, and no way to state one in between.
The mute button's own tooltip admits the gap: "the receiver keeps its own volume setting".

Two facts bound the work:

1. Only the daemon owns the receiver's RTSP session, so only a daemon change can set a level.
2. This repository already carries the mechanism for that: three patches in
   `vendor/doubletake/` are applied to a pinned upstream checkout and the two binaries are
   installed to `/home/bin`. A fourth patch is the supported way to extend the backend.

## 2. Goals

1. A user can set a receiver's volume anywhere from 1% to 100% from the application.
2. The level is remembered **per receiver** and re-applied by the application whenever it
   starts audio for that receiver and after a mute is lifted, so a new session and an unmute
   never land at maximum.
3. Mute, unmute, Stop, Retry audio, the audio-decision prompt and receiver replacement keep
   their current guarantees.
4. A backend without volume support keeps today's behaviour exactly: mute works, the volume
   row is disabled with one plain reason, and nothing regresses.
5. No new live-receiver dependency in the automated suite: all proof stays offline.

## 3. Non-goals

- Balancing or per-application mixing on the sending machine.
- Volume for the local speakers; the level is the receiver's.
- Turning the app into a remote control for arbitrary receiver settings.
- Virtual-display work, discovery changes, or any capability the design does not name here.
- Retroactively repairing the level of a stream that is already playing when the remembered
  level changes for a *different* receiver.

## 4. Decisions taken with the operator

| # | Question | Decision |
| --- | --- | --- |
| 1 | Who owns the level | **A.** The app remembers a level per TV and re-applies it on every new session and every unmute. A change made with the TV's own remote survives only until the next session start. |
| 2 | Where the control lives | **C.** A dedicated `Volume` row under the `Start`/`Stop`/`Pair` line, full pane width, always visible. The speaker button stays the only mute control. |
| 3 | Slider and mute | **A.** Moving the slider above 0% lifts the mute; dragging to 0% mutes. The speaker button still mutes and unmutes in one click, restoring the last audible level. |

The mockup shown and chosen is the composite at
`docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`; region C is the
accepted placement.

## 5. Architecture

Four layers change, and each keeps its existing responsibility boundary:

```text
UI (window/presenter)          Volume row: slider 1..100, readout, disabled reason
        |  SetVolume / SetMuted
Session audio authority        confirmed level, per-command fence, ordering before audio start
        |  set_volume(handle, level)
Transport port + doubletake    ctl "volume <pct> <target>", status "volume" per stream
        |  RTSP SET_PARAMETER volume
Backend daemon (patch 0004)    percent -> dB mapping, restore-on-unmute, status reporting
```

Storage sits beside the session authority: the remembered level is a per-receiver preference,
not session state.

### 5.1 Backend patch `vendor/doubletake/0004-target-volume.patch`

Stacks on 0001+0002+0003 and changes four things.

1. **A new ctl command:** `volume <level-percent> [target]`. Level is an integer 0–100; a
   target restricts the command to that receiver, exactly like `mute`/`unmute`. The usage row
   carries the same target-scope statement the other target-scoped rows carry, because the
   application proves target scoping from the published text and must not infer it.
2. **The mapping, in one place:** for `level` 1–100 the daemon sends
   `SET_PARAMETER volume = 20 * log10(level / 100)` dB; for `level` 0 it sends `-144` dB
   (silence). 100% is exactly 0 dB and 0% is exactly the existing mute value, so the two
   endpoints agree with today's behaviour and no caller can produce a value outside the range.
   The mapping is a named function so the Go proof can compile and test it directly.
3. **`unmute` restores the stream's stored level** instead of sending 0 dB. A stream that has
   never been given a level keeps today's behaviour (0 dB) so an unpatched caller sees no
   change.
4. **`ctl status` reports the level per stream** as an optional integer percent, read from that
   stream's own record. Absent means "no level has ever been applied to this stream", which is
   different from 0 and must stay distinguishable. `-capabilities` gains the token
   `target-volume`.

The recognized profile becomes `doubletake-0.5+source+volume`. Sender help does not change, so
the pinned sender-flag set is untouched; the new token plus the new ctl usage row is the whole
difference the application has to prove.

### 5.2 Application backend proof

`contract.py` currently requires an exact match against one pinned ctl command table and
refuses every other surface outright. It gains a second accepted table — the pinned table plus
the `volume` row — and two rules:

- the volume table is accepted only together with the `target-volume` capability token; a
  surface that shows the command but not the token is refused (fail closed, as today's
  "merely-similar help proves nothing" rule already requires);
- `BackendFeatures.target_volume` is true only for that combination, and nothing else may set
  it.

The profile string, `BackendFeatures` field, status decoder field and transport operation are
the only places volume becomes visible; no gate is loosened, and `physical_source`,
`foreground_control`, `owner_handshake`, `keyring`, `virtual_source` and the pairing group keep
their current values.

### 5.3 Domain, port and transport

- `VolumeLevel`: a validated integer 0–100 with `0` meaning silence; constructed only through a
  check, never from raw JSON without validation.
- `SetVolume(receiver_id, level)` message, beside `SetMuted`.
- `TransportPort.set_volume(handle, level, context) -> Result[int]` returning the level the
  backend confirmed.
- `TransportEventKind.volume_result` carries `VolumePayload(level, ok)`; the effect kind is
  `volume`, joining the existing audio effect set so the audio authority claims it.
- `BackendStream.volume: int | None`, decoded strictly: an integer in 0–100, or absent; any
  other shape refuses the whole status reply as today.

### 5.4 Session authority rules

These are the rules the specification must make exactly testable.

1. **Ordering.** For a stream with a remembered level, the level command is issued after the
   stream exists and **before** audio is started on it. Audio is never started first and then
   corrected. This is what removes the full-volume burst at session start.
2. **Confirmation.** Only a confirmed result changes the recorded level. A failed, refused or
   stale result leaves the last confirmed level in place and raises one diagnostic; it never
   silently substitutes a default.
3. **Fencing.** A volume command is identified by its own operation id, exactly as mute is, so
   a slow `30%` arriving after a `60%` cannot apply. The volume's stream fence is the same
   transport lease the audio authority already uses.
4. **Lease scoping.** A replacement stream starts with *no* confirmed level, because only a
   stream that actually received the command may claim one. This mirrors the existing
   `muted_lease` rule.
5. **Unmute.** Lifting a mute restores the last audible level. The daemon does this itself
   (patch item 3); the application re-sends the level only when the level it wants differs from
   the stream's last confirmed level, for example after the level was changed for that receiver
   from another path.
6. **Slider/mute coupling.** `SetMuted(true)` and a slider moved to 0 express the same session
   fact; a slider moved above 0 while muted lifts the mute in one user action (unmute and level
   are issued together, level first). Mute remains session-only.
7. **No audio, no level.** With audio `Disabled` (video-only), `Unavailable` or undecided, a
   volume command is refused by the authority rather than sent, and the remembered level is
   preserved for the next session.
8. **Never tears down video.** A volume failure never stops the stream, never retries the
   transport, and never blocks audio from starting.

### 5.5 Storage

`SavedReceiver` gains `remembered_volume: int | None`, next to `remembered_resolution`, and
inherits `PreferenceAccess` unchanged: an ambiguous, replaced, non-persistable, non-continuity
receiver never inherits a level. Values are stored as 1–100; **0 is not storable**, because the
mute is session-only and a persisted 0 would make an unmute silent.

The receiver record moves to schema version 2. Records written at version 1 must still load
(the new field reads as absent), and the other record schemas keep version 1 unless this design
names them. A record that carries a 0, a negative number, a value above 100, or a non-integer
volume is refused as malformed, not clamped.

### 5.6 UI

`Volume` row, immediately under `Start`/`Stop`/`Pair`, as accepted in the mockup:

- label `Volume`, horizontal slider 1–100 in 1% steps, right-aligned readout `30%`;
- the row is disabled with one short reason when the backend does not prove `target_volume`, or
  when the receiver has no audio (video-only, unavailable, undecided, disconnected);
- the level shown is the last confirmed level. When the stream reports a level the app did not
  set (a remote control changed it), the readout follows the stream, and the remembered level
  is untouched until the next session start;
- a receiver that has never been given a level shows what the stream reports, and nothing is
  pushed until the user moves the slider;
- keyboard: the slider is reachable by tab, arrow keys move by 1%, page keys by 10%, and the
  accessible name is `Receiver volume`;
- the speaker button keeps its current icons, tooltip semantics and session-only mute.

Activity log gains three codes with catalog messages: level applied, level change failed, and
the disabled reason when a receiver cannot be controlled.

## 6. Error handling

| Situation | Behaviour |
| --- | --- |
| Backend proves volume | Row enabled; level applied before audio start and after unmute |
| Backend proves only mute (today's build) | Row disabled with one reason; mute unchanged; no diagnostic noise |
| Backend surface inconsistent (command without token) | Whole backend refused, exactly as today's unrecognised surface |
| `volume` command fails or times out | Keep last confirmed level; one diagnostic; audio still starts |
| Stale volume result after a newer one | Discarded by operation id; confirmation unchanged |
| Receiver replaced mid-session | New stream has no confirmed level; remembered level is not inherited by a replaced identity |
| Stored level malformed | Record refused; receiver treated as having no remembered level |
| Level requested with audio unavailable | Refused by the authority, never sent; reason shown |

## 7. Test strategy

All offline; no live receiver, no network.

- **Contract:** both surfaces recognized; volume row without token refused; status decode with
  and without `volume`, plus out-of-range and non-integer refusals.
- **Go producer proof:** the carried patch's mapping function compiles and is exercised at
  0, 1, 50, 99, 100 — endpoints exact, monotonic — in the same offline style as the existing
  carried-source producer test.
- **Transport:** `volume` argv shape for target-scoped and unscoped calls; confirmed level
  returned; fence respected.
- **Session:** ordering (level before audio start), fencing (slow 30 then 60), failure keeps the
  last confirmed level, replacement stream has no inherited level, unmute restores the last
  audible level, slider-to-0 mutes, slider-up-while-muted unmutes and levels in one action,
  no command while audio is disabled/unavailable.
- **Storage:** v1 record loads with no level; v2 round-trip; 0 and out-of-range refused;
  `PreferenceAccess` refusal paths keep withholding the level.
- **UI:** row states (enabled, disabled reason per cause), readout format, arrow/page steps,
  accessible name, no regression to the speaker button or the action grid.
- **Regression:** the existing audio, mute, mirroring and capability suites must stay green,
  including every test that asserts the current no-volume surface.

## 8. Delivery and installation

The application change is delivered in this repository. Making the feature *live* requires the
fourth patch to be applied and the two binaries rebuilt and installed, which is a separate
receipted step at delivery with the exact commands recorded in `docs/backend-fork.md`,
including the new patch's digest, the base commit, the Go toolchain, the build commands and the
verify command (`./doubletake -capabilities` must print the four tokens; `ctl volume` must
appear in the usage block). The previous binary pair is kept as `.prev` files so the change can
be reverted by restoring two files.

## 9. Risks

1. **A shared binary changes.** Until the rebuild is installed the feature is inert; a
   half-installed pair (sender new, ctl old) is exactly what the two-surface proof detects, and
   it fails closed to mute-only rather than to a wrong assumption.
2. **Receivers differ in how they treat SET_PARAMETER volume.** The mapping is defined once and
   confirmed against the reported level; a receiver that ignores the parameter shows a level
   that does not match, and the readout follows the stream rather than the request.
3. **Record schema version bump.** A v1 record must never be lost: the v2 decoder reads both,
   and a refused record must not take the receiver's other preferences down with it.
4. **Perceived loudness curve.** 50% is −6 dB (half amplitude), not half perceived loudness.
   This is the standard sender mapping and both endpoints are exact, but the operator may find
   the middle of the range quieter or louder than expected. Changing the curve later is one
   function in one place.

## 10. What must not regress

- Mute stays session-only and receiver-local; Stop disconnects only that receiver.
- Audio-decision, video-only and retry-audio paths keep their current states and messages.
- Capability gating keeps failing closed; no unproven capability is ever inferred.
- No secret reaches argv, metadata, activity log or the pane.
- The no-remote delivery path stays a local merge only.
