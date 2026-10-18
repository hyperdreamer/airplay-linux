# AirPlay Linux Discovery Lane Remediation Plan (epoch 3)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `discovery` lane. Prior run roots are terminal and preserved byte-for-byte and must never be resumed, edited, or cited as review evidence: `.superpowers/sdd/lane-discovery-epoch-1-attempt-1` (`FINAL_BLOCKED`), `.superpowers/sdd/lane-discovery-epoch-2-attempt-1` (`FINAL_BLOCKED`).
- Source authority is Git. The lane's committed state at this plan's base is `ad46f07c14c9ffdcd69f9ec6a34c9d42c67826..fa05d72bfee449cc4ffbba40ecc0b4a5763cfd2c`.
- **Continuity evidence must be the strongest compatible evidence ever observed for a stable identity, not the evidence of the first snapshot that happened to be seen.** Missing metadata is compatible; conflicting positive metadata is not. A receiver that once advertised a model and later advertises a different one must not silently inherit the earlier receiver's trust or remembered resolution.
- No invented dependency APIs: every call into an external dependency binds to a signature the installed version actually has, and a test double must reproduce that real shape.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `diagnostics/**`, `storage/**`, `process/**`, `backend/**`, `testing/**`, `pyproject.toml`, the design and this plan are read-only.
- `pytest-asyncio` availability varies by interpreter; verify async behaviour by driving coroutines with `asyncio.run` when the plugin is missing, and never weaken an async test to sync.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Keep the strongest positive model evidence as the continuity anchor

**Implementer tier:** Advanced
**Lane/category:** discovery / identity-continuity.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/discovery/identity.py`
- Test: `tests/discovery/test_identity.py`

**Interfaces:**
- Consumes the existing `IdentityResolver.update(...)`, `IdentityResolver.seed_saved(...)`, `_Evidence`, `_compatible_models` and `ReceiverRecordSet` surface, unchanged in signature.
- Produces the same public surface. Only how the continuity anchor's model evidence is accumulated changes.

- [ ] **Step 1: Write the failing three-snapshot regression.** For one stable device ID, drive the resolver through successive complete snapshots:

  1. RAOP with `model=''`;
  2. that RAOP plus an AirPlay record with `model='AppleTV6,2'`;
  3. that RAOP plus the same AirPlay key, address and port but `model='OtherModel'`.

  Assert that snapshot 2 verifies continuity (missing companion metadata must not invalidate an unchanged identity) and that snapshot 3 does **not**: `continuity_verified=False` with `replacement_of` set appropriately. Also assert `PreferenceAccess.for_record_set(snapshot_3, saved)` does not hand back the saved record, and repeat the sequence through `ZeroconfDiscovery` so the adapter path is covered too. Keep the existing missing-companion and conflicting-model tests passing.

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/discovery/test_identity.py`; expect the new snapshot-3 assertions to fail, because the anchor retains the original model-less evidence and `_compatible_models` therefore always accepts.

- [ ] **Step 3: Implement.** Retain accepted nonempty model evidence as it becomes available, independently of the fallback endpoint anchor, and compare later positive evidence against that strongest evidence. Missing model metadata stays compatible; a later conflicting positive model sets `continuity_verified=False` and the appropriate `replacement_of`. Do not weaken the existing empty-model compatibility behaviour or the whole-graph ambiguity rules.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/discovery` then the full suite; expect no failures, and confirm the earlier identity regressions still pass. Verify async tests through the file's `run_async` helper or `asyncio.run` if the plugin is absent.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/discovery/identity.py tests/discovery/test_identity.py` then `git commit -m "fix(discovery): anchor continuity on the strongest positive model evidence"`.
