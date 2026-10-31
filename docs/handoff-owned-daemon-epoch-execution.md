# Handoff — the owned-daemon epoch, execution in progress

This document is for the session that continues `pm-run-20261030-040814-aefef9c0`. Read
`docs/superpowers/plans/2026-10-30-owned-daemon-control.md` (contract), its `-amendment-1.md` (operator
decision `A`, plus Clarification 1.1), the approved design, the reviewed specification, and the derived
execution manifest before acting. The plan and manifest are pinned; do not re-plan.

## Where the run stands

```text
contract      plan v2 + Amendment 1 (operator reply A) + Clarification 1.1     committed
design        approved, snapshot de04d23 / f0708be9 / sha256 b55faca1…         committed
spec          reviewed (3 rounds: 4 blockers, 2 blockers, PASS), R1–R13        committed 76c0ec5
plan          10 tasks, 82 named tests, 9 lanes                                committed 8bbf11e
manifest      MANIFEST_VALID tasks=10 lanes=9 interfaces=34                    committed (ownership of
                                                                               T-004 widened for OI-11)
integration   branch pm/owned-daemon-epoch/run-pm-run-20261030-040814-aefef9c0
              HEAD 1bb1da9, generation 1
delivered     T-001 capability  INTEGRATED (44 tests in the candidate)
              T-002 receipts    implemented (1e7b64d), task review in flight
              T-003 verifier    not started   (ready: no dependencies)
              T-004 teardown    not started   (ready; projection carries OI-11)
              T-005…T-010       not started   (blocked on their dependencies)
```

Ledger: `$STATE_ROOT` = `/data/home/henry-arch/.local/state/pi/project-manager/runs/
2f85929c2b2e9a7bef4df2b51d4f73eee4d6202cfdab6e68ef6ff9c831e8f436/pm-run-20261030-040814-aefef9c0`
(`state.json`, `events.ndjson`, `receipts/`, `projections/`, `archives/`, `reports/`). The PM phase is
`SCHEDULING_ACTIVE`; graph renders come from `pm-graph.mjs` with `--manifest` pointing at the derived
YAML, which is what makes the lane topology appear.

## Tools that stay outside the repository

- `pm-ledger.py` — revision-checked atomic ledger updates (`init`, `update`, `show`).
- `derive-manifest.py` / `validate-manifest.py` — manifest derivation and the ten-rule gate.
- `derive-projection.py` — lane projections (provenance into Global Constraints, task remapping, consumed
  interfaces, host-transition clause).
- `sdd.py` — thin driver over the unchanged `sdd-state` (`show`, `gate`, `intent`, `transition`). It never
  reimplements the reducer, and every prompt comes from `sdd-state render-prompt`.

Lane worktrees live under `PM_WORKTREE_ROOT/lanes/<lane>/epoch-1/attempt-<n>`; each lane-local SDD run
root is beneath its own worktree at `.superpowers/sdd/lane-<lane>-epoch-1-attempt-1`.

## Continuing execution

1. For a lane with no run yet: create the worktree from `integration.current_commit_oid`, run
   `sdd-workspace` **from inside that worktree** with the projection path, `sdd-state init` with
   `--repo-root /data/home/guest/Development/airplay-linux`, then `sdd.py gate <root> exact`.
2. `sdd.py intent <root> implementer 1 '<expected outcome>'`, then `spawn_subsession` with the rendered
   prompt bytes and `tier` from the intent, then record `dispatch-started` with the returned session id.
   Batch: `T-003` and `T-004` are ready now; `T-007` becomes ready once `T-001` is integrated (it is).
3. After an implementer returns: `implementer-finished` → `implementer-status-recorded` → task-reviewer
   intent → reviewer → `task-review-finished` + `review-approved` → final-reviewer intent with
   `baseSha`/`headSha` → `final-review-finished` + `final-complete`.
4. Integrate through the candidate worktree: merge `--no-ff --no-commit` the lane branch onto the current
   integration tip, prove every staged path is inside the lane's manifest ownership, run the task's
   validation vector in the candidate, commit, then `git merge --ff-only` in the integration worktree,
   write the receipt, bump `integration.generation`, mark the task `INTEGRATED`, remove the candidate.

## Traps that cost this session time

- **Children die silently.** Three children returned idle after announcing intent, with no report. Check
  for the report file before believing a completion notice; archive the attempt and start a new one rather
  than reusing its worktree.
- **`TASK_REVIEW_RUNNING` has no re-dispatch edge.** The reducer accepts only `dispatch-mismatch` or
  `task-review-finished`. A dead reviewer therefore forces a quarantined attempt: record
  `recovery-ruling-recorded`, archive the run root, and start `attempt-2` (that is what `capability`
  attempt-1 did; its commit `2ffe54e` was retained as evidence, not adopted).
- **Concern notes are bounded to 256 characters**, dispatch keys must match
  `<runId>:task-<n>:<role>:attempt-<n>`, and the final-review role name in the key is `final`, not
  `final-reviewer`.
- **Run pytest from the candidate worktree**, with `--basetemp /tmp/oe<n>`; the venv is
  `~/.venvs/airplay-linux` (pytest installed there), headless env `QT_QPA_PLATFORM=offscreen
  PYTHONHASHSEED=0 LC_ALL=C.UTF-8`.
- **A review report can be contract-shaped and evidence-free.** The capability task review was 42 bytes
  with an empty findings list. The Step 9 frontier audit must re-derive from the diff, never from a
  review summary.

## Open items

- `OI-11` — `tests/testing/test_fakes.py` still implements the pre-epoch four-argument
  `ProcessPort.terminate`. Assigned to the teardown lane (`T-004`); its projection carries the obligation
  and its manifest ownership was widened to include the file. The full suite cannot pass until it lands.

## What has not been proven

No production spawn path exists yet: `T-009` is the only activation commit. `T-005`–`T-010` are
unimplemented, so the design's receipts, verifier, authority, launcher, credential proof, source
verification and acceptance matrix exist only as reviewed text. Nothing in this run has contacted a live
AirPlay receiver, and nothing should outside `unshare -rn` with a private socket.

## Receipts review outcome (recorded after the first handoff write)

`T-002`'s task review returned `SPEC: FAIL / QUALITY: CHANGES_REQUESTED` with **11 load-bearing
Important findings**, each with file:line evidence and disposable mutation probes. The lane's SDD state is
`TASK_REVIEW_DECISION` at revision 11, the findings are in the canonical finding ledger, and the fixer
package is written at
`<receipts run root>/finding-package-round-1.json` with `prior_attempts: []`.

Next action for that lane is a fix round 1 dispatch (`fixer`, tier = implementer tier), then re-review,
then final review, then candidate integration. The findings cluster into five mechanisms: durability
confirmation on identical republication (F-1), stage-complete receipt validation (F-2, F-3),
initial-publication/completion ordering and filename refusal (F-4, F-5), identity and privacy contracts
(F-6, F-7), encoder robustness and bounds (F-8, F-9), and test strength (F-10, F-11). F-11 also affects
the analogous test in `tests/storage/test_repositories.py`, and its correction must use the real
`build_runtime(..., overrides={'process': recorder})` entrypoint.
