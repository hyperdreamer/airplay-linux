# Carrying the source-reporting patch for `doubletake`

## Why we carry a patch

Mirror mode promises to send **one chosen physical monitor**. The portal already tells the
sender which display it selected (`source_type`, `size`, `position`), but `doubletake` reads only
the size and discards the rest, and its daemon `status` reply carries no resolution and no source
identity. Without that, this application cannot verify what it is actually sending: it cannot tell
the requested monitor from a different one, from a window, or from a virtual output another
application (or our own helper) created. Refusing to guess is a design rule here, not a preference.

We raised it upstream as `omarroth/doubletake#76`. We closed it ourselves because we need the
capability for a shipping product and did not want it gated on review timing; the patch is small,
applies to `main`, and passes their test suite, so the door is open to revisit.

## What the patches do

`vendor/doubletake/0001-capture-report-selected-source.patch`
- adds `airplay.SourceInfo{Type, Width, Height, X, Y}` with `IsMonitor()`
- parses `source_type`, `position` and `size` from the ScreenCast portal reply
- invokes the callback only when all five fields are present and valid; missing position is not converted to `(0,0)`
- adds optional `CaptureConfig.OnSource func(SourceInfo)`; nil keeps existing behaviour
- covers both portal paths, because `PrepareCapture` is the single request site

`vendor/doubletake/0002-status-report-selected-source.patch` (stacks on 0001)
- `CapturePreparation.Source()` exposes the decoded source
- `videoCaptureGroup` records it under the daemon mutex
- `StreamInfo.Source` is reported as an optional `source` object in `ctl status`

`vendor/doubletake/0003-capability-tokens.patch` (stacks on 0001+0002)
- adds `-capabilities`, printing the complete stable machine-readable token list
  (`capabilities-v1`, `status-source`, `status-source-complete`) and exiting 0

## Python test-suite prerequisite

The default Python suite executes the carried source producer with `go test`, so it also
requires a Go toolchain with `go` on `PATH`, even when not rebuilding the backend. Install Go
and expose its executable on `PATH` before running pytest. A missing toolchain deliberately
fails the producer proof with setup instructions; this proof is mandatory for complete
verification. The separate Git patch-parser proof remains mandatory and does not require Go.
The producer proof uses only local fixtures and the Go standard library, with network module
and toolchain downloads disabled; it needs neither GStreamer nor a live backend.

## How to build

Requires Go >= 1.25 (verified with 1.27.3) and the GStreamer development libraries the project already needs.

```sh
git clone https://github.com/omarroth/doubletake
cd doubletake
git checkout c896e3689ab85c3ed520e502308dedfbb5a4c221 # v0.5.0, activation rebuild base
git apply /path/to/vendor/doubletake/0001-capture-report-selected-source.patch
git apply /path/to/vendor/doubletake/0002-status-report-selected-source.patch
git apply /path/to/vendor/doubletake/0003-capability-tokens.patch
CGO_ENABLED=1 go build -trimpath -buildvcs=false -o ./doubletake ./cmd/doubletake
CGO_ENABLED=1 go build -trimpath -buildvcs=false -o ./doubletake-ctl ./cmd/doubletake-ctl
```

Historical verification when the patches were carried (Go 1.27.3) recorded `go build ./...`,
`go vet` and `go test -count=1 ./...` passing. For the activation rebuild, both commands above
were executed successfully from a clean archive of the pinned base, applying each carried patch
with `git apply --check` followed by `git apply` in 0001 → 0002 → 0003 order. No local source
changes from the reference checkout were included. The activation build used Go 1.27.3 linux/amd64,
`CGO_ENABLED=1`, `GOPROXY=off`, `GOSUMDB=off`, `GOTOOLCHAIN=local`, cached modules in `/tmp/gopath`
and GStreamer 1.28.8. Its identity check is:

```sh
sha256sum ./doubletake ./doubletake-ctl
./doubletake -capabilities
```

The expected capability output is exactly three lines, in order:
`capabilities-v1`, `status-source`, `status-source-complete`.

## Activation rebuild identity

The actual rebuild artifacts are `/tmp/oe9-build-_ptfq1cr/doubletake` and
`/tmp/oe9-build-_ptfq1cr/doubletake-ctl`. The build record is
`/tmp/oe9-build-_ptfq1cr/identity.json`; these temporary verification artifacts were not installed.
The immutable inputs and observed artifacts are:

```text
base v0.5.0 commit   c896e3689ab85c3ed520e502308dedfbb5a4c221
0001 sha256         195038d392f41c263cba4533604d4d7c2a5fb8122ebb0bae87106d27b0e3b169
0002 sha256         e10ff1d922f14f6ff7996d249264588b000942321dc5f66afce5d3269cc5ee80
0003 sha256         482e7ec85940b35381686730ae248b491065bfc66d28ec449b62717e0d803567
doubletake sha256   eabdf411c3882004780100e8b11ebb9fcf877e7c2cb1057047282d095c7fb8f6
doubletake-ctl      c1b1f0e8069dbbb53237a68c9b0b30d3f0e018bc21cc5726ab36bc68b271304f
```

The rebuilt sender exited 0 with exactly the three capability lines above. This switch exits
before daemon construction; verification launched no daemon, opened no control socket and
contacted no receiver or session bus. Binary hashes identify this particular build; compiler and
native library differences can change hashes in another environment.

## Installed locally (this machine)

The currently installed `/home/bin` binaries predate the third patch. Their recorded identity is:

```text
/home/bin/doubletake       sha256 22187a1802f36a2d6eae56e2fc891722d3ef10de9bd237d7da77c40c263324e6
/home/bin/doubletake-ctl   sha256 6da625901b5e0629437378ec404d5869791fc1e5e19ed87b26ee31b14872d4fe
```

They print only `capabilities-v1` and `status-source`, so they are intentionally **not** a valid
full-chain installation. This activation rebuilt and identified the full chain above; selecting
those binaries for an operator installation remains a separate action. Require all three capability
lines before using Mirror mode. The two currently installed file hashes were rechecked during
activation and still match this block.

`/home/bin` precedes `/usr/bin` on this account's `PATH`, so `doubletake` and `doubletake-ctl`
now resolve to the patched builds **for this user**. The packaged files are untouched.

To revert the backend shadowing: `rm /home/bin/doubletake /home/bin/doubletake-ctl`.
To remove the application: `rm /home/bin/airplay-linux && rm -rf ~/.venvs/airplay-linux`.
To reinstall the application after repo changes:
`~/.venvs/airplay-linux/bin/pip install --force-reinstall /data/home/guest/Development/airplay-linux`.

Isolated verification of the installed pre-third-patch build (network namespace, loopback only, private socket,
temporary credentials, process killed afterwards): the daemon logs `[daemon] listening on <path>`,
`ctl status` returns `{"ok": true, "state": "idle", "has_audio": false, "audio_muted": false}`, killing the
process removes the socket, and `--help` is identical to the packaged build except the `Usage of <argv0>:`
line. This verifies the old binary's containment only; it does not make it eligible for source verification.

Because `--help` is otherwise unchanged and the probe normalises that `Usage of <argv0>:` line, the
capability probe resolves a full-chain build to profile `doubletake-0.5+source` only after all three
tokens are observed.

## How the application recognises it

Recognition requires **both** halves, and neither alone is enough:

- the sender `--help` surface must match the patched build (`tests/fixtures/backend/doubletake-0.5-source-help.txt`,
  which differs from the pinned fixture by exactly the `-capabilities` row);
- `doubletake -capabilities` must exit 0 and print all three tokens: `capabilities-v1`, `status-source`,
  and `status-source-complete`.

When both hold the profile becomes `doubletake-0.5+source` with `source_reporting_complete=True`. That flag states
that the build reports a complete display identity. It deliberately does **not** set `physical_source`,
`owner_handshake`, `foreground_control` or `keyring`: reporting a source is not proving it is the physical monitor
we asked for. The verifier consumes the report against one fresh KScreen inventory before Mirror mode opens.

An extra flag without the tokens fails closed, tokens without the matching surface fail closed, a partial token set fails
closed, and tokens printed on a non-zero exit are ignored.

## Activation in the application

The capability probe in `backend/contract.py` derives source-reporting support from the documented
`--help` evidence and the complete three-token output. A two-token installation is deliberately
refused even when it can return a partial `source` object.

Production composition now installs receipt-backed `EvidenceAuthority`, shared root authority,
strict pidfd teardown, the isolated credential-service witness and the fresh KScreen source verifier.
Public transport admission uses `owned_daemon_control` and `keyring`; the fixture-only handshake
contract remains separate. Mirror additionally requires `multi_target` and verified physical-source
evidence. The selected target's source must match type, complete geometry and signed position before
stream readiness, under the same five-second deadline as credential-challenge polling. Connect-time
or status-time credential challenges disconnect only that target without pairing or prompting.

These checks establish cooperative same-user ownership, not protection against a malicious process
with the same UID. Missing, unreadable, conflicting or stale evidence refuses control/teardown and
retains cleanup obligations. The activation verification used deterministic boundaries and did not
claim a live receiver acceptance run. See the README for the complete refusal and residual-guarantee
disclosure.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
*modified binary* would bring LGPL obligations with it (source availability for the modified work),
so treat a locally built patched binary as a development dependency until that is settled.
