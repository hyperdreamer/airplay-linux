# Owned daemon control — plan v2 (revised against the scope review)

Status: **BLOCKED on an external prerequisite.** Do not begin phase 1 until P1 is satisfied, unless the intent is
explicitly to buy infrastructure with no user-visible capability.

## External preconditions

- **P1 — source identity (blocking).** The sender must report which output it captured. The upstream patch
  (`reports/upstream/0001-capture-report-selected-source.patch`, verified to build and pass `go test ./...` against
  `v0.5.0` with Go 1.27.3) exposes it to a caller; `StreamInfo` must also carry it before a supervisor can read it.
  Without P1 there is no way to satisfy HR-03 and this plan cannot deliver Mirror.
- **P2 — credential service proof.** `keyring` requires a real Secret Service round trip plus backend credential
  operations, including refusal without file fallback. Selecting `-cred-backend keyring` proves nothing.
- **P3 — not required for Mirror:** `krfb-virtualmonitor` (that is Virtual's blocker).

## Activation invariant (the review's ordering requirement)

No production detached spawn may exist until receipt production, verification, strict pidfd teardown and cross-process
reconciliation are all installed. **Every phase commit must carry a test proving that no production spawn occurs**, so an
intermediate commit cannot detach a candidate the remaining code cannot stop. Activation happens in exactly one place:
phase 4.

## Phase 1 — plumbing and codecs (production switch stays closed)

- **T1** `owned_daemon_control` capability: default false, composition-derived, and structurally impossible for
  `probe_backend` to set. `foreground_control` keeps its delivered meaning; no relabelling.
  *Accept:* a help-text-derived profile never sets it; a composition that can spawn does; an ambient-socket composition
  does not. *Mutation M1:* set it true inside `probe_backend` → the composition-derived test fails.
- **T2** Versioned reservation and launch-receipt codecs, persisted atomically (boot ID, app pid/`start_ticks`, owner UID,
  runtime-directory device/inode, endpoint generation, launch UUID). No launcher changes.
  *Accept:* round-trip and rejection of an unknown version, a missing field and a partial record.
  *Mutation M2:* drop the endpoint generation from the encoded receipt → codec test fails.
  *No-spawn proof:* a test asserting the production path performs no `spawn` while phase 1 is the current tree.

## Phase 2 — verification, teardown and reconciliation (still no launcher)

- **T3** Ownership verifier: dirfd-anchored endpoint identity, socket inode → owning pid, `start_ticks` equality, command
  line naming that exact socket, refusal on ambiguous FD holders or unreadable evidence.
  *Mutations:* **M3** accept a pid whose command line names a different socket; **M4** accept a mismatched `start_ticks`.
- **T4** Strict teardown: acquire a pidfd, re-validate identity after opening, signal **only** through the descriptor;
  refuse the numeric-PID fallback path entirely for owned-daemon termination; durable `stopping` intent before signalling;
  bounded exact-exit wait; endpoint cleanup; obligation retained on failure.
  *Mutation M5:* fall back to a numeric kill → test fails.
- **T5** Cross-process authority: shared-root lock, active-owner refusal, prior-instance reaping through the verifier,
  `CleanupRequired` retained when evidence is uncertain.
  *Mutation M6:* skip the shared-root lock → concurrent-startup test fails.
  *No-spawn proof:* the same phase-1 invariant test.

## Phase 3 — isolated idle launcher (not exposed to production)

- **T6** Daemonised spawn on a private socket inside the per-instance directory with **no receiver, capture or credential
  side effects**; every crash window tested explicitly — reservation→detach, detach→bind, bind→receipt, receipt→running,
  running→stopping. A failed verification must produce either exact cleanup or retained uncertainty that blocks
  replacement; it may never leave an unowned candidate and may never enable streaming.
  *Mutations:* **M7** leave the candidate running when verification fails; **M8** erase the durable receipt on failure.

## Phase 4 — gate integration and disclosure (the single activation commit)

- **T7** Migrate every consumer of `foreground_control`/`owner_handshake`, wire the credential-service proof (P2) and the
  source verification (P1+upstream), and expose the actual guarantee/refusal state in `README` and the startup capability
  detail. Activation requires a complete eligible composition; anything missing leaves the gate closed.
  *Mutation M9:* enable with source verification absent → acceptance test fails.

## Phase 5 — offline acceptance

- **T8** Production-composition acceptance using fakes only: no live devices, no real Secret Service. Negative tripwires
  prove live paths cannot run in the default suite, and each skip names the missing capability exactly.
  *Mutation M10:* permit a live path by default → tripwire test fails.

## Out of scope
Virtual mode; hostile same-UID defence; whole-screen mirroring; any relaxation of receiver, trust, provider, target
generation, pairing-compensation or cleanup gates.

## Reviewer items resolved by this revision
R1 receipt producer/format/lifetime + command peer (T2, T3) · R2 crash recovery and failed verification (T6) · R3 gate
migration and independent prerequisites (T1, T7, P1, P2) · R4 cross-process authority (T5) · R5 endpoint generation and
reconnect (T2, T4, T5) · R6 strict teardown without numeric fallback + activation order (T4, phase ordering) · R7 enforced
isolation and useful acceptance (T8) · R8 named mutations M1–M10 per task · R9 operator-visible bounded guarantee (T7).
