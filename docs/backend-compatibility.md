# Backend compatibility

This project pins the control contract to the published `doubletake` 0.5.0
release. The evidence was collected from the Arch `doubletake-bin` 0.5.0-1
package and the upstream release source:

- Release tag: [`v0.5.0`](https://github.com/omarroth/doubletake/releases/tag/v0.5.0)
- Annotated release tag object: `c896e3689ab85c3ed520e502308dedfbb5a4c221` (the `v0.5.0` tag)
- Peeled source commit: [`e18f85d07f74a3b9d49f448762d28f3f7feb3938`](https://github.com/omarroth/doubletake/commit/e18f85d07f74a3b9d49f448762d28f3f7feb3938)
- Sender source: [`internal/daemon/daemon.go`](https://github.com/omarroth/doubletake/blob/e18f85d07f74a3b9d49f448762d28f3f7feb3938/internal/daemon/daemon.go)
- Client source: [`internal/daemon/daemonclient/client.go`](https://github.com/omarroth/doubletake/blob/e18f85d07f74a3b9d49f448762d28f3f7feb3938/internal/daemon/daemonclient/client.go)

The captured command output is retained in:

- `tests/fixtures/backend/doubletake-0.5-help.txt`
- `tests/fixtures/backend/doubletake-ctl-0.5-help.txt`
- `tests/fixtures/backend/status-0.5.json` (concurrent PIN-waiting and streaming)
- `tests/fixtures/backend/status-0.5-password.json` (password-waiting and streaming)

## Verified v0.5 surface

The sender and control help, the release source, and the status fixture prove
these operations and fields:

| Surface | Contract evidence | Adapter policy |
| --- | --- | --- |
| `status` | `doubletake-ctl` command; JSON `ok`, `state`, audio fields, and `streams` | Supported through an explicit `-socket` path |
| `discover` | `doubletake-ctl` command; `devices` response entries | Supported through an explicit `-socket` path |
| `devices` | `doubletake-ctl` command; `devices` response entries | Supported through an explicit `-socket` path |
| target `connect` | documented `connect [target]` and request `target` field | Mapped only when a target is supplied; credential arguments are rejected |
| target `disconnect` | documented `disconnect [target]` and request `target` field | Mapped only when a target is supplied |
| target `mute`/`unmute` | documented target forms and request `target` field | Mapped only when a target is supplied |
| status credentials | `needs_credential`, `credential_kind`, legacy `needs_pin` | Decoded as `pin` or `password`; unknown values fail closed |
| status streams | `device`, `device_ip`, `state`, `has_audio`, `audio_muted`, `credential_kind` | Decoded into immutable typed records |

All commands use `doubletake-ctl -socket <explicit-private-path> ...`. The
adapter never emits the unqualified destructive commands and never places a
PIN or password in process arguments.

## Absent or unverified capabilities

The 0.5 help and release source do **not** independently prove the following:

- an application-owned foreground daemon mode;
- an authenticated owner token and daemon-instance handshake;
- typed physical-monitor or virtual-output source verification;
- target-bound pairing through a secret-safe application channel;
- transactional keyring prepare/commit or provisional trust records;
- cancellation, revocation, or invalidation of pairing trust;
- audio restart, mode querying, or a typed resolution capability response.

The decoder therefore leaves those capability flags false. `-daemonize` does
not prove foreground ownership, `-cred-backend keyring` does not prove keyring
transactions, and generic screen-capture flags do not prove a typed physical or
virtual source contract. A start or trust operation that requires these flags
returns `dependency_incompatible` or `unsupported_operation`; unknown or
ambiguous values cannot enable it.

The installed 0.5 release also accepts a positional PIN/password on the
`connect` and `pin` CLI forms. Those forms are intentionally not used by the
application adapter because process arguments and shell-facing interfaces are
not a verified secret-safe channel. Pairing remains unsupported until an
independently verified channel and ownership handshake exist.

## Application contract fixture

`tests/fixtures/backend/contract-v1.json` is an application adapter-contract
fixture. Its `provenance` is `application-contract-fixture` and
`live_support_proven` is `false`. Its capability matrix is test evidence for
the adapter's gates only; it is not a claim that an installed `doubletake` 0.5
daemon implements protocol v1.

Live verification is optional and must be performed against a disposable,
explicit private socket. It is never part of capability negotiation and no
live sender is launched during contract discovery.
