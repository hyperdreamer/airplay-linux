# Owned-daemon epoch — design

- **Status:** draft for review
- **Contract:** `docs/superpowers/plans/2026-10-30-owned-daemon-control.md` (plan v2, the reviewed contract)
- **Handoff:** `docs/handoff-owned-daemon-epoch.md`
- **Epoch:** 1, run wave 1
- **Scope:** Mirror mode against the locally patched `doubletake` 0.5.0 build that reports its captured display.
  Virtual mode is out of scope.

## 1. Background

The application already ships a 30-task implementation of sessions, transport, display, trust, diagnostics and UI,
with every gate closed because the published backend cannot prove the operations those gates require. Two of those
gates — `foreground_control` and `owner_handshake` — were written for an *authenticated* daemon control channel that
`doubletake` does not have and will not have. `docs/backend-compatibility.md` records the resulting dead end and the
two ways out; this epoch takes the first one deliberately:

> **Adopt the cooperative single-user threat model explicitly.** Ownership is evidence-based: the daemon is trusted
> because it lives in a directory only we created, its identity matches a launch receipt, and its socket inode, owning
> process and command line agree. A hostile same-UID process is out of scope: on this desktop it already owns the
> session.

`physical_source` is blocked for a second, independent reason: the released backend cannot be told which monitor to
send and does not report which one it captured. This machine carries a locally patched build (`/home/bin/doubletake`,
profile `doubletake-0.5+source`, `source_reporting=True`) whose `status` reply reports the selected display identity
(`source_type`, size, position) per stream. That makes Mirror verifiable for the first time — but only if the
application *consumes* that report and refuses an unverifiable capture.

Both changes are switches on a live control path that can start and stop a real daemon. The contract's activation
invariant therefore governs the whole epoch:

> No production detached spawn may exist until receipt production, verification, strict pidfd teardown and
> cross-process reconciliation are all installed. **Every phase commit must carry a test proving that no production
> spawn occurs**, so an intermediate commit cannot detach a candidate the remaining code cannot stop. Activation happens
> in exactly one place.

## 2. Goals and non-goals

### Goals

1. `owned_daemon_control` is a capability that only a *composition* can affirm, that admission consumes instead of
   the unprovable `foreground_control`/`owner_handshake` pair, and that the control adapter consumes through an
   injected ownership authority so no mutating request is authorized by a flag alone.
2. Durable, versioned reservation and launch-receipt records carry every fact the verifier needs, published atomically.
3. Ownership is proven from evidence: dirfd-anchored socket identity, socket inode → owning pid, matching
   `start_ticks`, a command line naming that exact socket, matching owner UID, and the recorded runtime-directory
   device/inode, all re-read at verification time.
4. Teardown of an owned daemon never signals a number: a pidfd is opened and re-validated, every signal goes through
   the descriptor, and an unavailable pidfd is a typed refusal, not a fallback.
5. A shared-root authority serialises daemon launch, reconciliation and whole-daemon shutdown across application
   processes, refusing while another live owner exists and reaping a prior instance only through the verifier.
6. The launcher starts an *idle* daemon on a private socket with no target, no capture request and no credential
   operation, and every crash window between reservation and `stopping` has a defined, tested outcome.
7. Activation is one commit: every consumer migrates, the credential-service proof and the source verification are
   wired, and the operator-visible guarantee/refusal text matches what is actually proven.
8. Acceptance runs on fakes only and proves with negative tripwires that no live path can run in the default suite.

### Non-goals

Virtual mode; hostile same-UID defence; whole-screen mirroring without a source identity; any relaxation of the
receiver, transport-admission, trust, provider, target-generation, pairing-compensation or cleanup gates; changing
the delivered `foreground_control`/`owner_handshake` *meanings* (they stay help-derived and stay false).

## 3. Threat model and the honesty boundary

| Adversary | In scope? | Why |
| --- | --- | --- |
| A crashed, restarted or duplicated *application* instance | yes | the failure mode that strands or double-starts daemons |
| A recycled PID, a replaced socket inode, a stale receipt from a previous boot | yes | ordinary races that must fail closed |
| A different user | yes | UID checks on directory, socket and process |
| A hostile process running as the same user | **no** | stated residual: it already owns the session and can drive the daemon through the same filesystem socket |

The residual is disclosed in `README.md` and in the startup capability detail in the same words the design uses here.
Nothing in this epoch may be described as authenticated ownership.

## 4. Capability model

### 4.1 `owned_daemon_control` is composition-only

`BackendFeatures` gains one field, `owned_daemon_control: bool = False`, with the same boolean-only invariant as its
siblings. It is **never** set by `features_from_help` or `probe_backend`: help text proves a command surface, not that
this composition can own a daemon. A pure composition function decides it:

```
derive_composition_capabilities(
    features,                      # help-derived, never trusted for ownership
    launcher: LaunchCapability,    # spawn seam present and idle-launch argv constructible
    ambient_socket: str | None,    # an inherited DOUBLETAKE socket, if any
) -> BackendFeatures
```

`owned_daemon_control` becomes true only when the launcher seam is present, the private-socket runtime root is
writable and no ambient socket is configured. An ambient-socket composition — the shape that would talk to a daemon
somebody else started — cannot affirm it, and a help-derived feature set never carries it at all.

`foreground_control` and `owner_handshake` keep their delivered meaning and stay `False` on every public backend
build. They are no longer *admission* inputs.

### 4.2 Credential-service proof (P2)

`keyring` likewise stops being a help-derived claim and becomes a composition-derived proof, because
`-cred-backend keyring` merely selects storage. The composition runs a bounded, receiver-free
`CredentialServiceProbe` before the capability report is finalised:

- **service leg** — one Secret Service introspection round trip through the existing D-Bus capability client
  (no unlock, no item creation);
- **backend positive leg** — launch the backend idle on a private socket in a throwaway directory with
  `-cred-backend keyring` inside a private network namespace (`unshare -rn`), require the control socket to appear and
  `status` to report `idle`, then tear it down; the backend constructs its keyring backend during startup, which is a
  real `keyring.Get` round trip and fails hard when the Secret Service is unreachable;
- **backend negative leg** — the same launch with `DBUS_SESSION_BUS_ADDRESS` pointing at a nonexistent socket, require
  a nonzero exit, no control socket and no credentials file created anywhere: the backend refuses instead of falling
  back to the file backend.

All three legs must pass. A missing `unshare`, a missing Secret Service, or an unreadable backend leaves `keyring`
false and admission refuses; the startup detail names the failed leg. The measurement that motivates every leg is in
`docs/backend-fork.md` and the handoff; the positive and negative behaviour of the installed build is recorded in
§12.

### 4.3 `physical_source` becomes true only with a verifier that consumes the report

`features_from_help` keeps `physical_source=False`. The composition affirms it only when all of these hold:

1. the backend advertises and this application verified `source_reporting` (`capabilities-v1` + `status-source`
   tokens **and** the patched help surface);
2. a `ReportedSourceVerifier` is wired into the transport (§7);
3. a physical capture provider is present and can inventory monitors.

Absent any one of them, `physical_source` stays false and Mirror stays gated. The verifier is what converts the
backend's report into evidence; without it the flag would be a claim.

### 4.4 Migration table (one row per consuming site)

| Site | Today | After |
| --- | --- | --- |
| `policy.capabilities.ADMISSION_BACKEND_CAPABILITIES` | `foreground_control, owner_handshake, keyring` | `owned_daemon_control, keyring` (both composition-derived) |
| `policy.capabilities` mode rubric | `admitted = …`, `mirror = … and physical_source` | unchanged shape; the three inputs are now the derived ones |
| `transport.DoubleTakeTransport._admit` | refuses unless `foreground_control and owner_handshake and keyring` | refuses unless `owned_daemon_control and keyring` |
| `process.DaemonRegistry._compatible` | same pair | `owned_daemon_control and keyring` |
| `process.supervisor.DaemonSupervisor` start check | same pair | `owned_daemon_control` for the public backend; the fixture profile keeps `foreground_control and owner_handshake and keyring` |
| `backend.control.PublishedDoubletakeControl._available` | every mutation and `shutdown` require `owner_handshake` | asks the injected `OwnershipAuthority` (§4.5); `owner_handshake` is no longer consulted by the evidence authority |
| `backend.control.PublishedDoubletakeControl.handshake` | refuses before I/O when `owner_handshake` is false | the evidence authority returns a `VerifiedEndpoint`-backed proof for the same record; the fixture authority keeps the delivered handshake |
| `backend.control.PublishedDoubletakeControl._revalidate_ownership` | requires the backend-reported owner token/instance/daemon/socket/protocol before every mutating request | the evidence authority re-proves the endpoint (socket `(device, inode)` unchanged, holder pid and `start_ticks` unchanged, generation current) immediately before and after the I/O; no backend-reported ownership field is required |
| `backend.control.PublishedDoubletakeControl._available(connect)` | requires `foreground_control, owner_handshake, multi_target, target_bound_pairing, physical/virtual source, provisional_commit, cancel_pairing, revoke_pairing` | requires `owned_daemon_control, multi_target, (physical_source or virtual_source)`; the pairing-transaction group is removed **from connect only** (§4.6) |
| `process.DaemonRegistry` readiness/release | `handshake()` + `shutdown` on the same control | `authority.prove(record)` for readiness and `authority.release(record)` (strict pidfd teardown) for release; the fixture authority keeps the delivered handshake |
| `process.supervisor._request_release` | handshake then shutdown request | `authority.prove` then `authority.release`; the fixture profile keeps the handshake request |
| `runtime._control_factory` | binds `owner_token`, `instance_uuid`, `protocol_version` into the profile | additionally binds the current receipt and the dispatch-fence source, and selects the authority from the derived features |
| `runtime._capability_gates` / startup detail | lists the missing admission flags | names the owning authority, the credential leg and the source verifier |
| `README.md` | documents the honest refusal | documents the cooperative-same-user guarantee, the residual and the exact refusal conditions |

No site may read `foreground_control`/`owner_handshake` for admission after activation; the fields remain on the
dataclass for the application-contract fixture, whose handshake path continues to work unchanged.

### 4.5 The control authorization seam (post-activation)

`backend/control.py` is the one module the delivered migration table missed: it is both an admission consumer and the
only place that authorizes a mutating request. It gains one injected collaborator, so the control never decides
*which* ownership evidence is acceptable by itself:

```
class OwnershipAuthority(Protocol):
    async def prove(self, record: DaemonRecord) -> Result[Proof]      # pre-flight / readiness
    async def authorize(self, record, operation) -> Result[Proof]    # immediately before mutating I/O
    async def confirm(self, record, proof) -> Result[None]           # immediately after it
    async def release(self, record) -> Result[None]                  # typed teardown only
```

Two implementations, selected by the derived features at composition time:

- **`HandshakeAuthority`** — the delivered behaviour, unchanged: `owner_handshake` gating, `handshake()` proof,
  backend-reported `owner_token`/`instance_uuid`/`daemon`/`socket_path`/`protocol_version` equality, fence checked
  before the request. It is selected only for a profile that affirms `owner_handshake` (the application-contract
  fixture).
- **`EvidenceAuthority`** — the public-backend path: `prove`/`authorize` run `verify_owned_endpoint(receipt)` (§6),
  require the verified generation to be the registry's current generation, and require the recorded dispatch fence
  (`target.receiver_id`, endpoint generation and fingerprint) to match the request's target. `confirm` re-checks the
  socket `(device, inode)` and that the same pid/`start_ticks` still holds it, so a replaced endpoint is detected even
  if it happened mid-request. `release` is the strict pidfd path (§8).

Consequences that the plan must carry:

1. `_available()` asks `authority.allows(operation)`; the authority answers from the derived features *and* the
   evidence, never from `features.owner_handshake`.
2. `handshake()` becomes `authority.prove(record)` for the evidence authority, so `DaemonRegistry` and the supervisor
   keep one call shape.
3. The dispatch fence stays mandatory and stays evaluated immediately before mutating I/O. Removing it would be a
   target-generation relaxation, which the contract forbids.
4. The production factory injects the current receipt and the authority; it never constructs a control with an
   unproven record.

### 4.6 Connect admission after activation

The delivered `connect` gate also required the pairing-transaction group (`target_bound_pairing`, `provisional_commit`,
`cancel_pairing`, `revoke_pairing`). That group stays closed for the public backend, so a mechanical swap of the
ownership pair alone would leave Mirror unusable. The migration is deliberately narrow:

- `connect` requires `owned_daemon_control ∧ multi_target ∧ (physical_source ∨ virtual_source) ∧` the delivered
  trust fence (no `CleanupRequired`/`RevocationPending`, unchanged) `∧` the exact-target dispatch fence (unchanged).
- **every pairing operation** (`begin/submit/prepare/commit/cancel/revoke/mark_invalid`), `remembered_pairing`, and the
  provisional-commit compensation path keep their delivered capability requirements and are not touched.
- a start whose backend acknowledgement reports a credential challenge is **refused and disconnected for that exact
  target**, returning `dependency_incompatible`: submitting a credential requires the gated pairing channel, so the
  application must not leave a half-started stream behind. This is a typed refusal with a test, not a silent gap.

No receiver, trust, provider, target-generation, pairing-compensation or cleanup check is removed by this row; only the
*precondition* that a connect must itself be able to pair is dropped, because this backend cannot pair and Mirror does
not require pairing for an already-trusted receiver.

## 5. Durable records

Two new versioned record types, both persisted through `AtomicFiles` (temp + fsync + rename + directory fsync), both
in `domain/records.py` with codecs in `storage/codecs.py` and a `JsonLaunchReceiptStore` repository:

### 5.1 `DaemonReservation` (`airplay-linux/daemon-reservation`, version 1)

Written **before** any spawn, so a crash between spawn and publication leaves something to reconcile.

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

The reservation fields plus the observed launch state. One record per `launch_uuid`, retained after failure.

| Field | Meaning |
| --- | --- |
| `state` | `reserved` → `detached` → `bound` → `ready` → `stopping` → `stopped`; or `failed` |
| `supervisor`, `daemon` | `{pid, start_ticks}` once observed (nullable) |
| `socket_device`, `socket_inode` | `fstat` of the socket via the dirfd once bound |
| `status_state` | the `state` field of the last `status` reply (`idle`, …) |
| `credential_service` | `keyring_proven` \| `file_refused` \| `unknown` — evidence, never a secret |
| `failure` | the diagnostic code of the transition into `failed` |
| `updated_at` | store clock |

Rules:

- codecs reject an **unknown version**, a **missing field**, and any **partial record** (a `bound` state without
  socket identity, a non-null `daemon` without `supervisor`, a `ready` without a `status_state`);
- the receipt never contains a token, PIN, password, credential reference or environment dump;
- an old receipt is never overwritten by a newer launch: the store keeps each `launch_uuid` and the registry records
  which one is current, so a stale generation is detectable rather than erased;
- `endpoint_generation` is what makes a rebuild of the same socket path detectable; the socket inode recorded at
  `bound` is what proves the generation actually refers to the live endpoint.

### 5.3 One writer, one pointer, explicit transitions

The registry is the **only** writer of both record types. The supervisor never writes a receipt: it keeps publishing
its own and the child's identity into the delivered `DaemonRecord` store (the existing, tested observation channel),
and the registry copies those observations into the receipt. This removes a two-writer crash window entirely and keeps
the delivered supervisor handshake intact.

Receipt transitions are compare-and-publish, each with a precondition that the registry checks while it holds the
in-process registry lock and the shared-root authority (§9):

| Transition | Precondition | Written fields |
| --- | --- | --- |
| `reserved` (create) | no current receipt for this endpoint, or the previous one is `stopped`/`failed` and its generation is provably finished | all reservation fields, `endpoint_generation = previous + 1` |
| `reserved → detached` | the store record for this `instance_uuid` names the supervisor and the daemon identity, and the daemon identity equals the spawned child identity | `supervisor`, `daemon` |
| `detached → bound` | the socket exists via the dirfd; the verifier proves the endpoint from a `reserved`-quality receipt | `socket_device`, `socket_inode` |
| `bound → ready` | verification still holds and `status` answers `state == idle` | `status_state`, `credential_service` |
| any → `failed` | a typed failure; the receipt is **retained** | `failure` |
| `ready → stopping` | whole-daemon shutdown admitted | — |
| `stopping → stopped` | strict teardown proved exact absence | — |

The current-launch pointer is `state_root/daemon/current.json` — `{schema, launch_uuid, endpoint_generation}`, written
in the same operation as the transition that creates the receipt. Reconciliation compares the pointer, the receipt and
the live verifier; a mismatch is `stale_event`, never an adoption.

Config handoff: the registry keeps the delivered config-pipe contract (owner token, protocol, record root, executable,
features) and adds two fields — `launch_uuid` and `endpoint_generation` — so the supervisor's environment and any
diagnostics can be correlated with the receipt without ever carrying a secret. The supervisor still refuses to start
when the reservation does not match its arguments.

## 6. Ownership verifier

New module `process/ownership.py`. One entry point:

```
verify_owned_endpoint(receipt, *, proc_root=/proc) -> Result[VerifiedEndpoint]
```

`VerifiedEndpoint` carries the daemon identity, the socket `(device, inode)`, the runtime-directory `(device, inode)`
and the receipt generation. The check list, in order, each failing closed:

1. **Dirfd anchoring.** Open the runtime directory from `/` with `O_NOFOLLOW|O_DIRECTORY`, require it to be a
   directory owned by `owner_uid`, mode `0700`, and require its `fstat` `(device, inode)` to equal the receipt's.
2. **Socket identity via the dirfd.** `fstat` the socket name relative to that descriptor; require `S_ISSOCK`, owner
   `owner_uid`, and — when the receipt is already `bound` — equality with the recorded socket `(device, inode)`.
3. **Inode → owning pid.** Read `/proc/net/unix`, find the row whose path equals the socket path and take its inode.
   Scan `/proc/<pid>/fd` for exactly one process holding a socket whose inode matches. Zero holders, two or more
   holders, an unreadable `/proc/net/unix` or an unreadable candidate `fd` directory is a refusal
   (`ownership_mismatch`) — never a "best candidate".
4. **`start_ticks` equality.** `read_identity_evidence(pid)` must be `present` and its `start_ticks` must equal the
   receipt's `daemon.start_ticks`.
5. **Command line names that exact socket.** `/proc/<pid>/cmdline` must contain the socket absolute path as the value
   of `-socket` (and `-daemonize`), with no other `-socket` value. A pid whose command line names a *different*
   socket is a refusal even when it holds the inode.
6. **Owner UID.** The process's `/proc/<pid>` must be owned by `owner_uid`.
7. **Boot identity.** The receipt's `boot_id` must equal the current boot id; a receipt from another boot is stale and
   is never used to adopt or signal anything.
8. **Generation.** `receipt.endpoint_generation` must equal the generation the registry recorded as current; a
   superseded receipt is `stale_event`.

The verifier reads; it never signals, never unlinks, never writes.

## 7. Reported-source verification (Mirror safety)

New module `capture/source.py`:

```
BackendSource(type: int, width: int, height: int, x: int, y: int)          # decoded from status
verify_reported_source(requested: str, reported: BackendSource,
                       layout: tuple[OutputInfo, ...]) -> Result[None]
```

Acceptance requires **all** of: `type == 1` (portal monitor source); exactly one physical output in a fresh KScreen
observation whose `(width, height)` and `(x, y)` equal the reported size and position; and that output's connector
equal to the requested connector. Zero matches, two matches, a window source, an unknown type, or a layout that
cannot be read all fail with `unsupported_source`.

Where it runs: `DoubleTakeTransport.start_stream` sends `connect <target>`, then polls `status` (bounded by the
existing startup timeout) until the stream for that target carries a `source`. On success the stream is admitted; on
mismatch, timeout or refusal the transport disconnects **that target only** and returns the typed failure, so an
unverified capture is never left running.

Decoder rule: the status `source` object has Go's default field names (`Type`, `Width`, `Height`, `X`, `Y`) and is
decoded strictly — integers only, non-negative, no unknown keys tolerated as evidence. An absent `source` decodes to
`None`, which the verifier treats as "unverified", never as "fine".

The data path is pinned end to end, so a profile carrying `source_reporting` can never stand in for a verifier:

1. `backend/contract.py`: `BackendStream` gains `source: BackendSource | None = None`, validated like its siblings;
   `decode_status` calls a strict `_decode_source` that rejects non-mapping values, non-integer or negative members,
   and a `source` key with no usable identity.
2. `capture/source.py`: the pure `verify_reported_source` function plus a `ReportedSourceVerifier` port whose
   production implementation reads a fresh layout from the injected `KScreenObserver` and refuses when the
   observation is stale, unreadable or ambiguous.
3. `transport.DoubleTakeTransport.__init__` takes `source_verifier: ReportedSourceVerifier | None`; `_start` calls it
   after the connect acknowledgement and before `ready`/`emit`, and on any failure calls `_stop_owned` (disconnect of
   that exact target) before returning `unsupported_source`.
4. `runtime._compose_production` constructs the verifier and passes it to the transport, and
   `derive_composition_capabilities` sets `physical_source` only when that same verifier object was wired. A
   `source_reporting` backend with no verifier yields `physical_source=False`.

## 8. Strict teardown

`ProcessPort.terminate` gains a strict keyword (`strict=True` is used for every owned-daemon termination):

1. classify the identity; a mismatch is a refusal, an absent process is success;
2. open a pidfd; **if no pidfd can be opened, refuse** with `dependency_incompatible` and signal nothing — the
   numeric branch remains only for the non-owned paths that already rely on it;
3. re-validate the identity *after* opening the descriptor (§6 steps 4–6 against the same start ticks and socket);
4. deliver `SIGTERM` through the descriptor only;
5. wait for exact exit with a bounded deadline, polling the descriptor (never a PID);
6. escalate to `SIGKILL` through the descriptor, then require exit within the kill bound;
7. only after proven exit: unlink the socket through the dirfd after re-checking `(device, inode)` and proving no
   live holder, and delete the receipt.

A durable `stopping` intent is written **before** the first signal. On any failure the receipt, the obligation and the
`cleanup_required` notice are retained; teardown never reports success it did not observe.

Serialisation: a per-receiver `Stop` issues `disconnect <exact target>` on the authenticated control and nothing else.
The shared daemon is terminated only inside `DaemonRegistry.shutdown()` (whole-application shutdown) while the
shared-root authority is held.

Explicit contract (F-003):

- port signature `terminate(identity, owner_uuid, runtime_directory, deadline, *, strict: bool = False)`;
- `strict=True` is passed by every owned-daemon call site: `DaemonRegistry._stop`, `DaemonRegistry.shutdown`,
  `EvidenceAuthority.release`, `DaemonSupervisor._request_release` and the credential-probe teardown. A test asserts
  the call sites pass it, so a future call cannot silently inherit the non-strict path;
- `strict=True` and no pidfd ⇒ `Err(dependency_incompatible)` **before** any signal, never `os.kill`;
- the receipt-backed identity is revalidated after the descriptor is opened and again immediately before the first
  signal; a mismatch closes the descriptor and refuses;
- the bounded exact-exit wait polls the descriptor (a process that is not our direct child has no `wait` obligation), so
  "exited" is proven by the descriptor becoming readable, not by a PID probe;
- the non-strict branch is retained only for the already-delivered non-owned paths and is asserted to be unreachable
  from the owned-daemon registry.

## 9. Cross-process authority

New module `process/authority.py`:

- lock file `<state_root>/daemon/authority.lock` (`flock`, non-blocking with bounded retry);
- owner record `<state_root>/daemon/authority.owner` — `{schema, instance_uuid, app: {pid, start_ticks}, boot_id,
  acquired_at}` written atomically after the lock is held;
- `acquire(instance) -> Result[AuthorityLease]` refuses with `ownership_mismatch` while another owner is recorded and
  its process is *proved live* (identity + boot + UID); a recorded owner that is provably absent/terminated is
  replaced inside the same critical section; unreadable evidence retains `CleanupRequired` and refuses.

**Lease lifetime.** The lease is per mutating operation, not for the process lifetime: `_reconcile()`, `_launch()` and
`shutdown()` acquire it, perform their work, and release it in a `finally`. The lock therefore only serialises
mutations; *durable* ownership is carried by the owner record, which names the application instance that currently
owns the daemon and stays until that instance transfers or clears it in its own shutdown. A second application
instance can always read the record, and refuses while the named owner is proved live; it never waits forever and
never adopts a live daemon. `release()` is a compare-and-remove of the exact record it wrote, so a stale release can
never delete a successor's owner record; an unreadable or mismatched record leaves the lease held and reports
`CleanupRequired`.

The registry takes the authority for `_reconcile()` (prior-instance reaping through the verifier), `_launch()` and
`shutdown()`; `ensure()` on an already-ready record does not need it but re-verifies ownership each time. A second
application instance therefore cannot launch a second daemon, cannot reap the first one's, and cannot silently adopt
it.

## 10. Isolated idle launcher

### 10.1 The launcher capability seam

`LaunchCapability` is a small protocol in `process/launcher.py` so the composition — never a probe — decides whether
this process can own a daemon:

```
class LaunchCapability(Protocol):
    def available(self) -> bool          # a verified daemon-mode surface and a writable, owned private root
    def argv(self, socket_path: str) -> tuple[str, ...]
    def ambient_socket(self) -> str | None
```

The production implementation is `BackendLaunchCapability(executable, features, runtime_root, environment)`:
`available()` requires the help surface to expose `-daemonize`, `-socket` and `-cred-backend` (already proven by the
pinned profile), the runtime root to be an absolute, owned, writable directory that is not a symlink, and
`ambient_socket()` to return `None`; `argv()` is the exact idle shape below. One call site decides the capability —
`runtime._compose_production`, immediately after `probe_backend` and before the registry is constructed:

```
features = derive_composition_capabilities(
    probed, launcher=BackendLaunchCapability(...), credential_proof=..., source_verifier=...)
```

Nothing else may set `owned_daemon_control`; a test asserts the only assignment site is this composition function.

### 10.2 Launch shape

```
doubletake -daemonize -socket <runtime_directory>/daemon.sock -cred-backend keyring
```

- no `-target`, `-code`, `-pair`, `-test`, `-x11-*`, `-creds`: nothing resolves a receiver, selects a capture source or
  performs a credential operation;
- `-cred-backend keyring` is mandatory: it is what forbids the file fallback at credential time, and its construction
  is the positive credential-service leg;
- the child stays the supervisor's direct child (the installed build does not fork), keeps the
  parent-death `SIGKILL` protection, and its identity is published in the receipt as it becomes observable;
- readiness = socket present **and** verifier proof **and** `status` answers `state == idle` **and** the daemon
  identity equals the spawned child identity.

Crash windows, each an explicit test:

| Window | Required outcome |
| --- | --- |
| reservation → spawn | receipt `failed`; no process; directory and socket absent or exactly removed |
| spawn → bind | verified child terminated by pidfd; receipt retained `failed`; replacement allowed only after proof of absence |
| bind → receipt | socket identified, daemon verified, receipt completes; a verification failure terminates the exact daemon and retains the receipt |
| receipt → running | `status` failure or non-idle state → terminate exactly, retain `failed` receipt, block replacement while uncertainty remains |
| running → stopping | `stopping` intent durable before the first signal; exact-exit wait; obligation retained on failure |

A failed verification may leave either an exact cleanup or a *retained uncertainty* that blocks replacement. It may
never leave an unowned candidate and may never enable streaming.

## 11. Activation (one commit)

The activation commit contains, together:

1. §4.4's migration for every site — including `backend/control.py`'s `_available`, `handshake` and
   `_revalidate_ownership` through the `OwnershipAuthority` seam (§4.5) and the narrowed `connect` admission (§4.6);
2. the credential-service probe and its capability wiring;
3. the reported-source verifier, the strict status decode and the `physical_source` derivation (§7);
4. the operator disclosure (README guarantee/refusal/residual, startup capability detail naming the owning authority,
   the credential leg and the source verifier);
5. the activation-gated no-spawn test: with any one element missing (no owned-daemon capability, no credential proof,
   no source verifier, no control authority), the production composition performs **zero** spawns and admission
   refuses.

Documentation corrections shipped with the same commit: `docs/backend-fork.md` must apply the full carried chain
(`vendor/doubletake/0001-…`, `0002-…`, `0003-…`) in order, because the application requires the capability tokens from
`0003` to verify the patched surface; the plan's `reports/upstream/0001-…` path is superseded by that chain, and the
recorded build identity is the three-patch chain against `v0.5.0`. T6 is stated precisely as **no receiver selection,
no capture request, no credential mutation and no credential material in any application record**; the bounded
read-only keyring construction performed by the backend at startup is required by P2 and is the only credential-side
operation the launcher causes.

Nothing in the activation commit relaxes an existing gate: receiver, transport admission, trust, provider, target
generation, pairing compensation and cleanup keep their delivered checks.

## 12. Measured evidence for the external preconditions

Recorded on this machine before design freeze (receiver-free, inside `unshare -rn`, private socket, temporary state):

- patched backend: `doubletake -capabilities` → `capabilities-v1`, `status-source`; help surface matches the pinned
  patched layout; `status` reply shape for a stream carries `source` (Go field names).
- `doubletake -daemonize -socket <private> -cred-backend keyring` idles, creates `daemon.sock` (`srwx------`) and
  `daemon.sock.lock`, answers `{"ok":true,"state":"idle"}`, exits 0 on `SIGTERM`.
- the same launch with an unreachable session bus exits 1 with `keyring backend: system keyring not available`, creates
  no control socket and no credentials file.
- the socket appears in `/proc/net/unix` with its path and inode, which is what §6 step 3 consumes.

These measurements make P1 and P2 satisfiable here; they are not themselves the proof the shipped code needs, which is
why §4.2 and §7 are implemented and tested rather than asserted.

## 13. Error handling

Every refusal uses an existing catalog code — `ownership_mismatch` (evidence disagreement, ambiguity, refused
authority, network-namespace or backend evidence missing), `stale_socket`/`stale_event` (superseded endpoint or
generation), `daemon_failed` (readiness/exit failures), `cleanup_timeout` (unconfirmed teardown),
`dependency_incompatible` (no pidfd, no `unshare`), `unsupported_source` (unverifiable reported source). No new code,
no backend exception text and no path beyond the runtime directory appears in a diagnostic. Application metadata never
contains a PIN, secret or credential reference; the receipt's `credential_service` field is a three-state evidence
token, not a reference.

## 14. Test strategy

### 14.1 Named mutations (from the contract; each must be killed by a named test)

| ID | Mutation | Must fail |
| --- | --- | --- |
| M1 | set `owned_daemon_control=True` inside `probe_backend` | the composition-derived capability tests |
| M2 | drop `endpoint_generation` from the encoded receipt | the receipt codec round-trip test |
| M3 | accept a pid whose command line names a different socket | the verifier's command-line test |
| M4 | accept a mismatched `start_ticks` | the verifier's identity test |
| M5 | fall back to a numeric `kill` when no pidfd exists | the strict-teardown test |
| M6 | skip the shared-root lock | the concurrent-startup test |
| M7 | leave the candidate running when verification fails | the launcher crash-window test |
| M8 | erase the durable receipt on failure | the launcher retention test |
| M9 | enable `owned_daemon_control` with source verification absent | the activation acceptance test |
| M10 | permit a live path by default | the negative-tripwire test |

Each mutation is applied in a disposable copy, the killing test is run from the lane or audit worktree, and the
survivors (if any) are reported by name rather than waved through.

### 14.2 No-spawn proofs

Every phase commit carries `tests/acceptance/test_no_production_spawn.py`, which composes the production path with a
recording process port and asserts zero `spawn` calls while activation is incomplete. In phase 4 the same file gains
the completeness matrix (four elements: owned-daemon capability, credential proof, source verifier, receipt +
teardown + authority present): each missing element must yield zero spawns and a typed refusal.

### 14.3 Activation acceptance matrix (F-001)

A dedicated acceptance file proves the seam rather than the flag:

| Composition | Expected |
| --- | --- |
| complete owned composition, fake backend + fake verifier + fake credential witness | launch admitted; `status` succeeds; `connect` is authorized by the evidence authority; a target-scoped `disconnect`/`mute`/`unmute` is authorized with a matching fence; whole-daemon shutdown releases by strict teardown |
| evidence authority with a receipt whose generation or socket inode no longer matches | every mutating operation refused before I/O; no spawn; `stale_event` |
| fence mismatch (receiver/endpoint generation) | refused before I/O; `stale_target` |
| `owned_daemon_control` absent | zero spawns; `dependency_incompatible` |
| credential proof absent | zero spawns; `dependency_incompatible` naming the credential leg |
| source verifier absent with `source_reporting=True` | `physical_source=False`, `connect` refused, zero spawns |
| credential-challenging acknowledgement on connect | exact-target disconnect, `dependency_incompatible`, no pairing attempt |

### 14.4 Offline acceptance (phase 5)

A fake backend that speaks the delivered control protocol (delivered fixture profile, no network), a fake KScreen
observer and a fake Secret Service witness: production composition, launch, verify, per-receiver stop, whole-daemon
shutdown, restart after crash, and a refusal matrix. Negative tripwires: default-suite tests fail if a live
`doubletake`, a live D-Bus session bus, or a live socket lease is reachable from an unmarked test; each skip message
names the missing capability exactly (`owned daemon control`, `credential service proof`, `physical source
verification`).

### 14.5 Suite hygiene

Full suite headless (`QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8`), short `--basetemp`, real-process
tests against the repo installed in `~/.venvs/airplay-linux`. The known supervisor flake is recorded, not masked.

## 16. Review resolutions (design review 1)

| Finding | Resolution in this document |
| --- | --- |
| F-001 blocker — control authorization seam | §4.5 (`OwnershipAuthority`, two implementations, `_available`/`handshake`/`_revalidate_ownership`, fence kept), §4.6 (narrow `connect` admission and the credential-challenge refusal), §14.3 (acceptance matrix). |
| F-002 major — source verification not wired | §7 data path 1–4 (contract field, strict decoder, verifier port, transport injection, capability derivation). |
| F-003 major — pidfd-only teardown | §8 explicit contract: `strict` keyword, refusal before signal, revalidation after open, poll-based exact exit, named strict call sites, non-strict branch unreachable from the registry. |
| F-004 major — receipt lifecycle and supervisor handoff | §5.3 single writer (registry), compare-and-publish transition table with preconditions, `current.json` pointer, config-pipe additions (`launch_uuid`, `endpoint_generation`); the supervisor keeps the delivered `DaemonRecord` publication. |
| F-005 major — composition authority not connected | §4.1 + §10.1 `LaunchCapability` protocol, the single composition call site, the ambient-socket rule; §9 per-operation `AuthorityLease` around `_reconcile`/`_launch`/`shutdown`, durable owner record, compare-and-remove release. |
| F-006 minor — evidence/isolation wording | §11 documentation corrections: the full three-patch chain, `vendor/` identities, precise T6 wording (no credential mutation/material; only the bounded read-only keyring construction required by P2). |

## 17. Residuals

- A hostile same-UID process is out of scope (§3) and is disclosed.
- Evidence-based ownership cannot distinguish our daemon from a same-user impersonator that binds the socket first and
  reproduces the recorded command line; the 0700 directory, dirfd anchoring and receipt narrow this to an attack that
  already requires the user's own privileges.
- Reported-source verification trusts the portal's report and the compositor's observation; it proves the *reported*
  source equals the requested monitor's geometry, not that the compositor drew that monitor.
- Remembered trust still does not survive a restart on the adapter (`docs/operations.md`); this epoch does not change
  that, and the startup detail continues to say so.
