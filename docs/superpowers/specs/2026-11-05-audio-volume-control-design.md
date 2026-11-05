# Audio volume control — design

- **Status:** design revision 3 after independent review rounds 1–2; awaiting review round 3.
- **Date:** 2026-11-05 (revision 3)
- **Delivery target:** `/data/home/guest/Development/airplay-linux`, branch `master`, no remote.
- **Mockup record:** `docs/superpowers/specs/assets/2026-11-05-audio-volume-control-mockup.png`

## Revision history

**Revision 2** fixed round 1: audio is carried by `connect`, so ordering before audio start is
impossible from outside the daemon; a new profile string would fall out of twelve literal
profile comparisons; and the receiver reports no live volume, so "follow the TV's remote" was
withdrawn.

**Revision 3** fixes round 2, and changes the mechanism once more because three of the six
blockers shared one root cause: revision 2 staged the level in the daemon *before* `connect`,
which creates pending state that must be owned, bounded, expired, cancelled and matched to a
start — and still leaves the setup path writing 0 dB when staging fails. The level now travels
**with the connect intent**, so:

| Round-2 item | Resolution in revision 3 |
| --- | --- |
| B1 startup staging had no interface or typed receipt | §5.4: the level is an input to stream acquisition; the connect acknowledgement carries the level the receiver acknowledged, or its absence |
| B2 pending levels had no ownership, bounds or retirement | §5.1: there is no pending state at all |
| B3 patch scope excluded the code that emits volume | §5.1 names `internal/airplay/mirror.go`, `client.go`, the daemon and the ctl, with the current line evidence |
| B4 startup failure still allowed the full-volume burst | §5.1.4 + §5.4.3: an explicitly requested level that the receiver refuses silences the stream and is reported; it never falls back to maximum |
| B5 remembered vs last audible conflicted on unmute | §5.4.6: one destination per unmute intent, no post-unmute resend, and a full transition table |
| B6 the new surface formula weakened existing proof | §5.2: a delta on the existing recogniser, keeping its scope proof and its source-token rejection |
| MAJOR capability producer omitted | §6 inventory adds `policy/capabilities.py`, `session/mirror.py`, `session/reconnect.py`, `transport/targets.py` |
| MAJOR staged reply missing | §5.3: `AckReply.volume` |
| MAJOR producer proof not achievable offline | §8: the executable seam, in the same carried-code style the repository already uses |
| MINOR half-install had two definitions | §5.2/§7: whole-backend refusal for an incoherent surface; mute-only only for a verified original backend |
| MINOR stale completions required and forbade a diagnostic | §5.4.2/§5.8: silent discard, no diagnostic |
| MINOR tooltip promised restoration on a backend that cannot do it | §5.7: copy is derived from proven capability |

## 1. Background

The application can mute a receiver and nothing else. That is not a UI omission; it is the whole
surface the backend offers. `doubletake-ctl` publishes **eight** commands (`status`, `discover`,
`devices`, `connect`, `pin`, `disconnect`, `mute`, `unmute`) and the application proves that
surface before it enables any audio action (`backend/contract.py:59`).

The consequence is what the operator reported, and it is visible in the daemon source:

- during stream setup the daemon writes `SET_PARAMETER volume` with `audioVolumeBody(false)`,
  which is 0 dB — the receiver's **maximum** (`internal/airplay/mirror.go:1128-1135`, and again at
  `:1138`);
- a failed setup write is logged as non-fatal and streaming continues at maximum
  (`mirror.go:1132-1133`);
- `SetAudioMuted(muted bool)` and `audioVolumeBody(muted bool)` can express only 0 dB and −144 dB
  (`mirror.go:2414-2437`);
- the receiver *does* report an `initialVolume` in its `/info` plist (`client.go:46`), but the
  daemon never uses it as a level to apply.

So the chain has two levels, maximum and silence, and no way to state one in between. The mute
button's own tooltip admits the gap: "the receiver keeps its own volume setting".

Two facts bound the work: only the daemon owns the receiver's RTSP session, so only a daemon
change can set a level; and this repository already carries that change mechanism — three patches
in `vendor/doubletake/` applied to a pinned upstream checkout, installed to `/home/bin`. A fourth
patch is the supported way to extend the backend.

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

Stacks on 0001+0002+0003. Scope, by file, with the current code each change replaces:

| File | Change |
| --- | --- |
| `internal/airplay/mirror.go` | `audioVolumeBody(percent int)`: 0 → `-144` dB, 1–100 → `20*log10(percent/100)` dB, 100 → exactly `0` dB. New `SetAudioLevel(percent int) error` beside `SetAudioMuted`. `SetAudioMuted(false)` re-applies the session's last audible percent instead of a fixed 0 dB. Setup writes use the configured initial percent instead of `audioVolumeBody(false)` at `:1128` and `:1138`. |
| `internal/airplay/client.go` | `StreamConfig` gains `InitialVolumePercent int`; 0 or unset means "not requested", which reproduces today's behaviour exactly. |
| `internal/daemon/…` | Parse the level from the connect request, pass it into `StreamConfig`, keep per stream the last **acknowledged** percent and whether the initial level was applied, and serve the new `volume` command on a live stream. |
| `cmd/doubletake-ctl` | `connect [target] [PIN-or-password] [--volume <0-100>]` and `volume <0-100> [target]` in the usage table, both carrying the same target-scope statement the other target-scoped rows carry. `-capabilities` gains the token `target-volume`. |

Four behavioural rules the patch must satisfy:

1. **One mapping, one place.** `audioVolumeBody` is the only percent-to-dB conversion, so the
   endpoints are exact and a caller cannot produce a value outside 0–100.
2. **Level travels with the intent.** The initial percent reaches setup through
   `StreamConfig`; there is no pre-connect staging, so no pending state exists to own, expire,
   cancel or match to a start.
3. **A requested level that the receiver refuses is never replaced by maximum.** If an explicit
   initial percent was requested and its `SET_PARAMETER` fails, the daemon silences the stream
   (percent 0) and records that the initial level was not applied. If no level was requested, the
   existing non-fatal behaviour is unchanged.
4. **Acknowledge, don't echo.** A stream's stored percent is the level the receiver acknowledged.
   `status` reports it per stream as optional `volume` (integer 0–100), absent when no level has
   ever been acknowledged — which is not the same as 0. The connect acknowledgement reports the
   level it applied, or its absence.

The sender's help output does not change, so the pinned sender-flag set is untouched.

### 5.2 Application backend proof

The existing recogniser compares the ctl table by exact argument signature
(`backend/contract.py:822`), proves target scope separately (`:786`), and rejects a source-help
surface whose capability tokens are missing (`:844`). Revision 3 **extends** it; it does not
replace those rules.

```text
volume_row      := the usage row "volume <0-100> [target]"
volume_row_ok   := volume_row.arguments == "<0-100> [target]"
                   and _target_scope_proven(volume_row)        # same predicate as mute/unmute
connect_row_ok  := connect row == "connect [target] [PIN-or-password] [--volume <0-100>]"

ctl_pinned      := signatures == the pinned 8-row table
ctl_volume      := signatures == the pinned table with the extended connect row
                   and the extra volume row, with volume_row_ok and connect_row_ok

target_volume   := ctl_volume
                   and {"capabilities-v1", "status-source", "target-volume"} ⊆ tokens

# Unchanged from today, and still required: a source-reporting sender without its tokens is
# refused outright rather than downgraded.
inconsistent    := (ctl_volume and not target_volume)
                   or (ctl_pinned and "target-volume" in tokens)

verified        := sender flags in {pinned, source}
                   and (ctl_pinned or ctl_volume)
                   and not inconsistent
                   and source tokens consistent with the sender's source surface
```

`target_volume` is therefore true only for a fully coherent extended surface: the exact volume
signature **and** its affirmative scope statement **and** the token. A row that says the target is
ignored, a missing token, a borrowed token, or a token without the row all leave `target_volume`
false; the first two and the last also make the whole surface incoherent, which refuses the
backend exactly as today's unknown surface does.

**No new profile string.** The profile stays `doubletake-0.5+source` (or `doubletake-0.5`), so the
twelve literal profile comparisons in `transport/doubletake.py:525`, `process/launcher.py:80`,
`process/daemon_registry.py:251,343,372,946,1081`, `process/supervisor.py:572`,
`runtime.py:424,454` and `backend/control.py:584,589` keep working unchanged. `BackendFeatures`
grows `target_volume: bool`, false everywhere except the coherent extended surface.
`physical_source`, `foreground_control`, `owner_handshake`, `keyring`, `virtual_source` and the
pairing group are untouched.

### 5.3 Domain, port, transport and typed control

- `VolumeLevel`: validated integer 0–100, `0` meaning silence.
- `SetVolume(receiver_id, level)`, added to the closed command union (`domain/messages.py:130`).
- `StartRequest.volume: VolumeLevel | None` (`domain/resources.py`), so the remembered level is an
  input to starting a stream rather than something fetched mid-flight. The presenter fills it from
  the receiver preference it already holds, after `PreferenceAccess` validation; a reconnect reuse
  carries the snapshot's value, so a retry cannot invent a different level.
- `TransportPort.start_stream(..., initial_volume: VolumeLevel | None)` and
  `TransportPort.set_volume(handle, level, context) -> Result[int]`.
- `AckReply.volume: int | None` (`backend/contract.py:320`) — the typed startup receipt: absent
  means the level was not applied, which is distinct from 0.
- `BackendStream.volume: int | None`; strict decode, an integer 0–100 or absent.
- `TransportEventKind.volume_result` with `VolumePayload(level, ok)`; the effect kind `volume`
  joins `_AUDIO_EFFECTS` (`session/audio.py:67`).
- `CapabilityReport.volume: bool` (`domain/resources.py:222`), produced by
  `policy/capabilities.py:536` from the proven feature — the same producer that already builds the
  report the UI receives.
- Typed control: `BackendOperation.volume` (`backend/control.py:47`), `VolumePayload` in the
  payload union (`:137`), membership in the dispatch-fence set (`:160`), a target-scoped argv
  branch (`:372`) that **refuses a missing target** like `mute`/`unmute`, the connect argv branch
  extended with the optional level, acknowledgement decoding for the new field (`:912`), and
  authorisation in `process/ownership.py:318` conditioned on proven `target_volume`.

### 5.4 Session authority rules

Three distinct values, never conflated:

| Value | Meaning | Lifetime |
| --- | --- | --- |
| `remembered` | The per-receiver preference, 1–100, or absent | Durable |
| `effective` | The level this stream last had acknowledged, 0–100, or unknown | Per transport lease |
| `last_audible` | The last acknowledged non-zero level on this stream, or absent | Per transport lease |

1. **Acquisition carries the level.** `start_stream` receives `remembered` and passes it to
   `connect`. The connect acknowledgement supplies `effective`; its absence means the level was
   not applied.
2. **Confirmation only.** A failed, refused or timed-out result changes neither `effective` nor
   `last_audible`. A **stale** result is discarded silently, by operation id, exactly as
   `session/fencing.py:78` and `session/engine.py:532` already discard inadmissible events —
   without a diagnostic, because the operation it belonged to is gone.
3. **A refused initial level never becomes maximum.** If the acknowledgement reports no level, the
   daemon has already silenced the stream; the authority records `effective = 0` with a
   `volume_unavailable` diagnostic and offers the existing audio-decision recovery. It never
   treats the receiver's 0 dB default as the applied level.
4. **Fencing.** A live volume command carries its own operation id, so a slow `30%` after a `60%`
   cannot apply. The stream fence is the transport lease the audio authority already uses.
5. **Lease scoping.** A replacement stream starts with no `effective` and no `last_audible`; it
   acquires them only through its own acknowledgement.
6. **One destination per unmute.** The transition table below is the whole contract. There is no
   post-unmute resend, so persistence failure cannot make unmute and "remembered" disagree.

| Intent | Condition | One command | `effective` | `last_audible` | `remembered` |
| --- | --- | --- | --- | --- | --- |
| Start | `remembered` present | `connect --volume r` | ack level or 0 | ack level if > 0 | unchanged |
| Start | `remembered` absent | `connect` | unknown | unknown | unchanged |
| Slider to n ≥ 1 | audio audible or muted | `volume n` | n on ack | n on ack | n on ack, then persist |
| Slider to 0 | any audited state | `volume 0` | 0 on ack | unchanged | unchanged |
| Speaker mute | audio enabled | `mute` | 0 on ack | unchanged | unchanged |
| Speaker unmute | muted, `last_audible` known | `unmute` | `last_audible` on ack | unchanged | unchanged |
| Speaker unmute | muted, `last_audible` unknown | `unmute` | ack level (daemon default) | ack level if > 0 | unchanged |

`remembered` changes only on a **user-initiated** level, and only after acknowledgement; a failed
preference write is reported and leaves the previous `remembered` in place, while `effective` and
`last_audible` keep the applied value. The UI then shows the applied level and says the change
could not be saved.

7. **No audio, no level.** With audio `Disabled` (video-only), `Unavailable` or undecided, the
   authority refuses a volume command rather than sending it, and `remembered` is preserved for
   the next session.
8. **Never tears down video.** A live volume failure never stops the stream and never retries the
   transport. Only a *requested at start* level failure takes the documented audio-decision route,
   which already exists and already keeps video alive.
9. **Snapshot and restart.** `effective`, `last_audible` and `remembered` live in
   `SessionSnapshot`; the first two are stripped by the transient-authority rule in
   `session/persistence.py:20` beside `muted`, and `remembered` is reloaded from the receiver
   record.

### 5.5 Routing and event acceptance

Three closed sets must accept the new message and result: `SessionManager._route`
(`session/engine.py:776`) routes `SetVolume` to `Route.audio`; `session/fencing.py:89` classifies
the volume result as an audio result so its own operation id is honoured; and
`session/audio.py`'s claim sets include the new effect and event kinds. Proof of delivery and stale
rejection goes through `SessionManager`, not only through direct handler calls.

### 5.6 Storage

`SavedReceiver` gains `remembered_volume: int | None` beside `remembered_resolution` and inherits
`PreferenceAccess` unchanged: an ambiguous, replaced, non-persistable or non-continuity receiver
never inherits a level. Values are stored 1–100; **0 is not storable**.

- **Record version.** The receiver record moves to version 2; version-1 records still load with no
  remembered volume. The global `VERSION` constant (`storage/codecs.py:89`) is not bumped for the
  other schemas, so the codec gains a per-schema accepted-version set.
- **Volume is a preference, not identity.** A malformed or out-of-range volume drops **only the
  volume**, with one diagnostic; the rest of the record loads and the receiver keeps its
  remembered resolution and trust. Identity and structural corruption still refuse the whole
  record. This is deliberate: `storage/repositories.py:197` fails the entire receiver listing on
  one bad record and `session/recovery.py:227` propagates that while startup admission is blocked,
  so refusing the record would let one bad integer withhold every other preference.
- **One writer.** One shared receiver-preference writer reuses the existing read-modify-write shape
  (`session/virtual.py:816` — `receivers.get` → mutate → `put`), so a remembered volume and a
  remembered resolution cannot overwrite each other. It is instantiated once at the composition
  root and shared by the resolution path and the volume path. The presenter refreshes its cache
  after a successful write and reports a failure; it never presents a level that failed to store
  as durable.

### 5.7 UI

`Volume` row, immediately under `Start`/`Stop`/`Pair`, as accepted in the mockup:

- label `Volume`, slider **0–100** in 1% steps, right-aligned readout (`30%`, `Mute` at 0),
  accessible name `Receiver volume`;
- enabled only when all hold: proven `target_volume` (via `CapabilityReport.volume`), a selected
  receiver, a live audio-capable phase, a current stream, no cleanup pending, and the trust
  constraints the mute button already applies (`ui/state.py:867`). Disabled precedence: no backend
  support, no receiver, not streaming, audio unavailable or video-only, cleanup in progress — each
  with one fixed reason;
- readout: `effective` when known, `Mute` when muted, `—` when unknown. It never invents a number;
- keyboard: tab-reachable, arrows 1%, page keys 10%, Home/End to the ends;
- the speaker button keeps its icons and interaction. Its copy is now **derived from proven
  capability**: "muting is for this session; unmuting returns to the last level" on a volume-capable
  backend, and today's accurate wording on a backend that cannot restore a level;
- the presenter suppresses render-time signals while it sets the slider programmatically;
- activity log: registered codes with bounded integer fields — level applied, level change failed,
  volume unavailable with a fixed reason. Capability absence is reported once per session, never
  per render.

### 5.8 Diagnostics and activity

`diagnostics/activity.py:75` and the closed registry at `:211` accept the new codes and the integer
field; `diagnostics/catalog.py:50` carries `audio_volume_failed` and `volume_unavailable` with fixed
messages and recovery actions. Emission is once per operation on failure and on transition for
availability; stale completions emit nothing. `AudioLifecycle._emit()` swallows sink errors
(`session/audio.py:266`), so proofs must assert emitted events, not assume them.

## 6. Change inventory

| Area | Files |
| --- | --- |
| Backend patch | `vendor/doubletake/0004-target-volume.patch`, `docs/backend-fork.md` |
| Proof | `backend/contract.py` (surface tables, feature flag, status decode, ack field) |
| Capability producer | `policy/capabilities.py`, `domain/resources.py` (`CapabilityReport.volume`) |
| Typed control | `backend/control.py`, `process/ownership.py` |
| Transport | `transport/doubletake.py`, `transport/targets.py` |
| Domain | `domain/messages.py`, `domain/ports.py`, `domain/resources.py`, `domain/records.py` |
| Session | `session/audio.py`, `session/engine.py`, `session/fencing.py`, `session/persistence.py`, `session/virtual.py`, `session/mirror.py`, `session/reconnect.py` |
| Storage | `storage/codecs.py`, `storage/repositories.py` |
| UI | `ui/state.py`, `ui/presenter.py`, `ui/window.py` |
| Diagnostics | `diagnostics/activity.py`, `diagnostics/catalog.py` |
| Docs | `docs/operations.md`, `docs/backend-fork.md`, `docs/backend-compatibility.md` |
| Tests | §8, including the pinned inventories in `tests/domain/test_contracts.py:68` and `tests/environment/test_installed_contracts.py:57` |

## 7. Error handling

| Situation | Behaviour |
| --- | --- |
| Backend proves `target_volume` | Row enabled; level applied through `connect`, and on change |
| Verified backend without volume (today's build) | Row disabled with one reason; mute unchanged and documented as legacy; one diagnostic per session |
| Incoherent surface (row without token, token without row, ignored target) | Whole backend refused, exactly as today's unknown surface — never downgraded to mute-only |
| Initial level refused by the receiver | Stream silenced by the daemon; ack reports no level; `volume_unavailable`; audio-decision recovery offered; video preserved |
| Live `volume` refused, failed or timed out | `effective`/`last_audible` unchanged; one diagnostic; audio keeps playing |
| Stale volume result | Discarded silently by operation id; no event, no diagnostic |
| Receiver replaced mid-session | New stream has no `effective`/`last_audible`; a replaced identity inherits no remembered level |
| Stored volume malformed or out of range | Only the volume preference is dropped and reported |
| Level requested with audio unavailable | Refused by the authority, never sent |
| Preference write fails | Reported; `effective` stands; `remembered` stays at its previous value; UI says it could not be saved |
| Sender patched, ctl not (or the reverse) | Incoherent surface → whole backend refused |

## 8. Test strategy

All offline; no live receiver, no network.

- **Contract:** both ctl tables recognized; volume row without token refused; token without row
  refused; a volume row whose scope statement is missing, negated or borrowed refused; a
  source-help surface without tokens refused (unchanged); status decode with `volume` absent, 0,
  100, out of range, non-integer; `AckReply.volume` present/absent; the profile string unchanged for
  every surface, so the twelve profile branches stay covered.
- **Carried-code proof (Go, offline, GOPROXY=off).** The patch factors the decisions into
  declarations a test can compile with a fake RTSP writer, in the same carried-code style as
  `tests/backend/test_carried_source_patch.py`: `audioVolumeBody(percent)` and the setup
  decision helper. The proof asserts 0/1/50/99/100 map exactly and monotonically; the setup write
  uses the configured percent, not 0 dB; a refused requested level produces the silencing write and
  `applied=false`; `SetAudioMuted(false)` re-applies the last audible percent; the ctl parser
  accepts `--volume` only in the documented position and rejects out-of-range values. This is the
  executable seam that makes the assertions possible without a receiver.
- **Transport:** `start_stream` passes the initial level into the connect argv; an absent target is
  refused before dispatch; acknowledged level returned; `AckReply.volume` absent handled as
  "not applied"; fence respected.
- **Session (through `SessionManager`):** the whole transition table in §5.4, including
  persistence failure; fencing (slow 30 after 60); replacement stream inherits nothing; refused
  initial level takes the audio-decision route with video preserved; no command while audio is
  disabled or unavailable; snapshot clearing on reload.
- **Storage:** v1 record loads with no level; v2 round-trip; 0 and out-of-range dropped while the
  rest of the record loads; identity corruption still refuses the record; concurrent preference
  writes both survive; `PreferenceAccess` refusal paths withhold the level.
- **UI:** enablement precedence per reason; `—` for unknown; arrow/page/Home/End; accessible name;
  capability-derived tooltip copy on both backend kinds; no render feedback loop.
- **Regression:** existing audio, mute, mirroring, capability, environment and storage suites stay
  green, including every test pinning today's no-volume surface and the legacy fixtures.

## 9. Delivery and installation

The application change is delivered in this repository. Making it live requires the fourth patch to
be applied and both binaries rebuilt and installed: a separate, receipted step at delivery,
recorded in `docs/backend-fork.md` with the new patch digest, the base commit, the Go toolchain, the
build commands and the verify command (`./doubletake -capabilities` prints the four tokens; ctl
usage shows `volume` and the extended `connect`). The previous pair is kept as `.prev` files so the
change reverts by restoring two files. `docs/operations.md` gains the operator-facing behaviour:
remembered levels, the disabled row, and what a failed application reports.

## 10. Risks

1. **A shared binary changes.** Until the rebuild is installed the feature is inert. A half-installed
   pair is an incoherent surface and fails closed to a refused backend, never to a wrong assumption.
2. **Receivers differ in how they treat `SET_PARAMETER volume`.** Confirmation means "acknowledged",
   not "the speaker output matches". A receiver that acknowledges and ignores the parameter cannot be
   detected, and the documentation says so rather than papering over it.
3. **One Go library change is not free.** `SetAudioMuted` semantics change for unmute, so the carried
   patch must keep the `mute`/`unmute` ctl contract byte-compatible in its acknowledgements.
4. **Perceived loudness curve.** 50% is −6 dB (half amplitude), not half perceived loudness. Both
   endpoints are exact; the curve is one function in one place.
5. **Record version bump.** A v1 record must never be lost: the v2 decoder reads both, and only the
   volume preference is droppable.

## 11. What must not regress

- Mute stays session-only and receiver-local; Stop disconnects only that receiver.
- Audio-decision, video-only and retry-audio paths keep their states and messages.
- Capability gating keeps failing closed; no unproven capability is inferred.
- No secret reaches argv, metadata, activity log or the pane.
- The no-remote delivery path stays a local merge only.
- Startup recovery and the receiver listing survive one bad preference.
- A plain `connect`, `mute` and `unmute` from an unpatched caller behave exactly as today.
