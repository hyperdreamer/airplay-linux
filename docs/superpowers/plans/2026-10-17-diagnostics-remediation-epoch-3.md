# AirPlay Linux Diagnostics Lane Remediation Plan (epoch 3)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `diagnostics` lane. Prior run roots are terminal and preserved byte-for-byte: `.superpowers/sdd/lane-diagnostics-epoch-1-attempt-1` ended `DISPATCH_MISMATCH_BLOCKED`; `.superpowers/sdd/lane-diagnostics-epoch-2-attempt-1` ended `FINAL_BLOCKED` with residual `F-4`. Neither may be resumed, edited, or cited as review evidence.
- Source authority is Git, not prior reports. The lane's committed state at this plan's base is `514087f76cecefba96d90e9c0a2571052b33170d..7984d683fefbe012868c13ba4454713aa5764bf9`.
- Prior findings carried into this run's ledger, to be reconciled by the final review: epoch-1 `F-1` (recovery tuples vs design), `F-2` (escape/truncation boundary), `F-3` (activity receiver escaping); epoch-2 `F-1` (activity subclass value/name), `F-2` (activity code subclasses), `F-3` (catalog command subclasses); epoch-2 final `F-3` (catalog receiver identity); epoch-2 final re-review `F-4` (catalog ErrorCode impostor).
- Secret handling is unconditional. PINs, passwords, credential material, opaque correlation tokens, owner tokens, restore tokens, raw argv/stdout/stderr, backend text and exception text never reach diagnostics messages, activity entries, exports, logs, `repr` of any record, or rejection exceptions.
- A type check is not a trust boundary when the value's class can be spoofed or its rendering hooks are caller-controlled. Every value crossing the diagnostics boundary is normalized to exact built-in data, or rejected with fixed text that never renders the argument.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document, and this plan are read-only in this run.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` so the verification itself leaves the worktree clean.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Reject code impostors at the diagnostics boundary

**Implementer tier:** Advanced
**Lane/category:** diagnostics / secret-boundary.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/diagnostics/catalog.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the frozen `ErrorCode` StrEnum from `domain/resources.py` and the existing `diagnostic(code, receiver_id=None, *, command=None) -> Diagnostic` signature, both unchanged.
- Produces the same public surface and the same fixed catalog copy. No signature, message, or recovery-action changes are permitted by this task.

- [ ] **Step 1: Write failing tests.** Reproduce the impostor mechanism directly. Define a `str` subclass exposing `__class__` as `ErrorCode`, with `__str__`/`__repr__` returning a canary. Cover both a genuine enumerated value as its underlying data and an unregistered one. Assert that in both cases the call is rejected with the existing fixed error type, that the canary appears in neither the rejection text nor its `repr`, and that no `Diagnostic` is produced carrying the impostor. Add a positive test iterating every genuine `ErrorCode` member and asserting `diagnostic(member)` succeeds with `type(result.code) is ErrorCode` and unchanged fixed copy.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the impostor tests to fail against the current `isinstance`-only check, and expect the unregistered case to surface the leaking `KeyError` rather than a fixed rejection.

- [ ] **Step 3: Implement.** Require the actual enum type before any catalog lookup, so a spoofed `__class__` cannot satisfy the boundary, and reject impostors with the existing fixed error without rendering the argument. Never index the catalog with an unvalidated object. Keep the receiver and command normalization already committed, and keep every message and recovery tuple byte-identical.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures, no canary in any message, `repr`, or exception, and a clean `git status --porcelain`.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics/catalog.py tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): reject code impostors at the catalog boundary"`.
