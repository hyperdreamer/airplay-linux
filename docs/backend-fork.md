# Carrying source reporting and target audio control for `doubletake`

## Why we carry a patch

Mirror mode promises to send **one chosen physical monitor**. Upstream `doubletake` reads the
selected stream's size from the ScreenCast portal reply and discards the other source metadata;
its daemon `status` reply exposes no captured-source information. The carried chain retains the
portal's `source_type`, `size` and `position` per capture group and reports them per stream, so the
application has evidence to compare with compositor outputs. Refusing to guess is a design rule.

The fourth patch adds acknowledged receiver volume and per-stream audio closure. It preserves
the source chain, public profiles, source checks, ownership/trust authority and legacy behavior
when the volume surface is absent. An RTSP acknowledgement records the application's requested
level; it cannot establish audible behavior or observe a receiver remote.

That evidence is partial in its guarantees. Portal `source_type=1` means MONITOR; on KDE this also
covers regions and existing virtual outputs, so `IsMonitor()` does not prove that a source is
physical or owned by us. Portal size and position use compositor coordinates, which need not be
pixels. `x` and `y` are usable only when `has_position` is true: an absent or malformed position
must remain distinct from a genuine `(0,0)`. The sender must correlate metadata with compositor
output identity and ownership, refusing ambiguous matches and any output our own helper created.

We raised source reporting upstream as `omarroth/doubletake#76`. It was closed once because the
product needed the capability without waiting for review; the maintainer then said they were open
to merging it and asked for reporting **per stream**. The reopened PR has that shape: the source
belongs to the stream's own capture group, so different captures report different sources and
streams sharing one capture report that capture's source.

## Historical upstream status (2026-10-31)

The recorded PR-76 head is `2b5f1b0`, with four commits (capture layer, parser tests, per-stream
status, and the optional-position/production-handoff follow-up), based on `main` at `5e2f1cb`.
The recorded fork branch is `streaminfo-source`; the follow-up was pushed as a descendant commit
to preserve the reopened PR's history. This is historical provenance, not a fresh upstream check.

PR-76 can report partial metadata, with `has_position=false` when position is unavailable. The
reconciled local chain below is deliberately stricter: it omits `source` unless type, positive
dimensions and both signed coordinates are valid. It preserves PR-76's lowercase JSON keys,
explicit position availability and per-stream attribution. It is therefore **not byte-identical**
to PR-76 and requires the third local capability token before application activation.

## What the patches do

`vendor/doubletake/0001-capture-report-selected-source.patch`

- adds `airplay.SourceInfo{Type, Width, Height, X, Y, HasPosition}` with `IsMonitor()`;
- parses `source_type`, `position` and `size` from the ScreenCast portal reply;
- invokes the callback only when type, positive dimensions and both signed coordinates are valid;
  missing or malformed position is never converted to `(0,0)`;
- serialises lowercase keys `type`, `width`, `height`, `x`, `y`, `has_position`;
  `HasPosition` is set true only after a valid coordinate pair, including a genuine `(0,0)`;
- adds optional `CaptureConfig.OnSource func(SourceInfo)`; nil keeps existing behaviour;
- covers both portal paths through the single request site in `PrepareCapture`;
- calls `OnSource` after portal selection, before opening the PipeWire remote, so the callback
  does not prove that preparation or capture subsequently succeeded.

`vendor/doubletake/0002-status-report-selected-source.patch` (stacks on 0001)

- retains the complete decoded source in `CapturePreparation.Source()`;
- records it on `videoCaptureGroup` under the daemon mutex;
- reports optional `StreamInfo.Source` in `ctl status` from **that stream's own capture group**;
- omits `source` for incomplete metadata and the legacy capture path;
- retains comments explaining that metadata alone does not prove physical identity or ownership.

`vendor/doubletake/0003-capability-tokens.patch` (stacks on 0001+0002)

- adds `-capabilities`, printing exactly `capabilities-v1`, `status-source`,
  `status-source-complete`, one token per line in that order, and exiting 0 before constructing a daemon;
- patch 0004 extends this advertisement with `target-volume` as the fourth line.

`vendor/doubletake/0004-target-volume.patch` (stacks on 0001+0002+0003)

- changes `cmd/doubletake-ctl/main.go`, `cmd/doubletake/main.go`,
  `internal/airplay/client.go`, `internal/airplay/mirror.go`,
  `internal/airplay/mirror_setup_test.go`, `internal/daemon/daemon.go` and
  `internal/daemon/daemonclient/client.go`;
- adds the exact extended connect, target `volume` and `audio off` ctl rows, socket fields and
  daemon client methods; adds no sender flag, profile name, audio-on command or source selector;
- maps percent to `20*log10(percent/100)` with six decimal places (0 uses -144 dB), and accepts
  only a final RTSP 200 as a requested-level acknowledgement;
- shares two seconds across requested setup's two writes, bounds a live write to two seconds
  including mutex admission/framing/Digest retry, and bounds audio closure to two seconds total;
- preserves a usable connection on complete rejection, withholds unadmitted startup audio,
  preserves prior acknowledged live history, and uses existing setup cleanup for incomplete I/O;
- serializes each stream's latest desired level through one worker, keeps pending/refused/applied
  state tied to actual settlement, and records a one-way audio-off latch with producer joins;
- publishes optional per-stream `volume`, `volume_state` and `audio_closed` in full status replies,
  with no aggregate volume, connect receipt or receiver readback.

The Python status decoder consumes exactly the six lowercase fields above and requires
`has_position` to be the boolean `true`. Missing or false availability, malformed coordinates,
uppercase legacy objects and partial objects refuse; no missing value becomes an origin.
The typed `BackendSource` represents only complete geometry admitted by that decoder.

## Mandatory offline producer proof

The default Python suite executes both carried source and volume producers with `go test`, so
Go must be on `PATH` even when not rebuilding the backend. Missing Go deliberately fails with
actionable setup instructions. The independent Git patch-parser proof remains mandatory when
Go is unavailable; a missing tool never turns either proof into a pass or skip.

The proof extracts exact carried production declarations with standard-library-only fake
Variant/size, connection, capture and daemon boundaries. Source cases cover missing, malformed,
real-zero and signed positions. Volume cases cover mapping, two-write setup, bounded RTSP and
Digest exchanges, queue settlement, partial setup/retry and audio closure. Actual Go serializer
status traces feed the Python decoder and composed manager/transport/control recovery tests.
The proof uses `CGO_ENABLED=0`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`
and empty `GOFLAGS`; it needs no GStreamer, receiver, daemon or host portal.

## Offline patch provenance (2026-11-05)

The supplied base is annotated v0.5.0 **tag object**
`c896e3689ab85c3ed520e502308dedfbb5a4c221`. The read-only local source
`/tmp/doubletake-pr76-audit.G2sjMn` resolves it to peeled commit
`e18f85d07f74a3b9d49f448762d28f3b7feb3938`. Apply 0001 → 0002 → 0003 → 0004 to a fresh
local clone of that commit, checking each patch with `git apply --check --whitespace=error-all`
before `git apply --whitespace=error-all`. Never build in the reference clone or in
`/tmp/delivery-reconcile.GMO4Nx/reapply`.

Measured with these commands, both exit 0:

```bash
git -C /tmp/doubletake-pr76-audit.G2sjMn rev-parse 'c896e3689ab85c3ed520e502308dedfbb5a4c221^{commit}'
sha256sum vendor/doubletake/0001-capture-report-selected-source.patch vendor/doubletake/0002-status-report-selected-source.patch vendor/doubletake/0003-capability-tokens.patch vendor/doubletake/0004-target-volume.patch
```

```text
peeled commit       e18f85d07f74a3b9d49f448762d28f3b7feb3938
0001 sha256         622ecded534be335557b61332cd66d57cecfdea869abc00f5e58f6f75a84d687
0002 sha256         0927e2d24abf63f413441a3a291c545288eb09e7b3dca102d3f328bb4b764dc7
0003 sha256         482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567
0004 sha256         8fc2a133fae50c0383bdf257199e3f119377d0721baceaa8b78463a3534eb191
```

> **Superseded by the audit correction below.** That 0004 digest identifies patch 0004 as it stood
> at commit `257df34`. The final patch in this tree is different; see “Final 0004 provenance”.

The observed proof commands used this environment and the supplied local Python interpreter:

```bash
export PATH=/home/henry-arch/.local/go/bin:$PATH QT_QPA_PLATFORM=offscreen
export AIRPLAY_LINUX_LIVE_BACKEND=0 AIRPLAY_LINUX_LIVE_KDE=0 AIRPLAY_LINUX_LIVE_HARDWARE=0
export PIP_NO_INDEX=1 GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local GOWORK=off
/data/home/henry-arch/.venvs/airplay-linux/bin/python -m pytest -q tests/backend/test_carried_source_patch.py --basetemp=/tmp/av-volume-doc-proof
/data/home/henry-arch/.venvs/airplay-linux/bin/python -m pytest -q tests/backend/test_contract.py tests/environment/test_installed_contracts.py -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-docs
```

The mandatory source/volume/Git proof passed **4 tests**, exit 0. The contract/documentation
selection passed **138 tests, 2 deselected**, exit 0. The preceding application gate passed
**3,709 tests, 2 deselected** for the required focused selection, then **5,809 tests, 12
deselected** for the full offline suite, both exit 0. Its command was
`/data/home/henry-arch/.venvs/airplay-linux/bin/python -m pytest -q -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-full`.
No live boundary was admitted. The six composed startup cases already passed the integrated
implementation on first execution; the two existing diagnostics-copy failures were reproduced
before their approved map-only correction. This is offline implementation evidence.

Full native compilation uses Go with `CGO_ENABLED=1`, `GOPROXY=off`, `GOSUMDB=off`,
`GOTOOLCHAIN=local`, `GOWORK=off`, `GOPATH=/tmp/gopath`, `GOMODCACHE=/tmp/gopath/pkg/mod`
and `GOFLAGS=-mod=readonly`, plus GStreamer development libraries. The local prerequisites
report Go **1.27.3 linux/amd64** and **1.28.8** for both `gstreamer-1.0` and
`gstreamer-app-1.0`. A missing cached module/tool/library is a preparation failure, not permission
to download anything.

The measured four-patch build and operator handoff are recorded below. Historical hashes in
later sections identify only their dated builds.

## Prepared volume helper pair (2026-11-05)

Status: **prepared, not installed**. The automatic build ran from
`2026-11-05T11:45:34.796712+00:00` through `2026-11-05T11:45:43.896966+00:00`, exit **0**.
The build directory is **`/tmp/av-delivery.7OYAi0`**, its fresh patched source is
`/tmp/av-delivery.7OYAi0/source`, and the local evidence is `build.sh`, `build.log` and
`build-result.json` in that directory. The application source commit used was `257df34`,
following the composed-proof commit `1aaf0fe`.

| Item | Measured identity |
| --- | --- |
| Supplied v0.5.0 tag object | `c896e3689ab85c3ed520e502308dedfbb5a4c221` |
| Peeled source commit | `e18f85d07f74a3b9d49f448762d28f3b7feb3938` |
| 0001 SHA-256 | `622ecded534be335557b61332cd66d57cecfdea869abc00f5e58f6f75a84d687` |
| 0002 SHA-256 | `0927e2d24abf63f413441a3a291c545288eb09e7b3dca102d3f328bb4b764dc7` |
| 0003 SHA-256 | `482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567` |
| 0004 SHA-256 | `8fc2a133fae50c0383bdf257199e3f119377d0721baceaa8b78463a3534eb191` |
| `/tmp/av-delivery.7OYAi0/doubletake` SHA-256 | `a4542ba42c6da4a9553131d45b8376bd49343ca1599f3a2782e7793036f6fed4` |
| `/tmp/av-delivery.7OYAi0/doubletake-ctl` SHA-256 | `56dbf542d89d75e23dbe25c379bfa90daa77328267fec28a0270edf1b4f1bd23` |

All four ordered `git apply --check --whitespace=error-all` checks and applications passed in
the fresh local clone. Both explicit builds exited 0 using Go **1.27.3 linux/amd64**,
`CGO_ENABLED=1`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, `GOWORK=off`,
`GOPATH=/tmp/gopath`, `GOMODCACHE=/tmp/gopath/pkg/mod`, `GOFLAGS=-mod=readonly`, and GStreamer
**1.28.8** for both native packages. The mapping command passed with
`ok doubletake/internal/airplay 0.008s`, exit 0. `file` identified both artifacts as x86-64 ELF
executables, independent SHA-256 reads matched the table, and patched-source `git diff --check`
passed. No module/library download or reference-checkout modification was needed.

| Delivery step | Observed status |
| --- | --- |
| Fresh clone, patch checks, two builds, native mapping test, digest collection | Passed, exit 0 |
| Prepared sender `-capabilities` / ctl `--help` | **Unexecuted**: the user prohibits running rebuilt helpers |
| Pair installation and `.prev` backup creation | **Unexecuted**: no separate installation authorization |
| Installed hash/help/token observation | **Unexecuted**; no current installed or backup hashes claimed |
| Rollback | **Unexecuted** |
| Receiver acceptance, audible gain, remote readback | Unobserved; outside offline preparation |

### Final 0004 provenance (2026-11-05, post-audit)

The frontier audit compared the digest recorded above with the patch actually shipped in this tree
and found they differ. The record above is preserved unchanged as the history of what was prepared;
this section states what a future installation must build from.

```text
0004 sha256 (final)  d5d3359c93fcaf408e7495053de029194c47c193b5e1bf1a33d2466eafaae5c5
```

Consequences, stated plainly:

- the digest `8fc2a133…` in the measured-identity table belongs to patch 0004 at commit `257df34`;
  that revision predates the audit fixes, so the prepared pair described above does **not**
  represent the final source;
- any installation must rebuild the sender and ctl from the current tree and verify
  `d5d3359c…` first — do not install a pair prepared from the earlier revision;
- the ordered `git apply --check --whitespace=error-all` and application of the final patch against
  the peeled commit `e18f85d0…` passed during the audit, and the offline production-declaration
  proof passed against that same application, so the final patch is buildable as recorded here.

There is no authorization reference or installation time for this pair. The exact stdout/exit
conditions below are **expected checks**, not observed results. The user restriction leaves
the executable help/capability check incomplete. This receipt must retain that limitation until
an authorized operator actually runs it. The artifacts and logs are temporary; preserve this
whole directory before `/tmp` is cleared, or rebuild from the recorded source and patch chain.

### Installed final-source pair (2026-11-06)

Status: **installed and verified**. The operator instruction "Install the backend." authorized the
replacement. The pair was rebuilt from this tree's final source; nothing from
`/tmp/av-delivery.7OYAi0` (built from the superseded 0004 revision) was installed.

| Item | Measured identity |
| --- | --- |
| Supplied base archive | `codeload` tar of tag `c896e368…`, root `doubletake-c896e368…/`, 373,690 bytes |
| 0001–0003 SHA-256 | `622ecded…`, `0927e2d2…`, `482e7ec8…` (as recorded above) |
| 0004 SHA-256 | `d5d3359c93fcaf408e7495053de029194c47c193b5e1bf1a33d2466eafaae5c5` |
| `/home/bin/doubletake` SHA-256 | `3d50ab13904738c73c6eaaec11c4bce7d596348bc74c6d7f08ab4f5788c2abf6` |
| `/home/bin/doubletake-ctl` SHA-256 | `85eb4d3140f6dc5a2f8105d245027479f04da6b7d014107facd681ac7a507f12` |

The ordered `git apply --check --whitespace=error-all` and application of 0001 → 0004 to a fresh
extraction at `/tmp/dt-build/source` passed, and `git diff --check` was clean. The build used Go
**1.27.3-X:nodwarf5** (`/usr/bin/go`, package `go 2:1.27.3-1`) — this host's `/usr/local/bin/go`
symlink shadows it with **1.14.2**, so the build put `/usr/bin` first — with `CGO_ENABLED=1`,
`GOFLAGS=-mod=mod`, `GOPROXY=https://goproxy.cn,direct`, `GOSUMDB=off`, `GOTOOLCHAIN=local`,
`GOWORK=off`, `GOPATH=/tmp/dt-gopath`, `GOMODCACHE=/tmp/dt-gopath/pkg/mod` and GStreamer
**1.28.8**. The module cache was empty, so dependencies were downloaded through `goproxy.cn`;
`proxy.golang.org` timed out on this host. `go vet ./...` was clean and `go test -count=1 ./...`
passed (four tested packages, two without test files).

Observed executable evidence, not expected values:

- `/home/bin/doubletake -capabilities` exits 0 with empty stderr and exactly the four lines
  `capabilities-v1`, `status-source`, `status-source-complete`, `target-volume`;
- `/home/bin/doubletake-ctl --help` exits 2 with empty stdout and the extended
  `connect … --volume`, `volume <0-100> [target]` and `audio off [target]` rows in the
  documented order before `pin`;
- `probe_backend(LinuxProcessRunner())` returns `profile=doubletake-0.5+source` with
  `source_reporting=True`, `source_reporting_complete=True`, `target_volume=True`,
  `multi_target=True` and `target_mute=True`. `physical_source` stays False because composition,
  not the public probe, derives it.

Both files were replaced atomically with hash re-verification. Backups:

| Backup | Pair |
| --- | --- |
| `/home/bin/*.prev` | previous three-token source chain, `44d85c94…` / `3a1018bb…` |
| `/home/bin/*.prev-2token` | 2026-10-31 two-token build, `22187a18…` / `6da62590…` |

The documented two-file rollback restores `.prev`; restoring `*.prev-2token` returns to the state
where Mirror is gated by incomplete source reporting. No daemon, session, service or receiver was
started, stopped or contacted, so an already running daemon keeps its earlier executable. Audible
gain, receiver readback and live acceptance remain unobserved. The repository's own
`tests/environment/test_installed_contracts.py` was not run on this host because it has no pytest
or application venv; the probe above was executed directly with the standard interpreter.

### The fork branch is not an installation candidate (2026-11-06 observation)

`https://github.com/hyperdreamer/doubletake/tree/capture-report-selected-source` carries the
reopened PR-76 source-reporting commits only: `internal/airplay/capture.go`, its tests, the daemon
status/source wiring and its tests. It adds no `-capabilities` flag and no volume surface. Built
from that branch, `doubletake -capabilities` exits 2 with
`flag provided but not defined: -capabilities`, and the production probe decodes the pair as
`profile=doubletake-0.5` with `source_reporting=False`, `source_reporting_complete=False` and
`target_volume=False` — the plain release profile. Installing it would therefore gate Mirror,
which requires complete source reporting, and disable the volume row, even though the branch does
report metadata to a Go caller. The carried chain stays the installation source; PR-76 remains
upstream provenance, not a deliverable binary.

Delivery documentation validation passed **138 tests, 2 deselected**, exit 0, using
`/data/home/henry-arch/.venvs/airplay-linux/bin/python -m pytest -q tests/backend/test_contract.py tests/environment/test_installed_contracts.py -m 'not live_backend and not live_kde and not live_hardware' --basetemp=/tmp/av-volume-delivery`.
The five runbook groups below match the approved brief verbatim and pass `bash -n`; embedded
Python parses without executing it. A separate receipt check matches both source HEAD and the
fresh tag peel to the measured commit, plus all four patch and both binary hashes.

The final unfiltered command was
`/data/home/henry-arch/.venvs/airplay-linux/bin/python -m pytest -q` from the lane worktree.
It finished at `2026-11-05T11:52:25.918358+00:00` with **5,809 passed, 12 skipped, 166 warnings**,
exit **0**. Live opt-ins stayed 0; `PYTEST_ADDOPTS` supplied only the short temporary root
`--basetemp=/tmp/av-volume-final`, with no marker filter. The warnings are qasync deprecations.
The exact log and command/exit receipt are `/tmp/av-delivery.7OYAi0/final-suite.log` and
`/tmp/av-delivery.7OYAi0/final-suite-result.json`. These application tests did not execute the
newly rebuilt helper pair.

### Offline rebuild command group

Run from the application repository. This is the command group used, with the generated
`volume_delivery` resolving to `/tmp/av-delivery.7OYAi0` for the measured build:

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

### Prepared executable verification — unexecuted

For the existing measured pair, set this path in the operator's shell:

```bash
volume_delivery=/tmp/av-delivery.7OYAi0
```

The following receiver-free checks require authorization to run the rebuilt helpers. They
supply no command operand and do not construct a daemon or connect a control socket. The sender
must exit 0 with exactly four lines and empty stderr. The ctl deliberately preserves
`flag.ContinueOnError`: help exits **2**, stdout is empty, and its table is on stderr.

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

Expected on a future successful run: exit 0 and
`prepared pair: exact four-token and target-scoped help contracts verified`. That message has
not been observed for this prepared pair.

### Operator-only pair installation — unexecuted

Separate installation authorization is required. Use the measured directory above, complete
its pending executable verification, and retain both old files before replacing either.
The command stages both new files and restores both previous files on a replacement or
verification error. It performs no process stop/start or service action.

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

Only an actual successful execution establishes two previous/installed hash lines and the
immediately prior `.prev` pair. Record the authorization reference, installation time, exact
hashes and exit status before changing this receipt's installation status. An already running
daemon keeps its earlier executable; replacing paths does not update it. Delivery waits for an
ordinary separately managed next application start. No running session is manipulated here.

### Installed observation — applied to the final-source pair

This group was written for the superseded prepared pair. It was executed on 2026-11-06 against
the final-source pair instead; the observed outputs are in the
[installed final-source pair receipt](#installed-final-source-pair-2026-11-06).

The same commands, if an operator repeats them:

```bash
sha256sum /home/bin/doubletake /home/bin/doubletake-ctl /home/bin/doubletake.prev /home/bin/doubletake-ctl.prev
/home/bin/doubletake -capabilities
/home/bin/doubletake-ctl --help
```

Expected: installed hashes match the built pair; sender exit 0 with exactly the
four tokens; ctl exit 2 with the extended table on stderr. Record both backup hashes and
observed outputs/exits. The `/tmp/av-delivery.7OYAi0` pair itself was never installed, so its
status remains **prepared, not installed**; neither the offline suite nor the older historical
hashes below supplies an installation observation.

### Operator-only two-file rollback — unexecuted

If the operator elects rollback after an installation, restore both files together:

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

Expected: both restored hashes equal their recorded previous hashes and that pair's earlier
help/token surface returns. The legacy pair lacks `target-volume`, so the volume row becomes
disabled while existing mute behavior remains. Restore both sides; a one-file downgrade can
produce an incoherent pair that refuses the whole backend. This command includes no restart,
receiver or session action. Record a rollback result only if it is actually executed.


## Reconciled rebuild identity (2026-11-02)

Each patch was checked and applied in 0001 → 0002 → 0003 order to a clean archive of the pinned
v0.5.0 commit. That exact tree passed `go build ./...`, `go vet ./...` and
`go test -count=1 ./...` (four tested packages, two packages without tests). The two recorded
explicit binary builds also passed. Verification used Go 1.27.3 linux/amd64, `CGO_ENABLED=1`,
`GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, cached modules in `/tmp/gopath`, and
GStreamer 1.28.8. No source changes from the reference checkout were included.

The actual artifacts are `/tmp/delivery-reconcile.GMO4Nx/doubletake` and
`/tmp/delivery-reconcile.GMO4Nx/doubletake-ctl`; the clean patched source is in
`/tmp/delivery-reconcile.GMO4Nx/reapply` and the command output in
`/tmp/delivery-reconcile.GMO4Nx/go-verification.log`. These temporary artifacts were not installed.

```text
base v0.5.0 tag      c896e3689ab85c3ed520e502308dedfbb5a4c221
0001 sha256         622ecded534be335557b61332cd66d57cecfdea869abc00f5e58f6f75a84d687
0002 sha256         0927e2d24abf63f413441a3a291c545288eb09e7b3dca102d3f328bb4b764dc7
0003 sha256         482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567
doubletake sha256   68737ed1e5b2830e11a787b09e7446c3840e51cc2cc90bbd441d0dacb949f43a
doubletake-ctl      7bdadb77479dd5e4acde0d31776162082a7d497cf106037ed8fbf10588e64d30
```

The rebuilt sender's recorded exit was 0 with the three-token source advertisement. This switch exits
before daemon construction; it launched no daemon, opened no control socket and contacted no
receiver or session bus. Hashes identify this particular build; compiler and native library
differences can change binary hashes. The earlier epoch's activation-build hashes do not identify
this reconciled chain and have been superseded here.

The 2026-10-31 PR record separately reported race-enabled parser/status and private-portal
production-path tests (distinct groups, joining streams, missing metadata, and position
availability). Those historical PR checks are not evidence that this stricter chain is identical
to the PR. The carried patches contain product code; the mandatory local producer/consumer proof
is in `tests/backend/test_carried_source_patch.py`.

## Historical local installation records

These dated records predate volume patch 0004. They are not a current installed-volume receipt.

### 2026-11-03 complete source-chain build installed

The stale two-token binaries were replaced with a rebuild of this exact chain. Identity of the
installed artifacts:

```text
base v0.5.0 tag      c896e3689ab85c3ed520e502308dedfbb5a4c221
0001 sha256         622ecded534be335557b61332cd66d57cecfdea869abc00f5e58f6f75a84d687
0002 sha256         0927e2d24abf63f413441a3a291c545288eb09e7b3dca102d3f328bb4b764dc7
0003 sha256         482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567
doubletake sha256   44d85c94d2eb1ea317fa71d7085b7b92d63e06fd2add20afbea7e0cb047adba2
doubletake-ctl      3a1018bbd9c339568453332f2c9e3ae6e5eee2f3c9dfb438cc1cfd36c08c7ac2
```

Built with Go 1.27.3 linux/amd64, `CGO_ENABLED=1`, `GOFLAGS=-mod=mod`, cached modules in a private
`GOMODCACHE`, and GStreamer 1.28.8. `go vet ./...` was clean and `go test -count=1 ./...` passed
(four tested packages, two without tests). The sender exits 0 with all three capability lines:

```text
capabilities-v1
status-source
status-source-complete
```

Hashes are build-specific, as above. The previous two-token build is retained as
`/home/bin/doubletake.prev` and `/home/bin/doubletake-ctl.prev`; restoring those files reverts the
installation to a state where Mirror is gated by incomplete source reporting.

The prior installed pair, recorded during the 2026-11-02 reconciliation, was:

```text
/home/bin/doubletake       sha256 22187a1802f36a2d6eae56e2fc891722d3ef10de9bd237d7da77c40c263324e6
/home/bin/doubletake-ctl   sha256 6da625901b5e0629437378ec404d5869791fc1e5e19ed87b26ee31b14872d4fe
```

They printed only `capabilities-v1` and `status-source` and were not a valid full-chain
installation. Reporting metadata alone proves no physical-output guarantee, so installing the
tokens is necessary evidence, never sufficient on its own.

At that time `/home/bin` preceded `/usr/bin` on this account's `PATH`; the packaged files were
untouched, and `/home/bin/airplay-linux` pointed to the application in `~/.venvs/airplay-linux`.
Those observations and backup paths are historical; no replacement or rollback was executed
for the current volume source change.

Historical containment checks are separate observations: the epoch recorded a loopback-only
network-namespace probe of the older build; master's 2026-10-31 installed-build probe used a
private socket and temporary credentials without a separate network namespace. Both recorded an
idle status and socket removal after process termination. Neither is a live receiver acceptance
run for this reconciled build. The latter recorded a help surface differing from the packaged
build by the `Usage of <argv0>:` line and the added `-capabilities` row.

## How the application recognises it

Recognition requires both the matching sender/ctl help surface and capability advertisement:

- sender help matches `tests/fixtures/backend/doubletake-0.5-source-help.txt`, which differs from
  the pinned fixture by the `-capabilities` row;
- `doubletake -capabilities` exits 0 and advertises `capabilities-v1` plus `status-source` for
  `source_reporting=True` and profile `doubletake-0.5+source`;
- the additional `status-source-complete` token is required for `source_reporting_complete=True`
  and Mirror eligibility. A two-token source profile can be recognised but is ineligible for Mirror;
- `target_volume=True` additionally requires the exact extended ctl table, with independent
  affirmative scope for `volume` and `audio`, and `capabilities-v1`, `status-source`, `target-volume`.
  The old table plus a volume token or the extended table without its required tokens refuses
  the entire backend. [Backend compatibility](backend-compatibility.md) contains both exact tables.

The four-patch sender adds only the existing `-capabilities` advertisement's fourth token. The
profiles stay `doubletake-0.5` and `doubletake-0.5+source`. Source completeness still requires
its own sender/token evidence, independently of volume.

These tokens never set `physical_source`, `owned_daemon_control`, `owner_handshake`,
`foreground_control` or `keyring` in the public probe. Composition derives its own evidence. An
extra flag without tokens, tokens without the matching help surface, incomplete token evidence,
or tokens on a non-zero exit cannot grant complete reporting.

## Activation in the application

The epoch supplies receipt-backed `EvidenceAuthority`, shared root authority, strict pidfd
teardown, the isolated credential-service witness and a fresh KScreen source verifier. Public
transport admission uses `owned_daemon_control` and `keyring`; fixture-only handshake admission
remains separate. Mirror additionally requires `multi_target` and physical-source evidence.
Source acquisition and credential-challenge polling share the same five-second deadline.
Connect/status credential challenges disconnect only that target without pairing or prompting.

The delivered **T5a** verifier reads geometry and origin from one fresh KScreen document for
each check. Explicit position is the primary identity key across all enabled outputs; size
corroborates the unique match using `round(reported.width / scale)` and
`round(reported.height / scale)` against its current-mode size. Each stream is checked against
its own requested connector. Missing source/position, ambiguous position, scale disagreement,
non-monitor type, virtual or helper-owned outputs, and unproved physical origin produce typed
refusal reasons. A non-object output entry refuses the entire inventory read with
`inventory_unavailable`; no partial layout is accepted. Unknown enabled state or unreadable
enabled-output geometry/scale also prevents verification.

The verifier returns `Result[SourceObservation]`: success retains the reported source and matched
connector; `SourceVerificationRefusal` retains a refused observation and its `SourceRefusal`
reason. Observation itself performs no stream mutation. The activated **T7** transport consumes
these results before publishing ready: successful ready events carry the verified connector and
reported geometry, and failed events carry the sanitized refusal reason in the diagnostic detail.
Exact-target cleanup follows a start refusal; unconfirmed disconnect takes `cleanup_timeout`
precedence and retains the cleanup obligation.

These checks establish cooperative same-user ownership, not protection against a malicious
process with the same UID. Missing, unreadable, conflicting or stale evidence refuses control
and teardown and retains cleanup obligations. See README for the full refusal and residual
guarantee disclosure. No live receiver acceptance run is claimed.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
modified binary brings LGPL obligations, including source availability for the modified work;
treat a locally built patched binary as a development dependency until that is settled.
