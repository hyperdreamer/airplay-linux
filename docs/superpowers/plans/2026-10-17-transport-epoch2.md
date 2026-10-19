# AirPlay Linux Transport Lane — Remediation Plan (epoch 2)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `transport` lane. The prior run root is terminal and preserved byte-for-byte and must never be resumed, edited, or cited as review evidence: `.superpowers/sdd/lane-transport-epoch-1-attempt-1` (`FINAL_BLOCKED`).
- Source authority is Git. The lane's committed state at this plan's base is `3938e9a38f7594a339130946e5d25d3fa332fe08`.
- **Exact-target-before-I/O must hold at the actual mutating-command dispatch boundary.** A check that runs before entering an adapter method, or after its reply returns, is not sufficient when the adapter awaits in between: the awaited work must not be able to invalidate the target and still let the command dispatch to a replacement.
- The scope of this epoch is deliberately widened to include `src/airplay_linux/backend/control.py`. That widening is authorized **only** for the dispatch-boundary fix and its tests. Do not change the backend's wire contract, published profile mappings, capability probe, error mapping, ownership handshake semantics, argv construction rules, or any other integrated behaviour.
- Do not weaken an existing guard to make a new check fit. The backend's own daemon-ownership revalidation before a mutation stays exactly as it is.
- `domain/**`, `diagnostics/**`, `storage/**`, `process/**`, `trust/**`, `discovery/**`, `testing/**`, the design and the pins in the plan header are read-only.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, and a private pytest `--basetemp` directory.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Fence mutating backend commands against target replacement at dispatch

**Implementer tier:** Advanced
**Lane/category:** transport / dispatch-boundary-fence.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/backend/control.py`
- Modify: `src/airplay_linux/transport/doubletake.py`
- Test: `tests/backend/test_control.py`
- Test: `tests/transport/test_doubletake.py`

**Interfaces:**
- Consumes the existing `BackendControl.request(operation, target, payload, context)` signature and the existing `_revalidate_ownership` behaviour, unchanged in their public shape.
- Produces: a validated-dispatch seam on the control adapter such that a caller can require a freshness predicate to be re-evaluated immediately before argv execution, with the mutating command skipped (typed failure, no process spawn) when it fails.

- [ ] **Step 1: Write the failing replacement-during-revalidation regressions.** Compose the real `PublishedDoubletakeControl` with the application contract profile and a gated `ProcessPort`, exactly as the final review did. For connect, disconnect, and mute: suspend the per-command ownership status, admit a *different* receiver at the same address through `TargetRegistry.update`, then release status with valid daemon ownership. Assert that no mutating command reaches the process journal, that the operation returns a typed failure, and that the replacement is untouched. Confirm all three fail on the current code.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/backend/test_control.py tests/transport/test_doubletake.py`; expect the new no-command assertions to fail because the command was already dispatched.

- [ ] **Step 3: Implement the dispatch fence.** Add a validated-dispatch seam so the caller's freshness predicate is evaluated at the dispatch boundary — after the adapter's ownership await, immediately before the mutating child is spawned — and a false predicate yields a typed failure with no process spawn. Transport must supply a predicate that re-checks receiver/endpoint identity for the exact target (and cleanup ownership on the teardown path). Keep every existing check; this closes the window rather than replacing it. Do not duplicate argv construction, do not bypass the adapter, and do not abandon a spawned child.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/backend tests/transport tests/process tests/trust` then the full suite; expect no failures. Re-run the three replacement-during-revalidation probes and confirm no mutating command is journalled. Confirm the previously killed mutations (post-await freshness, replacement protection, daemon notice scoping, private coordinator, capture verification, capability gating, audio-disabled, requested-mute-value, early-exit memory, multi-target gate) are still killed.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/backend/control.py src/airplay_linux/transport/doubletake.py tests/backend/test_control.py tests/transport/test_doubletake.py` then `git commit -m "fix(transport): fence mutating commands against target replacement at dispatch"`.
