# Carrying the source-reporting patch for `doubletake`

## Why we carry a patch

Mirror mode promises to send **one chosen physical monitor**. Upstream `doubletake` reads the
selected stream's size from the ScreenCast portal reply and discards the other source metadata;
its daemon `status` reply exposes no captured-source information. The carried chain retains the
portal's `source_type`, `size` and `position` per capture group and reports them per stream, so the
application has evidence to compare with compositor outputs. Refusing to guess is a design rule.

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
  `status-source-complete`, one token per line in that order, and exiting 0 before constructing a daemon.

The Python status decoder consumes exactly the six lowercase fields above and requires
`has_position` to be the boolean `true`. Missing or false availability, malformed coordinates,
uppercase legacy objects and partial objects refuse; no missing value becomes an origin.
The typed `BackendSource` represents only complete geometry admitted by that decoder.

## Python test-suite prerequisite

The default Python suite executes the carried source producer with `go test`, so a Go toolchain
with `go` on `PATH` is required even when not rebuilding the backend. Install Go and expose its
executable before running pytest. Missing Go deliberately fails with setup instructions; this
proof is mandatory. The separate Git patch-parser proof is also mandatory and does not need Go.
The producer proof compiles the actual carried declarations with local Variant/size boundaries,
uses only the standard library, and disables network module and toolchain downloads. It checks
missing/malformed/real-zero/signed positions and feeds the producer's actual JSON to the Python
status decoder. It needs neither GStreamer nor a live backend.

## How to build

Requires Go >= 1.25 (verified with 1.27.3) and the project's GStreamer development libraries.

```sh
git clone https://github.com/omarroth/doubletake
cd doubletake
git checkout c896e3689ab85c3ed520e502308dedfbb5a4c221 # v0.5.0, activation rebuild base
git apply --check --whitespace=error-all /path/to/vendor/doubletake/0001-capture-report-selected-source.patch
git apply --whitespace=error-all /path/to/vendor/doubletake/0001-capture-report-selected-source.patch
git apply --check --whitespace=error-all /path/to/vendor/doubletake/0002-status-report-selected-source.patch
git apply --whitespace=error-all /path/to/vendor/doubletake/0002-status-report-selected-source.patch
git apply --check --whitespace=error-all /path/to/vendor/doubletake/0003-capability-tokens.patch
git apply --whitespace=error-all /path/to/vendor/doubletake/0003-capability-tokens.patch
CGO_ENABLED=1 go build -trimpath -buildvcs=false -o ./doubletake ./cmd/doubletake
CGO_ENABLED=1 go build -trimpath -buildvcs=false -o ./doubletake-ctl ./cmd/doubletake-ctl
sha256sum ./doubletake ./doubletake-ctl
./doubletake -capabilities
```

## Reconciled rebuild identity (2026-11-02)

Each patch was checked and applied in 0001 → 0002 → 0003 order to a clean archive of the pinned
v0.5.0 commit. That exact tree passed `go build ./...`, `go vet ./...` and
`go test -count=1 ./...` (four tested packages, two packages without tests). The two explicit
binary-build commands above also passed. Verification used Go 1.27.3 linux/amd64, `CGO_ENABLED=1`,
`GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, cached modules in `/tmp/gopath`, and
GStreamer 1.28.8. No source changes from the reference checkout were included.

The actual artifacts are `/tmp/delivery-reconcile.GMO4Nx/doubletake` and
`/tmp/delivery-reconcile.GMO4Nx/doubletake-ctl`; the clean patched source is in
`/tmp/delivery-reconcile.GMO4Nx/reapply` and the command output in
`/tmp/delivery-reconcile.GMO4Nx/go-verification.log`. These temporary artifacts were not installed.

```text
base v0.5.0 commit   c896e3689ab85c3ed520e502308dedfbb5a4c221
0001 sha256         622ecded534be335557b61332cd66d57cecfdea869abc00f5e58f6f75a84d687
0002 sha256         0927e2d24abf63f413441a3a291c545288eb09e7b3dca102d3f328bb4b764dc7
0003 sha256         482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567
doubletake sha256   68737ed1e5b2830e11a787b09e7446c3840e51cc2cc90bbd441d0dacb949f43a
doubletake-ctl      7bdadb77479dd5e4acde0d31776162082a7d497cf106037ed8fbf10588e64d30
```

The rebuilt sender exited 0 with exactly the three capability lines above. This switch exits
before daemon construction; it launched no daemon, opened no control socket and contacted no
receiver or session bus. Hashes identify this particular build; compiler and native library
differences can change binary hashes. The earlier epoch's activation-build hashes do not identify
this reconciled chain and have been superseded here.

The 2026-10-31 PR record separately reported race-enabled parser/status and private-portal
production-path tests (distinct groups, joining streams, missing metadata, and position
availability). Those historical PR checks are not evidence that this stricter chain is identical
to the PR. The carried patches contain product code; the mandatory local producer/consumer proof
is in `tests/backend/test_carried_source_patch.py`.

## Installed locally (this machine)

### 2026-11-03 complete build installed

The stale two-token binaries were replaced with a rebuild of this exact chain. Identity of the
installed artifacts:

```text
base v0.5.0 commit   c896e3689ab85c3ed520e502308dedfbb5a4c221
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

`/home/bin` precedes `/usr/bin` on this account's `PATH`; the packaged files are untouched.
To revert shadowing, remove `/home/bin/doubletake` and `/home/bin/doubletake-ctl`, or restore the
previous patched builds from their `.prev` files. `/home/bin/airplay-linux` points to the
application in `~/.venvs/airplay-linux`.

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
  and Mirror eligibility. A two-token profile can be recognised but is ineligible for Mirror.

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
