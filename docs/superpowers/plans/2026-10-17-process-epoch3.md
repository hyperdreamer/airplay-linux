# AirPlay Linux Process Lane Remediation Plan (epoch 3)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `process` lane. Prior run roots are terminal and preserved byte-for-byte and must never be resumed, edited, or cited as review evidence: `.superpowers/sdd/lane-process-epoch-1-attempt-1` (`DISPATCH_MISMATCH_BLOCKED`), `.superpowers/sdd/lane-process-epoch-2-attempt-1` (`FINAL_BLOCKED`).
- Source authority is Git. The lane's committed state at this plan's base is `47289d3..5cda8f3382d8195f9186002b350e3981682f140e`.
- Secret handling is unconditional. Credentials, opaque tokens, backend text and exception text never reach diagnostics messages, activity entries, exports, retained records, `repr`, or any field of a raised exception.
- **A process the runner started is never abandoned.** Possession of a launch must always end in one of three states: the exact child is confirmed gone, a strong cleanup obligation remains retained and observable, or cleanup uncertainty is explicitly recorded. Returning silently while a child may still be alive is forbidden.
- Never signal a bare PID without immediately revalidated exact evidence. A pidfd, or a freshly re-read identity that matches the launched child, is required before any signal; absence of both means retain uncertainty rather than guess.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/diagnostics/**`, `src/airplay_linux/storage/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document and this plan are read-only.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Retain cleanup ownership when pidfd evidence is unavailable

**Implementer tier:** Advanced
**Lane/category:** process / cancellation-safety.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/process/linux.py`
- Test: `tests/process/test_linux_runner.py`

**Interfaces:**
- Consumes the existing `LinuxProcessRunner`, `_kill_direct_child`, `_cleanup_untracked_child`, `_retain_reaper` and `_reapers` machinery, unchanged in signature.
- Produces the same public surface. Only the ownership bookkeeping on the pidfd-unavailable cancellation path changes.

- [ ] **Step 1: Write the failing test.** Force `_open_pidfd` to return `None` and make launch identity unavailable, then cancel `spawn` while it is acquiring identity. Assert that after cancellation the runner either has a retained reaper for that child or has recorded explicit cleanup uncertainty — and that in no case does it report nothing. A control run with pidfd available must still reap exactly the child. Never assert success by leaving a stray process behind: the test must clean up whatever it started.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/process/test_linux_runner.py -k pidfd`; expect the new case to fail because cleanup returns without retaining anything.

- [ ] **Step 3: Implement.** On the no-pidfd path, do not let cleanup complete silently while the child may be alive. Reacquire and validate launch identity before any fallback signal; if no exact evidence can be obtained, keep a strong cleanup obligation (retained reaper plus recorded uncertainty) instead of finishing. Preserve the refusal to signal a bare PID, and keep the pidfd path exactly as it is.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/process` then `PYTHONPATH=src python -m pytest -q`; expect no failures, no surviving owned test children, and a clean worktree.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/process/linux.py tests/process/test_linux_runner.py` then `git commit -m "fix(process): retain cleanup ownership without pidfd evidence"`.
