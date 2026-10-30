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
- adds optional `CaptureConfig.OnSource func(SourceInfo)`; nil keeps existing behaviour
- covers both portal paths, because `PrepareCapture` is the single request site

`vendor/doubletake/0002-status-report-selected-source.patch` (stacks on 0001)
- `CapturePreparation.Source()` exposes the decoded source
- `videoCaptureGroup` records it under the daemon mutex
- `StreamInfo.Source` is reported as an optional `source` object in `ctl status`

## How to build

Requires Go >= 1.21 (verified with 1.27.3) and the GStreamer plugins the project already needs.

```sh
git clone https://github.com/omarroth/doubletake
cd doubletake
git checkout v0.5.0            # or main; both verified
git apply /path/to/vendor/doubletake/0001-capture-report-selected-source.patch
git apply /path/to/vendor/doubletake/0002-status-report-selected-source.patch
go build -o "$HOME/.local/bin/doubletake" ./cmd/doubletake
go build -o "$HOME/.local/bin/doubletake-ctl" ./cmd/doubletake-ctl
```

Verification performed when the patches were carried (Go 1.27.3):
`go build ./...` OK, `go vet` clean, `go test -count=1 ./...` passes
(`internal/airplay` 11.4 s, `internal/daemon` 13.4 s).

## Installed locally (this machine)

Built from the two patches applied to upstream `main` (commit of the work tree: `0d69465`,
branch `streaminfo-source`) with Go 1.27.3, and installed ahead of the packaged binaries:

```text
/home/bin/doubletake       10532199 bytes   (patched; /usr/bin/doubletake is the packaged 6476312)
/home/bin/doubletake-ctl    6924757 bytes
/home/bin/airplay-linux    -> ~/.venvs/airplay-linux/bin/airplay-linux   (the application)
```

`/home/bin` precedes `/usr/bin` on this account's `PATH`, so `doubletake` and `doubletake-ctl`
now resolve to the patched builds **for this user**. The packaged files are untouched.

To revert the backend shadowing: `rm /home/bin/doubletake /home/bin/doubletake-ctl`.
To remove the application: `rm /home/bin/airplay-linux && rm -rf ~/.venvs/airplay-linux`.
To reinstall the application after repo changes:
`~/.venvs/airplay-linux/bin/pip install --force-reinstall /data/home/guest/Development/airplay-linux`.

Isolated verification of the installed patched build (network namespace, loopback only, private socket,
temporary credentials, process killed afterwards): the daemon logs `[daemon] listening on <path>`,
`ctl status` returns `{"ok": true, "state": "idle", "has_audio": false, "audio_muted": false}`, killing the
process removes the socket, and `--help` is identical to the packaged build except the `Usage of <argv0>:`
line.

Because `--help` is otherwise unchanged and the probe normalises that `Usage of <argv0>:` line, the
capability probe still resolves the patched build to profile `doubletake-0.5` with the same withheld flags.
That is the point of the next section: installing the patch changes nothing observable to the application
until the build advertises what it can do.

## What is still missing on our side

The capability probe in `backend/contract.py` derives support from documented `--help` evidence only,
and the administrative help text is unchanged by these patches. Until a patched build advertises the
capability in a way the probe can verify, `physical_source` stays false and Mirror stays gated — that
recognition step is task T1 of `owned-daemon-control-v2`, not something to assume.

## Licensing

`doubletake` is LGPL-3.0. These files are our own diffs, not copies of its source. Distributing a
*modified binary* would bring LGPL obligations with it (source availability for the modified work),
so treat a locally built patched binary as a development dependency until that is settled.
