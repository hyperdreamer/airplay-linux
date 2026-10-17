# AirPlay Linux Diagnostics Lane Remediation Plan (epoch 6)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `diagnostics` lane. Prior run roots are terminal and preserved byte-for-byte and must never be resumed, edited, or cited as review evidence: `.superpowers/sdd/lane-diagnostics-epoch-1-attempt-1` (`DISPATCH_MISMATCH_BLOCKED`), `-epoch-2-attempt-1`, `-epoch-3-attempt-1`, `-epoch-4-attempt-1`, `-epoch-5-attempt-1` (all `FINAL_BLOCKED`).
- Source authority is Git. The lane's committed state at this plan's base is `514087f76cecefba96d90e9c0a2571052b33170d..38c35243a9955d1193e958c4bb3407f302187e84`.
- Secret handling is unconditional. No caller or backend text may reach diagnostics messages, entries, exports, logs, retained records, `repr`, or any field of any raised exception — including `args`, `__cause__`, `__context__`, and `__notes__`.
- The shared boundary in `src/airplay_linux/diagnostics/boundary.py` remains the single normalization authority. Every fix in this run extends that boundary; no new per-site type inspection may be introduced.
- Boundary invariants that must not regress: exact built-in types only; no method, descriptor, or iteration protocol of caller data is invoked; enum membership by identity against captured genuine members; zero caller-hook invocations; fixed rejection text.
- Sanitizing a value is only half of ownership. A value the module validates must also not remain reachable in a form that lets the caller change what the module will later read. Retained state is either a private snapshot or revalidated through the boundary immediately before use.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document, and this plan are read-only in this run.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Scrub rejection context under an active caller exception

**Implementer tier:** Advanced
**Lane/category:** diagnostics / boundary-core.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/diagnostics/boundary.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the existing `rejecting` wrapper and `rejection` factory from `boundary.py`, unchanged in signature.
- Produces the same public boundary surface. Only the raised replacement's exception fields are guaranteed clean.

- [ ] **Step 1: Write failing tests.** For each public entry point (`safe_activity`, `diagnostic`, `ActivityLog.emit`, `ActivityLog.export`, `Subscription.close`), call it from inside an active `except Exception` handler and separately from inside an active `except BaseException` handler, where the handled exception's message is a canary. Assert the resulting rejection has no canary in `str`, `repr`, `args`, `__cause__`, `__context__`, or `__notes__`. Also assert `__context__ is None` for the replacement. Include one case where the caller's handler is already handling a second exception, so the chain is two deep.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the new context assertions to fail because the replacement inherits the active caller exception as `__context__`.

- [ ] **Step 3: Implement.** Clear the replacement's implicit context *after* Python attaches it, which means raising inside a `try/finally` and scrubbing in the `finally`, rather than relying on `from None` alone. Keep the replacement a plain built-in `ValueError` (or `TypeError` where already used), keep the fixed text, and do not add any new inspection of the original failure. Verify that ordinary raising paths still produce exactly one clean exception with a useful fixed message.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures and a clean `git status --porcelain`.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics/boundary.py tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): scrub rejection context under active handlers"`.

## Task 2: Take a private snapshot of accepted capability evidence

**Implementer tier:** Advanced
**Lane/category:** diagnostics / retained-state.
**Depends on:** Task 1.

**Files:**
- Modify: `src/airplay_linux/diagnostics/activity.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the frozen `CapabilityReport` and `Diagnostic` types and the Task 1 boundary, unchanged.
- Produces the same `ActivityLog(capabilities=...)` signature and the same export document shape and schema identifier.

- [ ] **Step 1: Write failing tests.** Construct an accepted exact `CapabilityReport`, pass it to `ActivityLog(capabilities=report)`, then mutate the caller's retained alias after construction by replacing its `__class__` with a subclass that instruments `__getattribute__`. Exercise both a benign-returning hook and one that raises a canary. Assert that `export()` invokes the hook zero times and either rejects with fixed text or exports from a private snapshot, with no canary anywhere. Add the same retained-alias test for a diagnostics item inside the report's diagnostics collection.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the hook counter to record at least one invocation during `export()`.

- [ ] **Step 3: Implement.** At acceptance time, normalize the capability report into a private immutable snapshot of exact built-in leaves (the environment label, the six exact booleans, and the validated diagnostic codes), and read only that snapshot at export. Do not re-read the caller's object at export time, and do not hold a reference that the caller can retype. Route any normalization through `boundary.py`. Keep the export schema and every existing message unchanged.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect zero hook calls in the retained-alias tests and no regressions elsewhere.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics/activity.py tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): snapshot accepted capability evidence"`.
