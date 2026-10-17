# AirPlay Linux Diagnostics Lane Remediation Plan (epoch 5)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `diagnostics` lane. Prior run roots are terminal and preserved byte-for-byte: `.superpowers/sdd/lane-diagnostics-epoch-1-attempt-1` (`DISPATCH_MISMATCH_BLOCKED`), `.superpowers/sdd/lane-diagnostics-epoch-2-attempt-1` (`FINAL_BLOCKED`), `.superpowers/sdd/lane-diagnostics-epoch-3-attempt-1` (`FINAL_BLOCKED`), `.superpowers/sdd/lane-diagnostics-epoch-4-attempt-1` (`FINAL_BLOCKED`). None may be resumed, edited, or cited as review evidence.
- Source authority is Git. The lane's committed state at this plan's base is `514087f76cecefba96d90e9c0a2571052b33170d..683312e9906545274d0458e692e68709d7ea6234`.
- Secret handling is unconditional. Credentials, opaque tokens, backend text, and exception text never reach diagnostics messages, activity entries, exports, logs, `repr` of any record, or rejection exceptions.
- **Architectural rule for this run.** Four prior rounds each fixed one instance of one class of defect and each was reviewed as incomplete for a fresh instance of the same class. Ad-hoc type inspection is therefore out of bounds. Every value crossing the diagnostics boundary is handled by a single shared normalization boundary with these properties, and no new per-site type logic may be written:
  1. Only exact built-in types are accepted (`type(value) is str`, `type(value) is dict`, and similar). No `isinstance`, no MRO walking, no `__class__`, no metaclass attribute access.
  2. No method, attribute, descriptor, or iteration protocol of a caller-supplied object is invoked. A caller object that is not an exact accepted builtin is rejected without inspection.
  3. Enum membership is `identity`, not equality and not dictionary lookup: captured genuine members are compared with `is`, so a forged exact-type nonmember cannot pass.
  4. Every public entry point is wrapped so that any exception, including `BaseException`, becomes a fixed rejection with `from None`; no `args`, `cause`, or `context` may carry caller data.
- The shared boundary lives in exactly one module and both `catalog.py` and `activity.py` consume it. Existing per-site helpers that duplicate normalization are removed rather than left beside it.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document, and this plan are read-only in this run.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Centralize a hook-free normalization boundary

**Implementer tier:** Advanced
**Lane/category:** diagnostics / boundary-core.
**Depends on:** none.

**Files:**
- Create: `src/airplay_linux/diagnostics/boundary.py`
- Modify: `src/airplay_linux/diagnostics/activity.py`
- Modify: `src/airplay_linux/diagnostics/catalog.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the frozen `ErrorCode` enum, `ReceiverId`, `Diagnostic`, `ActivityEvent` and `CapabilityReport` types, unchanged.
- Produces `boundary.py` with: `exact_text(value: object) -> str` returning the character data of an exact `str` only; `exact_mapping_items(value: object) -> tuple[tuple[str, object], ...]` returning pairs from an exact `dict` only, without invoking any subclass hook; `genuine_error_code(value: object) -> ErrorCode` accepting only an object identical to a captured registered member; and `rejection(message: str) -> ValueError` plus a decorator or wrapper that converts any raised `BaseException` into a fixed `rejection` with `from None`. Every existing public signature stays unchanged.

- [ ] **Step 1: Write failing tests.** Build a hook-instrumented matrix. For each of: an exact-type enum nonmember forged with `str.__new__(ErrorCode, ...)`; a `dict` subclass whose `items` raises `BaseException`; the same dict wrapped in `MappingProxyType`; a `str` subclass whose metaclass defines a raising `__mro__` property; a `str` subclass with self-returning `__str__` plus spoofed `__eq__`/`__hash__`; and a raising `__class__` — assert that a counter on the hook records zero calls and that the rejection is a fixed `ValueError` whose `str`, `repr`, `args`, `cause` and `context` contain no canary. Run each probe through `safe_activity`, `diagnostic`, and `ActivityLog.emit`. Include positive cases proving ordinary exact strings, exact dicts, `MappingProxyType` over an exact dict, and every genuine `ErrorCode` member still work.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the forged-enum, proxy-mapping and metaclass cases to fail because they currently execute caller hooks or are accepted.

- [ ] **Step 3: Implement.** Create `boundary.py` with exact-type accessors that never dispatch into caller code, identity-based genuine-member checking against a module-level captured member set, and a fixed-rejection wrapper catching `BaseException`. Rewire `catalog.py` and `activity.py` to use it and delete their local `_text_data`/type-inspection helpers. Preserve all existing messages, recovery tuples, accepted identity forms, field vocabularies, bounds, escaping, and export shape.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures, zero hook calls in the matrix tests, and a clean `git status --porcelain`.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics tests/diagnostics/test_safety.py` then `git commit -m "refactor(diagnostics): centralize a hook-free boundary"`.

## Task 2: Prove the boundary against an adversarial matrix

**Implementer tier:** Advanced
**Lane/category:** diagnostics / boundary-verification.
**Depends on:** Task 1.

**Files:**
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes only the public diagnostics surface and the `boundary` helpers produced by Task 1.
- Produces no new production symbol. This task adds verification and repairs only behavior the matrix proves wrong within `diagnostics/`.

- [ ] **Step 1: Write the matrix.** Parameterize every public parameter of `safe_activity` (code, receiver_id, field names, field values, utc) and `diagnostic` (code, receiver_id, command), plus every leaf of `CapabilityReport` and each item of its diagnostics collection. For each parameter, drive the full adversarial family: exact-type nonmember enum, mapping subclass, proxy over a hostile mapping, hostile iterator, metaclass descriptor, self-returning `__str__`, spoofed `__eq__`/`__hash__`, raising `__class__`, raising `__iter__`, and a `BaseException` payload. Assert for every cell: zero caller-hook invocations, a fixed rejection or safe normalization, and no canary in the value, any retained record, `entries()`, `export()`, or any exception field.

- [ ] **Step 2: Run red where the matrix finds a real gap.** `PYTHONPATH=src python -m pytest -q tests/diagnostics`; any failing cell must fail for its own mechanism, not for a collection error.

- [ ] **Step 3: Repair only what the matrix proves.** Fix any remaining gap inside `diagnostics/` by routing it through the Task 1 boundary, not by adding a new local type check. Do not change `domain/`, the design, or any message text.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect a fully green matrix and no regressions.

- [ ] **Step 5: Commit.** `git add tests/diagnostics/test_safety.py src/airplay_linux/diagnostics` then `git commit -m "test(diagnostics): prove the boundary against an adversarial matrix"`.
