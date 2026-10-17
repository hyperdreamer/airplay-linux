# AirPlay Linux Diagnostics Lane Remediation Plan (epoch 4)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `diagnostics` lane. Prior run roots are terminal and preserved byte-for-byte: `.superpowers/sdd/lane-diagnostics-epoch-1-attempt-1` (`DISPATCH_MISMATCH_BLOCKED`), `.superpowers/sdd/lane-diagnostics-epoch-2-attempt-1` (`FINAL_BLOCKED`), `.superpowers/sdd/lane-diagnostics-epoch-3-attempt-1` (`FINAL_BLOCKED`). None may be resumed, edited, or cited as review evidence.
- Source authority is Git. The lane's committed state at this plan's base is `514087f76cecefba96d90e9c0a2571052b33170d..97bec2c96f464d4a3a8cfb65e745ddfc56140cd5`.
- Reception identity forms are defined by the approved design: `airplay-device:<normalized-device-id>`, `airplay-fingerprint:<sha256>`, `airplay-endpoint:<sha256>`, and the non-persisted `airplay-ambiguous:<sha256>`. A diagnostics boundary that accepts identity values must accept all four forms and reject anything else.
- Secret handling is unconditional. PINs, passwords, credential material, opaque correlation tokens, owner tokens, restore tokens, raw argv/stdout/stderr, backend text and exception text never reach diagnostics messages, activity entries, exports, logs, `repr` of any record, or rejection exceptions — including when a caller supplies an object whose hooks raise.
- A caller-controlled attribute, property, method, or `__class__` is never consulted during validation. Values are normalized to exact built-in data, or rejected with fixed text that renders nothing from the input, including `args`, `cause`, and `context`.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `src/airplay_linux/testing/**`, `pyproject.toml`, the design document, and this plan are read-only in this run.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider` so verification leaves the worktree clean.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Accept every design identity form in diagnostics

**Implementer tier:** Advanced
**Lane/category:** diagnostics / identity-policy.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/diagnostics/catalog.py`
- Modify: `src/airplay_linux/diagnostics/activity.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the frozen `ReceiverId` and `ErrorCode` types and the existing `diagnostic(...)` and `safe_activity(...)` signatures, all unchanged.
- Produces the same public surface. Only the accepted receiver-identity language widens, to exactly the four design-defined prefixes with their defined payload shapes.

- [ ] **Step 1: Write failing tests.** Add positive tests that both `diagnostic()` and `safe_activity()` accept every design-defined identity form: `airplay-device:` with a normalized device identifier, and `airplay-fingerprint:`, `airplay-endpoint:`, `airplay-ambiguous:` with a lowercase 64-character hexadecimal digest. Assert the accepted value is preserved byte-for-byte as an exact built-in string. Keep and extend the negative tests: wrong prefix, uppercase or truncated or over-long digest, embedded control characters, embedded whitespace, arbitrary backend text, and a secret-bearing suffix after a valid prefix must all be rejected with fixed text that contains no part of the input.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the three fallback-identity positive tests to fail because the current validator accepts only `airplay-device:`.

- [ ] **Step 3: Implement.** Replace the single-prefix rule with one grammar per design-defined prefix, sharing a single validator used by both constructors so the two boundaries cannot drift apart. Accept the device form and a bare lowercase hex digest for the three hash-derived forms; reject everything else with the existing fixed rejection type and text. Do not consult caller-controlled attributes; normalize and validate exact built-in data only.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures and a clean `git status --porcelain`.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics/catalog.py src/airplay_linux/diagnostics/activity.py tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): accept every design identity form"`.

## Task 2: Validate capability diagnostic leaves before export

**Implementer tier:** Advanced
**Lane/category:** diagnostics / export-boundary.
**Depends on:** Task 1.

**Files:**
- Modify: `src/airplay_linux/diagnostics/activity.py`
- Test: `tests/diagnostics/test_safety.py`

**Interfaces:**
- Consumes the frozen `CapabilityReport(environment, discovery, mirror, virtual_display, audio, remembered_pairing, additional_virtual_outputs, diagnostics)` and `Diagnostic` types, unchanged.
- Produces the same `ActivityLog.export()` document shape and schema identifier. Only the acceptance rules for the capability diagnostics collection tighten.

- [ ] **Step 1: Write failing tests.** Construct an exact `CapabilityReport` whose `diagnostics` field carries a caller object whose `code` attribute or property raises an exception containing a canary. Assert `ActivityLog.export()` rejects it with a fixed exception whose text, `repr`, `args`, `cause` and `context` contain no canary. Cover: a hostile item with a raising `code`, a hostile item whose `code` is not an `ErrorCode`, a plain immutable tuple of genuine diagnostics still exporting its fixed codes, and a mutable list rejected or normalized deterministically.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/diagnostics/test_safety.py`; expect the hostile-`code` case to propagate the canary instead of raising a fixed rejection.

- [ ] **Step 3: Implement.** Validate the collection shape and each item before reading any attribute: require `Diagnostic` instances by actual type and require `code` to be a genuine `ErrorCode` member before deriving its fixed string. Derive exported code strings only from the validated enum members, never from caller-provided objects. Fail with fixed rejection text and no chained context.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/diagnostics` then `PYTHONPATH=src python -m pytest -q`; expect no failures, and confirm a normal capability document still exports its seven leaves and diagnostic codes unchanged.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/diagnostics/activity.py tests/diagnostics/test_safety.py` then `git commit -m "fix(diagnostics): validate capability diagnostic leaves"`.
