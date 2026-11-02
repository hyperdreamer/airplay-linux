# Owned daemon control — plan v2 (revised against the scope review)

Status: **P1 satisfied; P2 still blocks phase 4.** Phase 1–3 may proceed. `physical_source` may not become `True`
until T5a and T7 are delivered (`P2` is then the only remaining external prerequisite).

> **Amended for run `pm-run-20261030-040814-aefef9c0` (epoch 1).** The run recorded P1 satisfied locally by the
> carried source-reporting patches (measured then: `capabilities-v1`, `status-source`; see the design's §12 and
> `docs/backend-fork.md`), consistent with the PR-76 delivery recorded below. Those two tokens alone do not prove
> complete source reporting or physical identity; this epoch requires the additional `status-source-complete` token
> and source verification. The operator authorised Amendment 1 (`2026-10-30-owned-daemon-control-amendment-1.md`,
> reply: `A`), which changes exactly one admission precondition list — the `connect` gate — and keeps every pairing
> operation, remembered trust and every other gate closed. Read the amendment with this plan; where they differ on
> `connect`, the amendment governs. T5a's source-verification semantics and T7's single activation boundary remain
> authoritative.

## External preconditions

- **P1 — source identity (satisfied 2026-10-31).** The sender reports which output each stream captured.
  Delivered as `omarroth/doubletake#76` (head `2b5f1b0`), carried locally by
  `vendor/doubletake/0001-capture-report-selected-source.patch` and `0002-status-report-selected-source.patch`, and by
  the installed `/home/bin` binaries advertising `capabilities-v1` + `status-source` (`profile doubletake-0.5+source`,
  `source_reporting=True`). Per stream, `status` carries `source: {type, width, height, x, y, has_position}` and omits
  it when the portal reported nothing usable. `docs/backend-fork.md` records what the field does and does not mean.
  Remaining work is consuming it (T5a), not obtaining it.
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
- **T5a** Source identity verification, phase 2 (after T3–T5, before T7; added in revision 3 for P1). Join each
  stream's reported `source` with the facts already trusted here: `OutputInfo` (connector, logical `position`,
  current-mode size, `scale`, `virtual`) and the ownership store. Portal monitor streams report
  `position = screen->geometry().topLeft()` and `size = screen->size()` in device-independent coordinates, so
  `position` is the primary key and `size` is corroboration: a match requires
  `round(width/scale), round(height/scale)` to equal the output's current-mode size. Verify a stream only against its
  **own** requested connector (per `device_ip`), and only when the reported type is monitor, `has_position` is true,
  and exactly one enabled output matches; that output's connector must equal the requested one and must not be
  virtual or owned by the helper. Refuse — never fall through to an unverified capture — on a missing `source`,
  missing `has_position`, no match, several matches, a scale disagreement, or a non-monitor type.
  The verifier returns a typed observation and the production default is **observe only**: the outcome (reported
  source plus matched connector, or the refusal reason) is surfaced in status/diagnostics so real layouts can be
  checked before it gates anything. No gate changes in this task; T7 is the single commit that acts on it.
  *Accept:* a 1.5-scaled output whose portal size is the logical size matches its connector; two outputs sharing a
  position are refused as ambiguous; a window-typed source, a source without `has_position`, a source matching a
  virtual or owned output, and a source matching a different connector each refuse with their own reason; two
  streams with different sources are verified independently, so one mismatch does not disturb the other; an
  observing verifier never blocks a stream.
  *Mutations:* **M11** compare portal size with the mode size directly, ignoring `scale` → the scaled-layout test
  fails. **M12** accept the first candidate when several outputs share the position → the ambiguity test fails.
  **M13** treat a missing `has_position` as a real `(0,0)` → the absent-position test fails. **M14** drop the
  virtual/owned-output exclusion → the self-capture test fails.
  *No-spawn proof:* the same phase-1 invariant test.

## Phase 3 — isolated idle launcher (not exposed to production)

- **T6** Daemonised spawn on a private socket inside the per-instance directory with **no receiver, capture or credential
  side effects**; every crash window tested explicitly — reservation→detach, detach→bind, bind→receipt, receipt→running,
  running→stopping. A failed verification must produce either exact cleanup or retained uncertainty that blocks
  replacement; it may never leave an unowned candidate and may never enable streaming.
  *Mutations:* **M7** leave the candidate running when verification fails; **M8** erase the durable receipt on failure.

## Phase 4 — gate integration and disclosure (the single activation commit)

- **T7** Migrate every consumer of `foreground_control`/`owner_handshake`, wire the credential-service proof (P2) and act on
  the source verification produced by T5a (P1), and expose the actual guarantee/refusal state in `README` and the startup
  capability detail. Activation requires a complete eligible composition and no standing source refusal; anything
  missing leaves the gate closed.
  *Mutation M9:* enable while T5a's refusals are ignored → acceptance test fails.

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
isolation and useful acceptance (T8) · R8 named mutations M1–M14 per task · R9 operator-visible bounded guarantee (T7) ·
R10 consuming the reported source without overclaiming it (P1, T5a, T7).
