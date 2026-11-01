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

Requires Go >= 1.21 (verified with 1.27.3) and the GStreamer plugins the project already needs.

```sh
git clone https://github.com/omarroth/doubletake
cd doubletake
git checkout v0.5.0            # or main; both verified
git apply /path/to/vendor/doubletake/0001-capture-report-selected-source.patch
git apply /path/to/vendor/doubletake/0002-status-report-selected-source.patch
git apply /path/to/vendor/doubletake/0003-capability-tokens.patch
go build -o "$HOME/.local/bin/doubletake" ./cmd/doubletake
go build -o "$HOME/.local/bin/doubletake-ctl" ./cmd/doubletake-ctl
```

Verification performed when the patches were carried (Go 1.27.3):
`go build ./...` OK, `go vet` clean, `go test -count=1 ./...` passes
(`internal/airplay` 11.4 s, `internal/daemon` 13.4 s).
The full-chain binary identity check is:

```sh
sha256sum "$HOME/.local/bin/doubletake" "$HOME/.local/bin/doubletake-ctl"
"$HOME/.local/bin/doubletake" -capabilities
```

The expected capability output is exactly three lines, in order:
`capabilities-v1`, `status-source`, `status-source-complete`.

## Installed locally (this machine)

The currently installed `/home/bin` binaries predate the third patch. Their recorded identity is:

```text
/home/bin/doubletake       sha256 22187a1802f36a2d6eae56e2fc891722d3ef10de9bd237d7da77c40c263324e6
/home/bin/doubletake-ctl   sha256 6da625901b5e0629437378ec404d5869791fc1e5e19ed87b26ee31b14872d4fe
```

They print only `capabilities-v1` and `status-source`, so they are intentionally **not** a valid
full-chain installation. After rebuilding with all three patches, replace this block with the two
new SHA-256 values from the identity command above and require all three capability lines before
using Mirror mode.

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

## What is still missing on our side

The capability probe in `backend/contract.py` derives source-reporting support from the documented `--help`
evidence and the complete three-token output. Until a patched build advertises the third token,
`source_reporting_complete` and `physical_source` stay false and Mirror stays gated. A two-token installation is
therefore deliberately refused even though it can still return a partial `source` object.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
*modified binary* would bring LGPL obligations with it (source availability for the modified work),
so treat a locally built patched binary as a development dependency until that is settled.
