# Audio volume control — implementation specification

Authority: `docs/superpowers/specs/2026-11-05-audio-volume-control-design.md`, revision 8.

## 1. Scope and precedence

This specifies the fourth carried backend patch and its Python producer/consumer contracts.
The approved design wins on every conflict. Installation is a separate delivery action.
All implementation verification described here is offline; none requires an AirPlay receiver.

Paths starting with `src/` and `tests/` are application-repository paths. Backend paths
starting with `internal/` or `cmd/` are paths inside patch
`vendor/doubletake/0004-target-volume.patch`, based on
`c896e3689ab85c3ed520e502308dedfbb5a4c221` after patches 0001–0003. The inspected reference is
`/tmp/delivery-reconcile.GMO4Nx/reapply`. A file column or a `File:` caption supplies the
path for every symbol in that row or code block. Names local to pseudocode are local variables,
not additional public interfaces. New declarations go in the existing files named here.

Compatibility rule: without proven `target_volume`, preserve today's connect argv, audio
receipts' meaning, mute/unmute, decision/retry behavior, profiles, source checks, trust gates,
ownership proofs, cleanup and timers. A saved level cannot enable this feature. The UI adds a
disabled volume row and one capability-absence activity diagnostic per session. No new profile,
sender flag, remote readback, audio-on command or independent status polling owner is introduced.

## 2. Backend patch contract

### 2.1 Mapping, setup receipt and bounded RTSP

File: `internal/airplay/mirror.go`. Replace the boolean mapping with exactly:

```go
func audioVolumeBody(percent int) []byte {
    if percent < 0 { percent = 0 }
    if percent > 100 { percent = 100 }
    db := -144.0
    if percent == 100 {
        db = 0.0
    } else if percent > 0 {
        db = 20 * math.Log10(float64(percent) / 100)
    }
    return []byte(fmt.Sprintf("volume: %.6f\r\n", db))
}
```

| Input | Complete body, Go byte-string notation | Length |
| --- | --- | ---: |
| 0 or negative | `"volume: -144.000000\r\n"` | 21 |
| 1 | `"volume: -40.000000\r\n"` | 20 |
| 50 | `"volume: -6.020600\r\n"` | 19 |
| 99 | `"volume: -0.087296\r\n"` | 19 |
| 100 or greater | `"volume: 0.000000\r\n"` | 18 |

Only this function converts percent to dB. Domain, ctl and daemon inputs reject invalid levels;
clamping is the library's defensive boundary, not permission to accept malformed wire input.

| File | Declaration/change | Contract |
| --- | --- | --- |
| `internal/airplay/client.go` | `StreamConfig.InitialVolume *int` | Nil means no requested level. Copy pointed-to input before asynchronous work. Explicit zero requests silence. |
| `internal/airplay/client.go` | `StreamConfig.AudioEnabled *bool` | Nil preserves the caller's existing `NoAudio`; false disables this stream; true cannot override daemon-wide `NoAudio`. |
| `internal/airplay/mirror.go` | `MirrorSession.SetAudioLevel(percent int) error` | Same session/client/URI and disabled-audio checks as `SetAudioMuted`; bounded `SET_PARAMETER` to this session's audio URI, mapping above. Nil error means final RTSP **200**, after at most one Digest retry. |
| `internal/airplay/mirror.go` | `MirrorSession.SetAudioMuted(muted bool) error` | Keep existing signature, failure text and legacy RTSP path; use `audioVolumeBody(0)` for true and `audioVolumeBody(100)` for false. Do not route legacy calls through the new shorter timer. |
| `internal/airplay/mirror.go` | `MirrorSession.InitialAudioLevel() (*int, string)` | Immutable setup result copied out: last acknowledged percent, or nil, and `unrequested`, `applied` or `refused`. Never infer 100 from a legacy setup write. |
| `internal/airplay/mirror.go` | `AirPlayClient.setupMirrorSession` | Compute effective no-audio before compatibility/setup decisions. Replace **both** setup volume bodies. Transfer requested-level outcome into the returned session. |
| `internal/airplay/client.go` | `AirPlayClient.rtspRequestUntil(method, uri, contentType string, body []byte, extraHeaders map[string]string, deadline time.Time) ([]byte, map[string]string, error)` | New bounded sibling of `rtspRequest`; preserve existing helper for all legacy callers. |
| `internal/airplay/client.go` | `AirPlayClient.rtspRequestOnceUntil(method, uri, contentType string, body []byte, extraHeaders map[string]string, deadline time.Time) ([]byte, map[string]string, error)` | Already owns connection mutex. Same framing/encryption as `rtspRequestOnce`, with absolute I/O deadlines and strict 200 success. |

In `internal/daemon/daemon.go::Daemon.connectAndStream`, build a local config with
`cfg := d.mirrorStreamConfig()`, copy entry.initialAudio to cfg.AudioEnabled and entry.initialVolume
to cfg.InitialVolume, then set
`cfg.NoAudio = cfg.NoAudio || (cfg.AudioEnabled != nil && !*cfg.AudioEnabled)`.
`internal/airplay/mirror.go::AirPlayClient.setupMirrorSession` applies the same expression before
its first use of cfg.NoAudio so direct library callers also respect the intent. True cannot
override global no-audio. Never mutate `internal/daemon/daemon.go::Daemon.cfg`.

File: `internal/airplay/client.go`. The bounded helper's required algorithm is:

```text
deadline is supplied once; never replace it with now + a fresh timeout
acquire AirPlayClient.mu using TryLock and deadline-aware waits of at most 1 ms
    no goroutine may remain queued on Lock after the request expires
if expired before acquisition: return context.DeadlineExceeded; send nothing
under mu: recheck deadline and conn; add existing preemptive Digest header
for first attempt and, only if existing digestRetryHeader permits it, one retry:
    before encryption/write: check remaining time
    set both read and write deadlines to the SAME supplied deadline
    write the whole existing RTSP frame; partial write is incomplete
    call existing plaintext/encrypted readers directly; do not reset their deadlines
    preserve existing framing/body bounds and markIncompleteResponse semantics
    accept only status 200; a complete other 2xx is a complete rejection here
    Digest retry consumes the SAME remaining budget
on incomplete response/write: close the control connection while owning mu
    never call a helper that recursively locks mu
on complete response: leave the connection usable
clear deadlines before releasing mu; clearing failure makes the connection unusable
return; release mu on every exit
```

The existing readers in `internal/airplay/client.go` accept general 2xx for other commands.
Give `AirPlayClient.readPlaintextHTTPResponse` and `AirPlayClient.readEncryptedHTTPResponse` an
optional variadic `statusOut ...*int` argument, assigning the parsed status to its sole output
pointer when supplied. Existing calls omit it and retain their behavior. The bounded helper calls
the appropriate reader directly with a status pointer, preserves `markIncompleteResponse`, and
converts a complete non-200 2xx into `HTTPStatusError` after consuming its body. Thus 201/204 cannot
count as 200. Do not parse a body or error string to guess success. On a poisoned connection call
its Close while owning the mutex; retain the closed connection object so legacy callers fail on
I/O rather than dereference nil.

Timeout before connection admission does not poison a connection on which nothing was sent.
Any request that sent bytes and failed to consume its complete response does. During setup this
sets the existing `controlConnectionUsable` flag false and uses existing failed-setup cleanup.
A live volume failure does not stop video or request reconnection; an unusable control connection
is never reused, and any independently observed transport loss follows existing lifecycle rules.

Constants: requested setup's **pair** of writes shares 2 seconds total; each live write has
2 seconds from worker dispatch, including connection admission. A superseded value in the daemon's
single pending slot has no RTSP worker yet. At its dispatch, give it one new 2-second deadline.
The library `internal/airplay/mirror.go::MirrorSession.SetAudioLevel` creates that deadline once.
The setup caller passes one deadline through both calls, rather than calling this public wrapper
twice. Neither bound changes the application's existing 5-second control/source budgets or
20-second source-prepared handshake budget.

### 2.2 Per-stream authority

File: `internal/daemon/daemon.go`. Extend `Request`, `StreamInfo` and `activeStream`:

```go
// Request additions; pointers preserve presence, including false/zero.
Audio  *bool `json:"audio,omitempty"`
Volume *int  `json:"volume,omitempty"`

// StreamInfo additions; all patched entries emit the last two fields.
Volume      *int   `json:"volume,omitempty"`
VolumeState string `json:"volume_state"`
AudioClosed bool   `json:"audio_closed"`

// activeStream additions, guarded by Daemon.mu unless stated otherwise.
initialVolume *int                 // immutable admitted connect intent
initialAudio  *bool                // immutable admitted connect intent
volume        *int                 // last RTSP-200-acknowledged requested percent
volumeState   string              // unrequested | pending | applied | refused
audioLevelReady bool              // startup level admitted, independent of a later pending write
volumeDesired *volumeRequest       // at most one latest pending request
volumeRunning bool                // at most one worker, including unwinding
volumeCancel  context.CancelFunc   // cancel outstanding bounded worker on detach
volumeSeq     uint64               // local request correlation; never on wire
// audioClosed is one-way and is independent of receiver volume.
audioClosed   bool
audioCancel   context.CancelFunc   // separate from the video/stream cancellation
// All capture attempts are registered under mu before starting work.
audioStarting bool
audioCapture  *airplay.AudioCapture
audioDone     chan struct{}        // closed AFTER start/stop/StreamAudio retire
volumeDone    chan struct{}        // closed only when the volume worker retires
```

File: `internal/airplay/mirror.go::MirrorSession`. Add private immutable setup fields
`initialVolume *int` and `initialVolumeState string`; `InitialAudioLevel` returns a copy of the
integer, never the stored pointer. Nil session/config absence produces `(nil, "unrequested")`.

File: `internal/daemon/daemon.go`. `volumeRequest` holds `percent int`, `seq uint64` and
one buffered result channel. Its result is acknowledged/rejected/superseded/closed/expired,
not an echoed requested value. Replacing the desired slot completes its displaced waiter as
superseded. Handler waiters do not each create an RTSP worker. No queue of desired values exists.

| File | Owner/function | Ordering and locking |
| --- | --- | --- |
| `internal/daemon/daemon.go` | `Daemon.handleConnect` | Validate new fields before publishing anything. Store copied intent and initialize `volumeState="pending"` only for a requested, audio-enabled level; otherwise `"unrequested"`. Initialize `audioClosed` from effective no-audio. Publish entry, register worker under `mu`, unlock, launch existing `connectAndStream`. |
| `internal/daemon/daemon.go` | `Daemon.connectAndStream` | Copy entry intent into local stream config; never mutate daemon config. Keep intent through the existing Digest/setup retry. After successful setup, publish session and its setup receipt under `mu` only if map still contains this exact entry. Start video as today; call `maybeStartAudio` only after that check. |
| `internal/daemon/daemon.go` | `Daemon.maybeStartAudio(entry *activeStream)` | Under `mu`, require exact entry, session, streaming state, negotiated audio, no closed latch, no existing/starting audio, and audioLevelReady. Initialize readiness true only for audio-enabled unrequested startup; set it true after applied setup or a current live RTSP 200, never from a partial setup acknowledgement. Register `audioStarting`, child cancellation and `audioDone` BEFORE unlocking and calling capture startup. |
| `internal/daemon/daemon.go` | `Daemon.maybeStartAudio` continuation | Bind to original entry, session and child context. After startup returns, recheck all identity/latch/cancellation conditions under `mu`. An obsolete/closed start stops its returned capture and closes its own completion channel; it never publishes/streams it. Otherwise publish capture and run existing `StreamAudio` with the child context. |
| `internal/daemon/daemon.go` | `Daemon.detachStreamLocked`, `daemonCleanup.addStream` | Latch/cancel audio, clear desired value and finish its waiter, cancel volume worker, detach exact resources under `mu`. Run stops and joins outside `mu`. Register all new workers in existing `streamWorkers` before shutdown can wait. |
| `internal/daemon/daemon.go` | `Daemon.statusResponseLocked` | Read all volume/latch fields under `mu`, copying pointer values. Retain existing deterministic target ordering and aggregate projection. No network or capture work while locked. |

Only the one audio owner calls `internal/airplay/audio.go::AudioCapture.Stop` for each capture;
the current method's boolean guard is not synchronization. That owner starts capture, validates
its continuation, then launches StreamAudio as its subordinate. It waits for either cancellation
or stream completion, cancels its child, calls Stop itself, joins StreamAudio and only then closes
audioDone. Stop must run before a blocked StreamAudio read is required to finish. On startup
failure/obsolete continuation stop any returned capture and close audioDone without streaming.
A nil completion channel means no unfinished producer; cancellation must be wired before
startup can run. Close, detach and ordinary stream-end cleanup request cancellation and join that
owner instead of racing independent calls to Stop. `internal/daemon/daemon.go::daemonCleanup`
gains `audioJoins []<-chan struct{}` and `volumeJoins []<-chan struct{}`; addStream records the
original channels and cancels under the existing detach discipline, run joins them after cancel/
resource closure outside mu. Volume cancellation prevents another dispatch; an already-sent write
finishes under its existing absolute deadline and cannot publish into a detached entry.
Do not wait for RTSP, capture startup, capture stop or a completion channel while holding
`internal/daemon/daemon.go::Daemon.mu`.

### 2.3 Setup execution order

All rows retain the existing portal/video-preparation, SETUP, RECORD/skipRecord, cipher/audio-
transport preparation and video-sink attachment order. “Capture” below means **audio** capture;
video preparation may already have started its encoder during SETUP.

| Requested level | Effective audio intent | Execution order after RECORD/skipRecord |
| --- | --- | --- |
| absent | on/unset | Existing write 1 with `"volume: 0.000000\r\n"`; existing write 2 with the same bytes; preserve legacy nonfatal complete-rejection handling and existing incomplete-response cleanup; publish session; start video; start audio capture if negotiated. State remains unrequested, percent absent. |
| present, including 0 | on/unset | Start one 2-second deadline; write 1 with requested bytes; if response complete, write 2 with the same requested bytes and remaining budget; record every 200 as the last acknowledged percent; publish applied only if **both** final responses were 200, otherwise refused; publish session; start video; start audio capture only for applied. Zero starts silent audio, not video-only. |
| absent | off | Suppress both volume writes; publish unrequested with absent percent and closed latch; start video; never start audio capture. |
| present | off | Retain request presence for validation but suppress both writes; publish unrequested with absent percent and closed latch; start video; never start audio capture. |

For requested level: any complete non-200, including the final refused Digest attempt, is
recoverable refusal; continue video but withhold audio. Any incomplete response or deadline
expiry **after sending** aborts setup immediately; do not attempt the second write on a poisoned
connection. Deadline expiry before an unsent required write also fails requested setup.
A first 200 followed by a complete rejection retains the known percent but reports refused and
withholds capture. A later live 200 is sufficient to recover; it does not repeat setup.
The legacy setup writes retain their existing success/error treatment and timers.

### 2.4 Live handlers

| File | Function | Contract |
| --- | --- | --- |
| `internal/daemon/daemon.go` | `Daemon.handleVolume(req Request) Response` | Validate volume; under `mu` select exact target or snapshot all streaming targets for targetless manual use. Refuse missing/nonstreaming/no-audio/closed entries. Replace each entry's desired slot, mark pending, start at most one worker, unlock, wait for each selected request's completion; serialize current full status. |
| `internal/daemon/daemon.go` | `Daemon.runVolume(entry *activeStream)` | Under `mu` take latest slot, preserve running flag across receiver I/O, call `SetAudioLevel` outside `mu`, re-enter `mu` and revalidate exact map entry/session/latch. Only a current RTSP 200 updates percent/audioMuted and sets audioLevelReady=true. A failure on an already-playing stream never stops its audio. A success schedules `maybeStartAudio` if previously withheld; latch checks remain mandatory. Drain the next latest slot, or clear running only when the worker actually exits. Set the reported state by the exact rule in §2.4.1. |

#### 2.4.1 `volume_state` is a settled fact, never a leftover

The reported state is computed **at the end of each worker turn** (and again after a closure
retires work), never inherited from admission. Apply these rules in order; the first that matches
wins, which makes them mutually exclusive:

1. **Work remains.** A write is in flight, or another desired value is already queued → `pending`.
   This is the only situation that may report `pending`, so a quiescent worker never leaves it behind.
2. **The latch is closed and quiescent.** `audio_closed` is true and nothing remains → `applied` with
   the last acknowledged percent when any percent was ever acknowledged, otherwise `unrequested` with
   no `volume`. This branch sits **above** the startup rule on purpose: once audio is closed the
   withholding question is moot, so a closed stream can never report `refused`, and readiness is
   left exactly as it was.
3. **Audio is still withheld by an unresolved startup decision, latch open.** `audioLevelReady` is
   false — startup audio has not been admitted, including when neither setup write was acknowledged
   or only one of the two was — → `refused`. A later
   rejected live write does **not** move this to `applied`: a partial acknowledgement is history, not
   permission to play, and `refused` is what lets Retry recover.
4. **Audio was admitted and a percent was acknowledged at some point** → `applied` with that percent.
5. **Audio was admitted and no percent was ever acknowledged** (unrequested startup) → `unrequested`
   with no `volume`.

Two consequences make the rules total rather than advisory:

- **`applied` always carries `volume`.** Only rules 2 and 4 produce `applied`, and both require an
  acknowledged percent, so the decoder's "`applied` requires `volume`" check can never fail on
  produced data.
- **A closure settles too.** The latch stops new work and drops the queue, and when the last
  permitted worker retires the state is recomputed by the same rules. Rule 1 no longer matches
  because nothing remains, so rule 2 applies: a closed stream reports `applied` with its last
  acknowledged percent, or `unrequested` when nothing was ever acknowledged — including a stream
  whose startup was withheld, which reports `applied`/`unrequested` rather than `refused` once it is
  closed. It also reports `audio_closed: true`, and the last acknowledged percent and mute facts are
  retained as history (`has_audio` keeps meaning "negotiated transport", not "sound"):

| Situation after this worker turn | `volume_state` | `volume` |
| --- | --- | --- |
| Write in flight or a newer value queued | `pending` | last acknowledged percent, or absent |
| Latch closed, nothing remaining, a percent was acknowledged | `applied` | that percent |
| Latch closed, nothing remaining, nothing ever acknowledged | `unrequested` | absent |
| Startup withheld, latch open, nothing queued (including a partial setup acknowledgement) | `refused` | absent or the partial percent |
| Audio admitted, percent acknowledged, nothing queued | `applied` | that percent |
| Audio admitted, nothing ever acknowledged, nothing queued | `unrequested` | absent |
| `internal/daemon/daemon.go` | `Daemon.handleAudioOff(req Request) Response` | Accept only explicit audio=false and no volume. Select target(s) as above, including connecting entries. Set latch and retire pending level under `mu` **before** cancellation/waits. Cancel each original entry's audio child; stop/join through its owner outside `mu`, including an in-progress startup. Wait up to 2 seconds total for this handler's selected entries. Return success only after all selected producers quiesce. |
| `internal/daemon/daemon.go` | `Daemon.handleRequest` | Dispatch wire `"volume"` and `"audio"`; audio accepts false only. Never add `"audio on"`. |
| `internal/daemon/daemon.go` | `Daemon.handleSetMute` | Retain legacy endpoints and normal wire behavior. Use integer mapping through existing library wrapper. Do not reopen a latch. Any recorded success must still refer to the original selected entry/session. |

The backend live handler wait is at most the remainder of one current 2-second write plus one
new 2-second write for a surviving pending request; superseded pending handlers finish immediately.
The 30-second socket deadline is not permission for an RTSP worker to exceed its 2-second bound.
Targetless targets run independently, rather than sharing one receiver-connection lock.

For targetless volume/audio, snapshot entries at admission; a subsequently added entry is never
included. Process selected targets independently; all must acknowledge for overall ok=true.
No command may use an IP lookup after an await to adopt a replacement entry. A closed latch
rejects new volume work, drops pending work and suppresses a late level result's bookkeeping and
capture continuation. If audio is already running, a pending or refused live level leaves its
producer running. Readiness stays true through later pending/refused live writes, so a successful
retry can start previously withheld audio even when another request is already pending. For each
handler return, revalidate
its originally selected entry/session under mu; a replacement produces ok=false even when the
old write/quiescence succeeded. Retiring audio never cancels the video context.

While a connecting entry is latched closed, its already-running setup may finish its own bounded
writes, but no capture continuation starts and no setup receipt reopens the latch. This differs
from connect-time audio off, which suppresses both setup writes from the outset.

Close timeout returns an unconfirmed failure while retaining the latch and the audio owner.
Repeated close joins the same outstanding owner; after its quiescence it succeeds idempotently.
A complete rejection cannot undo a latch already admitted. Failure before valid entry selection
has no side effects. A replacement gets a fresh latch initialized from its own connect intent.

### 2.5 Acknowledgements, status and help

File: `internal/daemon/daemon.go`. `Response` retains its existing fields. New live handlers
return the **full status envelope**, with ok=true only on confirmed completion. There is no
new top-level volume, and no volume field in a connect acknowledgement. `StreamInfo.has_audio`
continues to mean negotiated audio transport, not proof of capture/audibility; closure does not
rewrite that historical meaning. `audio_closed` is the latch value, not a quiescence receipt by
itself: only a successful close response confirms quiescence.

The initial connect response remains asynchronous and byte-compatible in shape:
`Response{OK:true, State:d.overallStateLocked(), Device:target}`. In the common sole-target case:
`{"ok":true,"state":"connecting","device":"192.0.2.10","has_audio":false,"audio_muted":false}\n`.
Do not promise connecting when another stream or the goroutine changes aggregate state.
Credential-resume acknowledgements retain their existing device_ip field and behavior.
New intent on a credential-resume request may only be absent or equal to the admitted intent;
otherwise refuse `"connect intent cannot change while waiting for credentials"`.

File: `cmd/doubletake/main.go`. `-capabilities` stdout, exit 0, exactly these UTF-8 bytes:

```text
capabilities-v1
status-source
status-source-complete
target-volume
```

Each line ends in LF, including the last. No banner or blank line. Sender help flag inventory
is unchanged from the current source profile. File: `cmd/doubletake-ctl/main.go::usage`.
Retain all eight existing rows except connect, and insert these exact rows (spaces significant):

```text
  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)
  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)
  audio off [target]                  Close mirrored audio (all streams, or only the given IP)
```

Their order is connect, volume, audio, pin; all remaining rows keep today's order/copy.
Both new rows carry their **own** affirmative scope statement. No new sender option is allowed.

## 3. Wire contracts

### 3.1 Argv and grammar

File: `src/airplay_linux/backend/control.py::PublishedDoubletakeProfile.try_argv`.
Elements below are individual argv elements; no shell, quoting or `--` insertion occurs.
`S` is the existing absolute private socket path, `T` the canonical host, `N` decimal percent.

| Intent | Exact tuple |
| --- | --- |
| capable connect, audio enabled with level | `("doubletake-ctl", "-socket", S, "connect", T, "--audio", "on", "--volume", N)` |
| capable connect, audio enabled without level | `("doubletake-ctl", "-socket", S, "connect", T, "--audio", "on")` |
| capable connect, video-only | `("doubletake-ctl", "-socket", S, "connect", T, "--audio", "off")` |
| capable level change | `("doubletake-ctl", "-socket", S, "volume", N, T)` |
| capable live audio closure | `("doubletake-ctl", "-socket", S, "audio", "off", T)` |
| legacy connect, either policy | `("doubletake-ctl", "-socket", S, "connect", T)` |
| legacy mute | `("doubletake-ctl", "-socket", S, "mute", T)` |
| legacy unmute | `("doubletake-ctl", "-socket", S, "unmute", T)` |
| existing targeted stop | `("doubletake-ctl", "-socket", S, "disconnect", T)` |

The capable no-level connect has the same setup behavior as today despite its explicit intent.
Optional typed intent=None and level=None also maps to the legacy connect tuple for manual/test
callers. The production capable transport always supplies its audio policy.

File: `cmd/doubletake-ctl/main.go`. Add `parseConnectArgs(args []string) (daemon.Request, error)`;
input excludes the command itself. Scan left to right using this grammar, without Go flag's
stop-at-first-positional behavior:

```text
connect := [TARGET] ["--audio" ("on" | "off")] ["--volume" PERCENT] [CREDENTIAL]
volume  := PERCENT [TARGET]
audio   := "off" [TARGET]
PERCENT := one to three ASCII decimal digits with numeric value 0..100
TARGET/CREDENTIAL := a nonempty argv element not beginning with "-"
```

One initial positional is always TARGET, as today; `connect 30` is target `30`, never a level.
After flags, one positional is the credential. `connect T 30` keeps today's credential meaning.
Flags can precede an omitted target; `connect --volume 0` targets the first free device.
Audio must precede volume if both occur. Repeats, out-of-order flags, equals forms, a flag after
a credential, extra positionals and unknown flags are invalid. Missing values never become PINs.
Leading zeroes in PERCENT are accepted and normalized numerically. Reject signs, whitespace,
Unicode digits, fractions and bool-like strings. Existing pin/mute/unmute/disconnect forms retain
their legacy parsing. The application still refuses all credential-bearing connect payloads.

File: `cmd/doubletake-ctl/main.go`. On new parsing failure: stderr is exactly the corresponding
line below, exit 1, no daemon client constructed/contacted, stdout empty. Error precedence:
check arity/order/unknown flags as encountered, then each encountered flag's value.

| Failure | Complete stderr bytes, escaped |
| --- | --- |
| missing/malformed/out-of-range --volume value or volume operand | `"volume must be an integer from 0 to 100\n"` |
| missing/invalid --audio value | `"audio must be on or off\n"` |
| other connect grammar error | `"invalid connect arguments\n"` |
| volume has extra arguments/flag target | `"Usage: doubletake-ctl volume <0-100> [target]\n"` |
| audio missing off/uses on/extra arguments/flag target | `"Usage: doubletake-ctl audio off [target]\n"` |

Zero volume operands uses the volume-value error; two or more operands after the required level
uses the usage error. An unknown flag in a value position is an invalid value for that knob.
Never interpolate rejected text or credentials. Existing main's `"error: %v\n"` prefix applies
to client I/O failures, not these parser messages.

### 3.2 Socket JSON and response semantics

File: `internal/daemon/daemonclient/client.go`. Keep `Client.Connect(target string, port int,
pin string)` unchanged as an unset-intent wrapper. Add:

```go
func (c *Client) ConnectWithAudio(target string, port int, pin string, audio *bool, volume *int) (*daemon.Response, error)
func (c *Client) Volume(percent int, target string) (*daemon.Response, error)
func (c *Client) AudioOff(target string) (*daemon.Response, error)
```

File: `internal/daemon/daemon.go::Request`. JSON has exact lowercase keys and JSON primitive
types below. Encode requests with the existing JSON encoder, compact and LF terminated.
Examples are complete byte strings; omitted target means manual broadcast/free-target selection.

```json
{"cmd":"connect","target":"192.0.2.10","audio":true,"volume":30}
{"cmd":"connect","target":"192.0.2.10","audio":false}
{"cmd":"connect","target":"192.0.2.10"}
{"cmd":"connect","target":"192.0.2.10","pin":"1234"}
{"cmd":"volume","target":"192.0.2.10","volume":30}
{"cmd":"volume","volume":0}
{"cmd":"audio","target":"192.0.2.10","audio":false}
{"cmd":"audio","audio":false}
```

Key order shown follows the existing struct fields, then Audio, then Volume. Protocol consumers
compare JSON members, not serialization order. `port` and `pin` retain existing connect semantics;
application requests omit both. Volume requires volume and forbids audio/port/pin; audio requires
explicit false and forbids volume/port/pin. Connect accepts absent audio/volume; explicit JSON
null for a new key is malformed, not absence. Preserve legacy decoding for commands without new
keys. At the socket decoder retain raw presence for these two keys before unmarshalling so null,
bool-as-int, fraction, overflow and wrong types cannot masquerade as omitted pointers.

File: `internal/daemon/daemon.go`. New command validation/failure strings, exact JSON `error`
values; each response has ok=false and the existing state/boolean envelope:

| Condition | `error` |
| --- | --- |
| invalid/missing volume or forbidden volume-command fields | `"invalid volume request"` |
| invalid audio/intent type or forbidden audio-command fields | `"invalid audio request"` |
| malformed new connect intent/level | `"invalid connect audio intent"` |
| selected target absent | `"no active stream to " + target` (existing target-only pattern) |
| no selectable entries | `"not currently streaming"` |
| selected stream not ready for volume/has no negotiated audio | `"audio control unavailable"` |
| closed latch | `"audio is closed for this stream"` |
| pending slot superseded | `"volume request superseded"` |
| complete receiver rejection | `"receiver rejected volume"` |
| volume admission/I/O deadline or incomplete response | `"volume update failed"` |
| close not quiescent before deadline | `"audio close not confirmed"` |
| detached/replaced entry during live work | `"stream was replaced"` |

These strings never become application diagnostic text. A volume success and a close success
both serialize current full status, not `{"ok":true}` alone. Example successful volume reply
from `internal/daemon/daemon.go::statusResponseLocked`, shown compact (socket ends with LF):

```json
{"ok":true,"state":"streaming","device":"TV","device_ip":"192.0.2.10","has_audio":true,"audio_muted":false,"streams":[{"device":"TV","device_ip":"192.0.2.10","state":"streaming","has_audio":true,"audio_muted":false,"volume":30,"volume_state":"applied","audio_closed":false}]}
```

Successful close has the same envelope with this exact stream's `"audio_closed":true` and
ok=true **after** the quiescence join. It retains last volume/mute facts; they do not imply sound.
A failed close may already report audio_closed=true, which does not convert ok=false to success.
The ctl continues indenting stdout by two spaces and exits 1 for ok=false, 0 for ok=true.

### 3.3 RTSP and status shape

File: `internal/airplay/client.go::AirPlayClient.rtspRequestOnceUntil`. The method is exactly
`"SET_PARAMETER"`, content type `"text/parameters"`, URI is the established audio/session URI,
never a fabricated stream id. Plaintext frame follows existing framing:

```text
SET_PARAMETER <sessionURI> RTSP/1.0\r\n
CSeq: <existing incremented decimal sequence>\r\n
User-Agent: AirPlay/935.7.1\r\n
[existing Authorization header and CRLF when Digest supplies it]
Content-Type: text/parameters\r\n
Content-Length: <decimal body byte length>\r\n
\r\n
<body from section 2.1, already ending in CRLF>
```

Angle-bracket values denote existing dynamic bytes, not literal brackets. No extra LF, NUL,
JSON, percent sign or second empty line follows the body. No X-Apple-Session-ID is added.
Existing HAP encryption and nonce ownership remain under the connection mutex.

| File | JSON location | Type and absence rules |
| --- | --- | --- |
| `internal/daemon/daemon.go::StreamInfo` | `streams[i].volume` | Integer 0..100; omitted until requested level receives RTSP 200; never null. Keep previous acknowledged value on refusal/pending. |
| `internal/daemon/daemon.go::StreamInfo` | `streams[i].volume_state` | Exact string unrequested/pending/applied/refused; always present in patched entries, including connecting entries. |
| `internal/daemon/daemon.go::StreamInfo` | `streams[i].audio_closed` | Boolean, always present in patched entries. Starts true for effective no-audio and can only become true. |
| `internal/daemon/daemon.go::Response` | top level | Existing ok/state/device/device_ip/has_audio/audio_muted/credential/devices/streams/error shape and omission rules unchanged; no aggregate volume or latch field. |

## 4. Python domain contract

All new public domain records are frozen, keyword-only dataclasses. Exact type validation rejects
bool as int. File columns define every symbol's location.

| File | Declaration |
| --- | --- |
| `src/airplay_linux/domain/resources.py` | `VolumeLevel(value: int)`; require exact int, inclusive 0..100; TypeError `"volume must be an integer"`, ValueError `"volume must be between 0 and 100"`. |
| `src/airplay_linux/domain/resources.py` | `StartRequest.volume: VolumeLevel \| None = None`; validate type if present; `TypeError("start volume must be a VolumeLevel or None")`. Zero allowed as start input, though it is never a remembered preference. |
| `src/airplay_linux/domain/resources.py` | `CapabilityReport.volume: bool = False`; exact bool; supplied only from proven backend feature. Existing capability booleans retain their gates. |
| `src/airplay_linux/domain/records.py` | `AudioStartReceipt(audio: bool, volume_state: str \| None = None, level: int \| None = None)`; exact bool, optional exact string from the four-state vocabulary, optional exact int 0..100. Applied requires level; pending/refused may carry an older acknowledged level. Missing state stays None. |
| `src/airplay_linux/domain/messages.py` | `SetVolume(receiver_id: ReceiverId, level: VolumeLevel)`; add to `Command`. Wrong level type is TypeError `"volume command requires a VolumeLevel"`. |
| `src/airplay_linux/domain/messages.py` | `TransportEventKind.volume_result = "volume_result"`; insert after mute_result; `VolumePayload(level: int, ok: bool)` joins `TransportPayload`, exact int 0..100/exact bool. This event is also the owned worker's settlement signal (§8). |
| `src/airplay_linux/domain/ports.py` | `TransportPort.start_stream(self, target: ReceiverTarget, source: CaptureSource \| None, audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel \| None = None) -> Result[StreamHandle]`. Preserve existing positional arguments and desktop-source None. |
| `src/airplay_linux/domain/ports.py` | `TransportPort.start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]`. |
| `src/airplay_linux/domain/ports.py` | `TransportPort.set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]`. Success is the exact acknowledged percent, including 0. |
| `src/airplay_linux/domain/ports.py` | `TransportPort.close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]`. Success requires confirmed quiescence, not just an admitted latch. |
| `src/airplay_linux/backend/contract.py` | `BackendFeatures.target_volume: bool = False`; include in existing exact-bool validation. |
| `src/airplay_linux/backend/contract.py` | `BackendStream.volume: int \| None = None`, `volume_state: str \| None = None`, `audio_closed: bool \| None = None`. Preserve missing evidence; strict present-value validation. |
| `src/airplay_linux/backend/contract.py` | `StatusReply.streams` contains these extended `BackendStream` records; no new aggregate fields. Update decoding/representation, retaining existing projection and metadata checks. |
| `src/airplay_linux/policy/capabilities.py` | `EnvironmentProbe._probe` | Produce report.volume from proven backend.target_volume; check/refresh expose the resulting report. Runtime audio readiness still gates widget/command admission separately. |

`VolumePayload` in backend/control.py and messages.py are different types; import the event type as an explicit alias at adapter boundaries. Follow the
existing direct-import convention: `src/airplay_linux/domain/__init__.py` has no export facade
and needs no change. Export the new control payloads from backend/control.py's existing __all__.
Existing identity/context/result/enumeration names retain their declarations in
`src/airplay_linux/domain/identity.py`, `src/airplay_linux/domain/operations.py` and
`src/airplay_linux/domain/resources.py` respectively.

File: `src/airplay_linux/backend/contract.py::_decode_stream`. Absent new members are accepted
for legacy status/mute/handshake replies. Present volume must be exact int 0..100; present
volume_state must be an exact allowed string; present audio_closed must be exact bool. Reject
null and invalid types/values as dependency_incompatible. Reject applied without volume. Do not
require new fields just because other status members are present, nor borrow them from another
target or an aggregate field. Refusal of a capable backend's **missing** receipt belongs to the
volume/audio-start path, not this shared decoder.

File: `src/airplay_linux/domain/resources.py::ErrorCode`. Add exactly these members in this
order after audio_mute_failed. File: `src/airplay_linux/diagnostics/catalog.py::ENTRIES` owns
these exact messages/recovery tokens; no interpolation:

| Enum member / serialized token | Fixed message | RecoveryAction values |
| --- | --- | --- |
| `audio_volume_failed` / `"audio_volume_failed"` | `The audio control change could not be confirmed. The last confirmed audio state is kept. Try the control again, or stop the session.` | `back`, `stop` |
| `volume_unavailable` / `"volume_unavailable"` | `The starting volume could not be confirmed. Audio is withheld. Retry audio, start video only, or stop the session.` | `retry_audio`, `video_only`, `stop` |
| `volume_preference_invalid` / `"volume_preference_invalid"` | `The saved volume is invalid and was ignored. Other receiver preferences are unchanged.` | `back` |
| `volume_preference_failed` / `"volume_preference_failed"` | `The receiver volume was applied but could not be saved. The previous saved level is kept.` | `back`, `stop` |

File: `src/airplay_linux/diagnostics/activity.py`. Register exactly three activity codes:

| Code | Fixed message | Fields used |
| --- | --- | --- |
| `"audio.volume_applied"` | `Receiver volume was applied.` | level |
| `"audio.volume_failed"` | `Receiver audio control change could not be confirmed.` | level when a level operation, error_code, optional fixed reason for closure |
| `"audio.volume_unavailable"` | `Receiver volume control is unavailable.` | reason, error_code when applicable |

File: `src/airplay_linux/diagnostics/activity.py`. Add `"level"` to `ALLOWED_FIELDS` and
`_INTEGER_BOUNDS` with (0,100), and `"reason"` as a captured-text vocabulary in
`_VOCABULARIES`: `"backend_unsupported"`, `"startup_refused"`, `"receipt_pending"`,
`"receipt_missing"`, `"no_audio"`, `"invalid_preference"`, `"audio_closed"`,
`"close_unconfirmed"`. No free-form reason is accepted. Include volume in `_CAPABILITY_FLAGS`
and its immutable normalized export. Reuse the existing boundary's exact-type and hook-free
validation; do not add type dispatch to the activity/catalog modules. No extra ErrorCode,
transport-event kind or activity code is needed for audio closure.

Failure activity is once per operation, availability activity once per transition (absence once
per session), malformed preference once per distinct stored record version/content. Stale
completions emit nothing. Preference-save failure uses its catalog diagnostic with the existing
last-error channel; it does not emit a second claim that the level itself failed.

## 5. Recognition contract

File: `src/airplay_linux/backend/contract.py`. The two accepted ctl argument maps are exactly:

```python
_PINNED_050_CTL_COMMANDS = {
    "status": "", "discover": "", "devices": "",
    "connect": "[target] [PIN-or-password]", "pin": "<PIN-or-password>",
    "disconnect": "[target]", "mute": "[target]", "unmute": "[target]",
}
_VOLUME_CTL_COMMANDS = {
    **_PINNED_050_CTL_COMMANDS,
    "connect": "[target] [--audio on|off] [--volume <0-100>] [PIN-or-password]",
    "volume": "<0-100> [target]",
    "audio": "off [target]",
}
_TARGET_VOLUME_TOKEN = "target-volume"
```

File: `src/airplay_linux/backend/contract.py::features_from_help`. Replace only surface
classification, mirroring the existing structure below; retain existing scope predicates and
all source/ownership gates. Reuse `_sender_surface`, `_ctl_surface`, `_target_scope_proven`,
`_PINNED_050_SENDER_FLAGS` and `_PINNED_050_SOURCE_SENDER_FLAGS` without weakening their scanners.

```python
sender_flags = _sender_surface(sender_help)
ctl_commands = _ctl_surface(ctl_help)
signatures = (
    None if ctl_commands is None
    else {name: row.arguments for name, row in ctl_commands.items()}
)
ctl_pinned = signatures == _PINNED_050_CTL_COMMANDS
ctl_volume = (
    signatures == _VOLUME_CTL_COMMANDS
    and _target_scope_proven(ctl_commands.get("volume"))
    and _target_scope_proven(ctl_commands.get("audio"))
)
source_surface = sender_flags == _PINNED_050_SOURCE_SENDER_FLAGS
source_tokens = {_CAPABILITY_SCHEMA_TOKEN, _STATUS_SOURCE_TOKEN} <= set(advertised)
target_volume = (
    ctl_volume and source_tokens and _TARGET_VOLUME_TOKEN in advertised
)
inconsistent = (
    (ctl_volume and not target_volume)
    or (ctl_pinned and _TARGET_VOLUME_TOKEN in advertised)
)
verified = (
    sender_flags in (_PINNED_050_SENDER_FLAGS, _PINNED_050_SOURCE_SENDER_FLAGS)
    and (ctl_pinned or ctl_volume)
    and not inconsistent
)
# Existing source-surface rejection: extra flag without tokens is not evidence.
if source_surface and not source_tokens:
    verified = False
patched_surface = verified and source_surface
source_complete = patched_surface and {
    _CAPABILITY_SCHEMA_TOKEN, _STATUS_SOURCE_TOKEN, _STATUS_SOURCE_COMPLETE_TOKEN,
} <= set(advertised)
# Preserve the existing return branches and their other fields.
# Unknown: every capability, including target_volume, false.
# Verified: profile is _PINNED_050_SOURCE_PROFILE if patched_surface,
# otherwise _PINNED_050_PROFILE; target_volume is the boolean above.
```

A volume-like ten-row table with a missing/negated/borrowed scope claim matches neither valid
surface; it refuses, even when ctl_volume becomes false before the inconsistency expression.
The old disconnect/mute/unmute scope proof still determines their existing gates independently.
Do not grant source completeness from target-volume or from only two source tokens. Existing
private composition cannot be inferred from any advertisement. Unknown tokens grant nothing.

| Evidence | Result in `src/airplay_linux/backend/contract.py` |
| --- | --- |
| exact old table, pinned sender | doubletake-0.5, target_volume=false |
| exact old table, source sender + existing source tokens | doubletake-0.5+source, target_volume=false |
| exact extended table, either accepted sender flag set, required volume/source tokens | Existing profile for that sender surface, target_volume=true; source_reporting still requires the source sender flag set. |
| extended table without token; token with old table; partial/malformed audio or volume row; missing/negated/borrowed scope | doubletake-unknown, every gate false; operation admission returns dependency_incompatible before mutation I/O |
| source help without its required tokens | same whole-backend refusal as today |

The pure classifier retains today's unknown-features return; probe callers/admission retain
their existing Result behavior. Do not change a profile string or any of its literal consumers.

## 6. Typed control contract

| File | Symbol | Change |
| --- | --- | --- |
| `src/airplay_linux/backend/control.py` | `BackendOperation` | Add `volume="volume"`, `audio_off="audio_off"` immediately after unmute, plus uppercase aliases `VOLUME`, `AUDIO_OFF`. Wire audio_off maps to command audio + operand off. |
| `src/airplay_linux/backend/control.py` | `VolumePayload` | Frozen keyword-only `level: VolumeLevel`; validate exact domain type. Distinct from the event payload in domain/messages.py. |
| `src/airplay_linux/backend/control.py` | `AudioGatePayload` | Frozen keyword-only `enabled: Literal[False] = False`; exact False only. No true payload supported. |
| `src/airplay_linux/backend/control.py` | `ConnectPayload` | Add `audio: AudioPolicy \| None = None`, `initial_volume: VolumeLevel \| None = None`; keep credential/source repr isolation. |
| `src/airplay_linux/backend/control.py` | `BackendPayload`, payload allowlists, `__all__` | Include both new control payloads in published and application-fixture validation/exports. |
| `src/airplay_linux/backend/control.py` | `FENCE_REQUIRED_OPERATIONS` | Add volume and audio_off; all existing members remain. |
| `src/airplay_linux/process/ownership.py` | `EvidenceAuthority.allows` | For each new operation require owned_daemon_control AND multi_target AND target_volume. No new handshake or token substitutes for existing receipt-backed proof. |
| `src/airplay_linux/backend/control.py` | `PublishedDoubletakeControl._available` | Require the same feature and actual authority admission; fixture operation admission also requires target_volume. |
| `src/airplay_linux/backend/control.py` | `PublishedDoubletakeProfile.try_argv` | Apply §3 tuples. New operations refuse missing target as unsupported_operation and wrong payload/level as invalid_input; absent feature refuses unsupported_operation. |
| `src/airplay_linux/backend/control.py` | `PublishedDoubletakeControl.request` | Preserve ownership revalidation, post-await dispatch fence, no-await-to-spawn rule and post-I/O ownership confirmation for both operations. Missing fence fails before any I/O. |

File: `src/airplay_linux/backend/control.py::PublishedDoubletakeProfile.try_argv`. A legacy
feature view accepts only unset new connect fields. The transport strips those fields for legacy;
a direct caller that supplies them gets unsupported_operation, not silent ignoring. On capable
connect, reject level with explicit disabled audio as invalid_input; direct low-level backend
clients still exercise the suppress-both-writes combination. Source/credential rejections stay as
today. No caller can insert credentials into argv.

File: `src/airplay_linux/backend/control.py::PublishedDoubletakeControl.request`. Decode both
new live successes with `src/airplay_linux/backend/contract.py::decode_status` and return that
`StatusReply` through the existing reply union. Do not add AckReply.volume. For volume, require
one matching streaming entry, applied state, audio_closed=false, has_audio=true and exact
acknowledged percent equal to the payload. For audio_off, require one matching entry and
explicit audio_closed=true, plus ok=true from this close call. A connecting entry is acceptable
for closure. Duplicate/foreign/missing entry, malformed JSON or missing confirmation refuses;
never use top-level aggregate booleans to supply this evidence.

File: `src/airplay_linux/backend/control.py::_rejection_code`. Both new commands map valid
ok=false/exit-1 responses to audio_volume_failed. New ack validation failures also use this fixed
code; keep ownership/target/dependency failures typed. Exit/JSON envelope validation remains
strict as today. Connect still uses existing `_parse_ack` and reports admission only.
`ApplicationContractProfile` keeps its explicit non-live argv name, gains the new typed payload
branches, and defaults target_volume=false; test fixtures opt in explicitly.

## 7. Transport contract

File: `src/airplay_linux/transport/doubletake.py`. Extend `_Stream` with
`initial_volume: VolumeLevel | None = None`, `audio_closed: bool = False`,
`volume_state: str | None = None` and one optional owned volume-task record (operation context,
handle, task, expired flag). These are private and transient.

| Function, all in `src/airplay_linux/transport/doubletake.py` | Contract |
| --- | --- |
| `DoubleTakeTransport.start_stream`, `_start` | Validate new value before allocation. Only capable+enabled uses initial_volume. Store intent on exact lease. Capable connect builds ConnectPayload(audio=policy, initial_volume=value); legacy builds existing empty ConnectPayload. Preserve all source/trust/target/readiness/cleanup barriers. |
| `DoubleTakeTransport._reported_stream_audio` | Return Result[AudioStartReceipt], not Err-or-None. Use the **one existing** status request; require exactly one matching streaming entry; retain existing ownership/current-handle checks. Receipt fields come from that entry only: audio=has_audio, volume_state, level=volume. **A matching entry that reports `audio_closed: true` never yields an enabling receipt** — it returns `Err(audio_unavailable)`, because the latch permanently prevents sound even though the transport is still negotiated. This covers a closure made by another tool or by `audio off`, not only the application's own. No second query.
| `DoubleTakeTransport.start_audio`, `_start_audio` | Return receipt from the above call for the published profile. Before returning a current capable receipt, record its volume_state; applied+audio updates stream.audio/muted from level; refused/pending/missing clears confirmed mute and records Unavailable without emitting audio_recovered. For an independent legacy start-audio operation, normalize acknowledged success to AudioStartReceipt(audio=True, volume_state=None, level=None). Preserve existing 5-second deadline. |
| `DoubleTakeTransport.set_volume`, `_set_volume` | Feature, level, exact handle/current target, enabled policy, no latch, current generation/lease and daemon checks before work and after awaits. Normally require Enabled/Muted audio; additionally admit an exact original initial_volume retry when stream.volume_state is refused and negotiated audio exists. Issue BackendOperation.volume with typed VolumePayload through `_request`. Return exact acknowledged percent; update stream.muted/audio only on a current, unexpired confirmed success. |
| `DoubleTakeTransport.close_audio` | Feature/exact ownership checks; latch local stream.audio_closed and audio_policy.enabled=False at admission; issue BackendOperation.audio_off with AudioGatePayload through `_request`. Repeated close is admissible even though local policy is already disabled. Success returns Ok(None); failure never clears local latch. |
| `DoubleTakeTransport._request`, `_dispatch_fence` | Both new commands require original current-target fence, not cleanup permission. Preserve every existing wrapper-compatibility, endpoint, generation and post-I/O check. |
| `DoubleTakeTransport._stop_owned`, `_drop`, `_on_daemon` | Retire pending volume slot/results for exact lease; retain existing cleanup ownership. No volume failure invokes these functions. |

For capable receipts, refusal/pending/missing evidence is returned as a typed receipt for the
actor to interpret; don't short-circuit on has_audio=false before conveying refusal. Applied
requires valid level and audio=true to confirm audio; no-audio is an audio-unavailable outcome.
A legacy entry with has_audio=false retains today's typed audio_unavailable failure. Shared
status decoding remains permissive about omission, as specified in §4. Keep the private
volume_state as the actor-admitted startup/recovery evidence, updating it to applied on successful
set_volume; a failed live level does not manufacture a new startup-refused decision. At startup
with unrequested state, retain existing audio semantics and no effective percent.

The capable transport must not emit audio_recovered before the receipt is interpreted.
Return the receipt in the existing audio_start/audio_retry effect, and let the actor perform the
state/event decision together. Legacy event timing/meaning remains as today. Do not publish a
new raw backend notice as correlated stream evidence.

Live volume/close public waits use existing CONTROL_TIMEOUT=5.0 and `_wait` conventions.
The volume public timeout marks that task record expired and reports audio_volume_failed;
it does **not** mean the task has settled. A post-timeout ack does not update effective state,
last-audible, stream.muted or preferences. The actual volume task's done callback emits exactly
one volume_result **after task.done() and lock release**, including failures/cancellation, with
its original handle/context/level. Its payload.ok is true only for a current, nonexpired,
acknowledged success. This is the settlement signal used in §8. Do not emit an earlier result
from inside the lock. Every actor-admitted call with valid typed level/handle/context gets this
one callback, including a transport-preflight refusal: run validation inside the owned task.
If validation has no current _Stream, construct the settlement from the supplied original typed
handle/context, rather than resolving an entry by address; normal manager fences discard it if
obsolete. Malformed direct API arguments return invalid_input before task creation and occupy no
actor slot.
The callback is tied to actual worker retirement, not the control subprocess's public timeout.
Mark expiration in `_wait`'s abandon callback before a late worker can publish success.

Audio closure must be able to dispatch while a volume worker remains alive: use a separate
close path rather than queuing behind `_Stream.lock`. Its exact-target dispatch fence also checks
that the owned stream has not been released/replaced. All volume continuations recheck the local
latch after awaits. Stop still joins existing acquisition/control cleanup as required; it must
not wait for a public timeout to *mean* release. A close ack arriving after its public timeout is
ignored for state confirmation; a later explicitly admitted close must obtain its own success.

## 8. Session authority contract

### 8.1 State, single knob and settlement

| File | Symbol | Additions |
| --- | --- | --- |
| `src/airplay_linux/domain/records.py` | `SessionSnapshot` | `remembered_volume: int \| None = None` (1..100); `effective_volume: int \| None = None` (0..100); `last_audible_volume: int \| None = None` (1..100); `volume_state: str \| None = None`; `audio_close_pending: bool = False`; `volume_save_failed: bool = False`. Validate exact types/ranges. |
| `src/airplay_linux/session/audio.py` | `AudioRuntime` | Retain pending_mute/muted_lease for legacy. Add `volume_inflight: tuple[OperationId, LeaseId, int, str] \| None`, `volume_desired: tuple[OperationId, LeaseId, int, str] \| None`, `volume_reported: bool=False`, `close_operation: OperationId \| None=None`, `volume_absence_reported: bool=False`, `start_volume: VolumeLevel \| None=None`, `preference_operation: OperationId \| None=None`. Origin strings are exactly slider/mute/unmute/retry; only slider can persist a preference. start_volume carries the admitted startup input across pre-stream continuations, including explicit zero; preference_operation fences the latest acknowledged slider save. Neither is durable. |
| `src/airplay_linux/session/audio.py` | `_AUDIO_EFFECTS`, `_AUDIO_EVENTS`, `AudioLifecycle.claim` | Include volume, audio_close, remember_volume effects and volume_result event. Claim SetVolume. Handle receipt values in audio_start/audio_retry. |
| `src/airplay_linux/session/engine.py` | `SessionManager._route` | Route SetVolume to Route.audio; retain existing StartVideoOnly routing/interposition. |
| `src/airplay_linux/session/fencing.py` | `accept_transport` | Classify volume_result as audio result, requiring existing full stream/lease/session/endpoint/daemon/audio-operation fences. The handler additionally checks its individual operation id. |

File: `src/airplay_linux/session/audio.py::AudioLifecycle`. Admission uses the current
`src/airplay_linux/domain/resources.py::CapabilityReport.volume` AND the existing audio/trust/
endpoint/cleanup conditions. No capable stream calls backend mute or unmute: translate mute
into level 0 and unmute into last_audible_volume ?? remembered_volume ?? 100. Only current
acknowledgements can change effective_volume/last_audible_volume or confirmed mute.

For audio present, acknowledged level 0 sets snapshot.muted=true, AudioState.Muted,
AudioRuntime.muted_lease=current lease, and transport stream.muted=true. Positive sets all four
representations to unmuted/Enabled. Audio loss clears confirmed mute representations and
retires pending work; a later zero receipt restores them consistently. Preserve last acknowledged
level/last-audible on the same lease as historical facts; controls remain disabled until recovery.
Replacement/fresh lease clears both values and all mute authority.

File: `src/airplay_linux/session/audio.py::AudioLifecycle`, coalescing algorithm:

```text
on valid level intent:
    mint individual operation id; retain established audio_operation_id
    if no volume_inflight: launch one volume effect and record original id/lease/level/origin
    else: replace volume_desired with latest intent (never launch another worker)
on public volume EffectEvent:
    Err for the still-inflight operation: report failure once; keep occupied until settlement
    Ok: volume_result owns settlement/ack publication; apply nothing here
on volume_result from the actual worker's done callback:
    require all ordinary fences AND operation id/lease == volume_inflight
    retire that worker; never interpret a public wait's completion as this signal
    if closed policy or stale: no ack update
    otherwise if ok: apply acknowledged level and origin's state/persistence rule
    otherwise: report failure once, unless already reported
    drain exactly the one latest desired intent if still admissible
on Stop, audio loss, lease replacement, endpoint/daemon invalidation:
    retire desired/inflight publication authority; do not restart/drain obsolete work
```

All actor-admitted set_volume calls have exactly one settlement event, including transport
validation failures, so the actor always waits for volume_result. No additional port method or
event kind is introduced. Merely replacing the pending desired slot does **not** supersede the
in-flight operation's acknowledgement: that acknowledgement is a valid intermediate fact about
the receiver and updates all mute representations together before the next dispatch. Never issue
work or save a value for a desired slot that was replaced before dispatch.

A transport volume_result is ordered after that call's timeout bookkeeping if timeout won.
Mark expiration synchronously in the existing deadline/abandon callback. Public-effect and
result-event delivery may arrive in either order; consuming the settlement operation once
prevents duplicate diagnostics or a second drain. A late public effect from an already consumed
operation is ignored. Actual supersession by Stop, replacement, audio loss or closure prevents
publication and saving; a queued desired value alone does not.

File: `src/airplay_linux/session/audio.py::AudioLifecycle`. Each dispatched volume effect carries
its individual id in `src/airplay_linux/domain/operations.py::OperationContext.operation_id` and the established
id in audio_operation_id; minting a queued individual id must not replace snapshot.operation_id.
A refused-start Retry mints a new audio_operation_id because _hold_decision retired the old one — and
**only** when no volume worker is occupied. While a worker is occupied, a repeated Retry records
`retry_requested` and mints nothing that could rotate the established audio fence: the outstanding
worker's settlement must still be accepted, otherwise the actor can neither retire it nor drain its
slot. The new audio id is minted once, at settlement, immediately before the single re-dispatch.

Retry-origin failure and timeout transitions are explicit: an accepted `origin=retry` volume whose
result is a refusal, an error or an expired public wait returns the actor to
`Unavailable / audio_decision` (retryable) while **retaining** the occupied worker's audio fence and
inflight record. It never enters or leaves `Starting` in that case, and it never calls
`_hold_decision`, which would clear the fence the worker still needs.
Launch volume/audio_close/remember_volume through Route.audio so existing engine effect fences
apply; their handlers additionally compare volume_inflight/close_operation/preference_operation.
For closure, replace the audio-operation fence before launch to suppress old Retry results while
keeping the new close effect admissible. No global engine acceptance exception is introduced.

When an acknowledged positive slider level launches a save, set preference_operation to its
individual id and capture its full lease/intent fence in still_current. This fence survives
retirement of volume_inflight. A later acknowledged positive slider level supersedes the older
save-publication id; queued intents and mute do not. Retire it on Stop/loss/replacement/closure.
The shared writer serializes commits and each effect applies only if that publication id is
current, so a delayed save cannot overwrite newer remembered state. At receipt, volume-result,
close-result and save-result publication, also compare the captured trust_generation with the
current snapshot and require the original current target, no trust block and no cleanup. A
failed comparison is silent and never drains old desired work; no broader acceptance change to
`src/airplay_linux/session/engine.py::SessionManager._admit` is needed.

### 8.2 State-machine rows

All rows below are owned by `src/airplay_linux/session/audio.py::AudioLifecycle`, except start
acquisition in the three named lifecycle files. “Keep video” means no stop, reconnect, display
mutation or replacement stream. `r`, `e`, `a` denote remembered/effective/last-audible.

| Input/state | Admission and effect | Confirmed transition / failure |
| --- | --- | --- |
| Start, capable, enabled, r present | Existing start with initial_volume=r and audio on | Receipt decides state; no optimistic e. |
| Start, capable, enabled, r absent | Connect audio on, no volume unless an explicit StartRequest.volume was supplied | Unrequested receipt follows legacy audio behavior; explicit input follows requested receipt rules. |
| Start, incapable, any saved level | Existing exact connect argv | Existing audio behavior; ignore saved level for transport. |
| Start, capable, policy disabled | Connect audio off, no volume | Disabled, muted=false, closed new stream; no audio writes/capture. |
| Start, legacy, policy disabled | Existing legacy path | Exact existing state/commands; do not send new flags/barrier. |
| Slider n=1..100, Enabled/Muted, capable/current | Queue one volume n | On current ack e=a=n; Enabled; persist n, update r only after save succeeds. |
| Slider 0, Enabled/Muted, capable/current | Queue volume 0 | On ack e=0, Muted; retain a/r; no preference write. |
| Speaker mute, Enabled, capable | Queue volume 0 | On ack e=0, Muted; retain a/r. |
| Speaker unmute, Muted, a known | Queue volume a | On ack e=a, Enabled; retain r. |
| Speaker unmute, Muted, a unknown | Queue volume (r ?? 100) | On ack e=a=chosen value, Enabled; retain r. |
| Legacy speaker mute/unmute | Existing set_muted path | Existing confirmation/persistence behavior. |
| RetryAudio, startup refused, policy enabled, same lease, r known | Mint fresh audio_operation_id, queue volume r with retry origin; transport admits the withheld-start exception | On ack e=r, a=r if positive, audio active/recovered, keep video; r unchanged. |
| RetryAudio, refused explicit-zero startup with no r | Retry the retained start_volume=0 on the same lease with a fresh audio operation | On ack Muted/e=0, no saved zero and no invented positive value. |
| RetryAudio, pending/missing capable receipt | Existing start_audio call on same handle | Re-sample one receipt; no invented volume and no new timer. |
| RetryAudio, other audio loss, policy enabled | Existing retry; capable receipt interpretation | Keep lease/video; same-lease confirmed zero restores Muted, positive Enabled. |
| Slider/mute while Disabled, Starting, Unavailable or audio_decision | Refuse; no backend call | Preserve e/a/r, except explicit refused Retry row above. |
| Any volume intent without capability/current stream or under cleanup/trust block | Refuse, no backend mutation | Existing typed unsupported/stale/blocked admission, no preference write. |
| Live volume complete refusal/failure/public timeout | Fail that operation once | Keep video, e/a/r and confirmed mute unchanged; keep worker occupied until settled. |
| Startup requested write incomplete/timed out | Existing failed setup/transport cleanup | No retryable-stream promise; ordinary Retry/Start admission after cleanup. |
| Later has_audio=true after a refused receipt | Observation only | Cannot clear refusal/confirm audio; RetryAudio remains required. |
| Stale/duplicate volume result | Consume no current publication | No state, diagnostic, activity or preference mutation. |
| Stop / endpoint or daemon invalidation | Retire desired publication and audio operations | Existing exact cleanup; late volume cannot revive audio. |
| Lease replacement/reconnect | Clear e/a/mute/receipt/close-pending for old lease | New stream uses CURRENT policy and validated r, never inherited e/a. |
| Changed receiver identity | Apply PreferenceAccess rejection | No inherited r/e/a; no level sent from replaced record. |
| Preference write failure after positive slider ack | Keep applied e/a | r stays previous; volume_save_failed=true and fixed save diagnostic; keep video. |
| Subsequent successful positive slider save | Update r | Clear volume_save_failed, refresh preference cache. |

File: `src/airplay_linux/session/audio.py::AudioLifecycle`, receipt handling in **one actor step**:

| Receipt on capable current lease | AudioState | IntentStage | mute/e/a | Event decision |
| --- | --- | --- | --- | --- |
| applied, audio=true, level>0 | Enabled | active | false; e=level, a=level | audio_recovered; emit existing audio.recovered once |
| applied, audio=true, level=0 | Muted | active | true; e=0, retain a | audio_recovered; emit existing audio.recovered once |
| refused | Unavailable | audio_decision | false; e/a preserved if a level was already confirmed on this lease, otherwise unknown | audio_start_failed; volume_unavailable / startup_refused |
| pending | Unavailable | audio_decision | false; e/a preserved if a level was already confirmed on this lease, otherwise unknown | audio_start_failed; volume_unavailable / receipt_pending |
| missing volume_state | Unavailable | audio_decision | false; e/a preserved if a level was already confirmed on this lease, otherwise unknown | audio_start_failed; volume_unavailable / receipt_missing |
| applied with audio=false | Unavailable | audio_decision | false; no confirmed level | audio_start_failed; audio_unavailable / no_audio |
| unrequested, or any legacy receipt | existing behavior | existing behavior | unknown e/a unless already confirmed on same lease | existing behavior |

The transport returns the receipt; the actor's effect handler performs the listed event decision
and state update together, rather than feeding a new unfenced synthetic transport notification
back through the manager. Failure uses the existing three-action audio decision. Extend
`src/airplay_linux/session/audio.py::_decision_error` so it preserves volume_unavailable.
Pending never rests in Starting. Receipt error and actual transport failure remain distinct.

The three non-enabling rows are deliberately uniform: a receipt that cannot enable audio clears
the *confirmed mute* but **never erases a level this lease already had acknowledged**, because
§2.4.1 legitimately serialises `pending` with a last acknowledged percent. `e`/`a` are reported
unknown only when no level was ever confirmed on that lease. This is the same fact §8.1 states as
"preserve last acknowledged level/last-audible on the same lease as historical facts", and it is
what stops a recovery read from discarding a good level after a failed adjustment.

### 8.3 Video-only admission and latch

File: `src/airplay_linux/session/audio.py::AudioLifecycle._explicit_video_only`.

| State/input | Required actor ordering | Result |
| --- | --- | --- |
| No stream, audio_decision, choose video only | Cancel prior audio authority; apply AudioPolicy(enabled=False) first; publish Disabled; continue existing start | Next capable connect carries audio off; no live barrier. |
| Existing stream, audio_decision, choose video only | In one dispatch: apply policy=false FIRST; clear desired level slot and supersede retry publication; replace audio-operation fence, set audio_close_pending=true and fresh close_operation; issue close_audio for exact handle | Keep existing audio state until close ack; keep video. |
| Current close Ok(None) | Verify close operation/lease/context; clear pending | Disabled, muted=false, intent_stage=active if video streaming; policy stays false. |
| Current close refusal | Keep policy=false and audio state; retain pending marker | One audio_volume_failed diagnostic/activity reason close_unconfirmed; allow repeated StartVideoOnly and Stop. |
| Current close timeout | Same policy/latch retention; do not infer confirmation | No Disabled claim. Repeated close and Stop allowed; RetryAudio/SetVolume/SetMuted refused. |
| Late ack for obsolete retry or timed-out close | Discard for state | Cannot publish Enabled/Muted/Disabled, reopen, persist or emit activity. |
| Reconnect/replacement before old close confirms | Retire old close publication; read policy=false for new start | New connect audio off; old uncertainty cannot re-enable new audio. |

Admission of repeated StartVideoOnly is allowed while policy=false and audio_close_pending=true,
including while an old close waiter is alive. Deduplicate/join the transport close worker per
lease if still running, but require a fresh acknowledged request after a public timeout before
publishing Disabled. Do not convert ordinary Retry into volume Retry while policy is false.
Every row in §8.3 applies only when volume is proven; legacy
`src/airplay_linux/session/audio.py::AudioLifecycle._explicit_video_only` keeps today's exact
partial-stop/Disabled behavior and never calls close_audio. On the capable branch, the UI retains
the existing video-only label for repeated closure; no new action is necessary.

### 8.4 Startup propagation and persistence

| File | Functions/symbols | Required propagation |
| --- | --- | --- |
| `src/airplay_linux/ui/presenter.py` | `Presenter._start_request` | Read current cached SavedReceiver through PreferenceAccess; fill StartRequest.volume only with proven capability and enabled policy. |
| `src/airplay_linux/session/mirror.py` | `MirrorLifecycle._start`, `_begin_stream` | Retain request.volume in AudioRuntime.start_volume across capture/probe/pairing; snapshot remembered value only accepts positive input. Pass captured startup value, including explicit zero, to initial_volume; capture policy/value together before effect awaits. |
| `src/airplay_linux/session/virtual.py` | `VirtualLifecycle._start`, `_begin_stream`, resolution continuation | Preserve start_volume across initial resolution/pairing continuations. For later stream replacement, use current remembered preference and policy, clearing old e/a. |
| `src/airplay_linux/session/reconnect.py` | `ReconnectLifecycle._begin_stream` | Capture current policy and snapshot remembered value; never take an old retry's policy or effective level. |
| `src/airplay_linux/session/persistence.py` | `_safe_snapshot` | Strip e/a, volume_state, audio_close_pending, volume_save_failed beside muted/audio operation. No pending slot/task persisted. Remembered volume is reloaded, not session authority. |
| `src/airplay_linux/storage/codecs.py` | session/start allowlists | Keep session schema at v1. Omit all new transient snapshot members. Do not add volume to the durable nested StartRequest projection; reconstruction defaults None and recovery supplies receiver preference. |
| `src/airplay_linux/session/recovery.py` | `StartupRecovery._restore` | Reload remembered_volume from valid receiver preference only; defaults e/a/state/close-pending/save-failed. Never restore a volume from old session bytes or a refused identity. Retain durable audio_policy. |

## 9. Storage contract

### 9.1 Receiver v2

File: `src/airplay_linux/domain/records.py::SavedReceiver`. Add
`remembered_volume: int | None = None`; exact int 1..100 if present. Zero is not storable.
File: `src/airplay_linux/storage/codecs.py`. Receiver schema remains `"airplay-linux/receiver"`;
receiver document version becomes **2**. All old receiver fields retain their validation.

```text
v1 exact member set:
  schema, version, receiver_id, service_metadata, display_name,
  last_endpoint, continuity_fingerprint, remembered_resolution
v2 exact member set:
  all v1 members, plus remembered_volume
remembered_volume: JSON integer 1..100 or null; always encoded, including null
```

File: `src/airplay_linux/storage/codecs.py`. `encode_receiver` always emits v2, using existing
ASCII, sorted-key, compact JSON with no trailing newline and allow_nan=false. Example exact output
for a valid minimal receiver (no credential reference):

```json
{"continuity_fingerprint":"fp","display_name":"TV","last_endpoint":null,"receiver_id":"airplay-device:a","remembered_resolution":null,"remembered_volume":30,"schema":"airplay-linux/receiver","service_metadata":[],"version":2}
```

Replace 30 with null for absence. Retain refusal of non-null credential_ref and ambiguous identity.
`decode_receiver` reads v1 using its **old exact field set** and supplies remembered_volume=None;
v2 requires the new key, but its invalid leaf is droppable. A missing key is structural corruption,
not a malformed preference. Unknown extra keys, bad identity, invalid old fields, wrong schema,
unknown version or non-int version still refuse the record.

File: `src/airplay_linux/storage/codecs.py`. Replace the global-version-only test with:

```python
VERSION = 1  # unchanged default for every other existing schema
_ACCEPTED_VERSIONS = {
    SESSION_SCHEMA: frozenset({1}), OWNERSHIP_SCHEMA: frozenset({1}),
    DAEMON_SCHEMA: frozenset({1}), RECEIVER_SCHEMA: frozenset({1, 2}),
}
_WRITE_VERSIONS = {RECEIVER_SCHEMA: 2}
# _load_document: exact int version in _ACCEPTED_VERSIONS[schema];
# select exact receiver field set by version BEFORE field-set validation.
# _dump_document: _WRITE_VERSIONS.get(schema, VERSION).
```

Leave newer reservation/receipt codecs' separate schema/version mechanisms untouched. Do not
allow v2 sessions, ownership or daemon records as a side effect of v2 receiver support.

File: `src/airplay_linux/storage/codecs.py::decode_receiver`, signature:

```python
def decode_receiver(
    payload: bytes, *, on_diagnostic: Callable[[Diagnostic], None] | None = None,
) -> SavedReceiver: ...
```

Validate identity/structure/all other fields first. For v2's new leaf only, exact int 1..100 is
valid and null is absent; every other JSON value (including 0, bool, string, fraction, array,
object, negative or huge integer) becomes None and reports volume_preference_invalid through the
callback. The callback sees only a catalog diagnostic with validated receiver_id, never bad data.
Callback failure cannot refuse an otherwise valid record. No automatic rewrite on read.

File: `src/airplay_linux/storage/repositories.py::JsonReceiverStore`. Add optional constructor
`on_diagnostic: Callable[[Diagnostic], None] | None = None`; pass it through get/all reads.
Retain a per-receiver digest of the invalid record bytes already reported so repeated identical
reads emit only once; changed valid content clears the digest. In
`src/airplay_linux/runtime.py::_compose_production`, supply a local callback at construction:
before ActivityLog exists it retains at most one sanitized pending warning per receiver; after
construction bind its local activity reference and flush once. This needs no store setter/new port.
Both direct codec tests and repository tests must exercise the callback. A bad volume never
makes get/all Err or startup blocked; structural/identity errors retain the existing fail-closed
listing and StartupRecovery behavior. Keep corrupted bytes on disk.

### 9.2 Single preference writer

File: `src/airplay_linux/storage/repositories.py`. Add one shared class in this existing module:

```python
class ReceiverPreferenceWriter:
    def __init__(self, store: ReceiverStore, discovery: DiscoveryPort) -> None: ...

    async def update(
        self, record_set: ReceiverRecordSet, *,
        remembered_resolution: ResolutionDecision | None = None,
        remembered_volume: VolumeLevel | None = None,
        still_current: Callable[[], bool],
    ) -> Result[SavedReceiver]: ...
```

Exactly one preference argument is supplied. None means **unchanged**, not erase; zero volume
refuses invalid_input. No volume clear operation is needed. Lock per receiver for the whole
read-modify-write: await latest get **inside** lock, revalidate current discovery record set,
still_current and PreferenceAccess after that await, merge only the supplied field, then await
put before releasing lock. Build a missing valid record using the same metadata as the existing
resolution writer. Never reuse a whole cached SavedReceiver as the write base. A replaced,
ambiguous, nonpersistable or unjustified existing record refuses stale_target. Recheck freshness
after put before publishing the returned record; stale completion cannot update actor/UI state.
A commit already admitted cannot be rolled back by rewriting an older record over newer data.

| File | Consumer | Required use |
| --- | --- | --- |
| `src/airplay_linux/session/engine.py` | `ManagerDeps` | Add `preferences: ReceiverPreferenceWriter` dependency using the class in storage/repositories.py; no per-handler construction. |
| `src/airplay_linux/runtime.py` | `_compose_production` | Construct writer once from receivers/discovery; supply same instance to both paths. Wire malformed-preference diagnostics to existing safe activity/catalog channel after ActivityLog construction. |
| `src/airplay_linux/session/virtual.py` | `VirtualLifecycle._remember` | Replace nested receiver read-modify-write with shared update(remembered_resolution=...). Keep its resolution evidence/admission checks. |
| `src/airplay_linux/session/audio.py` | `AudioLifecycle`, remember_volume effect | Positive current slider acknowledgement invokes update(remembered_volume=VolumeLevel(value=n)); still_current binds original receiver, intent, lease and preference_operation. Apply returned remembered value only after success and the same publication-id check. |
| `src/airplay_linux/ui/presenter.py` | `Presenter._snapshot_arrived`, `_load_saved` | Coalesce receiver-record reloads after fresh snapshot publications; successful preference effects publish a snapshot even if other fields compare equal. A read crossed by another fresh publication reruns once for latest data. Refresh cached record only after current continuity validation. |

The writer's per-receiver lock is shared by resolution and volume calls. Independent receivers
need not serialize. PreferenceAccess itself remains unchanged. Mutation of r is a result of
successful durable preference publication, not optimistic acknowledgement. Storage failures in
the volume path become volume_preference_failed without erasing applied e/a.

## 10. UI contract

File: `src/airplay_linux/ui/window.py::MainWindow._build_pane`:

```text
selectedPane / existing primary action row: Start, Stop, Pair, muteToggle
volumeRow (QWidget, full pane width, immediately after primary row)
  volumeLabel (QLabel): "Volume", buddy=volumeSlider
  volumeSlider (QSlider, horizontal): 0..100, singleStep=1, pageStep=10
  volumeReadout (QLabel): projected acknowledged readout
```

Keep row visible with no selection/support/audio. No second mute button. Slider accessible name
is exactly `"Receiver volume"`; readout accessible name `"Receiver volume level"`; row label
`"Volume"`. Use StrongFocus and tab order after muteToggle. Arrows move 1, PageUp/PageDown 10,
Home 0, End 100, clamped to bounds. No wheel/keyboard path bypasses admission.

File: `src/airplay_linux/ui/state.py::PaneState`. Add `volume_supported: bool=False`,
`volume_enabled: bool=False`, `volume_level: int | None=None`, `volume_readout: str="—"`,
`volume_tooltip: str=""`, `volume_save_failed: bool=False`. Project values from acknowledged
snapshot only; desired slider position must not become an acknowledged numeric readout.
File: `src/airplay_linux/ui/state.py`, predicate/reason pseudocode (first match wins):

```python
# File: src/airplay_linux/ui/state.py; local projection variables.
if not inputs.capabilities.volume:
    enabled, reason = False, "The installed backend does not support receiver volume."
elif not selected:
    enabled, reason = False, "Select a receiver to change its volume."
elif state.stream is None or state.transport is not TransportState.Streaming:
    enabled, reason = False, "Start a stream to change receiver volume."
elif (not state.audio_policy.enabled or state.audio_close_pending
      or state.audio not in (AudioState.Enabled, AudioState.Muted)
      or state.intent_stage is IntentStage.audio_decision):
    enabled, reason = False, "Receiver volume is unavailable while audio is off or unconfirmed."
elif (state.cleanup_pending or _cleanup_uncertain(state)
      or state.phase in (SessionPhase.Stopping, SessionPhase.CleanupRequired)):
    enabled, reason = False, "Wait for cleanup before changing receiver volume."
elif _trust_uncertain(state):
    enabled, reason = False, "Resolve receiver trust before changing its volume."
elif state.phase not in (SessionPhase.Mirroring, SessionPhase.VirtualDisplay):
    enabled, reason = False, "Start a stream to change receiver volume."
else:
    enabled, reason = True, "Set this receiver's volume; 0% mutes it."
```

Here selected means the pane has a selected receiver id, including a saved offline selection;
MainWindow's no-selection renderer applies the first two reasons using the current report.
No additional trust permission is inferred; use existing mute constraints. Pairing, revocation,
cleanup, missing phase or stream can never enable volume. The precedence above governs copy
when several reasons apply, while the enable result requires **all** conditions.

| File | Readout case | Exact text |
| --- | --- | --- |
| `src/airplay_linux/ui/state.py::project` | e known positive | decimal + `"%"`, e.g. `"30%"` |
| same | e=0 | `"Mute"` |
| same | e unknown, no selection, or fresh replacement | `"—"` (U+2014) |
| same | pending/refused/disabled/audio loss with historical known e | retain last acknowledged text; enablement/reason explains unavailability; never render pending desired as applied |
| same | successful level but save failed | keep applied numeric/Mute text; error/notice exactly `"Applied, but could not be saved."` |

File: `src/airplay_linux/ui/window.py::MainWindow._set_mute_affordance`. Keep existing icons and
accessible names `"Mute receiver audio"` / `"Unmute receiver audio"`; capability-derived tooltip:

| Kind/state | Exact tooltip |
| --- | --- |
| capable, audible | `Mute this receiver's audio for this session. Unmute restores its last audible level.` |
| capable, muted | `Audio is muted for this receiver. Activate to restore its last audible level.` |
| legacy, audible | `Mute this receiver's audio for this session only; the receiver keeps its own volume setting.` |
| legacy, muted | `Audio is muted for this receiver. Activate to unmute; muting is receiver-local and lasts for this session only.` |

| File | Signal/handler | Contract |
| --- | --- | --- |
| `src/airplay_linux/ui/window.py` | `MainWindow.volume_requested = Signal(int)` | User slider valueChanged emits requested integer only while projected control enabled. No receiver/backend logic in widget. |
| `src/airplay_linux/ui/window.py` | `MainWindow._render_volume` | Hold QSignalBlocker for slider across setValue and related setup; set readout from projection. When unknown, leave a mechanically neutral thumb at 0, with accessible description `"Level unknown"`; the em dash is authoritative. Restore guards on exceptions. |
| `src/airplay_linux/ui/presenter.py` | `Presenter._on_volume_requested(level: int)` | Refuse while shutting down/disposed/no selection/disabled; validate VolumeLevel; capture selected id; schedule SetVolume via ManagerPort. No transport or preference writes from presenter. |
| `src/airplay_linux/ui/presenter.py` | `Presenter.start` | Connect volume signal once; rendering/record reload/receiver selection never dispatches SetVolume. |
| `src/airplay_linux/ui/state.py` | existing audio-decision action projection | With audio_close_pending/policy=false, offer video_only and Stop, suppress RetryAudio and slider/speaker. Keep existing legacy decision actions unchanged. |

File: `src/airplay_linux/ui/window.py::MainWindow.set_activity`. Use existing renderer of the
registered §4 messages/fields. Expected message/field content is fixed, e.g. applied has message
`"Receiver volume was applied."` and level=30; failure has
`"Receiver audio control change could not be confirmed."` and level=30/error_code=audio_volume_failed;
absence has `"Receiver volume control is unavailable."` and reason=backend_unsupported. Preserve
existing timestamps/receiver-id formatting. No raw response, RTSP value, credential, IP description
or exception text is inserted into a new activity line.

## 11. Test contract

The following are implementation requirements, not commands executed while drafting this spec.
Use existing test files; new named fixtures below live in their owning test modules. Keep old
fixtures to prove downgrade compatibility. Automated checks never run discovery, real receiver
I/O, installed application startup or live_backend tests for this feature.

### 11.1 Fixtures and assertions by layer

| Owning file | Fixture/test surface | Required assertions |
| --- | --- | --- |
| `tests/domain/test_contracts.py` | new records and existing frozen/type inventory | VolumeLevel accepts exact integers 0/1/100 and rejects bool, floats, strings and bounds; receipt absence differs from zero and applied requires level; SetVolume type validation; snapshot preference bounds 1..100 versus effective 0..100; CapabilityReport.volume exact bool with false default. |
| `tests/policy/test_capabilities.py` | existing offline EnvironmentProbe rigs | Proven target_volume alone supplies report.volume; absent/incoherent evidence stays false; no other capability, source, ownership, trust or start gate is promoted. Audio-unavailable reports still cannot enable UI mutation. |
| `tests/adapter_contracts/test_transport_contract.py` | existing TransportPort conformance harness | Accept new keyword and receipt return, typed level/close results, current-handle rejection and preserved legacy default behavior without invoking external control. |
| `tests/backend/test_contract.py` | `volume_help` fixture built from existing source-help and ctl fixtures, with exact §2 rows/tokens | Both accepted tables and unchanged profile; exact signatures; missing/malformed audio row; each audio/volume row missing its own scope, negating it or borrowing the other row's scope; absent token; token-only; sender/ctl half-installation; unchanged source-token rejection; completeness not borrowed. |
| `tests/backend/test_contract.py` | `volume_status` fixture | Absent/0/100 volume; all four states; unknown string; null, bool, string, float and bounds refused; applied requires level; old status and mute shapes still decode; optional latch strict when present; all aggregate/source/credential/ownership projection checks retained. |
| `tests/backend/test_control.py` | existing scripted process/profile fixtures plus capable feature opt-in | Both new argv tuples, connect flag positions, missing target, wrong payload, disabled feature, source/secret rejection; no fence means zero I/O; target replacement after awaited ownership check blocks spawn; post-I/O owner mismatch refuses; exact target receipt/close ok required. |
| `tests/process/test_ownership.py` | existing receipt/evidence fixtures | New operations admitted only by owned+multi_target+target_volume; no source/trust gate weakened; unauthorized operations do no mutation I/O. |
| `tests/transport/test_doubletake.py` | existing scripted BackendControl/registry/capture rigs, `volume_control` capable fixture | initial_volume to connect only when supplied/capable/enabled; capable audio off without level; legacy byte-identical argv; one existing status read yields AudioStartReceipt; missing capable receipt reaches volume refusal; live acknowledged integer; current fence/timeout; no e invention; live failure keeps video. |
| `tests/transport/test_doubletake.py` | `held_volume` fixture with separately controlled public timeout and actual task completion | Actual done callback emits one settlement after lock release; first public timeout does not permit a second worker; late ack cannot update mute/e; close bypasses volume lock and latches local policy; stale lease/daemon callbacks cannot affect replacement. |
| `tests/session/test_audio_lifecycle.py` | capable variants of existing mirror_rig/virtual_rig using real SessionManager dispatch | Every §8 row; slider/mute/unmute one knob; return-to-last-audible/remembered/100; only positive slider ack saves; save failure keeps previous r and applied e; no backend mute/unmute on capable streams. |
| `tests/session/test_audio_lifecycle.py` | `volume_drag` fixture: hold first task, submit several values, expire public waiter, then settle actual task | One worker+one latest slot, no work launched at timeout, only final desired value drains; timed-out ack has no publication/save, while a nonexpired active ack remains a valid intermediate fact; replaced pending values never dispatch/save. Count settlements, not just public completions; assert emitted activity directly. |
| `tests/session/test_audio_lifecycle.py` | `close_during_retry` fixture | Refused start→held Retry→video-only; policy=false is visible before close ack; late level success cannot open/confirm audio; refusal/timeout never publishes Disabled; repeated close and Stop work; ordinary Retry refused; reconnect and virtual replacement carry audio off before any capture. |
| `tests/session/test_audio_lifecycle.py` | zero/loss/recovery and stale-result matrices | All four mute representations agree; old operation/lease/endpoint/daemon results are silent; audio loss retires slots; replacement clears e/a; Stop preserves cleanup guarantees. |
| `tests/session/test_startup_recovery.py` | existing recovery metadata fixtures extended with receiver v1/v2 | No e/a/mute/task/latch receipt authority after reload; receiver preference reloaded only through continuity validation; bad volume warning does not block startup; identity corruption still blocks as today. |
| `tests/storage/test_atomic_records.py` | `receiver_v1` and `receiver_v2` exact byte fixtures | v1 read; v2 round-trip including null and boundaries; zero/wrong-type/out-of-range drop only volume and report once; structural corruption still refuses; other schemas reject v2; no credential reference; no transient new session fields. |
| `tests/storage/test_repositories.py` | `interleaved_preferences` fixture with Deferred read/write barriers | Concurrent resolution/volume writes both survive, both orderings; one writer instance/lock per receiver; independent receiver progress; post-await identity/PreferenceAccess refusal; volume save failure retains prior r. |
| `tests/ui/test_state_projection.py` | existing grid plus volume precedence matrix | Every §10 reason wins in declared order; trust/cleanup always block; refused slider disabled; readout unknown/zero/positive/historical; save warning and capability tooltips. |
| `tests/ui/test_dashboard_widgets.py` | existing offscreen qtbot fixtures | Exact object names/order/ranges; visible disabled row; accessible names; arrows/page/Home/End; QSignalBlocker prevents render feedback even on exception; readout not requested thumb value. |
| `tests/ui/test_presenter.py` | existing PresenterRig | Only typed SetVolume for captured selected receiver; disabled signal refuses; no render feedback; start uses validated saved preference only when capable; post-save cache reload cannot overwrite newer record or another receiver. |
| `tests/diagnostics/test_safety.py` | existing adversarial/positive matrices | New codes fixed and registered; level 0..100 exact-int only; reason closed vocabulary; volume boolean normalized immutably; no caller hook or secret leak; assert activity events are actually emitted despite AudioLifecycle._emit swallowing sink failures. |
| `tests/testing/test_fakes.py` | existing FakeTransport/Deferred fixtures | New methods/initial parameter, exact-handle/target/daemon fencing; level bookkeeping; latch; one settlement per volume call; scripted Ok(None) start normalized to legacy receipt, never fabricated volume. |

File: `src/airplay_linux/testing/adapters.py::FakeTransport`. Default start_audio success becomes
Ok(AudioStartReceipt(audio=True)); a scripted Ok(None) normalizes identically. A supplied receipt
passes through. Queue set_volume/close_audio through existing scripted mechanism, journal typed
arguments, update acknowledged fake state only on success, and preserve same settlement signal
semantics under Deferred. Keep legacy default capability false in existing rigs; capable tests
opt in. Update the test-local start_stream overrides wherever the added keyword is forwarded.
File: `src/airplay_linux/session/testing.py::SessionRig`: construct one preference writer over
its existing fake receiver store/discovery and supply it in ManagerDeps.

### 11.2 Carried Go proof surface

File: `tests/backend/test_carried_source_patch.py`. Add
`test_target_volume_patch_executes_carried_contract`; compile **exact postimage declarations**
from `vendor/doubletake/0004-target-volume.patch` in disposable standard-library-only modules.
Do not translate the state machine into a Python or test-only Go implementation. Patch production
helpers must have fake writer/capture/time seams that this proof can compile. Existing upstream
file `internal/airplay/mirror_setup_test.go::TestAudioVolumeBody` must change from its boolean
signature and expected format labels to integer cases; include that change in patch 0004.

| Actual carried declarations/files exercised | Required proof |
| --- | --- |
| `internal/airplay/mirror.go::audioVolumeBody` | Exact 0/1/50/99/100 bytes, negative/over-100 clamp, monotonic all 1..100; endpoints never negative zero. |
| `internal/airplay/mirror.go::AirPlayClient.setupMirrorSession` factored volume decision | All four §2.3 combinations; two requested writes instead of max; explicit zero; nil old path; audio off suppresses both writes and capture. |
| `internal/airplay/client.go` bounded helpers with fake connection | Complete 200/rejection/incomplete header/incomplete body/withheld response; actual connection unusability; single deadline across blocked mutex admission, write, response and Digest; expired admission sends nothing. |
| `internal/daemon/daemon.go::Daemon.handleVolume`, `runVolume`, `statusResponseLocked` | Actual target selection across two entries; one worker/latest slot; refusal retains old percent; retry applied opens withheld audio; JSON absent vs zero vs refused; no echo without 200. Drive admission, the worker and the real status serialiser for these three traces and feed each produced status into the composed recovery test: (a) acknowledge 30 then reject 40 with nothing queued → `applied`/30, (b) reject the very first live write on an unrequested startup → `unrequested` with no `volume`, (c) reject with a newer value queued → `pending`, then `applied` after the queued write succeeds. Assert `applied` is never serialised without `volume`, and that `pending` never survives a quiescent worker. |
| `internal/daemon/daemon.go::Daemon.handleAudioOff`, `maybeStartAudio`, detach helpers | Close before/after held level ack; startup already in progress; ack only after quiescence; Stop/replacement late capture/volume completion cannot touch new entry; one-way latch; no global video cancel. Inspect the serialised status **after** the retiring worker and after a close-during-write, asserting the §2.4.1 recomputation: never `pending` when nothing remains; `applied` only with a percent present; and, for a closed stream, the exact terminal value from **both** a wholly refused startup and a partially acknowledged one (`applied`/percent or `unrequested`/absent, never `refused`), including close during a held Retry followed by late settlement. |
| `cmd/doubletake-ctl/main.go::parseConnectArgs`, command parser | on/off and level at documented positions, explicit 0; invalid/missing audio, invalid levels, duplicate/out-of-order/unknown flags, no flag as PIN; existing connect with no flags yields exactly today's request/argv. |
| `internal/daemon/daemon.go` socket request decoder used by `handleConn` | Raw JSON cases through the **actual** decoder, not a re-implementation: absent vs explicit `null` vs `false`/`0`; wrong types; fractional and out-of-range levels; overflow; forbidden members (including an explicitly present `port:0` and `pin:""`). Each rejection must occur before stream publication, latch mutation or worker creation, and legacy request shapes must still decode as today. Feasible offline with `net.Pipe` or byte readers. |

Factor necessary production setup/dispatch/audio-owner decisions into dependency-light helpers
**inside those existing files**. The extraction proof must include their real call sites or
assertions tying extracted decisions to those call sites; detached unused helper tests are not
proof of production behavior. Capture and RTSP are fakes, never GStreamer/native receivers.
Use in-memory pipes/fake net.Conn, with no TCP/UDP listener. Run Go with GOPROXY=off,
GOSUMDB=off, GOTOOLCHAIN=local, GOWORK=off, CGO_ENABLED=0; missing Go is actionable pytest failure,
not skip/pass. Preserve the existing independent Git patch-parser proof and source producer proof.
The existing mandatory-Go prerequisite proof must continue to distinguish missing Go from Git.

### 11.3 Composed receipt test

File: `tests/session/test_audio_lifecycle.py`. Add
`test_manager_transport_control_start_volume_receipt`, parameterized over applied-positive,
applied-zero, refused, pending, missing and legacy. Assemble real SessionManager handlers and
real DoubleTakeTransport over the existing scripted control/daemon, discovery/capture/evidence
fakes and fake clock. Dispatch Start(volume=30), let connect acknowledge connecting, release
streaming/source readiness, then feed the existing start_audio status read. Assert:

1. Exact capable/legacy connect payload and argv; no extra status poll owner.
2. AudioStartReceipt reaches actor state: §8 table, correct e/a/mute, explicit decision on
   refusal/pending/missing, no earlier audio_recovered or optimistic Enabled.
3. Refused→RetryAudio sends volume 30 on the same handle and opens audio on acknowledged result.
4. Stop/replacement before receipt completion suppresses every late state/diagnostic/save.

Two further composed tests are required, both driven through the real `SessionManager` and the
real transport, because each one is invisible to handler-only tests:

- `test_live_volume_rejection_never_becomes_a_startup_refusal`. Start with **no** remembered level
  (so `initial_volume=None`), accept a slider acknowledgement at 30, then have the receiver
  completely reject a slider change to 40, then lose audio, then choose Retry audio. Assert the
  receipt is read as a *live* failure: `effective` stays 30, the daemon's `volume_state` is not
  `refused`, Retry dispatches on the same handle, and recovery reaches Enabled at 30. A start
  *with* a remembered level repeats the same trace and must not require the retried level to equal
  `initial_volume`. Repeat the trace once with the produced `pending` status (a newer value still
  queued) and once with a **partially acknowledged** startup: first setup write acknowledged, second
  refused, then a rejected Retry — the state must stay `refused` and Retry must still recover.
- `test_foreign_audio_close_is_never_an_enabling_receipt`. With the application's local policy
  still enabled, have the scripted daemon report a matching entry with `audio_closed: true`, then
  drive a recovery read. Assert the transport returns `audio_unavailable` (never an enabling
  receipt), that the actor does not emit `audio_recovered`, does not present `Disabled` as if it
  had confirmed quiescence, and keeps video.
- `test_repeated_retry_keeps_the_occupied_worker_fence` — the capable variant of
  `tests/session/test_audio_lifecycle.py::test_stale_audio_retry_completion_cannot_overwrite_a_newer_retry`.
  From a refused startup: hold the Retry, expire its public wait, request Retry again while the
  worker is still occupied, then settle the original worker. Assert exactly one subsequent
  dispatch, that the original settlement was accepted rather than discarded as stale, that the
  desired slot drained, that no second worker exists, and that the result recovers audio. Also
  cover a complete rejection returning the actor to a usable `audio_decision` state.

The applied-zero case uses explicit StartRequest.volume=VolumeLevel(value=0), not a stored zero.
Keep this composed proof separate from the fake-only authority matrix; both are required.

### 11.4 Existing tests/inventories that must be updated

Update exact inventories and parameterizations; preserve legacy assertions by adding capable
cases, never replacing old fixtures with only the extended surface.

| File | Existing test(s)/inventory | Required update |
| --- | --- | --- |
| `tests/domain/test_contracts.py` | `test_serialized_enums_round_trip`, `test_typed_result_and_public_event_unions_do_not_need_raw_backend_messages`, `test_protocol_boundaries_exist_without_instantiating_adapters`, `test_idle_snapshot_has_no_acquired_resources_or_pending_intent` | New error/event/command/payload/port members and safe snapshot defaults. |
| `tests/backend/test_control.py` | `test_application_contract_fake_exercises_every_typed_operation`, `test_application_contract_capability_denial_is_fail_closed`, `test_profile_maps_only_explicit_target_scoped_published_commands`, `test_profile_refuses_unqualified_destructive_controls`, `test_fixture_mutations_still_require_owner_handshake`, dispatch-fence parameterizations | Add operations/payloads/fence cases; default legacy published mappings remain the same. |
| `tests/environment/test_installed_contracts.py` | `PUBLISHED_OPERATIONS`, `test_profile_maps_exactly_the_documented_operations`, `test_installed_help_surface_proves_no_application_owned_gates` | Keep old operation set; derive extra set only for proven target_volume; do not infer private gates. No live gate weakened. |
| `tests/fixtures/backend/contract-v1.json` | requests/replies/features inventory | Add volume/audio_off typed fixture requests and status_reply tags, target_volume=false by default. Explicit non-live provenance unchanged. |
| `tests/backend/test_contract.py` | `test_application_contract_fixture_is_explicitly_non_live_and_has_conformance_messages`, `test_patched_surface_with_advertisement_reports_source_reporting_only`, `test_advertisement_cannot_promote_an_unpatched_surface` | Include new fixture operation inventory; preserve existing source-only case; add target-volume incoherence cases. |
| `tests/backend/test_carried_source_patch.py` | `test_carried_patch_chain_is_parseable`, `test_complete_token_requires_complete_producer`, `test_producer_proof_reports_missing_go_with_git_available` | Fourth patch parses; preserve source proof on 0001–0003; new proof has same mandatory offline Go behavior. |
| `tests/transport/test_doubletake.py` | `test_connect_records_audio_intent_and_only_separate_audio_start_enables_it`, `test_stream_reporting_audio_confirms_audio_without_a_backend_operation`, `test_stream_reporting_no_audio_keeps_the_typed_refusal`, `test_audio_start_refusal_preserves_its_failure_event` | Receipt return type and capable event ordering; old legacy outcomes remain asserted. |
| `tests/transport/test_doubletake.py` | `test_legacy_control_without_a_fence_is_rejected_before_any_call`, `test_fence_dropping_wrapper_cannot_dispatch_a_mutation`, `test_replacement_during_ownership_revalidation_never_dispatches_a_mutation` | Include both new fenced operations. |
| `tests/session/test_audio_lifecycle.py` | `test_default_start_probes_and_confirms_enabled_audio_without_touching_video`, `test_retry_restores_the_desired_muted_state`, `test_explicit_video_only_supersedes_an_inflight_audio_retry`, `test_start_video_only_after_ready_keeps_the_established_video`, `test_a_replacement_stream_is_not_reported_as_muted` | Retain legacy branch; add volume receipt/zero/latch/replacement branch. |
| `tests/storage/test_repositories.py` | `test_legacy_store_roundtrip_is_byte_identical_while_new_types_are_strict` | Receiver writes intentionally v2; v1 still readable, other legacy record byte checks remain unchanged. |
| `tests/storage/test_atomic_records.py` | `test_receiver_record_round_trips_its_non_secret_preferences`, `test_an_unknown_record_version_is_rejected`, `test_the_receiver_store_fails_closed_on_a_corrupt_sibling`, `test_preference_access_withholds_an_unjustified_saved_record` | v2 bytes, schema-specific versions, droppable volume exception only; identity corruption/continuity behavior preserved. |
| `tests/ui/test_dashboard_widgets.py` | `test_mute_toggle_is_labelled_and_emits_the_requested_state`, `test_keyboard_navigation_reaches_every_enabled_command`, `test_choice_controls_explain_themselves` | Both capability tooltip variants, volume keyboard/accessibility and no feedback. |
| `tests/ui/test_presenter.py` | `COMMAND_TYPES`, `test_only_typed_domain_commands_reach_the_manager`, `test_start_dispatches_a_typed_request_for_the_selected_mode`, `test_audio_actions_affect_only_the_selected_receiver` | Add SetVolume and capable start preference; preserve ManagerPort-only dispatch. |
| `tests/testing/test_fakes.py` | `test_scripted_results_cover_every_adapter_method`, `test_default_fakes_report_verified_success_and_unknown_mode_evidence`, `test_fakes_satisfy_their_domain_protocols` | New methods, keyword and AudioStartReceipt normalization. |
| `tests/diagnostics/test_safety.py` | `test_allowlist_matches_the_frozen_field_permit`, `test_every_error_code_is_pinned_or_deliberately_unpinned`, `test_export_includes_counts_and_capabilities_only`, capability snapshot positive/adversarial inventories | Level/reason fields, fixed codes, seventh capability flag with unchanged immutable boundary discipline. |

Before implementation completion run the relevant domain/backend/control/transport/session/
storage/UI/diagnostics/policy/fake tests and existing full offline regression suite. Required
Go assertions execute from pytest's carried-code proof. Compile/test a freshly patched backend
only in an implementer's disposable checkout, never by editing the read-only reference. Delivery
records in `docs/backend-fork.md`, `docs/backend-compatibility.md` and `docs/operations.md` must
then document patch digest/base/toolchain/build receipts, four tokens, new behavior, disabled
legacy row, acknowledgement limits and pairwise rollback. No installation is implied by this spec.

## 12. Decisions this spec makes

- **Volume bytes:** clamp in the library and use six fixed decimal places, retaining exact legacy
  endpoint bodies; intermediate mapping is 20·log10(percent/100).
- **Timeouts:** requested setup's two writes share 2 seconds; each live volume dispatch has one
  2-second RTSP deadline; a close handler has 2 seconds for quiescence. Reuse the existing
  5-second Python control wait. Connection-mutex admission polls with waits of at most 1 ms.
- **JSON and receipts:** request keys are audio (optional bool) and volume (optional int); stream
  keys are volume, volume_state and audio_closed. No aggregate volume or connect receipt field;
  successful live commands return full status, and only a successful close call proves quiescence.
- **Setup ordering:** retain both writes; requested startup is applied only when both final
  responses are 200. Any complete rejection withholds capture; any incomplete/expired requested
  exchange fails setup. Record acknowledged percent separately even if the other write refuses.
- **Ctl scanner:** fixed target→audio flag→volume flag→credential order; omitted target allowed;
  positional numbers retain target/credential meaning. Accept one to three ASCII digits including
  leading zeroes; reject repeats, equals forms, unknown/out-of-order flags and flag-like credentials
  with the fixed §3 errors.
- **Settlement:** the actual volume task's done callback emits volume_result once for every
  actor-admitted call, including transport-preflight refusal; public timeout cannot drain the
  slot. Expired/obsolete results cannot publish or save. A pending desired slot does not invalidate
  the current worker's acknowledged intermediate level; replaced pending slots never dispatch.
- **Closure ordering:** latch policy and retire desired levels at admission; dispatch closure
  independently of the volume lock; acknowledge only after registered capture/start work quiesces.
  A timed-out close requires a fresh confirmed close before Disabled can be published.
- **Preference writing:** one shared per-receiver locked writer in storage/repositories.py returns
  the merged SavedReceiver; positive slider acknowledgements alone request volume persistence.
  Receiver writes use v2, v1 reads remain strict, and malformed new leaves alone are droppable.
- **Diagnostics and UI details:** use the four fixed new error tokens and three activity codes in
  §4; unknown level uses an em dash and neutral thumb; retain acknowledged readout during pending
  work; cache reloads coalesce after fresh snapshots; exact reason/tooltips/object names are in §10.
