# Dashboard Device Pairing — Mirror Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Let an operator answer an owned mirror start's receiver PIN/password request in the dashboard, with receiver-owned credential storage and completion only when that start's exact handle is admitted.

**Architecture:** Pure transient records and one submission predicate connect a typed, fenced control adapter to a per-lease transport state machine. The receiver actor owns public challenge revisions and submission-completion identity; one transport release transition owns settlement and teardown. Transactional pairing retains its existing profile, tokens, payload channel and fence-less submission behavior.

**Tech Stack:** Existing Python >=3.12, dataclasses, asyncio, PySide6, qasync, pytest, pytest-asyncio and pytest-qt; existing process, storage, clock and scheduler ports. No added package, receiver patch, capability token or wire field.

## Global Constraints

**Authority:** `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md` and `docs/superpowers/specs/2026-11-15-gui-device-pairing-spec.md`, including the specification's reviewed amendments, complete field inventories, transition tables, §16 identifiers and mutation assignments. The design's retained review-status text does not reopen approval.

**Scope:** “Enter the PIN in the app; the receiver tool keeps the credential; no app credential store; no forget action”. “**Mirror only.** Virtual-display pairing is deferred; a virtual start that meets a credential request keeps today's refusal”.

**Fixed limits:** Exactly three consumed submissions per request generation; credential lifetime 120.0 s absolute; permission allowance 300.0 s; handshake 20.0 s from prepared-source evidence; source verification 5.0 s; CONTROL_TIMEOUT=5.0; SOURCE_POLL_INTERVAL=0.1; existing whole-application bound 15.0 s. Equality with an active deadline means expired. Only RelayPhaseController owns relay phase deadlines.

**Identity and state:** Every observation/command binds receiver, exact target/fingerprint, endpoint generation, session generation, original owner lease and original daemon UUID. One `domain.relay.can_submit` serves presentation, actor admission and the final spawn fence. Release sets submit_fenced synchronously before its first await; retained attempts/retries preserve it. An uncertain command remains an acquisition obligation after its caller completes or the target disappears.

**Custody and clocks:** No application credential record/reference, argv in diagnostics/receipts/records/PM ledger, durable relay trust or new persistence field. Raw transient input reaches only the credential payload/command/actual execution path. Every retained observer projects secret indices before retention. All transient times use injected monotonic Clock; TerminationProof.cleanup_completed_at copies persisted wall time unchanged and compares only with persisted intent_at.

**Compatibility:** Public relay leaves target_bound_pairing, provisional_commit, cancel_pairing and revoke_pairing false. `submit_pairing` is not added to the unconditional FENCE_REQUIRED_OPERATIONS set. The transactional coordinator and `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags` keep their delivered behavior; that existing test function remains byte-for-byte unchanged, including its command-count assertions. `runtime.derive_composition_capabilities` already uses replace(probed, ...); no remembered_pairing promotion accompanies relay.

**Environment:** Existing dependencies and ErrorCode vocabulary only. All validation is offline, with existing tripwires installed. No production backend/compositor/portal/receiver/Secret Service experiment, installation or session-terminating operation is authorized. Validation argv below contain no shell syntax or Git mutation. Fixed short `/tmp` basetemps are exclusive test resources, not production runtime roots; Python's pytest module is run from each lane worktree.

**Lanes and intended parallelism:** Three lanes: `core` owns pure domain contracts, historical termination evidence and observation/fixture redaction; `backend` owns capability/command/control authority and transport; `dashboard` owns actor, UI, acceptance, canary and documentation. After Task 1, Tasks 2, 3 and 6 may run concurrently. Task 4 can overlap Task 5 after Task 2 and Task 3; Task 6 can overlap both. Task 10 can overlap transport Tasks 7–9 after Task 6. Backend Tasks 2 → 4 → 7 → 8 → 9 are sequential, as are core Tasks 1 → 3 → 5 → 12 and dashboard Tasks 6 → 10 → 11 → 13. Task 7 also waits for Task 5; Task 8 waits for Task 3; Task 11 joins Tasks 9 and 10; Task 12 joins Tasks 5 and 9; Task 13 joins Tasks 11 and 12. All cross-lane edits are disjoint, including shared test support, docs and existing regression-test adaptations. Same-lane path overlap is intentional. A consumer's exact producer contracts below are prerequisites in its lane worktree; task numbers describe dependencies, not a requirement to serialize independent lanes.

**File responsibilities:** `domain/relay.py` contains the pure records/predicates, not asyncio tasks or mutable transport authority. `transport/relay.py` contains CredentialDispatch, RelayLease and RelayPhaseController. Backend command metadata stays in backend/control.py; sanitized dispatch failures stay in backend/contract.py. Registry history queries observe only. Shared test support lives in `tests/relay_support.py`, imported explicitly by each consuming test module; no change to `tests/conftest.py` or new autouse fixture. Remaining files retain their existing responsibilities.

**Authority findings:** Specification D-01 assigns exact pytest identifiers to the design's twenty strategy labels; those assigned names are authoritative. D-02 corrects the nonexistent `_after_connect` locator to `_start` in transport/doubletake.py. These are resolved provenance discrepancies, not missing product decisions. No unresolved behavior gap was found in the approved specification. Virtual-display pairing is the explicit deferred open item: its connecting acknowledgement returns a handle before the later prompt, so relay would need another readiness owner. The eventual delivery summary includes that reason without reopening the decision.

## Task 1: Establish pure relay records, shared predicates and command metadata

**Implementer tier:** Advanced

**Lane:** core
**Depends on:** none
**Owns:** src/airplay_linux/domain/relay.py, src/airplay_linux/domain/operations.py, src/airplay_linux/domain/records.py, src/airplay_linux/domain/messages.py, src/airplay_linux/domain/ports.py, tests/domain/test_credential_relay.py
**Exclusive resources:** pytest-relay-core
**Interfaces produced:** `can_submit(lease: RelayLeaseView) -> bool`; `relay_credential_representable(value: str, *, target_volume_proven: bool) -> bool`; the complete frozen RelayStartOptions, CredentialRequest, SuccessPending, RelayLeaseView, TerminationProof and ReleaseEvidence records from specification §4; `ChallengeView(..., presentation_revision: int = 0)` and `PairingResult(..., waiting: bool = False)` with the full existing fields preserved; `SessionSnapshot(..., relay: RelayLeaseView | None = None, relay_notice: RelayNotice | None = None)`; `RelayPairingPayload(*, lease: RelayLeaseView)` and `RelaySubmitCompletion(*, submission_id: OperationId, challenge_id: ChallengeId, receiver_id: ReceiverId, endpoint_generation: int, session_generation: int, owner_lease: LeaseId, daemon_instance_uuid: InstanceId, generation: int, result: PairingResult)`; `CommandSpec(*, argv: tuple[str, ...], env: Mapping[str, str], cwd: str | None = None, pass_fds: tuple[int, ...] = (), secret_indices: tuple[int, ...] = ())`, `CommandSpec.redacted_argv -> tuple[str, ...]` and redacted `CommandSpec.__repr__(self) -> str`.
**Interfaces consumed:** Existing `ReceiverTarget`, `ReceiverId`, `ChallengeId`, `OperationId`, `LeaseId`, `InstanceId`, `ProcessIdentity`, `ChallengeView`, `AudioPolicy`, `VolumeLevel`, `ErrorCode`, `Diagnostic`, `Err`, `Ok` and `Result` retain their current definitions. ProcessPort retains `async run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Result[CommandOutput]` and `async spawn(self, spec: CommandSpec) -> Result[ProcessIdentity]`.
**Validation:** python -m pytest -q tests/domain tests/storage/test_reservation_codecs.py --basetemp=/tmp/ap-relay-core
**Commit message:** feat(domain): define transient mirror pairing relay contracts

**Files:**

- Create: `src/airplay_linux/domain/relay.py`.
- Modify: `src/airplay_linux/domain/operations.py:133-185`, preserving PairingChallenge's token requirement.
- Modify: `src/airplay_linux/domain/records.py:17-42` and `:231-293`, adding only transient snapshot fields.
- Modify: `src/airplay_linux/domain/messages.py:7-27` and `:227-299`, extending the applicable payload/result typing without a new wire event.
- Modify: `src/airplay_linux/domain/ports.py:276-288`, including its obsolete no-secret-argv docstring.
- Create/Test: `tests/domain/test_credential_relay.py`.

### Steps

- [ ] Read specification §§3.1, 4, 6, 11.2 and 15 and the existing domain modules. Add pure tests before implementation: frozen record copying, default compatibility, non-integer/bool/negative revision rejection, every eligibility conjunct independently false, fingerprint/target/kind/owner mismatches, extended-versus-legacy representability, metadata validation and safe nested repr. These supplement the named §16 integration identifiers; they do not replace them or define those names in another file.
- [ ] Run the validation command and record the missing-contract failures. Import-only failure is acceptable at this initial domain RED step; mutation kills later require an executing green control.
- [ ] Transcribe the literal aliases and complete frozen record declarations from specification §4, with exactly its field names/types/defaults. Define RelayPairingPayload and RelaySubmitCompletion in domain/messages.py as §3.1 requires; type-only imports break cycles. CredentialDispatch and RelayLease belong to transport/relay.py in Task 7, and CredentialDispatchFailure belongs to backend/contract.py in Task 2. No runtime import of transport, Qt, process runner, backend implementation or storage enters domain/relay.py. ReleaseEvidence's AckReply/StatusReply annotations use TYPE_CHECKING. Add the two defaulted operation fields, exact revision validation, and transient snapshot fields. Existing persistent codecs keep their explicit allowlist.
- [ ] Implement the shared predicate literally, with no additional local clocks or authorization side effects:

```python
def can_submit(lease: RelayLeaseView) -> bool:
    r = lease.request
    c = lease.challenge
    return (
        r is not None
        and c is not None
        and lease.owner_live
        and lease.request_state == "live"
        and lease.request_waiting
        and r.generation == lease.generation
        and r.receiver_id == lease.receiver_id == c.receiver_id
        and r.target == lease.target
        and r.endpoint_generation == lease.endpoint_generation == c.endpoint_generation
        and r.session_generation == lease.session_generation == c.session_generation
        and r.owner_lease == lease.owner_lease
        and r.daemon_instance_uuid == lease.daemon_instance_uuid
        and c.credential_kind == r.credential_kind
        and c.endpoint_fingerprint == lease.target.endpoint_fingerprint
        and lease.submit_inflight is None
        and not lease.submit_fenced
        and lease.dispatch_state in {"none", "settled"}
        and lease.success_pending is None
        and not lease.prepared_source
        and not lease.streaming
    )


def relay_credential_representable(
    value: str, *, target_volume_proven: bool,
) -> bool:
    return not (target_volume_proven and value.startswith("-"))
```

- [ ] Use a test-local `eligible_view` fixture built from the exact §4 fields and existing `testing.builders.target/context`, with one matching request/challenge and zero progress/barriers. The negative test replaces one field at a time; it must retain a passing eligible control:

```python
@pytest.mark.parametrize("changes", [
    {"owner_live": False}, {"request": None}, {"challenge": None},
    {"request_state": "Stale"}, {"request_waiting": False},
    {"submit_inflight": 1}, {"submit_fenced": True},
    {"dispatch_state": "uncertain"}, {"dispatch_state": "acknowledged"},
    {"prepared_source": True}, {"streaming": True},
])
def test_each_relay_eligibility_gate(eligible_view, changes):
    assert can_submit(eligible_view)
    assert not can_submit(replace(eligible_view, **changes))
```

- [ ] Keep CommandSpec frozen/keyword-only, set repr=False, retain MappingProxyType(env), and validate `type(indices) is tuple`, unique indices, `type(i) is int` and bounds with a fixed `ValueError("invalid secret indices")`. Its redacted_argv replaces exactly the declared positions. Its repr renders redacted_argv, never the original argv. Example metadata test:

```python
@pytest.mark.parametrize("indices", [(-1,), (2,), (True,), (1, 1)])
def test_command_secret_indices_are_exact(indices):
    with pytest.raises(ValueError, match="^invalid secret indices$"):
        CommandSpec(argv=("ctl", "value"), env={}, secret_indices=indices)


def test_command_projection_preserves_execution_arguments():
    command = CommandSpec(argv=("ctl", "1234"), env={}, secret_indices=(1,))
    assert command.argv[1] == "1234"
    assert command.redacted_argv == ("ctl", "<redacted>")
    assert "1234" not in repr(command)
```

- [ ] Run the declared validation; inspect imports to confirm domain/relay.py is pure and no persistent codec accepts these new records. Commit only this task's owned paths with its declared message during execution. Planning does not execute that commit.

## Task 2: Prove relay capability and construct typed target-bound commands

**Implementer tier:** Advanced

**Lane:** backend
**Depends on:** 1
**Owns:** src/airplay_linux/backend/contract.py, src/airplay_linux/backend/control.py, src/airplay_linux/process/ownership.py, tests/backend/test_credential_relay.py, tests/backend/test_control.py, tests/backend/test_contract.py, tests/transport/test_doubletake.py
**Exclusive resources:** pytest-relay-backend
**Interfaces produced:** `BackendFeatures(..., credential_relay: bool = False)`; `_credential_relay_proven(*, verified: bool, signatures: dict[str, str] | None) -> bool`; `features_from_help(sender_help: str, ctl_help: str, *, advertised: frozenset[str] = frozenset()) -> BackendFeatures`; `CredentialDispatchFailure(*, error: Diagnostic, dispatch_state: Literal["no_spawn", "acknowledged", "uncertain"], refusal: Literal["shutdown", "ended", "stale", "rejected"] | None = None)` extending Err; `ProfileCommand(argv: tuple[str, ...], secret_indices: tuple[int, ...] = ())`, `ProfileCommand.redacted_argv -> tuple[str, ...]`, redacted `ProfileCommand.__repr__(self) -> str`; `CredentialPayload(*, value: SensitiveText | str, audio: AudioPolicy | None = None, initial_volume: VolumeLevel | None = None)`; both profiles' `try_argv(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload) -> Result[ProfileCommand]`, public profile `argv(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload) -> ProfileCommand`; `PublishedDoubletakeControl._run(self, command: ProfileCommand, *, operation: BackendOperation) -> Result[CommandOutput]` as an async method.
**Interfaces consumed:** Task 1 `relay_credential_representable(value: str, *, target_volume_proven: bool) -> bool`; `CommandSpec(*, argv: tuple[str, ...], env: Mapping[str, str], cwd: str | None = None, pass_fds: tuple[int, ...] = (), secret_indices: tuple[int, ...] = ())`; `CommandSpec.redacted_argv -> tuple[str, ...]`; unchanged `ConnectPayload(*, source: CaptureSource | None = None, credential: SensitiveText | str | None = None, audio: AudioPolicy | None = None, initial_volume: VolumeLevel | None = None)` and existing VolumeLevel validation.
**Validation:** python -m pytest -q tests/backend tests/transport/test_doubletake.py --basetemp=/tmp/ap-relay-backend
**Commit message:** feat(backend): prove credential relay and carry redacted command metadata

**Files:**

- Modify: `src/airplay_linux/backend/contract.py:104-155` and `:846-976`.
- Modify: `src/airplay_linux/backend/control.py:122-129`, `:410-584`, `:722-763`, `:814-843`, `:912-951`, `:1114-1137`, `:1291-1323` for typed mapping and all _run callers.
- Modify: `src/airplay_linux/process/ownership.py:318-344`, adding the exact allows branch.
- Create/Test: `tests/backend/test_credential_relay.py`, initial identifiers `test_relay_capability`, `test_relay_authorization`, `test_relay_command`, `test_relay_metadata_refusals`.
- Modify/Test: `tests/backend/test_control.py:173-253`, `:448-465`, `:601-642` for typed-return assertions only; preserve the §5.2 truth-table function unchanged.
- Modify/Test: `tests/transport/test_doubletake.py:2732-2749` for the existing try_argv assertion's `.value.argv`; no relay behavior is retrofitted into its old fixture.
- `tests/backend/test_contract.py` may adapt explicit field inventories for the new independent bool; existing feature assertions stay intact.

### Steps

- [ ] Add the four named tests and run them RED. Build `relay_help_tables` locally from the existing verbatim backend captures plus the extended table used by test_contract.py's volume_help fixture. Parametrize exact legacy/extended, absent pin, added row, wrong signature, failed source verification and inconsistent token/table pairs. Both accepted tables prove relay; no unrelated transactional feature becomes true.
- [ ] Add the independent capability derivation literally, called with existing `verified` and decoded `signatures`; default/unknown/application fixture profiles keep False unless proof actually comes from the recognized public surface:

```python
def _credential_relay_proven(*, verified: bool,
                             signatures: dict[str, str] | None) -> bool:
    return verified and (
        signatures == _PINNED_050_CTL_COMMANDS
        or signatures == _VOLUME_CTL_COMMANDS
    )
```

- [ ] Define ProfileCommand with the same index validation/projection rules as Task 1 and fixed safe repr. CredentialPayload's repr exposes neither value nor a stringified payload. Add CredentialDispatchFailure with sanitized Diagnostic and the exact closed classification fields. Add only this authorization branch, retaining existing branches:

```python
if operation is BackendOperation.submit_pairing:
    return (f.credential_relay and f.owned_daemon_control and f.multi_target
            and f.physical_source)
```

- [ ] Convert every profile return to `Ok(value=ProfileCommand(argv=...))` and public argv to the command object; migrate `_run`, `_revalidate_ownership`, HandshakeAuthority.prove and request to preserve it and pass the actual operation. Empty indices accompany all old operations and every ApplicationContractProfile command. The fixture executable/payload channel stays unchanged; credentials in that profile remain outside argv.
- [ ] In public submit_pairing only, require named target, proved relay and CredentialPayload. Repeat representability validation before command construction. Reuse ConnectPayload's existing option checks, including disabled-audio/volume refusal and missing target-volume proof. Append the credential after all options, with no selector or delimiter:

```python
arguments = () if payload.audio is None else (
    "--audio", "on" if payload.audio.enabled else "off",
)
if payload.initial_volume is not None:
    arguments += ("--volume", str(payload.initial_volume.value))
completed = (
    "doubletake-ctl", "-socket", self.socket_path, "connect",
    target.canonical_host, *arguments, credential,
)
return Ok(value=ProfileCommand(
    argv=completed, secret_indices=(len(completed) - 1,),
))
```

Here `credential` is the existing boundary reveal of payload.value after syntax/representation checks; it is local only. ConnectPayload.credential remains refused for ordinary connect. begin/cancel/prepare/commit/mark_invalid/revoke pairing remain unmapped publicly; targetless pin is never returned.

- [ ] `test_relay_command` parametrizes all five §5.3 option forms and both accepted tables; asserts target, audio/volume tokens and computed final index. It will acquire its real waiting-entry/fall-through variants in Task 4 after preflight exists, in the same top-level definition. `test_relay_authorization` toggles relay/owned/multi_target/physical individually, keeps all four transaction flags false, and proves virtual_source alone insufficient.
- [ ] Validate metadata before any I/O at both command construction and _run. Missing relay final index, bool/negative/duplicate/out-of-range indices or nonempty indices on another operation yield invalid_input without process entry. Example assertions inside the parametrized command test:

```python
mapped = profile.try_argv(BackendOperation.submit_pairing, receiver, payload)
assert isinstance(mapped, Ok)
command = mapped.value
assert command.argv[:5] == (
    "doubletake-ctl", "-socket", profile.socket_path, "connect", receiver.canonical_host,
)
assert command.secret_indices == (len(command.argv) - 1,)
assert command.redacted_argv[-1] == "<redacted>"
assert command.argv[5:-1] == expected_options
```

- [ ] Run validation, adapting only existing assertions that require the intentional typed return or new field inventory. Do not add tuple indexing/equality shims to ProfileCommand. Confirm the unchanged truth-table gate passes with its original command counts; commit owned paths.

## Task 3: Query historical exact-instance termination evidence without control effects

**Implementer tier:** Advanced

**Lane:** core
**Depends on:** 1
**Owns:** src/airplay_linux/process/daemon_registry.py, tests/process/test_relay_termination.py
**Exclusive resources:** pytest-relay-core
**Interfaces produced:** `DaemonRegistry.termination_proof(self, record: DaemonRecord) -> Result[TerminationProof]` as an async, observation-only method.
**Interfaces consumed:** Task 1 `TerminationProof(*, daemon_instance_uuid: InstanceId, daemon: ProcessIdentity, runtime_directory: str, launch_uuid: UUID, receipt_state: Literal["stopped", "failed"], cleanup_outcome: Literal["stopped"] | None, cleanup_completed_at: float | None, process_evidence: Literal["pid_absent", "pidfd_exited"], identity_verified_at: float, observed_at: float)`; existing `LaunchReceiptStore.all(self) -> Result[tuple[LaunchReceipt, ...]]` and `DaemonCleanupIntentStore.all(self) -> Result[tuple[DaemonCleanupIntent, ...]]` as async methods, `Clock.now(self) -> float`, `read_identity_evidence(pid: int, proc_root: Path = Path("/proc")) -> IdentityEvidence`, `ProcessIdentity(*, pid: int, start_ticks: int)`.
**Validation:** python -m pytest -q tests/process/test_relay_termination.py tests/process/test_daemon_registry.py tests/process/test_procfs.py --basetemp=/tmp/ap-relay-core
**Commit message:** feat(process): prove historical daemon teardown by exact identity

**Files:**

- Modify: `src/airplay_linux/process/daemon_registry.py:15-50`, `:276-291`, `:393-411` as insertion anchors for the separate query; existing current-generation admission/cleanup methods retain their semantics.
- Create/Test: `tests/process/test_relay_termination.py` with `test_relay_receipt_vocabulary`, `test_relay_termination_proof`, `test_relay_termination_requires_exit`, `test_relay_termination_clock_domains` exactly once each.
- Existing procfs and Linux runner are read-only consumers; no new signaling/process port is needed.

### Steps

- [ ] Implement test-local `relay_receipts` using the existing registry harness's injected repositories/process inspection. Supply a full valid original record, unique matching historical receipt and optional cleanup intent, with no physical runtime/socket requirement. Cross stopped and failed+completed-stopped terminal records independently with live/unreadable exit evidence; provide pid-absent and pidfd-exited controls. Add a newer receipt at the same runtime name without invalidating the original identity. Run the named tests RED.
- [ ] Add the async query under the registry's existing observation authority and lock discipline. Load all receipts/intents through repositories and propagate their typed failures. Select only `daemon_instance_uuid == record.instance_uuid`, require exactly one join, non-None matching recorded daemon, same runtime name and composed socket path. Reject wrong/ambiguous UUID/process/runtime with stale_event. This query calls neither ensure/restart/reconcile/release nor `_receipt_for`, `_current_generation`, `_runtime_matches` or filesystem/socket admission that depends on the old live inode.
- [ ] Accept `stopped`, or `failed` joined by launch_uuid to exactly one intent with outcome `stopped` and a non-None `completed_at >= intent_at`. A failed receipt alone, failed-to-start refusal, error outcome or incomplete intent returns cleanup_timeout. The query's terminal predicates use only the actual receipt vocabulary.
- [ ] Bind the receipt's ProcessIdentity to the original record before absence observation. Use authoritative `IdentityState.absent` from the injected exact classifier for pid_absent. For pidfd_exited, read matching live pid/start_ticks, open pidfd, revalidate that same identity, then observe exit on that descriptor; close every opened descriptor on all paths. An unreadable identity, different start_ticks, missing observation, cached alive=False, missing socket or completed task is not exit evidence. A still-live or unreadable exact process returns cleanup_timeout. The query never signals anything.
- [ ] Construct proof using monotonic query observations and the exact persisted cleanup value:

```python
proof = TerminationProof(
    daemon_instance_uuid=record.instance_uuid,
    daemon=record.daemon,
    runtime_directory=record.runtime_directory,
    launch_uuid=receipt.launch_uuid,
    receipt_state=receipt.state,
    cleanup_outcome=intent.outcome if intent is not None else None,
    cleanup_completed_at=intent.completed_at if intent is not None else None,
    process_evidence=process_evidence,
    identity_verified_at=identity_verified_at,
    observed_at=self._clock.now(),
)
```

The stopped branch has no cleanup intent requirement; the otherwise-valid failed branch has one. No wall value is compared to the last two fields. The clock test uses intent_at=1_800_000_000.0, completed_at=1_800_000_001.0, identity_verified_at=10.0 and observed_at=11.0 and asserts exact preservation.

- [ ] `test_relay_termination_requires_exit` uses one top-level parametrized test with four independent negative node cases: stopped/live, stopped/unreadable, failed-completed/live, failed-completed/unreadable. Every identity/join/terminal/time predicate passes. Assert cleanup_timeout and no proof for each; absent/exact-pidfd controls succeed. Task 8 will add active/retired transport consumption assertions in transport tests, and Task 12 will run each negative node under the exit-check-deletion mutation; there is no dependency on an unfinished transport implementation here.
- [ ] `test_relay_termination_proof` proves historical lookup after replacement, refusal of live failed-to-start, wrong process/runtime/instance and no registry mutation or process-control call. `test_relay_receipt_vocabulary` independently covers no/incomplete/error cleanup intents. Run validation and commit owned paths.

## Task 4: Enforce relay-only preflight, final reservation and typed dispatch classification

**Implementer tier:** Advanced

**Lane:** backend
**Depends on:** 2
**Owns:** src/airplay_linux/backend/control.py, src/airplay_linux/process/ownership.py, tests/backend/test_credential_relay.py
**Exclusive resources:** pytest-relay-backend
**Interfaces produced:** `DispatchStarted = Callable[[], Result[None]]`; `DispatchStatus = Callable[[StatusReply], Result[None]]`; `requires_dispatch_fence(operation: BackendOperation, *, relay_submission: bool = False) -> bool`; `PublishedDoubletakeControl.request(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None, *, dispatch_fence: DispatchFence | None = None, ownership_proof: OwnershipProof | None = None, dispatch_started: DispatchStarted | None = None, dispatch_status: DispatchStatus | None = None) -> Result[BackendReply]` as an async method; BackendControl/UnsupportedControl.request have the same optional keywords and protocol operation: BackendOperation; `_credential_rejection(error: object, target: ReceiverTarget) -> CredentialDispatchFailure`; unchanged public authority `EvidenceAuthority.authorize(self, record: DaemonRecord, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Result[OwnershipProof]` as async, with profile-dependent requirement.
**Interfaces consumed:** Task 2 `ProfileCommand(argv: tuple[str, ...], secret_indices: tuple[int, ...] = ())`; async `_run(self, command: ProfileCommand, *, operation: BackendOperation) -> Result[CommandOutput]`; `CredentialDispatchFailure(*, error: Diagnostic, dispatch_state: Literal["no_spawn", "acknowledged", "uncertain"], refusal: Literal["shutdown", "ended", "stale", "rejected"] | None = None)`; existing `DispatchFence = Callable[[], Result[None]]`, `OwnershipAuthority.authorize/confirm/prove`, and `ProcessPort.run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Result[CommandOutput]` as async.
**Validation:** python -m pytest -q tests/backend/test_credential_relay.py tests/backend/test_control.py tests/process/test_ownership.py --basetemp=/tmp/ap-relay-backend
**Commit message:** feat(backend): fence relay dispatch and preserve acquisition evidence

**Files:**

- Modify: `src/airplay_linux/backend/control.py:188-223`, `:299-317`, `:587-610`, `:722-763`, `:878-1034`, `:1189-1208`, `:1233-1240` and exports.
- Modify: `src/airplay_linux/process/ownership.py:422-442`.
- Extend/Test: `tests/backend/test_credential_relay.py` with `test_relay_fencing`, `test_relay_daemon_error_mapping`, `test_relay_transactional_compatibility`, extending the existing command/authorization/metadata definitions rather than duplicating them.

### Steps

- [ ] Add RED probes for every missing/dropped relay callback/fence, a vanished wait during awaited preflight, read-only repeated fence evaluation, atomic reservation, fixed refusals and the transactional no-callback control. Use deterministic Deferred/Event barriers, never elapsed sleeps.
- [ ] Preserve the unconditional set and implement the exact helper:

```python
def requires_dispatch_fence(
    operation: BackendOperation, *, relay_submission: bool = False,
) -> bool:
    return operation in FENCE_REQUIRED_OPERATIONS or (
        operation is BackendOperation.submit_pairing and relay_submission
    )
```

Internally classify relay_submission using the actual submit operation, recognized public profile name and proved credential_relay. A caller cannot select the classification via payload/fence presence. EvidenceAuthority applies the same classification; HandshakeAuthority uses the default False. Missing relay fence or either callback returns sanitized no_spawn/stale_target before any I/O. Existing other-operation codes and all supplied-fence checks remain intact.

- [ ] After local command/metadata/callback validation, perform the exact awaited sequence: ownership authorization; owned `request(status, None, EmptyPayload(), None)` without callbacks and its ownership confirmation; `dispatch_status(decoded)`; revalidation of mutation proof; read-only fence; `dispatch_started()` exactly once; _run. Malformed preflight status returns retryable evidence to transport without creating/charging a dispatch; transport Task 9 repeats under its active phase budget. Callback Err prevents runner entry. No await occurs between final fence, reservation and runner entry; an authority's earlier fence calls never reserve.
- [ ] The final boundary is synchronous until _run enters its runner:

```python
checked = dispatch_fence()
if isinstance(checked, Err):
    return CredentialDispatchFailure(
        error=checked.error, dispatch_state="no_spawn",
    )
reserved = dispatch_started()
if isinstance(reserved, Err):
    return CredentialDispatchFailure(
        error=reserved.error, dispatch_state="no_spawn",
    )
process_result = await self._run(command, operation=op)
```

Implement this only in the validated relay branch; ordinary mutations keep their delivered flow. The release-disconnect case invokes its supplied dispatch_started after its ordinary final fence, with no dispatch_status requirement.

- [ ] For relay submit and release-disconnect with dispatch_started, _run calls the existing runner timeout directly without an extra cancelling wait_for. A certified runner pre-exec dependency_missing is no_spawn; a pre-I/O adapter refusal is no_spawn. Arbitrary exceptions/cancelled waiters/other Err/no envelope cannot certify prevention of a child: classify uncertain. Decode a valid acknowledgement before post-command ownership confirmation; a later ownership failure suppresses public success but preserves acknowledged evidence. Valid ok=false at exit 0 or 1 is acknowledged. Transactional and unrelated operation reduction retain existing behavior.
- [ ] Classify exact errors in the adapter: shutdown → daemon_failed/shutdown; no waiting → stale_target/ended; multiple waiting → stale_target/stale; exact not-ready prefix plus this target → stale_target/stale; everything else, including wrong-target suffix/non-string → daemon_failed/rejected. Error strings disappear after typed classification. Never parse them in transport or put stdout/stderr in diagnostics.
- [ ] Complete `test_relay_command` waiting and vanished-after-final-check variants with this adapter and a deterministic public ctl double. The latter creates only the named target with original options; disappearance during the awaited status refuses before child entry. `test_relay_fencing` asserts zero total I/O for each missing input and zero credential children for late wait loss. `test_relay_transactional_compatibility` proves the original coordinator/token/payload path succeeds without all three new inputs and public flags remain false. Run validation including the untouched requirement truth table; commit owned paths.

## Task 5: Project secrets before observation and provide private relay fixtures

**Implementer tier:** Advanced

**Lane:** core
**Depends on:** 2, 3
**Owns:** src/airplay_linux/testing/adapters.py, tests/relay_support.py, tests/fixtures/processes/fake_doubletake.py, tests/acceptance/test_no_production_spawn.py, tests/testing/test_fakes.py
**Exclusive resources:** pytest-relay-core
**Interfaces produced:** `redact(value: Any) -> Any`; async `FakeProcess.run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Result[CommandOutput]` with projected retained runs; `ProbeProcessRecorder.observe_invocation(self, method: str, spec: CommandSpec) -> None` with projected retained invocations; test support `RelayGate.entered: asyncio.Event`, `RelayGate.result: Deferred[object]`, `RelayGate.wait(self) -> object` as async and `RelayGate.release(self, value: object = None) -> None`; `RelayDaemon(FakeProcess)` with async `run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Result[CommandOutput]`, `set_entry(self, target: str, *, state: str, credential_kind: CredentialKind | None = None, prepared: bool = False) -> None`, `remove_entry(self, target: str) -> None`, `gate(self, name: str) -> RelayGate`, `credential_children: list[tuple[str, tuple[str, ...]]]` (redacted argv), `disconnect_children: list[str]`, `streams: dict[str, BackendStream]`, `journal: Journal`; fixtures `relay_daemon` returning RelayDaemon, `relay_dispatch_barriers` returning `dict[str, RelayGate]`, `relay_store_spy` returning FakeCredentialStore whose forbidden transactions fail immediately, `relay_clock` returning FakeClock and `relay_scheduler` returning FakeScheduler. These fixtures are explicitly imported by consumers.
**Interfaces consumed:** Task 1 `CommandSpec(*, argv: tuple[str, ...], env: Mapping[str, str], cwd: str | None = None, pass_fds: tuple[int, ...] = (), secret_indices: tuple[int, ...] = ())`, `CommandSpec.redacted_argv -> tuple[str, ...]`; Task 2 `ProfileCommand(argv: tuple[str, ...], secret_indices: tuple[int, ...] = ())`, `ProfileCommand.redacted_argv -> tuple[str, ...]`; existing `Deferred.wait(self) -> T` as async and `Deferred.resolve(self, value: T) -> None`, `Journal.record(self, name: str, **arguments: Any) -> None`, `FakeClock.now(self) -> float`, `FakeScheduler.advance(self, seconds: float) -> None`.
**Validation:** python -m pytest -q tests/testing/test_fakes.py tests/acceptance/test_no_production_spawn.py tests/adapter_contracts/test_private_processes.py --basetemp=/tmp/ap-relay-core
**Commit message:** fix(testing): redact credential commands before retaining observations

**Files:**

- Modify: `src/airplay_linux/testing/adapters.py:155-172`, `:1074-1111`, `:1231-1264`.
- Modify: `tests/acceptance/test_no_production_spawn.py:40-62` only for ProcessRecorder projection, preserving classification/tests.
- Modify: `tests/fixtures/processes/fake_doubletake.py:714-844` for credential-aware projection and safe optional captures; existing private protocol identity is unchanged.
- Create: `tests/relay_support.py` for reusable deterministic fixture boundaries.
- Modify/Test: `tests/testing/test_fakes.py`, adding projection and fixture-behavior controls.

### Steps

- [ ] Write RED nested-command projection and retained-run tests. A safe repr alone cannot satisfy them: assert declared argv fields after Journal.entries/calls, FakeProcess.runs/spawned, ProbeProcessRecorder.invocations/outer_unshare and acceptance ProcessRecorder._record. Preserve env/cwd/pass_fds and metadata in sanitized CommandSpec copies. No test failure message interpolates a raw secret command.
- [ ] Special-case command dataclasses before generic dataclass recursion:

```python
if isinstance(value, (ProfileCommand, ports.CommandSpec)):
    return {
        item.name: redact(
            value.redacted_argv if item.name == "argv"
            else getattr(value, item.name)
        )
        for item in fields(value)
    }
```

Retained fake command collections contain `replace(spec, argv=spec.redacted_argv)`, not the original spec. Operation classification may inspect original argv only during the invocation, then retain its redacted projection. `_adopt_identity` and recorder subclasses also use the projected spec; keep existing owner env matching and nonsecret behavior.

- [ ] Add RelayGate using the existing Deferred, with an entered event before waiting and release resolving the Deferred once. Define named gates `ownership`, `status`, `final_fence`, `child_created`, `reply`, `post_reply_status`, `source`, `disconnect_ack`, `release_fenced`; consumers install a gate only at the named boundary. Event/state barriers establish ordering; no 5/20/120/300 s real sleeps.
- [ ] Implement the private fake ctl/daemon behavior needed by specification §16.1: at least A/B, per-target waits, known kind changes, aggregate hints independent of entry identity, connecting before prepared source, original and replacement UUIDs, targeted credential resume or absent-target creation, original options, and successful disconnect-A with aggregate streaming while B survives. RelayDaemon.run uses actual command shapes and records redacted argv/target/options/command identity only. Its `set_entry` creates BackendStream with valid source metadata when prepared=True; `remove_entry` affects only that target. It scripts decoded failures, missing envelope, delayed C1 execution and acknowledged reply before held status. It never advertises the application-contract fixture as a production backend.
- [ ] In fake_doubletake.py, parse the credential position only for its recognized targeted connect grammar; project it before `_append`, ctl-argv-env capture and request capture. Credential transport is transient within the private invocation, while journals/captures retain only input-present plus target/options/request ID. The older private wire schema and noncredential fixture behavior stay compatible. No fixture saves a credential file or writes a credential payload field to optional capture.
- [ ] Supply the failing transaction spy for prepare_pairing, commit_pairing, cancel_pairing, mark_invalid and revoke_pairing; actor tests also spy on coordinator forget_trust/retry_revocation and transactional routes. The public relay routing cancel remains allowed. Add controls proving the spy trips and fixture behavior is target-local.
- [ ] Run validation; consumers in later tasks run their exact named metadata/canary/acceptance identifiers against these observers. Commit only owned paths. Tests/conftest.py remains unchanged so Task 13's guard-deletion probes test the existing real tripwire.

## Task 6: Publish owned relay challenges and deduplicate actor submission completions

**Implementer tier:** Advanced

**Lane:** dashboard
**Depends on:** 1
**Owns:** src/airplay_linux/session/engine.py, src/airplay_linux/session/pairing.py, src/airplay_linux/session/virtual.py, tests/session/test_credential_relay.py
**Exclusive resources:** pytest-relay-dashboard
**Interfaces produced:** `PairingFlow.observe_relay(self, ctx: SessionContext, event: TransportEvent) -> bool`; `PairingFlow._submit(self, ctx: SessionContext, message: SubmitPairing) -> None`; async `PairingFlow._submit_result(self, message: SubmitPairing, context: OperationContext, *, relay: RelayLeaseView | None = None, submission_id: OperationId | None = None) -> Result[object]`; `PairingFlow._submit_effect(self, ctx: SessionContext, message: EffectEvent) -> bool`; unchanged admission signature `_admit(self, context: SessionContext, message: Command | ResourceEvent, route: Route) -> tuple[Route, Command | ResourceEvent] | None`; annotated existing bridge async `_with_backend_events(self, ctx: SessionContext, operation: OperationContext, work: Callable[[], Awaitable[Result[Any]]], *, acquisition: bool = False) -> Result[Any]` and `_effect(self, ctx: SessionContext, message: EffectEvent) -> bool`.
**Interfaces consumed:** Task 1 `can_submit(lease: RelayLeaseView) -> bool`, `relay_credential_representable(value: str, *, target_volume_proven: bool) -> bool`, `RelayPairingPayload(*, lease: RelayLeaseView)`, `RelaySubmitCompletion(*, submission_id: OperationId, challenge_id: ChallengeId, receiver_id: ReceiverId, endpoint_generation: int, session_generation: int, owner_lease: LeaseId, daemon_instance_uuid: InstanceId, generation: int, result: PairingResult)`; unchanged PairingPort async `submit_pairing(self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult`; existing `IdFactory.new(self) -> UUID`, `SessionContext.outstanding_acquisitions(self, *, names: frozenset[str] | None = None) -> int`, and `SessionManager.pending_resources(self, receiver_id: ReceiverId) -> tuple[StreamHandle | DisplayHandle | PhysicalMonitorSource, ...]`.
**Validation:** env QT_QPA_PLATFORM=offscreen python -m pytest -q tests/session/test_credential_relay.py tests/session/test_pairing_integration.py tests/session/test_actor_fences.py --basetemp=/tmp/ap-relay-dashboard
**Commit message:** feat(session): own relay challenges and claim submission completions once

**Files:**

- Modify: `src/airplay_linux/session/engine.py:522-567` for the shared eligibility admission.
- Modify: `src/airplay_linux/session/pairing.py:12-46`, `:229-266`, `:384-499` and a receiver-context pairing runtime holding the new secret-free collections/counter.
- Modify: `src/airplay_linux/session/virtual.py:465-499`, `:703-751` to route pending mirror RelayPairingPayload before transactional pairing logic, preserving existing ready-event validation.
- Create/Test: `tests/session/test_credential_relay.py` with `test_relay_submission_completion_deduplication`, `test_relay_option_shaped_password`, `test_relay_stale_completion` exactly once each.

### Steps

- [ ] Build a test-local actor rig using existing SessionRig and lifecycle handler composition, with its fake pairing/transport ports; this task does not wait for a receiver or for the new production transport. Inject fully fenced RelayPairingPayload events through the pending-start bridge and hold the original start acquisition. Write RED tests for wrong owner/endpoint/session/trust/daemon/start/mode, duplicate S1 then distinct identical S2, old-generation completion and extended-CLI password rejection.
- [ ] Observe only a MIRROR pending start or reconnect with active operator intent, exact online target/fingerprint, start operation, endpoint/session/trust, lease and daemon. Do not set snapshot.stream just to satisfy generic accept_transport. Preserve its strict behavior for raw stream-less events. Keep the bridge subscription through acquisition delivery; route relay eligibility and retirement updates for that exact owner through observe_relay. Virtual payloads cannot gain relay authority.
- [ ] Observe_relay sets Pairing trust, public relay/challenge and typed notice without begin_pairing, a new start, operation/session/lease change, trust-generation invalidation or an actor expiry timer. The transport's phase controller is the timer owner. A new challenge increments the receiver-context `_relay_presentation_revision`; later updates preserve the actor-assigned revision in both snapshot.challenge and snapshot.relay.challenge, regardless of incoming revision 0. The counter survives cleanup and challenge/lease replacement.
- [ ] After existing challenge/full-context admission, call the imported can_submit on the matching snapshot.relay. False publishes a fixed stale_target diagnostic and returns None from _admit, with no pairing invocation, new submission ID or consumed attempt. The existing CommandReceipt remains queue admission; no signature change or fabricated result type is needed. The transport still independently rechecks current state.
- [ ] On a true actor admission, allocate OperationId(ids.new()) before any input validation and synchronously register its immutable identity in `_relay_pending_submissions`. Keep `_relay_completed_submission_ids` for this receiver context's lifetime, across every cleanup/replacement; no secret/argv is stored there. A relay result wrapper uses only that captured identity:

```python
completion = RelaySubmitCompletion(
    submission_id=submission_id,
    challenge_id=message.challenge_id,
    receiver_id=relay.receiver_id,
    endpoint_generation=relay.endpoint_generation,
    session_generation=relay.session_generation,
    owner_lease=relay.owner_lease,
    daemon_instance_uuid=relay.daemon_instance_uuid,
    generation=relay.generation,
    result=result,
)
return Ok(value=completion)
```

For the existing transactional path both optional keywords remain None and its PairingResult handling stays unchanged. Never recover relay identity from the current challenge after awaiting.

- [ ] Validate exact SensitiveText, nonempty text, four ASCII digits for PIN, nonempty password and CLI representability with the captured request.start_options.target_volume_proven. A local InvalidInput completion uses the same ID, waiting=True, invalid_input and credential_ref=None, without transport dispatch/attempt charge. Reject -secret, --audio, --volume and -- for the extended parser even when original option tokens were absent; legacy/transactional password syntax remains unchanged. Do not rewrite, quote or prepend a delimiter.
- [ ] Claim a completion before reduction: match pending ID and full captured tuple; remove the pending entry and add the completed tombstone atomically. Unknown/mismatched IDs are stale_event; an already completed ID performs no mutation. Check the current owner/generation only after claiming, so a retired completion still leaves its tombstone but cannot change replacement trust/revision/counter. It may settle only its original transport cleanup observer. Provisional Trusted preserves the live challenge and Pairing and adds no revision; every other current completion increments once. Transport views already own consumption/dispatch state, so actor reduction must not double-charge them.
- [ ] In the deduplication test, hold S1's exact completion, deliver it twice, then admit S2 with the same invalid text/result/current generation but a fresh ID. Assert revision deltas 1/0/1, zero transport calls/charges and S1's tombstone across later cleanup/new challenge. `test_relay_stale_completion` parametrizes kind/generation, endpoint, session, lease and daemon changes and asserts unchanged replacement revision/trust. Task 11 extends these same definitions with cleanup and real-lifecycle controls, without adding duplicate functions.
- [ ] Run validation and confirm transactional integration tests remain green; commit owned paths. The later lifecycle task installs the remaining terminal/release routes and start-handle trust commit.

## Task 7: Implement the transport-only relay lease and single phase controller

**Implementer tier:** Advanced

**Lane:** backend
**Depends on:** 4, 5
**Owns:** src/airplay_linux/transport/relay.py, tests/transport/test_credential_relay.py
**Exclusive resources:** pytest-relay-backend
**Interfaces produced:** mutable `CredentialDispatch(*, command_id: OperationId, generation: int, attempt: int, admitted_at: float, state: DispatchState, request_task: asyncio.Task[Result[BackendReply]] | None, reply_decoded: bool, status_resolved: bool, charged: bool, spawn_admitted: bool = False, pre_spawn_retired: bool = False)`; mutable `RelayLease` with exactly RelayLeaseView's field inventory plus dispatch, unresolved_dispatch_ids, phase_controller, release_task, release_attempt, disconnect_task and disconnect_outstanding as specified in §4; `RelayPhaseController.__init__(self, *, clock: Clock, scheduler: Scheduler, expire: Callable[[int, ErrorCode], None]) -> None`; `connect_acknowledged(self, observed_at: float) -> None`; `request_live(self, request: CredentialRequest) -> None`; `observe_progress(self, *, generation: int, observed_at: float, waiting: bool, prepared: bool, streaming: bool) -> None`; `status_decoded(self, valid: bool) -> None`; `deadline(self) -> float | None`; `check(self, now: float) -> ErrorCode | None`; `_expire(self, generation: int, deadline: float) -> None`; `close(self) -> None` on RelayPhaseController.
**Interfaces consumed:** Task 1 CredentialRequest/RelayLeaseView and exact RelayPhase/DispatchState aliases from §4; `ReleaseEvidence` and `TerminationProof` remain frozen input records; existing `Clock.now(self) -> float`, `Scheduler.call_at(self, when: float, callback: Callable[[], None]) -> CancelHandle`, `CancelHandle.cancel(self) -> None`; Task 5 fixtures relay_clock: FakeClock and relay_scheduler: FakeScheduler with `advance(self, seconds: float) -> None`.
**Validation:** python -m pytest -q tests/transport/test_credential_relay.py::test_relay_phases --basetemp=/tmp/ap-relay-backend
**Commit message:** feat(transport): centralize relay phase deadlines and lease state

**Files:**

- Create: `src/airplay_linux/transport/relay.py`.
- Create/Test: `tests/transport/test_credential_relay.py`, initially `test_relay_phases` only; later transport tasks extend this file in the same lane.

### Steps

- [ ] Write the one named parametrized phase test using explicit fake-clock observations immediately before, at and after each budget, retained outcomes, known-kind replacement, stale and early callbacks and malformed-status overrides. Include >20 s in credential without handshake expiry, repeated streaming without rearm, connecting without prepared source, and final source acceptance exactly at its deadline losing to expiry. Run it RED.
- [ ] Define CredentialDispatch with repr=False and no secret/payload/argv field. Define RelayLease with exactly §4's mutable inventory and initial values, including request_state=Stale, generation=0, empty unresolved ID set, all task references None, release_attempt=0, submit_fenced=False, disconnect_outstanding=False. Per-instance default factories prevent sharing sets/tasks. Public views never expose these tasks/sets or become a second state authority.
- [ ] Implement the complete controller state inventory in specification §7. A phase transition cancels the previous callback, increments controller.generation and schedules only `(generation, active_deadline)`; repeated observations of an unchanged phase/request preserve its deadline/callback. No closure captures input. Deadline arithmetic uses the injected Clock only.
- [ ] On connect acknowledgement enter permission with observed_at+300.0. Request_live enters credential at request.observed_at+120.0 and saves suspension time; same generation does nothing. A new known kind retires the old callback and shifts saved permission/handshake deadlines by the elapsed old credential interval before beginning the next one. Retained Waiting/Rejected/no_spawn/InvalidInput never resume those clocks or rearm expiry. Resume on valid evidence of request progress, not submission completion:

```python
elapsed = observed_at - self.suspended_at
if self.permission_deadline is not None:
    self.permission_deadline += elapsed
if self.handshake_deadline is not None:
    self.handshake_deadline += elapsed
self.suspended_at = None
```

The block runs only while suspended, after proving progress. First prepared source begins 20.0 s handshake; connecting alone resumes remaining permission. First streaming begins 5.0 s source; subsequent streaming cannot restart it.

- [ ] Expiry checks both generation and deadline identity before time. Matching callback before the deadline does nothing; matching callback at/after it closes the phase once. Every observer calls check(now) before accepting progress/results. Credential expiry gives pairing_expired; permission/handshake/source give unsupported_source; unresolved malformed status overrides with transport_failed. `status_decoded(valid)` only changes the malformed flag. close retires the generation/callback and clears active deadline.
- [ ] Include this arithmetic control within test_relay_phases, using a valid CredentialRequest fixture with observed_at=10.0:

```python
controller.connect_acknowledged(0.0)
controller.request_live(request)
assert controller.deadline() == 130.0
controller.observe_progress(
    generation=request.generation, observed_at=40.0,
    waiting=False, prepared=False, streaming=False,
)
assert controller.deadline() == 330.0
controller.observe_progress(
    generation=request.generation, observed_at=41.0,
    waiting=False, prepared=True, streaming=False,
)
assert controller.deadline() == 61.0
```

The fixture clock moves to each observation before the call; a separate parametrized case tests the exact boundary, not this arithmetic control. Retain a stale callback object explicitly and call it after a new generation to prove harmlessness.

- [ ] Run validation, check there is never more than one active callback and no coordinator scheduler dependency, then commit owned paths. Production readiness wiring follows in Task 9, using these same methods and no duplicate clock owner.

## Task 8: Implement one fenced, shared and retryable relay release transition

**Implementer tier:** Capable

**Lane:** backend
**Depends on:** 3, 7
**Owns:** src/airplay_linux/transport/doubletake.py, tests/transport/test_credential_relay.py
**Exclusive resources:** pytest-relay-backend
**Interfaces produced:** `_Stream(..., source: CaptureSource | None, relay: RelayLease | None = None)` preserving existing fields; `DoubleTakeTransport._relay_view(self, stream: _Stream) -> RelayLeaseView`; async `release(self, lease: LeaseId, evidence: ReleaseEvidence) -> Result[None]`; async `_release_attempt(self, stream: _Stream, evidence: ReleaseEvidence) -> Result[None]`; `_reserve_release_dispatch(self, stream: _Stream, attempt: int) -> Result[None]`; async `_request(self, daemon: _Daemon, operation: BackendOperation, target: ReceiverTarget, payload: BackendPayload, context: OperationContext, *, cleanup: bool = False, dispatch_started: DispatchStarted | None = None, dispatch_status: DispatchStatus | None = None) -> Result[BackendReply]`; relay branches in existing async `stop_stream(self, handle: StreamHandle, context: OperationContext) -> Result[None]`, `_stop_owned(self, stream: _Stream, context: OperationContext) -> Result[None]`, `_shutdown(self) -> Result[None]`.
**Interfaces consumed:** Task 3 async `DaemonRegistry.termination_proof(self, record: DaemonRecord) -> Result[TerminationProof]`; Task 7 RelayLease/CredentialDispatch inventory and `RelayPhaseController.check(self, now: float) -> ErrorCode | None`, `close(self) -> None`; Task 1 `ReleaseEvidence(*, reason: ErrorCode | None, observed_at: float, disconnect_ack: AckReply | None = None, status: StatusReply | None = None, daemon_instance_uuid: InstanceId | None = None, endpoint_generation: int | None = None, termination: TerminationProof | None = None)` and `can_submit(lease: RelayLeaseView) -> bool`; Task 4 async `BackendControl.request(self, operation: BackendOperation, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None, *, dispatch_fence: DispatchFence | None = None, ownership_proof: OwnershipProof | None = None, dispatch_started: DispatchStarted | None = None, dispatch_status: DispatchStatus | None = None) -> Result[BackendReply]`, `CredentialDispatchFailure` classification; Task 5 RelayGate entered/result/wait/release, RelayDaemon gate/set_entry/remove_entry, redacted credential_children and target-only disconnect_children.
**Validation:** python -m pytest -q tests/transport/test_credential_relay.py tests/transport/test_doubletake.py --basetemp=/tmp/ap-relay-backend
**Commit message:** feat(transport): serialize relay settlement and confirmed lease release

**Files:**

- Modify: `src/airplay_linux/transport/doubletake.py:215-240`, `:461-484`, `:614-663`, `:762-770`, `:1095-1130`, `:1444-1478`, adding the named release/view/reservation methods beside existing ownership helpers.
- Extend/Test: `tests/transport/test_credential_relay.py` with `test_relay_release`, `test_relay_retained_retry`, `test_relay_release_while_other_receiver_streams`, `test_relay_release_vs_submit_interleaving` exactly once each. The last test's actual-submit variants are completed in Task 9; its direct predicate/release-reservation variants execute here.

### Steps

- [ ] Seed exact RelayLease state on owned transport streams in the test harness, without enabling new relay starts yet. Add RED cases for initial-connect barrier, uncertain/acknowledged credential barrier, one disconnect, joined callers, retained retries, late acknowledgement, target absence freshness, original termination proof, replacement-only absence and wrong proof. Assert state and child counts while each gate is held, before final completion.
- [ ] _relay_view first checks controller time and retires elapsed eligibility, then produces a frozen field copy. It never returns transport tasks/unresolved sets. Add relay state to _Stream with source accepting the existing desktop None. The _request relay branches forward original fences/callbacks and preserve CredentialDispatchFailure instead of reducing it through `_safe` to a generic Err. A wrapper unable to accept either new keyword is refused before invocation; read-only/transactional wrappers retain compatibility.
- [ ] In release, set submit_fenced synchronously and publish the fenced view before its first await or scheduling gap; install one shared release_task and shield it from individual callers. A settling caller joins the same task. Retained with no disconnect outstanding admits a new attempt on the same lease; an outstanding disconnect is observed/joined without a new child. Repeated released handle returns Ok without I/O or a second completion record. Wrong-instance proof gives stale_event and leaves the obligation/fence intact.
- [ ] Under one deadline `clock.now()+CONTROL_TIMEOUT`, join the original connect acquisition and credential barrier. Joining stream.lock covers initial connect only after Task 9's lock-scope adjustment; lock acquisition consumes the same allowance and cannot restart it. No submission/readiness wait owns a lock needed by release. Unsettled acquisition/dispatch at the bound means retained/cleanup_timeout with zero disconnects. Task completion or target absence never settles uncertain dispatch.
- [ ] Once settled, accept exact successful targeted disconnect acknowledgement regardless of aggregate state; otherwise accept a fresh absence read begun after settlement on the original authenticated daemon/endpoint, or exact-instance termination proof. A stale absence from a retained attempt must be reread; replacement absence cannot discharge an old lease. An already valid absence/proof emits zero children. With no discharge evidence and safe original target addressing, dispatch one disconnect using dispatch_started and no dispatch_status. The release reservation is synchronous and checks same attempt, settling state, submit_fenced, settled credential barrier and no outstanding disconnect before setting the flag.
- [ ] Retain/shield the exact disconnect task/observer. Decoded success releases once. Decoded refusal or certified no_spawn clears outstanding but retains lease/fence for later call. A waiter timeout, task completion without decoded reply or RetryCleanup cannot clear outstanding. Exact late acknowledgement or fresh valid absence/termination may discharge it. A later exact proof settles credential uncertainty and directly releases both active and retired obligations without requiring a replacement disconnect.
- [ ] Route relay `_stop_owned`, stop_stream including _retired handles, abandonment/failed-start cleanup and shutdown through release. Daemon notices can move an old stream to _retired, but cannot drop it. Registry shutdown success alone cannot bulk-drop relay entries; iterate original records, query proofs and consume each through release, publish stopped once, retain unresolved entries and return cleanup_timeout. Nonrelay paths retain existing behavior.
- [ ] In `test_relay_release_while_other_receiver_streams`, prepare A/B streaming, settle A's barrier, allow successful disconnect-A AckReply(state=streaming) and withhold all later status/proof fallbacks. Assert A removed/released once, B/its lease unchanged, exactly [A] disconnect_children. This independent control must fail if the old aggregate-state veto returns.
- [ ] In the release-vs-submit test, hold `release_fenced` after the flag is set and before disconnect, assert can_submit false; retain a separate pre-admitted candidate for Task 9's final fence control. Retained retry preserves the flag. In the retained-retry test an unacknowledged disconnect stays outstanding even when its client task is done; no second child appears. Run validation and commit owned paths.

## Task 9: Relay target-specific requests through dispatch settlement and original mirror readiness

**Implementer tier:** Capable

**Lane:** backend
**Depends on:** 8
**Owns:** src/airplay_linux/transport/doubletake.py, tests/transport/test_credential_relay.py, tests/backend/test_credential_relay.py
**Exclusive resources:** pytest-relay-backend
**Interfaces produced:** `DoubleTakeTransport._observe_credential_request(self, stream: _Stream, status: StatusReply, observed_at: float) -> CredentialRequest | None`; `_relay_submit_fence(self, stream: _Stream, generation: int) -> DispatchFence`; `_reserve_relay_dispatch(self, stream: _Stream, candidate: CredentialDispatch) -> Result[None]`; `_relay_dispatch_status(self, stream: _Stream, generation: int, status: StatusReply) -> Result[None]`; async `_submit_relay(self, stream: _Stream, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult`; async `submit_pairing(self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult`; async `cancel_pairing(self, receiver_id: ReceiverId, challenge_id: ChallengeId | None, context: OperationContext) -> Result[None]`; async `_await_connecting_stream(self, stream: _Stream) -> Result[None]`, `_verify_reported_source(self, stream: _Stream) -> Result[None]`, `_claim_streaming_readiness(self, stream: _Stream) -> Err | None`, `_poll_reported_source(self, stream: _Stream, deadline: float) -> Result[None]`, `_start(self, stream: _Stream, connected: asyncio.Future) -> Result[StreamHandle]`; existing async `begin_pairing(self, target: ReceiverTarget, context: OperationContext) -> Result[PairingChallenge]` remains transactional.
**Interfaces consumed:** Task 1 `can_submit(lease: RelayLeaseView) -> bool`, `relay_credential_representable(value: str, *, target_volume_proven: bool) -> bool`, `RelayPairingPayload(*, lease: RelayLeaseView)`, PairingResult with existing literal kinds and `waiting: bool = False`; Task 2 `CredentialPayload(*, value: SensitiveText | str, audio: AudioPolicy | None = None, initial_volume: VolumeLevel | None = None)`; Task 4 DispatchFence/DispatchStarted/DispatchStatus and full BackendControl.request keywords as restated in Task 8; Task 7 `RelayPhaseController.connect_acknowledged(self, observed_at: float) -> None`, `request_live(self, request: CredentialRequest) -> None`, `observe_progress(self, *, generation: int, observed_at: float, waiting: bool, prepared: bool, streaming: bool) -> None`, `status_decoded(self, valid: bool) -> None`, `deadline(self) -> float | None`, `check(self, now: float) -> ErrorCode | None`, `close(self) -> None`; Task 8 async `release(self, lease: LeaseId, evidence: ReleaseEvidence) -> Result[None]`, `_relay_view(self, stream: _Stream) -> RelayLeaseView`; Task 5 RelayDaemon, RelayGate and redacted observers with their declared APIs.
**Validation:** python -m pytest -q tests/transport tests/backend/test_credential_relay.py tests/backend/test_control.py tests/trust/test_pairing_transactions.py --basetemp=/tmp/ap-relay-backend
**Commit message:** feat(transport): answer owned mirror credential requests through relay

**Files:**

- Modify: `src/airplay_linux/transport/doubletake.py:76-151`, `:614-663`, `:703-755`, `:774-1093`, `:1409-1443` and the new release/relay helpers from Tasks 7–8.
- Extend/Test: `tests/transport/test_credential_relay.py` with `test_relay_observation`, `test_relay_barrier_and_adoption`, `test_relay_uncertain_dispatch`, `test_relay_exclusivity`, `test_relay_exclusive_progress_requires_single_dispatch`, `test_relay_malformed_status`; complete the single existing `test_relay_release_vs_submit_interleaving` definition.
- Extend/Test: `tests/backend/test_credential_relay.py` with `test_relay_credential_metadata`, `test_relay_production_runner_credential_index`, completing existing command/fencing variants as needed without changing their identifiers.

### Steps

- [ ] Add the named RED tests using Task 5's real-shape public adapter fixture, original/replacement identity and gates. Keep all old fixture profiles relay-disabled. Only an admitted PhysicalMonitorSource/desktop None mirror start with the proved public relay authority gets a RelayLease; virtual output and discovery/idle Pair cannot allocate one. Snapshot start options once from validated connect; legacy options are both None.
- [ ] Release stream.lock after initial connect acquisition, before human/readiness waits. A pin_required acknowledgement/aggregate prompt merely schedules/readies status; it cannot identify a request or fail an unrelated start. Inspect exactly one same-target streams entry, known pin/password and pin_required to mint a request. Same-kind observations preserve generation/object/observed_at/counter/expiry; kind change retires old request as Stale, creates a fresh challenge/counter/expiry and retains unresolved dispatch IDs/fence. Duplicate target entries/unknown kinds are malformed status.
- [ ] Publish RelayPairingPayload on the existing pairing_required event with the pending handle/lease/start identity; challenge backend token is never fabricated. _relay_view checks phase time first. Every relay readiness path consults the one controller; remove local accepted/permission/handshake/source timer ownership only for relay. The ordinary nonrelay deadline argument/counters retain their behavior. Malformed status retries, paced at 0.1, until the active deadline; no local third-failure cutoff, attempt charge or extension. unresolved malformed expiry is transport_failed.
- [ ] Direct transport submission repeats exact SensitiveText/syntax/representation validation before a candidate or I/O. Capture the current request/owner tuple and generation, create a secret-free candidate without installing it on the lease, and launch/retain the complete control-request task. The preflight loop handles malformed evidence within the existing active phase deadline without charging. A cancelled/expired preflight candidate sets pre_spawn_retired synchronously, so later awaited authorization cannot spawn it or charge an attempt.
- [ ] _relay_dispatch_status accepts only the candidate generation and one same-kind current waiting entry; it updates request_waiting and returns stale_target otherwise. Its fence verifies all original identities, phase time, current waiting observation and the imported can_submit. _reserve_relay_dispatch refuses pre_spawn_retired first, rechecks the same predicate, then synchronously reads the current counter ordinal, sets spawn_admitted, submit_inflight, dispatch_state=uncertain, installs this dispatch and adds its command ID. It runs only once after the final read-only fence, before runner entry. Never call the eligibility fence again after reserving against this command's own barrier.
- [ ] Start the client CONTROL_TIMEOUT waiter at reservation. Shield/retain the full control task and its late observer independently of the returned client coroutine. A positive no_spawn settles and charges once if this was a live valid-input refusal; no child could exist. A decoded reply is acknowledged and still holds the barrier through one valid resolving status read; absence is Ended only after that settlement. Awaiter timeout after reply decoding remains acknowledged, with resolving reads continuing under phase time. A possibly spawned command without decoded reply remains uncertain; raw exception/task completion/absence cannot settle it or produce an ordinary receiver rejection.
- [ ] Exclusive progress settles uncertain dispatch only with all §8 guards: exact retained command ID/generation/owner/target/daemon; post-admission read; exactly one target entry progressed to connecting with no credential kind, prepared or streaming; and exact singleton unresolved IDs. The central independent guard is:

```python
if relay.unresolved_dispatch_ids != {command.command_id}:
    # Leave dispatch_state, submit_inflight and the lease obligation unchanged.
    return
```

Only the exact command's permitted settlement or original-instance proof removes that ID. Latch accepted progress; stale/regressed snapshots cannot re-enable input. Two unresolved IDs must retain uncertainty even with every other predicate true.

- [ ] Apply the exact §11 result/error table with credential_ref=None. Waiting and retained typed rejection/no_spawn consume once via charged, preserve Pairing/original expiry and use waiting=True. Client InvalidInput, provisional Trusted, malformed evidence and unresolved uncertainty do not charge. On third consumed outcome close/fence/fail the owner atomically before release, preserve that third code, and reject a fourth. Shutdown refusal is terminal on its first consumed attempt. Terminal stale/ended classifications close independently of the retained counter. Emit typed notices before delivering results.
- [ ] A submission observing streaming sets SuccessPending once, retires input, preserves the challenge and returns provisional Trusted. Continue the same start through source verification, with the controller's final source deadline checked after awaited verification. Immediately before successful handle return, private request becomes succeeded without publishing public Trusted; Task 11 owns the public commit. All start/cancel/expiry/failure/daemon-replacement exits use Task 8 release; capture/display remain the actor cleanup owner's resources. Keep old result observers for old obligations, never for new-generation trust.
- [ ] Complete deterministic controls: B prompt does not affect A; aggregate-only hint mints nothing; absence while submit_inflight keeps the start and targeted fall-through adopts only T with original options. Delayed C1 returns uncertain; C2 is not offered/admitted/spawned; Cancel retains with zero disconnects before allowing C1 execution. Separately inject IDs {C1,C2} while retaining C1 and deliver connecting/no-kind/no-source evidence; singleton control settles, two-ID case retains. Release-vs-submit independently tests actor/presentation predicate refusal and an already-admitted candidate arriving at final fence after release_fenced; the first probe cannot short-circuit the second. Record child counts at gates.
- [ ] Add real LinuxProcessRunner.run interception for every argv option variant. Intercept `_exec`, not _run: use a private deterministic subprocess double with scripted wait/stdout/stderr, harmless process identity/pidfd seams and no host program creation. Compare the raw credential at its declared index only inside that active boundary, retain a Boolean and redacted spec, and assert the Boolean later. Installed offline run tripwires remain active; use the explicitly marked test-private ctl shim path/fixture authorization rather than a second refusal wrapper. Every ProfileCommand/CommandSpec/Journal/FakeProcess/recorder/nested projection is redacted. No canary is included in an assertion message.
- [ ] Run validation, including old transactional pairing and source/audio regressions, then commit owned paths. No implementation test is allowed to pass by changing the truth-table gate or enabling virtual relay.

## Task 10: Project relay actions and serialize prompts by receiver revision

**Implementer tier:** Advanced

**Lane:** dashboard
**Depends on:** 6
**Owns:** src/airplay_linux/ui/state.py, src/airplay_linux/ui/presenter.py, tests/ui/test_credential_relay.py
**Exclusive resources:** pytest-relay-dashboard
**Interfaces produced:** unchanged `project(snapshot: SessionSnapshot, inputs: ViewInputs) -> PaneState`, `_decide(state: SessionSnapshot, inputs: ViewInputs, ownership: OwnershipRecord | None, orphans: tuple[RecoveryItem, ...]) -> _Decision`, `_can_start(state: SessionSnapshot, inputs: ViewInputs) -> bool`; new `_relay_notice(state: SessionSnapshot, title: str) -> str | None`; unchanged Presenter `_render_now(self) -> None`, `_open_pairing(self, receiver_id: ReceiverId, challenge: ChallengeView, pane: PaneState | None = None) -> None`, `_maybe_open_pairing(self, receiver_id: ReceiverId, pane: PaneState) -> None`, async `_pairing_prompt(self, receiver_id: ReceiverId, challenge: ChallengeView, *, actions: frozenset[Action], retained_output: str | None) -> None`.
**Interfaces consumed:** Task 1 `can_submit(lease: RelayLeaseView) -> bool`, `ChallengeView` with `presentation_revision: int = 0`, `SessionSnapshot.relay: RelayLeaseView | None` and `relay_notice: RelayNotice | None`; Task 6 actor projection uses the same assigned ChallengeView in snapshot.challenge and snapshot.relay.challenge and allocates monotonic revisions for this receiver; existing ManagerPort async `dispatch(self, command: Command) -> CommandReceipt`, `snapshot(self, receiver_id: ReceiverId) -> SessionSnapshot`, `subscribe(self, callback: Callable[[SessionSnapshot], None]) -> Subscription`; DialogFactory async `pairing(self, challenge: ChallengeView, *, actions: frozenset[Action], retained_output: str | None) -> PairingAnswer | SensitiveText | None` and `invalidate_pairing(self, challenge: ChallengeView) -> None`.
**Validation:** env QT_QPA_PLATFORM=offscreen python -m pytest -q tests/ui/test_credential_relay.py tests/ui/test_presenter.py tests/ui/test_state_projection.py tests/ui/test_dashboard_widgets.py --basetemp=/tmp/ap-relay-dashboard
**Commit message:** feat(ui): present owned relay prompts once per receiver revision

**Files:**

- Modify: `src/airplay_linux/ui/state.py:558-583`, `:859-876` and typed notice selection near project/_decide.
- Modify: `src/airplay_linux/ui/presenter.py:370-371`, `:718-763`, `:934-960`, `:1238-1339`, `:1526-1544`.
- Create/Test: `tests/ui/test_credential_relay.py` with `test_relay_presentation`, `test_relay_eligibility`, `test_relay_prompt_for_unselected_started_receiver`, `test_relay_prompt_replacement_wipes_input` exactly once each.
- Read-only: `src/airplay_linux/ui/dialogs.py` keeps its existing action-based construction and all secret-wiping exits; no new retry counter/store enters it.

### Steps

- [ ] Write RED projection and real presenter tests, reusing the existing PresenterRig/FakeDialogs patterns without importing their test functions. Feed exact actor-shaped views rather than reaching into transport. Parametrize waiting, inflight, uncertain, success_pending, prepared/streaming progress, fenced release, retained cleanup, idle and admitted stream states. Use delayed old-dialog completion and real QtDialogFactory/PairingDialog checks where secret wiping matters.
- [ ] Keep cleanup priority. A live relay challenge offers Cancel and adds Submit only after the imported can_submit; it offers no Pair/Retry/Forget/retry_revocation. Pending success/progress retires input with the exact waiting-start copy; uncertainty shows its exact typed notice and Cancel. Active streams retain Stop/audio controls. Start requires every public idle conjunct: inactive intent, Idle transport, None_ display, no stream/source/display handle/ownership/lease/challenge, idle intent stage, no cleanup or retained relay view, online and capability. The actor's Task 11 cleanup commit guarantees no hidden pending acquisitions/resources before Available; UI does not consult manager internals or invent new snapshot fields for those counts.
- [ ] Implement _relay_notice using only the §14 closed RelayNotice table and sanitized receiver title; distinguish daemon_stopping from generic daemon_failed through typed state. Preserve PIN/password-specific request/rejection wording. Terminal notices survive idle cleanup and clear on explicit Start/operator action or admitted success, as controlled by the actor; UI never parses raw errors.
- [ ] Replace challenge-equality deduplication for relay with the key `(receiver_id, challenge.challenge_id, challenge.presentation_revision)`. Each eligible key is consumed once for the presenter's lifetime; repeated snapshots/results and stale keys cannot reopen. Progress/release invalidates and wipes existing input without minting another key. Distinct actor-assigned InvalidInput revisions each open once even with identical outcome/counter.
- [ ] Serialize replacement per receiver: register the pending replacement key, invalidate/wipe old dialog, await its task completion, reread current pane/can_submit/key, then open the replacement. An old finally block removes pending state only if it still owns that exact key; shutdown disposes pending work without publishing commands. Different receivers remain independent. Re-read the captured receiver's pane/actions after an answer; ordinary dismissal leaves the challenge live and creates neither Cancel nor a revision.
- [ ] In _render_now, keep dashboard selection/rendering intact and additionally scan each receiver with an owned live relay challenge using its own _pane(receiver_id). Relay prompt opening has no selected-receiver guard; transactional prompts retain their existing selection rule. Start A → select B → request A opens A's correctly labelled prompt immediately, while B stays selected. Submit/Cancel use captured A, never selection.
- [ ] Implement these exact assertions in the unselected-receiver test at the held dialog boundary:

```python
assert presenter.selected_receiver == receiver_b
assert opened_challenges[-1].receiver_id == receiver_a
assert manager.snapshot(receiver_b).challenge is None
assert len(opened_challenges) == 1
```

Use the existing presenter's public selection accessor if available; otherwise the test reads its existing selected field without introducing a production accessor. `opened_challenges` is the controlled factory's local observation list of ChallengeView, with no input. After repeated snapshots assert the count unchanged; submitting/cancelling yields a command naming A only.

- [ ] `test_relay_eligibility` holds source verification after submission-first progress; assert Cancel only, zero second dialog and no second typed SubmitPairing reaching the manager/transport. `test_relay_prompt_replacement_wipes_input` delays old destruction and verifies pending-key ownership, every hide/reject/replacement/expiry/cancel/dismissal wipe and untaken-handoff disposal. Run validation and commit owned paths.

## Task 11: Commit trust on the original handle and join terminal relay cleanup

**Implementer tier:** Capable

**Lane:** dashboard
**Depends on:** 9, 10
**Owns:** src/airplay_linux/session/pairing.py, src/airplay_linux/session/mirror.py, src/airplay_linux/session/reconnect.py, src/airplay_linux/session/virtual.py, src/airplay_linux/session/cleanup.py, src/airplay_linux/session/phase.py, tests/session/test_credential_relay.py
**Exclusive resources:** pytest-relay-dashboard
**Interfaces produced:** `PairingFlow.complete_relay_start(self, ctx: SessionContext, handle: StreamHandle) -> bool`; `PairingFlow.fail_relay(self, ctx: SessionContext, error: Diagnostic) -> None`; relay branches in `discovery_changed(self, ctx: SessionContext) -> bool`, `expire(self, ctx: SessionContext) -> bool`, `mark_invalid_from_backend(self, ctx: SessionContext) -> bool`; MirrorLifecycle/ReconnectLifecycle `_effect(self, ctx: SessionContext, message: EffectEvent) -> bool` and `_transport_event(self, ctx: SessionContext, event: TransportEvent) -> bool`; ReconnectLifecycle `_connection_failed(self, ctx: SessionContext, error: Diagnostic) -> None`; OrderedCleanup `_stop(self, ctx: SessionContext, queued: StartRequest | None) -> None`, `_continue(self, ctx: SessionContext) -> None`; unchanged `derive_phase(state: SessionSnapshot) -> SessionPhase` with relay priority.
**Interfaces consumed:** Task 6 `observe_relay(self, ctx: SessionContext, event: TransportEvent) -> bool`, async `_submit_result(self, message: SubmitPairing, context: OperationContext, *, relay: RelayLeaseView | None = None, submission_id: OperationId | None = None) -> Result[object]`, `_submit_effect(self, ctx: SessionContext, message: EffectEvent) -> bool` with captured RelaySubmitCompletion; Task 9 async `submit_pairing(self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult`, `cancel_pairing(self, receiver_id: ReceiverId, challenge_id: ChallengeId | None, context: OperationContext) -> Result[None]`, `stop_stream(self, handle: StreamHandle, context: OperationContext) -> Result[None]`; Task 8 async `release(self, lease: LeaseId, evidence: ReleaseEvidence) -> Result[None]` is transport-internal to those public routes; existing capture async `release(self, source: PhysicalMonitorSource) -> Result[None]`, display async `remove(self, owner_uuid: OwnerId, context: OperationContext) -> Result[None]`; Task 1 can_submit and snapshot/record types.
**Validation:** env QT_QPA_PLATFORM=offscreen python -m pytest -q tests/session tests/integration tests/ui/test_credential_relay.py --basetemp=/tmp/ap-relay-dashboard
**Commit message:** feat(session): finish relay on handle admission and ordered cleanup

**Files:**

- Modify: `src/airplay_linux/session/pairing.py:70-168`, `:384-499` and Task 6 relay runtime/reduction.
- Modify: `src/airplay_linux/session/mirror.py:175-246`, `:300-484`.
- Modify: `src/airplay_linux/session/reconnect.py:137-201`, `:369-474`, including _sync_target's preexisting pairing/revocation branch.
- Modify: `src/airplay_linux/session/virtual.py:703-751` only to finish mirror relay bridge/retirement integration; virtual pre-ready checks and refusal remain.
- Modify: `src/airplay_linux/session/cleanup.py:287-353`, `:384-464`, `:786-852` and its existing trust-cancellation continuation where relay routing needs selection.
- Modify: `src/airplay_linux/session/phase.py:20-85` only for the specified relay/cleanup priority.
- Extend/Test: `tests/session/test_credential_relay.py` with `test_relay_provisional_success`, `test_relay_exhaustion`, `test_relay_cleanup`, `test_relay_zero_trust_store_calls`, `test_relay_terminal_no_reconnect`; finish Task 6 definitions' real cleanup/replacement controls.

### Steps

- [ ] Add the named RED tests with real lifecycle composition and deterministic transport boundaries. Test both success arrival orders, source timeout after success_pending, stale old completion, three Waiting, three daemon refusals, three certified spawn failures and a mixed sequence; preserve the third code and reach idle only after release. Add store/coordinator spies on every relay exit. Tests that only reduce snapshots cannot replace these lifecycle tests.
- [ ] Complete_relay_start validates returned handle against exact pending start operation/receiver/target/endpoint/lease/daemon/session and the transport's current relay generation. If challenge/success_pending exists for that owner, commit Trusted and succeeded once before admitting the exact handle. A handle-first completion records the satisfied generation; its later Trusted submission completion consumes its ID without discarding that handle. Submit-first remains Pairing until this commit. A challenged start's trust generation/start effect identity is not invalidated by provisional success or challenge retirement.
- [ ] Invoke that commit in reconnect acquisition delivery before `_BLOCKED_TRUST` can pause/discard the returned handle, and in the mirror lifecycle's corresponding path. A start without relay challenge/pending success keeps the existing admission path. Any failed source/readiness result clears pending success and closes the challenge with that exact start code; no public Trusted is published. Preserve the original terminal cause independently when retained cleanup exposes cleanup_timeout.
- [ ] Select relay retirement before existing transaction/revocation handling in discovery_changed, _sync_target, expiry, credential_invalid, cancel, owner failure and shutdown cleanup. Capture the old immutable lease/challenge/context before superseding actor state, so replacement can release only the original obligation. Current Ended/Stale fails that waiting owner; a retired result affects cleanup only. Do not call begin/prepare/commit/transactional cancel/mark_invalid/forget/revoke/retry_revocation for relay. Public relay cancel routes transport release and is not a store transaction. Pairing trust becomes Untrusted on terminal routes, never RevocationPending/TrustCleanupRequired merely because transport cleanup is held.
- [ ] Put the dedicated relay terminal branch ahead of generic reconnect and daemon restart classification for pairing_expired, receiver_rejected, stale_target and daemon_failed and all other failed relay-start outcomes. No automatic credential resubmission/reconnect/restart follows. Ordinary owned reconnect can encounter a fresh challenge, but that does not reuse a prior lease. A later operator Start after idle allocates a new lease and may prompt again even if the previous intent committed Trusted.
- [ ] In OrderedCleanup, keep transport acquisition/release first, then audio/capture/display stages and all late acquisition-ledger obligations before ownership clearing. Relay cancellation joins the same release task rather than launching an independent disconnect. Cleanup pending retains its exact lease/public cleanup state until release succeeds. Clearing intent/ledger never proves release. Whole-application bound stays 15.0 s and may report retained cleanup_timeout instead of fabricating completion.
- [ ] Final cleanup commit enforces every §12 conjunct, not just stream absence:

```python
assert not state.intent_active
assert state.transport is TransportState.Idle
assert state.display is DisplayState.None_
assert state.stream is None
assert state.physical_source is None
assert state.display_handle is None
assert state.ownership_uuid is None
assert state.intent_stage is IntentStage.idle
assert not state.cleanup_pending
assert state.transport_lease_id is None
assert state.challenge is None
assert ctx.outstanding_acquisitions() == 0
assert not ctx._manager.pending_resources(state.receiver_id)
assert state.relay is None
```

These are assertions in test_relay_cleanup after confirmed transport/capture/display completion, with negative controls holding each stage/resource independently. Production code guards those conditions rather than relying on assertions. The actor clears the public relay projection only after the original transport obligation is confirmed released. It preserves relay_notice through idle, clears on explicit Start/operator action and on admitted success; derive_phase cannot return Available early.

- [ ] In exhaustion tests the third charge synchronously closes the challenge and fails owner eligibility before awaiting cleanup. Holding release withholds Start and keeps ownership; completing capture then display precedes ownership=None. Next Start produces a new lease. Add InvalidInput/uncertainty controls proving no charge; a third nonreceiver rejection reports its typed daemon/spawn code, not receiver_rejected.
- [ ] Extend the completion-dedup test through cleanup/new challenge without clearing S1's tombstone; attach controlled presenter observations from Task 10 to show exactly two invalid-result replacements for S1/S2 and none for replay. Exercise zero-store routes: success, cancel, expiry, invalidation, endpoint replacement/removal, exhaustion, stale/late result and shutdown. Run validation including old transaction/reconnect/virtual tests and commit owned paths.

## Task 12: Verify termination proofs against held active and retired relay obligations

**Implementer tier:** Advanced

**Lane:** core
**Depends on:** 5, 9
**Owns:** tests/process/test_relay_termination.py
**Exclusive resources:** pytest-relay-core
**Interfaces produced:** Completed single definitions of `test_relay_termination_proof`, `test_relay_termination_requires_exit`, `test_relay_receipt_vocabulary`, `test_relay_termination_clock_domains` in their specification-assigned process file, now including real transport-consumer assertions.
**Interfaces consumed:** Task 3 async `DaemonRegistry.termination_proof(self, record: DaemonRecord) -> Result[TerminationProof]`; Task 8 async `DoubleTakeTransport.release(self, lease: LeaseId, evidence: ReleaseEvidence) -> Result[None]`; Task 1 `ReleaseEvidence(*, reason: ErrorCode | None, observed_at: float, disconnect_ack: AckReply | None = None, status: StatusReply | None = None, daemon_instance_uuid: InstanceId | None = None, endpoint_generation: int | None = None, termination: TerminationProof | None = None)`; Task 7 mutable RelayLease/CredentialDispatch and unresolved command set; Task 5 RelayDaemon/Gate clock/scheduler fixtures and redacted child observations with the declared APIs.
**Validation:** python -m pytest -q tests/process/test_relay_termination.py tests/transport/test_credential_relay.py --basetemp=/tmp/ap-relay-core
**Commit message:** test(process): pin exact exit evidence through active and retired relay release

**Files:**

- Modify/Test: `tests/process/test_relay_termination.py`, extending the definitions created in Task 3; no duplicate or renamed top-level test identifier.
- All production source and shared fixtures are read-only in this task.

### Steps

- [ ] Add consumer assertions to the existing four-case exit predicate test. Construct actual DoubleTakeTransport with an active original lease and a distinct retired original lease, unresolved credential barrier and no alternate disconnect acknowledgement/status-absence/exit evidence. Query the real registry method for the original exact record. A replacement daemon/its empty status cannot discharge either old obligation.
- [ ] For each otherwise-valid stopped/live, stopped/unreadable, failed-completed/live and failed-completed/unreadable case, assert Err(cleanup_timeout), no proof and both obligations remain held after the bounded release attempt. Advance injected time through the same settlement deadline; don't wait five real seconds. There are zero credential/disconnect children. Each case independently reaches the positive-exit check with every earlier predicate true.
- [ ] Add absent-pid and exact pidfd-exited controls for both terminal forms. Pass the returned proof to release on active and retired leases; assert both removed exactly once, handles entered in _released and no disconnect required. Query after a newer generation occupies the same runtime name, including old runtime/socket absent or replaced, to pin history tolerance. A proof for a different daemon is refused stale_event without clearing the original fence/lease.
- [ ] Keep registry-only vocabulary/clock controls and their exact times. Test `cleanup_completed_at` equality by value, never numerical comparison with monotonic proof observations. Repeat the real transport consumption control with a failed-to-start live receipt: no proof and no drop on a generic daemon exit/shutdown success.
- [ ] In a disposable copy, remove only the positive-exit verification and execute each of the four negative parametrized nodes independently with a passing unmutated control. Each must fail at its expected cleanup_timeout/no-proof/retained-obligation assertion. Restore the copy and rerun that node green before another mutation. This prepares exact node IDs and failure evidence for Task 13's consolidated ledger; no file mutation occurs in the lane checkout.
- [ ] Run validation and commit the completed test consumer assertions. This separate task prevents the early query task from depending on a future transport implementation while retaining the exact specification-assigned file/function ownership.

## Task 13: Deliver offline runtime acceptance, canary coverage, documentation and mutation evidence

**Implementer tier:** Capable

**Lane:** dashboard
**Depends on:** 11, 12
**Owns:** tests/acceptance/test_credential_relay.py, tests/storage/test_relay_secrets.py, tests/relay_mutations.py, README.md, docs/backend-compatibility.md, docs/operations.md
**Exclusive resources:** pytest-relay-dashboard, relay-mutation-workspace
**Interfaces produced:** `test_relay_acceptance`, `test_relay_forbidden_spawn_guard`, `test_relay_documented_contract` in tests/acceptance/test_credential_relay.py and `test_relay_canary` in tests/storage/test_relay_secrets.py; offline mutation driver `python tests/relay_mutations.py --root . --basetemp-prefix /tmp/ap-relay-mut` with exit 0 only when every assigned executing baseline passes, every non-equivalent mutant fails its assigned assertion, restored controls pass and copied fixtures are cleaned up; updated current-behavior operator/backend documentation for specification §17.
**Interfaces consumed:** All integrated producer interfaces: `can_submit(lease: RelayLeaseView) -> bool`; async `DoubleTakeTransport.submit_pairing(self, challenge_id: ChallengeId, value: SensitiveText, context: OperationContext) -> PairingResult`, `release(self, lease: LeaseId, evidence: ReleaseEvidence) -> Result[None]`; async `DaemonRegistry.termination_proof(self, record: DaemonRecord) -> Result[TerminationProof]`; `PairingFlow.observe_relay(self, ctx: SessionContext, event: TransportEvent) -> bool`, `complete_relay_start(self, ctx: SessionContext, handle: StreamHandle) -> bool`; Task 10 `project(snapshot: SessionSnapshot, inputs: ViewInputs) -> PaneState` and Presenter prompt methods; Task 5 RelayDaemon/RelayGate fixtures and redacted ProcessRecorder/FakeProcess/ProbeProcessRecorder; existing `build_runtime(config: RuntimeConfig, **kwargs)` composition/injection surface, inspected in runtime.py without changing its signature, and existing `offline_boundary_tripwires` fixture's refusal_type, attempted list and attempts mapping.
**Validation:** env QT_QPA_PLATFORM=offscreen python -m pytest -q tests/acceptance/test_credential_relay.py tests/storage/test_relay_secrets.py --basetemp=/tmp/ap-relay-dashboard
**Validation:** env QT_QPA_PLATFORM=offscreen python tests/relay_mutations.py --root . --basetemp-prefix /tmp/ap-relay-mut
**Validation:** env QT_QPA_PLATFORM=offscreen python -m pytest -q --basetemp=/tmp/ap-relay-full
**Commit message:** test(pairing): verify offline relay delivery and document credential custody

**Files:**

- Create/Test: `tests/acceptance/test_credential_relay.py`.
- Create/Test: `tests/storage/test_relay_secrets.py`.
- Create: `tests/relay_mutations.py`, a standard-library subprocess/AST/tempfile driver for the specification's disposable mutation battery.
- Modify: `README.md:47-55` and `:89-104`.
- Modify: `docs/backend-compatibility.md:93-104` and `:129-145`.
- Modify: `docs/operations.md:19-20`, `:113-138`, adding mirror-relay workflow/residual/deferral copy in current sections.
- Approved design/specification, runtime.py, trust/coordinator.py, codecs, vendored patches and tests/conftest.py remain read-only.

### Steps

- [ ] Write the three acceptance tests and the canary test RED. Compose through build_runtime, actual adapters, actual SessionManager/lifecycle handlers, real state projection/presenter/dialog interfaces and the private fake daemon/process/portal/kernel boundaries. Existing acceptance public_runtime/OwnedCase patterns supply the external evidence without promoting an application-contract fixture to public support. No replacement top-level manager/relay implementation can satisfy this test.
- [ ] `test_relay_acceptance` runs Start A → automatic prompt → valid submit → prepared source → streaming → original handle admission/Trusted, preserving original audio off/on/initial volume and lease with no disconnect/restart during pairing. Assert no store/coordinator transactions and all retained observations redacted. Repeat for later fresh start after receiver forgets its own pairing. In the same top-level parametrized definition, a virtual credential request retains dependency_incompatible refusal/ordered cleanup and mints no relay/credential child. Offline acceptance is not a live receiver success claim.
- [ ] Independently pin the production-spawn guard itself using the installed tripwire, not the normal zero-call success scenario. This is the complete body shape for the required test, parametrized as below:

```python
@pytest.mark.parametrize("boundary", ["backend", "ctl"])
async def test_relay_forbidden_spawn_guard(
    offline_boundary_tripwires, monkeypatch, boundary,
):
    from airplay_linux.process.linux import LinuxProcessRunner

    reached = []

    async def inert_exec(self, *args, **kwargs):
        reached.append(True)
        raise FileNotFoundError

    monkeypatch.setattr(LinuxProcessRunner, "_exec", inert_exec)
    with pytest.raises(offline_boundary_tripwires.refusal_type):
        await offline_boundary_tripwires.attempts[boundary]()
    assert offline_boundary_tripwires.attempted == [boundary]
    assert reached == []
```

The run/spawn wrapper being tested remains installed; no second wrapper masks it. In the corresponding disposable mutant remove only check_process from guarded_run for ctl or guarded_spawn for backend. The inert stub is reached once, launches nothing and yields the ordinary typed missing-command result; absence of the exact expected refusal/zero-call condition kills that mutant, not a downstream refusal or lookup failure.

- [ ] `test_relay_canary` injects a transient credential only at an invocation boundary, collects diagnostics, ActivityLog/export, session/receiver records, receipts/intents, journals, PM ledger output when emitted by the test fixture, reprs, FakeProcess runs/spawned, recorder subclasses and private fixture captures, including nested dataclass containers. Scan each collected artifact for the canary without printing it in a failure. A captured input local is excluded from export collection; every retained command uses <redacted>. Also encode/decode SessionSnapshot through the unchanged explicit codec and assert no relay, live challenge, submission map, task, TerminationProof or RelaySubmitCompletion field is persisted. Reopening state must not claim receiver-owned credential storage as application remembered trust.
- [ ] Update all three current-behavior docs to specification §17: automatic owned mirror/reconnect prompts independent of selection; receiver keyring custody and startup witness; no application credential/reference/forget; targeted credential-bearing connect as the one argv exception; typed redaction plus same-UID argv visibility while ctl runs; both exact tables; independent ownership/physical-source/transaction gates; original audio/volume propagation and target-bound fall-through; 120 s original expiry/three consumed attempts/no automatic retry; provisional success/handle completion; Cancel/uncertain/retained cleanup and Start only after idle. Explain extended CLI leading-ASCII-minus password InvalidInput before dispatch and with zero consumed attempts, even when this start emitted no option tokens; no delimiter workaround. Preserve the separate transactional adapter restart limitation.
- [ ] Every doc names mirror-only scope and quotes the approved virtual deferral reason: a virtual start returns its handle on connecting before the later receiver prompt, so relay needs another readiness owner. Remove superseded current unconditional no-prompt/no-credential-argv claims; retain dated historical receipts as history. test_relay_documented_contract reads all three files and pins those claims and obsolete-claim removal only in current-behavior sections.
- [ ] Implement the mutation driver with AST/source-bounded edits in disposable copies only. It copies the current source/tests/pyproject into a fresh short temporary root, clears inherited PYTHONPATH/live opt-in flags, sets child cwd to the copy, and asserts imported airplay_linux.__file__ is inside that copy. The lane checkout and approved parser/spec never become mutation targets. Each row has a passing exact node baseline, one named mechanism deletion/change, an assigned diagnostic/assertion expectation and a restored green control. A missing edit target, collection/import failure, unavailable prerequisite, pre-existing red test, timeout or incidental exception is a failure of evidence, never a killed mutant. No dependency is installed and no production command can execute. Each child has a bounded subprocess timeout with cleanup; no output includes raw credential data. Temporary copies are cleaned on success and assertion failure; ledger output is secret-free and goes to the controller-assigned run report or a temporary artifact, not another repository deliverable.
- [ ] Execute every mutation row in the inventory below against its assigned identifier; for compound rows run each listed mechanism as its own isolated mutant. Preserve the exact failing assertion/node and child-count observation. The positive-exit deletion runs all four negative nodes independently; guard deletion independently exercises backend spawn and ctl run. No mutation in a merely redundant/irrelevant location counts as the named mechanism.

### Mutation inventory and exact killers

| Mechanism changed, one at a time | Required killer (file prefix given below) |
| --- | --- |
| Infer relay from partial table/connect-pin presence/unverified help | backend::test_relay_capability |
| Remove relay allows branch; physical-or-virtual authorization | backend::test_relay_authorization |
| Optional relay fence; optional dispatch_started; optional dispatch_status; pre-await-only fence | backend::test_relay_fencing |
| Targetless pin; dropped audio; dropped volume; fixed secret index; omitted final wait check | backend::test_relay_command |
| Aggregate prompt as identity; preserve old generation/counter on kind change | transport::test_relay_observation |
| Absence during barrier as failure; drop obligation at waiter completion | transport::test_relay_barrier_and_adoption |
| Close challenge on provisional success; discard owner handle; publish Trusted before admitted handle | session::test_relay_provisional_success |
| Disconnect before settlement; duplicate disconnect; replacement absence as release; drop retired on daemon event | transport::test_relay_release |
| Accept failed-to-start receipt; current-generation receipt lookup; substitute replacement identity | process::test_relay_termination_proof |
| Accept failed without completed stopped intent; accept completed error outcome; nonexistent terminal vocabulary | process::test_relay_receipt_vocabulary |
| Remove positive exit verification | process::test_relay_termination_requires_exit, each of four negative nodes independently |
| Convert/resample wall completion; compare it to monotonic observations/deadline | process::test_relay_termination_clock_domains |
| Require aggregate idle in exact disconnect acknowledgement | transport::test_relay_release_while_other_receiver_streams, fallback withheld |
| Resume permission on retained result; rearm credential expiry; begin handshake at connecting; accept stale callback | transport::test_relay_phases |
| Reset attempt counter; admit fourth charge; replace third code with generic code; charge InvalidInput | session::test_relay_exhaustion |
| Clear ownership before capture/display; allow Start with pending acquisition/retained resource | session::test_relay_cleanup |
| Generation/attempt/outcome prompt key; Retry/Pair during challenge; missing pending-key ownership | ui::test_relay_presentation |
| Reopen while success_pending; ignore prepared/streaming progress; close challenge early | ui::test_relay_eligibility |
| Refuse retained retry; allocate another lease; second disconnect while outstanding; disconnect despite proof | transport::test_relay_retained_retry |
| Settle uncertainty on client completion; on absent entry; settle acknowledged barrier before resolving status | transport::test_relay_uncertain_dispatch |
| Admit/offer C2 while C1 uncertain; disconnect during uncertain C1 | transport::test_relay_exclusivity |
| Remove only unresolved_dispatch_ids singleton equality guard, retaining C1 and every other predicate | transport::test_relay_exclusive_progress_requires_single_dispatch |
| Omit submit_fenced from shared predicate; clear it on retained; final-fence check missing | transport::test_relay_release_vs_submit_interleaving, independent admission/final-spawn controls |
| Drop _run secret_indices; generic dataclass recursion before command projection; raw FakeProcess runs | backend::test_relay_credential_metadata |
| Execute redacted argv; omit indices before production runner; fixed index across variants; fake-only runner coverage | backend::test_relay_production_runner_credential_index |
| Negative/out-of-range/bool/duplicate metadata allowed; missing relay index; indices on other operations | backend::test_relay_metadata_refusals |
| Generic receiver_rejected for refusals; transport text parsing; retain shutdown challenge; fuzzy suffix match | backend::test_relay_daemon_error_mapping |
| Third malformed read fails early; deadline extension on malformed; malformed resolves barrier | transport::test_relay_malformed_status |
| Remove submission identity; dedup by reused operation/result; double reduce; evict tombstone on retirement | session::test_relay_submission_completion_deduplication |
| Selected-pane-only relay scan/open; retarget answer to selection | ui::test_relay_prompt_for_unselected_started_receiver |
| Skip extended representation predicate; count as no_spawn; test emitted tokens only; rewrite/add delimiter | session::test_relay_option_shaped_password |
| Current identity around late result; partial owner fence; discard old cleanup observer | session::test_relay_stale_completion |
| Remove pending-key finalizer ownership; retain input/untaken handoff; retarget captured receiver | ui::test_relay_prompt_replacement_wipes_input |
| Any relay prepare/commit/cancel/mark_invalid/forget/revoke/retry_revocation; durable relay trust | session::test_relay_zero_trust_store_calls |
| Generic daemon_failed restart/retry; reuse failed relay lease | session::test_relay_terminal_no_reconnect |
| Unconditional submit fence; required callbacks on transactions; promote transaction flags; replace token path | backend::test_relay_transactional_compatibility and unchanged test_control truth-table gate |
| Raw argv journal/recorder/fixture capture; dump backend error/request with input | storage::test_relay_canary |
| Disconnect/restart instead of original resume; fake manager instead of build_runtime; enable virtual relay | acceptance::test_relay_acceptance |
| Remove only guarded_run.check_process; remove only guarded_spawn.check_process | acceptance::test_relay_forbidden_spawn_guard, ctl/backend nodes independently, inert _exec |
| Retain obsolete unconditional refusal/argv claim; omit receiver custody, virtual reason or option-shaped limitation | acceptance::test_relay_documented_contract |

Prefixes are exact repository files: backend=`tests/backend/test_credential_relay.py`; transport=`tests/transport/test_credential_relay.py`; session=`tests/session/test_credential_relay.py`; process=`tests/process/test_relay_termination.py`; ui=`tests/ui/test_credential_relay.py`; storage=`tests/storage/test_relay_secrets.py`; acceptance=`tests/acceptance/test_credential_relay.py`. The unchanged gate is `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags`.

### Coverage, final verification and execution handoff

- [ ] Mechanically count required §16 top-level test definitions by AST: exactly one per specification-assigned file/name, including the existing unchanged truth-table function. Imports, helper names, nested definitions, aliases and duplicate definitions do not satisfy inventory. Parametrization supplies variants. All twenty original strategy labels and every supplemental identifier map to the owning task/file above; no test name is dropped to make the suite green.
- [ ] Reconcile specification coverage: §§3–6/15 records and command authority → Tasks 1, 2, 4, 5, 6, 9; §7 phases → Tasks 7, 9; §§8–9 dispatch/release → Tasks 8–9; §10 history/clock/exit → Tasks 3, 8, 12; §§11–13 attempts/completion/cleanup → Tasks 6, 9, 11; §14 presentation → Tasks 6, 10–11; §§16–18 acceptance/docs/invariants → Tasks 2–13. Each mechanical invariant 1–20 must have a named executing test and its assigned mutation evidence, not only a prose assertion.
- [ ] Run all three validation argv in order. The full-suite run uses a short exclusive basetemp, offscreen Qt and cleared live opt-ins; it includes the old backend, process, transaction, audio, source, virtual, session, UI and offline-boundary regressions. Record actual counts, skip reasons and mutation outcomes; an offline optional-environment skip is described rather than a live success claim. Repeat only after a change or a failing/unresolved control justifies it.
- [ ] Review the final diff against this plan's path ownership and approved specification. No persistent credential/store/codec/token/dependency/receiver change is present; tests/conftest.py and the required truth-table function remain unchanged. Commit only owned paths with the task's message during execution. Return the controller's reviewable report with task/test/mutation evidence and the approved virtual-display deferral/reason. No live installation, receiver experiment, branch integration or publication is implied by this plan.
