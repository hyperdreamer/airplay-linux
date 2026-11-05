# Audio volume control — design

- **Status:** design revision 4 after independent review rounds 1–3; awaiting review round 4.
- **Date:** 2026-11-05 (revision 4)
- **Delivery target:** `/data/home/guest/Development/airplay-linux`, branch `master`, no remote.
- **Mockup record:** `docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`

## Revision history

**Revision 2** fixed round 1 (audio is carried by `connect`; no new profile string; no remote
readback claim). **Revision 3** fixed round 2 by carrying the level with the connect intent instead
of staging it in the daemon.

**Revision 4** fixes round 3, and simplifies the design rather than adding to it. Round 3's five
blockers shared two causes, and both are removed:

- the connect acknowledgement is asynchronous (`internal/daemon/daemon.go:812-820` launches
  `connectAndStream` in a goroutine and returns), so no acknowledgement can carry a finished
  setup receipt. Revision 4 **drops `AckReply.volume`** and takes its receipt from `status`, which
  the app already observes for `has_audio`.
- mute/unmute and volume were two knobs that had to be ordered against each other. On a
  volume-capable backend they are the same AirPlay parameter, so revision 4 makes the authority
  issue **one command type — `volume`** — and derives mute from it.

| Round-3 item | Resolution in revision 4 |
| --- | --- |
| B1 no complete receipt path for the applied level | §5.1.4 + §5.3: per-stream `volume_state` and `volume` in `status`; no ack field |
| B2 a refused write does not prove a fallback mute | §5.1.3: audible media is withheld locally until the level is acknowledged, so the fallback does not depend on the receiver honouring a mute |
| B3 explicit zero is indistinguishable from "not requested" | §5.1.2: `--volume` presence is the request; the value is separate, so `--volume 0` means silence |
| B4 the refusal state has no working Retry audio | §5.1.3 + §5.4.4: a later acknowledged `volume` on the same stream opens audio, so Retry is a volume retry, not a restart |
| B5 startup rules do not preserve the legacy path | §5.4.1: `--volume` is sent only when the backend proves the capability; legacy keeps `mute`/`unmute` and never sees a third argument |
| M1 pending per-stream command work | §5.4.5: one in-flight command per lease, coalesced to the latest desired level, retired on Stop/replacement/audio loss |
| M2 inventory gaps | §6 adds `cmd/doubletake/main.go`, `runtime.py`, `session/recovery.py`, `testing/adapters.py` |
| M3 proof gaps | §8 adds live dispatch, two-target isolation, status output and refusal/retry |

## 1. Background

The application can mute a receiver and nothing else. That is not a UI omission; it is the whole
surface the backend offers. `doubletake-ctl` publishes **eight** commands (`status`, `discover`,
`devices`, `connect`, `pin`, `disconnect`, `mute`, `unmute`) and the application proves that
surface before it enables any audio action (`backend/contract.py:59`).

The consequence is what the operator reported, and it is visible in the daemon source:

- during stream setup the daemon writes `SET_PARAMETER volume` with `audioVolumeBody(false)`,
  which is 0 dB — the receiver's **maximum** (`internal/airplay/mirror.go:1128-1135`, repeated at
  `:1138`);
- a failed setup write is logged as non-fatal and streaming continues at maximum
  (`mirror.go:1132-1133`);
- `SetAudioMuted(muted bool)` and `audioVolumeBody(muted bool)` can express only 0 dB and −144 dB
  (`mirror.go:2414-2437`);
- the receiver *does* report an `initialVolume` in its `/info` plist (`client.go:46`), which the
  daemon never uses as a level to apply.

So the chain has two levels, maximum and silence, and no way to state one in between. The mute
button's own tooltip admits the gap: "the receiver keeps its own volume setting".

Only the daemon owns the receiver's RTSP session, so only a daemon change can set a level; and this
repository already carries that change mechanism — three patches in `vendor/doubletake/` applied to
a pinned upstream checkout, installed to `/home/bin`. A fourth patch is the supported way.

## 2. Goals

1. A user can set a receiver's volume anywhere from 1% to 100%, and 0% means mute.
2. The level is remembered **per receiver** and applied as part of starting a stream, so a new
   session never begins at maximum.
3. Unmute returns to the last audible level instead of maximum.
4. Mute, unmute, Stop, Retry audio, the audio-decision prompt and receiver replacement keep their
   current guarantees.
5. A backend without volume support keeps today's behaviour exactly: mute works, the volume row is
   disabled with one plain reason, and nothing regresses.
6. No live-receiver dependency in the automated suite: all proof stays offline.

## 3. Non-goals

- Sensing the receiver's actual volume, or noticing changes made with the TV's own remote.
- Balancing or per-application mixing on the sending machine.
- Volume for the local speakers; the level is the receiver's.
- Virtual-display work, discovery changes, or any capability this design does not name.

## 4. Decisions taken with the operator

| # | Question | Decision |
| --- | --- | --- |
| 1 | Who owns the level | **A.** The app remembers a level per TV and re-applies it when it starts that TV's stream. A change made with the TV's own remote survives only until the next session start. |
| 2 | Where the control lives | **C.** A dedicated `Volume` row under `Start`/`Stop`/`Pair`, full pane width, always visible. The speaker button stays the only mute control. |
| 3 | Slider and mute | **A.** Moving the slider above 0% lifts the mute; dragging to 0% mutes. The speaker button still mutes and unmutes in one click, returning to the last audible level. |

The chosen mockup is region C of
`docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`.

## 5. Design

### 5.1 Backend patch `vendor/doubletake/0004-target-volume.patch`

Stacks on 0001+0002+0003. Scope, by file, against the pinned tree:

| File | Change |
| --- | --- |
| `internal/airplay/mirror.go` | `audioVolumeBody(percent int)`: 0 → `-144` dB, 1–100 → `20*log10(percent/100)` dB, 100 → exactly `0` dB. New `SetAudioLevel(percent int) error` beside `SetAudioMuted`. Both setup writes at `:1128` and `:1138` use the configured initial percent instead of a hardcoded 0 dB. |
| `internal/airplay/client.go` | `StreamConfig` gains a requested initial level as `*int` (presence and value are separate); nil reproduces today's behaviour exactly. |
| `internal/daemon/…` | Carry the requested level from the connect request into `StreamConfig`; hold per stream the last acknowledged percent and a `volume_state`; serve `volume` on a live stream; report both in `status`. |
| `cmd/doubletake-ctl` | `connect [target] [--volume <0-100>] [PIN-or-password]` and `volume <0-100> [target]`, parsed explicitly — today's `args[2]` slot is the PIN (`cmd/doubletake-ctl/main.go:49`), so a bare positional must never be assumed to be a level. `-capabilities` gains `target-volume` in `cmd/doubletake/main.go`. |

Four behavioural rules:

1. **One mapping.** `audioVolumeBody` is the only percent-to-dB conversion, so both endpoints are
   exact and no caller can produce a value outside 0–100.
2. **Requested is not the same as unset.** `--volume` presence is the request; `--volume 0` means
   silence. Unset reproduces today's 0 dB setup write.
3. **Audible media is withheld until the level is acknowledged.** When a level was requested, the
   daemon does not start audio capture for that stream until the receiver acknowledges it (the
   current start point is `internal/daemon/daemon.go:1246`, gated on `session.HasAudio()`). If the
   write is refused or never acknowledged, audio is **not** flowing and `status` says
   `volume_state: refused`. A later acknowledged `volume` on the same stream opens audio. This is
   local suppression, so it does not depend on the receiver honouring a mute, and it cannot fall
   back to maximum.
4. **Acknowledge, don't echo.** A stream's stored percent changes only when the receiver
   acknowledged the `SET_PARAMETER` (RTSP 200). `status` reports per stream the optional `volume`
   (integer 0–100, absent until something is acknowledged) and `volume_state`
   (`unrequested | pending | applied | refused`). Absent is not 0, and `pending` is not `refused`.

The sender's help output does not change, so the pinned sender-flag set is untouched.

### 5.2 Application backend proof

The existing recogniser compares the ctl table by exact argument signature
(`backend/contract.py:822`), proves target scope separately (`:786`), and rejects a source-help
surface whose tokens are missing (`:844`). Revision 4 **extends** it; it does not replace those
rules.

```text
volume_row      := the usage row "volume <0-100> [target]"
volume_row_ok   := volume_row.arguments == "<0-100> [target]"
                   and _target_scope_proven(volume_row)         # same predicate as mute/unmute
connect_row_ok  := connect row == "connect [target] [--volume <0-100>] [PIN-or-password]"

ctl_pinned      := signatures == the pinned 8-row table
ctl_volume      := signatures == the pinned table with the extended connect row
                   and the extra volume row, with volume_row_ok and connect_row_ok

target_volume   := ctl_volume
                   and {"capabilities-v1", "status-source", "target-volume"} ⊆ tokens

inconsistent    := (ctl_volume and not target_volume)
                   or (ctl_pinned and "target-volume" in tokens)

verified        := sender flags in {pinned, source}
                   and (ctl_pinned or ctl_volume)
                   and not inconsistent
                   and source tokens consistent with the sender's source surface
```

`target_volume` is true only for a fully coherent extended surface: the exact volume signature, its
affirmative scope statement, and the token. A missing token, a borrowed token, a token without the
row, or a row that says the target is ignored all leave it false; the incoherent combinations refuse
the backend exactly as today's unknown surface does.

**No new profile string.** The profile stays `doubletake-0.5+source` (or `doubletake-0.5`), so the
twelve literal profile comparisons keep working (`transport/doubletake.py:525`,
`process/launcher.py:80`, `process/daemon_registry.py:251,343,372,946,1081`,
`process/supervisor.py:572`, `runtime.py:424,454`, `backend/control.py:584,589`).
`BackendFeatures` grows `target_volume: bool`, false everywhere else.

### 5.3 Domain, port, transport and typed control

- `VolumeLevel`: validated integer 0–100, `0` meaning silence.
- `SetVolume(receiver_id, level)`, added to the closed command union (`domain/messages.py:130`).
- `StartRequest.volume: VolumeLevel | None` (`domain/resources.py`) — the remembered level is an
  input to starting a stream. The presenter fills it from the receiver preference it already holds,
  after `PreferenceAccess` validation, **and only when the backend proves `target_volume`**. Reconnect
  reuse carries the snapshot's value so a retry cannot invent a level.
- `TransportPort.start_stream(..., initial_volume: VolumeLevel | None)` and
  `TransportPort.set_volume(handle, level, context) -> Result[int]`.
- `BackendStream.volume: int | None` and `BackendStream.volume_state: str`, decoded strictly
  (`unrequested|pending|applied|refused`); any other value refuses the status reply.
- `TransportEventKind.volume_result` with `VolumePayload(level, ok)`; effect kind `volume` joins
  `_AUDIO_EFFECTS` (`session/audio.py:67`).
- `CapabilityReport.volume: bool` (`domain/resources.py:222`), produced by
  `policy/capabilities.py:536` from the proven feature.
- Typed control: `BackendOperation.volume` (`backend/control.py:47`), `VolumePayload` in the payload
  union (`:137`), membership in the dispatch-fence set (`:160`), a target-scoped argv branch (`:372`)
  that **refuses a missing target**, the connect argv branch extended with the optional level in the
  documented position, acknowledgement decoding for the live command (`:912`), and authorisation in
  `process/ownership.py:318` conditioned on proven `target_volume`.

### 5.4 Session authority rules

Three values, never conflated:

| Value | Meaning | Lifetime |
| --- | --- | --- |
| `remembered` | The per-receiver preference, 1–100, or absent | Durable |
| `effective` | The level this stream last had acknowledged, 0–100, or unknown | Per transport lease |
| `last_audible` | The last acknowledged non-zero level on this stream, or absent | Per transport lease |

**One knob.** On a volume-capable backend the authority issues exactly one command type, `volume`,
and derives mute from it: `muted ⇔ effective == 0 and audio present`. `SetMuted` is translated —
muted → `volume 0`; unmuted → `volume (last_audible ?? remembered ?? 100)`. The `mute`/`unmute` ctl
commands stay for legacy backends and for manual use, and the app never sends them on a
volume-capable stream. One command type means mute and volume can never be ordered against each
other.

1. **Startup is capability-conditioned.** `start_stream` receives `initial_volume` only when the
   backend proves `target_volume`, the audio policy is enabled, and a `remembered` level exists.
   Otherwise no level is requested and the stream behaves exactly as today.
2. **Startup confirmation comes from `status`.** After the exact handle is adopted, the authority
   waits within the existing connect budget for `volume_state` to leave `pending`:
   `applied` → `effective = volume`, `last_audible = volume` (when > 0); `refused` → §5.4.4;
   `unrequested` → `effective` unknown, as on a legacy backend. A timeout leaves `effective`
   unknown with one diagnostic; it never assumes the level was applied.
3. **A stale result is discarded silently** by operation id, as `session/fencing.py:78` and
   `session/engine.py:532` already do — no event, no diagnostic.
4. **A refused startup level is recoverable, not fatal.** The daemon is already withholding audio,
   so the stream is video-only and `volume_unavailable` is reported. `RetryAudio` re-issues
   `volume <remembered>` on that same stream; the daemon answers with `volume_state = applied` and
   opens audio, so recovery does not need an audio restart. `Start video only` keeps the current
   video-only behaviour, and `Stop` tears the stream down. A `has_audio` observation alone never
   clears the refusal.
5. **Bounded command work.** At most one `volume` command is in flight per transport lease; newer
   desired levels coalesce into a single latest value, issued when the current one completes.
   Queued work is retired on Stop, on lease replacement and on audio loss, so a slider drag cannot
   accumulate commands. `effective`/`last_audible` change only on acknowledgement.
6. **Lease scoping.** A replacement stream starts with no `effective` and no `last_audible`; it
   acquires them only through its own acknowledgement.
7. **Transition table** — the whole contract:

| Intent | Condition | One command | On acknowledgement |
| --- | --- | --- | --- |
| Start | `remembered` present, backend proves the capability, audio enabled | `connect … --volume r` | `effective = volume`, `last_audible = volume` |
| Start | no `remembered`, or no proven capability | `connect` (unchanged) | `effective` unknown |
| Slider to n ≥ 1 | audio `Enabled`, `Muted`, or a refused startup on this lease | `volume n` | `effective = n`, `last_audible = n`, `remembered = n` then persist |
| Slider to 0 | audio `Enabled` or `Muted` | `volume 0` | `effective = 0`, `last_audible` unchanged, `remembered` unchanged |
| Speaker mute | audio `Enabled` | `volume 0` | `effective = 0`, `last_audible` unchanged |
| Speaker unmute | `last_audible` known | `volume last_audible` | `effective = last_audible` |
| Speaker unmute | `last_audible` unknown | `volume (remembered ?? 100)` | `effective = that` |
| Retry audio | startup refused on this lease | `volume remembered` | `effective` set, daemon opens audio |
| Legacy backend mute/unmute | backend does not prove the capability | `mute` / `unmute` unchanged | unchanged |

`remembered` changes only on a **user-initiated** level, and only after acknowledgement. A failed
preference write is reported and leaves the previous `remembered` in place while `effective` and
`last_audible` keep the applied value; the UI shows the applied level and says it could not be
saved.

8. **No audio, no level.** With audio `Disabled` (video-only), `Unavailable`, or undecided, a volume
   command is refused rather than sent, and `remembered` is preserved — except the explicit Retry
   row above, which is the only way out of `refused`.
9. **Never tears down video.** A live volume failure never stops the stream and never retries the
   transport.
10. **Snapshot and restart.** `effective` and `last_audible` live in `SessionSnapshot` and are
    stripped by the transient-authority rule in `session/persistence.py:20` beside `muted`;
    `remembered` is reloaded from the receiver record by `session/recovery.py`.

### 5.5 Routing and event acceptance

`SessionManager._route` (`session/engine.py:776`) routes `SetVolume` to `Route.audio`;
`session/fencing.py:89` classifies the volume result as an audio result so its own operation id is
honoured; `session/audio.py`'s claim sets include the new effect and event kinds. Proof goes through
`SessionManager`, not only through direct handler calls.

### 5.6 Storage

`SavedReceiver` gains `remembered_volume: int | None` beside `remembered_resolution` and inherits
`PreferenceAccess` unchanged. Values are stored 1–100; **0 is not storable**.

- **Record version.** The receiver record moves to version 2; version-1 records still load with no
  remembered volume. The global `VERSION` constant (`storage/codecs.py:89`) is not bumped for other
  schemas, so the codec gains a per-schema accepted-version set.
- **Volume is a preference, not identity.** A malformed or out-of-range volume drops **only the
  volume**, with one diagnostic; the rest of the record loads. Identity and structural corruption
  still refuse the whole record. `storage/repositories.py:197` fails the entire receiver listing on
  one bad record and `session/recovery.py:227` propagates that while startup admission is blocked,
  so refusing the record would let one bad integer withhold every preference.
- **One writer.** A single shared receiver-preference writer reuses the read-modify-write shape at
  `session/virtual.py:816`, is instantiated once at the composition root (`runtime.py:475,488`), and
  is used by both the resolution and volume paths so they cannot overwrite each other. The presenter
  refreshes its cache after a successful write and reports a failure.

### 5.7 UI

`Volume` row, immediately under `Start`/`Stop`/`Pair`, as accepted in the mockup:

- label `Volume`, slider **0–100** in 1% steps, readout (`30%`, `Mute` at 0), accessible name
  `Receiver volume`;
- enabled only when all hold: proven `target_volume` (via `CapabilityReport.volume`), a selected
  receiver, a live audio-capable phase, a current stream, no cleanup pending, and the trust
  constraints the mute button already applies (`ui/state.py:867`). Disabled precedence: no backend
  support, no receiver, not streaming, audio unavailable or video-only, cleanup in progress — each
  with one fixed reason;
- readout: `effective` when known, `Mute` at 0, `—` when unknown. It never invents a number;
- keyboard: tab-reachable, arrows 1%, page keys 10%, Home/End;
- the speaker button keeps its icons and interaction; its copy is derived from proven capability —
  restoration wording only where the backend can restore a level;
- the presenter suppresses render-time signals while it sets the slider programmatically;
- activity log: registered codes with bounded integer fields (level applied, level change failed,
  volume unavailable with a fixed reason); capability absence is reported once per session.

### 5.8 Diagnostics and activity

`diagnostics/activity.py:75` and the closed registry at `:211` accept the new codes and the integer
field; `diagnostics/catalog.py:50` carries `audio_volume_failed` and `volume_unavailable` with fixed
messages and recovery actions. Emission is once per operation on failure and on transition for
availability; stale completions emit nothing. `AudioLifecycle._emit()` swallows sink errors
(`session/audio.py:266`), so proofs assert emitted events.

## 6. Change inventory

| Area | Files |
| --- | --- |
| Backend patch | `vendor/doubletake/0004-target-volume.patch`; backend files `internal/airplay/{mirror,client}.go`, `internal/daemon/*`, `cmd/doubletake-ctl/main.go`, `cmd/doubletake/main.go` |
| Proof | `backend/contract.py` (surface tables, feature flag, status decode) |
| Capability producer | `policy/capabilities.py`, `domain/resources.py` (`CapabilityReport.volume`) |
| Typed control | `backend/control.py`, `process/ownership.py` |
| Transport | `transport/doubletake.py`, `transport/targets.py` |
| Domain | `domain/messages.py`, `domain/ports.py`, `domain/resources.py`, `domain/records.py` |
| Session | `session/audio.py`, `session/engine.py`, `session/fencing.py`, `session/persistence.py`, `session/recovery.py`, `session/virtual.py`, `session/mirror.py`, `session/reconnect.py` |
| Composition | `runtime.py` (preference writer, capability report wiring) |
| Storage | `storage/codecs.py`, `storage/repositories.py` |
| UI | `ui/state.py`, `ui/presenter.py`, `ui/window.py` |
| Testing aids | `testing/adapters.py` (`FakeTransport.start_stream`, `set_volume`) |
| Diagnostics | `diagnostics/activity.py`, `diagnostics/catalog.py` |
| Docs | `docs/operations.md`, `docs/backend-fork.md`, `docs/backend-compatibility.md` |
| Tests | §8, including the pinned inventories in `tests/domain/test_contracts.py:68` and `tests/environment/test_installed_contracts.py:57`, the backend fixtures and the fake operation set |

## 7. Error handling

| Situation | Behaviour |
| --- | --- |
| Backend proves `target_volume` | Row enabled; level applied through `connect`, and on change |
| Verified backend without volume | Row disabled with one reason; `mute`/`unmute` as today; one diagnostic per session |
| Incoherent surface | Whole backend refused, exactly as today's unknown surface — never downgraded |
| Startup level refused | Daemon withholds audio; `volume_state = refused`; `volume_unavailable`; video preserved; Retry re-issues `volume` |
| Startup level never acknowledged within the connect budget | `effective` unknown, one diagnostic, audio still withheld by the daemon; Retry available |
| Live `volume` refused, failed or timed out | `effective`/`last_audible` unchanged; one diagnostic; audio keeps playing |
| Stale volume result | Discarded silently |
| Receiver replaced mid-session | New stream has no `effective`/`last_audible`; a replaced identity inherits no remembered level |
| Stored volume malformed | Only the volume preference is dropped and reported |
| Level requested with audio unavailable | Refused, except the explicit Retry row |
| Preference write fails | Reported; `effective` stands; `remembered` unchanged; UI says it could not be saved |
| Sender patched, ctl not (or the reverse) | Incoherent surface → whole backend refused |
| Legacy backend with a saved level | `--volume` is never sent (capability-conditioned), so downgrade cannot break streaming |

## 8. Test strategy

All offline; no live receiver, no network.

- **Contract:** both ctl tables recognized; volume row without token refused; token without row
  refused; a volume row whose scope statement is missing, negated or borrowed refused; a source-help
  surface without tokens refused (unchanged); status decode for `volume` absent/0/100/out-of-range/
  non-integer and every `volume_state`, including an unknown state; profile string unchanged for
  every surface.
- **Carried-code proof (Go, offline, GOPROXY=off).** The patch factors its decisions into
  declarations a test can compile against a fake RTSP writer, in the carried-code style of
  `tests/backend/test_carried_source_patch.py`: the mapping, the setup decision, the `volume`
  dispatch with target selection across two streams, the refusal/retry bookkeeping, and status
  output. Assertions: 0/1/50/99/100 map exactly and monotonically; the setup write uses the
  requested percent, not 0 dB; unset keeps 0 dB; explicit zero silences; a refused level withholds
  audio and reports `refused`; an acknowledged `volume` afterwards reports `applied` and opens audio;
  a rejected write leaves the previous acknowledged percent; two targets do not cross; `status`
  distinguishes absent, 0 and `refused`; the connect parser accepts `--volume` in the documented
  position, rejects out-of-range, and never treats a flag as a PIN.
- **Transport:** `start_stream` passes the initial level into the connect argv only when supplied;
  an absent target is refused before dispatch; acknowledged level returned; fence respected; timeout
  leaves `effective` unknown.
- **Session (through `SessionManager`):** every row of §5.4.7, including a legacy backend with a
  saved level, a refused startup followed by Retry, the coalescing queue under a slider drag, Stop
  during a pending command, and replacement mid-flight; snapshot clearing on reload; FakeTransport
  parity.
- **Storage:** v1 record loads with no level; v2 round-trip; 0 and out-of-range dropped while the
  rest of the record loads; identity corruption still refuses the record; concurrent preference
  writes both survive; `PreferenceAccess` refusal paths withhold the level.
- **UI:** enablement precedence per reason; `—` for unknown; arrow/page/Home/End; accessible name;
  capability-derived tooltip copy on both backend kinds; no render feedback loop.
- **Regression:** existing audio, mute, mirroring, capability, environment and storage suites stay
  green, including every test pinning today's no-volume surface and the legacy fixtures.

## 9. Delivery and installation

The application change is delivered in this repository. Making it live requires the fourth patch to
be applied and both binaries rebuilt and installed: a separate, receipted step at delivery, recorded
in `docs/backend-fork.md` with the patch digest, the base commit, the Go toolchain, the build
commands and the verify command (`./doubletake -capabilities` prints the four tokens; ctl usage shows
`volume` and the extended `connect`). The previous pair is kept as `.prev` files so the change
reverts by restoring two files. `docs/operations.md` gains the operator-facing behaviour: remembered
levels, the disabled row, and what a failed application reports.

## 10. Risks

1. **A shared binary changes.** Until the rebuild is installed the feature is inert. A
   half-installed pair is an incoherent surface and fails closed to a refused backend.
2. **Receivers differ in how they treat `SET_PARAMETER volume`.** Confirmation means
   "acknowledged", not "the speaker output matches". A receiver that acknowledges and ignores the
   parameter cannot be detected; the documentation says so.
3. **Withheld audio is a behavioural change on the patched backend only.** It applies only when a
   level was requested, so an unpatched caller and a caller without a saved level see today's
   behaviour.
4. **One Go change is not free.** `SetAudioLevel` and the setup decision sit in the AirPlay library,
   so the patch must keep `mute`/`unmute` and unset-connect behaviour byte-compatible.
5. **Perceived loudness curve.** 50% is −6 dB (half amplitude), not half perceived loudness. Both
   endpoints are exact; the curve is one function in one place.
6. **Record version bump.** A v1 record must never be lost: the v2 decoder reads both, and only the
   volume preference is droppable.

## 11. What must not regress

- Mute stays session-only and receiver-local; Stop disconnects only that receiver.
- Audio-decision, video-only and retry-audio paths keep their states and messages on legacy
  backends; on the patched backend the refusal state reuses them rather than inventing a new UI.
- Capability gating keeps failing closed; no unproven capability is inferred.
- No secret reaches argv, metadata, activity log or the pane.
- The no-remote delivery path stays a local merge only.
- Startup recovery and the receiver listing survive one bad preference.
- A plain `connect`, `mute` and `unmute` from an unpatched caller behave exactly as today.
