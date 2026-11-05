# Audio volume control — design

- **Status:** design revision 8 after independent review rounds 1–7; awaiting review round 8.
- **Date:** 2026-11-05 (revision 8)
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

**Revision 5** fixes round 4. Round 4's two blockers were both about the receipt channel and about
one promise that overreached the RTSP layer:

| Round-4 item | Resolution in revision 5 |
| --- | --- |
| B1 the startup receipt never reaches the authority | §5.3/§5.4.2: `start_audio` becomes the typed carrier (`AudioStartReceipt`) built from the per-stream status read the transport already performs, so no new poll owner or subscription is invented |
| B2 "never acknowledged" cannot preserve a retryable stream | §5.1.3/§5.4.4: a complete receiver rejection is recoverable and keeps video; an incomplete or timed-out RTSP response follows the library's existing unusable-connection rule, fails setup and is cleaned up. The retry promise is narrowed to what the protocol can honestly support |
| M1 coalescing ignores workers that outlive a timeout | §5.4.5: "complete" means the owned worker settled; the next value is drained only after settlement, one worker and one slot per lease |
| M2 legacy omission of the new status fields | §5.3/§7: the fields stay optional in the shared decoder, and a *capable* backend's missing receipt is refused in the volume path, not in the shared decoder |
| M3 inventory misses the upstream mapping test | §6 adds `internal/airplay/mirror_setup_test.go`, whose `TestAudioVolumeBody` calls the bool signature |
| m refused-startup slider admission | §5.4.7: the exception is removed; Retry is the only way out of `refused` |

**Revision 6** fixes round 5. Round 5 closed every prior item and found one race plus two settlement /
transition gaps, all of which come from the daemon owning the audio decision while the app owns the
user's intent:

| Round-5 item | Resolution in revision 6 |
| --- | --- |
| B1 "Start video only" cannot supersede a withheld-stream retry | §5.1.5/§5.4.6: the patch adds an acknowledged per-stream audio gate. Video-only closes the gate before the app publishes `Disabled`, so a late acknowledgement cannot open audio (the two-way form was replaced by a one-way latch in revision 7) |
| M1 Python settlement is not daemon settlement | §5.1.6/§5.4.5: the daemon coalesces the latest level per stream behind one worker with a bounded write deadline, so application-side settlement never has to stand in for daemon-side work |
| M2 receipt outcomes lack an actor/event contract | §5.4.8: an outcome table fixes `AudioState`, `intent_stage`, mute, and which events may be emitted for every receipt outcome |
| m `FakeTransport.start_audio` not inventoried | §6/§8: the fake is inventoried and its legacy `Ok(None)` successes are normalized to a receipt |

**Revision 7** fixes round 6, and again removes state rather than adding it. Rounds 5–6 kept a
*mutable* audio gate in the daemon and then had to synchronise it with the app across stream
replacement, reconnect and uncertain timeouts — a distributed state machine. Revision 7 makes the
intent travel with the stream and the live gate one-way:

| Round-6 item | Resolution in revision 7 |
| --- | --- |
| F1 the audio barrier is outside the proven and typed surface | §5.2/§5.3: the `audio` row joins the pinned extended table with its own scope statement, and its typed operation, payload, fence, ownership authorisation and acknowledgement are inventoried |
| F2 video-only intent is lost on a new stream | §5.1.2/§5.4.1: `connect` carries the audio intent as well as the level, so an explicitly video-only start suppresses the setup writes and capture before anything plays — no live barrier is needed for a stream that never had audio |
| F3 a timed-out close can make Retry falsely confirm | §5.1.5/§5.4.6: the gate is a **latched one-way close** with no `audio on`, so an obsolete acknowledgement cannot reopen it; an uncertain close admits repeated close and Stop, not Retry |
| F4 the pending receipt is a dead end | §5.4.8: `pending` collapses into `Unavailable / audio_decision`, which is retryable, and Retry re-samples the receipt instead of resting in `Starting` |
| M1 gate quiescence and worker retirement unproven | §5.1.5/§8: a close acknowledgement means existing and in-progress audio production for that stream has quiesced, with carried-Go probes for both acknowledgement orders and late completions |

**Revision 8** fixes round 7. Round 7 closed F1, F2, F4 and M1 and left one blocker: because the
video-only *policy* was only published after the daemon acknowledged the close, an automatic
reconnect could build its next stream from a still-enabled policy. Revision 8 publishes the policy
at admission and defers only the confirmed `AudioState.Disabled`, so the next `connect` carries
`--audio off` regardless of whether the old close was ever acknowledged. It also adds the audio
row's own negative proof cases and the connect parser's `--audio` cases.

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
| `internal/daemon/…` | Carry the requested level and the audio intent from the connect request into `StreamConfig`; hold per stream the last acknowledged percent, a `volume_state` and a **latched one-way audio close**; coalesce level changes behind one worker per stream; serve `volume` and `audio off` on a live stream; report level and state in `status`. |
| `cmd/doubletake-ctl` | `connect [target] [--audio on\|off] [--volume <0-100>] [PIN-or-password]`, `volume <0-100> [target]` and `audio off [target]`, parsed explicitly — today's `args[2]` slot is the PIN (`cmd/doubletake-ctl/main.go:49`), so a bare positional must never be assumed to be a level. `-capabilities` gains `target-volume` in `cmd/doubletake/main.go`. |

Four behavioural rules:

1. **One mapping.** `audioVolumeBody` is the only percent-to-dB conversion, so both endpoints are
   exact and no caller can produce a value outside 0–100.
2. **Requested is not the same as unset, for both knobs.** `--volume` presence is the request;
   `--volume 0` means silence; unset reproduces today's 0 dB setup write. `--audio off` suppresses
   both setup volume writes and audio capture for that stream; unset reproduces today's behaviour.
3. **Audible media is withheld until the level is acknowledged.** When a level was requested, the
   daemon does not start audio capture for that stream until the receiver acknowledges it (the
   current start point is `internal/daemon/daemon.go:1246`, gated on `session.HasAudio()`). This is
   local suppression, so it does not depend on the receiver honouring a mute, and it cannot fall
   back to maximum. Three outcomes are distinguished, because they have different consequences:
   - **Complete rejection** — the receiver answered and refused. The control connection is still
     usable, video is unaffected, audio stays withheld, and `status` says `volume_state: refused`.
     A later acknowledged `volume` on that stream opens audio.
   - **Incomplete or timed-out response** — the existing rule that an unfinished response leaves the
     RTSP control connection unusable applies unchanged. Setup fails, the stream is cleaned up by
     the current failure path, and the application sees an ordinary transport failure. The design
     makes no stronger promise here, and the write is bounded well inside the application's
     source-prepared handshake budget so the daemon cannot outlive it silently.
   - **Never requested** — today's behaviour, unchanged.
4. **Acknowledge, don't echo.** A stream's stored percent changes only when the receiver
   acknowledged the `SET_PARAMETER` (RTSP 200). `status` reports per stream the optional `volume`
   (integer 0–100, absent until something is acknowledged) and `volume_state`
   (`unrequested | pending | applied | refused`). Absent is not 0, and `pending` is not `refused`.
5. **The close is a latch, not a switch.** `audio off [target]` closes audio for that stream and
   there is **no reopen**: the stream's gate only ever goes from open to closed, and it dies with
   the stream. Acknowledgement of the close means existing and any in-progress audio production for
   that stream has quiesced, and every capture continuation is bound to its original stream entry,
   so a late step cannot start audio after the latch closed. Replacement and reconnect create a new
   stream whose intent comes from `connect` (§5.1.2), so they cannot inherit or defeat the latch.
6. **The daemon coalesces, so the app never has to wait for it.** Each stream keeps the latest
   desired percent and applies it through one worker; a newer value replaces the pending one. The
   live write carries one absolute deadline covering receiver-connection admission, the write, the
   response read and any Digest retry, so a slow receiver cannot accumulate queued writes behind
   the connection mutex. This is why the application's settlement rule (§5.4.5) is sufficient: it
   never has to represent daemon work.

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
audio_row       := the usage row "audio off [target]"
audio_row_ok    := audio_row.arguments == "off [target]" and _target_scope_proven(audio_row)
connect_row_ok  := connect row == "connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]"

ctl_pinned      := signatures == the pinned 8-row table
ctl_volume      := signatures == the pinned table with the extended connect row
                   and the extra volume and audio rows,
                   with volume_row_ok, audio_row_ok and connect_row_ok

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
- `TransportPort.start_stream(..., initial_volume: VolumeLevel | None)`,
  `TransportPort.set_volume(handle, level, context) -> Result[int]`, and
  `TransportPort.close_audio(handle, context) -> Result[None]` (the `audio off` gate barrier).
- `AudioStartReceipt` (`domain/records.py`): `audio: bool`, `volume_state: str | None`,
  `level: int | None`. `TransportPort.start_audio` returns `Result[AudioStartReceipt]` instead of
  `Result[None]`. On this profile the transport already fetches exactly one status entry for the
  stream in `_reported_stream_audio` (`transport/doubletake.py:1131-1155`), so the receipt is built
  from a read that already happens — no new poll owner, subscription or deadline is introduced, and
  callers that ignore the receipt keep working. The two new fields stay **optional** in the shared
  status decoder, which also serves handshake and mute replies.
- `BackendStream.volume: int | None` and `BackendStream.volume_state: str`, decoded strictly
  (`unrequested|pending|applied|refused`); any other value refuses the status reply.
- `TransportEventKind.volume_result` with `VolumePayload(level, ok)`; effect kind `volume` joins
  `_AUDIO_EFFECTS` (`session/audio.py:67`).
- `CapabilityReport.volume: bool` (`domain/resources.py:222`), produced by
  `policy/capabilities.py:536` from the proven feature.
- Typed control: `BackendOperation.volume` and `BackendOperation.audio_off` (`backend/control.py:47`),
  `VolumePayload` and `AudioGatePayload` in the payload union (`:137`), membership in the
  dispatch-fence set (`:160`), target-scoped argv branches (`:372`) that **refuse a missing target**,
  the connect argv branch extended with the optional intent and level in the documented positions,
  acknowledgement decoding for both live commands (`:912`), and authorisation in
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

1. **Startup carries the intent and, when it applies, the level.** `start_stream` receives the audio
   intent from the audio policy and, only when the backend proves `target_volume` and the policy is
   enabled and a `remembered` level exists, the level as well. An explicitly video-only start is sent
   as `--audio off`, so the daemon suppresses the setup volume writes and capture before anything can
   play; a start with no remembered level sends no `--volume` and behaves exactly as today. Because
   the intent travels with the request, a replacement stream or a reconnect cannot silently re-enable
   audio that the user turned off.
2. **Startup confirmation arrives in the audio-start receipt.** The authority calls
   `start_audio(handle, context)` as it already does, and reads `AudioStartReceipt.volume_state`:
   `applied` → `effective = level`, `last_audible = level` (when > 0); `refused` → §5.4.4;
   `pending` or absent on a capable backend → one diagnostic and `effective` unknown, never assumed;
   `unrequested` or a legacy backend → `effective` unknown, exactly as today. The deadline is the
   one that call already has; the authority never invents a second timer.
3. **A stale result is discarded silently** by operation id, as `session/fencing.py:78` and
   `session/engine.py:532` already do — no event, no diagnostic.
4. **A rejected startup level is recoverable; a broken connection is not.** A complete rejection
   leaves video running and audio withheld, reports `volume_unavailable`, and admits `RetryAudio`,
   which re-issues `volume <remembered>` on that same stream; the daemon answers with
   `volume_state = applied` and opens audio, so recovery needs no audio restart. An incomplete or
   timed-out response is an ordinary transport failure: the stream is cleaned up under the existing
   rules and the ordinary Retry/Start path applies, with no same-stream promise. `Start video only`
   keeps the current video-only behaviour and `Stop` tears the stream down. A `has_audio`
   observation alone never clears a refusal.
5. **Bounded command work.** One worker and one desired-value slot per transport lease. A newer
   desired level replaces the slot; the slot is drained only when the current worker has
   **settled**, not when the public wait timed out — today `_wait` returns on timeout without
   settling its child (`transport/doubletake.py:493`) while the worker still holds `stream.lock`
   (`:1165`), so "completion" must mean settlement or a slider drag would accumulate workers
   behind a surviving lock. The slot is retired on Stop, on lease replacement and on audio loss.
   `effective`/`last_audible` change only on acknowledgement.
6. **Lease scoping.** A replacement stream starts with no `effective` and no `last_audible`; it
   acquires them only through its own acknowledgement.
7. **Transition table** — the whole contract:

| Intent | Condition | One command | On acknowledgement |
| --- | --- | --- | --- |
| Start | `remembered` present, backend proves the capability, audio enabled | `connect … --volume r` | `effective = volume`, `last_audible = volume` |
| Start | no `remembered`, or no proven capability | `connect` (unchanged) | `effective` unknown |
| Slider to n ≥ 1 | audio `Enabled` or `Muted` | `volume n` | `effective = n`, `last_audible = n`, `remembered = n` then persist |
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

**Start video only is a latch, and a decision made before the stream exists needs no barrier at all.**
There are two cases and they are not the same:

- **Before or at start** — the audio policy is already `enabled=False`, so the connect request carries
  `--audio off`; the daemon never performs the setup volume writes and never starts capture. Nothing
  can start playing, and no live command is needed.
- **While a stream exists and is undecided** — choosing it does two things in one actor step,
  **in this order**: it records `AudioPolicy(enabled=False)` immediately, and it issues
  `close_audio` (`audio off`) for the current stream. Only the *confirmed* `AudioState.Disabled`
  waits for the daemon's acknowledgement.

The split matters because the policy is what a later stream is built from. `reconnect`
(`session/reconnect.py:319`) and the virtual path (`session/virtual.py:599`) copy the current policy
into their next start, so recording the policy at admission is what stops a reconnect from starting
a stream that re-enables audio: the next `connect` carries `--audio off` and performs neither setup
volume write nor capture, even if the old stream's close was never acknowledged. A level retry
acknowledged after the latch closed cannot reopen it, because the latch is one-way.

If the close is refused, the authority keeps the audio state, reports one diagnostic, and the close
can be retried. If the close times out, the requested policy still stands: repeated `close_audio`
attempts and `Stop` are admitted, ordinary volume Retry is not, and `Disabled` is not published until
the latch is confirmed. There is no `audio on`, so no obsolete or reordered command can reopen a
stream's audio.

**Receipt outcomes.** `AudioStartReceipt` decides the actor state, so every outcome is fixed here:

| Receipt | `AudioState` | `intent_stage` | mute | Events |
| --- | --- | --- | --- | --- |
| `applied`, level > 0 | `Enabled` | `active` | false | `audio_recovered` |
| `applied`, level = 0 | `Muted` | `active` | true | `audio_recovered` |
| `refused` | `Unavailable` | `audio_decision` | false | `audio_start_failed` |
| `pending`, or missing on a capable backend | `Unavailable` | `audio_decision` | false | `audio_start_failed` |
| `unrequested` or legacy | today's behaviour | today's behaviour | today's behaviour | today's behaviour |

`pending` is deliberately **not** a resting state: it collapses into the retryable
`Unavailable / audio_decision` outcome, and `RetryAudio` re-samples the receipt instead of leaving the
actor in `Starting`. That keeps every row reachable by an existing action and removes the dead end
where an active session offers no Retry and a decision stage offers one the handler rejects.

The transport must not emit `audio_recovered` before the receipt is known: today it emits it and
then returns success (`transport/doubletake.py:1118`), which would confirm audio for a stream whose
level was withheld. Confirmation is emitted with the receipt, in the same actor step.

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
| Backend patch | `vendor/doubletake/0004-target-volume.patch`; backend files `internal/airplay/{mirror,client}.go`, `internal/airplay/mirror_setup_test.go` (`TestAudioVolumeBody` calls the bool signature and will not compile), `internal/daemon/*`, `cmd/doubletake-ctl/main.go`, `cmd/doubletake/main.go` |
| Proof | `backend/contract.py` (surface tables, feature flag, status decode) |
| Capability producer | `policy/capabilities.py`, `domain/resources.py` (`CapabilityReport.volume`) |
| Typed control | `backend/control.py`, `process/ownership.py` |
| Transport | `transport/doubletake.py`, `transport/targets.py` |
| Domain | `domain/messages.py`, `domain/ports.py`, `domain/resources.py`, `domain/records.py` |
| Session | `session/audio.py`, `session/engine.py`, `session/fencing.py`, `session/persistence.py`, `session/recovery.py`, `session/virtual.py`, `session/mirror.py`, `session/reconnect.py` |
| Composition | `runtime.py` (preference writer, capability report wiring) |
| Storage | `storage/codecs.py`, `storage/repositories.py` |
| UI | `ui/state.py`, `ui/presenter.py`, `ui/window.py` |
| Testing aids | `testing/adapters.py` (`FakeTransport.start_stream`, `start_audio`, `set_volume`, `close_audio`) |
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
| Startup level rejected by the receiver (complete response) | Daemon withholds audio, video keeps running; `volume_unavailable`; Retry re-issues `volume` on that stream |
| Startup level write times out or the response is incomplete | The library's unusable-connection rule applies: setup fails, the stream is cleaned up, ordinary transport failure; no same-stream promise |
| Legacy reply omits `volume`/`volume_state` | Accepted as "unknown", never manufactured into an applied level; a **capable** backend's missing receipt is refused in the volume path |
| Live `volume` refused, failed or timed out | `effective`/`last_audible` unchanged; one diagnostic; audio keeps playing |
| Stale volume result | Discarded silently |
| Receiver replaced mid-session | New stream has no `effective`/`last_audible`; a replaced identity inherits no remembered level |
| Stored volume malformed | Only the volume preference is dropped and reported |
| Level requested with audio unavailable | Refused, except the explicit Retry row |
| Preference write fails | Reported; `effective` stands; `remembered` unchanged; UI says it could not be saved |
| Video-only close refused | Audio state kept, `Disabled` not published, one diagnostic; Retry of the close remains available |
| Video-only close timed out | Application latch stays closed; repeated close and `Stop` admitted, ordinary volume Retry refused, `Disabled` not published |
| Late level acknowledgement after the gate closed | Ignored: the daemon leaves audio closed for that stream |
| Start video only while a Retry is in flight | The acknowledged gate closes first; the late acknowledgement cannot open audio |
| Sender patched, ctl not (or the reverse) | Incoherent surface → whole backend refused |
| Legacy backend with a saved level | `--volume` is never sent (capability-conditioned), so downgrade cannot break streaming |

## 8. Test strategy

All offline; no live receiver, no network.

- **Contract:** both ctl tables recognized; volume row without token refused; token without row
  refused; a volume row whose scope statement is missing, negated or borrowed refused; the same three
  cases for the `audio off [target]` row; a missing or malformed audio row refused; a source-help
  surface without tokens refused (unchanged); status decode for `volume` absent/0/100/out-of-range/
  non-integer and every `volume_state`, including an unknown state; a legacy status or mute reply that
  omits both new fields still decodes exactly as today; a capable backend whose receipt is missing is
  refused in the volume path, not by the shared decoder; profile string unchanged for every surface.
- **Carried-code proof (Go, offline, GOPROXY=off).** The patch factors its decisions into
  declarations a test can compile against a fake RTSP writer, in the carried-code style of
  `tests/backend/test_carried_source_patch.py`: the mapping, the setup decision, the `volume`
  dispatch with target selection across two streams, the refusal/retry bookkeeping, and status
  output. Assertions: 0/1/50/99/100 map exactly and monotonically; the setup write uses the
  requested percent, not 0 dB; unset keeps 0 dB; explicit zero silences; a refused level withholds
  audio and reports `refused`; an acknowledged `volume` afterwards reports `applied` and opens audio;
  a rejected write leaves the previous acknowledged percent; an incomplete or withheld response
  leaves the control connection unusable and fails setup rather than continuing at maximum; two
  targets do not cross; `status` distinguishes absent, 0 and `refused`; the connect parser accepts
  `--audio on|off` and `--volume` in the documented positions, rejects out-of-range values and
  `--audio` with an invalid argument, and never treats a flag as a PIN; a legacy `connect` with
  neither flag produces the argv of today.
- **Transport:** `start_stream` passes the initial level into the connect argv only when supplied;
  an absent target is refused before dispatch; acknowledged level returned; `AudioStartReceipt`
  carries `volume_state` and the level from the same status read; fence respected; a timeout leaves
  `effective` unknown.
- **Session (through `SessionManager`):** every row of §5.4.7, including a legacy backend with a
  saved level, a rejected startup followed by Retry, a timed-out write ending in ordinary cleanup,
  the coalescing queue under a slider drag with settlement counted (not public timeouts), Stop during
  a pending command, and replacement mid-flight; snapshot clearing on reload; FakeTransport parity,
  where a scripted `Ok(None)` start is normalized to a legacy receipt with no manufactured level;
  mute representation stays consistent through zero → audio loss → Retry/recovery → positive level
  (`AudioRuntime.muted_lease`, `stream.muted`, `snapshot.muted`, `AudioState.Muted`), and **no**
  backend `mute`/`unmute` command is sent on a volume-capable stream.
- **Video-only barrier:** rejected start → Retry with the response held → Start video only → late
  RTSP 200 must leave audio capture closed; a refused close must not publish `Disabled`; a close whose
  acknowledgement arrives after its public timeout must keep the application latch closed, admit a
  second close and Stop, and reject ordinary Retry; video-only carried through replacement and
  reconnect must never start capture.
- **Gate quiescence and worker retirement (carried Go):** both acknowledgement orders (close before
  a pending level acknowledgement, and after), a capture start already in progress, Stop and
  replacement with late completions, one worker and one latest-value slot per stream, and one
  absolute deadline covering connection admission, write, response and Digest retry.
- **Composed:** one `SessionManager` → real transport → scripted control/daemon-fake test that drives
  a start with a remembered level and asserts the receipt, because handler-only tests cannot show
  that the receipt reaches the authority.
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
