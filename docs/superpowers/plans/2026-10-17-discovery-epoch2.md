# AirPlay Linux Discovery Lane Remediation Plan (epoch 2)

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design.
- This run continues the `discovery` lane. The prior run root `.superpowers/sdd/lane-discovery-epoch-1-attempt-1` is terminal in `FINAL_BLOCKED` and is preserved byte-for-byte; it must never be resumed, edited, or cited as review evidence.
- Source authority is Git. The lane's committed state at this plan's base is `ad46f07c14c9ffdcd69f9ec6a34c9d42c67826..0dbdada6ba838223d6b13e23f66b3eff10249305`.
- **No invented dependency APIs.** Every call into `zeroconf` must bind to a signature that the installed dependency actually has. A test double must reproduce the dependency's real method shape, so a wrong arity fails in the test rather than only in production.
- A process-wide rule from this lane: an unreadable or unavailable observation is never evidence of absence, and a reissued endpoint must never carry a generation that a stale event could still accept.
- Only the paths declared by a task may be created or modified. `src/airplay_linux/domain/**`, `diagnostics/**`, `storage/**`, `process/**`, `backend/**`, `testing/**`, `pyproject.toml`, the design and this plan are read-only.
- `pytest-asyncio` availability varies by interpreter; verify async behaviour by driving coroutines with `asyncio.run` when the plugin is missing, and never weaken an async test to sync.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, `LC_ALL=C.UTF-8`.
- Every task is committed on the lane branch before the next task begins. Worktree must be clean at each task boundary.

## Task 1: Verify every zeroconf call against the installed dependency

**Implementer tier:** Advanced
**Lane/category:** discovery / dependency-contract.
**Depends on:** none.

**Files:**
- Modify: `src/airplay_linux/discovery/zeroconf_adapter.py` (only if a defect is proven)
- Test: `tests/discovery/test_zeroconf_adapter.py`

**Interfaces:**
- Consumes the existing `ZeroconfDiscovery` and `AzcMdnsClient` public surface, unchanged.
- Produces no new public symbol. This task proves the adapter's calls match the real dependency and repairs any that do not.

- [ ] **Step 1: Enumerate the real surface.** For each `zeroconf` / `zeroconf.asyncio` attribute the adapter touches, inspect the installed version's actual signature (`inspect.signature`, `dir`) and record it. At minimum: `AsyncZeroconf()`, `AsyncZeroconf.async_get_service_info(...)`, `AsyncServiceBrowser(...)` construction, its `async_cancel()`, `AsyncServiceInfo`/`ServiceInfo` attribute and method shapes used for addresses and TXT, and `async_close()`. Note the unit conventions (for example that the resolve timeout is milliseconds).

- [ ] **Step 2: Write failing tests where a mismatch exists.** For each call, add a test that binds the **genuine** class or function where possible (importorskip, class-level signature assertions) so wrong arity or wrong units fail without a network. Where a fake is unavoidable, shape the fake from the real signature rather than inventing one.

- [ ] **Step 3: Repair only what is proven wrong.** Keep the seconds-based public `resolve_timeout` interface with conversion at the boundary. Do not add a public surface. Do not invent a "scan now" call if the dependency has none: state plainly in a comment what the adapter can and cannot drive.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/discovery` then the full suite; expect no failures and a clean worktree. Also confirm the async tests pass under `asyncio.run` when the plugin is absent.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/discovery tests/discovery` then `git commit -m "test(discovery): bind every zeroconf call to the installed dependency"`.
