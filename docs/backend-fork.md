# Carrying the source-reporting patch for `doubletake`

## Why we carry a patch

Mirror mode promises to send **one chosen physical monitor**. Upstream `doubletake` reads the
selected stream's size from the ScreenCast portal reply and discards the other source metadata;
its daemon `status` reply exposes no captured-source information. This patch retains the portal's
optional `source_type`, `size` and `position` per capture group and reports them per stream, so the
application has evidence to compare with compositor outputs. Refusing to guess is a design rule
here, not a preference.

That evidence is partial. Portal `source_type=1` means MONITOR; on KDE this also covers regions
and existing virtual outputs, so `IsMonitor()` does not prove that a source is physical or owned
by us. Portal size and position use compositor coordinates, which need not be pixels. `x` and `y`
are usable only when `has_position` is true: an absent or malformed position must remain distinct
from a genuine `(0,0)`. The sender must correlate this metadata with compositor output identity
and ownership, and refuse ambiguous matches or any output our own helper created.

We raised it upstream as `omarroth/doubletake#76`. We closed it once because we needed the
capability for a shipping product and did not want it gated on review timing; the maintainer then
said they are open to merging it and asked for the source to be reported **per stream**, because a
daemon can serve several streams whose captures differ. The PR is reopened with exactly that shape:
the reported source belongs to the stream's own capture group, so different captures report
different sources and streams sharing one capture report that capture's source.

## Upstream status (2026-10-31)

`omarroth/doubletake#76` is open at `2b5f1b0` with four commits (capture layer, parser tests,
per-stream status, and the optional-position/production-handoff follow-up), based on `main` at
`5e2f1cb`. The maintainer has said they are open to merging it. The fork branch `streaminfo-source`
tracks the PR head, so a patched build can be rebuilt from it directly. The follow-up was pushed
as a descendant commit to preserve the reopened PR's history.

## What the patches do

`vendor/doubletake/0001-capture-report-selected-source.patch`
- adds `airplay.SourceInfo{Type, Width, Height, X, Y}` with `IsMonitor()`
- parses `source_type`, `position` and `size` from the ScreenCast portal reply
- adds optional `CaptureConfig.OnSource func(SourceInfo)`; nil keeps existing behaviour
- covers both portal paths, because `PrepareCapture` is the single request site

`vendor/doubletake/0002-status-report-selected-source.patch` (stacks on 0001)

- `CapturePreparation.Source()` exposes the decoded, potentially partial metadata
- `videoCaptureGroup` records it under the daemon mutex
- `StreamInfo.Source` is reported as an optional `source` object in `ctl status`, read per stream
  from that stream's own capture group and omitted when no decoded type or usable size was reported
- `SourceInfo` serialises with lowercase keys (`type`, `width`, `height`, `x`, `y`, `has_position`)
- `HasPosition` is false for missing or malformed position and true for a valid coordinate pair,
  including `(0,0)`; its JSON key is always present when `source` is present
- documents the limits of source type and compositor coordinates; `OnSource` runs after portal
  selection, before opening the PipeWire remote, so a callback does not prove setup succeeded

`vendor/doubletake/0003-capability-tokens.patch` (stacks on 0001+0002)
- adds `-capabilities`, printing a stable machine-readable token list (`capabilities-v1`, `status-source`) and
  exiting 0

## How to build

Requires Go >= 1.25 (verified with 1.27.3) and the GStreamer plugins the project already needs.

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
cleanly with strict whitespace checks to both that revision and `v0.5.0`; the patched tree passes
`go build ./...`, `go vet ./...`, and `go test -count=1 ./...`. Its capture and daemon source files
are byte-identical to PR #76 at `2b5f1b0`; patch 0003 adds the local capability advertisement.

The PR additionally passes `go test -race -count=1 ./...` and includes the parser/status tests and
three production-path integration tests. These exercise a private D-Bus portal through real
capture preparation, group creation and status, using synthetic video at the GStreamer source
boundary. They cover distinct groups, a joining stream retaining the original group's source,
missing metadata, and known versus unknown position in status JSON. Disposable-copy mutations
that drop the portal callback, preparation retention, accessor result, group storage or position
availability all fail the integration tests. Those tests require `dbus-daemon` and GStreamer
software-encoding/RTP elements and skip when dependencies are absent. The vendored patches carry
product code only.

## Installed locally (this machine)

Built from the three patches applied to upstream `main` with Go 1.27.3, matching PR #76 (fork
branch `streaminfo-source`, commit `2b5f1b0`), and installed ahead of the packaged binaries:

```text
/home/bin/doubletake       10552847 bytes   (patched; /usr/bin/doubletake is the packaged 6476312)
/home/bin/doubletake-ctl    6942725 bytes
/home/bin/doubletake.prev   10549231 bytes   the previous patched builds, kept for a one-command revert
/home/bin/doubletake-ctl.prev 6943093 bytes
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

The help surface retains the packaged build's options plus `-capabilities`. Recognition uses both
that help surface and the tokens, as described below. Reporting source metadata alone does not
establish the physical-output guarantees required by Mirror mode.

## How the application recognises it

Recognition requires **both** halves, and neither alone is enough:

- the sender `--help` surface must match the patched build (`tests/fixtures/backend/doubletake-0.5-source-help.txt`,
  which differs from the pinned fixture by exactly the `-capabilities` row);
- `doubletake -capabilities` must exit 0 and print `capabilities-v1` and `status-source`.

When both hold the profile becomes `doubletake-0.5+source` with `source_reporting=True`. That flag states only that the
build reports portal source metadata per stream. It deliberately does **not** set `physical_source`, `owner_handshake`,
`foreground_control` or `keyring`: reporting a source is not proving it is the physical monitor we asked for, and the
verification that consumes the report belongs to the owned-daemon work, not to this patch.

An extra flag without the tokens fails closed, tokens without the matching surface fail closed, a partial token set fails
closed, and tokens printed on a non-zero exit are ignored.

## What is still missing on our side

The profile recognises the reporting capability, but the application does not yet consume the
per-stream `source` object. Verification must compare available metadata with compositor output
identity and ownership, including rejection of the helper's own virtual outputs. `physical_source`
stays false and Mirror stays gated until those checks are implemented; neither the capability
token nor `IsMonitor()` is sufficient evidence.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
*modified binary* would bring LGPL obligations with it (source availability for the modified work),
so treat a locally built patched binary as a development dependency until that is settled.
