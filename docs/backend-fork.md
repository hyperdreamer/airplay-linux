# Carrying the source-reporting patch for `doubletake`

## Why we carry a patch

Mirror mode promises to send **one chosen physical monitor**. The portal already tells the
sender which display it selected (`source_type`, `size`, `position`), but `doubletake` reads only
the size and discards the rest, and its daemon `status` reply carries no resolution and no source
identity. Without that, this application cannot verify what it is actually sending: it cannot tell
the requested monitor from a different one, from a window, or from a virtual output another
application (or our own helper) created. Refusing to guess is a design rule here, not a preference.

We raised it upstream as `omarroth/doubletake#76`. We closed it once because we needed the
capability for a shipping product and did not want it gated on review timing; the maintainer then
said they are open to merging it and asked for the source to be reported **per stream**, because a
daemon can serve several streams whose captures differ. The PR is reopened with exactly that shape:
the reported source belongs to the stream's own capture group, so different captures report
different sources and streams sharing one capture report that capture's source.

## Upstream status (2026-10-31)

`omarroth/doubletake#76` is open with three commits (capture layer, parser tests, per-stream
status), based on `main` at `5e2f1cb`, and the maintainer has said they are open to merging it.
The fork branch `streaminfo-source` tracks the PR head, so a patched build can be rebuilt from it
directly.

## What the patches do

`vendor/doubletake/0001-capture-report-selected-source.patch`
- adds `airplay.SourceInfo{Type, Width, Height, X, Y}` with `IsMonitor()`
- parses `source_type`, `position` and `size` from the ScreenCast portal reply
- adds optional `CaptureConfig.OnSource func(SourceInfo)`; nil keeps existing behaviour
- covers both portal paths, because `PrepareCapture` is the single request site

`vendor/doubletake/0002-status-report-selected-source.patch` (stacks on 0001)
- `CapturePreparation.Source()` exposes the decoded source
- `videoCaptureGroup` records it under the daemon mutex
- `StreamInfo.Source` is reported as an optional `source` object in `ctl status`, read per stream
  from that stream's own capture group and omitted when the portal reported no usable identity
- `SourceInfo` serialises with lowercase keys (`type`, `width`, `height`, `x`, `y`)

`vendor/doubletake/0003-capability-tokens.patch` (stacks on 0001+0002)
- adds `-capabilities`, printing a stable machine-readable token list (`capabilities-v1`, `status-source`) and
  exiting 0

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

Re-verified 2026-10-31 against upstream `main` (`5e2f1cb`) with Go 1.27.3: all three patches apply
cleanly to a clean clone, `go build ./...` OK, `go vet ./...` clean, and `go test -count=1 ./...`
passes (`internal/airplay` 11.2 s, `internal/daemon` 13.1 s). The same tree is what PR #76 carries,
except that the PR also includes the Go tests for the parser and for per-stream reporting; the
vendored patches carry product code only.

## Installed locally (this machine)

Built from the three patches applied to upstream `main` with Go 1.27.3, matching PR #76 (fork
branch `streaminfo-source`, commit `a97b835`), and installed ahead of the packaged binaries:

```text
/home/bin/doubletake       10549231 bytes   (patched; /usr/bin/doubletake is the packaged 6476312)
/home/bin/doubletake-ctl    6943093 bytes
/home/bin/doubletake.prev   10531487 bytes   the previous patched builds, kept for a one-command revert
/home/bin/doubletake-ctl.prev 6924757 bytes
/home/bin/airplay-linux    -> ~/.venvs/airplay-linux/bin/airplay-linux   (the application)
```

`/home/bin` precedes `/usr/bin` on this account's `PATH`, so `doubletake` and `doubletake-ctl`
now resolve to the patched builds **for this user**. The packaged files are untouched.

To revert the backend shadowing: `rm /home/bin/doubletake /home/bin/doubletake-ctl`, or restore the
previous patched builds by moving the two `.prev` files back over them.
To remove the application: `rm /home/bin/airplay-linux && rm -rf ~/.venvs/airplay-linux`.
To reinstall the application after repo changes:
`~/.venvs/airplay-linux/bin/pip install --force-reinstall /data/home/guest/Development/airplay-linux`.

Isolated verification of the installed patched build (private socket in a temporary directory,
temporary credentials, process killed afterwards; the 2026-10-31 probe did not use a separate
network namespace): the daemon logs `[daemon] listening on <path>`, `ctl status` returns
`{"ok": true, "state": "idle", "has_audio": false, "audio_muted": false}`, killing the process
removes the socket, `-capabilities` prints `capabilities-v1` and `status-source`, and `--help` is
identical to the packaged build except its `Usage of <argv0>:` line and the `-capabilities` row.

Because `--help` is otherwise unchanged and the probe normalises that `Usage of <argv0>:` line, the
capability probe still resolves the patched build to profile `doubletake-0.5` with the same withheld flags.
That is what the next section is about: the only difference the application could observe without
`-capabilities` is the new `source` object in `ctl status`, and it does not read that yet, so a build
has to advertise what it can do before the application will consume it.

## How the application recognises it

Recognition requires **both** halves, and neither alone is enough:

- the sender `--help` surface must match the patched build (`tests/fixtures/backend/doubletake-0.5-source-help.txt`,
  which differs from the pinned fixture by exactly the `-capabilities` row);
- `doubletake -capabilities` must exit 0 and print `capabilities-v1` and `status-source`.

When both hold the profile becomes `doubletake-0.5+source` with `source_reporting=True`. That flag states only that the
build reports which display it captured. It deliberately does **not** set `physical_source`, `owner_handshake`,
`foreground_control` or `keyring`: reporting a source is not proving it is the physical monitor we asked for, and the
verification that consumes the report belongs to the owned-daemon work, not to this patch.

An extra flag without the tokens fails closed, tokens without the matching surface fail closed, a partial token set fails
closed, and tokens printed on a non-zero exit are ignored.

## What is still missing on our side

The capability probe in `backend/contract.py` derives support from documented `--help` evidence only,
and the administrative help text is unchanged by these patches. Until a patched build advertises the
capability in a way the probe can verify, `physical_source` stays false and Mirror stays gated — that
recognition step is task T1 of `owned-daemon-control-v2`, not something to assume.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
*modified binary* would bring LGPL obligations with it (source availability for the modified work),
so treat a locally built patched binary as a development dependency until that is settled.
