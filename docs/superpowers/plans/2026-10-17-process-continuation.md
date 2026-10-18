# AirPlay Linux Mirroring Implementation Plan

## Global Constraints

- Approved authority: `docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md` at `a621d8f3cb77fa2fe1cff51d849aeef743fa4716`; do not edit the design. The initial repository contains this document only; every application path below is new.
- Product requirement: "Support KDE Plasma 6 Wayland as a guaranteed v1 environment." Other environments are capability-gated; missing dependencies fail the attempted capability and keep unrelated functionality usable. Do not install system packages automatically.
- Product requirement: "Support multiple receiver sessions at the same time." "Running Mirror and Virtual display modes concurrently for the same receiver" is a non-goal. Confirm Stop before switching, and start the queued mode only after confirmed cleanup.
- Product requirement: "Mirror mode accepts a physical monitor source only. It creates no virtual output." Never capture an application window, virtual output, or silently substituted physical monitor for Mirror.
- Product requirement: "Guessing a TV resolution when the receiver does not provide reliable information" is a non-goal. Resolve a typed decision before reserving a virtual output; remember it only for the same stable, compatible identity after successful setup.
- Keep `TransportState`, `DisplayState`, `AudioState`, and `TrustState` independently authoritative. Derive `SessionPhase` and UI actions from them; an unknown or unconfirmed resource is never reported released.
- Preserve a valid Virtual output across transport-only failure, receiver absence, local transport retry exhaustion, pairing, and daemon restart. Helper failure invalidates the source and triggers ordered cleanup.
- Each command gets a new operation UUID. Session generation advances only for a new superseding start, abandoned start, Stop/confirmed mode switch, resolution reconfiguration, or shutdown; same-intent retries and active challenge submission keep it. Transport leases, audio operation IDs, trust generations/challenges, and display owner identities fence their own resources.
- Transport state events require current receiver, endpoint fingerprint/generation, session generation, lease, stream, and daemon instance as applicable. Exact-handle cleanup acknowledgements can finish teardown after generation changes. Display events use owner UUID, connector/output, and helper PID/start time, independently of network generations. Stale successful acquisitions must still be released or durably recorded as cleanup uncertain.
- Teardown order: durably publish `desired_state=removing`/stage `removing` for any reserved/owned display, stop transport and confirm its release, stop audio control, stop/remove the exact owned helper/output and confirm disappearance, then conditionally restore eligible layout properties. A failure to publish removal intent blocks destructive teardown and exposes cleanup retry.
- Display reservation stages are exactly `reserved`, `helper_started`, `output_observed`, `published`, `removing`; desired state is exactly `active` or `removing`. Use temporary write, file fsync, atomic rename, and parent-directory fsync. The provider-wide creation lock spans baseline through verified publication. Never remove an unowned output automatically.
- Own one private foreground doubletake daemon through a Linux owner-death supervisor; never use `-daemonize`, the default socket, or an unrelated daemon. Runtime path is `$XDG_RUNTIME_DIR/airplay-linux/instances/<app_instance_uuid>/`; single-instance lock is `$XDG_RUNTIME_DIR/airplay-linux/airplay-linux.lock`. Authenticate owner token, UUID, socket, and PID/start-time pairs before use or termination.
- The transport coordinator is the only pairing authority. Commit trust only while challenge, receiver, endpoint, session generation, and trust generation still match. Cancel/expiry/Forget/replacement invalidate trust; compensate late success with revocation. Unconfirmed cancellation/revocation blocks new pairing and start as `TrustState.CleanupRequired`.
- Use backend keyring mode exclusively. PINs, passwords, opaque correlation tokens, provisional credentials, and backend secrets never enter metadata, exception text, process argv, activity logs, diagnostic exports, or test snapshots. Sensitive input is transient and has a redacted representation. Application code never implements AirPlay authentication or stores secret bytes in its own records.
- Audio defaults enabled. Before video readiness, failed/denied audio requires explicit Retry audio, Start video only, or Cancel. After video readiness, audio failure preserves video and disables mute; audio retry does not replace transport/display leases. Mute is receiver-local and session-only; failed mute preserves the last confirmed state.
- Network backoff is `min(1 * 2**attempt, 30)` seconds, with no offline retry limit and no jitter in v1. Local daemon restart budget is 3 attempts in 60 monotonic seconds. Pairing expires after 120 monotonic seconds and allows 3 invalid submissions. Control calls time out after 5 seconds, stream stop after 5 seconds, helper appearance/removal after 5 seconds, supervisor TERM escalation after 3 seconds, and whole-app shutdown after 15 seconds. These are implementation constants, not new receiver capability claims.
- Planning evidence: installed `doubletake-bin 0.5.0-1` help lists `-socket`, `-cred-backend`, `-daemonize`, `-target`, and `-no-audio`; ctl lists status/discover/devices/connect/pin/disconnect/mute/unmute, but does not establish foreground multi-target control, source verification, transactional trust/revocation, audio restart, or owner handshake. Treat each unverified operation as unsupported. No invented backend flags, socket messages, or simulated implementation may be presented as live capability.
- All paths are relative to the integration worktree. Task 1 freezes shared DTOs/protocols. Later tasks may create only their declared files; consume earlier interfaces without editing their owners. An interface conflict is a concrete integration finding, not permission to change an undeclared path. No task modifies the approved design or this plan during execution.
- Run commands from the worktree root with `PYTHONPATH=src`, `QT_QPA_PLATFORM=offscreen`, `PYTHONHASHSEED=0`, and `LC_ALL=C.UTF-8`. Normal tests use no LAN, real keyring, TV, desktop mutation, or privileged operation. Live tests are separately marked and require explicit test-environment selection.
- File map: `domain/` owns immutable DTOs and protocols; `testing/` owns deterministic adapters; `diagnostics/` owns safe messages; `storage/` owns atomic metadata; `discovery/`, `process/`, `backend/`, `trust/`, `transport/`, `capture/`, `display/`, and `policy/` own their adapters; `session/` owns actor internals; `ui/` owns projection/widgets/presenter; `app.py` is the composition root. Tests mirror these directories.
- PM lane map: `foundation` Task 1 then Task 2; `diagnostics` Task 3; `storage` Task 4; `discovery` Tasks 5–6; `process` Tasks 7–9 with Task 8 also producing the backend wire contract; `trust` Task 10; `transport` Task 11; `capture` Task 12; `display` Tasks 13–15; `policy` Tasks 16–17; `session` Tasks 18–23; `ui` Tasks 24–26; `assembly` Task 27; `adapter-acceptance` Task 28; `lifecycle-acceptance` Task 29; `ui-acceptance` Task 30. File ownership is disjoint between lanes. Explicit per-task dependencies, rather than numeric adjacency, define scheduling.

### Projection provenance
- Lane: `process`
- Segment task IDs + source tasks: `T-007` (source Task 7), `T-008` (source Task 8), `T-009` (source Task 9)
- Plan sequence range: `7-9`
- Projection task mapping: Task 1 -> T-007 (source Task 7), Task 2 -> T-008 (source Task 8), Task 3 -> T-009 (source Task 9)
- Epoch: `1`; attempt: `1`
- Source plan SHA-256: `4d1d90be2502063827e8287e312a97f51b4baf236c4275442857a78c1522b22d`
- Manifest SHA-256: `44998017c1b0308e83fb5372999040dcaaa467d0f8a4f9348cf6154b35f66fc4`
- Generated by: `derive-pm-lane-projections`

### Consumed interfaces from integrated tasks
- `TypedDomainContracts` (produced by T-001, source Task 1, lane `foundation`; digest `ffb1bc5c0aaca7b08a60a4c2cff77965953fce1a5f042d53d66cce7d2e15b6b5`): Typed domain contracts expose immutable receiver, resource, operation, record, message, and adapter-port DTOs for the AirPlay session.
- `DeterministicTestAdapters` (produced by T-002, source Task 2, lane `foundation`; digest `b62b3bdb441e34b04f65c81207b46526b39a3994c2adcd1eb06d319143dc1a12`): Deterministic test adapters expose fake clocks, schedulers, IDs, ports, stores, event sources, and journaled secret-free effects.
- `SafeDiagnostics` (produced by T-003, source Task 3, lane `diagnostics`; digest `6f191807bb897dcb561b1963d918514a4a1de82db2ec2949a279abb5ff922cd7`): Safe diagnostics expose allowlisted catalog messages, bounded activity records, and secret-rejecting exports.
- `AtomicMetadataStorage` (produced by T-004, source Task 4, lane `storage`; digest `74f1558127f4a47dcf9ccc0cd8fb9f3205e441fb89736bf42ddec75e5d1c0a86`): Atomic metadata storage exposes versioned non-secret JSON codecs, repositories, and continuity-filtered preferences.

## Task 1: Re-verify the committed backend contract against the pinned source

**Implementer tier:** Advanced
**Lane/category:** process / backend-contract.
**Depends on:** Tasks 1–3, 7.

**Files:**
- Create: `src/airplay_linux/backend/__init__.py`
- Create: `src/airplay_linux/backend/contract.py`
- Create: `src/airplay_linux/backend/control.py`
- Create: `tests/fixtures/backend/doubletake-0.5-help.txt`
- Create: `tests/fixtures/backend/doubletake-ctl-0.5-help.txt`
- Create: `tests/fixtures/backend/status-0.5.json`
- Create: `tests/fixtures/backend/contract-v1.json`
- Create: `docs/backend-compatibility.md`
- Test: `tests/backend/test_contract.py`
- Test: `tests/backend/test_control.py`

**Interfaces:**
- Consumes the committed backend contract at `92c1da4` and its remediation at `31fcd46`, plus the eight findings raised against it and the pinned `omarroth/doubletake` source at peeled commit `e18f85d07f74a3b9d49f448762d28f3f7feb3938`.
- Produces the same public backend surface as before. This task changes no interface; it verifies and, where the findings prove a defect, repairs the implementation and its fixtures.

- [ ] **Step 1: Reproduce every finding.** For each of the eight findings raised against `92c1da4` — capability proof inferred from help text, mutation before ownership validation, `devices,omitempty`, per-target mute confirmation, exit-1 rejection envelopes, the missing application-contract profile, the impossible status fixture, and the unresolvable source citations — write a probe that fails on `92c1da4`. Confirm the remediation at `31fcd46` actually closes it, in code, not in prose.

- [ ] **Step 2: Run red where anything is still open.** `PYTHONPATH=src python -m pytest -q tests/backend`; any still-open finding must fail for its own mechanism.

- [ ] **Step 3: Repair only what the probes prove.** Keep the fail-closed posture: unverified profiles and obligations stay disabled, mutations require an authenticated handshake bound to the selected socket and exact daemon instance, and no backend text reaches diagnostics. Do not add another heuristic where positive proof is required.

- [ ] **Step 4: Verify.** `PYTHONPATH=src python -m pytest -q tests/backend` then `PYTHONPATH=src python -m pytest -q`; expect no failures and a clean worktree.

- [ ] **Step 5: Commit.** `git add src/airplay_linux/backend tests/backend tests/fixtures/backend docs/backend-compatibility.md` then `git commit -m "fix(backend): verify the pinned contract against published source"`.

## Task 2: Own a foreground daemon through supervisor and runtime reconciliation

**Implementer tier:** Capable
**Lane/category:** process / daemon-ownership.
**Depends on:** Tasks 1–4, 7–8.

**Files:**
- Create: `src/airplay_linux/process/supervisor.py`
- Create: `src/airplay_linux/process/daemon_registry.py`
- Test: `tests/process/test_supervisor.py`
- Test: `tests/process/test_daemon_registry.py`

**Interfaces:**
- Consumes Task 1 DaemonRecord and DaemonStore (`write/all/delete`), ProcessPort, ProcessIdentity, Clock/Scheduler/IdFactory; Task 4 AtomicFiles and JsonDaemonStore; Task 7 exact owned-process checks; Task 8 BackendFeatures and `BackendControl.handshake(owner_token, instance_uuid) -> Awaitable[Result[Handshake]]`.
- Produces `DaemonSupervisor` executable via `python -m airplay_linux.process.supervisor`, accepting app PID/start, instance UUID, runtime directory, and a private inherited config FD; `.run() -> int` owns one foreground child. `DaemonRegistry.ensure() -> Awaitable[Result[DaemonRecord]]`, `.reconcile_previous() -> Awaitable[Result[None]]`, `.restart() -> Awaitable[Result[DaemonRecord]]`, `.shutdown() -> Awaitable[Result[None]]`, `.subscribe(callback: Callable[[DaemonNotice],None]) -> Subscription`; `DaemonNotice(kind: Literal['ready','exit','cleanup_required'], instance_uuid, diagnostic)`.

- [ ] **Step 1: Write failing tests.** Pin durable starting-before-spawn, running-only-after-handshake, stopping-before-TERM, PID reuse, mismatched owner/socket, stale socket with live process, starting/stopping crash windows, parent death, direct-child death, surviving previous streams, and restart budget accounting. Use a fake daemon executable in tmp_path and real private Unix socket only in a disposable temporary runtime directory.

```python
import pytest
from airplay_linux.process.supervisor import foreground_argv

def test_foreground_command_never_daemonizes():
    argv = foreground_argv('/usr/bin/doubletake', '/run/test/private.sock')
    assert '-daemonize' not in argv
    assert '-socket' in argv
    assert argv[argv.index('-socket') + 1] == '/run/test/private.sock'
    assert argv[argv.index('-cred-backend') + 1] == 'keyring'
```

- [ ] **Step 2: Run red.** `PYTHONPATH=src python -m pytest -q tests/process/test_supervisor.py tests/process/test_daemon_registry.py`; expect missing supervisor/registry.
- [ ] **Step 3: Implement supervision.** Supervisor sets `prctl(PR_SET_PDEATHSIG, SIGTERM)` before spawning, then rechecks recorded app PID/start to close the parent-death race. Child installs its own parent-death signal in a minimal launcher before exec so supervisor death cannot leave a free daemon. Avoid threaded preexec_fn; use a dedicated launcher process. Forward termination, request target release, wait up to 3 seconds then KILL exact child and reap. Bind the config pipe using CommandSpec.pass_fds, close the GUI's copies after spawn, and pass authentication config through that protected FD/runtime record, never a printable token argument. Runtime dir 0700/socket path owned by current UID, no symlink traversal. Write starting before launch, record supervisor/daemon PID-start pairs as known, accept socket only after path/record/process/owner-token/protocol handshake, then publish running. Never launch an incompatible foreground backend merely to see whether it works.
- [ ] **Step 4: Implement prior-instance reconciliation and verify.** Authenticate each old record by UUID, owner token and exact process pairs; request shutdown and confirm both supervisor/daemon and streams gone before replacement. If app is dead and matching owned children survive, terminate only those exact children; if evidence is live-mismatched or inaccessible return cleanup_required and prohibit replacement streams. A stale socket is unlinked only after recorded processes are proven dead. Apply identical rules to starting/stopping records and failed atomic updates. Run `PYTHONPATH=src python -m pytest -q tests/process`; expect no failures, including real parent-death probe and bounded escalation.
- [ ] **Step 5: Commit.** `git add src/airplay_linux/process/supervisor.py src/airplay_linux/process/daemon_registry.py tests/process/test_supervisor.py tests/process/test_daemon_registry.py` then `git commit -m "feat(process): supervise and reconcile private foreground daemon ownership"`.

