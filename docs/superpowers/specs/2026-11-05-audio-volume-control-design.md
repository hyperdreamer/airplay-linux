# Audio volume control — design

- **Status:** design revised after independent review round 1; awaiting review round 2.
- **Date:** 2026-11-05 (revision 2)
- **Delivery target:** `/data/home/guest/Development/airplay-linux`, branch `master`, no remote.
- **Mockup record:** `docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`

## Revision note

Round 1 raised six blockers against revision 1. Three of them changed the design rather than
its wording:

1. **Audio starts during `connect`.** `audio_restart` is False for the shipped profile, so
   `transport/doubletake.py:1102-1110` treats audio as carried by the stream itself and only
   *observes* it. Revision 1's "send the level before audio starts" therefore described
   something the transport cannot order. The level must reach the daemon **before** `connect`.
2. **A new profile string would break production admission.** `doubletake-0.5+source` is
   compared by literal in twelve places (`transport/doubletake.py:525`, `process/launcher.py:80`,
   `process/daemon_registry.py:251,343,372,946,1081`, `process/supervisor.py:572`,
   `runtime.py:424,454`, `backend/control.py:584,589`). The design now adds **no new profile
   value**: the new capability travels in `BackendFeatures`, which is what already gates
   behaviour.
3. **The receiver reports no volume back.** Confirmation can only mean "the receiver
   acknowledged the command". Revision 1 claimed the readout would follow changes made with the
   TV's own remote; that is unsupported and is withdrawn.

## 1. Background

The application can mute a receiver and nothing else. That is not a UI omission; it is the whole
surface the backend offers. `doubletake-ctl` publishes **eight** commands (`status`, `discover`,
`devices`, `connect`, `pin`, `disconnect`, `mute`, `unmute`) and the application proves that
surface before it enables any audio action (`backend/contract.py:59`).

The consequence is audible and is what the operator reported:

- the daemon sends `SET_PARAMETER volume=0` (0 dB, the receiver's **maximum**) while setting up a
  stream, so every new session starts at full volume;
- `unmute` sends the same 0 dB value, so unmuting also lands at full volume;
- the only other level the chain can express is `-144 dB` (silence), which `mute` sends.

So the chain has two levels, maximum and silence, and no way to state one in between. The mute
button's own tooltip admits the gap: "the receiver keeps its own volume setting".

Two facts bound the work:

1. Only the daemon owns the receiver's RTSP session, so only a daemon change can set a level.
2. This repository already carries the mechanism for that: three patches in
   `vendor/doubletake/` are applied to a pinned upstream checkout and the two binaries are
   installed to `/home/bin`. A fourth patch is the supported way to extend the backend.

## 2. Goals

1. A user can set a receiver's volume anywhere from 1% to 100% from the application, and can
   reach 0% as a mute.
2. The level is remembered **per receiver** and re-applied before the receiver starts playing a
   new stream, so a new session never begins at maximum.
3. Unmute restores the last audible level instead of maximum.
4. Mute, unmute, Stop, Retry audio, the audio-decision prompt and receiver replacement keep
   their current guarantees.
5. A backend without volume support keeps today's behaviour exactly: mute works, the volume row
   is disabled with one plain reason, and nothing regresses.
6. No new live-receiver dependency in the automated suite: all proof stays offline.

## 3. Non-goals

- Sensing the receiver's actual volume, or noticing changes made with the TV's own remote.
- Balancing or per-application mixing on the sending machine.
- Volume for the local speakers; the level is the receiver's.
- Turning the app into a remote control for arbitrary receiver settings.
- Virtual-display work, discovery changes, or any capability the design does not name here.

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

```text
UI (window/presenter)          Volume row: slider 0..100, readout, disabled reason
        |  SetVolume / SetMuted
Session audio authority        effective level, last audible level, per-command fence
        |  set_volume(handle, level)   |   pending level before connect
Transport port + doubletake    ctl "volume <pct> <target>", status "volume" per stream
        |  RTSP SET_PARAMETER volume
Backend daemon (patch 0004)    percent -> dB, pending startup level, restore-on-unmute
```

Storage sits beside the session authority: the remembered level is a per-receiver preference,
not session state.

### 5.1 Backend patch `vendor/doubletake/0004-target-volume.patch`

Stacks on 0001+0002+0003. Five changes, all inside the daemon and its ctl.

1. **A new ctl command:** `volume <level-percent> [target]`. Level is an integer 0–100; a target
   restricts it to that receiver. The usage row carries the same target-scope statement the
   other target-scoped rows carry, because the application proves target scoping from the
   published text and must not infer it.
2. **Two behaviours for one command, so ordering is possible.** If a live stream exists for the
   target, the level is applied to it at once. If no stream exists yet, the level is recorded as
   that receiver's **pending startup level** and applied during the next stream's setup in place
   of the hardcoded `0 dB`. A receiver with no pending level keeps today's `0 dB`, so an
   unpatched caller sees no change.
3. **The mapping, in one place:** for `level` 1–100 the daemon sends
   `SET_PARAMETER volume = 20 * log10(level / 100)` dB; for `level` 0 it sends `-144` dB
   (silence). 100% is exactly 0 dB and 0% is exactly the existing mute value. The mapping is a
   named function so the carried-code proof can compile and exercise it.
4. **Level is state, not a request echo.** A stream records the level it last had **acknowledged
   by the receiver** (an RTSP 200 to the `SET_PARAMETER`). A refused or failed send does not
   update it, and the failure is reported to the caller — unlike today's setup path, which logs
   `[SETUP] SET_PARAMETER volume failed (non-fatal)` and continues.
5. **`volume`, `mute` and `unmute` share one level model.** `mute` and `volume 0` both silence
   the receiver and both keep the last audible level. `unmute` and any `volume` of 1–100 both
   make the receiver audible; `unmute` uses the stored last audible level, and a `volume` of
   1–100 applies the new level and lifts the mute in the same operation. A stream that has never
   had a level keeps today's `0 dB` for `unmute`.

`ctl status` reports, per stream, the optional integer `volume` — the last level that receiver
acknowledged — read from that stream's own record. Absent means "no level has ever been
acknowledged on this stream", which is not the same as 0 and must stay distinguishable.
`-capabilities` gains the token `target-volume`; **the sender's help output does not change**, so
the pinned sender-flag set is untouched.

### 5.2 Application backend proof

`contract.py` currently requires an exact match against one pinned ctl command table and refuses
every other surface (`backend/contract.py:820-900`). It gains a second accepted table — the
pinned table plus the `volume` row — and these rules:

```text
ctl_pinned   := ctl table == the pinned 8-row table
ctl_volume   := ctl table == the pinned 8-row table + the volume row
tokens       := tokens advertised by -capabilities

target_volume      := ctl_volume and "target-volume" in tokens
inconsistent       := (ctl_volume and "target-volume" not in tokens)
                      or (ctl_pinned and "target-volume" in tokens)
verified           := sender flags in {pinned, source} and (ctl_pinned or ctl_volume)
                      and not inconsistent
profile            := "doubletake-0.5+source" when the sender shows source reporting,
                      otherwise "doubletake-0.5"   # unchanged for every existing build
source_reporting   := verified and source flags and source tokens
source_reporting_complete := source_reporting and "status-source-complete"
```

An inconsistent surface refuses the whole backend exactly as today's unknown surface does.
**No new profile string is introduced**, so all twelve existing profile comparisons keep working
unchanged; only `BackendFeatures` grows a `target_volume: bool` field, defaulting to `False` in
every constructor that does not prove it. `physical_source`, `foreground_control`,
`owner_handshake`, `keyring`, `virtual_source` and the pairing group keep their current values.

### 5.3 Domain, port, transport and typed control

- `VolumeLevel`: a validated integer 0–100, `0` meaning silence; constructed only through a
  check, never straight from JSON.
- `SetVolume(receiver_id, level)` message, added to the closed command union
  (`domain/messages.py:130`) beside `SetMuted`.
- `TransportPort.set_volume(handle, level, context) -> Result[int]` returning the acknowledged
  level.
- `TransportEventKind.volume_result` carrying `VolumePayload(level, ok)`; the effect kind
  `volume` joins the audio effect set (`session/audio.py:67`) and the audio claim sets.
- `BackendStream.volume: int | None`, decoded strictly: an integer 0–100 or absent; any other
  shape refuses the status reply, as every other wrong field already does.
- `CapabilityReport.volume: bool` (`domain/resources.py:222`) so the UI can gate the row from the
  same report it already receives.
- Typed control, not just a port method: `BackendOperation.volume` in the operation enum
  (`backend/control.py:47`), a `VolumePayload` in the payload union (`:137`), membership in the
  dispatch-fence set (`:160`), a target-scoped argv branch in the mapper (`:372`) that **refuses
  a missing target** like `mute`/`unmute` do, an acknowledgement decoder that returns the
  acknowledged integer (`_ack`, `:912`), and authorisation in `process/ownership.py:318`
  conditioned on proven `target_volume`.

### 5.4 Session authority rules

Three distinct values, and nothing may conflate them:

| Value | Meaning | Lifetime |
| --- | --- | --- |
| `remembered` | The per-receiver preference, 1–100, or absent | Durable |
| `effective` | The level this stream last had acknowledged, 0–100, or unknown | Per transport lease |
| `last_audible` | The last acknowledged non-zero level for this stream, or absent | Per transport lease |

Rules the specification must make exactly testable:

1. **Before the stream.** For a receiver with a `remembered` level, the level is sent to the
   daemon **before `connect`**, using the command's pending-startup behaviour (§5.1.2). Audio is
   carried by `connect` on this backend, so this is the only ordering that keeps the first
   audible sample at the chosen level rather than at maximum.
2. **Confirmation.** Only an acknowledged result changes `effective`. A failed, refused, stale
   or timed-out result leaves `effective` and `last_audible` alone and raises one diagnostic. No
   default is ever substituted.
3. **Fencing.** A volume command carries its own operation id, exactly as mute does, so a slow
   `30%` arriving after a `60%` cannot apply. The stream fence is the transport lease the audio
   authority already uses.
4. **Lease scoping.** A replacement stream starts with no `effective` and no `last_audible`,
   because only a stream that actually received an acknowledgement may claim one. This mirrors
   the existing `muted_lease` rule.
5. **One command, no compound race.** Slider-up-while-muted is a single `volume <n>` for
   `n` ≥ 1, which the daemon applies as "audible at n". There is no separate unmute for the
   application to interleave, so partial success cannot exist. Slider-to-0 is a single
   `volume 0` (equivalent to `mute`). The speaker button keeps using `mute`/`unmute`.
6. **Unmute.** `unmute` restores `last_audible` in the daemon, so the audible level survives a
   mute without a second command. The application re-sends `remembered` only when it differs
   from `last_audible`, for example after the level was changed for that receiver elsewhere.
7. **No audio, no level.** With audio `Disabled` (video-only), `Unavailable` or undecided, a
   volume command is refused by the authority rather than sent, and `remembered` is preserved
   for the next session.
8. **Never tears down video.** A volume failure never stops the stream, never retries the
   transport, and never prevents audio from being carried.
9. **Snapshot and restart.** `effective` and `last_audible` live in `SessionSnapshot` and are
   cleared by the existing transient-authority stripping in `session/persistence.py:20`, beside
   `muted`. `remembered` is reloaded from the receiver record on startup.

### 5.5 Routing and event acceptance

Naming the message is not enough; three closed sets must accept it:

- `SessionManager._route` (`session/engine.py:776`) must route `SetVolume` to `Route.audio`
  alongside `SetMuted` and `RetryAudio`;
- `session/fencing.py:89` must classify the volume result as an audio result, so its own
  operation id is honoured rather than being mistaken for a stream-start event;
- `session/audio.py` claim sets (`_AUDIO_EFFECTS`, `_AUDIO_EVENTS`) must include the new effect
  kind and event kind.

Proof of delivery and stale rejection must go through `SessionManager`, not only through direct
handler calls.

### 5.6 Storage

`SavedReceiver` gains `remembered_volume: int | None` beside `remembered_resolution`, and
inherits `PreferenceAccess` unchanged: an ambiguous, replaced, non-persistable or non-continuity
receiver never inherits a level. Values are stored 1–100; **0 is not storable**.

Three separate concerns, kept separate:

- **Record version.** The receiver record moves to version 2 and version-1 records still load,
  with no remembered volume. The global `VERSION` constant (`storage/codecs.py:89`) must not be
  bumped for the other schemas, so the codec gains a per-schema accepted-version set.
- **Volume is a preference, not identity.** A malformed or out-of-range volume drops **only the
  volume**: the rest of the record loads, one diagnostic is raised, and the receiver keeps its
  remembered resolution and trust. Identity and structural corruption still refuse the whole
  record as today. This is deliberate: `storage/repositories.py:197` currently fails the entire
  receiver listing on one bad record, and `session/recovery.py:227` propagates that while
  startup admission is blocked, so "refuse the record" would let one bad integer withhold every
  other preference and block recovery.
- **One writer.** Persistence reuses the existing read-modify-write pattern
  (`session/virtual.py:816` — `receivers.get` → mutate → `put`) through a single shared
  receiver-preference writer, so a remembered volume and a remembered resolution cannot
  overwrite each other. The presenter refreshes its cache after a successful write and reports a
  write failure; it never shows a level it failed to store as if it were durable.

### 5.7 UI

`Volume` row, immediately under `Start`/`Stop`/`Pair`, as accepted in the mockup:

- label `Volume`, horizontal slider **0–100** in 1% steps, right-aligned readout (`30%`, `Mute`
  at 0), accessible name `Receiver volume`;
- the row is enabled only when **all** hold: proven `target_volume`, a selected receiver, a live
  audio-capable phase, a current stream, no cleanup pending, and the same trust constraints the
  mute button already applies (`ui/state.py:867`). Otherwise it is disabled with the first
  applicable reason, in this precedence: no backend support, no receiver, not streaming, audio
  unavailable or video-only, cleanup in progress;
- the readout shows `effective` when known, `Mute` when the session is muted, and `—` when the
  stream has no acknowledged level. It never invents a number;
- keyboard: tab-reachable, arrow keys 1%, page keys 10%, Home/End to the ends;
- the speaker button keeps its icons and interaction, and its copy is updated: muting is
  session-only and unmuting returns to the last audible level (today's text promises the
  receiver keeps its own volume, which is exactly what this design changes);
- the presenter suppresses render-time signals while it programmatically sets the slider, as the
  existing pane already does for other controls;
- activity log: three registered codes with bounded integer fields — level applied, level change
  failed, and volume unavailable with a fixed reason. Capability absence is reported once per
  session, not on every render.

### 5.8 Diagnostics and activity

`diagnostics/activity.py:75` and the closed code registry at `:211` must accept the new codes and
the integer field; `diagnostics/catalog.py:50` must carry the error code
`audio_volume_failed` with its fixed message and recovery actions. Emission is once per
operation on failure and on transition for availability; discarded stale completions emit
nothing, matching the existing fencing behaviour. `AudioLifecycle._emit()` swallows sink errors
(`session/audio.py:266`), so the proof must assert the emitted events rather than assume they
happen.

## 6. Change inventory

Every place the feature touches, so nothing is discovered mid-implementation:

| Area | Files |
| --- | --- |
| Backend patch | `vendor/doubletake/0004-target-volume.patch`, `docs/backend-fork.md` |
| Proof | `backend/contract.py` (surface tables, feature flag, status decode) |
| Typed control | `backend/control.py` (operation, payload union, fence set, argv, ack), `process/ownership.py` |
| Transport | `transport/doubletake.py` (`set_volume`, pending level before connect, status field) |
| Domain | `domain/messages.py`, `domain/ports.py`, `domain/resources.py` (`VolumeLevel`, `CapabilityReport.volume`), `domain/records.py` (`SessionSnapshot`) |
| Session | `session/audio.py`, `session/engine.py`, `session/fencing.py`, `session/persistence.py`, `session/virtual.py` |
| Storage | `storage/codecs.py`, `storage/repositories.py` |
| UI | `ui/state.py`, `ui/presenter.py`, `ui/window.py` |
| Diagnostics | `diagnostics/activity.py`, `diagnostics/catalog.py` |
| Docs | `docs/operations.md`, `docs/backend-fork.md`, `docs/backend-compatibility.md` |
| Tests | see §8, including the pinned inventories in `tests/domain/test_contracts.py:68` and `tests/environment/test_installed_contracts.py:57` |

## 7. Error handling

| Situation | Behaviour |
| --- | --- |
| Backend proves `target_volume` | Row enabled; level applied before `connect`, and on change |
| Backend proves only mute (today's build) | Row disabled with one reason; mute unchanged; one diagnostic per session |
| Surface inconsistent (command without token, or token without command) | Whole backend refused, exactly as today's unrecognised surface |
| `volume` command refused, failed or timed out | `effective`/`last_audible` unchanged; one diagnostic; audio still carried |
| Stale volume result after a newer one | Discarded by operation id; no event |
| Receiver replaced mid-session | New stream has no `effective`/`last_audible`; a replaced identity inherits no remembered level |
| Stored volume malformed or out of range | Only the volume preference is dropped and reported; the rest of the record loads |
| Level requested with audio unavailable | Refused by the authority, never sent; reason shown |
| Preference write fails | Reported; UI does not present the level as durable |
| Sender patched, ctl not (or the reverse) | Surface inconsistent → mute-only, fail closed |

## 8. Test strategy

All offline; no live receiver, no network.

- **Contract:** both ctl tables recognized; volume row without token refused; token without row
  refused; status decode with `volume` absent, 0, 100, out of range, non-integer; the profile
  string is unchanged for every surface, so the twelve existing profile branches stay covered.
- **Carried-code proof (Go, offline):** compile the patch's mapping function and its command
  parsing with a fake RTSP boundary, and assert: 0/1/50/99/100 map exactly and monotonically;
  `volume` with no stream records a pending level; `connect` afterwards applies the pending level
  instead of 0 dB; a live stream applies immediately; two streams to two targets do not cross;
  the last audible level survives `mute` and is restored by `unmute`; a refused ack does not
  update the level; `status` serialises the acknowledged level and omits it when absent.
- **Transport:** target-scoped argv; an absent target is refused before dispatch; acknowledged
  integer returned; fence respected; pending-level send happens before `connect`.
- **Session (through `SessionManager`):** ordering against `connect`; fencing (slow 30 after 60);
  failure keeps both values; replacement stream inherits nothing; unmute restores the last
  audible level; slider-to-0 mutes; slider-up-while-muted is one atomic command; no command while
  audio is disabled or unavailable; snapshot clearing on reload.
- **Storage:** v1 record loads with no level; v2 round-trip; 0 and out-of-range dropped while the
  rest of the record loads; identity corruption still refuses the record; two concurrent
  preference writes both survive; `PreferenceAccess` refusal paths withhold the level.
- **UI:** enablement precedence per reason; `—` for unknown; arrow/page/Home/End steps;
  accessible name; no regression to the speaker button or the action grid; no render feedback
  loop when the slider is set programmatically.
- **Regression:** existing audio, mute, mirroring, capability, environment and storage suites
  stay green, including every test that pins today's no-volume surface and the legacy fixtures.

## 9. Delivery and installation

The application change is delivered in this repository. Making the feature *live* requires the
fourth patch to be applied and the two binaries rebuilt and installed: a separate, receipted step
at delivery, recorded in `docs/backend-fork.md` with the new patch digest, the base commit, the Go
toolchain, the build commands, and the verify command (`./doubletake -capabilities` must print
the four tokens and `ctl` usage must show `volume`). The previous pair is kept as `.prev` files so
the change is reverted by restoring two files. `docs/operations.md` gains the operator-facing
behaviour: remembered levels, what the disabled row means, and what a failed application reports.

## 10. Risks

1. **A shared binary changes.** Until the rebuild is installed the feature is inert; a
   half-installed pair is exactly what the two-table proof detects, and it fails closed to
   mute-only rather than to a wrong assumption.
2. **Receivers differ in how they treat `SET_PARAMETER volume`.** Confirmation means the receiver
   acknowledged the command, not that its speaker output matches the number. A receiver that
   ignores the parameter still acknowledges, and the app cannot tell. This is stated in the
   operations documentation rather than papered over.
3. **Record version bump.** A v1 record must never be lost: the v2 decoder reads both, and only
   the volume preference is droppable.
4. **Perceived loudness curve.** 50% is −6 dB (half amplitude), not half perceived loudness. Both
   endpoints are exact; changing the curve later is one function in one place.
5. **Pending startup level is daemon state.** It must be bounded to the daemon's lifetime, cleared
   for a target that never connects, and never applied to a different receiver.

## 11. What must not regress

- Mute stays session-only and receiver-local; Stop disconnects only that receiver.
- Audio-decision, video-only and retry-audio paths keep their current states and messages.
- Capability gating keeps failing closed; no unproven capability is ever inferred.
- No secret reaches argv, metadata, activity log or the pane.
- The no-remote delivery path stays a local merge only.
- Startup recovery and the receiver listing survive one bad preference.
