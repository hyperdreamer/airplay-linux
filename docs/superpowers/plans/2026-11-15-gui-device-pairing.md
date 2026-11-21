
# GUI Device Pairing (Route B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator answer a PIN or password request for the exact target started by a Mirror operation, report only the target-specific status observation permitted by Route B, and recover every unknown outcome without retaining the credential or weakening daemon ownership.

**Architecture:** Build the feature from the existing actor, transport, owned-daemon, atomic-file, and projection seams. The implementation first establishes secret-free domain records and a durable target-keyed recovery barrier, then adds capability-gated credential dispatch, exact-row transport observation, actor settlement, relay-only UI projection, and endpoint-changing release. The assembled runtime is activated only after each producer and consumer contract has its own tests and mutation controls.

**Tech Stack:** Python 3 with asyncio, frozen dataclasses and typing protocols, PySide6 projections, pytest with pytest-asyncio and pytest-qt, the existing `doubletake-ctl` process/control fakes, and the repository's atomic JSON persistence and daemon ownership adapters.

## Global Constraints

- Treat `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md` at design commit `addfdc541ad29c6111810871f3306b901ae911ae` and SHA-256 `ed15f8c24296a1bc9269f8dc22b3b9f7116edad5bfac5bde100a24df34cedf94` as the design authority. Treat `docs/superpowers/specs/2026-11-15-gui-device-pairing-spec.md` at spec commit `206ae11d602aa2324eda7dffccd436c2dd6a5bb5` and SHA-256 `40a7be2273f69cb3a0bf1b4baabeed3dbcab9e27ed91d451c1e6ec3cd614bbe2` as the mechanism authority.
- Implement Route B only. The flow is operator-started Mirror mode with one exact matching per-stream `pin_required` row. Do not add virtual-display pairing, credential storage or forget actions, retry-in-place behavior, remembered-credential claims, or a second trust/settlement model.
- Preserve the transactional pairing path byte-for-byte in behavior. Keep `PairingFlow`, `PairingPort`, `SubmitPairing`, `RelaySubmitCompletion.result: PairingResult`, `pairing_required`, trust generations, keyring transactions, provisional commit, cancel pairing, revoke pairing, and their existing tests on their current path. The relay branch never calls the transactional coordinator or `PairingPort`.
- Use the exact target-first backend forms. Legacy dispatch is `doubletake-ctl -socket SOCKET connect TARGET CREDENTIAL`. Extended dispatch is `doubletake-ctl -socket SOCKET connect TARGET [--audio on|off] [--volume N] CREDENTIAL`. The credential is always the final argv element and `ProfileCommand.secret_indices` contains exactly its index. Never invoke the targetless historical `pin` command.
- Keep `SensitiveText` transient. Validate the raw value before constructing it in the dialog, validate it again at the backend boundary, unwrap it only for validation and the single process invocation, and exclude it from session snapshots, codecs, stores, journals, diagnostics, crash records, callback representations, fake process records, exports, and assertion output. Use a unique canary in every relevant test and assert that it never appears in retained state or output.
- Use `relay_credential_representable(value)` as the profile-independent helper. Its structural predicate is exactly `return not value.startswith("-")`. Empty, NUL-containing, platform-unencodable, and leading-dash values fail before actor command or marker creation. Do not add a PIN-format restriction.
- A complete command reply is never success by itself. Positive and negative complete replies enter typed `Submitted` status observation. Only a target-specific `streaming` observation reports connected. A dispatched command without a complete decoded reply is `NoReply` with one of `command_timeout`, `service_shutdown`, `child_cleanup_uncertain`, `incomplete_reply`, `malformed_reply`, or `dispatch_cancelled`, and retains the recovery marker.
- A request is created only from exactly one status row whose `target == ReceiverTarget.canonical_host`, whose state is `pin_required`, and whose typed credential kind is supported. Aggregate status, connect acknowledgements, another target, absent rows, duplicate rows, and ambiguous rows never create or retarget a prompt. The request event is the sole new `TransportEventKind` member and carries lifecycle `live | ended` with `ended` reason `row_ended`.
- Apply every freshness and ownership check at the existing boundary. Carry the Start identity, target, endpoint fingerprint and generation, session and trust generations, transport lease, daemon instance, challenge ID, relay generation, marker generation, and optional submission ID through effects and completions. Re-evaluate `DispatchFence` after awaited ownership work and synchronously immediately before child creation. Never infer dispatch certainty from a generic backend error.
- Stop must support both pre-submit and dispatched-submission pending Starts. It captures `PendingStartCleanupOwner` before superseding the Start, signals readiness exit without taking `stream.lock`, cancels only the read-only readiness poll, transfers the exact handle once, and waits for settlement and handoff before target-specific disconnect cleanup. It never cancels a credential command or its owner-correlated status read and never starts a second command timeout.
- Recovery markers are secret-free, canonical-target keyed, generation checked, and retained as tombstones. Store transitions are atomic and durable. A visible next-generation file after an error following rename is evidence for retry, not confirmation. Only a successful repeated `AtomicFiles.write` advances the coordinator cache, changes UI, retires a marker, or releases a fence. Corrupt, duplicate, unreadable, mismatched, or ambiguous evidence fails closed and never becomes an empty store.
- Startup order is fixed. Load and validate every marker, including retired tombstones. Run `confirm_all_durable()` before startup events, retired-record trust, or any Start. Populate target fences only after confirmation, reconcile marker-bound attempts, then pass exact `DaemonRecoveryExemption` values to `DaemonRegistry.reconcile_previous(recovery_exemptions=...)`. Ordinary registry reconciliation must not run first. Any failure keeps the global Start barrier active and exposes only the sanitized recovery-store diagnostic.
- Endpoint-changing release requires exact old daemon identity, exact termination proof, a fresh attempt ID, a never-used unique private runtime directory and socket, matching `recovery_attempt_id` in launch and readiness evidence, durable replacement-ready and retirement transitions, and a distinct endpoint before same-target Start is admitted. Never fall back to ordinary restart or the configured socket. Show the shared-daemon impact before release and write `reboot-required.json` with evidence and provenance if a host transition is required. Do not reboot, shut down, signal the PM process tree, or alter graphical targets.
- Do not use a live receiver, real KDE, a production daemon, or a host `go build` in routine tests. Use fake process, control, runtime, clock, scheduler, ownership, and filesystem seams. The host Go toolchain is 1.14.2 while the vendored receiver requires Go 1.22 or newer. Inspect and use a pinned toolchain mechanism if receiver compilation is independently required, otherwise record Go >=1.22 as an environment prerequisite without scheduling a host build.
- Run Python tests with `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest` and a distinct short `--basetemp=/tmp/ap-relay-<task-slug>` for every task. Do not use pytest's default deep temporary root because AF_UNIX paths can exceed the platform limit. Mutation probes run only in disposable copies and each probe has a passing baseline or control.
- Every task ends with an independently testable deliverable and a fresh reviewer gate. Keep checkboxes unticked because the controller's `state.json` is canonical. Use exact file ranges for modified files, keep implementation and tests in the named files, and do not edit the execution graph or DOT artifact as part of this plan.

## Task 1: Establish secret-free relay domain contracts and observer boundaries

**Implementer tier:** Advanced

**Category:** domain-contracts

**Files:**

- Modify: `src/airplay_linux/domain/relay.py:1-193`
- Modify: `src/airplay_linux/domain/resources.py:1-265`
- Modify: `src/airplay_linux/domain/records.py:1-318`
- Modify: `src/airplay_linux/domain/messages.py:1-329`
- Modify: `src/airplay_linux/domain/ports.py:1-372`
- Modify: `src/airplay_linux/session/persistence.py:1-127`
- Test: `tests/domain/test_credential_relay.py:1-933`
- Test: `tests/domain/test_contracts.py:1-469`
- Test: `tests/storage/test_secret_boundaries.py:1-450`

**Interfaces:**

- Consumes: existing `SensitiveText`, `ReceiverTarget`, `ChallengeView`, `OperationId`, `LeaseId`, `SessionSnapshot`, `TransportPayload`, `Command`, `TransportEventKind`, `TransportPort`, and `ProcessPort` conventions.
- Produces: `NoReplyReason = Literal["command_timeout", "service_shutdown", "child_cleanup_uncertain", "incomplete_reply", "malformed_reply", "dispatch_cancelled"]`; Start-bound `CredentialRequest`; `CredentialRequestPayload` with `lifecycle: Literal["live", "ended"]` and fixed `row_ended` ending reason; `Submitted`; `NoReply`; `CredentialPromptState`; `CredentialSubmissionSettlement`; `PendingStartCleanupOwner`; `SubmitCredential`; `DismissCredentialPrompt`; `StopCredentialRecovery`; `DismissCredentialRecovery`; `RestartReceiverToRelease`; and the exact closed-union additions required by the specification.
- Produces: `relay_credential_representable(value)` with the exact profile-independent AST predicate `return not value.startswith("-")`, with no retained argument, profile parameter, closure, or module state.
- Produces: secret-free `repr`, equality, pickling, codec, callback, journal, and safe-snapshot contracts for every relay record. `CredentialPromptState` has `request`, `challenge`, `state` in `editing | submitting | observing`, `submission_id`, and `report` in `connected | not_observed_streaming | outcome_unknown | None`.

**Dependencies:** This is the first implementation task. It uses only existing domain types and preserves all transactional pairing variants.

- [ ] **Step 1: Write the failing domain and retention tests**

  Add tests for the exact `NoReplyReason` values, frozen relay records, the local prompt identity tuple `(receiver_id, start_operation_id, first credential_kind observed)`, lifecycle `live` and `ended` payloads, and every new command's route and fields. Add tests that a canary credential is absent from `repr`, `str`, equality diagnostics, pickle attempts, callback envelopes, safe snapshots, and any serialized relay record. Add AST extraction of `relay_credential_representable` and assert its body is exactly the leading-dash predicate. Test the pure helper against `-secret`, `--audio`, `--volume`, `-`, and ordinary values. Separately test that the dialog admission pipeline rejects empty, NUL-containing, platform-unencodable, and leading-dash raw input before constructing `SensitiveText` or issuing `SubmitCredential`.

- [ ] **Step 2: Run the focused tests and confirm the contract fails**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/domain/test_credential_relay.py tests/domain/test_contracts.py tests/storage/test_secret_boundaries.py --basetemp=/tmp/ap-relay-domain-contracts`

  Expected: FAIL because the new records, closed-union members, helper, and structural redaction assertions do not yet exist.

- [ ] **Step 3: Implement the minimal domain contracts**

  Add the frozen secret-free relay records and closed-union members at the existing domain seams. Capture Start identity and endpoint fields in `CredentialRequest` without a receiver request ID, provisional trust token, release evidence, settlement state, or second lease. Keep `CredentialPromptState` as a transient projection record. Add the actor commands with the exact field shapes from the specification and route them to `Route.mirror` or `Route.recovery` as required. Add `NoReplyReason` in `domain.resources` and preserve the existing `ResourceEvent` union.

- [ ] **Step 4: Implement and verify the no-retention helper and snapshot boundary**

  Implement `relay_credential_representable` with the exact AST body. Ensure `SensitiveText` never enters `SessionPersistence._safe_snapshot` or a domain record's retained representation. Extend protocol declarations only with the named relay methods and completion types. Keep `SubmitPairing`, `PairingResult`, `PairingFlow`, and the transactional `pairing_required` branch unchanged. Run the focused tests and inspect failure output for any canary-bearing assertion or diagnostic.

- [ ] **Step 5: Run the passing contract and structural checks**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/domain/test_credential_relay.py tests/domain/test_contracts.py tests/storage/test_secret_boundaries.py --basetemp=/tmp/ap-relay-domain-contracts-pass`

  Expected: PASS with no canary in captured representations. Also run `git diff --check` and verify that the transactional pairing tests remain unchanged and green through the existing focused pairing command.

- [ ] **Step 6: Commit the domain boundary**

  ```bash
  git add src/airplay_linux/domain/relay.py src/airplay_linux/domain/resources.py src/airplay_linux/domain/records.py src/airplay_linux/domain/messages.py src/airplay_linux/domain/ports.py src/airplay_linux/session/persistence.py tests/domain/test_credential_relay.py tests/domain/test_contracts.py tests/storage/test_secret_boundaries.py
  git commit -m "feat(relay): add secret-free credential contracts"
  ```

## Task 2: Implement the durable recovery marker store and target reservation

**Implementer tier:** Capable

**Category:** durable-storage

**Files:**

- Modify: `src/airplay_linux/storage/atomic.py:1-673`
- Modify: `src/airplay_linux/storage/codecs.py:1360-1509`
- Modify: `src/airplay_linux/storage/repositories.py:1-703`
- Modify: `src/airplay_linux/domain/ports.py:150-230`
- Modify: `src/airplay_linux/domain/records.py:180-318`
- Create: `tests/storage/test_credential_recovery.py`
- Modify: `tests/storage/test_atomic_records.py:1-2143`
- Modify: `tests/storage/test_reservation_codecs.py:1-369`
- Modify: `tests/storage/test_repositories.py:1-433`

**Interfaces:**

- Consumes: the frozen marker, identity, release-attempt, retirement-evidence, `Diagnostic`, `Result`, `AtomicFiles`, and existing JSON repository conventions from Task 1 and the current storage layer.
- Produces: `CredentialRecoveryMarkerStore` with `read_all`, `confirm_all_durable`, `for_target`, `create`, `mark_release_pending`, `dismiss`, `record_release_attempt`, `record_replacement_ready`, `retire_no_spawn`, `retire_after_reply`, and `retire_after_replacement`.
- Produces: `JsonCredentialRecoveryMarkerStore` rooted at `credential-recovery/`, with lowercase SHA-256 UTF-8 target filenames, strict schema and digest validation, one store-owned async lock, complete identity plus generation comparison, and sanitized `persistence_failed` or `ownership_mismatch` results.
- Produces: transitions that increment `marker_generation`, preserve retired tombstones, atomically set `state = "release_pending"` and `automatic_status_checks = false` for dismissal, and publish no cache or UI effect before a successful durability confirmation.

**Dependencies:** Task 1. Do not add coordinator startup behavior here. The store must be independently testable with fake `AtomicFiles`, clocks, and filesystem records.

- [ ] **Step 1: Write failing store, codec, and mutation-control tests**

  Add tests for canonical target filenames, valid schema decoding, unknown files, digest mismatch, duplicate targets, malformed records, unreadable files, and fail-closed non-empty errors. Cover generation conflicts and every transition's expected state, generation, and evidence. Add concurrent target reservations proving only one in-memory owner can create a marker before the durable create await. Add a fake `AtomicFiles.write` that reports an error after rename while exposing the exact next-generation bytes. Assert readback does not commit, retry writes identical bytes and repeats directory confirmation, and a failed retry leaves the target and global fences blocked. Cover `confirm_all_durable` over active and retired records, including restart after post-rename failure.

- [ ] **Step 2: Run the storage tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/storage/test_credential_recovery.py tests/storage/test_atomic_records.py tests/storage/test_reservation_codecs.py tests/storage/test_repositories.py --basetemp=/tmp/ap-relay-recovery-store`

  Expected: FAIL because the marker schema, codec, repository, and transition methods are absent.

- [ ] **Step 3: Implement strict marker codecs and the store protocol**

  Define the versioned codec for the marker, `OriginalDaemonIdentity`, `ReleaseAttempt`, `ReleaseAttemptTerminalEvidence`, `OwnedDaemonRecoveryRestart` evidence, and immutable retirement evidence. Reject receiver-controlled path material by recomputing the target digest. Decode all required fields with exact enum and type checks. Add the protocol beside the other lifecycle stores and keep raw exceptions, paths, record contents, and receiver text out of diagnostics.

- [ ] **Step 4: Implement generation-checked atomic transitions**

  Implement one lock-owned read, identity-and-generation comparison, canonical next-record construction, and `AtomicFiles.write` publication for each transition. Keep old verified cache state and fences when a write is ambiguous. Retry the exact intended bytes under the same lock only when recovery can select them. Make `dismiss` one atomic write that disables automatic checks. Allow no marker overwrite while watching or release-pending, and allow a new marker only at a strictly larger generation than the tombstone. Ensure `retire_no_spawn`, `retire_after_reply`, and `retire_after_replacement` validate their typed evidence before writing.

- [ ] **Step 5: Run the store tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/storage/test_credential_recovery.py tests/storage/test_atomic_records.py tests/storage/test_reservation_codecs.py tests/storage/test_repositories.py --basetemp=/tmp/ap-relay-recovery-store-pass`

  Expected: PASS. In a disposable copy, remove marker-before-dispatch ordering, treat readback as commit, skip one `confirm_all_durable` write, treat corrupt storage as empty, make dismissal two writes, or clear a marker after an absent/disconnected observation. Each mutation must fail a named control while the unmutated copy passes. Run `git diff --check`.

- [ ] **Step 6: Commit the durable store**

  ```bash
  git add src/airplay_linux/storage/atomic.py src/airplay_linux/storage/codecs.py src/airplay_linux/storage/repositories.py src/airplay_linux/domain/ports.py src/airplay_linux/domain/records.py tests/storage/test_credential_recovery.py tests/storage/test_atomic_records.py tests/storage/test_reservation_codecs.py tests/storage/test_repositories.py
  git commit -m "feat(recovery): add durable credential markers"
  ```

## Task 3: Add startup recovery coordination and registry exemptions

**Implementer tier:** Capable

**Category:** startup-recovery

**Files:**

- Modify: `src/airplay_linux/session/recovery.py:1-377`
- Modify: `src/airplay_linux/session/engine.py:1-814`
- Modify: `src/airplay_linux/runtime.py:1-957`
- Modify: `src/airplay_linux/process/daemon_registry.py:1-1483`
- Modify: `src/airplay_linux/process/ownership.py:1-618`
- Modify: `tests/session/test_startup_recovery.py:1-664`
- Modify: `tests/session/test_daemon_recovery.py:1-439`
- Modify: `tests/process/test_daemon_registry.py:1-983`
- Modify: `tests/storage/test_launch_receipts.py:1-514`

**Interfaces:**

- Consumes: `CredentialRecoveryMarkerStore` from Task 2, `OriginalDaemonIdentity`, `DaemonRecord`, `LaunchReceipt`, `DaemonCleanupIntent`, `SupervisorReleaseIntent`, `EvidenceAuthority`, `verify_owned_endpoint`, and the existing `StartupRecovery` and `ManagerDeps` seams.
- Produces: `CredentialRecoveryCoordinator` with `load_before_startup() -> Result[None]`, `recover_before_registry_reconciliation() -> Result[tuple[DaemonRecoveryExemption, ...]]`, `require_start_allowed(target, start_operation_id) -> Result[None]`, `start_dispatch_fence(target, start_operation_id) -> DispatchFence`, `begin_submission(marker) -> Result[CredentialRecoveryMarker]`, and `credential_dispatch_fence(marker_identity, marker_generation) -> DispatchFence`.
- Produces: `DaemonRecoveryExemption` values for one exact authenticated watching original or one exact verified replacement, and `DaemonRegistry.reconcile_previous(recovery_exemptions=...)` admission that revalidates each exemption against receipt and process identity.
- Produces: startup ordering in which all marker records are loaded and validated, `confirm_all_durable()` succeeds, target fences are populated, marker-bound recovery runs, and only then ordinary registry reconciliation and Start admission proceed.

**Dependencies:** Task 2. Task 8 later completes the endpoint-changing replacement operation while consuming the coordinator's release-attempt seam. This task must already block conservatively when a release attempt is unresolved.

- [ ] **Step 1: Write failing startup-order and evidence tests**

  Add tests that record the exact call order `load_before_startup`, marker validation, `confirm_all_durable`, fence population, `recover_before_registry_reconciliation`, and `reconcile_previous(recovery_exemptions=...)`. Assert no startup event, retired-tombstone trust, or Start admission occurs before confirmation. Cover unreadable, malformed, duplicate, ambiguous, and durability-failed markers returning only the sanitized recovery diagnostic while the process-wide barrier remains active. Add exact matching tests for launch UUID, daemon instance UUID, endpoint generation, PID/start ticks, runtime directory, socket path, and target identity. An unrelated receipt, same-address process, stale socket, or mismatched process must not be exempted or contacted.

- [ ] **Step 2: Run the startup tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_startup_recovery.py tests/session/test_daemon_recovery.py tests/process/test_daemon_registry.py tests/storage/test_launch_receipts.py --basetemp=/tmp/ap-relay-startup-recovery`

  Expected: FAIL because the coordinator, startup barrier, exemption parameter, and exact evidence reconciliation are not implemented.

- [ ] **Step 3: Implement the coordinator and process-wide admission barrier**

  Add the coordinator to `ManagerDeps` and compose it from the existing atomic root. Keep a target-keyed in-memory table that changes only after verified store transitions. Reserve a canonical target synchronously before awaiting `create`. Make `start_dispatch_fence` compose with the existing receiver and endpoint freshness fence, return `persistence_failed` with `keep_blocked` for global store health failure, and return `ownership_mismatch` with `keep_blocked` for another Start or release-pending marker. Make `credential_dispatch_fence` admit only the exact Start and marker generation in watching state.

- [ ] **Step 4: Implement startup ordering and exact daemon evidence**

  Change `StartupRecovery.run` to perform the specified load, validation, durability confirmation, fence construction, marker-bound recovery, and only then registry reconciliation. Add `recovery_exemptions` to the registry call while preserving ordinary processing for every non-exempt receipt. Authenticate a watching marker's original daemon and socket before resuming status checks. A stopped or unavailable original remains fenced and receives no command. Keep release-pending attempts blocked until Task 8 supplies a matching replacement proof. Ensure registry recovery reads supervisor intent as evidence and never uses it to re-signal a departed supervisor daemon.

- [ ] **Step 5: Run the startup tests and mutation controls**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_startup_recovery.py tests/session/test_daemon_recovery.py tests/process/test_daemon_registry.py tests/storage/test_launch_receipts.py --basetemp=/tmp/ap-relay-startup-recovery-pass`

  Expected: PASS. In disposable copies, skip startup durability confirmation, reconcile before marker restoration, accept a historical identity missing one field, or exempt a same-address unrelated process. Each mutation must fail its control. Also run the existing session pairing integration test to prove the transactional pairing path remains behaviorally unchanged.

- [ ] **Step 6: Commit startup recovery**

  ```bash
  git add src/airplay_linux/session/recovery.py src/airplay_linux/session/engine.py src/airplay_linux/runtime.py src/airplay_linux/process/daemon_registry.py src/airplay_linux/process/ownership.py tests/session/test_startup_recovery.py tests/session/test_daemon_recovery.py tests/process/test_daemon_registry.py tests/storage/test_launch_receipts.py
  git commit -m "feat(recovery): restore markers before daemon reconciliation"
  ```

## Task 4: Add fenced credential process and backend dispatch

**Implementer tier:** Advanced

**Category:** credential-dispatch

**Files:**

- Modify: `src/airplay_linux/process/linux.py:1-972`
- Modify: `src/airplay_linux/testing/adapters.py:1043-1280`
- Modify: `src/airplay_linux/process/credential_probe.py:1-1379`
- Modify: `src/airplay_linux/backend/contract.py:100-430`
- Modify: `src/airplay_linux/backend/control.py:1-1474`
- Modify: `src/airplay_linux/domain/ports.py:145-250`
- Test: `tests/process/test_linux_runner.py:1-1882`
- Test: `tests/testing/test_fakes.py:1-1947`
- Test: `tests/process/test_credential_probe.py:1-1105`
- Test: `tests/backend/test_control.py:1-1245`
- Create: `tests/backend/test_credential_relay.py`
- Test: `tests/adapter_contracts/test_keyring_contract.py:1-677`

**Interfaces:**

- Consumes: Task 1's `CredentialPayload`, `NoReplyReason`, `CredentialProcessResult` domain shape, `SensitiveText`, `ProfileCommand`, existing `EvidenceAuthority`, `BackendOperation.submit_pairing`, `OperationContext`, `DispatchFence`, and Task 3's owned-daemon authority.
- Produces: `ProcessPort.run_credential(spec: CommandSpec, timeout: float, dispatch_fence: Callable[[], Result[None]]) -> CredentialProcessResult`, with `CredentialProcessNotSpawned`, `CredentialProcessCompleted`, and `CredentialProcessUncertain`.
- Produces: `CredentialSubmissionReply` with `reply_at` and exact boolean `accepted`; `CredentialDispatchFailure` fields `dispatch_state`, `refusal`, `reply_at`, `reply_accepted`, and `reason`, with the specified literal validation and no receiver text.
- Produces: `CredentialRelayControl.submit_credential(target, payload, context, *, dispatch_fence) -> CredentialSubmissionReply | CredentialDispatchFailure` and `PublishedDoubletakeControl` implementation that forwards the original fence through awaited ownership proof and the final process admission boundary.
- Produces: exact legacy and extended `ProfileCommand` construction, final secret index, capability proof for both pinned help surfaces, target-scope proof, and profile-independent audio/volume representability refusal before process creation.

**Dependencies:** Tasks 1 and 3. Generic `BackendControl.request`, transactional `submit_pairing`, and the existing keyring contract must retain their current behavior.

- [ ] **Step 1: Write failing process, adapter, capability, and classification tests**

  Add tests for the three process results and prove `run_credential` returns `NotSpawned` only when no child can exist, `Uncertain` after child admission, timeout, cancellation, or ambiguous creation, and `Completed` only after a normal child exit. Add a barrier in awaited ownership validation and replace the target or marker generation to prove the final fence refuses before `asyncio.create_subprocess_exec`. Add exact legacy and extended argv tests, final secret index and redacted projections, malformed/partial/contradictory help rejection, exact target-scope wording, `target_volume` source-token proof, and capability separation from transaction flags, profile keyring, and composed keyring proof. Add decoder tests for exact boolean `ok`, exit statuses 0 and 1, malformed JSON, free-form error discard, fixed refusals, and all `NoReplyReason` values.

- [ ] **Step 2: Run the focused tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/process/test_linux_runner.py tests/testing/test_fakes.py tests/process/test_credential_probe.py tests/backend/test_control.py tests/backend/test_credential_relay.py tests/adapter_contracts/test_keyring_contract.py --basetemp=/tmp/ap-relay-dispatch`

  Expected: FAIL because credential-specific process admission, capability proofs, relay dispatch, and typed decoder results are incomplete.

- [ ] **Step 3: Implement the fenced process boundary**

  Extend `ProcessPort`, `LinuxProcessRunner`, and `FakeProcess` in `testing/adapters.py` with `run_credential`. The fake evaluates the same fence and returns the same typed admission results as the Linux runner. Its deferred callbacks and command-journal projections use the declared secret indices before retaining any record. Validate the command and secret index, invoke the supplied fence synchronously in the same task immediately before subprocess creation, and leave no await or task scheduling between the predicate and `asyncio.create_subprocess_exec`. Contain process exceptions and classify only proven pre-child failures as `NotSpawned`. Classify every post-admission timeout, cancellation, uncertain cleanup, or lost completion as `Uncertain`. Keep output transient and redacted in all retained fake and runner observations.

- [ ] **Step 4: Implement capability proof, command mapping, and adapter dispatch**

  Extend the pinned help parser to accept exactly the legacy 0.5.0 and approved extended target-volume tables, reject all altered or contradictory surfaces, and prove target scope with exact argument and affirmative wording checks. Keep relay capability independent from transaction capabilities, profile keyring, and composed keyring proof. Build the exact target-first command with the original Start audio and volume only when the extended table proves those options. Validate the raw credential again before constructing the command. Revalidate ownership after awaited proof, forward the same `dispatch_fence`, and evaluate it again at child creation. Decode only complete envelopes with boolean `ok`, discard receiver error text, and map outcomes to the fixed typed classifications.

- [ ] **Step 5: Run dispatch tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/process/test_linux_runner.py tests/testing/test_fakes.py tests/process/test_credential_probe.py tests/backend/test_control.py tests/backend/test_credential_relay.py tests/adapter_contracts/test_keyring_contract.py --basetemp=/tmp/ap-relay-dispatch-pass`

  Expected: PASS. In disposable copies, hard-code the secret index, skip one redacted observer, drop `dispatch_fence` forwarding, classify uncertainty as `Err`, remove final fence timing, or make the legacy profile carry unsupported audio/volume. Each mutation must fail a specific control while the clean baseline passes. Run the existing transactional pairing and backend control tests as a regression gate.

- [ ] **Step 6: Commit credential dispatch**

  ```bash
  git add src/airplay_linux/process/linux.py src/airplay_linux/testing/adapters.py src/airplay_linux/process/credential_probe.py src/airplay_linux/backend/contract.py src/airplay_linux/backend/control.py src/airplay_linux/domain/ports.py tests/process/test_linux_runner.py tests/testing/test_fakes.py tests/process/test_credential_probe.py tests/backend/test_control.py tests/backend/test_credential_relay.py tests/adapter_contracts/test_keyring_contract.py
  git commit -m "feat(relay): fence credential process dispatch"
  ```

## Task 5: Implement exact-row transport detection and pending-Start handoff

**Implementer tier:** Capable

**Category:** transport

**Files:**

- Modify: `src/airplay_linux/transport/doubletake.py:1-1477`
- Modify: `src/airplay_linux/domain/ports.py:120-250`
- Modify: `src/airplay_linux/transport/__init__.py:1-120`
- Modify: `tests/transport/test_doubletake.py:1-2846`
- Modify: `tests/adapter_contracts/test_transport_contract.py:1-300`
- Modify: `tests/testing/test_fakes.py:1-1947`
- Modify: `tests/fixtures/processes/fake_doubletake.py:1-882`

**Interfaces:**

- Consumes: Task 1 relay events and results, Task 4 `CredentialRelayControl.submit_credential`, `DispatchFence`, `StatusReply`, `BackendStream`, exact `ReceiverTarget.canonical_host`, `StreamHandle`, `OperationContext`, and the existing `TransportPort` stream lifecycle.
- Produces: `TransportPort.submit_credential(target, value, context, *, dispatch_fence) -> Result[Submitted | NoReply]`; `read_credential_status(target, context) -> Result[StatusReply]`; and `stop_pending_start(owner, stop_context) -> Result[StreamHandle]`.
- Produces: one exact `credential_request` transport event with `CredentialRequestPayload` lifecycle `live | ended`, owner identity, endpoint and daemon identity, and optional pending handle. The event is emitted before `start_stream` returns its handle or after the handle is installed, with stale events rejected by existing fences.
- Produces: target-specific connect and recovery behavior that reads the pinned daemon socket, maps only a unique matching row, never treats aggregate `pin_required` as a refusal or request, and disconnects only an exact matching target row.
- Produces: pending-start readiness handoff that validates the complete owner including optional submission ID, signals a per-lease readiness-stop event without `stream.lock`, transfers one exact handle, and never cancels credential dispatch or owner-correlated status observation.

**Dependencies:** Tasks 1 and 4. Task 6 consumes these events and methods. Preserve the existing non-relay stream, audio, source, and transactional pairing behavior.

- [ ] **Step 1: Write failing transport and handoff tests**

  Add cases for matching `canonical_host`, another target, normalized alternative address, aggregate-only credential fields, absent row, duplicate row, unsupported kind, and matching row ending before submission. Assert only one live request and one `ended` event per local prompt identity. Cover request delivery before and after the Start handle is installed. Add legacy and extended submit calls with original audio/volume snapshots and the unchanged dispatch fence. Add a unique canary to fake-process deferred callbacks, process command journals, and fake-daemon capture, then assert it is absent from retained records, safe failing assertions, and diagnostics. Add pre-submit and dispatched `PendingStartCleanupOwner` handoffs, including the receiver changing the target row to `connecting`, exact one-time handle transfer, stale and duplicate handoff refusal, lock release, original deadline retention, no second timeout, and exact target-specific stop.

- [ ] **Step 2: Run transport tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/transport/test_doubletake.py tests/adapter_contracts/test_transport_contract.py tests/testing/test_fakes.py --basetemp=/tmp/ap-relay-transport`

  Expected: FAIL because relay events, owner-correlated status and submission methods, pending-start handoff, and the fake-observer canary controls are not implemented.

- [ ] **Step 3: Implement target-bound request detection and typed submission mapping**

  Extend the target row matching logic to compare only the exact canonical target. Keep the aggregate connect acknowledgement as progress data and continue row polling when it reports `pin_required`. Emit a live request from exactly one supported matching row and emit `ended` only when a later pre-submit status lacks that row. Carry the Start identity, endpoint fingerprint/generation, daemon instance, and pending handle through the existing transport event envelope. Map typed adapter replies to `Submitted` or `NoReply`, preserve post-dispatch error semantics, and forward the exact fence without wrapping or dropping it.

- [ ] **Step 4: Implement owner-correlated status and pending-start transfer**

  Add read-only status using the pinned daemon record's explicit socket and no aggregate request construction. Track the optional submission operation ID on the pending stream. Add the per-lease readiness-stop event, check it before the normal operation-current test, race only the read-only readiness request, and release `stream.lock` before transferring the exact handle. Use a dedicated `_start` handoff branch that skips failure cleanup and causes the original acquisition to end as `Err(stale_event)` without dropping the transferred handle. Validate Stop context and every owner field, including marker generation for dispatched submissions.

- [ ] **Step 5: Run transport tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/transport/test_doubletake.py tests/adapter_contracts/test_transport_contract.py tests/testing/test_fakes.py --basetemp=/tmp/ap-relay-transport-pass`

  Expected: PASS. In disposable copies, remove the exact matching-target check, create a prompt from aggregate fields, cancel the credential command during handoff, restart the command timeout, transfer a handle twice, or disconnect all streams. Each mutation must fail its named control. Run the existing full transport suite for non-relay regression coverage.

- [ ] **Step 6: Commit transport integration**

  ```bash
  git add src/airplay_linux/transport/doubletake.py src/airplay_linux/domain/ports.py src/airplay_linux/transport/__init__.py tests/transport/test_doubletake.py tests/adapter_contracts/test_transport_contract.py tests/testing/test_fakes.py tests/fixtures/processes/fake_doubletake.py
  git commit -m "feat(relay): add exact-row transport handoff"
  ```

## Task 6: Add actor settlement, effect fencing, and ordered cleanup

**Implementer tier:** Capable

**Category:** session-lifecycle

**Files:**

- Modify: `src/airplay_linux/session/engine.py:1-814`
- Modify: `src/airplay_linux/session/fencing.py:1-192`
- Modify: `src/airplay_linux/session/mirror.py:1-562`
- Modify: `src/airplay_linux/session/cleanup.py:1-879`
- Modify: `src/airplay_linux/session/phase.py:1-260`
- Modify: `src/airplay_linux/domain/records.py:1-318`
- Modify: `tests/session/test_engine.py:1-585`
- Modify: `tests/session/test_mirror_and_trust.py:1-606`
- Modify: `tests/session/test_actor_fences.py:1-691`
- Modify: `tests/session/test_stop_matrix.py:1-248`
- Modify: `tests/session/test_lifecycle_regressions.py:1-340`

**Interfaces:**

- Consumes: Task 1 closed commands and completions, Task 5 request/submission/status/handoff methods, `EffectEvent`, existing per-receiver queues, `SessionContext`, `SessionSnapshot`, `CleanupRuntime`, and Task 3 coordinator fences.
- Produces: actor admission for `SubmitCredential` and `DismissCredentialPrompt` on `Route.mirror`, recovery commands on `Route.recovery`, captured `CredentialEffectOwner`, one `CredentialSubmissionSettlement` per submission ID, and typed `CredentialSubmissionCompletion` and `CredentialStatusCompletion` effects.
- Produces: `CredentialSubmissionSettlement` acceptance after Stop supersedes Start or prompt dismissal while suppressing prompt publication. It correlates operation ID, marker generation, Start/session/trust/endpoint/daemon identities, original command deadline, and cleanup phase.
- Produces: ordered result semantics where `no_spawn` retires only after concurrent cleanup, `Submitted` starts bounded status observation at actual reply time, `post_dispatch_error` refuses status polling, `NoReply` persists `release_pending` before cleanup, and marker retirement occurs only after terminal observation and cleanup settlement.
- Produces: exact Stop behavior for both pre-submit and dispatched pending Starts, including a single readiness exit, exact handle transfer, no readiness-budget wait, owner-correlated status completion after supersession, and target-specific disconnect before `cleanup_pending` clears.

**Dependencies:** Tasks 2, 3, and 5. This task must leave transactional pairing, trust state, and phase derivation unchanged outside the relay additions.

- [ ] **Step 1: Write failing actor, completion, and Stop matrix tests**

  Add tests for closed command/event routing, stale challenge and Start rejection before effect launch, the full `CredentialEffectOwner`, one settlement accepting one completion only while awaiting reply, status result wrapping, and generation validation after supersession. Cover pre-submit Stop, dispatched Stop before reply, receiver acknowledgement with `connecting` row, complete positive and negative replies, `NoReply`, ownership loss after reply, prompt dismissal, stale or duplicate callbacks, late target appearance, late disconnect, second Stop, repeated cleanup, failed cleanup, persistence failures, absent/disconnected samples, and marker retention.

- [ ] **Step 2: Run session tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_engine.py tests/session/test_mirror_and_trust.py tests/session/test_actor_fences.py tests/session/test_stop_matrix.py tests/session/test_lifecycle_regressions.py --basetemp=/tmp/ap-relay-session`

  Expected: FAIL because relay actor routing, settlement gates, owner-correlated completion admission, and pending-start cleanup ordering are absent.

- [ ] **Step 3: Implement actor commands and owner-correlated effects**

  Add `SubmitCredential` resolution against the current trust-neutral prompt and owning Start. Allocate a fresh submission operation ID, capture all freshness and marker fields, and reject stale challenge, replaced Start, release-pending, mismatched receiver, or obsolete session before launching an effect. Route relay completions through the existing `EffectEvent.result`, keep them separate from `TransportPayload` and `ResourceEvent`, and accept status completion only with the retained owner and settlement generations.

- [ ] **Step 4: Implement settlement and prompt-report suppression**

  Register the settlement after durable marker creation and before dispatch. Keep it admissible after prompt dismissal and Start supersession, but set `report_suppressed` before `ctx.supersede("stop")` and prevent it from publishing to a newer prompt or mutating the superseded Start. Map `CredentialDispatchFailure` exactly, start post-reply status observation from `reply_at`, retain original deadlines, and retain the marker on `NoReply` or any post-dispatch persistence/ownership failure.

- [ ] **Step 5: Implement ordered cleanup and prove both handoff forms**

  Capture `PendingStartCleanupOwner` before supersession. Call the corresponding transport handoff, skip suspended readiness waits, and wait for settlement transition and handle acquisition before target-specific disconnect. Ensure a dispatched handoff does not cancel the child, credential completion, or owner-correlated status read. Retire no-spawn, terminal reply, and replacement evidence only through the typed store operations. Keep absent, duplicate, failed, or unverifiable recovery observations fenced.

- [ ] **Step 6: Run session tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/session/test_engine.py tests/session/test_mirror_and_trust.py tests/session/test_actor_fences.py tests/session/test_stop_matrix.py tests/session/test_lifecycle_regressions.py --basetemp=/tmp/ap-relay-session-pass`

  Expected: PASS. In disposable copies, publish success without follow-up status, write the marker after dispatch, map uncertain dispatch to `Err`, retire on absent status, publish a stale completion, skip settlement after prompt dismissal, or clear `cleanup_pending` before handoff and disconnect. Each mutation must fail a control. Run transactional pairing integration and existing shutdown/drain tests.

- [ ] **Step 7: Commit actor settlement**

  ```bash
  git add src/airplay_linux/session/engine.py src/airplay_linux/session/fencing.py src/airplay_linux/session/mirror.py src/airplay_linux/session/cleanup.py src/airplay_linux/session/phase.py src/airplay_linux/domain/records.py tests/session/test_engine.py tests/session/test_mirror_and_trust.py tests/session/test_actor_fences.py tests/session/test_stop_matrix.py tests/session/test_lifecycle_regressions.py
  git commit -m "feat(relay): settle submissions across stop"
  ```

## Task 7: Add transient prompt and recovery projections

**Implementer tier:** Advanced

**Category:** presentation

**Files:**

- Modify: `src/airplay_linux/ui/state.py:1-1281`
- Modify: `src/airplay_linux/ui/presenter.py:1-1811`
- Modify: `src/airplay_linux/ui/dialogs.py:1-604`
- Modify: `src/airplay_linux/ui/window.py:1-964`
- Modify: `src/airplay_linux/storage/codecs.py:1490-1509`
- Modify: `src/airplay_linux/session/persistence.py:1-127`
- Modify: `src/airplay_linux/domain/records.py:1-318`
- Modify: `tests/ui/test_state_projection.py:1-1920`
- Modify: `tests/ui/test_presenter.py:1-2431`
- Modify: `tests/ui/test_dashboard_widgets.py:1-2235`

**Interfaces:**

- Consumes: Task 1's `CredentialPromptState`, `CredentialRequest`, `SensitiveText`, and relay commands; Task 6's session snapshots, settlement reports, recovery diagnostics, and actor routes; existing `PaneState`, `PairingDialog`, `ChallengeView`, `Action`, presenter maps, and pure projection rules.
- Produces: a transient `credential_prompt` snapshot projection that is set to `None` by `_safe_snapshot` and omitted from the closed `SESSION_SCHEMA`; no credential or prompt serialization path.
- Produces: relay dialog mode keyed by local prompt identity, with `Action.submit_credential`, `Action.dismiss_credential_prompt`, `Action.dismiss_credential_recovery`, and `Action.restart_receiver_to_release`; relay mode offers only submit and Cancel while transactional mode remains unchanged.
- Produces: raw input validation before `SensitiveText` construction and before `SubmitCredential`, one-call `PairingDialog.take_input()` that clears the edit control, disabled/read-only field transitions, fixed secret-free validation feedback, target-bound prompt selection, recovery warning/action visibility, and report states `connected`, `not_observed_streaming`, and `outcome_unknown`.

**Dependencies:** Tasks 1 and 6. Task 7 defines and dispatches the `RestartReceiverToRelease` recovery command; Task 8 supplies its endpoint-changing implementation. UI projection and command dispatch remain independently reviewable before daemon release is complete.

- [ ] **Step 1: Write failing projection, dialog, and canary tests**

  Add state-matrix tests for global Start blocking, `Cleanup pending`, retained target fences, release-pending restart visibility, disabled automatic checks after dismissal, recovery diagnostics, and each four-resource badge. Add presenter tests proving a prompt opens for its producing receiver even when another pane is selected, selection changes do not retarget it, repeated same-kind events do not reopen it, and a replacement Start or credential kind invalidates the old prompt. Add dialog tests for exact PIN/password wording, masked input, empty/NUL/unencodable/leading-dash validation before wrapping, `take_input` clearing, in-flight disablement, observing read-only state, Cancel without a receiver command, and absence of a unique canary from widgets, messages, snapshots, callbacks, and exports.

- [ ] **Step 2: Run UI tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/ui/test_state_projection.py tests/ui/test_presenter.py tests/ui/test_dashboard_widgets.py --basetemp=/tmp/ap-relay-ui`

  Expected: FAIL because relay prompt mode, recovery projections, input validation, and transient snapshot exclusion are not implemented.

- [ ] **Step 3: Implement transient snapshot and projection boundaries**

  Add `credential_prompt` to the live session projection while keeping it out of `_safe_snapshot` and every `SESSION_SCHEMA` field. Project typed diagnostics and target-blocked actions from their own authorities. Show `Cleanup pending` with the specified delayed-connection and repeated-Stop wording. Show `Restart receiver to release` only for a validated release-pending marker after direct operator action. Keep the marker generation and target fence invisible as secret-free identity data and do not expose any stored credential claim.

- [ ] **Step 4: Implement separate relay dialog and presenter routing**

  Add a relay mode to `PairingDialog` and a presenter map keyed by `(receiver_id, start_operation_id, first credential kind)`. Route submit to `SubmitCredential` and local dismissal to `DismissCredentialPrompt`. Preserve the existing `ChallengeView`, transactional dialog mode, `SubmitPairing`, challenge expiry, trust actions, and pairing wording. Keep the prompt bound to the producing Start regardless of selection changes. Offer only `submit_credential` and Cancel in relay mode, with no transactional retry, trust, Forget, or revoke action.

- [ ] **Step 5: Implement input lifetime and recovery actions**

  Use `relay_credential_representable` plus empty, NUL, and platform-encoding checks on raw text before constructing `SensitiveText` or creating an actor command. Make `take_input()` return one `SensitiveText` and clear the line edit in the same call. Disable the field during submission and make it read-only during bounded status observation. Close on Cancel without a receiver command. Dispatch `StopCredentialRecovery`, `DismissCredentialRecovery`, and `RestartReceiverToRelease` with the current marker generation and fixed target identity. Do not add a typing timer or a credential persistence path.

- [ ] **Step 6: Run UI tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/ui/test_state_projection.py tests/ui/test_presenter.py tests/ui/test_dashboard_widgets.py --basetemp=/tmp/ap-relay-ui-pass`

  Expected: PASS. In disposable copies, bypass dialog prevalidation, publish a prompt from another target, retarget after selection change, serialize the prompt, expose a transactional retry action in relay mode, or show restart before a validated release-pending marker. Each mutation must fail a control. Run the existing transactional pairing UI tests as a regression gate.

- [ ] **Step 7: Commit relay projections**

  ```bash
  git add src/airplay_linux/ui/state.py src/airplay_linux/ui/presenter.py src/airplay_linux/ui/dialogs.py src/airplay_linux/ui/window.py src/airplay_linux/storage/codecs.py src/airplay_linux/session/persistence.py src/airplay_linux/domain/records.py tests/ui/test_state_projection.py tests/ui/test_presenter.py tests/ui/test_dashboard_widgets.py
  git commit -m "feat(relay): add transient prompt projections"
  ```

## Task 8: Implement endpoint-changing daemon release and recovery attempts

**Implementer tier:** Capable

**Category:** daemon-recovery

**Files:**

- Modify: `src/airplay_linux/process/daemon_registry.py:1-1483`
- Modify: `src/airplay_linux/process/ownership.py:1-618`
- Modify: `src/airplay_linux/process/launcher.py:1-500`
- Modify: `src/airplay_linux/process/instance.py:1-400`
- Modify: `src/airplay_linux/session/recovery.py:1-377`
- Modify: `src/airplay_linux/storage/codecs.py:1360-1509`
- Modify: `tests/process/test_daemon_registry.py:1-983`
- Modify: `tests/session/test_daemon_recovery.py:1-439`
- Modify: `tests/session/test_startup_recovery.py:1-664`
- Modify: `tests/storage/test_launch_receipts.py:1-514`

**Interfaces:**

- Consumes: Task 2 marker transitions, Task 3 coordinator and `DaemonRecoveryExemption`, existing lifecycle lock, `LaunchReceipt`, cleanup intents, supervisor pairs, `EvidenceAuthority`, `verify_owned_endpoint`, and the Task 7 restart action.
- Produces: `DaemonRegistry.restart_with_new_endpoint(expected_old: OriginalDaemonIdentity, attempt_id: OperationId, runtime_directory: PrivateRuntimePath, socket_path: PrivateSocketPath) -> Result[OwnedDaemonRecoveryRestart]`.
- Produces: `termination_proof(identity) -> Result[TerminationProof]` and lock-free `_termination_proof_locked(identity, matching_receipt) -> Result[TerminationProof]`, with exact historical receipt resolution after in-memory registry reconciliation.
- Produces: unique private endpoint reservation, attempt-correlated launch and readiness evidence, exact old-process termination proof, idempotent attempt adoption, and `record_replacement_ready` followed by `retire_after_replacement` before releasing the target fence.
- Produces: startup recovery for interrupted attempts before ordinary reconciliation, with matching `recovery_attempt_id`, endpoint, instance, generation, process identity, socket identity, readiness, and old-daemon proof. Ambiguous evidence remains blocked and never launches another daemon.

**Dependencies:** Tasks 2, 3, 6, and 7. Task 7 supplies the operator action and presenter route; this task implements its endpoint-changing daemon operation. The marker store and coordinator remain the sole marker writers. This task must use the existing lifecycle and owned-daemon authority rather than introducing an ordinary restart shortcut.

- [ ] **Step 1: Write failing release, identity, and interruption tests**

  Add tests for matching and mismatched old daemon identity, historical termination proof after in-memory reconciliation, exact PID/start-ticks and socket identity, old socket isolation, endpoint separation, private path uniqueness and non-reuse, lifecycle lock ownership, readiness ordering, and shared-daemon restart warning. Cover interruption before reservation, during partial launch, after old termination, after replacement readiness publication, and before marker retirement. Verify matching `recovery_attempt_id` across marker, launch receipt, and readiness evidence. Add controls proving an unrelated receipt, same-address process, ordinary restart, or configured socket cannot satisfy release.

- [ ] **Step 2: Run release tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/process/test_daemon_registry.py tests/session/test_daemon_recovery.py tests/session/test_startup_recovery.py tests/storage/test_launch_receipts.py --basetemp=/tmp/ap-relay-release`

  Expected: FAIL because endpoint-changing release, exact termination proof, attempt persistence, and interrupted-attempt adoption are incomplete.

- [ ] **Step 3: Implement lock-safe termination proof and exact old identity checks**

  Add `termination_proof` with one lifecycle lock acquisition and `_termination_proof_locked` for callers that already hold it. Resolve exactly one historical receipt when the in-memory record is absent. Require the complete original identity, persisted cleanup intent, supervisor pair, process identity, runtime directory, socket path, endpoint generation, crash-window scan, and live-endpoint absence. Refuse ambiguous or incomplete evidence without signaling another process and preserve the marker fence.

- [ ] **Step 4: Implement unique private replacement reservation and readiness**

  Allocate a fresh attempt ID and never-used private runtime directory/socket under the app-owned root. Persist `record_release_attempt` before touching the daemon. Reject old, reused, non-private, already assigned, or undurable paths. Hold the lifecycle lock through old termination, reservation, launch, explicit-socket authentication, and normal owned-daemon readiness. Carry `recovery_attempt_id` in launch and readiness evidence and never call ordinary `restart()` or the configured socket.

- [ ] **Step 5: Implement durable replacement adoption and retry recovery**

  Validate `OwnedDaemonRecoveryRestart`, persist `record_replacement_ready`, then persist `retire_after_replacement`; release the target fence only after both writes succeed. Make the operation idempotent by attempt ID. On restart, adopt only one exact ready replacement with matching evidence. If terminal cleanup is proven without readiness, persist terminal evidence and allocate a new attempt and never-used endpoint. If any evidence is absent, duplicate, unreadable, contradictory, or ambiguous, retain the marker and block Start. Pass only revalidated watching-original or release-replacement exemptions to ordinary reconciliation.

- [ ] **Step 6: Run release tests and mutation probes**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/process/test_daemon_registry.py tests/session/test_daemon_recovery.py tests/session/test_startup_recovery.py tests/storage/test_launch_receipts.py --basetemp=/tmp/ap-relay-release-pass`

  Expected: PASS. In disposable copies, use ordinary restart, reuse an endpoint, acquire the lifecycle lock twice, accept lost historical identity, reconcile before marker restoration, retire before replacement readiness, or omit attempt-ID matching. Each mutation must fail a control. Run the daemon ownership and process teardown regressions as a separate gate.

- [ ] **Step 7: Commit endpoint-changing release**

  ```bash
  git add src/airplay_linux/process/daemon_registry.py src/airplay_linux/process/ownership.py src/airplay_linux/process/launcher.py src/airplay_linux/process/instance.py src/airplay_linux/session/recovery.py src/airplay_linux/storage/codecs.py tests/process/test_daemon_registry.py tests/session/test_daemon_recovery.py tests/session/test_startup_recovery.py tests/storage/test_launch_receipts.py
  git commit -m "feat(recovery): release shared daemon on a new endpoint"
  ```

## Task 9: Activate the assembled runtime, acceptance suite, and delivery documentation

**Implementer tier:** Capable

**Category:** assembled-acceptance

**Files:**

- Modify: `src/airplay_linux/runtime.py:1-957`
- Modify: `src/airplay_linux/app.py:1-400`
- Modify: `tests/app/test_composition.py:1-282`
- Modify: `tests/runtime/test_capability_report.py:1-222`
- Modify: `tests/runtime/test_owned_probe.py:1-47`
- Modify: `tests/acceptance/test_no_production_spawn.py:1-374`
- Modify: `tests/integration/test_concurrent_sessions.py:1-500`
- Modify: `tests/ui_acceptance/test_multireceiver_flow.py:1-617`
- Modify: `tests/session/test_pairing_integration.py:1-213`
- Modify: `tests/session/test_shutdown.py:1-293`
- Modify: `tests/session/test_shutdown_drain.py:1-94`
- Modify: `README.md:1-104`
- Modify: `docs/backend-compatibility.md:1-213`
- Modify: `docs/operations.md:1-140`

**Interfaces:**

- Consumes: Tasks 1–8's records, marker store and coordinator, capability proof, process and backend dispatch, exact-row transport, session settlement, transient projections, and endpoint-changing release.
- Produces: `runtime.py` composition that wires `CredentialRecoveryCoordinator`, marker store, credential relay control, transport events, recovery handlers, presenter actions, and the existing owned-daemon installation without activating the graph before all lifecycle seams are installed.
- Produces: assembled-runtime acceptance from operator Start through matching request, prompt, submit, target-specific status observation, report, unknown-outcome recovery, Stop handoff, and endpoint-changing release using only fake process/control/runtime seams.
- Produces: documentation describing the Route B scope, exact backend profiles and argv, independent authorization proofs, storage and recovery limits, delayed connections, target-specific Stop, dismissal fencing, application-restart behavior, and shared-daemon restart impact.
- Produces: a complete deterministic test inventory with a clean baseline, passing controls, named assertions for every required mutation, and no production spawn or real receiver dependency.

**Dependencies:** Tasks 1 through 8. This is the only task allowed to activate the assembled graph in `runtime.py`. Do not modify `2026-11-15-gui-device-pairing.execution-graph.yaml` or its DOT artifact.

- [ ] **Step 1: Write failing composition and end-to-end acceptance tests**

  Add composition tests that prove all producers are installed before handlers and `SessionManager` are activated, the coordinator is in `ManagerDeps`, and the public Start predicate requires composed `owned_daemon_control` and `keyring` while relay capability remains independent. Add no-production-spawn acceptance using fake process/control/runtime adapters for operator Start, exact target request, prompt display, raw input, marker-before-dispatch, typed submission, target-specific streaming observation, and report. Add end-to-end unknown-outcome, dismissed-prompt, Stop-before-handle, Stop-after-acknowledged-connecting, late target, cleanup failure, restart recovery, and old-socket isolation scenarios.

- [ ] **Step 2: Run the assembled tests and confirm they fail**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/app/test_composition.py tests/runtime/test_capability_report.py tests/runtime/test_owned_probe.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py tests/session/test_pairing_integration.py tests/session/test_shutdown.py tests/session/test_shutdown_drain.py --basetemp=/tmp/ap-relay-assembled`

  Expected: FAIL because the assembled runtime has not yet wired the new route and the acceptance controls are incomplete.

- [ ] **Step 3: Activate the complete runtime composition**

  Wire the marker store from the existing `AtomicFiles` root, instantiate the recovery coordinator with the closed Start barrier, pass recovery exemptions through startup, connect relay handlers to the existing per-receiver actor queues, and install the endpoint-changing release handler. Keep the capability report and backend compatibility checks explicit. Preserve the inactive-composition shutdown shortcut and all existing generic, transactional pairing, keyring, trust, display, audio, and daemon lifecycle handlers.

- [ ] **Step 4: Implement the assembled acceptance flow and controls**

  Drive one fake receiver through Start, one exact matching `pin_required` row, live prompt event, dialog submission, durable marker creation, credential dispatch, complete reply, bounded target-specific status observation, and `streaming` report without a production spawn. Add every §10 required row, including capability proof, authorization separation, command construction, input validation, process classification, exact-row detection, closed unions, pending-Start handoff, durable recovery, reporting, canary absence, assembled acceptance, settlement after Stop and dismissal, endpoint-changing release, and stale callback fencing. For §10.8, assert each §7.2 observation separately: request row ends before submit with no result and Start resumes; a submitted row leaving `pin_required` remains read-only while polling; exact-target `streaming` yields only `Connected to <TV>.`; absent target or deadline yields only `The target was not observed streaming.`; no complete reply including shutdown yields only the fixed unknown-outcome wording; cancel sends no command and suppresses every later report. Cover complete negative replies followed by status polling and prove late streaming never retroactively changes a closed prompt's report. Exercise the assembled runtime in operator-started Mirror mode only and prove virtual-display starts cannot produce a credential prompt. Keep every mutation in a disposable copy with a clean baseline and a passing control.

- [ ] **Step 5: Run the complete mutation battery and independent suites**

  Run: `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/app/test_composition.py tests/runtime/test_capability_report.py tests/runtime/test_owned_probe.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py tests/session/test_pairing_integration.py tests/session/test_shutdown.py tests/session/test_shutdown_drain.py --basetemp=/tmp/ap-relay-assembled-pass`

  Expected: PASS with no production process, receiver, KDE, or host Go invocation. Run the complete repository suite with `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest --basetemp=/tmp/ap-relay-full`. In disposable copies, remove matching-target detection, remove credential representability validation, hard-code the secret index, skip one redaction observer, publish success without the follow-up status, write the marker after dispatch, map uncertainty to `Err`, drop dispatch-fence forwarding, clear release-pending after absent/disconnected status, omit a closed-union branch, and publish a stale relay completion. Require each mutation to fail its specific assertion while the clean baseline and independent controls pass. Record the Go >=1.22 toolchain requirement if the vendored receiver's pinned build mechanism cannot run on the host's Go 1.14.2.

- [ ] **Step 6: Write the required delivery documentation**

  Update `README.md` to say that an operator-started Mirror with a proven `credential_relay` profile can present a PIN/password prompt for the started target and reports success only when that target reports `streaming`. Update `docs/backend-compatibility.md` with both pinned help profiles, target-first legacy and extended argv, final secret index, exact scope proof, EvidenceAuthority receipt refresh, endpoint requirements, independent keyring composition, disabled transactional pairing, and separate target-volume proof. Update `docs/operations.md` with address continuity, unknown outcomes, Stop limits, delayed connections, repeated target-specific Stop, dismissal retaining the Start fence, restart recovery, shared-daemon impact, and no monitoring promise while stopped. Keep all prose aligned with observed target-specific status and never say the credential is stored or remembered.

- [ ] **Step 7: Run final verification and prepare the handoff**

  Run the focused assembled suite again with `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest tests/app/test_composition.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py --basetemp=/tmp/ap-relay-final`, then run `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest --basetemp=/tmp/ap-relay-final-full`, `git diff --check`, and a repository search proving the canary is absent from retained output fixtures and generated reports. Verify no execution graph or DOT artifact changed and report the exact Go toolchain prerequisite without attempting a host `go build`.

- [ ] **Step 8: Commit the assembled feature and documentation**

  ```bash
  git add src/airplay_linux/runtime.py src/airplay_linux/app.py tests/app/test_composition.py tests/runtime/test_capability_report.py tests/runtime/test_owned_probe.py tests/acceptance/test_no_production_spawn.py tests/integration/test_concurrent_sessions.py tests/ui_acceptance/test_multireceiver_flow.py tests/session/test_pairing_integration.py tests/session/test_shutdown.py tests/session/test_shutdown_drain.py README.md docs/backend-compatibility.md docs/operations.md
  git commit -m "feat(relay): integrate route-b pairing and recovery"
  ```
