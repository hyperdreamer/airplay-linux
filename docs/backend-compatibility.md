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

The historical source record above and `contract-v1.json` contain a transcription discrepancy:
they spell the peeled commit `e18f85d07f74a3b9d49f448762d28f3f7feb3938`. Fresh local
`rev-parse` of the supplied tag and HEAD of the prepared clone both return
**`e18f85d07f74a3b9d49f448762d28f3b7feb3938`**. The prepared pair uses this measured commit.
The older strings/links remain recorded fixture provenance and must not substitute for the
[measured build identity](backend-fork.md#prepared-volume-helper-pair-2026-11-05).

## Recognized help and capability contracts

The historical v0.5 release capture and the locally extended volume surface are two exact
accepted tables. The sender retains the pinned flag set, optionally plus the existing
`-capabilities` flag. Volume introduces no sender flag or profile name.

The legacy ctl command table is:

```text
  status                              Show daemon state and all active streams
  discover                            Discover AirPlay devices on the network
  devices                             List cached discovered devices
  connect [target] [PIN-or-password]  Start mirroring (to target IP, or first free device)
  pin <PIN-or-password>               Submit pairing credentials for a waiting device
  disconnect [target]                 Stop mirroring (all streams, or only the given IP)
  mute [target]                       Mute mirrored audio (all streams, or only the given IP)
  unmute [target]                     Unmute mirrored audio (all streams, or only the given IP)
```

Patch 0004 carries this extended table, with the three changed/new rows in connect, volume,
audio, pin order:

```text
  status                              Show daemon state and all active streams
  discover                            Discover AirPlay devices on the network
  devices                             List cached discovered devices
  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)
  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)
  audio off [target]                  Close mirrored audio (all streams, or only the given IP)
  pin <PIN-or-password>               Submit pairing credentials for a waiting device
  disconnect [target]                 Stop mirroring (all streams, or only the given IP)
  mute [target]                       Mute mirrored audio (all streams, or only the given IP)
  unmute [target]                     Unmute mirrored audio (all streams, or only the given IP)
```

Recognition checks the complete argument-signature table. Each of `volume` and `audio` must
independently affirm **"only the given IP"** without a negation; a scope statement on another
row cannot prove either operation. The existing disconnect/mute/unmute scope checks remain.
The four-token advertisement produced by the full carried chain is exactly:

```text
capabilities-v1
status-source
status-source-complete
target-volume
```

| Help and token evidence | Recognition |
| --- | --- |
| Exact legacy table, pinned sender | `doubletake-0.5`, `target_volume=false` |
| Exact legacy table, source sender plus `capabilities-v1` and `status-source` | `doubletake-0.5+source`, `target_volume=false` |
| Exact extended table, either accepted sender flag set, `capabilities-v1`, `status-source`, `target-volume` | Existing sender-derived profile, `target_volume=true` |
| Extended table without its tokens; `target-volume` with the old table; partial, malformed, negated or borrowed new scope | `doubletake-unknown`, all gates false; refuse the whole backend |

Source reporting still requires the source sender help surface and its existing tokens.
`source_reporting_complete` independently requires `status-source-complete`; `target-volume`
cannot grant completeness, physical identity, owned control or any private trust gate. Unknown
tokens grant nothing. A half-installed sender/ctl pair must be repaired as a pair.

## Typed operations and receipts

All application commands use `doubletake-ctl -socket <explicit-private-path> ...`. Status,
discover and devices are read operations. Connect, disconnect, mute and unmute retain their
published target forms. A capable connect adds `--audio on`, optionally `--volume N`, or
`--audio off` for video-only. Live changes use `volume N T`; closure uses `audio off T`.
The application always supplies the target for mutation, never emits the targetless broadcast
forms, and never places a PIN/password in process arguments. There is no `audio on` command.

A legacy saved level cannot send new flags: connect remains `connect T`, and target
mute/unmute keep their existing semantics. Version strings and remembered preferences cannot
promote support.

Status retains `ok`, `state`, audio fields, `needs_credential`, `credential_kind`, legacy
`needs_pin` and per-stream identity. Credential kinds decode only as `pin` or `password`;
unknown values refuse. Optional `streams[i].volume` is an exact integer 0..100;
`volume_state` is unrequested/pending/applied/refused;
`audio_closed` is an exact boolean. Missing evidence stays absent, applied requires a level,
and present null/invalid members refuse decoding. These facts never come from an aggregate or
another stream. Successful live volume/close replies contain full status for the exact target;
a connect acknowledgement carries no volume receipt. The existing audio-start status read
carries the receipt, with no additional polling owner.

Only a receiver RTSP 200 acknowledges a requested level. Remote-control changes are not sensed,
and an acknowledgement cannot prove the receiver honored audible gain. Operational readouts,
last audible restore, save failures and audio decisions are described in [Operations](operations.md).

## Public evidence and composition evidence

The public help/token decoder leaves foreground control, owner handshake, physical/virtual
source identity, keyring transactions, target-bound pairing, provisional commit, cancellation,
revocation and mode querying unproven. `-daemonize`, `-cred-backend keyring` and capture flags
alone cannot prove those permissions. Credential-bearing connect and pin forms stay outside
the application adapter's secret-safe contract.

Current production composition can derive `owned_daemon_control` from receipt-backed launch,
endpoint and teardown evidence, `keyring` from its isolated credential-service witness, and
physical-source eligibility from complete source reporting plus a fresh KScreen verifier.
The public `owner_handshake`/`foreground_control` flags stay false; receipt-backed transport
admission uses its own authority. Every start, mutation and cleanup still checks the current
receiver, daemon, endpoint, lease and trust evidence. Missing or stale evidence refuses.
These implementation facts do not establish a live receiver result. The
[2026-11-05 prepared-pair receipt](backend-fork.md#prepared-volume-helper-pair-2026-11-05)
records the build prepared in `/tmp/av-delivery.7OYAi0` from a superseded patch revision, which was
not installed. The [2026-11-06 installed final-source pair](backend-fork.md#installed-final-source-pair-2026-11-06)
records the four-token pair installed in `/home/bin`, its measured source/patch/binary hashes,
Go/native versions, observed sender token and ctl help contracts, the production probe result and
both rollback backups. The older installations below that receipt remain dated historical evidence.

## Historical release-only ownership observations

The following receiver-free spike predates the 2026-11-05 volume work and describes unmodified
v0.5.0. It is retained as historical evidence about the release's launch surface. The current
receipt-backed composition is described above and in [Backend activation](backend-fork.md#activation-in-the-application).

### What the backend offers (observed, receiver-free spike)

- Foreground launch without `-daemonize` **ignores** `-socket`, creates no control socket, and begins streaming to a
  discovered device immediately.
- `-daemonize` creates the control socket at the requested private path, logs `[daemon] listening on <path>`, starts mDNS
  discovery, and idles: `ctl status` returns `{"ok": true, "state": "idle"}`.
- The control socket exists after a kill only until the owning process dies; killing the owner removes it.

### What the backend does not offer

- **No authenticated control channel.** `doubletake-ctl` exposes `status`, `discover`, `devices`, `connect`, `pin`,
  `disconnect`, `mute`, `unmute` and a `-socket` override. There is no owner token, no handshake, no per-connection
  identity, no privilege separation.
- Consequently **any process running as the same user can drive the daemon**: start streams, disconnect other targets, and
  mute or unmute audio. The socket is protected only by filesystem permissions.
- The `-cred-backend keyring` flag selects where the backend stores *pairing* secrets; it says nothing about control
  authority, and selecting it is not evidence that the Secret Service is reachable or usable.

### The consequence for this design

Ownership can only be established from secondary evidence — per-instance `0700` runtime directory, launch receipt, socket
inode to owning pid, `start_ticks`, and a pidfd-bounded termination. That evidence is sufficient for a **cooperative
same-user environment**, where no process is trying to impersonate our daemon. It is **not** sufficient against a
malicious same-UID process, which can bind the socket first, rewrite a receipt it can read, or simply issue its own
commands to the daemon.

The earlier design considered authenticated backend control and a cooperative single-user
ownership model. The implemented receipt-backed authority adopts the latter boundary and
refuses incomplete evidence. It does not claim authenticated backend control or promote the
public `owner_handshake` flag. The release observations above explain that residual limit;
they no longer imply that all production transport admission is unconditionally disabled.

## Historical release-only source observations

Mirror mode's promise is specific: send **a chosen physical monitor**, and never a virtual or helper-created output. The
design treats that restriction as a safety property (HR-03), not a preference, because mirroring an inferred source would
send the wrong screen or a display the application itself created.

The unmodified v0.5.0 backend examined in the historical spike could not express that request:

- `doubletake` has **no source, monitor or output selector**. Its capture is the desktop as a whole on Wayland.
- The original spike also recorded X11 window selectors and a synthetic test pattern; neither
  established Wayland output identity.
- Consequently the application's `physical_source` affirmation cannot be proven: the backend cannot be told which monitor
  to send, and it does not report which one it captured.

This remains independent of daemon ownership: a private owned socket alone cannot establish
which output was captured. The source-reporting chain now supplies complete per-stream portal
metadata, and the application correlates it with fresh compositor identity and ownership
exclusions before ready. It still adds no source-selector flag. The historical prerequisites
and their present implementation boundaries are:

| Prerequisite | Unmodified v0.5.0 evidence | Current composition requirement |
| --- | --- | --- |
| owned daemon control | No authenticated backend channel | Current receipt/endpoint authority and strict teardown in the cooperative same-user scope |
| physical source identity | No monitor selector or source report | Complete source tokens, per-stream metadata and a unique fresh physical-output match |
| credential service and pairing transactions | Storage selector alone proves neither | Isolated service witness; pairing transaction gates remain independent |

Virtual mode separately requires an available helper and its exact ownership association.
Neither source metadata nor the new volume token establishes those facts. Live host/receiver
acceptance is separate from the offline implementation evidence.

## Application contract fixture

`tests/fixtures/backend/contract-v1.json` is an application adapter-contract
fixture. Its `provenance` is `application-contract-fixture` and
`live_support_proven` is `false`. Its capability matrix is test evidence for
the adapter's gates only; it is not a claim that an installed `doubletake` 0.5
daemon implements protocol v1.

Live verification is optional and must be performed against a disposable,
explicit private socket. It is never part of capability negotiation and no
live sender is launched during contract discovery.
