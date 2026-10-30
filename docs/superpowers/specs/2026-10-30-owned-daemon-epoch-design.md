# Owned-daemon epoch — design

- **Status:** draft for review round 3
- **Contract:** `docs/superpowers/plans/2026-10-30-owned-daemon-control.md` (plan v2) **as amended by**
  `docs/superpowers/plans/2026-10-30-owned-daemon-control-amendment-1.md` (operator-authorised, reply `A`)
- **Handoff:** `docs/handoff-owned-daemon-epoch.md`
- **Run:** `pm-run-20261030-040814-aefef9c0`, epoch 1, wave 1
- **Scope:** Mirror mode against the locally patched `doubletake` 0.5.0 build that reports its captured display.
  Virtual mode is out of scope.

## 1. Background

The application already ships a 30-task implementation of sessions, transport, display, trust, diagnostics and UI,
with every gate closed because the published backend cannot prove the operations those gates require. Two of those
gates — `foreground_control` and `owner_handshake` — were written for an *authenticated* daemon control channel that
`doubletake` does not have and will not have. `docs/backend-compatibility.md` records the dead end and the two ways
out; this epoch takes the first one deliberately:

> **Adopt the cooperative single-user threat model explicitly.** Ownership is evidence-based: the daemon is trusted
> because it lives in a directory only we created, its identity matches a launch receipt, and its socket entry, its
> listening peer and its command line agree. A hostile same-UID process is out of scope: on this desktop it already
> owns the session.

`physical_source` is blocked for a second, independent reason: the released backend cannot be told which monitor to
send and does not report which one it captured. This machine carries a locally patched build whose `status` reply
reports the selected display identity per stream — but only if the report is *complete* and the application verifies
it instead of interpreting it.

Both changes sit on a live control path that can start and stop a real daemon, so the contract's activation invariant
governs the epoch:

> No production detached spawn may exist until receipt production, verification, strict pidfd teardown and
> cross-process reconciliation are all installed. **Every phase commit must carry a test proving that no production
> spawn occurs.** Activation happens in exactly one place.

## 2. Goals and non-goals

### Goals

1. `owned_daemon_control` is a capability that only a *composition* can affirm, that admission consumes instead of the
   unprovable `foreground_control`/`owner_handshake` pair, and that the control adapter consumes through an injected
   ownership authority, so no mutating request is authorized by a flag alone.
2. Durable, versioned reservation and launch-receipt records carry every fact the verifier needs, published atomically
   as single authoritative documents.
3. Ownership is proven end to end: dirfd-anchored directory entry, the *live control peer* that answers on that entry,
   its `start_ticks`, its command line naming that exact socket, its owner UID, the recorded directory identity, the
   boot id and the endpoint generation.
4. Teardown of an owned daemon never signals a number: a pidfd is opened and re-validated, every signal goes through
   the descriptor, and an unavailable pidfd is a typed refusal.
5. A shared-root authority serialises launch, reconciliation and whole-daemon shutdown across application processes,
   refusing while another live owner exists, reaping a prior instance only through the verifier, and keeping that
   owner evidence for as long as the daemon lives.
6. The launcher starts an *idle* daemon on a private socket with no target, no capture request and no credential
   operation of its own, and every crash window between reservation and `stopping` has a defined, tested outcome.
7. Activation is one commit: every consumer migrates, the credential-service proof and the source verification are
   wired behind a single ordered eligibility boundary, and the operator-visible guarantee/refusal text matches what is
   proven.
8. Acceptance runs on fakes only, through the real production composition, and proves with negative tripwires that no
   live path can run in the default suite.

### Non-goals

Virtual mode; hostile same-UID defence; whole-screen mirroring without a source identity; any relaxation of the
receiver, trust, provider, target-generation, pairing-compensation or cleanup gates. The one admissible change to
transport admission is Amendment 1's `connect` predicate (§4.6), which the operator authorised explicitly.

## 3. Threat model and the honesty boundary

| Adversary | In scope? | Why |
| --- | --- | --- |
| A crashed, restarted or duplicated *application* instance | yes | the failure mode that strands or double-starts daemons |
| A recycled PID, a replaced socket entry, a stale receipt from a previous boot | yes | ordinary races that must fail closed |
| A different user | yes | UID checks on directory, socket entry and process |
| A hostile process running as the same user | **no** | stated residual: it already owns the session |

The residual is disclosed in `README.md` and in the startup capability detail in the same words used here. Nothing in
this epoch may be described as authenticated ownership.

## 4. Capability model

### 4.1 `owned_daemon_control` is composition-only

`BackendFeatures` gains `owned_daemon_control: bool = False`. It is **never** set by `features_from_help` or
`probe_backend`: help text proves a command surface, not that this composition can own a daemon. One pure function
decides it, and one composition call site invokes that function:

```text
derive_composition_capabilities(probed, launcher, credential_proof, source_verifier, ambient_socket)
    -> BackendFeatures
```

`owned_daemon_control` is true only when the launcher seam is available (verified `-daemonize`/`-socket`/
`-cred-backend` surface, an absolute owned writable non-symlink runtime root), no ambient socket is configured, and
the composition is the production graph. `foreground_control` and `owner_handshake` keep their delivered meaning, stay
`False` on every public build, and stop being admission inputs.

### 4.2 Credential-service proof (P2)

`keyring` also stops being a help-derived claim and becomes a composition-derived proof. Before the capability report
is finalised, and **only after** the structural prerequisites of §10.3 are satisfied, the composition runs
`CredentialServiceProbe` with three legs:

- **service leg** — one Secret Service introspection round trip through the existing D-Bus capability client (no
  unlock, no item creation);
- **backend positive leg** — inside a private network namespace, an idle backend on a private socket with
  `-cred-backend keyring` must reach `status == idle`; the backend constructs its keyring backend during startup,
  which is a real `keyring.Get` round trip and fails hard when the Secret Service is unreachable;
- **backend negative leg** — the same launch with `DBUS_SESSION_BUS_ADDRESS` pointing at a nonexistent socket must
  exit nonzero, create no control socket and create no credentials file.

All three legs must pass. A missing `unshare`, a missing Secret Service, an unreadable backend or a failed leg leaves
`keyring` false, admission refuses, and the startup detail names the failed leg. The probe's own lifecycle is
specified in §10.3 because it launches a backend.

### 4.3 `physical_source` requires a verified *complete* report

`BackendFeatures` gains `source_reporting_complete: bool = False`, decoded only when the patched surface *and* a third
advertised token (`status-source-complete`) are present. The two-token build keeps `source_reporting=True` but is not
complete, so it can never enable Mirror. The composition affirms `physical_source` only when all hold:

1. `source_reporting_complete` is true;
2. a `ReportedSourceVerifier` (§7) is wired into the transport;
3. a physical capture provider can inventory monitors (delivered rule: enabled **and** connected **and** a known
   physical connector type).

Absent any one, `physical_source` stays false and Mirror stays gated. `features_from_help` keeps `physical_source`
`False` in every case.

### 4.4 Migration table (one row per consuming site)

| Site | Today | After |
| --- | --- | --- |
| `policy.capabilities.ADMISSION_BACKEND_CAPABILITIES` | `foreground_control, owner_handshake, keyring` | `owned_daemon_control, keyring` (both composition-derived) |
| `policy.capabilities` mode rubric | `admitted`, `mirror = … and physical_source` | unchanged shape with the derived inputs |
| `transport.DoubleTakeTransport._admit` | refuses unless the old triplet | refuses unless `owned_daemon_control and keyring` |
| `process.DaemonRegistry._compatible` | same triplet | `owned_daemon_control and keyring` |
| `process.supervisor.DaemonSupervisor` start check | same triplet | public profile: `owned_daemon_control`; fixture profile unchanged |
| `backend.control.PublishedDoubletakeControl._available` | every mutation and `shutdown` require `owner_handshake` | asks `authority.allows(operation)` (§4.5) |
| `backend.control.PublishedDoubletakeControl._available(connect)` | old triplet + `multi_target` + pairing group + source | §4.6 (Amendment 1) |
| `backend.control.PublishedDoubletakeControl.handshake` | refuses before I/O when `owner_handshake` is false | `authority.prove(record)`; the evidence authority returns an evidence proof |
| `backend.control.PublishedDoubletakeControl._revalidate_ownership` | compares backend-reported owner/instance/daemon/socket/protocol before every mutating request | `authority.authorize(...)` before I/O and `authority.confirm(...)` after it; no backend-reported ownership field is required |
| `backend.control.PublishedDoubletakeControl._ownership_check` (every status reply) | compares the same five backend fields | evidence authority: compares the status reply to *nothing ownership-bearing*; the identity comes from `authority.prove`/`confirm`. Fixture authority keeps the delivered comparison |
| `transport.DoubleTakeTransport._start` / `_request` | requires `Handshake` and forwards it | requires `OwnershipProof` (`kind: handshake|evidence`) and forwards it unchanged |
| `process.DaemonRegistry._authenticate` (readiness) | handshake + backend metadata in `status` | `authority.prove` + `status` *reachability* only; no backend identity fields |
| `process.DaemonRegistry._authenticate(release=True)` | handshake then `shutdown` request | `authority.release(record)` (strict pidfd path) |
| `process.supervisor._request_release` | handshake then shutdown request | `authority.prove` then `authority.release`; fixture profile unchanged |
| `runtime._control_factory` | binds token/instance/protocol into the profile | binds the current receipt, the authority and the fence source; the legacy bindings remain only for the fixture authority |
| `runtime._capability_gates` / startup detail | lists missing admission flags | names the owning authority, the credential leg and the source verifier |
| `README.md` | documents the honest refusal | documents the cooperative-same-user guarantee, the residual, the Mirror precondition and the exact refusal conditions |

No site may read `foreground_control`/`owner_handshake` for admission after activation. The fields remain on the
dataclass so the application-contract fixture's handshake path keeps working unchanged.

### 4.5 The control authorization seam (F-013)

```text
OwnershipProof = {kind: "handshake"|"evidence", daemon: ProcessIdentity,
                  socket_entry: (device, inode), generation: int, handshake: Handshake|None}

class OwnershipAuthority(Protocol):
    def allows(self, operation: BackendOperation) -> bool                       # synchronous, pure
    async def prove(self, record: DaemonRecord) -> Result[OwnershipProof]
    async def authorize(self, record, operation, target: ReceiverTarget|None,
                        fence: DispatchFence|None) -> Result[OwnershipProof]
    async def confirm(self, record, proof: OwnershipProof) -> Result[None]
    async def release(self, record: DaemonRecord) -> Result[None]
```

Two implementations, selected by the derived features inside `runtime._control_factory`:

- **`HandshakeAuthority`** — the delivered behaviour unchanged, selected only for a profile that affirms
  `owner_handshake` (the application-contract fixture): token/instance/daemon/socket/protocol equality, `Handshake`
  proof, fence before the request.
- **`EvidenceAuthority`** — the public path. `prove` runs `verify_owned_endpoint(receipt)` (§6) and requires the
  verified generation to be the registry's current generation. `authorize` re-proves, then requires the fence to match
  the request's target (receiver id, endpoint generation, fingerprint). `confirm` re-runs the peer and entry checks so a
  replacement during the request is detected. `release` is the strict teardown of §8.

Operation classes for `allows`, stated once:

| Operation class | Requirement |
| --- | --- |
| `status`, `discover`, `devices` | always allowed; the reply is checked by `prove`, never by backend ownership metadata |
| `disconnect`, `mute`, `unmute` | `owned_daemon_control and multi_target` (+ `target_mute` for mute) **and** a matching fence |
| `connect` | §4.6 |
| `shutdown` | `owned_daemon_control`, no target, no fence; allowed only on the authority's `release` path |
| pairing operations, audio restart, mode query | unchanged delivered capability requirements, all currently false |

`prove`/`confirm` are the only places a proof is minted; a control constructed without a proven record cannot perform
a mutating request. `authorize` and `confirm` are awaited immediately before and after the adapter's I/O; no
unverified window remains open across an awaited ownership check.

### 4.6 Connect admission (Amendment 1, operator reply `A`)

`docs/superpowers/plans/2026-10-30-owned-daemon-control-amendment-1.md` is the authority for this row:

```text
owned_daemon_control and multi_target and (physical_source or virtual_source)
and trust-not-blocked and dispatch-fence-valid
```

Every pairing operation, remembered trust, provisional-commit compensation, cancellation and revocation keeps its
delivered requirements and stays refused. The mandatory terms:

1. **No pairing at connect.** A credential challenge in the acknowledgement or in `status` disconnects that exact
   target and returns `dependency_incompatible`. No prompt, no secret, no leftover stream, no claimed capability.
2. **The challenge window is observed.** The bounded post-connect wait of §7 is the same wait that detects a
   challenge, so a credential-waiting start cannot sit unnoticed.
3. The amendment is recorded in the contract, with the verbatim reply and the residual (§17).

## 5. Durable records

Two versioned record types, both published through `AtomicFiles` (temp + fsync + rename + directory fsync):

### 5.1 `DaemonReservation` (`airplay-linux/daemon-reservation`, version 1)

Written **before** any spawn.

| Field | Meaning |
| --- | --- |
| `schema_version` | 1 |
| `launch_uuid` | unique per launch attempt |
| `instance_uuid` | application instance that owns the launch |
| `app` | `{pid, start_ticks}` of the owning application process |
| `owner_uid` | effective UID of the application |
| `boot_id` | `/proc/sys/kernel/random/boot_id` at reservation |
| `runtime_directory` | absolute private directory path |
| `runtime_device`, `runtime_inode` | `fstat` of the dirfd at reservation |
| `endpoint_generation` | monotonically increasing per endpoint, `previous + 1` |
| `socket_name` | the socket's basename inside the runtime directory |
| `protocol_version` | control-protocol token |

### 5.2 `LaunchReceipt` (`airplay-linux/launch-receipt`, version 1)

The reservation fields plus observed launch state. Two **different** identity pairs are recorded, because they are
different kernel objects and must never be conflated:

| Field | Meaning |
| --- | --- |
| `state` | `reserved` → `detached` → `bound` → `ready`; `stopping` intent from any launched state; terminal `stopped` or `failed` |
| `supervisor`, `daemon` | `{pid, start_ticks}` once observed (nullable) |
| `entry_device`, `entry_inode` | `fstat` of the socket **directory entry**, taken via the dirfd |
| `socket_inode` | the **kernel socket** inode from the unique `/proc/net/unix` row for that path |
| `peer_pid`, `peer_start_ticks`, `peer_uid` | the peer observed through `SO_PEERCRED` on a control connection (§6) |
| `status_state` | the `state` field of the last accepted `status` reply (`idle`, …) |
| `credential_service` | `keyring_proven` \| `file_refused` \| `unknown` — evidence, never a secret |
| `failure` | the diagnostic code of the transition into `failed` |
| `updated_at` | store clock |

Codec rules: reject an **unknown version**, a **missing field**, and any **partial record** (`bound` without entry and
socket identity, `ready` without `peer_pid`/`status_state`, a non-null `daemon` without `supervisor`, a `stopping`
without a preceding launched state). No field may default in a way that hides absence. Records never contain a token,
PIN, password, credential reference or environment dump.

### 5.3 One authoritative document per receipt; derived current (F-012)

There is **no separate pointer file**. Each receipt is one file `<state_root>/daemon/receipts/<launch_uuid>.json`, and
every transition is a single-record atomic replace. "Current" is *derived* from the store, never published:

```text
current = the unique receipt whose state is in {reserved, detached, bound, ready, stopping}
```

- exactly one ⇒ that is the current launch;
- two or more ⇒ ambiguity: `CleanupRequired`, no adoption, no replacement, no teardown of either;
- none ⇒ no current launch (a `failed`/`stopped` record may still exist as evidence).

The registry is the only writer of receipts. The supervisor keeps writing its own `DaemonRecord` lifecycle (the
delivered observation channel) and never writes a receipt; after a crash the registry reconstructs the receipt state
from that record plus the verifier. Transition preconditions, checked under the registry lock and the authority lease:

| Transition | Precondition | Written fields |
| --- | --- | --- |
| create `reserved` | derived current is empty, and generation = previous max + 1 | all reservation fields |
| `reserved → detached` | store record names supervisor and daemon; daemon identity equals the spawned child | `supervisor`, `daemon` |
| `detached → bound` | entry exists via the dirfd; socket row and peer prove the endpoint (§6) | `entry_*`, `socket_inode`, `peer_*` |
| `bound → ready` | proof still holds and `status` answers `state == idle` | `status_state`, `credential_service` |
| `reserved|detached|bound|ready → stopping` | whole-daemon shutdown or reconciliation admitted the stop; durable intent | state only |
| `stopping → stopped` | strict teardown proved exact absence | state only |
| `reserved|detached|bound|ready|stopping → failed` | a typed failure; evidence retained | `failure` |

**Retention.** `failed` and `stopped` receipts are immutable terminal evidence and are **never deleted by teardown**.
Only an explicit archival step outside teardown may move them, and it records a receipt of its own. M8 asserts survival
across successful cleanup, retry and restart.

**Crash injection** is required at each actual boundary — reservation write, spawn, supervisor publication, bind,
receipt write, `status` poll, `stopping` write, signal, exit proof, unlink — not at coarse lifecycle labels.

## 6. Ownership verifier (F-010)

New module `process/ownership.py`:

```text
verify_owned_endpoint(receipt) -> Result[VerifiedEndpoint]
```

The chain, each step failing closed, and the peer step is what binds the pathname to a process:

1. **Dirfd anchoring.** Open the runtime directory from `/` with `O_NOFOLLOW|O_DIRECTORY`; require a directory owned by
   `owner_uid`, mode `0700`, whose `(device, inode)` equals the receipt's.
2. **Entry identity.** `fstat` the socket name relative to that descriptor; require `S_ISSOCK`, owner `owner_uid`, and
   — when the receipt is already `bound` — equality with `entry_device/entry_inode`.
3. **Live peer.** Connect an `AF_UNIX` socket to that entry and read `SO_PEERCRED`; the peer's `(pid, uid)` is the
   process actually serving the path now. Require `uid == owner_uid`. Close immediately without sending a request.
4. **Socket row uniqueness.** Read `/proc/net/unix`; require **exactly one** row whose path equals the socket path, and
   require that the peer holds an `fd` referencing that row's inode. Two rows, zero rows, or a row whose only holders
   are other processes is a refusal — never a best candidate.
5. **`start_ticks` equality.** `read_identity_evidence(peer_pid)` must be `present` with `start_ticks` equal to the
   receipt's `daemon.start_ticks`, and the UID of `/proc/<peer_pid>` must equal `owner_uid`.
6. **Command line names that exact socket.** `/proc/<peer_pid>/cmdline` must carry the absolute socket path as the
   value of `-socket` (and `-daemonize`), with no other `-socket` value.
7. **Entry unchanged.** Re-`fstat` the entry through the descriptor; require the same `(device, inode)` as step 2. A
   change during verification is a refusal.
8. **Boot and generation.** Receipt `boot_id` equals the current boot id; `endpoint_generation` equals the registry's
   current generation. A superseded receipt is `stale_event`.

The verifier reads only: no signal, no unlink, no write. `authorize`/`confirm` (§4.5) re-run steps 3–7 around the I/O,
which is what detects a replacement between authorization and dispatch.

## 7. Reported-source verification (F-008)

### 7.1 Producer completeness (carried patch)

The carried `vendor/doubletake/0001-…` patch currently marks a source usable when only the type *or* the size arrived,
so a missing position becomes `(0,0)` and is indistinguishable from a real one. The epoch revises the carried chain:

- `portalStreamSource` reports usable **only** when the source type is present, the width and height are present and
  positive, and the position is present as two integral values;
- `vendor/doubletake/0003-…` advertises a third token, `status-source-complete`, alongside `capabilities-v1` and
  `status-source`;
- `docs/backend-fork.md` records the full three-patch chain, the rebuild command and the new binary's identity;
- the installed build must be rebuilt from the chain; a build without the third token yields
  `source_reporting_complete=False` and Mirror stays closed.

### 7.2 Verifier acceptance

```text
BackendSource(type: int, width: int, height: int, x: int, y: int)      # decoded from status `source`
verify_reported_source(requested: str, reported: BackendSource, layout: tuple[OutputInfo, ...])
    -> Result[None]
```

Acceptance requires **all** of:

1. `type == 1` (portal monitor source);
2. `width > 0` and `height > 0` — dimensions are positive; **coordinates are signed** and may legitimately be
   negative (the delivered KScreen adapter accepts negative positions), so only the dimensions carry a positivity rule;
3. exactly one **enabled output of any type or origin** in a fresh KScreen observation matches `(width, height, x, y)`;
   zero matches, two matches, or an unreadable layout all refuse;
4. that unique output's connector equals the requested connector **and** it satisfies the delivered physical-origin rule
   (enabled, connected, known physical connector type). A same-geometry virtual or unknown output therefore refuses
   instead of aliasing the requested monitor.

### 7.3 Data path (pinned end to end)

1. `backend/contract.py`: `BackendStream.source: BackendSource | None`; a strict `_decode_source` rejects non-mapping
   values, non-integer members, unknown keys and an object with no usable identity. Absent `source` decodes to `None`,
   which the verifier treats as unverified.
2. `capture/source.py`: the pure function plus a `ReportedSourceVerifier` port whose production implementation reads a
   fresh layout from the injected `KScreenObserver` and refuses an unreadable, stale or ambiguous observation.
3. `transport.DoubleTakeTransport.__init__(source_verifier=…)`; `_start` polls `status` (bounded) after the connect
   acknowledgement for the stream of that exact target, then verifies, then emits `ready`. A credential challenge in
   the same poll is a refusal under §4.6. Any failure ⇒ `_stop_owned` disconnects that exact target and returns
   `unsupported_source`.
4. `runtime._compose_production` constructs the verifier and passes it to the transport, and
   `derive_composition_capabilities` sets `physical_source` only when that same object was wired.

## 8. Strict teardown

Port signature: `terminate(identity, owner_uuid, runtime_directory, deadline, *, strict: bool = False)`.

1. classify the identity; a mismatch refuses, an absent process is success;
2. open a pidfd; **if none can be opened, refuse** with `dependency_incompatible` and signal nothing — the numeric
   branch remains only for the delivered non-owned paths;
3. re-validate the identity *after* opening (start ticks, UID, socket peer/argv where the receipt supplies it);
4. deliver `SIGTERM` through the descriptor only;
5. wait for exact exit with a bounded deadline, polling the descriptor (a non-child process has no `wait` obligation);
6. escalate to `SIGKILL` through the descriptor, then require exit within the kill bound;
7. only after proven exit: re-check the entry through the dirfd, unlink it, and transition the receipt to `stopped`
   (the receipt is retained, §5.3).

A durable `stopping` intent is written **before** the first signal. On any failure the receipt, the obligation and the
`cleanup_required` notice are retained; teardown never reports success it did not observe.

Strict call sites: `DaemonRegistry._stop`, `DaemonRegistry.shutdown`, `EvidenceAuthority.release`,
`DaemonSupervisor._request_release` (public profile) and the credential probe's teardown. A test asserts each passes
`strict=True`.

Serialisation: a per-receiver `Stop` issues `disconnect <exact target>` on the authorized control and nothing else. The
shared daemon terminates only inside `DaemonRegistry.shutdown()`, while the authority is held.

## 9. Cross-process authority (F-009)

New module `process/authority.py`:

- lock file `<state_root>/daemon/authority.lock` (`flock`, non-blocking, bounded retry);
- owner record `<state_root>/daemon/authority.owner` — `{schema, instance_uuid, app:{pid,start_ticks}, boot_id,
  acquired_at}`, written atomically while the lock is held.

Three **distinct** operations, never conflated:

| Operation | Effect |
| --- | --- |
| `acquire(instance) -> Lease` | takes the flock; refuses with `ownership_mismatch` while another owner is recorded and proved live; replaces a provably absent owner; refuses on unreadable evidence and retains `CleanupRequired` |
| `Lease.unlock()` | releases **only** the flock, in a `finally` after each mutating operation |
| `clear_owner(expected)` | compare-and-removes the durable owner record, only after exact teardown/reconciliation proved absence, and only while holding the lock |

Lifetime: the owner record is published when a launch is admitted and **survives every per-operation unlock** until
`clear_owner` proves the daemon is gone. A second application instance therefore always sees a live owner between
operations and refuses. `clear_owner` compares the exact record it wrote, so a stale release cannot delete a
successor's record.

Nesting rule: acquisition is non-reentrant; `shutdown()` acquires once and calls the internal `_reconcile_locked()`,
never the public `reconcile()`. Cancellation still runs the `finally` that unlocks, and a cancellation that arrives
after a successful `clear_owner` is a no-op.

## 10. Isolated idle launcher

### 10.1 Launcher capability seam

`LaunchCapability` (in `process/launcher.py`): `available()`, `argv(socket_path)`, `ambient_socket()`. The production
implementation checks the verified `-daemonize`/`-socket`/`-cred-backend` surface, an absolute owned writable
non-symlink runtime root, and `ambient_socket() is None`. The single call site is `runtime._compose_production`.

### 10.2 Launch shape

```text
doubletake -daemonize -socket <runtime_directory>/daemon.sock -cred-backend keyring
```

No `-target`, `-code`, `-pair`, `-test`, `-x11-*`, `-creds`: nothing resolves a receiver, selects a capture source or
performs a credential operation. `-cred-backend keyring` is mandatory: it forbids the file fallback and its
construction is the positive credential leg. The child stays the supervisor's direct child (the installed build does
not fork), keeps the parent-death `SIGKILL` protection, and its identity is published as it becomes observable.
Readiness = socket entry present **and** §6 proof **and** `status == idle` **and** the verified daemon identity equals
the spawned child identity.

*(Unchanged from the previous revision: the five crash-window rows.)*

| Window | Required outcome |
| --- | --- |
| reservation → spawn | receipt `failed`; no process; directory and socket absent or exactly removed |
| spawn → bind | verified child terminated by pidfd; receipt retained `failed`; replacement only after absence is proved |
| bind → receipt | entry and peer identified, daemon verified, receipt completes; a verification failure terminates the exact daemon and retains the receipt |
| receipt → running | `status` failure or non-idle state ⇒ terminate exactly, retain `failed`, block replacement while uncertainty remains |
| running → stopping | `stopping` durable before the first signal; exact-exit wait; obligation retained on failure |

### 10.3 The credential probe runs on the same lifecycle (F-011)

The probe is a backend launch and is therefore bound by the activation invariant. Two rules make that true:

1. **Ordered eligibility.** The composition evaluates structural prerequisites in order and stops at the first
   absence: `owned_daemon_control` derivable (launcher available, private root, no ambient socket) → source verifier
   wired → receipt store, verifier, authority and strict teardown constructible → *only then* the credential probe.
   An incomplete composition therefore performs **zero** launches, including the probe.
2. **Namespace-consistent lifecycle.** The probe does not launch the backend from the application process. It runs a
   dedicated foreground child:

   ```text
   unshare -rn <python> -m airplay_linux.process.credential_probe --socket <private> --record-root <private>
   ```

   which inside the namespace writes its own reservation, spawns the backend as its direct child, requires `idle`,
   records `SO_PEERCRED`/`/proc/net/unix` evidence **in the namespace it will read**, tears it down by the strict pidfd
   path, retains a `failed` record on any failure, and prints a verdict document on stdout. Every fact the probe
   reports is therefore observed in the same namespace and by the same lifecycle code as a production launch.

## 11. Activation (one commit)

The activation commit contains, together:

1. §4.4's migration for **every** row, including `_ownership_check`, the transport `Handshake`→`OwnershipProof`
   change, registry readiness/release and the supervisor release path;
2. §4.6's connect predicate, with Amendment 1 cited and the credential-challenge refusal tested;
3. the credential probe (§4.2, §10.3) and its capability wiring;
4. the reported-source verifier, strict decode, `source_reporting_complete` and `physical_source` derivation (§7), plus
   the revised carried patch chain and its rebuild record;
5. the operator disclosure (README guarantee/refusal/residual, startup capability detail naming the owning authority,
   the credential leg and the source verifier);
6. the activation-gated no-spawn test (§14.2) with its guard-removal control and the per-element omission matrix.

Nothing in the activation commit relaxes any other gate.

## 12. Measured evidence for the external preconditions

Recorded on this machine before design freeze (receiver-free, inside `unshare -rn`, private socket, temporary state):

- patched backend: `doubletake -capabilities` → `capabilities-v1`, `status-source`; help surface matches the pinned
  patched layout; a stream's `status` reply carries `source`.
- `-daemonize -socket <private> -cred-backend keyring` idles, creates `daemon.sock` (`srwx------`) and
  `daemon.sock.lock`, answers `{"ok":true,"state":"idle"}`, exits 0 on `SIGTERM`.
- the same launch with an unreachable session bus exits 1 with `keyring backend: system keyring not available`, creates
  no control socket and no credentials file.
- the socket appears in `/proc/net/unix` with its path and inode; the peer of a connection to it is the daemon.
- the delivered foreground argv (no `-daemonize`) ignores `-socket` and starts discovery — the behaviour this epoch
  replaces.

These measurements make P1 and P2 satisfiable here; the shipped code must still prove them (§4.2, §7).

## 13. Error handling

Existing catalog codes only: `ownership_mismatch` (evidence disagreement, ambiguity, refused authority, missing
`unshare` or backend evidence), `stale_socket`/`stale_event` (superseded endpoint or generation), `daemon_failed`
(readiness/exit failures), `cleanup_timeout` (unconfirmed teardown), `dependency_incompatible` (no pidfd, missing
prerequisite, credential challenge at connect), `unsupported_source` (unverifiable reported source). No backend
exception text and no path beyond the runtime directory appears in a diagnostic. Metadata never contains a PIN, secret
or credential reference.

## 14. Test strategy

### 14.1 Named mutations (each killed by a named test that pins the mechanism)

| ID | Mutation | Killing test obligation |
| --- | --- | --- |
| M1 | `owned_daemon_control=True` inside `probe_backend` | raw probe/decode result is false; eligible composition true; ambient-socket false |
| M2 | omit `endpoint_generation` from the encoded receipt | encode/decode round-trip and strict rejection of the missing field, with a non-default generation; derived-current agreement |
| M3 | accept argv naming a different socket | holding identity/UID/ticks/entry valid, change only the `-socket` value ⇒ refuse before status/mutation/signal |
| M4 | accept mismatched `start_ticks` | change only the recorded-vs-observed ticks ⇒ refuse; separately cover reuse during pidfd opening |
| M5 | numeric kill when pidfd is unavailable | otherwise-owned live identity, pidfd unavailable ⇒ typed refusal **and** an independent signal observer records zero numeric signals; every owned call passes `strict=True` |
| M6 | remove the shared-root `flock` | two authority/registry instances, barrier before owner publication ⇒ both would proceed; live owner between operations; stale release after succession |
| M7 | leave the candidate live after a verification failure | exact-identity case ⇒ candidate death observed; unreadable-identity case ⇒ nothing signalled, obligation retained, replacement blocked |
| M8 | erase the receipt on failure | reopen storage after failure, after successful cleanup, after retry, after restart ⇒ the `failed` record survives and cannot authorize replacement |
| M9 | enable ownership with source verification absent | all other fakes valid, remove only the wired verifier ⇒ `physical_source=False`, typed refusal, zero launches at both process boundaries |
| M10 | permit a live path by default | unmarked default test **attempts** the live backend, D-Bus and socket boundaries and fails on the attempt, even when the dependency is unavailable |

Additional regression obligations (not renumbering M1–M10): each former connect prerequisite individually; absent or
malformed source position; same-size non-origin selection; physical/virtual geometry collision; negative-coordinate
layouts; duplicate `/proc/net/unix` path rows; unlinked old listener plus replacement entry; replacement during first
bind; replacement between authorization and dispatch; owner survival after ordinary unlock; retained failed receipts
after exact cleanup; public-shape status without legacy metadata.

### 14.2 No-spawn proofs

`tests/acceptance/test_no_production_spawn.py` observes the **final process boundary** (`run` *and* `spawn`) through an
injected process port in the real production composition (`build_runtime(..., overrides={"process": recorder})`).

- Phases 1–3: assert zero launches, and assert that the same test with all fake prerequisites satisfied but the
  activation guard removed **does** attempt a launch — so the assertion cannot pass vacuously against today's
  incompatible profile.
- Phase 4: for each of the six elements (owned-daemon capability, receipt production, verifier, strict teardown,
  authority, credential proof, source verifier) remove exactly one and require zero launches plus a typed refusal.
  The unmodified composition is the only case that launches.

### 14.3 Activation acceptance matrix

| Composition | Expected |
| --- | --- |
| public-shape fake (`foreground_control=False`, `owner_handshake=False`, pairing flags false, status replies with **no** owner/instance/daemon/socket/protocol fields), complete fakes | `EvidenceAuthority` is selected and recorded; launch admitted; `status` accepted; `connect` authorized with a valid fence; target-scoped `disconnect`/`mute`/`unmute` authorized; whole-daemon shutdown releases by strict teardown |
| same, receipt generation or entry inode changed | every mutating operation refused before I/O; no launch; `stale_event` |
| same, fence mismatch | refused before I/O; `stale_target` |
| same, credential challenge on connect | exact-target disconnect, `dependency_incompatible`, no pairing attempt, no stream |
| owned_daemon_control absent / credential proof absent / source verifier absent / authority absent | zero launches; typed refusal naming the element; `physical_source=False` where the verifier is the missing element |
| legacy fixture profile | `HandshakeAuthority` selected; delivered handshake behaviour unchanged (separate test, not the public-acceptance evidence) |

### 14.4 Offline acceptance (phase 5)

Fake backend speaking the delivered control protocol with the **public** shape, fake KScreen observer, fake Secret
Service witness: production composition, launch, verify, per-receiver stop, whole-daemon shutdown, restart after crash,
and the refusal matrix. Negative tripwires: an unmarked default test fails if a live `doubletake`, a live D-Bus session
bus or a live socket lease is reachable from it, and each skip names the missing capability exactly (`owned daemon
control`, `credential service proof`, `physical source verification`).

### 14.5 Suite hygiene

Full suite headless (`QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8`), short `--basetemp`, real-process tests
against the repo installed in `~/.venvs/airplay-linux`. Baseline recorded: 3787 passed, 12 skipped. The known
supervisor flake is recorded, not masked.

## 15. Review resolutions

### Design review 1

| Finding | Resolution |
| --- | --- |
| F-001 control authorization seam | §4.5 (authority, two implementations, full consumer table in §4.4) |
| F-002 source data path | §7.3 (contract field, strict decoder, verifier port, transport injection, derivation) |
| F-003 pidfd-only teardown | §8 (strict contract, refusal before signal, revalidation after open, call sites) |
| F-004 receipt lifecycle | §5.3 (single writer, transition preconditions, retention) |
| F-005 composition authority | §4.1/§10.1 (launcher seam, one call site) and §9 |
| F-006 docs wording | §11 (full patch chain, precise T6 wording) |

### Design review 2

| Finding | Resolution |
| --- | --- |
| F-007 connect admission relaxed without authority | Operator decision `A`; contract Amendment 1; §4.6 with the challenge refusal and bounded window; §14.3 row |
| F-008 fabricated coordinates / ambiguous match | §7.1 producer completeness + third token; §7.2 signed coordinates, positive dimensions, uniqueness across **all** outputs, physical-origin rule retained |
| F-009 owner erased on unlock | §9 (`unlock` vs `clear_owner`, owner lifetime, nesting rule) |
| F-010 pathname not bound to the live peer | §6 steps 3–7 (`SO_PEERCRED`, unique socket row, entry unchanged, separate entry/socket identity fields in §5.2) |
| F-011 probe outside the activation boundary | §10.3 (ordered eligibility, namespace-consistent probe lifecycle) and §14.2 (final-boundary observation, guard-removal control) |
| F-012 two-file publication / stop and retention contradictions | §5.3 (one authoritative document, derived current, `stopping` from every launched state, immutable retention) |
| F-013 authority protocol under-specified / fixture could select legacy | §4.5 (proof type, `allows`, no-target operations, `authorize`/`confirm` placement) and §14.3 (public-shape acceptance, legacy fixture kept separate) |

## 16. Residuals

- A hostile same-UID process is out of scope (§3) and is disclosed.
- Evidence-based ownership cannot distinguish our daemon from a same-user impersonator that binds the entry first and
  reproduces the recorded command line; the 0700 directory, dirfd anchoring, peer credentials and the receipt narrow
  this to an attack that already requires the user's own privileges.
- Reported-source verification proves the *reported, complete* identity equals the requested monitor's geometry and
  that the unique matching output is the requested physical connector. It does not prove the compositor drew that
  monitor.
- Amendment 1's residual: the application may start to a receiver it cannot re-pair; a later credential demand makes
  every subsequent start fail with a typed refusal.
- Remembered trust still does not survive a restart on the adapter (`docs/operations.md`); this epoch does not change
  that, and the startup detail continues to say so.
