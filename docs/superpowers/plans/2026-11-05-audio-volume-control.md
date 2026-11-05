# Audio Volume Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax; leave them unticked during execution.

**Goal:** Add acknowledged, receiver-local volume with durable positive preferences, safe startup, return-to-last-audible mute, and an audio-close latch, while preserving the legacy backend contract.

**Architecture:** Carry the Go producer as patch 0004, then introduce the Python domain and recognition seams before their consumers. One shared preference writer and the existing per-receiver session actor own durable preferences and transient audio authority respectively; transport task retirement, rather than public timeouts, controls draining. All tasks share the single `audio-volume` lane because later tasks consume earlier contracts; installation is a separately authorized operator action with its own receipt.

**Tech Stack:** Python >=3.12, frozen keyword-only dataclasses, asyncio, pytest/pytest-asyncio/pytest-qt, PySide6/qasync, Go >=1.25 from `/home/henry-arch/.local/go`, the pinned doubletake tree and cached native GStreamer dependencies.

**Authority:** Read `docs/superpowers/specs/2026-11-05-audio-volume-control-design.md` revision 8 and `docs/superpowers/specs/2026-11-05-audio-volume-control-spec.md` in full before implementing a task. The approved design wins on conflicts. Section numbers below refer to the implementation specification unless prefixed `design`.

**File structure:** Patch 0004 contains all Go changes, inside the existing upstream files. Python additions stay in the existing modules named in the spec: resources/records/messages/ports for contracts, backend/contract for proof, backend/control and process/ownership for dispatch, transport/doubletake for exact-lease I/O, storage/codecs and repositories for preferences, session lifecycles for actor authority, and ui/state/presenter/window for presentation. Tests extend the existing owning modules, with local fixtures rather than another fixture package. The three delivery documents own instructions and receipts; no new persistent receipt schema is introduced.

**Coverage:** §§2–3 and §11.2 → Task 1; §4 → Task 2; §5 and capability production → Task 3; §9.1 → Task 4; §9.2/composition → Task 5; §6 → Task 6; §7 and fake parity → Task 7; §8.4 → Task 8; §§8.1–8.3 → Task 9; §10 → Task 10; §§11.1/11.3/11.4 and full offline regression → Task 11; design §§9–11 and the delivery documentation contract → Tasks 12–13. Exact inventory updates belong to the producing task and are checked again in Task 11.

**Modification locators:** File line ranges below refer to the inspected integration base and, for upstream paths, the read-only 0001–0003 postimage. Earlier tasks can shift these lines; use the named declaration in the same range as the edit anchor. An upstream `Modify` is an edit in an implementer's disposable clone whose diff is carried in patch 0004, never an edit in the reference directory.

## Global Constraints

- Authority is `docs/superpowers/specs/2026-11-05-audio-volume-control-design.md` revision 8 plus `docs/superpowers/specs/2026-11-05-audio-volume-control-spec.md`; implement every requirement, without adding remote readback, sender flags, a profile string, audio-on, discovery changes, display changes or a status-polling owner.
- Compatibility rule: without proven `target_volume`, preserve today's connect argv, audio receipts' meaning, mute/unmute, decision/retry behavior, profiles, source checks, trust gates, ownership proofs, cleanup and timers.
- All automated verification is offline; no AirPlay receiver, real discovery, installed application startup, real portal/capture, live_backend, live_kde or live_hardware invocation is admitted.
- Never execute a power, sleep, reboot, halt or kexec command; never manipulate the display manager, graphical.target, desktop session or the process tree hosting PM.
- `/tmp/delivery-reconcile.GMO4Nx/reapply` is read-only reference material: never build, run, modify or use it as a mutable worktree.
- Author `vendor/doubletake/0004-target-volume.patch` against a fresh local clone checked out at `c896e3689ab85c3ed520e502308dedfbb5a4c221`, after applying 0001, 0002 and 0003 in that order; check every application with `git apply --check --whitespace=error-all`.
- `/tmp/doubletake-pr76-audit.G2sjMn` contains the pinned upstream object and is a read-only local clone source; use `git clone --no-hardlinks --no-checkout` from it without fetch, pull or network access, and record both the supplied base object and its peeled commit.
- Full backend compilation uses `PATH=/home/henry-arch/.local/go/bin:$PATH`, `CGO_ENABLED=1`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`, `GOPATH=/tmp/gopath`, `GOMODCACHE=/tmp/gopath/pkg/mod`, `GOFLAGS=-mod=readonly`.
- The mandatory extracted Go proof uses exact carried declarations, standard library only, fake connections/capture, `CGO_ENABLED=0`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`, `GOFLAGS=`; missing Go must fail pytest with actionable instructions, and Git proof stays independent.
- Execute pytest through `/tmp/airplay-volume-venv/bin/python`; Task 1 provisions this venv from local installed dependencies, and reinstall the current repo with `-m pip install --no-index --no-build-isolation --no-deps .` before each task's tests.
- Every pytest command inherits `PATH=/home/henry-arch/.local/go/bin:$PATH`, `QT_QPA_PLATFORM=offscreen`, `AIRPLAY_LINUX_LIVE_BACKEND=0`, `AIRPLAY_LINUX_LIVE_KDE=0`, `AIRPLAY_LINUX_LIVE_HARDWARE=0`, `PIP_NO_INDEX=1`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`; apply these exact exports in each fresh implementer shell.
- Keep existing offline tripwires in `tests/conftest.py` enabled; use short disposable `/tmp/av-*` pytest basetemps so private AF_UNIX fixtures remain within their path bound.
- Requested setup's two RTSP writes share 2 seconds total; a live volume write gets 2 seconds at dispatch including mutex admission, framing, response and Digest retry; close waits 2 seconds total; Python CONTROL_TIMEOUT=5.0, SOURCE_TIMEOUT=5.0 and CONNECT_STREAMING_TIMEOUT=20.0 remain unchanged.
- Only final RTSP 200 acknowledges a level; complete rejection preserves the usable connection and withholds unadmitted startup audio, whereas an incomplete or expired requested setup fails setup using existing cleanup.
- A public timeout is not worker settlement; each actor-admitted valid typed volume call emits one volume_result after its actual task is done and its stream lock released, including transport-preflight failure.
- The audio gate is a per-stream one-way latch; apply policy=false at video-only admission and publish Disabled only after a current confirmed close; reconnect/replacement uses current policy even while old closure is unconfirmed.
- Remembered volume is an exact int 1..100 or absent; effective is an exact int 0..100 or unknown; only acknowledged positive slider changes request a durable preference write, and saved zero is forbidden.
- No secret or raw backend/exception text reaches argv, metadata, diagnostics or UI; use the exact four catalog codes, three activity codes, integer bounds, fixed reasons and copy in §§4/10.
- Session schema remains v1; only receiver documents write v2 and accept v1/v2; malformed volume alone is droppable, while identity/structure corruption remains fail-closed and bytes stay on disk.
- Every task is in `audio-volume` and executes in numbered order; consume the exact signatures repeated in its Interfaces block, retain legacy test fixtures and opt into target_volume explicitly in new capable fixtures.
- Implementation and offline verification do not authorize installation; Task 13 prepares and documents the two-binary delivery, and only an explicitly authorized operator executes its installation/rollback blocks; never report an unexecuted installation as installed.
- Delivery stays local, with no remote operation; these implementation tasks do not merge or alter another checkout.

## Task 1: Carry the bounded volume producer, stream latch and offline Go proof

**Implementer tier:** Capable
**Lane:** audio-volume

**Files:**

- Create: `vendor/doubletake/0004-target-volume.patch`.
- Modify through patch only: `internal/airplay/client.go:263-317,338-385,941-1168,1320-1337` (all listed upstream modifications are inside the disposable clone and carried in patch 0004).
- Modify through patch only: `internal/airplay/mirror.go:206-237,463-475,665-692,1115-1249,2407-2439`.
- Modify through patch only: `internal/airplay/mirror_setup_test.go:984-996`.
- Modify through patch only: `internal/daemon/daemon.go:43-83,207-329,527-565,604-665,715-822,861-1315,1572-1775`.
- Modify through patch only: `internal/daemon/daemonclient/client.go:43-94`, `cmd/doubletake-ctl/main.go:1-98`, `cmd/doubletake/main.go:60-94`.
- Test/Modify: `tests/backend/test_carried_source_patch.py:1-216` (append the new proof and extend the missing-tool control; preserve the old producer proof).

**Interfaces:**

- Consumes: existing `func (c *AirPlayClient) rtspRequest(method, uri, contentType string, body []byte, extraHeaders map[string]string) ([]byte, map[string]string, error)`, `digestRetryHeader`, `preemptiveAuthHeader`, `markIncompleteResponse`, `hasIncompleteResponse`, `HTTPStatusError{StatusCode int, Body []byte}` and `Daemon.streamWorkers sync.WaitGroup` from the pinned postimage.
- Produces: `func audioVolumeBody(percent int) []byte`; `func (s *MirrorSession) SetAudioLevel(percent int) error`; unchanged `func (s *MirrorSession) SetAudioMuted(muted bool) error`; `func (s *MirrorSession) InitialAudioLevel() (*int, string)`.
- Produces: `StreamConfig.InitialVolume *int`, `StreamConfig.AudioEnabled *bool`; `func (c *AirPlayClient) rtspRequestUntil(method, uri, contentType string, body []byte, extraHeaders map[string]string, deadline time.Time) ([]byte, map[string]string, error)` and the identically typed `rtspRequestOnceUntil` which already owns `c.mu`.
- Produces: `func (c *AirPlayClient) readPlaintextHTTPResponse(statusOut ...*int) ([]byte, map[string]string, error)` and `readEncryptedHTTPResponse(statusOut ...*int)` with the same return types; old call sites omit the optional pointer.
- Produces: `Request.Audio *bool`, `Request.Volume *int`; `StreamInfo.Volume *int`, `VolumeState string`, `AudioClosed bool`; `func (d *Daemon) handleVolume(req Request) Response`, `runVolume(entry *activeStream)`, `handleAudioOff(req Request) Response`, `maybeStartAudio(entry *activeStream)`.
- Produces: `func parseConnectArgs(args []string) (daemon.Request, error)`; unchanged `func (c *Client) Connect(target string, port int, pin string) (*daemon.Response, error)` plus `ConnectWithAudio(target string, port int, pin string, audio *bool, volume *int) (*daemon.Response, error)`, `Volume(percent int, target string) (*daemon.Response, error)`, `AudioOff(target string) (*daemon.Response, error)`.
- Produces: mandatory pytest `test_target_volume_patch_executes_carried_contract(tmp_path) -> None`, including a module-local `carried_volume_status_traces() -> tuple[dict[str, object], ...]` that executes/cache-reuses the real extracted Go serializer in a TemporaryDirectory and returns its emitted recovery trace envelopes for the composed tests in Task 11; it is test evidence, not another production protocol.

- [ ] Provision the offline Python test venv and verify its dependencies without starting the application.

```bash
export PATH="/home/henry-arch/.local/go/bin:$PATH"
export QT_QPA_PLATFORM=offscreen AIRPLAY_LINUX_LIVE_BACKEND=0 AIRPLAY_LINUX_LIVE_KDE=0 AIRPLAY_LINUX_LIVE_HARDWARE=0 PIP_NO_INDEX=1 GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off
python3 -m venv --system-site-packages /tmp/airplay-volume-venv
/tmp/airplay-volume-venv/bin/python -c 'from pathlib import Path; import sysconfig; Path(sysconfig.get_path("purelib"), "offline-airplay-dependencies.pth").write_text("/home/henry-arch/.venvs/airplay-linux/lib/python3.14/site-packages\n", encoding="utf-8")'
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -c 'from importlib.util import find_spec; assert all(find_spec(name) is not None for name in ("PySide6", "qasync", "zeroconf", "dbus_next", "pytest", "pytest_asyncio", "pytestqt")); print("offline test dependencies available")'
```

Expected: pip installs this repository successfully from its local path; the last command prints `offline test dependencies available`. The `.pth` supplies local dependencies only; the installed current package and pytest's `src` path supply the implementation. Missing local prerequisites are actionable failures, not permission to download packages.

- [ ] Add the named pytest proof and its production-declaration extractor before creating patch 0004.

Use the existing source-proof style with postimage extraction, complete declaration boundaries and explicit file sections. Start the new proof with this assertion so absence is a behaviorally relevant RED:

```python
def test_target_volume_patch_executes_carried_contract(tmp_path):
    patch = PATCHES / "0004-target-volume.patch"
    assert patch.is_file(), "target-volume producer patch is required"
    traces = carried_volume_status_traces()
    assert {item["trace"] for item in traces} >= {
        "ack30_reject40", "unrequested_reject_first", "queued_pending",
        "queued_applied", "closed_refused", "closed_partial",
    }
    for item in traces:
        for stream in item["status"]["streams"]:
            assert stream["volume_state"] != "applied" or "volume" in stream
            if item["quiescent"]:
                assert stream["volume_state"] != "pending"
```

Implement `carried_volume_status_traces` in this same test module using exact extracted production declarations, the Go assertions below and machine-readable `TRACE_JSON=` lines. Its test modules may stub external boundary types, RTSP/capture/time dependencies, encryption primitives and logging; they must execute production decision/decoder/handler/serializer bodies, rather than copies of those algorithms. Assert the real setup, worker, decoder and audio-owner call sites invoke the extracted helpers. Preserve the existing mandatory-Go message, and extend `test_producer_proof_reports_missing_go_with_git_available` to select the new proof too: expected child summary becomes `2 failed, 1 passed`, both Go failures actionable and the Git parser passing.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_carried_source_patch.py::test_target_volume_patch_executes_carried_contract --basetemp=/tmp/av-go-red`.
Expected: exit 1 with `target-volume producer patch is required`; no receiver/process launch.

- [ ] Prepare a fresh offline upstream clone and commit the exact 0001–0003 baseline inside it.

```bash
volume_repo="$PWD"
volume_work=$(mktemp -d /tmp/av-backend.XXXXXX)
git clone --no-hardlinks --no-checkout /tmp/doubletake-pr76-audit.G2sjMn "$volume_work/source"
git -C "$volume_work/source" checkout --detach c896e3689ab85c3ed520e502308dedfbb5a4c221
git -C "$volume_work/source" rev-parse 'c896e3689ab85c3ed520e502308dedfbb5a4c221^{commit}'
for volume_patch in 0001-capture-report-selected-source.patch 0002-status-report-selected-source.patch 0003-capability-tokens.patch; do
    git -C "$volume_work/source" apply --check --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
    git -C "$volume_work/source" apply --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
done
git -C "$volume_work/source" add internal/airplay internal/daemon cmd/doubletake
git -C "$volume_work/source" -c user.name='AirPlay patch builder' -c user.email='airplay-patch@localhost' commit -m 'Apply carried source-reporting baseline'
```

Expected: checkout and all three patch checks exit 0; baseline commit contains only carried 0001–0003. The supplied c896 object is an annotated upstream object: record `rev-parse ...^{commit}` as observed, rather than inventing or substituting another base. Neither local source repository nor read-only reference is altered.

- [ ] Implement the percent mapping, immutable startup receipt and bounded RTSP sibling in the disposable upstream files.

Use this exact mapping:

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

Add `InitialVolume *int` and `AudioEnabled *bool` to StreamConfig, copy pointed-to inputs before asynchronous work, and apply `cfg.NoAudio = cfg.NoAudio || (cfg.AudioEnabled != nil && !*cfg.AudioEnabled)` before the first compatibility/setup decision. Preserve `SetAudioMuted`'s signature, existing failure messages and old `rtspRequest` timer, selecting integer 0/100 without calling the short-timer wrapper.

`rtspRequestUntil` acquires `c.mu` with TryLock and deadline-aware waits <=1 ms, with no Lock goroutine left behind. Under the mutex it rechecks deadline/conn, attaches existing preemptive Digest and runs at most one permitted Digest retry. `rtspRequestOnceUntil` uses the existing audio/session URI, complete framing, encryption/nonces and body bounds; it sets read and write deadlines to the same supplied time, writes the complete frame and calls the plaintext/encrypted reader directly with a status output pointer. Add the optional variadic pointer to those real readers; preserve all legacy 2xx behavior for calls without it. Consume a complete 201/204 body but return HTTPStatusError; only 200 succeeds. Incomplete write/header/body closes `c.conn` under the mutex without recursive locking or replacing it with nil. Unsent expired admission sends nothing and does not poison the connection. Clear both deadlines before unlock; a clearing failure also closes it.

Both requested setup writes share one `time.Now().Add(2*time.Second)` deadline and requested body. Record each final 200 as historical percent, but admit startup capture only if both final responses are 200. Complete rejection still performs the second required write if budget remains and yields refused; incomplete response or expiry of either required write fails setup immediately via controlConnectionUsable=false. Nil input uses the existing legacy two-write path and timers, with unrequested/no manufactured 100. Audio-off suppresses both writes. `InitialAudioLevel` returns a copy, `(nil,"unrequested")` for nil/absent state. Live `SetAudioLevel` creates its single two-second deadline once.

Run: `git -C "$volume_work/source" diff --check`.
Expected: exit 0, no whitespace errors; the exact new declarations and both setup call-site replacements appear in the diff.

- [ ] Implement the per-entry daemon volume worker, audio owner and close latch in the same disposable clone.

Add every activeStream field in §2.2 with the specified types: immutable initialVolume/initialAudio, acknowledged volume/state/readiness, one volumeDesired request, volumeRunning/Cancel/Seq/Done, audioClosed/Cancel/Starting/Capture/Done. `volumeRequest` contains percent, seq and a buffered result channel; results distinguish acknowledged/rejected/superseded/closed/expired. Guard fields and map identity with d.mu. Select exact original entries at admission, copy all pointer values, and initialize pending only for requested enabled audio. Credential-resume intent must be absent or equal; otherwise return exactly `connect intent cannot change while waiting for credentials`.

In connectAndStream copy entry intents into a local `cfg := d.mirrorStreamConfig()` and retain that config through the Digest/setup retry; never mutate d.cfg. Publish session/receipt and register workers only for the exact map entry. Register audio startup/cancellation/completion under d.mu before unlocking. Its one owner starts capture, rechecks original entry/session/latch/context, streams only if current, and on cancellation cancels its subordinate, calls capture.Stop before joining blocked StreamAudio, then closes audioDone. An obsolete startup stops its own returned capture once without publication. Register audio and volume workers in streamWorkers before shutdown can wait.

`handleVolume` validates before mutation, selects snapshot target(s), replaces the single desired slot and immediately completes a displaced waiter as superseded. One runVolume worker keeps running=true across I/O and unwinding; no per-handler RTSP worker or unbounded channel queue. Revalidate original entry/session/latch after I/O. A current 200 alone updates percent/audioMuted and readiness; existing admitted audio stays running after live failure. A recovery 200 can start withheld audio even with a newer desired value pending. A detached/latched result changes nothing. Each dispatch gets its own two-second library budget; waiters are bounded by remaining current plus one next write.

Recompute volume_state after every turn and closure, in this exact order:

```text
in-flight or desired slot -> pending
closed and quiescent with acknowledged percent -> applied/that percent
closed and quiescent without acknowledged percent -> unrequested/absent
open and audioLevelReady=false -> refused, retaining any partial acknowledged percent
admitted with acknowledged percent -> applied/that percent
admitted without acknowledged percent -> unrequested/absent
```

`handleAudioOff` accepts false only/no volume, includes connecting entries, latches/clears desired under mu before cancellation, then joins original audio owners outside mu with one two-second total deadline. It does not cancel video or wait behind the volume lock. Repeated close joins the same owner; false ok never proves quiescence. A late level/setup completion cannot reopen it. Revalidate original entries before a handler's full-status return; replacement returns `stream was replaced`. Add audioJoins/volumeJoins to daemonCleanup; cancellation/resource closure and joins run outside mu. Preserve deterministic status target ordering, source metadata, aggregate projection and legacy mute identity checks.

Run: `git -C "$volume_work/source" diff --check`.
Expected: exit 0; no d.mu-held RTSP/capture wait and no second capture.Stop owner in the changed code.

- [ ] Implement strict socket presence validation, client wrappers, CLI parsing and exact advertised surface.

Decode new-key raw presence in the actual handleConn decoder before ordinary Request unmarshalling: explicit null, bool-as-int, fractions, overflow and invalid ranges/types refuse before publication/latch/work. Detect forbidden `port:0` and `pin:""` by presence for new volume/audio commands. Keep legacy decoding for commands with neither new key. Use every exact error string in §3.2; no rejected text is interpolated except the existing selected-target-not-found pattern. New live success responses are full status envelopes; connect acknowledgement remains admission-only with no volume field.

Implement parseConnectArgs with TARGET→audio flag→volume flag→credential order, allowing omitted target/flags before it. One positional before flags is always target, `connect T 30` retains credential meaning, and decimal operands accept 1–3 ASCII digits/leading zeroes only. Reject repeats, equals forms, order errors, flag-like targets/credentials, missing/unknown values and extra positionals with §3.1's exact precedence/messages. Parse before daemonclient.New so invalid input produces no client or I/O; preserve the old I/O `error: %v` prefix only for actual client failures. Keep Connect as the unset wrapper, implement ConnectWithAudio/Volume/AudioOff and compact LF-terminated encoder requests.

Use these exact ctl rows in connect, volume, audio, pin order:

```text
  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)
  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)
  audio off [target]                  Close mirrored audio (all streams, or only the given IP)
```

Append only `target-volume` to the capability token loop; its stdout is exactly `capabilities-v1\nstatus-source\nstatus-source-complete\ntarget-volume\n`. Leave sender flags and all other ctl row text/order unchanged. Convert upstream TestAudioVolumeBody from bool/%t to int/%d cases and pin exact six-decimal bodies.

Run: `git -C "$volume_work/source" diff --check`.
Expected: exit 0; no sender flag addition and no audio-on wire handler.

- [ ] Generate patch 0004 from only the changed upstream files and verify it on a second fresh pinned clone.

```bash
git -C "$volume_work/source" diff --binary -- internal/airplay/client.go internal/airplay/mirror.go internal/airplay/mirror_setup_test.go internal/daemon/daemon.go internal/daemon/daemonclient/client.go cmd/doubletake-ctl/main.go cmd/doubletake/main.go > "$volume_repo/vendor/doubletake/0004-target-volume.patch"
git clone --no-hardlinks --no-checkout /tmp/doubletake-pr76-audit.G2sjMn "$volume_work/check"
git -C "$volume_work/check" checkout --detach c896e3689ab85c3ed520e502308dedfbb5a4c221
for volume_patch in 0001-capture-report-selected-source.patch 0002-status-report-selected-source.patch 0003-capability-tokens.patch 0004-target-volume.patch; do
    git -C "$volume_work/check" apply --check --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
    git -C "$volume_work/check" apply --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
done
```

Expected: four check/apply pairs exit 0; clean baseline plus carried patches reproduces the postimage. Do not edit patches 0001–0003 or substitute a diff against an unpatched tree.

- [ ] Finish the mandatory production-extraction Go probes for every backend row and run them through pytest.

The exact production bodies must be exercised for all these cases, using net.Pipe/fake net.Conn and fake capture/time, without TCP/UDP listeners or native capture:

| Probe | Exact expected observation |
| --- | --- |
| Mapping | 0=`volume: -144.000000\r\n`, 1=`volume: -40.000000\r\n`, 50=`volume: -6.020600\r\n`, 99=`volume: -0.087296\r\n`, 100=`volume: 0.000000\r\n`; negative/high clamp; monotonic 1..100; no negative zero. |
| Setup | Four §2.3 combinations; two requested bodies, zero silence, nil legacy max/unrequested, audio off no writes/capture; 200+rejection preserves percent but refuses readiness; later one live 200 recovers. |
| Bounded I/O | 200, 201, 204, complete refusal, incomplete headers/body, withheld response, partial write, locked admission, deadline-clear failure; actual poisoned connection closed; expired unsent admission sends zero bytes; one budget through Digest; no queued Lock goroutine. |
| Live worker | Two targets independently; latest slot supersedes waiters; no more than one worker/slot per entry; late detach/replacement/Stop cannot publish; live rejection never stops admitted producer. |
| Serializer recovery traces | ack30→reject40 settles applied/30; first live rejection after unrequested startup settles unrequested/absent; newer queued value gives pending then applied on its 200. Emit these actual envelopes as TRACE_JSON. |
| Closed terminal traces | Wholly refused startup closes to unrequested/absent; partial acknowledged startup closes to applied/percent; no quiescent pending, no closed refused, no applied without volume, including held Retry→close→late 200. |
| Audio owner | Both close/volume acknowledgement orders, capture startup already in progress, close timeout/repeated join, Stop and replacement with late capture return; close ok only after owner quiescence, one Stop owner, no video cancellation. |
| Parser | All valid omitted-target/on/off/zero/leading-zero/credential cases and every §3.1 fixed stderr/exit-1/empty-stdout case; parser rejection constructs no client. |
| Socket decoder | Actual handleConn decoder: absence/null/false/zero, wrong types/fractions/overflow/out-of-range and forbidden present port/pin; no state/worker mutation on refusal; legacy request shape preserved. |

Extend the missing-Go subprocess control without counting collection/import errors as proof. Execute each Go module with subprocess timeout=60, its own temporary directory, `go test -count=1 -v .`, and the exact extraction environment from Global Constraints. Assert emitted JSON, plus production call-site linkage, in Python.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_carried_source_patch.py --basetemp=/tmp/av-go-proof`.
Expected: exit 0, old Git/source producer proofs and new mandatory volume proof pass; missing-Go negative control reports the intentional child failures and passes its outer assertions. At this producer stage retain the new status envelopes for Task 11; their new-field Python decoder assertions are added after Task 3.

- [ ] Compile the freshly checked postimage with CGO and run only the receiver-free upstream mapping test.

```bash
cd "$volume_work/check"
export PATH="/home/henry-arch/.local/go/bin:$PATH" CGO_ENABLED=1 GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off GOPATH=/tmp/gopath GOMODCACHE=/tmp/gopath/pkg/mod GOFLAGS=-mod=readonly
go build ./...
go test -run '^$' ./...
go test -count=1 -run '^TestAudioVolumeBody$' ./internal/airplay
cd "$volume_repo"
```

Expected: all exit 0, all upstream packages compile, and TestAudioVolumeBody passes. `-run '^$'` compiles the other tests without running their native/network fixtures. Exhaustive behavioral proof is the mandatory in-memory extracted Go suite, not an assertion of live receiver acceptance.

- [ ] Commit the carried producer and its proof.

```bash
git add vendor/doubletake/0004-target-volume.patch tests/backend/test_carried_source_patch.py
git commit -m "feat(backend): carry acknowledged target volume and audio close"
```

Expected: one commit containing only these application-repository paths; disposable Go source stays outside the repository.

## Task 2: Define frozen domain contracts and safe diagnostics

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/domain/resources.py:91-121,219-242`.
- Modify: `src/airplay_linux/domain/records.py:181-251`.
- Modify: `src/airplay_linux/domain/messages.py:8-24,82-139,153-220`.
- Modify: `src/airplay_linux/domain/ports.py:28-60,114-141`.
- Modify: `src/airplay_linux/diagnostics/catalog.py:30-110,211-305`.
- Modify: `src/airplay_linux/diagnostics/activity.py:75-173,201-249,566-605`.
- Test/Modify: `tests/domain/test_contracts.py:1-317`, `tests/diagnostics/test_safety.py:1-4456` (new cases beside existing field/code/capability inventories).

**Interfaces:**

- Consumes: `Result[T] = Ok[T] | Err`, `Diagnostic`, `ReceiverId`, `ReceiverTarget`, `StreamHandle`, `OperationContext`, `AudioPolicy` from the existing direct-import domain modules.
- Produces: frozen keyword-only `VolumeLevel(value: int)`; `StartRequest.volume: VolumeLevel | None = None`; `CapabilityReport.volume: bool = False`.
- Produces: frozen keyword-only `AudioStartReceipt(audio: bool, volume_state: str | None = None, level: int | None = None)` in domain/records.py; frozen keyword-only `SetVolume(receiver_id: ReceiverId, level: VolumeLevel)` and event `VolumePayload(level: int, ok: bool)` in domain/messages.py.
- Produces: `TransportEventKind.volume_result = "volume_result"` immediately after mute_result and inclusion of SetVolume/VolumePayload in the closed unions.
- Produces: `TransportPort.start_stream(self, target: ReceiverTarget, source: CaptureSource | None, audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel | None = None) -> Result[StreamHandle]`, `start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]`, `set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]`, `close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]`.
- Produces: SessionSnapshot fields `remembered_volume: int | None`, `effective_volume: int | None`, `last_audible_volume: int | None`, `volume_state: str | None` default None and `audio_close_pending: bool`, `volume_save_failed: bool` default False; `SavedReceiver.remembered_volume: int | None = None`.
- Produces: exactly ErrorCode audio_volume_failed, volume_unavailable, volume_preference_invalid, volume_preference_failed, in that order after audio_mute_failed; catalog diagnostic and safe_activity validation described below.

- [ ] Add focused domain tests with exact type, bounds and absence/zero expectations.

```python
@pytest.mark.parametrize("n", [0, 1, 100])
def test_volume_level_exact_integer_boundaries(n):
    assert resources.VolumeLevel(value=n).value == n

@pytest.mark.parametrize("bad", [True, False, 1.0, "30", None])
def test_volume_level_rejects_nonintegers(bad):
    with pytest.raises(TypeError, match="^volume must be an integer$"):
        resources.VolumeLevel(value=bad)

@pytest.mark.parametrize("bad", [-1, 101])
def test_volume_level_rejects_out_of_range(bad):
    with pytest.raises(ValueError, match="^volume must be between 0 and 100$"):
        resources.VolumeLevel(value=bad)

def test_volume_receipt_distinguishes_missing_and_silent():
    missing = records.AudioStartReceipt(audio=True)
    silent = records.AudioStartReceipt(audio=True, volume_state="applied", level=0)
    assert missing.volume_state is missing.level is None
    assert silent.level == 0
    with pytest.raises((TypeError, ValueError)):
        records.AudioStartReceipt(audio=True, volume_state="applied")
    with pytest.raises(TypeError, match="^volume command requires a VolumeLevel$"):
        messages.SetVolume(receiver_id=RECEIVER, level=30)
```

Also parameterize every new receipt/snapshot/member type: exact bool, four strings only, optional integers, remembered/last_audible 1..100, effective/receipt 0..100, no bool-as-int; StartRequest wrong type gets exactly `start volume must be a VolumeLevel or None`. Add SetVolume, receipt and event payload to frozen/keyword-only and union inventories; update exact enum ordering and idle defaults without relaxing old checks. Assert all port signatures, keyword-only default and receipt result annotation.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/domain/test_contracts.py -k volume --basetemp=/tmp/av-domain-red`.
Expected: exit 1 for missing VolumeLevel/AudioStartReceipt/SetVolume before implementation.

- [ ] Implement the domain declarations, validation and exact port signatures.

```python
@dataclass(frozen=True, kw_only=True)
class VolumeLevel:
    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int:
            raise TypeError("volume must be an integer")
        if not 0 <= self.value <= 100:
            raise ValueError("volume must be between 0 and 100")
```

Use runtime imports for types used in exact-type validation, and TYPE_CHECKING imports for annotations only. Receipt validation requires exact bool, an optional exact string in unrequested/pending/applied/refused, an optional exact int 0..100 and a level whenever state=applied. Event payload validates exact int/bool. Add optional snapshot fields at the end to preserve existing constructors; validate them in SessionSnapshot.__post_init__ beside existing generation validation. Add SavedReceiver's positive preference validation. Extend StartRequest.__post_init__ without weakening physical-source checks. Add exact bool validation for report.volume with False default. No domain/__init__.py facade change.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/domain/test_contracts.py --basetemp=/tmp/av-domain-green`.
Expected: exit 0; all old domain inventories and new validation cases pass.

- [ ] Register the exact catalog and activity vocabulary with adversarial positive/negative tests.

| ErrorCode | Exact message | Recovery tuple |
| --- | --- | --- |
| audio_volume_failed | The audio control change could not be confirmed. The last confirmed audio state is kept. Try the control again, or stop the session. | back, stop |
| volume_unavailable | The starting volume could not be confirmed. Audio is withheld. Retry audio, start video only, or stop the session. | retry_audio, video_only, stop |
| volume_preference_invalid | The saved volume is invalid and was ignored. Other receiver preferences are unchanged. | back |
| volume_preference_failed | The receiver volume was applied but could not be saved. The previous saved level is kept. | back, stop |

| Activity code | Exact message | Permitted fields used |
| --- | --- | --- |
| audio.volume_applied | Receiver volume was applied. | level |
| audio.volume_failed | Receiver audio control change could not be confirmed. | level for level operations; error_code; fixed reason for closure |
| audio.volume_unavailable | Receiver volume control is unavailable. | reason; error_code when applicable |

Add `level` to ALLOWED_FIELDS/_INTEGER_BOUNDS as (0,100), `reason` to captured vocabulary with exactly backend_unsupported/startup_refused/receipt_pending/receipt_missing/no_audio/invalid_preference/audio_closed/close_unconfirmed, and `volume` as the seventh immutable capability flag. Reuse existing hook-free boundary validation; no type dispatch is added. Test bool/float/hostile int/string/subclass hooks, malformed reasons, unsupported fields, fixed messages/recovery, immutable export and secret rejection. Update the existing allowlist, error-code-pinned and capability snapshot inventories. Add a positive assertion such as:

```python
event = safe_activity("audio.volume_applied", RECEIVER, {"level": 30}, "2026-11-05T00:00:00Z")
assert event.message == "Receiver volume was applied."
assert dict(event.fields) == {"level": 30}
```

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/diagnostics/test_safety.py --basetemp=/tmp/av-diagnostics`.
Expected: exit 0; all new positive events survive the sanitizer and hostile values invoke no rendering hooks.

- [ ] Reinstall, verify and commit the complete contract seam.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/domain/test_contracts.py tests/diagnostics/test_safety.py --basetemp=/tmp/av-contract-seam
git add src/airplay_linux/domain/resources.py src/airplay_linux/domain/records.py src/airplay_linux/domain/messages.py src/airplay_linux/domain/ports.py src/airplay_linux/diagnostics/catalog.py src/airplay_linux/diagnostics/activity.py tests/domain/test_contracts.py tests/diagnostics/test_safety.py
git commit -m "feat(domain): define acknowledged volume and diagnostic contracts"
```

Expected: install and pytest exit 0; one commit with the listed domain/diagnostic paths. Concrete adapters are updated before their own conformance tests in Task 7.

## Task 3: Recognize only coherent volume surfaces and decode per-stream evidence

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/backend/contract.py:59-140,199-229,432-474,800-925`.
- Modify: `src/airplay_linux/policy/capabilities.py:386-545`.
- Test/Modify: `tests/backend/test_contract.py:1-1153`, `tests/policy/test_capabilities.py:1-914`.
- Test/Modify: `tests/backend/test_carried_source_patch.py:1-216` (Task 1 additions: feed actual emitted envelopes into the new decoder).

**Interfaces:**

- Consumes: `features_from_help(sender_help: str, ctl_help: str, *, advertised: frozenset[str] = frozenset()) -> BackendFeatures`, `decode_status(value: object) -> StatusReply | Err`, existing `_sender_surface`, `_ctl_surface`, `_target_scope_proven`, and Task 2 `CapabilityReport.volume: bool = False`.
- Produces: `BackendFeatures.target_volume: bool = False`; `BackendStream.volume: int | None = None`, `volume_state: str | None = None`, `audio_closed: bool | None = None`; unchanged `StatusReply` aggregate fields/profile strings.
- Produces: `EnvironmentProbe._probe(self) -> CapabilityReport`, `check(self) -> CapabilityReport`, `refresh(self) -> CapabilityReport` exposing volume only from proven backend.target_volume; no other gate changes.

- [ ] Add a local `volume_help` fixture and exact classification/row-scope cases before changing recognition.

```python
@pytest.fixture
def volume_help():
    root = Path(__file__).parents[1] / "fixtures" / "backend"
    sender = (root / "doubletake-0.5-source-help.txt").read_text()
    ctl = (root / "doubletake-ctl-0.5-help.txt").read_text()
    old = "  connect [target] [PIN-or-password]  Start mirroring (to target IP, or first free device)"
    new = (
        "  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)\n"
        "  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)\n"
        "  audio off [target]                  Close mirrored audio (all streams, or only the given IP)"
    )
    assert ctl.count(old) == 1
    return sender, ctl.replace(old, new), frozenset({
        "capabilities-v1", "status-source", "status-source-complete", "target-volume",
    })

def test_volume_surface_retains_source_profile(volume_help):
    sender, ctl, tokens = volume_help
    features = features_from_help(sender, ctl, advertised=tokens)
    assert features.profile == "doubletake-0.5+source"
    assert features.target_volume is True
    assert features.source_reporting_complete is True
    assert features.owned_daemon_control is False
```

Use module-local Path/imports as needed. Preserve all old sender/ctl fixtures. Add pinned-sender+extended-table recognition, source-only legacy recognition, missing/token-only/half-installation cases, malformed/missing audio row, wrong signature/order operands, and independent missing/negated/borrowed scope cases for each new row. Unknown tokens grant nothing, source completeness still needs the third token, and source help without tokens still refuses the whole backend. Do not borrow a disconnect or volume scope claim for audio.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_contract.py -k volume --basetemp=/tmp/av-recognition-red`.
Expected: exit 1 because the extended ten-row table is unknown/target_volume is absent.

- [ ] Implement §5's classifier verbatim around the existing scanners and returns.

```python
_VOLUME_CTL_COMMANDS = {
    **_PINNED_050_CTL_COMMANDS,
    "connect": "[target] [--audio on|off] [--volume <0-100>] [PIN-or-password]",
    "volume": "<0-100> [target]",
    "audio": "off [target]",
}
_TARGET_VOLUME_TOKEN = "target-volume"
```

Compute signatures, ctl_pinned, ctl_volume with both own-row scope predicates, source_surface/source_tokens, target_volume and inconsistency exactly as §5 specifies. A malformed ten-row surface matches neither accepted table and refuses even if ctl_volume=false. Unknown returns every gate false; verified returns the same pinned/source profile and adds target_volume. Preserve disconnect/mute/unmute scope proof, source completeness, advertisement consistency and all private composition gates. Add the bool field to BackendFeatures' existing exact-bool loop, not a second permissive truthiness path.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_contract.py -k 'volume or patched_surface or advertisement or scope' --basetemp=/tmp/av-recognition-green`.
Expected: exit 0 for new coherent/incoherent and retained source/scope cases.

- [ ] Add strict optional stream decoding and backend-to-report capability production.

Use member presence to distinguish omission from null in `_decode_stream`; present volume requires `type(value) is int` and 0..100, state requires exact allowed string, latch exact bool. Applied without volume refuses. Extend BackendStream direct-constructor validation consistently. Preserve existing target, credential, source, aggregate and metadata checks; never synthesize a new aggregate level/latch or copy another target's evidence. Missing all three fields remains a valid legacy status or mute reply.

Add a `volume_status` fixture built from an existing valid exact-target stream envelope. Parameterize absent/0/100, four states, applied-without-level, null/bool/string/float/bounds, bad state, strict latch and mixed/foreign target entries. Decode Task 1's produced status envelopes and assert their values rather than rebuilding equivalent hand-authored traces.

In EnvironmentProbe._probe add `volume=backend.target_volume` from its proven BackendFeatures view; keep audio readiness/start/trust/source gates independent. Extend existing offline probe fixtures with capable/legacy/incoherent results and verify check/refresh both expose the report.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_contract.py tests/policy/test_capabilities.py tests/backend/test_carried_source_patch.py --basetemp=/tmp/av-proof-report`.
Expected: exit 0; legacy omission decodes and missing capable receipt remains a later audio-path decision.

- [ ] Reinstall and commit the proof seam.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
git diff --check
git add src/airplay_linux/backend/contract.py src/airplay_linux/policy/capabilities.py tests/backend/test_contract.py tests/policy/test_capabilities.py tests/backend/test_carried_source_patch.py
git commit -m "feat(backend): prove coherent volume surfaces and stream receipts"
```

Expected: install/diff check exit 0 and one scoped commit; no profile consumer changes.

## Task 4: Store receiver v2 preferences without blocking recovery on a bad level

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/storage/codecs.py:24-90,697-715,741-784,914-958`.
- Modify: `src/airplay_linux/storage/repositories.py:24-62,175-243`.
- Test/Modify: `tests/storage/test_atomic_records.py:239-258,1-2028` (receiver/version/session projection cases).
- Test/Modify: `tests/storage/test_repositories.py:1-68,132-151`.

**Interfaces:**

- Consumes: Task 2 `SavedReceiver.remembered_volume: int | None` with exact positive validation, `Diagnostic`, `ErrorCode.volume_preference_invalid`, existing `AtomicFiles` and `RecordError`.
- Produces: `encode_receiver(saved: SavedReceiver) -> bytes`, always receiver v2; `decode_receiver(payload: bytes, *, on_diagnostic: Callable[[Diagnostic], None] | None = None) -> SavedReceiver`, reading v1/v2 with the new leaf alone droppable.
- Produces: `JsonReceiverStore(files: AtomicFiles, *, on_diagnostic: Callable[[Diagnostic], None] | None = None)` with unchanged `get(self, receiver_id: ReceiverId) -> Result[SavedReceiver | None]`, `all(self) -> Result[tuple[SavedReceiver, ...]]`, `put(self, saved: SavedReceiver) -> Result[None]` and per-record content-digest diagnostic deduplication.
- Produces: unchanged `VERSION=1`; `_ACCEPTED_VERSIONS` for session/ownership/daemon={1}, receiver={1,2}; `_WRITE_VERSIONS={RECEIVER_SCHEMA: 2}`. Newer receipt/reservation codec mechanisms remain separate.

- [ ] Add exact v1/v2 byte tests and droppable-volume/strict-structure matrices.

```python
def test_receiver_volume_v2_exact_minimal_bytes():
    record = SavedReceiver(
        receiver_id=ReceiverId("airplay-device:a"), service_metadata=(),
        display_name="TV", last_endpoint=None, continuity_fingerprint="fp",
        credential_ref=None, remembered_resolution=None, remembered_volume=30,
    )
    expected = b'{"continuity_fingerprint":"fp","display_name":"TV","last_endpoint":null,"receiver_id":"airplay-device:a","remembered_resolution":null,"remembered_volume":30,"schema":"airplay-linux/receiver","service_metadata":[],"version":2}'
    assert encode_receiver(record) == expected
    assert decode_receiver(expected) == record

@pytest.mark.parametrize("bad", [0, -1, 101, True, False, "30", 3.5, [], {}, 10**100])
def test_bad_volume_drops_only_preference(bad):
    raw = json.loads(encode_receiver(saved()))
    raw["remembered_volume"] = bad
    warnings = []
    decoded = decode_receiver(json.dumps(raw).encode(), on_diagnostic=warnings.append)
    assert decoded == replace(saved(), remembered_volume=None)
    assert [item.code for item in warnings] == [ErrorCode.volume_preference_invalid]
    assert warnings[0].receiver_id == decoded.receiver_id
```

Keep named receiver_v1/receiver_v2 fixtures in this module. Test v1's exact old field set, v2 positive/null/boundaries, absent new key as structural error, unknown extras/identity/schema/non-int or unknown version as errors, all other schemas refusing v2, non-null credential_ref refusal, callback failure swallowed, no automatic rewrite and session/queued-start bytes excluding all new fields. Update the receiver branch of the existing byte-roundtrip test to v2 without weakening other legacy byte pins.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_atomic_records.py -k 'volume or receiver' --basetemp=/tmp/av-storage-red`.
Expected: exit 1 for receiver v1 writes and missing diagnostic callback before implementation.

- [ ] Introduce schema-specific read/write versions and validate the volume leaf after identity and all old fields.

```python
VERSION = 1
_ACCEPTED_VERSIONS = {
    SESSION_SCHEMA: frozenset({1}), OWNERSHIP_SCHEMA: frozenset({1}),
    DAEMON_SCHEMA: frozenset({1}), RECEIVER_SCHEMA: frozenset({1, 2}),
}
_WRITE_VERSIONS = {RECEIVER_SCHEMA: 2}
```

Keep the v1 receiver field tuple unchanged and select the exact receiver tuple by version before field-set validation. Give the v2 leaf a raw JSON-value boundary until all structural/identity/old-member decoding succeeded. Only then map null to None, exact int 1..100 to itself, and all other leaf values to None plus the fixed catalog diagnostic. The callback receives only validated receiver_id and sanitized diagnostic; contain sink failures. Use existing sorted compact ASCII/allow_nan=False/no-newline output; write the new key even for None. Keep nested StartRequest projection unchanged so volume reconstructs to None.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_atomic_records.py --basetemp=/tmp/av-storage-codecs`.
Expected: exit 0; identity/structural corruption remains refused and every unrelated schema keeps v1.

- [ ] Thread sanitized warnings through JsonReceiverStore with content-sensitive deduplication.

Pass a local callback to decode_receiver for get/all. Keep the invalid-content SHA-256 by validated receiver id; report identical invalid bytes once, changed invalid bytes once again, and clear the retained digest after valid content. Callback errors cannot turn get/all into Err. Keep invalid bytes untouched; structural errors continue to fail the whole authoritative listing. Test direct codec and repository callbacks, get/all repeated reads, invalid sibling with a valid sibling, changed invalid content, a valid intervening version, and a callback that raises. No ReceiverStore port setter or rewrite-on-read is added.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_atomic_records.py tests/storage/test_repositories.py --basetemp=/tmp/av-storage-warnings`.
Expected: exit 0; malformed preference emits sanitized deduplicated warning and both records still load, while corrupt identity still makes all() Err.

- [ ] Reinstall and commit receiver storage.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
git diff --check
git add src/airplay_linux/storage/codecs.py src/airplay_linux/storage/repositories.py tests/storage/test_atomic_records.py tests/storage/test_repositories.py
git commit -m "feat(storage): add receiver volume preferences with v1 reads"
```

Expected: install/diff check exit 0 and one storage commit; no session schema bump.

## Task 5: Share one continuity-checked preference writer at composition

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/storage/repositories.py:24-62,545-604` (new class before PreferenceAccess and export in this module).
- Modify: `src/airplay_linux/session/engine.py:20-80,107-124`.
- Modify: `src/airplay_linux/session/virtual.py:775-846`.
- Modify: `src/airplay_linux/runtime.py:30-110,475-490,545-552,683-704`.
- Modify: `src/airplay_linux/session/testing.py:20-111`.
- Test/Modify: `tests/storage/test_repositories.py:1-151`, `tests/session/test_virtual_and_resolution.py:1-848` (existing resolution-write scenarios), `tests/app/test_composition.py:1-212` (ManagerDeps wiring assertions).

**Interfaces:**

- Consumes: unchanged `PreferenceAccess.for_record_set(record_set: ReceiverRecordSet, saved: SavedReceiver | None) -> SavedReceiver | None`; `ReceiverStore.get(self, receiver_id: ReceiverId) -> Result[SavedReceiver | None]`, `put(self, saved: SavedReceiver) -> Result[None]`; `DiscoveryPort.current(self, receiver_id: ReceiverId) -> ReceiverView | None`; `VolumeLevel(value: int)` and `ResolutionDecision`.
- Produces: `ReceiverPreferenceWriter.__init__(self, store: ReceiverStore, discovery: DiscoveryPort) -> None` and `async update(self, record_set: ReceiverRecordSet, *, remembered_resolution: ResolutionDecision | None = None, remembered_volume: VolumeLevel | None = None, still_current: Callable[[], bool]) -> Result[SavedReceiver]` in storage/repositories.py.
- Produces: required `ManagerDeps.preferences: ReceiverPreferenceWriter`; one instance per production graph and one per SessionRig shared by resolution and volume consumers.
- Produces: `VirtualLifecycle._remember(self, ctx: SessionContext) -> None` calls shared update with remembered_resolution and its existing evidence/publication fence; successful preference effects publish a snapshot even if other fields compare equal.

- [ ] Add barrier-driven interleaved-preference tests before the writer exists.

Create `interleaved_preferences` in tests/storage/test_repositories.py using existing MemoryReceiverStore/FakeDiscovery/Deferred and explicit get/put barriers. Drive two concurrent update calls, in both orderings, with resolution and volume=30. Assert each get occurs inside the same per-receiver lock, the final saved record contains both changes and existing metadata, and another receiver progresses while the first is held. Test missing valid record creation, zero/both/neither preference arguments, stale discovery after get, PreferenceAccess refusal of an unjustified existing record, still_current before/after put, put failure, and no rollback over a newer record.

A minimal new API assertion is:

```python
assert inspect.iscoroutinefunction(repositories.ReceiverPreferenceWriter.update)
assert tuple(inspect.signature(repositories.ReceiverPreferenceWriter.update).parameters) == (
    "self", "record_set", "remembered_resolution", "remembered_volume", "still_current",
)
```

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_repositories.py -k preferences --basetemp=/tmp/av-writer-red`.
Expected: exit 1 for missing ReceiverPreferenceWriter; the barriers must later assert actual durable merged values, not merely call counts.

- [ ] Implement the per-receiver locked read-modify-write with continuity validation.

Create `_locks: dict[ReceiverId, asyncio.Lock]` on the writer. Require exactly one non-None preference argument of its exact domain type; zero volume returns invalid_input. Acquire the receiver's lock, await latest get inside it, then recheck discovery record_set equality, online/current intent via still_current, persistable/nonambiguous/nonreplacement identity and PreferenceAccess. A refused existing record returns stale_target; never replace it with cached preferences. Merge only the supplied field. For a missing valid record use current view.display_name, record_set.records/fingerprint and the existing endpoint projection:

```python
endpoint = ReceiverTarget(
    receiver_id=record_set.receiver_id, service_records=record_set.records,
    canonical_host=record_set.canonical_address,
    service_port=record_set.records[0].port if record_set.records else 7000,
    protocol="airplay", endpoint_fingerprint=record_set.endpoint_fingerprint,
    endpoint_generation=record_set.endpoint_generation,
)
```

Create SavedReceiver with credential_ref=None and both preference defaults before replacing the requested field. Await put while holding the lock. Revalidate record_set, still_current and PreferenceAccess after put before returning Ok(saved). If publication is stale, return stale_target without attempting to rewrite old bytes. Store failures stay typed; caller maps a volume save failure to volume_preference_failed. Leave PreferenceAccess unchanged.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_repositories.py --basetemp=/tmp/av-writer-green`.
Expected: exit 0; both field orderings survive and independent receivers remain independent.

- [ ] Wire the shared instance and safe malformed-preference diagnostic bridge.

Add the required ManagerDeps field, import the class directly, and update its only two constructor sites: `_compose_production` and SessionRig. Build production writer once from receivers/discovery; do not construct in handlers. Replace VirtualLifecycle._remember's nested get/replace/put closure with `self.deps.preferences.update(record_set, remembered_resolution=remembered, still_current=...)`, retaining the existing observed-resolution evidence and captured intent/lease/receiver fence. Its successful effect still publishes a fresh snapshot.

At JsonReceiverStore construction, supply a local on_diagnostic closure retaining at most one sanitized pending warning per receiver until ActivityLog exists. After creating activity bind its local reference and flush once using `audio.volume_unavailable`, reason=invalid_preference and error_code=volume_preference_invalid. The store already deduplicates identical bytes. Do not expose a new mutable store setter or port. Test warnings before/after log construction and composition's one writer instance without starting real production adapters; use existing injected production-graph test boundaries.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/storage/test_repositories.py tests/session/test_virtual_and_resolution.py tests/app/test_composition.py --basetemp=/tmp/av-shared-writer`.
Expected: exit 0; resolution persistence remains functional and no production host boundary is consulted.

- [ ] Reinstall and commit preference composition.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
git diff --check
git add src/airplay_linux/storage/repositories.py src/airplay_linux/session/engine.py src/airplay_linux/session/virtual.py src/airplay_linux/runtime.py src/airplay_linux/session/testing.py tests/storage/test_repositories.py tests/session/test_virtual_and_resolution.py tests/app/test_composition.py
git commit -m "feat(storage): serialize receiver preference updates across audio and resolution"
```

Expected: install/diff check exit 0 and one scoped commit.

## Task 6: Add target-fenced typed controls and full-status acknowledgements

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/backend/control.py:33-178,372-508,582-643,804-939,1140-1211`.
- Modify: `src/airplay_linux/process/ownership.py:318-343`.
- Test/Modify: `tests/backend/test_control.py:1-909`, `tests/process/test_ownership.py:1-1024`.
- Modify: `tests/fixtures/backend/contract-v1.json:1-69`.
- Test/Modify: `tests/backend/test_contract.py:577-598` (fixture inventory only).

**Interfaces:**

- Consumes: `BackendFeatures.target_volume`, `BackendStream(volume, volume_state, audio_closed)` and `decode_status(value: object) -> StatusReply | Err` from Task 3; `VolumeLevel`, `AudioPolicy`; existing `DispatchFence = Callable[[], Result[None]]` and receipt-backed OwnershipAuthority.
- Produces: `BackendOperation.volume="volume"`, `audio_off="audio_off"` immediately after unmute, uppercase aliases VOLUME/AUDIO_OFF; `VolumePayload(level: VolumeLevel)` and `AudioGatePayload(enabled: Literal[False] = False)`, frozen keyword-only and exported in backend/control.__all__.
- Produces: `ConnectPayload(source: CaptureSource | None = None, credential: SensitiveText | str | None = None, audio: AudioPolicy | None = None, initial_volume: VolumeLevel | None = None)` retaining redacted repr.
- Produces: unchanged `PublishedDoubletakeProfile.try_argv(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload) -> Result[tuple[str, ...]]` and `PublishedDoubletakeControl.request(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None, *, dispatch_fence: DispatchFence | None = None, ownership_proof: OwnershipProof | None = None) -> Result[BackendReply]`; new successes return StatusReply, not AckReply.
- Produces: `EvidenceAuthority.allows(self, operation: BackendOperation) -> bool` requires owned_daemon_control AND multi_target AND target_volume for both new operations; FENCE_REQUIRED_OPERATIONS contains both.

- [ ] Add exact argv and payload tests using legacy and explicitly capable features.

```python
def test_volume_profile_uses_exact_targeted_argv():
    from dataclasses import replace
    from airplay_linux.backend.control import VolumePayload, AudioGatePayload
    from airplay_linux.domain.resources import VolumeLevel
    profile = PublishedDoubletakeProfile(socket_path="/run/private/control.sock")
    profile = replace(profile, features=replace(profile.features, target_volume=True))
    receiver = target()
    assert profile.argv(BackendOperation.volume, receiver, VolumePayload(level=VolumeLevel(value=30))) == (
        "doubletake-ctl", "-socket", "/run/private/control.sock", "volume", "30", receiver.canonical_host,
    )
    assert profile.argv(BackendOperation.audio_off, receiver, AudioGatePayload()) == (
        "doubletake-ctl", "-socket", "/run/private/control.sock", "audio", "off", receiver.canonical_host,
    )
```

Add exact capable connect tuples for audio on+30, on without level, off without level, and None/None legacy tuple. Direct legacy payloads with either new field refuse unsupported_operation; level plus disabled audio refuses invalid_input. Test missing targets, wrong payload/level types, False-only gate, absent feature, source/credential secrecy and unchanged old tuple mappings.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_control.py -k volume --basetemp=/tmp/av-control-red`.
Expected: exit 1 for missing operation/payload support.

- [ ] Implement typed payloads, feature/ownership admission and exact tuples without changing dispatch discipline.

Add both payload classes to BackendPayload, published/application-fixture validation and __all__. Validate exact domain VolumeLevel and exact False. Add new connect fields and exact type checks while preserving redacted source/credential repr. Extend try_argv with §3.1 tuples and feature checks; production capable transport will supply policy, but direct None/None remains old tuple. Reject missing targets before I/O. Extend EvidenceAuthority/_available with the specified triple gate; fixture defaults target_volume=False and opts in explicitly in capable tests. Keep handshake admission for application fixture mutations.

Add both operations to the existing required fence set; preserve post-await original fence evaluation, the no-await-to-spawn gap and post-I/O owner confirmation. Do not replace receipt-backed ownership with capability tokens. Extend every existing control/ownership truth table and fence parameterization with volume/audio_off.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_control.py::test_volume_profile_uses_exact_targeted_argv tests/process/test_ownership.py --basetemp=/tmp/av-control-admission`.
Expected: exit 0 for the exact new profile tuples and ownership truth tables; full control-reply/fixture verification follows its implementation in the next step.

- [ ] Decode new live replies as full exact-target status and pin failure behavior.

Use decode_status on successful volume/audio_off replies, apply existing ownership metadata checks, then select exactly one original target entry. Volume requires streaming, has_audio=True, applied, audio_closed is False and level equal to payload.value. Close permits connecting and requires audio_closed is True plus this call's ok=True. Never accept an aggregate bool, a foreign/duplicate/missing entry or a false-ok latched status as confirmation. Return the StatusReply through BackendReply; add no AckReply field. Branch before the old `_parse_ack` path and avoid applying its `.target` assumptions to the full-status stream notice.

Map valid ok=false/exit-1 and new acknowledgement-evidence failures to audio_volume_failed; keep ownership/target/dependency error distinctions and strict exit/JSON envelope handling. Do not expose daemon error text. Test malformed JSON, wrong exit/ok combinations, missing confirmation, rejected/late ownership, target replacement after authorization and zero mutation I/O for missing fence. Update exact typed-operation fixture requests/replies with volume/audio_off and status_reply tags while keeping its explicit non-live provenance and target_volume=false default. The all-operations fixture test creates a capable variant for just the new operations; legacy inventory remains tested.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_control.py tests/process/test_ownership.py tests/backend/test_contract.py --basetemp=/tmp/av-control-receipts`.
Expected: exit 0; no new command bypasses owner/fence proof and connect still confirms admission only.

- [ ] Reinstall and commit typed control.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
git diff --check
git add src/airplay_linux/backend/control.py src/airplay_linux/process/ownership.py tests/backend/test_control.py tests/process/test_ownership.py tests/fixtures/backend/contract-v1.json tests/backend/test_contract.py
git commit -m "feat(control): fence target volume and audio-close acknowledgements"
```

Expected: install/diff check exit 0 and one control commit.

## Task 7: Carry receipts through transport and emit actual worker settlement

**Implementer tier:** Capable
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/transport/doubletake.py:18-46,203-222,388-508,590-634,666-727,957-1033,1083-1183`.
- Inspect: `src/airplay_linux/transport/targets.py:1-57` (reuse require_current/cleanup; no new target authority).
- Modify: `src/airplay_linux/testing/adapters.py:345-529` (FakeTransport and its imports).
- Test/Modify: `tests/transport/test_doubletake.py:1-2641`, `tests/testing/test_fakes.py:1-1893`, `tests/adapter_contracts/test_transport_contract.py:1-930`.
- Modify: `tests/session/virtual_scenarios.py:30-55`, `tests/integration/test_lifecycle_matrix.py:56-83`, `tests/ui_acceptance/test_matrix.py:170-199` (keyword-forwarding start_stream overrides).

**Interfaces:**

- Consumes: Task 2 `VolumeLevel`, `AudioStartReceipt(audio: bool, volume_state: str | None, level: int | None)`, event `VolumePayload(level: int, ok: bool)`; Task 6 backend `VolumePayload(level: VolumeLevel)`, `AudioGatePayload(enabled: Literal[False]=False)`, extended ConnectPayload and new fenced operations returning StatusReply.
- Produces: `DoubleTakeTransport.start_stream(self, target: ReceiverTarget, source: CaptureSource | None, audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel | None = None) -> Result[StreamHandle]`; `start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]`.
- Produces: `DoubleTakeTransport.set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]`; `close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]`; identical FakeTransport signatures.
- Produces: `_reported_stream_audio(self, stream: _Stream, context: OperationContext) -> Result[AudioStartReceipt]`, using one existing status read; private `_Stream.initial_volume: VolumeLevel | None`, `audio_closed: bool`, `volume_state: str | None`, owned volume-task record and per-lease close ownership.
- Produces: exactly one original-handle/context volume_result from the actual volume task's done callback, after task.done()/lock release; `payload.ok` true only for current nonexpired acknowledged success, including exact acknowledged zero.

- [ ] Add capable transport tests, held-worker tests and receipt normalization assertions.

Extend the existing Harness/ContractHarness with `volume_control` and `held_volume` fixtures in tests/transport/test_doubletake.py. Explicitly set target_volume=True and use the published profile for status-receipt behavior. Script independent barriers for control result, public clock expiry and actual worker completion. Name the startup tests `test_volume_startup_receipt_uses_existing_status_read` and `test_volume_connect_payload_is_capability_conditioned`; parameterize receipt cases applied-positive, applied-zero, refused, pending, missing, unrequested, no-audio and audio_closed=True. Assert one existing status request, exact target evidence and no early capable audio_recovered. Pin initial-volume/on/off/legacy payloads, invalid input before allocation and both new fence operations.

In tests/testing/test_fakes.py include this compatibility assertion using its existing live fake-handle setup:

```python
transport.queue_result("start_audio", Ok(value=None))
result = await transport.start_audio(handle, operation)
assert result == Ok(value=AudioStartReceipt(audio=True))
assert result.value.level is None
```

The local fixture must first create a current handle with the operation's lease. Add wrong/stale handle/target/daemon checks and explicit receipt passthrough.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/transport/test_doubletake.py tests/testing/test_fakes.py -k volume --basetemp=/tmp/av-transport-red`.
Expected: exit 1 for absent initial_volume/set_volume/close_audio/receipt contracts.

- [ ] Implement exact lease startup input and one-read audio receipts.

Validate VolumeLevel before allocating anything. Store policy/input on the original _Stream. Capable+enabled ConnectPayload carries policy and optional level; capable+disabled carries off/no level; legacy passes existing empty ConnectPayload. Retain all source, ownership, trust, target, readiness, abandoned-acquisition and cleanup barriers.

Change `_reported_stream_audio` to return the matching entry's receipt after its existing status request and post-await current checks. A reported closed latch returns audio_unavailable even if local policy remains enabled. Capable refused/pending/missing state is a typed receipt even when has_audio=false; applied confirms only with valid level and audio=true. Legacy has_audio=false keeps its current refusal. Update `_start_audio`: retain five-second call deadline; capable return does not emit audio_recovered before actor interpretation, while legacy preserves event meaning/timing and normalizes independent acknowledged start to AudioStartReceipt(audio=True). Update stream.muted/audio only on applied+audio; refusal/pending/missing clears confirmed mute and records Unavailable. Maintain stream.volume_state as admitted startup/recovery evidence, not an interpretation of failed live writes.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/transport/test_doubletake.py::test_volume_startup_receipt_uses_existing_status_read tests/transport/test_doubletake.py::test_volume_connect_payload_is_capability_conditioned --basetemp=/tmp/av-transport-receipt`.
Expected: exit 0; these startup tests prove receipt/connect behavior without selecting the worker-settlement tests whose implementation is the next step.

- [ ] Implement volume task ownership and a close path independent of the volume lock.

Alias the event payload explicitly, for example `VolumePayload as EventVolume`, keeping backend VolumePayload distinct. For well-typed handle/level/context create one owned task record carrying original identity, task, requested level and expired flag. Put transport-preflight checks inside that task so every actor-admitted typed call settles once even if its current _Stream is missing. Malformed direct API arguments return invalid_input without a task. `_set_volume` checks feature, current exact handle/target/session/endpoint/daemon/trust, enabled policy/latch, normal Enabled/Muted admission or exact initial_volume refused-start retry, before and after awaits. Issue typed volume through `_request` and current dispatch fence. Success is the exact receipt percent, not an echo.

Use `_wait`'s abandon callback to set expired synchronously before a late worker can publish. A public timeout returns audio_volume_failed but does not clear ownership or release a slot. The actual task done callback, after lock release, emits one settlement even for cancellation/preflight refusal; build it from the supplied typed original handle/context if no current stream exists. Noncurrent/expired success has ok=False and cannot alter stream.muted/audio, effective state or preferences. Actor fences discard obsolete callbacks. A failed live level retains earlier startup evidence and never calls stop/drop/reconnect.

At close admission latch local audio_closed=True and replace audio_policy with enabled=False before an await. Dispatch audio_off through a separate path which never waits on _Stream.lock; revalidate original stream/fence. Repeated close joins an outstanding per-lease close worker, but an expired public result cannot confirm Disabled: after that worker retires, a newly admitted close obtains its own fresh acknowledged request. Failures retain the latch; post-await volume continuations recheck it. Stop/drop/daemon exit retire publication ownership and preserve existing resource cleanup joins. Do not treat cleanup permission as current-target mutation authority.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/transport/test_doubletake.py -k 'held_volume or volume or fence or replacement or daemon_exit' --basetemp=/tmp/av-transport-settlement`.
Expected: exit 0; held public timeout leaves one worker, settlement observes lock unlocked/task done, close dispatches while that worker remains held, and late success changes no confirmed state.

- [ ] Implement FakeTransport parity and adapt every test-local start_stream override.

Journal initial_volume and typed new method arguments. Default/scripted Ok(None) audio starts normalize to a legacy receipt, explicit receipt passes through. Queue volume/close in the existing scripted/Deferred mechanism; success alone updates acknowledged fake levels/mute, close is one-way and current-handle/target/daemon checks remain. Emit one retirement settlement per admitted volume call, including scripted failures and preflight refusal; Deferred public completion and actual retirement must be separately observable where tested. Clear fake per-lease authority on stop/replacement. Retain legacy defaults in existing rigs.

Add keyword-only `initial_volume=None` to the three listed overrides and forward it unchanged into super().start_stream; do not lose desktop source None or old positional arguments. Extend adapter contract tests with the new port signature and typed result behavior over explicit fakes, without promoting the non-live profile's default capability. Keep the existing real private-process contract regressions intact.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/transport/test_doubletake.py tests/testing/test_fakes.py tests/adapter_contracts/test_transport_contract.py tests/integration/test_lifecycle_matrix.py tests/ui_acceptance/test_matrix.py --basetemp=/tmp/av-transport-parity`.
Expected: exit 0; no live backend or host capture boundary is used.

- [ ] Reinstall and commit the transport/adapter seam.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
git diff --check
git add src/airplay_linux/transport/doubletake.py src/airplay_linux/testing/adapters.py tests/transport/test_doubletake.py tests/testing/test_fakes.py tests/adapter_contracts/test_transport_contract.py tests/session/virtual_scenarios.py tests/integration/test_lifecycle_matrix.py tests/ui_acceptance/test_matrix.py
git commit -m "feat(transport): return startup receipts and settle owned volume tasks"
```

Expected: install/diff check exit 0 and one scoped commit.

## Task 8: Propagate startup volume and strip transient authority on recovery

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/session/audio.py:94-118,275-290` (start_volume carrier only; authority handlers are Task 9).
- Modify: `src/airplay_linux/session/mirror.py:175-290`.
- Modify: `src/airplay_linux/session/virtual.py:379-421,599-656`.
- Modify: `src/airplay_linux/session/reconnect.py:201-209,307-337`.
- Modify: `src/airplay_linux/session/engine.py:190-215` (new lease resets transient snapshot values).
- Modify: `src/airplay_linux/session/persistence.py:20-33`, `src/airplay_linux/session/recovery.py:210-350`.
- Test/Modify: `tests/session/test_audio_lifecycle.py:1-1175`, `tests/session/test_startup_recovery.py:1-586`, `tests/session/test_reconnect.py:1-648`, `tests/session/test_virtual_and_resolution.py:1-848`.

**Interfaces:**

- Consumes: Task 2 `StartRequest.volume: VolumeLevel | None`, snapshot volume fields and `AudioPolicy`; Task 7 `TransportPort.start_stream(self, target: ReceiverTarget, source: CaptureSource | None, audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel | None = None) -> Result[StreamHandle]`; unchanged `PreferenceAccess.for_record_set(record_set: ReceiverRecordSet, saved: SavedReceiver | None) -> SavedReceiver | None`.
- Produces: `AudioRuntime.start_volume: VolumeLevel | None = None`, retained over initial capture/audio-probe/resolution/pairing continuations; `MirrorLifecycle._start/_begin_stream`, `VirtualLifecycle._start/_begin_stream`, `ReconnectLifecycle._begin_stream` capture startup policy/value together before awaits.
- Produces: `_safe_snapshot(state: SessionSnapshot) -> SessionSnapshot` removes effective_volume/last_audible_volume/volume_state/audio_close_pending/volume_save_failed and all old mute/audio authority; `StartupRecovery._restore(self)` reloads remembered_volume only from continuity-approved saved receiver data, keeping durable audio policy.

- [ ] Add startup input and recovery tests before changing lifecycle propagation.

Extend start helpers in tests/session/test_audio_lifecycle.py with explicit `volume: VolumeLevel | None = None` passed into StartRequest, without changing legacy defaults. Add capable positive remembered startup, explicit zero with no saved zero, unrequested startup, capable video-only, incapable saved preference and initial resolution/pairing continuation cases. Assert the actual fake transport journal's `initial_volume` and policy, not a snapshot-only claim. Reconnect/replacement tests capture current policy and current remembered preference, clear old effective/last-audible and do not reuse a held retry's values. Identity replacement withholds old remembered values.

Recovery tests persist a snapshot with e=0/a=30/close-pending/save-failed and queued StartRequest(volume=30), then reconstruct through persistence/recovery with v1 and v2 receiver records. Assert session bytes lack all new fields, the reconstructed start volume is None, all transient values default, and only validated receiver remembered preference is reloaded. Invalid volume warning must not block recovery; structural receiver corruption still blocks admission as before.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py tests/session/test_startup_recovery.py -k 'volume or transient' --basetemp=/tmp/av-startup-red`.
Expected: exit 1 for dropped initial input/unrestored validated preference before implementation.

- [ ] Carry initial intent/value through Mirror, Virtual and reconnect with fresh-lease resets.

Store request.volume in AudioRuntime.start_volume at initial start admission, preserving explicit zero across capture/probe/resolution/pairing. Snapshot remembered_volume only accepts positive inputs justified for that receiver; explicit start volume zero never becomes durable r. At initial `_begin_stream`, capture policy and start_volume before effect awaits; pass the value only when capability.volume and policy.enabled. Capable disabled start passes off/no level. On later reconnect/virtual replacement read CURRENT policy and snapshot remembered_volume, never old retry policy, last-audible or effective. A saved preference alone cannot enable feature flags.

A typical stream-acquisition closure binds values before dispatch:

```python
policy = ctx.state.audio_policy
initial = audio_runtime(ctx).start_volume
if not self.deps.capabilities().volume or not policy.enabled:
    initial = None
ctx.launch(
    Route.mirror, "transport_start", context,
    lambda target=target, source=source, policy=policy, context=context, initial=initial:
        self.deps.transport.start_stream(
            target, source, policy, context, initial_volume=initial,
        ),
    acquisition=True,
)
```

This is the exact MirrorLifecycle._begin_stream launch shape with its existing `target/source/context` locals and `transport_start` effect name. Virtual retains its existing `_with_backend_events(..., acquisition=True)` wrapper, and reconnect retains its current-target discovery check; bind their policy/value into the nested closure before its first await. In SessionContext.new_transport_lease reset e/a/mute/receipt/close-pending/save-failed before the new stream can publish; Task 9 additionally retires AudioRuntime's slots on identity/lifetime changes. Do not make a new lease inherit an acknowledged level.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py tests/session/test_reconnect.py tests/session/test_virtual_and_resolution.py -k 'volume or reconnect or video_only' --basetemp=/tmp/av-startup-propagation`.
Expected: startup journal tests pass; no legacy start sends new fields to the backend.

- [ ] Strip new transient members and reload remembered preference through the existing continuity boundary.

Set transient fields to their safe defaults beside muted in `_safe_snapshot`; codecs' explicit v1 allowlist already omits them and queued-start volume. Recovery must not load new authority from old bytes or carry session-snapshot remembered values as evidence. Reload r from the validated receiver store record via PreferenceAccess, with absent/invalid/replaced record meaning None; preserve stored audio_policy. Bad volume is a warning from the store, not an Err/listing/startup barrier. Do not change other recovery or durable trust/cleanup handling.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_startup_recovery.py tests/storage/test_atomic_records.py --basetemp=/tmp/av-volume-recovery`.
Expected: exit 0; unsafe authority never survives serialization or custom SessionStore boundaries.

- [ ] Reinstall, run the lifecycle regression selection and commit startup propagation.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py tests/session/test_startup_recovery.py tests/session/test_reconnect.py tests/session/test_virtual_and_resolution.py --basetemp=/tmp/av-startup-regression
git add src/airplay_linux/session/audio.py src/airplay_linux/session/mirror.py src/airplay_linux/session/virtual.py src/airplay_linux/session/reconnect.py src/airplay_linux/session/engine.py src/airplay_linux/session/persistence.py src/airplay_linux/session/recovery.py tests/session/test_audio_lifecycle.py tests/session/test_startup_recovery.py tests/session/test_reconnect.py tests/session/test_virtual_and_resolution.py
git commit -m "feat(session): carry startup volume and reload safe receiver preferences"
```

Expected: install/pytest exit 0 and one lifecycle commit; capable receipt decisions are added in Task 9.

## Task 9: Implement actor receipt, single-knob, settlement and close authority

**Implementer tier:** Capable
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/session/audio.py:34-742`.
- Modify: `src/airplay_linux/session/engine.py:18-65,776-810` (SetVolume import/route only; no broad _admit exception).
- Modify: `src/airplay_linux/session/fencing.py:89-130`.
- Test/Modify: `tests/session/test_audio_lifecycle.py:1-1175`, `tests/session/test_fencing.py:1-369`, `tests/session/test_actor_fences.py:1-691`.

**Interfaces:**

- Consumes: `SetVolume(receiver_id: ReceiverId, level: VolumeLevel)`, `SetMuted(receiver_id: ReceiverId, muted: bool)`, `RetryAudio(receiver_id: ReceiverId)`, `StartVideoOnly(receiver_id: ReceiverId)`; `AudioStartReceipt(audio: bool, volume_state: str | None = None, level: int | None = None)`.
- Consumes: `TransportPort.set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]`, `close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]`, `start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]`.
- Consumes: `ReceiverPreferenceWriter.update(self, record_set: ReceiverRecordSet, *, remembered_resolution: ResolutionDecision | None = None, remembered_volume: VolumeLevel | None = None, still_current: Callable[[], bool]) -> Result[SavedReceiver]`; `audio_runtime(ctx: SessionContext) -> AudioRuntime` owns each actor's mutable slots.
- Produces: `AudioRuntime.volume_inflight: tuple[OperationId, LeaseId, int, str] | None`, `volume_desired` same type, `volume_reported: bool=False`, `close_operation: OperationId | None=None`, `volume_absence_reported: bool=False`, `start_volume: VolumeLevel | None=None`, `preference_operation: OperationId | None=None`; origin vocabulary exactly slider/mute/unmute/retry. Existing pending_mute/muted_lease remain for legacy. The occupied-Retry flag `retry_requested: bool=False` records a repeated retry without rotating its audio fence.
- Produces: AudioLifecycle claims SetVolume, volume_result and volume/audio_close/remember_volume effects through Route.audio; startup/retry receipt state and event decisions occur in one actor step.
- Produces: `SessionManager._route(message: Command | ResourceEvent) -> Route | None` routes SetVolume to audio; `accept_transport(state: SessionSnapshot, event: TransportEvent) -> bool` treats volume_result as an audio result under existing full identity fences; individual id/lease checks remain in AudioLifecycle.
- Produces: acknowledged snapshot e/a/r, confirmed mute and current preference-publication fencing, with no additional transport event kind, closure error code or global engine admission exception.

- [ ] Add real-SessionManager transition, receipt and single-knob tests.

Create capable variants of mirror_rig/virtual_rig by replacing the capability callback with a report whose volume=True; preserve legacy rigs unchanged. Table-drive every row below through manager.dispatch and actual effect/event delivery, observing fake transport calls, snapshot state, durable store and emitted activity:

| Intent/result | Required expectation |
| --- | --- |
| applied startup level>0 | Enabled/active, e=a=level, muted false, one audio.recovered. |
| applied startup zero | Muted/active, e=0, prior a retained, all four mute representations agree. |
| refused/pending/missing receipt | Unavailable/audio_decision, confirmed mute false, historical same-lease e/a preserved, exact volume_unavailable reason, no early recovery. |
| applied without audio | Unavailable/audio_decision, audio_unavailable/no_audio, no invented e. |
| unrequested or legacy | Old behavior, no invented 100/e/a. |
| slider 1..100 acknowledgement | e=a=n, Enabled, shared save requested, r changes only after save success. |
| slider 0 or speaker mute | volume 0, Muted/e=0, a/r retained, no save. |
| speaker unmute | one volume of a, else r, else 100; no legacy mute/unmute on capable stream. |
| refused startup Retry | retained initial value/r (including explicit zero), same handle, new audio fence only when worker free. |
| pending/missing receipt Retry | one start_audio resample on same handle, unchanged timer. |
| unavailable/disabled/starting/decision slider or mute | no backend mutation; explicit Retry is the only refused-start exception. |
| live rejection/failure/timeout | video/handle/e/a/r/mute kept; one diagnostic/activity. |
| save failure | e/a retained, old r, volume_save_failed=true, exact save diagnostic, no second audio.volume_failed. |
| later successful positive save | r updated, save-failed cleared, fresh snapshot publication. |

Use direct activity assertions because AudioLifecycle._emit contains sink failures. Include capability absence once per session and availability transition deduplication, and invalid trust/cleanup/current-target cases with zero mutation I/O.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py -k volume --basetemp=/tmp/av-actor-red`.
Expected: exit 1 because SetVolume has no route/receipt state publication yet.

- [ ] Route and fence new audio work, then interpret capable receipts atomically.

Import SetVolume and event VolumePayload; add volume/audio_close/remember_volume to _AUDIO_EFFECTS and volume_result to _AUDIO_EVENTS. Route SetVolume to Route.audio and classify volume_result as audio in accept_transport; keep original stream/lease/session/endpoint/daemon fences and add captured trust_generation/current-target/no-trust-block/no-cleanup checks at each handler publication. No change to SessionManager._admit's broad acceptance rules.

For capable audio_start/audio_retry Ok receipts, apply §8.2's exact state/stage/event table in one actor step. Applied+audio sets acknowledged e, a only if positive, snapshot.muted/audio, AudioRuntime.muted_lease and transport-confirmed representation consistently. Refused/pending/missing collapses to retryable Unavailable/audio_decision, clears confirmed mute but preserves historical e/a on the same lease. Preserve volume_unavailable in _decision_error. Unrequested/legacy uses today's logic. A later has_audio/audio_recovered observation cannot clear a refused decision; only the owned receipt/Retry can. Startup transport failure remains ordinary cleanup and promises no reusable broken stream.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py -k 'receipt or startup or volume_surface or unavailable' tests/session/test_fencing.py --basetemp=/tmp/av-actor-receipts`.
Expected: receipt/fence tests pass; no new global acceptance exemption.

- [ ] Implement one in-flight operation and one latest desired value, draining only on actual settlement.

Mint an individual operation id without replacing snapshot.operation_id or the established audio_operation_id. If no inflight, record `(id, lease, level, origin)` and launch volume through Route.audio; otherwise replace the single desired slot without launching/saving it. Public EffectEvent Err reports once for the matching inflight worker but keeps it occupied; public Ok applies nothing. Settlement compares full fences plus individual id/lease, consumes the worker once, applies a current nonexpired acknowledged result, then dispatches at most one latest still-admissible value. Public/result delivery in either order cannot duplicate failure/drain. A queued desired value does not invalidate a valid intermediate acknowledgement. A timeout's expired result cannot change state or save.

For refused-start Retry retain the original AudioRuntime.start_volume, including zero. Mint a fresh audio id only with no occupied worker. If repeated Retry arrives while occupied, set retry_requested without minting or rotating any fence; original settlement remains acceptable and mints the next audio id immediately before its single re-dispatch. Retry refusal/error/timeout returns to Unavailable/audio_decision while retaining the inflight record/fence; do not call _hold_decision and retire the fence it needs. A failed live write cannot set a new startup-refused state.

Retire desired/inflight publication, pending preference publication and retry flag on Stop/Cancel, closure, audio loss, fresh lease, endpoint/daemon/trust invalidation. Loss clears confirmed mute representations but retains same-lease historical e/a; fresh lease clears e/a. Late/duplicate/obsolete results emit no activity/diagnostic/save and never drain old work. Add volume_drag with first worker held, several requests, public timeout, then actual settlement; assert one worker/slot and only the latest queued dispatch, plus the nonexpired intermediate acknowledgement variant.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py -k 'volume_drag or volume or retry or mute' tests/session/test_actor_fences.py --basetemp=/tmp/av-actor-settlement`.
Expected: exit 0 for coalescing/timeout/occupied-Retry cases; no backend mute/unmute on capable streams.

- [ ] Persist positive slider acknowledgements with an independent current save-publication id.

On an acknowledged positive slider set runtime.preference_operation to its individual id and capture receiver/intent/lease/target/audio/trust generation plus a still_current predicate. Launch remember_volume through Route.audio with `preferences.update(record_set, remembered_volume=VolumeLevel(value=n), still_current=...)`. This predicate remains valid after volume_inflight is consumed; a later acknowledged positive slider replaces it, queued desires and mute do not. Retire on Stop/loss/replacement/closure. Recheck the predicate on save-result publication so a stale result cannot update r/UI even after a write already committed. The writer serializes durable merges and performs no rollback to older bytes. Success alone updates r, clears volume_save_failed and publishes a fresh snapshot. Failure preserves e/a and old r, sets save-failed and last_error=volume_preference_failed; do not misreport the level itself as failed.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py -k 'preference or save or volume' tests/storage/test_repositories.py --basetemp=/tmp/av-actor-saving`.
Expected: exit 0; delayed older save cannot overwrite a newer remembered publication and resolution+volume durable fields both survive.

- [ ] Implement video-only admission ordering and confirmed close transitions.

Keep the legacy branch byte/behavior equivalent. Capable no-stream audio decision: retire old authority, set policy=false, publish Disabled, then continue start with audio off. Capable current-stream decision: in the same dispatch set policy=false FIRST, retire desired/retry/preference publication, replace audio-operation fence and mint close_operation, set audio_close_pending=true, then issue close_audio for the exact handle through Route.audio. Leave previous audio state until current close Ok(None). On that success clear pending and publish Disabled/muted=false/active when video streaming. On refusal/timeout preserve policy=false/pending/audio state, emit one fixed audio_volume_failed with reason close_unconfirmed, allow repeated StartVideoOnly and Stop, reject RetryAudio/SetVolume/SetMuted. No stale public close/volume result can confirm anything.

Add close_during_retry: rejected start→held Retry→video-only, inspect policy=false before any ack, hold/late complete level, refuse/time out first close, repeat close, then current close confirmation. Include reconnect and virtual replacement before old closure acknowledgement; next connect must carry audio off before capture. A timed-out close's eventual ack is ignored and a fresh explicit close must confirm. Test quiescence via the Go proof, not by interpreting audio_closed alone as a receipt. All capable close paths keep video/display and do not cancel video context.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py -k 'close or video_only or replacement or loss or volume' --basetemp=/tmp/av-actor-close`.
Expected: exit 0; policy admission is immediate, Disabled confirmation is delayed, and no ordinary Retry is admitted under a closed requested policy.

- [ ] Reinstall, run the complete audio/fencing suite and commit authority.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py tests/session/test_fencing.py tests/session/test_actor_fences.py tests/session/test_reconnect.py tests/session/test_virtual_and_resolution.py --basetemp=/tmp/av-audio-authority
git diff --check
git add src/airplay_linux/session/audio.py src/airplay_linux/session/engine.py src/airplay_linux/session/fencing.py tests/session/test_audio_lifecycle.py tests/session/test_fencing.py tests/session/test_actor_fences.py
git commit -m "feat(session): own acknowledged volume, worker drain and audio closure"
```

Expected: install/pytest/diff check exit 0; legacy audio/mute/video-only and capable authority matrices pass.

## Task 10: Project the acknowledged volume row and dispatch typed UI requests

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `src/airplay_linux/ui/state.py:136-167,361-395,423-675,867-875,1049-1061`.
- Modify: `src/airplay_linux/ui/window.py:15-60,183-232,415-443,473-495,502-517,675-693,762-767,799-802,831-850`.
- Modify: `src/airplay_linux/ui/presenter.py:30-95,289-421,617-640,752-785,902-930,1141-1157,1477-1531`.
- Test/Modify: `tests/ui/test_state_projection.py:1-1832`, `tests/ui/test_dashboard_widgets.py:1-2090`, `tests/ui/test_presenter.py:1-2226`.

**Interfaces:**

- Consumes: Task 2 `CapabilityReport.volume: bool`, snapshot remembered/effective/last-audible/close-pending/save-failed fields; `SetVolume(receiver_id: ReceiverId, level: VolumeLevel)` through ManagerPort.dispatch only; unchanged PreferenceAccess and ReceiverStore read APIs.
- Produces: PaneState `volume_supported: bool=False`, `volume_enabled: bool=False`, `volume_level: int | None=None`, `volume_readout: str="—"`, `volume_tooltip: str=""`, `volume_save_failed: bool=False`.
- Produces: `MainWindow.volume_requested = Signal(int)`, `_render_volume(self, pane: PaneState) -> None`, volumeRow/volumeLabel/volumeSlider/volumeReadout objects; `_set_mute_affordance(self, *, muted: bool, volume_supported: bool = False) -> None` retains icons/names with capability copy.
- Produces: `Presenter._on_volume_requested(self, level: int) -> None`; `_start_request(self, receiver_id: ReceiverId, mode: Mode) -> StartRequest | None` uses continuity-approved remembered volume only for capable enabled start; coalesced `_load_saved(self) -> None` refreshes latest validated cached records after fresh snapshots.

- [ ] Add projection matrices with exact disabled precedence/readouts/copy.

Parameterize capability absent, no selection (including saved offline), no stream/nonstreaming, audio off/unconfirmed/close-pending/decision, cleanup, trust and wrong phase, with overlapping reasons to prove precedence and every condition's blocking effect. Expected reasons are exactly:

```text
The installed backend does not support receiver volume.
Select a receiver to change its volume.
Start a stream to change receiver volume.
Receiver volume is unavailable while audio is off or unconfirmed.
Wait for cleanup before changing receiver volume.
Resolve receiver trust before changing its volume.
Set this receiver's volume; 0% mutes it.
```

Readouts: known positive decimal+%, zero Mute, unknown/no-selection/fresh replacement U+2014 em dash. Pending/refused/disabled/loss keeps historical acknowledged readout while disabled; desired thumb position is never applied evidence. Save failure keeps the numeric/Mute readout and notice `Applied, but could not be saved.`. Under pending close/policy=false retain video_only/Stop and suppress RetryAudio/slider/speaker. Keep legacy action projections unchanged.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/ui/test_state_projection.py -k volume --basetemp=/tmp/av-ui-projection-red`.
Expected: exit 1 for absent PaneState fields/projection.

- [ ] Implement the strict projection and capability-derived mute tooltip.

Add PaneState defaults and project using this exact first-match predicate (local `inputs`, `selected`, `state`, `enabled` and `reason` are the existing projection's values):

```python
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

Reuse the existing mute trust constraints. Copy only e to numeric/readout, never r/desired/last-audible as an invented effective value.

Use these exact tooltips while preserving accessible names and icons:

| Backend/state | Tooltip |
| --- | --- |
| capable audible | Mute this receiver's audio for this session. Unmute restores its last audible level. |
| capable muted | Audio is muted for this receiver. Activate to restore its last audible level. |
| legacy audible | Mute this receiver's audio for this session only; the receiver keeps its own volume setting. |
| legacy muted | Audio is muted for this receiver. Activate to unmute; muting is receiver-local and lasts for this session only. |

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/ui/test_state_projection.py --basetemp=/tmp/av-ui-projection-green`.
Expected: exit 0 with exact precedence and acknowledged history retained.

- [ ] Add and verify the always-visible native widget row immediately after primary actions.

```python
self._volume_row = QWidget(pane)
self._volume_row.setObjectName("volumeRow")
volume_layout = QHBoxLayout(self._volume_row)
volume_layout.setContentsMargins(0, 0, 0, 0)
self._volume_label = QLabel("Volume", self._volume_row)
self._volume_label.setObjectName("volumeLabel")
self._volume_slider = QSlider(Qt.Orientation.Horizontal, self._volume_row)
self._volume_slider.setObjectName("volumeSlider")
self._volume_slider.setRange(0, 100)
self._volume_slider.setSingleStep(1)
self._volume_slider.setPageStep(10)
self._volume_slider.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
self._volume_slider.setAccessibleName("Receiver volume")
self._volume_label.setBuddy(self._volume_slider)
self._volume_readout = QLabel("—", self._volume_row)
self._volume_readout.setObjectName("volumeReadout")
self._volume_readout.setAccessibleName("Receiver volume level")
volume_layout.addWidget(self._volume_label)
volume_layout.addWidget(self._volume_slider, 1)
volume_layout.addWidget(self._volume_readout)
layout.addWidget(self._volume_row)
```

Add Signal(int), wire valueChanged once to a handler which emits only when the projected slider is enabled, and place tab order immediately after muteToggle. `_render_volume` holds QSignalBlocker across setValue/range/setup; release the blocker in exception-safe scope, not a prematurely destroyed temporary. Set level or neutral thumb 0, accessible description `Level unknown` only when unknown, exact projected readout, tooltip and enablement. No-selection renderer uses current report for the first two reasons and keeps row visible. Render activity through the existing registered-code renderer without raw fields.

Add offscreen qtbot tests for exact row order/names, 0..100/single1/page10, visibility, accessible names/buddy/tab chain, arrows/page/Home/End and clamps, disabled wheel/keyboard/no bypass, unknown thumb/readout, requested position never replacing acknowledged readout and QSignalBlocker restored even when setup throws. Extend existing mute/keyboard/choice tests with both backend kinds.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/ui/test_dashboard_widgets.py --basetemp=/tmp/av-volume-widgets`.
Expected: exit 0; no render-time volume_requested emission and every declared keyboard action works offscreen.

- [ ] Wire typed presenter dispatch, validated startup preference and coalesced cache refresh.

Connect the new signal once in Presenter.start. `_on_volume_requested` rejects disposed/shutting-down/no-selection/disabled, validates VolumeLevel, captures selected receiver id and schedules SetVolume through existing `_dispatch`; no transport/preference writes here. Add SetVolume to COMMAND_TYPES and manager-only tests. `_start_request` looks up cached record via PreferenceAccess and populates volume only if report.volume and requested audio policy enabled; never borrow a changed/replaced receiver's cached r.

Coalesce saved-record reload after every fresh snapshot publication that may reflect a successful preference write. Retain a publication generation and pending-refresh flag: a read crossed by another fresh publication reruns once against latest state. Check selected identity/current continuity before updating each cache entry; stale reads cannot overwrite newer record data or another receiver. Actor success publishes a snapshot even when other fields compare equal. Record reload/render/selection must never send SetVolume. Test disabled malicious signals, changing selected receiver during scheduled work, fresh/invalid saved preference starts and delayed reload across two saves/selection replacement.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/ui/test_presenter.py --basetemp=/tmp/av-volume-presenter`.
Expected: exit 0; only typed commands for the captured receiver reach ManagerPort and latest preference cache wins.

- [ ] Reinstall, verify the UI selection and commit.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/ui/test_state_projection.py tests/ui/test_dashboard_widgets.py tests/ui/test_presenter.py --basetemp=/tmp/av-volume-ui
git diff --check
git add src/airplay_linux/ui/state.py src/airplay_linux/ui/window.py src/airplay_linux/ui/presenter.py tests/ui/test_state_projection.py tests/ui/test_dashboard_widgets.py tests/ui/test_presenter.py
git commit -m "feat(ui): expose acknowledged receiver volume and safe typed controls"
```

Expected: install/pytest/diff check exit 0; no installed application run is needed.

## Task 11: Prove composed receipts, live-failure recovery and complete offline regression

**Implementer tier:** Capable
**Lane:** audio-volume

**Files:**

- Test/Modify: `tests/session/test_audio_lifecycle.py:1-1175` (owning composed fixtures/tests).
- Test/Modify: `tests/backend/test_carried_source_patch.py:1-216` (Task 1 trace helper consumed by composed tests).
- Test/Modify: `tests/environment/test_installed_contracts.py:30-167`.
- Test/Inspect: `tests/domain/test_contracts.py:68-317`, `tests/backend/test_control.py:181-316,569-740`, `tests/backend/test_contract.py:577-598,834-880`, `tests/transport/test_doubletake.py:1204-1242,1588-1601,1769-1941,2344-2378` (inventories updated by owning earlier tasks).
- Test/Inspect: `tests/storage/test_atomic_records.py`, `tests/storage/test_repositories.py`, `tests/diagnostics/test_safety.py`, `tests/testing/test_fakes.py`, `tests/ui/test_presenter.py` (exact inventories already updated in Tasks 2–10).

**Interfaces:**

- Consumes: real `SessionManager(deps: ManagerDeps, handlers: Mapping[Route, Handler])`; `DoubleTakeTransport.start_stream(self, target: ReceiverTarget, source: CaptureSource | None, audio_policy: AudioPolicy, context: OperationContext, *, initial_volume: VolumeLevel | None = None) -> Result[StreamHandle]`, `start_audio(self, handle: StreamHandle, context: OperationContext) -> Result[AudioStartReceipt]`, `set_volume(self, handle: StreamHandle, level: VolumeLevel, context: OperationContext) -> Result[int]`, `close_audio(self, handle: StreamHandle, context: OperationContext) -> Result[None]`.
- Consumes: `PublishedDoubletakeControl.request(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None, *, dispatch_fence: DispatchFence | None = None, ownership_proof: OwnershipProof | None = None) -> Result[BackendReply]` over scripted process/evidence/daemon/capture/discovery/clock boundaries; `DispatchFence = Callable[[], Result[None]]`; new successful live replies are StatusReply.
- Consumes: `ReceiverPreferenceWriter.update(self, record_set: ReceiverRecordSet, *, remembered_resolution: ResolutionDecision | None = None, remembered_volume: VolumeLevel | None = None, still_current: Callable[[], bool]) -> Result[SavedReceiver]`; `StartRequest.volume: VolumeLevel | None`; `SetVolume(receiver_id: ReceiverId, level: VolumeLevel)`; event `VolumePayload(level: int, ok: bool)`; receipt `AudioStartReceipt(audio: bool, volume_state: str | None = None, level: int | None = None)`.
- Consumes: test-only `carried_volume_status_traces() -> tuple[dict[str, object], ...]` from tests/backend/test_carried_source_patch.py, which executes exact Go production handlers/status serialization and returns actual envelopes, including trace name/quiescent flag/status.
- Produces: exact required tests `test_manager_transport_control_start_volume_receipt`, `test_live_volume_rejection_never_becomes_a_startup_refusal`, `test_foreign_audio_close_is_never_an_enabling_receipt`, `test_repeated_retry_keeps_the_occupied_worker_fence` in tests/session/test_audio_lifecycle.py.
- Produces: unchanged legacy `PUBLISHED_OPERATIONS` plus a separate capable expected set `PUBLISHED_OPERATIONS | {BackendOperation.volume, BackendOperation.audio_off}` selected only when features.target_volume; no private gate inference or live test opt-in.

- [ ] Assemble the real manager→transport→typed-control fixture inside the owning audio test module.

Use SessionRig discovery/capture/audio/store/activity/time and ManagerDeps with real transport substituted; use scripted control process/evidence/registry, not FakeTransport, for these composed cases. The scripted process records exact argv and returns the actual published JSON shape, not application-contract acknowledgements. Use the already-tested transport harness design without importing a second test module's mutable global rig. Keep fake-only authority matrices independent. Provide local capable/legacy fixtures and held status/volume/close barriers; expose process argv and status-read count so assertions prove the existing one-read audio-start carrier and no extra polling owner.

Parameterize `test_manager_transport_control_start_volume_receipt` over applied-positive, explicit applied-zero, refused, pending, missing and legacy. Start with StartRequest(volume=VolumeLevel(value=30)); use explicit VolumeLevel(0) for zero with r absent. Connect acknowledges connecting, then release streaming/source readiness and the existing audio-start status. Assert exact capable/legacy tuple, actor state/stage/e/a/mute and no early audio_recovered. Refused Retry sends volume30 on the same handle; Stop/replacement before receipt completion makes late state/diagnostic/activity/save silent.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py::test_manager_transport_control_start_volume_receipt --basetemp=/tmp/av-composed-start`.
Expected: exit 0 for all six parameter cases. If an integrated case is already green, record that truth and verify the assertions use real manager/control calls rather than fabricating a RED.

- [ ] Feed actual carried-Go status traces into composed recovery and test foreign closure.

Import the local proof module via an explicit file-based test import or module-local sys.path insertion restricted to tests/backend, then call carried_volume_status_traces; it executes/caches the exact extracted production serializer. Script these returned status envelopes as control replies:

```text
no remembered level -> slider 30 acknowledged -> slider 40 complete rejection
    -> audio loss -> Retry start_audio same handle -> applied/30 -> Enabled/e30
remembered startup -> same trace -> no requirement that live recovered level equal initial_volume
first live rejection on unrequested startup -> unrequested/absent -> ordinary recovery semantics
newer queued value -> pending with historical 30 -> retryable decision preserves e/a -> queued 200 applied
partial setup 200 then refusal -> refused with partial percent -> rejected Retry stays refused -> later 200 recovers
```

`test_live_volume_rejection_never_becomes_a_startup_refusal` owns all variants and asserts no fake-produced status is substituted for the three required production traces. A quiescent live rejection never produces refused/pending, and applied always has a level. Add `test_foreign_audio_close_is_never_an_enabling_receipt`: local enabled policy, matching actual-status shape audio_closed=true, recovery read returns audio_unavailable; no enabling receipt/audio_recovered/confirmed Disabled, and video stays running.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py::test_live_volume_rejection_never_becomes_a_startup_refusal tests/session/test_audio_lifecycle.py::test_foreign_audio_close_is_never_an_enabling_receipt tests/backend/test_carried_source_patch.py --basetemp=/tmp/av-composed-recovery`.
Expected: exit 0; producer serialization and Python recovery interpretation agree on all named traces.

- [ ] Prove repeated Retry keeps its occupied audio fence and finish exact operation inventories.

Add `test_repeated_retry_keeps_the_occupied_worker_fence`: refused startup, hold Retry, expire public wait, request Retry again while occupied, settle original task, then acknowledge the single subsequent dispatch. Assert original settlement was accepted, one desired value drained, no parallel worker, recovery succeeds, and the complete-rejection variant returns to usable audio_decision. Preserve the legacy `test_stale_audio_retry_completion_cannot_overwrite_a_newer_retry` separately. Also run zero→loss→zero-recovery→positive consistency with snapshot/transport/runtime/audio-state mute representations and stale id/lease/endpoint/daemon/trust matrices.

Update tests/environment/test_installed_contracts.py's profile mapping test to choose the extra operation set only for proven target_volume and to supply the new typed payloads. Keep old PUBLISHED_OPERATIONS constant and private-gate assertions; new read-only observation tests may inspect help/capabilities only and must not start the installed app or a daemon. Verify all §11.4 inventories remain exact: enums/closed unions/ports, all operations/fixture tags/fence cases, no-volume source fixtures, receiver v1/v2 bytes, fake method inventory, presenter COMMAND_TYPES and seventh immutable diagnostic capability flag.

Run: `/tmp/airplay-volume-venv/bin/python -m pytest -q tests/session/test_audio_lifecycle.py::test_repeated_retry_keeps_the_occupied_worker_fence tests/environment/test_installed_contracts.py -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-retry-inventory`.
Expected: exit 0; installed surface checks are read-only, and existing missing-installed-tool skips are confined to those optional observation tests, never the mandatory Go proof.

- [ ] Reinstall and run the complete required focused suite.

```bash
/tmp/airplay-volume-venv/bin/python -m pip install --no-index --no-build-isolation --no-deps .
/tmp/airplay-volume-venv/bin/python -m pytest -q -m 'not live_backend and not live_kde and not live_hardware' tests/domain tests/backend tests/process/test_ownership.py tests/transport tests/session tests/storage tests/ui tests/diagnostics tests/policy tests/testing tests/environment/test_installed_contracts.py tests/adapter_contracts/test_transport_contract.py --basetemp=/tmp/av-volume-focused
```

Expected: exit 0; all new and legacy selected tests pass, live markers deselected. Record actual pass/skip/deselection counts and commands; no invented count is an acceptance target.

- [ ] Run the existing full offline suite once after the focused suite passes.

```bash
/tmp/airplay-volume-venv/bin/python -m pytest -q -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-full
git diff --check
```

Expected: both exit 0, with no live boundary attempted and mandatory carried Go proof executed. Any failure is repaired in its owning path/task scope and the affected selection rerun before repeating the full gate; do not downgrade failures to skips or alter tripwires. Report observed counts and limitations rather than receiver acceptance.

- [ ] Commit the composed proof and exact environment inventory.

```bash
git add tests/session/test_audio_lifecycle.py tests/backend/test_carried_source_patch.py tests/environment/test_installed_contracts.py
git commit -m "test(audio): prove composed volume receipts and occupied-worker recovery"
```

Expected: one test-only commit; required full offline suite evidence exists before documentation/delivery.

## Task 12: Document operational behavior, coherent compatibility and carried provenance

**Implementer tier:** Standard
**Lane:** audio-volume

**Files:**

- Modify: `docs/operations.md:22-109`.
- Modify: `docs/backend-fork.md:1-7,53-114,158-205`.
- Modify: `docs/backend-compatibility.md:1-73,143-156`.
- Inspect/Test: `tests/backend/test_contract.py:568-575`, `tests/environment/test_installed_contracts.py:1-244`.

**Interfaces:**

- Consumes: `features_from_help(sender_help: str, ctl_help: str, *, advertised: frozenset[str] = frozenset()) -> BackendFeatures`; `decode_status(value: object) -> StatusReply | Err`; sender `doubletake -capabilities -> (exit=0, stdout=b"capabilities-v1\nstatus-source\nstatus-source-complete\ntarget-volume\n")` and the three exact extended ctl rows carried in patch 0004.
- Produces: three documents accurately distinguishing offline implementation proof, prepared build identity, installed identity and live receiver observations; no installed/readback claim follows from an acknowledgement.
- Produces: patch 0004 digest/base/ordered-chain and mandatory offline-proof provenance in docs/backend-fork.md, measured with sha256sum. Rebuild/install/rollback commands and measured binary receipt belong to the separate delivery task, so this task does not depend on a future task's text or unbuilt hashes.

- [ ] Rewrite affected operational and compatibility explanations around the current proven boundaries.

In operations document the always-visible row, slider0=mute, positive acknowledgement/save, remembered per-receiver reapply, speaker restore a/r/100, unknown readout, save failure, legacy disabled reason and session-local mute. Describe complete startup refusal/retry on same stream, pending/missing receipt decision, broken setup ordinary cleanup, live failure keeping video, and video-only policy admission with confirmed close required for Disabled. State remote controls are not sensed; acknowledgement cannot prove the receiver honored audible gain; 50% is -6.020600 dB amplitude, not half perceived loudness. No new warnings, actions or capabilities beyond the spec.

In compatibility document both exact tables, their independent scope proofs and tokens; incoherent half-installed pair refuses the whole backend, legacy saved levels never send flags, source completeness still needs its existing evidence, profiles/sender flags/private gates unchanged. Keep pinned object/peeled-source historical provenance tests intact. Remove or qualify stale present-tense statements in the edited sections that conflict with today's receipt-backed composition; retain historical observations as dated evidence, without inventing new discovery/display work or live acceptance.

Run: `rg -n 'target-volume|last audible|Audio is withheld|could not be saved|acknowledg|legacy' docs/operations.md docs/backend-compatibility.md`.
Expected: matches explain those operator behaviors and boundaries in the two documents; no fabricated installation receipt.

- [ ] Document carried patch 0004, the exact offline proof commands and measured patch provenance.

In backend-fork add patch file scope, base+0001–0003 ordering, mandatory source/volume producer proofs, Go toolchain/native build environment and four capability tokens. Preserve dated prior install hashes as history. State that this source change has not yet produced an installation receipt; keep the later prepared/installed distinction explicit. Record the supplied base object and measured peeled commit, the four patch digests and the implementation commands/outcomes already observed. Binary rebuild/install/rollback instructions and measured build identity are authored together in the next delivery task, after its artifacts exist. Never invent a digest or reuse an earlier build hash for this new source.

```bash
git -C /tmp/doubletake-pr76-audit.G2sjMn rev-parse 'c896e3689ab85c3ed520e502308dedfbb5a4c221^{commit}'
sha256sum vendor/doubletake/0001-capture-report-selected-source.patch vendor/doubletake/0002-status-report-selected-source.patch vendor/doubletake/0003-capability-tokens.patch vendor/doubletake/0004-target-volume.patch
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_carried_source_patch.py --basetemp=/tmp/av-volume-doc-proof
```

Expected: the pinned peeled commit, four concrete SHA-256 lines and a passing mandatory source/volume producer proof. Put the observed digest/base/chain and exact command/outcome in the new documentation entry.

- [ ] Check the documented surface against contract tests and commit the documentation.

```bash
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_contract.py tests/environment/test_installed_contracts.py -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-docs
git diff --check
git add docs/operations.md docs/backend-fork.md docs/backend-compatibility.md
git commit -m "docs(audio): describe acknowledged volume and paired backend delivery"
```

Expected: pytest/diff check exit 0; one documentation commit. Task 13 supplies measured rebuild/installation receipt without asserting an unexecuted operator action.

## Task 13: Prepare the rebuilt helper pair and receipt the operator installation

**Implementer tier:** Advanced
**Lane:** audio-volume

**Files:**

- Modify: `docs/backend-fork.md:76-205` (Task 12 build section/dated receipt; use named anchors after line shifts).
- Modify: `docs/backend-compatibility.md:15-73` (measured prepared/installed surface status only).
- Modify: `docs/operations.md:22-109` (delivery status/revert cross-reference only).
- Operator-installed external paths, only with separate delivery authorization: `/home/bin/doubletake`, `/home/bin/doubletake-ctl`; backups `/home/bin/doubletake.prev`, `/home/bin/doubletake-ctl.prev`.
- Disposable build/receipt artifacts: a new `/tmp/av-delivery.XXXXXX` directory, never the read-only reference or PM runtime.

**Interfaces:**

- Consumes: validated application/patch commits and full offline evidence from Tasks 1–12; base `c896e3689ab85c3ed520e502308dedfbb5a4c221`, ordered carried patches and local upstream clone source `/tmp/doubletake-pr76-audit.G2sjMn`.
- Produces: rebuilt executable pair from the same postimage, measured digest/toolchain/base/command receipt in the three docs; sender `-capabilities` exact four-line stdout and ctl `--help` exact extended table, with no daemon/socket/receiver action.
- Produces: operator-only pair replacement retaining the previous pair as `.prev`, verifying both installed hashes, and the exact two-file rollback below. Prepared-only completion is recorded as `prepared, not installed`; a receipt says `installed` only after separately authorized installation and verification actually exit 0.

This task is the installation/receipt handoff, not implicit delivery authorization. Its automatic portion ends with a built, checked pair and concrete receipt/runbook. Author the complete build, prepared verification, pair-install, installed observation and rollback command groups below verbatim in docs/backend-fork.md, replacing shell-variable explanations with the actual measured build directory in the receipt. If no separate operator authorization exists, the implementation controller completes that preparation and records installation as pending; it must not execute the installation or rollback blocks. No application termination, launch, receiver contact or session manipulation is part of either portion.

- [ ] Build a fresh exact patched pair offline and collect concrete provenance.

```bash
volume_repo="$PWD"
volume_delivery=$(mktemp -d /tmp/av-delivery.XXXXXX)
git clone --no-hardlinks --no-checkout /tmp/doubletake-pr76-audit.G2sjMn "$volume_delivery/source"
git -C "$volume_delivery/source" checkout --detach c896e3689ab85c3ed520e502308dedfbb5a4c221
git -C "$volume_delivery/source" rev-parse 'c896e3689ab85c3ed520e502308dedfbb5a4c221^{commit}'
for volume_patch in 0001-capture-report-selected-source.patch 0002-status-report-selected-source.patch 0003-capability-tokens.patch 0004-target-volume.patch; do
    git -C "$volume_delivery/source" apply --check --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
    git -C "$volume_delivery/source" apply --whitespace=error-all "$volume_repo/vendor/doubletake/$volume_patch" || exit 1
done
export PATH="/home/henry-arch/.local/go/bin:$PATH" CGO_ENABLED=1 GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off GOPATH=/tmp/gopath GOMODCACHE=/tmp/gopath/pkg/mod GOFLAGS=-mod=readonly
cd "$volume_delivery/source"
go version
go env CGO_ENABLED GOPROXY GOSUMDB GOTOOLCHAIN GOWORK GOPATH GOMODCACHE GOFLAGS
pkg-config --modversion gstreamer-1.0 gstreamer-app-1.0
go build -trimpath -buildvcs=false -o "$volume_delivery/doubletake" ./cmd/doubletake
go build -trimpath -buildvcs=false -o "$volume_delivery/doubletake-ctl" ./cmd/doubletake-ctl
go test -count=1 -run '^TestAudioVolumeBody$' ./internal/airplay
cd "$volume_repo"
sha256sum vendor/doubletake/0001-capture-report-selected-source.patch vendor/doubletake/0002-status-report-selected-source.patch vendor/doubletake/0003-capability-tokens.patch vendor/doubletake/0004-target-volume.patch "$volume_delivery/doubletake" "$volume_delivery/doubletake-ctl"
```

Expected: all patch/build/mapping commands exit 0 using cached modules; measured Go/native versions and six digest lines exist. A missing toolchain/module/native library is a reported preparation failure, never permission to download or build in the reference directory. Record the actual absolute volume_delivery path for the operator.

- [ ] Verify the prepared binaries through help/capabilities only and record an uninstalled receipt.

```bash
python3 - "$volume_delivery" <<'PY'
import pathlib
import subprocess
import sys
root = pathlib.Path(sys.argv[1])
cap = subprocess.run([str(root / "doubletake"), "-capabilities"], capture_output=True, timeout=5)
assert cap.returncode == 0, cap.returncode
assert cap.stdout == b"capabilities-v1\nstatus-source\nstatus-source-complete\ntarget-volume\n"
assert cap.stderr == b""
ctl = subprocess.run([str(root / "doubletake-ctl"), "--help"], capture_output=True, timeout=5)
assert ctl.returncode == 2, ctl.returncode
assert ctl.stdout == b""
rows = [
    b"  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)",
    b"  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)",
    b"  audio off [target]                  Close mirrored audio (all streams, or only the given IP)",
]
positions = [ctl.stderr.index(row) for row in rows]
assert positions == sorted(positions)
assert positions[-1] < ctl.stderr.index(b"  pin <PIN-or-password>")
print("prepared pair: exact four-token and target-scoped help contracts verified")
PY
```

Expected: exit 0 and `prepared pair: exact four-token and target-scoped help contracts verified`. The ctl preserves its existing `flag.ContinueOnError` behavior: `--help` prints usage to stderr and main exits 2; the verification script deliberately accepts exactly 2. No command operand is supplied. Add actual base/patch/binary hashes, commands, versions, exact stdout/exit checks and `prepared, not installed` to backend-fork.md; compatibility/operations link that receipt. Offline proof cannot establish audible behavior, receiver acceptance or installed state.

- [ ] Present the following concrete pair-install command to the separately authorized operator and receipt its actual result if executed.

The operator uses the measured build directory recorded above as `volume_delivery` in the same shell. Installation copies both old binaries to the exact `.prev` paths before either replacement, stages both new binaries, and rolls both current files back on a replacement error. This command contains no process stop/start or service action:

```bash
python3 - "$volume_delivery" <<'PY'
from pathlib import Path
import hashlib
import os
import shutil
import sys
build = Path(sys.argv[1])
bin_dir = Path("/home/bin")
names = ("doubletake", "doubletake-ctl")
for name in names:
    assert (build / name).is_file()
    assert (bin_dir / name).is_file()
old_hashes = {name: hashlib.sha256((bin_dir / name).read_bytes()).hexdigest() for name in names}
new_hashes = {name: hashlib.sha256((build / name).read_bytes()).hexdigest() for name in names}
for name in names:
    shutil.copy2(bin_dir / name, bin_dir / (name + ".prev"))
for name in names:
    shutil.copy2(build / name, bin_dir / (name + ".volume-new"))
    os.chmod(bin_dir / (name + ".volume-new"), 0o755)
try:
    for name in names:
        os.replace(bin_dir / (name + ".volume-new"), bin_dir / name)
    for name in names:
        assert hashlib.sha256((bin_dir / name).read_bytes()).hexdigest() == new_hashes[name]
except BaseException:
    for name in names:
        shutil.copy2(bin_dir / (name + ".prev"), bin_dir / (name + ".volume-restore"))
        os.replace(bin_dir / (name + ".volume-restore"), bin_dir / name)
    raise
for name in names:
    print(name, "previous=" + old_hashes[name], "installed=" + new_hashes[name])
PY
```

Expected if authorized/executed: exit 0, two concrete previous/installed hash lines matching the measured prepared artifacts, and both `.prev` files retain the immediately prior pair. If it fails before replacement, leave current binaries intact; if replacement fails, the block restores both. Do not classify an existing running daemon as updated merely because executable paths changed. Operator delivery waits for an ordinary separately managed next application start; this task never manipulates a running session or PM.

- [ ] Verify the installed observation surface only after authorized installation, or retain the explicit pending status.

```bash
sha256sum /home/bin/doubletake /home/bin/doubletake-ctl /home/bin/doubletake.prev /home/bin/doubletake-ctl.prev
/home/bin/doubletake -capabilities
/home/bin/doubletake-ctl --help
```

Expected if installed: hash pair equals the prepared pair, sender exits 0 with exactly the four tokens, ctl exits 2 with volume/audio/extended connect rows. Record these observed values/exits, backup hashes, authorization reference and installation time in backend-fork.md and update the two linked status descriptions. Without authorization this step is unexecuted and the recorded status remains `prepared, not installed`; neither offline tests nor old historical hashes stand in for this receipt.

- [ ] Supply and document this exact operator rollback path without running it during preparation.

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib
import os
import shutil
root = Path("/home/bin")
names = ("doubletake", "doubletake-ctl")
for name in names:
    assert (root / (name + ".prev")).is_file()
for name in names:
    shutil.copy2(root / (name + ".prev"), root / (name + ".volume-restore"))
for name in names:
    os.replace(root / (name + ".volume-restore"), root / name)
for name in names:
    assert (root / name).read_bytes() == (root / (name + ".prev")).read_bytes()
    print(name, hashlib.sha256((root / name).read_bytes()).hexdigest())
PY
/home/bin/doubletake -capabilities
/home/bin/doubletake-ctl --help
```

Expected if operator elects rollback: both restored hashes equal their recorded previous hashes and the preceding pair's help/capability surface returns; legacy lacks target-volume, so the application disables the row and retains mute behavior. Restore both files, never downgrade one side only. No restart, receiver or session action is included. Record rollback only when actually executed.

- [ ] Validate and commit the measured delivery documentation.

```bash
/tmp/airplay-volume-venv/bin/python -m pytest -q tests/backend/test_contract.py tests/environment/test_installed_contracts.py -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-delivery
git diff --check
git add docs/backend-fork.md docs/backend-compatibility.md docs/operations.md
git commit -m "docs(backend): receipt the prepared volume helper pair and rollback"
```

Expected: pytest/diff check exit 0 and one measured receipt commit; commit text/body states the observed installation status accurately. Full offline application regression evidence from Task 11 and prepared binary verification remain distinct from operator installation and any future live receiver acceptance.
