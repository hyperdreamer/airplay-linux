# AirPlay Linux Diagnostics Lane Continuation Plan

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This is a continuation of the `diagnostics` lane. The prior run root `.superpowers/sdd/lane-diagnostics-epoch-1-attempt-1` is terminal in `DISPATCH_MISMATCH_BLOCKED` and is preserved byte-for-byte; it must never be resumed, edited, or used as review evidence.
- Source authority is Git. The lane implementation already committed is `514087f76cecefba96d90e9c0a2571052b33170d..696a583991d1042f9daa30dd5ff9bc1c13fd56f3`. Reviews in the blocked run are inadmissible as evidence; the code, not the reports, is the subject.
- Prior findings `F-1`, `F-2`, `F-3` were raised, fixed and re-reviewed. The continuation re-verifies their mechanisms directly in the code rather than trusting those reports.
- Secret handling is unconditional: PINs, passwords, credential material, opaque correlation tokens, owner tokens, restore tokens, raw argv/stdout/stderr and exception text never reach activity entries, exports, logs, or diagnostic `repr`.
- An allowlist of field *names* is not a sanitization boundary. Field *values* are untrusted input and must be constrained to shapes that cannot carry backend text or secret bytes.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document, and the plan are read-only in this lane.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, and `LC_ALL=C.UTF-8`. Normal tests use no LAN, real keyring, TV, or privileged operation.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Constrain activity field values to sanitized shapes

**Implementer tier:** Advanced
**Lane/category:** diagnostics / secret-boundary.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/diagnostics/activity.py`
- Modify: `src/airplay_linux/diagnostics/__init__.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes `ActivityEvent(utc, code, receiver_id, message, fields)`, `ActivitySink.emit(event) -> None`, and `ErrorCode` from the frozen domain package, unchanged.
- Produces the same public surface: `safe_activity(code, receiver_id=None, fields=None, utc=...) -> ActivityEvent`, `ActivityLog(max_entries=1000).emit/.entries/.subscribe/.export`. No signature changes are permitted; only the value policy tightens.

- [ ] **Step 1: Write failing tests.** Prove that a canary secret cannot survive `safe_activity` in any position. Cover at minimum: `phase`, `transport`, `display`, `audio`, `trust`, `command_name`, `connector`, `output_id`, `resolution`, `receiver_name`, and an unregistered `code`. Each must either be rejected or normalized to a value that cannot contain the canary. Assert on the `ActivityEvent`, on `ActivityLog.entries()`, on `repr()` of both, and on `export()`. Include a positive test that legitimate enumerated values for those fields are still accepted unchanged.

```python
import pytest
from airplay_linux.diagnostics.activity import safe_activity

def test_allowlisted_field_value_cannot_carry_backend_text():
    for field in ('phase', 'transport', 'command_name', 'connector'):
        with pytest.raises(ValueError):
            safe_activity('session', None, {field: 'raw-backend-canary-8496'}, '2026-10-17T00:00:00Z')
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the new value-policy tests to fail because the current implementation accepts arbitrary strings.

- [ ] **Step 3: Implement.** Decide a per-field value policy and enforce it in one place: enumerated/identifier-shaped fields accept only a strict token charset and bounded length, numeric fields accept only bounded integers, and anything unrecognized is rejected rather than escaped. Rejection is the default; do not fall back to echoing input. Keep the existing escaping and bounding for values that are still accepted. Update `_FALLBACK_MESSAGE` handling so an unknown `code` is rejected or mapped to a fixed literal, never echoed. Do not weaken any existing F-1/F-2/F-3 behavior: the design action matrix stays exact, escape boundaries stay intact, and `receiver_id` stays escaped and bounded.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures and no canary in any emitted entry, export, or `repr`.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): constrain activity values to sanitized shapes"`.

## Task 2: Re-audit the diagnostics lane and verify the design matrix

**Implementer tier:** Advanced
**Lane/category:** diagnostics / audit.
**Depends on:** Task 1.

**Files:**
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the public diagnostics surface produced by Task 1 and the frozen domain `ErrorCode`/`RecoveryAction` enums.
- Produces no new production symbol. This task adds verification only, plus any test repair needed to pin behavior that Task 1 changed.

- [ ] **Step 1: Write the audit tests.** Add a table-driven test that asserts the exact `recovery` action set for every `ErrorCode` row that the approved design enumerates in its "Allowed primary actions" table, so a future tuple edit cannot drift from the design. Include the rows for resolution mismatch, dependency/capability failure, cleanup required, trust cleanup required, orphan candidate, audio-unavailable, recovering/reconnecting, and failed. Add a test asserting every `ErrorCode` member has a distinct, non-empty fixed message and that no message contains a placeholder or an interpolated input value.

- [ ] **Step 2: Run red where applicable.** `PYTHONPATH=src python -m pytest -q tests/diagnostics`; a table row that does not yet match the design must fail for that row, not for a collection error.

- [ ] **Step 3: Repair only what the audit proves wrong.** If a catalog row disagrees with the design action column, correct the catalog to match the design. Do not edit the design, do not add enum members, and do not change the recovery matrix for any row the design does not enumerate.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures. Confirm the whole-repository suite still passes so the diagnostics lane has not perturbed `domain` or `testing`.

- [ ] **Step 5: Commit.** `git add tests/diagnostics/test_safety.py src/airplay_linux/diagnostics` then `git commit -m "test(diagnostics): pin the design recovery matrix"`.
