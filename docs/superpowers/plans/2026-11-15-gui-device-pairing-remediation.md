# GUI Device Pairing Route B — Recovery Continuation Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the single residual carried out of the blocked credential-relay run — automatic recovery checks that stop permanently when a marker generation advances while a direct check is in flight — and re-attest the delivery gates inside a fresh run so the branch can complete and integrate.

**Architecture:** The prior run left one open final-review finding (`FINAL-1`). Its wave-3 correction is committed at `57a6249fb77697218fe364bc4f71d56eac82bfa7` on the lane branch, but the blocked run had already pinned its single final re-review verdict, so no independent review of that correction exists inside a completing run. This plan re-establishes the correction as a first-class, TDD-gated task with its own reviewer, then re-attests every delivery gate the final review named so the new run's final review has the evidence in its own range.

**Tech Stack:** Python 3 with asyncio, frozen dataclasses, PySide6 projections, pytest with pytest-asyncio, the existing atomic JSON persistence, daemon registry, recovery coordinator, and per-receiver actor queue seams.

## Global Constraints

- Treat `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md` at design commit `addfdc541ad29c6111810871f3306b901ae911ae` and SHA-256 `ed15f8c24296a1bc9269f8dc22b3b9f7116edad5bfac5bde100a24df34cedf94` as the design authority. Treat `docs/superpowers/specs/2026-11-15-gui-device-pairing-spec.md` at spec commit `206ae11d602aa2324eda7dffccd436c2dd6a5bb5` and SHA-256 `40a7be2273f69cb3a0bf1b4baabeed3dbcab9e27ed91d451c1e6ec3cd614bbe2` as the mechanism authority. Do not modify either authority in this plan.
- This is a remediation run for one residual. Do not redesign Route B, do not add features, and do not weaken any previously approved contract: secret-free markers, target-bound commands, exact-row observation, fail-closed recovery, one final re-review per run, and the no-overclaim reporting rules all stay exactly as they are.
- Automatic recovery checks must remain marker-owned and bound to the exact canonical target, marker generation, and original daemon. Every read and every disconnect re-resolves that binding. Retirement, dismissal, replacement, and shutdown must stop scheduling; a stale check must never act for a newer generation.
- The residual is precise: scheduling defers when the owned task is present, and completion reschedules only when the current generation equals the generation that scheduled the check. A generation advance performed by `RestartReceiverToRelease` while a check is in flight therefore ends periodic recovery permanently. The correction must reschedule the *current* durable marker after the owned task exits, subject to the existing eligibility checks.
- Keep `SensitiveText` transient exactly as previously approved. Use a unique canary in every new test and assert it never appears in retained state, fixtures, or reports.
- Run Python tests with `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest` and a distinct short `--basetemp=/tmp/ap-relay-rem-<task-slug>` for every task. Do not use pytest's default deep temporary root because AF_UNIX paths can exceed the platform limit.
- Mutation probes run only in disposable copies, each with a passing baseline or control, and each must name the assertion it detects.
- Do not use a live receiver, real KDE, a production daemon, or a host `go build`. The vendored receiver requires Go 1.22 or newer; record that prerequisite rather than scheduling a host build.
- Do not modify `2026-11-15-gui-device-pairing.execution-graph.yaml` or its DOT artifact. Keep checkboxes unticked because the controller's `state.json` is canonical.
- Do not edit controller state, progress ledgers, dispatch artifacts, or any prior run's reports.

## Task 1: Prove automatic recovery continuation across marker-generation advancement

**Implementer tier:** Capable

**Category:** recovery-continuation

**Files:**

- Modify: `src/airplay_linux/session/recovery.py:1-780`
- Modify: `tests/session/test_final_recovery_regressions.py:1-420`
- Test: `tests/process/test_daemon_registry.py:1-1300`
- Test: `tests/session/test_startup_recovery.py:1-520`
- Test: `tests/session/test_daemon_recovery.py:1-520`
- Test: `tests/storage/test_credential_recovery.py:1-540`

**Interfaces:**

- Consumes: the existing `CredentialRecoveryCoordinator` scheduling and execution paths, `RecoveryMarkerStore` durable transitions, `RestartReceiverToRelease`, `DaemonRegistry` recovery exemptions, and the per-receiver actor queue.
- Produces: a continuation rule in which the owned direct automatic-check task, on normal exit, reschedules the *current* durable marker when that marker is still enabled, still bound to its exact target and daemon, and neither dismissed, retired, replaced, nor shut down.
- Produces: an interleaving regression that holds the first check, advances the generation through a failed `RestartReceiverToRelease`, proves the old fence refuses, and observes three subsequent checks against the new generation, plus a dismissal control proving rescheduling is suppressed.

**Dependencies:** None. This is the only task allowed to change the automatic-check continuation rule.

- [ ] **Step 1: Write the failing generation-advance interleaving regression**

  Add a regression using the real startup recovery, marker store, coordinator, actor command path, and fake clock. Restore a `release_pending` marker with automatic checks enabled, hold the first direct check in flight, dispatch `RestartReceiverToRelease`, persist the advanced generation, and fail the replacement operation before it changes the daemon. Release the held check, advance three scheduler intervals, and assert the observed calls are `[2, 3, 3, 3]` — the old generation once, then the current durable generation each interval. Add a dismissal control that disables checks during the held operation and asserts no further calls. Add a shutdown control that asserts the same.

- [ ] **Step 2: Run the new regression and confirm it fails**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_final_recovery_regressions.py -k generation --basetemp=/tmp/ap-relay-rem-generation`

  Expected: FAIL on the enabled case at `[2]` versus `[2, 3, 3, 3]`, with the dismissal and shutdown controls passing.

- [ ] **Step 3: Correct the continuation rule**

  On normal exit of the owned direct automatic-check task, reschedule the current durable marker when it is still enabled and still bound to the same target and daemon, whether or not its generation equals the generation that scheduled the check. Preserve every existing eligibility check so dismissal, retirement, replacement, and shutdown still suppress scheduling. Do not introduce a second timer, do not act for a superseded generation, and do not weaken the stale-fence refusal inside the check body.

- [ ] **Step 4: Run the focused recovery suites**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_final_recovery_regressions.py tests/process/test_daemon_registry.py tests/session/test_daemon_recovery.py tests/session/test_startup_recovery.py tests/storage/test_credential_recovery.py --basetemp=/tmp/ap-relay-rem-recovery`

  Expected: PASS with the new interleaving regression and every restored-marker, watching-marker, dismissal, retirement, and shutdown control green.

- [ ] **Step 5: Run the mutation probes**

  In disposable copies with imports pinned to the copied source, revert the continuation rule, remove the enabled-marker eligibility check, remove the exact-target binding, and remove the shutdown suppression. Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_final_recovery_regressions.py --basetemp=/tmp/ap-relay-rem-mutation`

  Expected: each mutation fails its specific assertion; the clean baseline and the remaining independent controls pass.

- [ ] **Step 6: Commit the recovery continuation correction**

  ```bash
  git add src/airplay_linux/session/recovery.py tests/session/test_final_recovery_regressions.py
  git commit -m "fix(relay): continue recovery after marker generation advances"
  ```

## Task 2: Re-attest the delivery gates inside this run

**Implementer tier:** Capable

**Category:** delivery-evidence

**Files:**

- Test: `tests/app/test_composition.py:1-300`
- Test: `tests/acceptance/test_no_production_spawn.py:1-400`
- Test: `tests/integration/test_concurrent_sessions.py:1-540`
- Test: `tests/ui_acceptance/test_multireceiver_flow.py:1-660`
- Modify: `docs/operations.md:1-200`

**Interfaces:**

- Consumes: Task 1's corrected continuation rule and every previously approved producer and consumer contract.
- Produces: a recorded, reproducible re-attestation of the assembled selection, the focused selection, the complete repository suite, the retained-report canary scan, and the design/specification digest comparison, each with the exact command and observed counts.
- Produces: `docs/operations.md` prose stating the exact automatic-check continuation guarantee, including what happens when a marker generation advances during an in-flight check.

**Dependencies:** Task 1. Do not change production behaviour in this task; if a gate fails, report the failure rather than repairing it here.

- [ ] **Step 1: State the continuation guarantee in the operations documentation**

  Add a short paragraph to `docs/operations.md` describing what automatic recovery checks do after an application restart, what happens when an endpoint-changing release advances the marker generation while a check is in flight, and that dismissal and shutdown stop scheduling. Keep the prose aligned with observed target-specific status and never claim the credential is stored or remembered.

- [ ] **Step 2: Run the assembled selection**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/app/test_composition.py tests/runtime/test_capability_report.py tests/runtime/test_owned_probe.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py tests/session/test_pairing_integration.py tests/session/test_shutdown.py tests/session/test_shutdown_drain.py --basetemp=/tmp/ap-relay-rem-assembled`

  Expected: PASS with no production process, receiver, KDE session, or host Go invocation.

- [ ] **Step 3: Run the focused selection and the complete repository suite**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/app/test_composition.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py --basetemp=/tmp/ap-relay-rem-focused`, then `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest -p no:cacheprovider --basetemp=/tmp/ap-relay-rem-full`.

  Expected: PASS on both. Record the exact passed, skipped, and warning counts. Any failure must be reported with a base-revision comparison, never reclassified as pre-existing without one.

- [ ] **Step 4: Re-attest the retained-report canary scan and the authority digests**

  Scan every retained fixture and report for the assembled credential canaries and record the file count and match count. Verify the design and specification SHA-256 values against the authorities named in Global Constraints, and verify the execution graph and DOT artifact are unchanged.

- [ ] **Step 5: Commit the documentation and evidence**

  ```bash
  git add docs/operations.md
  git commit -m "docs(relay): state automatic recovery continuation guarantee"
  ```
