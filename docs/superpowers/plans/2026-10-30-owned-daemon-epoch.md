# Owned-Daemon Epoch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Leave the checkboxes unchanged; the controller owns execution state.

**Goal:** Enable cooperative, receipt-backed ownership of an idle public DoubleTake daemon and verified physical-monitor Mirror, preserving every delivered lifecycle and trust gate except the explicitly amended connect predicate.

**Architecture:** Production composition captures launcher eligibility, installs durable receipt/ownership/teardown/authority seams, and obtains a file-backed credential witness before constructing the registry. Public control uses fresh endpoint evidence around I/O; a separately selected fixture authority retains the delivered handshake. Foundation and source/probe preparation remain behind the production activation guard until the single Task 9 activation commit.

**Tech Stack:** Python >=3.12, frozen keyword-only dataclasses, delivered Result/AtomicFiles ports, Linux procfs/AF_UNIX SO_PEERCRED/pidfd/flock/unshare, KDE KScreen JSON, carried Go DoubleTake patches, pytest/pytest-asyncio/pytest-qt.

### File structure and responsibility map

| File or exact pair | Responsibility and owning tasks |
| --- | --- |
| `src/airplay_linux/backend/contract.py`, `src/airplay_linux/runtime.py` | Immutable feature inputs and one ordered production composition; Tasks 1→7→8→9 serialize runtime changes. |
| `src/airplay_linux/domain/records.py`, `src/airplay_linux/domain/ports.py` | Required persisted DTOs and structural ports; Task 2. |
| `src/airplay_linux/storage/codecs.py`, `src/airplay_linux/storage/repositories.py` | Strict new-document decoding and atomic immutable history; Task 2. |
| `src/airplay_linux/process/ownership.py`, `src/airplay_linux/backend/control.py` | Fresh endpoint proof and selected authority; Tasks 3→9. |
| `src/airplay_linux/process/procfs.py`, `src/airplay_linux/process/linux.py` | Stage evidence and post-open pidfd enforcement; Task 4. |
| `src/airplay_linux/process/authority.py`, `src/airplay_linux/process/teardown.py` | Durable shared-root ownership and outcome-only cleanup recording; Tasks 5 and 4 respectively. |
| `src/airplay_linux/process/launcher.py`, `src/airplay_linux/process/daemon_registry.py` | Captured argv, sole application receipt writer and lifecycle serialization; Tasks 1→5→6→9. |
| `src/airplay_linux/process/supervisor.py`, `src/airplay_linux/process/credential_probe.py` | Independent supervisor observation/intent and namespace-local credential witness; Tasks 6→9 and 8 respectively. |
| `src/airplay_linux/display/inventory.py`, `src/airplay_linux/capture/source.py` | Shared fresh origin/geometry and exact requested-source proof; Task 7. |
| `src/airplay_linux/transport/doubletake.py`, `src/airplay_linux/policy/capabilities.py` | Source-before-ready, amended admission, unchanged operation-specific gates; Tasks 7→9 and 9 respectively. |

## Global Constraints

- Never execute `reboot`, `shutdown`, `poweroff`, `halt`, `kexec`, any `systemctl` power or sleep verb, `init 0|6`, or `telinit 0|6`; never restart or stop the display manager or `graphical.target`; never terminate your session or signal the process tree that hosts the PM. If you conclude a host transition is required, write `reboot-required.json` with `{schema: 1, trigger, evidence: [{path, sha256}], provenance: {run_id, lane, attempt, dispatch_key, observed_boot_id}}`, then report it and stop.
- Authority order is `docs/superpowers/plans/2026-10-30-owned-daemon-control.md`, its `2026-10-30-owned-daemon-control-amendment-1.md`, approved `docs/superpowers/specs/2026-10-30-owned-daemon-epoch-design.md`, then `docs/superpowers/specs/2026-10-30-owned-daemon-epoch-spec.md` for exact contracts. Read the handoff, operations, compatibility and backend-fork documents for delivered behavior. Do not edit those authorities, the amendment or this pinned plan during execution.
- Approved design identity: commit `de04d236c67c9c3ca30890f4c6bc2810e55be511`, blob `f0708be96bb7e3d115228d675308e8b00abc536c`, SHA-256 `b55faca175746411b31009c45f94ff086f73422ecd438bcef4f9ae7d40b5614c`; reviewed specification commit `76c0ec57d8c993f4e5d6755e9e098bacae72d3db`; Amendment 1 including §6 Clarification 1.1 at `dfe780a`; run `pm-run-20261030-040814-aefef9c0`, epoch 1. The design heading's draft wording does not supersede that approved identity.
- Never contact a live AirPlay receiver outside `unshare -rn` with a private socket and temporary credentials. Default verification uses deterministic fake backend/process/KScreen/Secret Service boundaries; no network dependency, host receiver, host socket lease or host session-bus client. Probe/mutation tooling never ships in the repository: the specified runtime credential-proof module is production functionality, while disposable observation drivers, mutants and mutation ledgers stay outside the checkout.
- Application metadata never contains a PIN, secret or credential reference. New reservations, receipts, intents, owner records, verdicts, logs and reprs contain no owner token, credential, raw backend output, environment dump or portal token. UUIDs, identities, paths, boot IDs and handles on new records use field(repr=False); use explicit persistence projections, never asdict. Retain the delivered DaemonRecord observation/fixture schema, including its existing owner_token; the public path marks that field unused and never copies it into new metadata.
- Never weaken a gate: receiver, transport admission except Amendment 1's connect change, trust, provider, target-generation, pairing-compensation and cleanup remain as delivered. Virtual mode, virtual-source authority, whole-screen inference and hostile same-UID defence are outside scope. Public foreground_control/owner_handshake and unsupported pairing/remembered-trust/audio-restart/query-modes flags remain false.
- Activation invariant: no production detached spawn, including a credential-probe spawn, is reachable until receipt production, verification, strict pidfd teardown and cross-process reconciliation exist. Tasks 1–8 keep the production activation guard closed; Task 9 alone activates the complete composition. Every task commits a test proving no production spawn for its forbidden/incomplete composition; phases 1–3 additionally require zero outer unshare invocations. Task 10 preserves Task 9's activation boundary.
- The no-spawn proof calls real `build_runtime(..., overrides={'process': recorder})` and observes the final injected ProcessPort.run and .spawn. Record help/version/capability/inventory observations separately; count backend `-daemonize` only where directly invoked, and every outer unshare separately. Probe `run(config, *, process, namespace)` uses a separately injected inner recorder for both actual backend legs. Never substitute SessionRig.deps, nested-argv inference, an incompatible-help false positive or a detached test-only composition.
- File ownership is exact in each task below; modify/create only listed paths. Line ranges locate the reviewed snapshot and may shift after predecessors. Shared runtime.py, backend/contract.py, backend/control.py, transport/doubletake.py, policy/capabilities.py and test-file edits follow the listed dependency edges. Tasks 1–4 have no task prerequisites: consume frozen structural signatures using postponed/type-only imports and local evidence fakes; do not add runtime imports of another unfinished foundation lane. Integrate all four before Task 5. Task 2 owns canonical persisted DTO definitions including AuthorityOwner and ProbeVerdict in domain/records.py; Tasks 5/8 re-export those identical classes at their public seams, avoiding circular imports or test-only decoder constructors.
- Use `~/.venvs/airplay-linux`; install the executing worktree into that environment before real-process tests. Run with `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8` and short `--basetemp /tmp/oeN` paths, exclusively after validating offline boundary interception in the task's local fixtures; Task 10 makes those tripwires suite-wide. Baseline `3787 passed, 12 skipped` is historical, not a promised result. Record `tests/process/test_supervisor.py::test_killing_supervisor_cannot_leave_a_daemon_that_ignores_term` as the known flake; no blanket retries, weaker assertions or new skips to mask it.
- All new durable fields are required constructor/wire members; null is explicit. Reject bool-as-int, duplicates, extra/missing members, wrong versions/types, nonfinite times, partial states, filename/UUID mismatch and all trailing bytes including whitespace. PID 1..2**31-1; durable integers <=2**63-1; generations/inodes positive; UID/device/ticks nonnegative; timestamps accept finite JSON int/float and decode to float; MAX_TEXT=4096 and MAX_IDENTIFIER=256. Canonical UUIDs, no-follow owned absolute paths, safe RecordError reasons and atomic temp-write → file-fsync → rename → directory-fsync are mandatory. Existing record codecs/stores retain their exact validation/tolerance and never pass through new decoders.
- Receipt current/generation comes from all endpoint receipts, terminal included: unique nonterminal at the all-receipts maximum; next generation maximum+1, refusing exhaustion. Ambiguous/stale nonterminals block control, cleanup and replacement. Failed/stopped receipts are immutable; no teardown deletion API or mutable current-pointer file. Application ID, launch UUID and observed daemon ID are distinct channels.
- Exactly two durable pre-signal alternatives exist: registry/isolated-child receipt stopping, or a supervisor-published SupervisorReleaseIntent satisfying spec §3.4. DaemonCleanupIntent and DaemonRecord stopping are outcome/observation only. Supervisor never writes receipts; recovery retains its release-intent file, terminalises through registry-only writes and never re-signals. Unreadable is never absence; uncertainty retains cleanup_required and blocks replacement.
- Owned termination is pidfd-only with stage-complete TerminationEvidence and synchronous same-stage post-open revalidation; no numeric-PID fallback. Use TERMINATION_GRACE=3.0, existing LinuxProcessRunner.kill_timeout and overall shutdown <=15 seconds without resetting bounds. Flock uses one 5.0 s deadline/0.05 s backoff; source/challenge one 5.0 s deadline/0.1 s polling; credential positive socket+idle 3.0 s, idle confirmation 2.0 s, teardown grace 3.0 s.
- Every M1–M10 mutation and every additional spec §10 obligation must be killed by its named test with an unchanged passing control. A test that stays green when its claimed mechanism is removed is insufficient. Mutations run only in disposable copies; record import provenance, mechanism-specific failure and cleanup outside the repository. Test lists below cover all 82 distinct module-qualified functions named by §10, including the retained existing supervisor flake; additional focused tests are permitted only in owned test files.
- Treat every command below as future implementation validation, not evidence already obtained. This planner creates only `docs/superpowers/plans/2026-10-30-owned-daemon-epoch.md`; it does not run pytest, launch the backend or use the network. A task is complete only after its named tests, its no-spawn proof and its commit have actual recorded results.

## Task 1: Add owned_daemon_control plumbing and pure composition derivation

**Implementer tier:** Frontier
**Lane:** capability
**Depends on:** none

**Files:**

- Create: `src/airplay_linux/process/launcher.py` — LaunchCapability and captured ProductionLaunchCapability eligibility/argv, with no launch side effect.
- Modify: `src/airplay_linux/backend/contract.py:1-918` — composition-only flag and transient CredentialProofVerdict.
- Modify: `src/airplay_linux/runtime.py:1-627` — pure derivation, final process-override injection before probing, and closed epoch installation/activation boundary.
- Test: `tests/backend/test_contract.py`.
- Create: `tests/acceptance/test_no_production_spawn.py` — final-port recorder, eligible fakes and phase guard proof.

**Interfaces:**

- Produces `BackendFeatures.owned_daemon_control: bool = False` and the inert default `BackendFeatures.source_reporting_complete: bool = False`, both exact bools. Raw help/probe/token decoding always leaves ownership false; Task 7 owns complete-source recognition. Retain public `foreground_control=False` and `owner_handshake=False`.
- Produces `LaunchCapability`: `production_graph: bool; available(self) -> bool; argv(self, socket_path: str) -> tuple[str, ...]; ambient_socket(self) -> str | None`.
- Produces `CredentialProofVerdict(service_leg: Literal['passed','failed','not_run'], backend_positive_leg: Literal['passed','failed','not_run'], backend_negative_leg: Literal['passed','failed','not_run'], failure: ErrorCode | None)`; `.proven` means all three passed and no failure. Failed leg without failure and all-pass with failure are invalid. It remains transient and gains no wire decoder here.
- Produces `derive_composition_capabilities(probed: BackendFeatures, launcher: LaunchCapability, credential_proof: CredentialProofVerdict, source_verifier: ReportedSourceVerifier | None, ambient_socket: str | None) -> BackendFeatures` in runtime; source protocol annotations are postponed until Task 7 installs the implementation.
- Preserves `build_runtime(config: RuntimeConfig, overrides: Mapping[str, Any] | None = None) -> Awaitable[Result[Runtime]]` and extends internal `_compose_production(config: RuntimeConfig, *, process: ProcessPort | None = None) -> Awaitable[_ProductionGraph]`. The special process override is valid without a deps override, is consumed before any production probes, and is not a ManagerDeps adapter replacement.
- Consumes future source structural contract `physical_inventory_available: bool; async verify(self, requested: str, reported: BackendSource | None) -> Result[None]` and delivered BackendFeatures/Result/process composition. No source/probe module import is required to evaluate the closed boundary.

### Required tests and assertions

- Add `tests/backend/test_contract.py::test_owned_control_is_composition_only` (M1): raw/public/tokens cannot set ownership; eligible derivation can; ambient socket and nonproduction origin cannot; old flags stay false.
- Add `tests/acceptance/test_no_production_spawn.py::test_process_observations_classify_direct_and_unshare_daemon_launches`: direct backend invocation and outer namespace invocation are distinct recorder events; ordinary observations remain recorded. Task 8 extends the inner two-leg witness assertions.
- Add `tests/acceptance/test_no_production_spawn.py::test_phase_guard_blocks_eligible_production_launch`: fully eligible launch fakes, real runtime/process override, zero direct launch and zero unshare; removing only the guard reaches the recorded attempted-launch boundary with the same evidence.
- Eligible fakes supply captured launcher/root/token/inventory evidence at installation seams, not today's incompatible help. Guard-removal controls may use controlled fakes for uninstalled adapters; they must preserve real build_runtime wiring and expose the actual final process boundary. Tasks 8–9 replace preparatory adapter seams with the installed complete graph.

### Implementation steps

- [ ] Add the named capability and no-spawn tests; run the task command and observe failure at missing flag/derivation/guard behavior, not at an unrelated dependency.
- [ ] Add process to OVERRIDABLE outside _ADAPTER_OVERRIDES, pass it from build_runtime into _compose_production before probe_backend, and use it in every produced adapter. Construct LinuxProcessRunner only when process is None. Preserve individual ManagerDeps override validation and exercise the injected final port without a deps override.
- [ ] Add the exact feature default and transient verdict validation; use fixed leg/failure values and redacted identifying fields. Raw capability producers cannot assert ownership or keyring.
- [ ] Capture launcher eligibility once: verified daemonize/socket/cred-backend surface, absolute private owned writable nonsymlink root, no ambient socket, production origin. `available()` only reads captured evidence; `argv()` rejects outside-root/invalid paths with one fixed ValueError message before I/O.
- [ ] Implement the pure formula below. Complete-report evidence is false until Task 7 installs exact three-token recognition; absent source verifier never means physical_source.

```python
from dataclasses import replace

def derive_composition_capabilities(probed, launcher, credential_proof,
                                    source_verifier, ambient_socket):
    L = launcher.production_graph and launcher.available() and ambient_socket is None
    C = credential_proof.proven
    R = probed.source_reporting_complete
    V = source_verifier is not None
    I = V and source_verifier.physical_inventory_available
    return replace(probed, owned_daemon_control=L,
                   physical_source=bool(R and V and I), keyring=C)
```

- [ ] Put the closed activation check before any production backend/probe dispatch. Keep eligibility derivation distinct from launch admission, so injected all-passed pure inputs cannot bypass installation requirements or trigger probing.
- [ ] Run the no-spawn test with unchanged eligible fakes and its guard-removal control; keep the observing recorder in the committed tests and mutation mechanics outside the checkout.
- [ ] Validate, then commit only owned files with `feat(capabilities): add composition-only owned daemon control`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/backend/test_contract.py tests/acceptance/test_no_production_spawn.py --basetemp /tmp/oe1 -q`.
**Exit evidence:** raw capability false; pure eligible formula true; dormant production still records zero launch/unshare; M1 and guard-removal controls fail for the intended assertion when mutated.

## Task 2: Add versioned lifecycle codecs and immutable receipt stores

**Implementer tier:** Frontier
**Lane:** receipts
**Depends on:** none

**Files:**

- Modify: `src/airplay_linux/domain/records.py:1-184` — reservation, receipt, both intent records, AuthorityOwner and ProbeVerdict canonical DTOs; existing DaemonRecord unchanged.
- Modify: `src/airplay_linux/domain/ports.py:1-300` — three lifecycle store protocols and the exact receipt-free terminate signature.
- Modify: `src/airplay_linux/storage/codecs.py:1-1112` — six strictly scoped new document codec families.
- Modify: `src/airplay_linux/storage/repositories.py:1-397` — receipt, cleanup-intent and supervisor-intent repositories.
- Modify: `src/airplay_linux/storage/__init__.py:1-86` — explicit new exports.
- Create: `tests/storage/test_reservation_codecs.py`, `tests/storage/test_launch_receipts.py`, `tests/storage/test_cleanup_intents.py`, `tests/storage/test_supervisor_release_intents.py`.
- Test: `tests/storage/test_repositories.py`.

**Interfaces:**

- Produces frozen keyword-only DaemonReservation/LaunchReceipt/DaemonCleanupIntent/SupervisorReleaseIntent with every exact required member, envelope and state combination from spec §§3.3–3.6; no constructor/decoder defaults and no new member on DaemonRecord. Also produces canonical `AuthorityOwner(schema: Literal['airplay-linux/daemon-authority-owner/v1'], instance_uuid: InstanceId, app: ProcessIdentity, boot_id: str, acquired_at: float)` and `ProbeVerdict(schema: Literal['airplay-linux/credential-service-proof'], version: int, service_leg: Literal['passed','failed','not_run'], backend_positive_leg: Literal['passed','failed','not_run'], backend_negative_leg: Literal['passed','failed','not_run'], failure: ErrorCode | None)`, version=1, without imports from future lifecycle modules.
- Produces `encode_daemon_reservation(record: DaemonReservation) -> bytes`, `decode_daemon_reservation(payload: bytes) -> DaemonReservation`; `encode_launch_receipt(record: LaunchReceipt) -> bytes`, `decode_launch_receipt(payload: bytes) -> LaunchReceipt`.
- Produces `encode_daemon_cleanup_intent(record: DaemonCleanupIntent) -> bytes`, `decode_daemon_cleanup_intent(payload: bytes) -> DaemonCleanupIntent`; `encode_supervisor_release_intent(record: SupervisorReleaseIntent) -> bytes`, `decode_supervisor_release_intent(payload: bytes) -> SupervisorReleaseIntent`.
- Produces LaunchReceiptStore and DaemonCleanupIntentStore: `async write(self, record: LaunchReceipt | DaemonCleanupIntent) -> Result[None]` with each protocol using only its own record type; `async all(self) -> Result[tuple[LaunchReceipt, ...]]` or `Result[tuple[DaemonCleanupIntent, ...]]` respectively.
- Produces `JsonLaunchReceiptStore(files: AtomicFiles)` and `JsonDaemonCleanupIntentStore(files: AtomicFiles)` implementing those exact type-specific signatures; no removal API.
- Produces SupervisorReleaseIntentStore and `JsonSupervisorReleaseIntentStore(files: AtomicFiles)`: `async write(self, record: SupervisorReleaseIntent) -> Result[None]`, `async read(self) -> Result[SupervisorReleaseIntent | None]` anchored to one runtime directory and release-intent.json.
- Publishes `ProcessPort.terminate(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Result[None]`; TerminationEvidence is a postponed Task 4 procfs type. No receipts cross this port.
- Produces strict owner/verdict projection codecs from spec §3.5 with `encode_authority_owner(record: AuthorityOwner) -> bytes`, `decode_authority_owner(payload: bytes) -> AuthorityOwner`, `encode_probe_verdict(record: ProbeVerdict) -> bytes`, `decode_probe_verdict(payload: bytes) -> ProbeVerdict`. All constructors come from this task's canonical domain records; Tasks 5/8 re-export the same classes, so codecs and valid controls execute now without future-module imports.

### Required tests and assertions

- Add `tests/storage/test_launch_receipts.py::test_receipt_generation_roundtrip_and_missing_member_refused` (M2): exact generation 37 and required explicit-null daemon_instance_uuid; reopen preserves history/current derivation.
- Add `tests/storage/test_reservation_codecs.py::test_reservation_codec_rejection_matrix` and `tests/storage/test_launch_receipts.py::test_receipt_codec_rejection_matrix_by_state`.
- Add `tests/storage/test_cleanup_intents.py::test_cleanup_intent_codec_rejection_matrix` and `tests/storage/test_supervisor_release_intents.py::test_supervisor_release_intent_codec_rejection_matrix`.
- Add `tests/storage/test_launch_receipts.py::test_all_partial_states_and_unknown_versions_refuse` and `tests/storage/test_launch_receipts.py::test_failed_cleanup_intent_roundtrip_and_one_completion_preserve_receipt`.
- Add `tests/storage/test_repositories.py::test_legacy_store_roundtrip_is_byte_identical_while_new_types_are_strict`: all delivered record families preserve validation/tolerance; byte-identical DaemonRecord store round trip including owner_token; all six new families independently reject duplicates/trailing/missing members.
- Add `tests/storage/test_launch_receipts.py::test_malformed_receipt_cannot_reach_launch_writer`: injected launch/signal/unlink observers remain zero for every malformed consumed receipt. This is this task's no-production-spawn proof, requiring no capability-lane import.

### Test kernel

Implement receipt_state_documents as the exact valid state→LaunchReceipt fixture; this representative test runs on its reserved value and complements the full per-type/state rejection matrices.

```python
import pytest
from airplay_linux.storage.codecs import RecordError, encode_launch_receipt, decode_launch_receipt

def test_all_partial_states_and_unknown_versions_refuse(receipt_state_documents):
    reserved = receipt_state_documents['reserved']
    raw = encode_launch_receipt(reserved)
    assert decode_launch_receipt(raw) == reserved
    assert reserved.daemon_instance_uuid is None
    for suffix in (b' ', b'\n', b'\t', b'{}'):
        with pytest.raises(RecordError):
            decode_launch_receipt(raw + suffix)
```

### Implementation steps

- [ ] Write exact valid controls per new type/state plus malformed matrices: schema/version, missing/extra/duplicate, leaf/bool/int/range/time, partial identity, filename and trailing-byte cases. Run the targeted command and capture the first relevant failure.
- [ ] Define required reservation fields: schema_version, launch_uuid, instance_uuid, app, owner_uid, boot_id, runtime_directory, runtime_device, runtime_inode, endpoint_generation, socket_name, protocol_version. Receipt adds state, daemon_instance_uuid, supervisor, daemon, entry_device, entry_inode, socket_inode, peer_pid, peer_start_ticks, peer_uid, status_state, credential_service, failure, updated_at.
- [ ] Encode explicit allowlists; reservation/receipt and supervisor intent require envelope version plus schema_version payload, cleanup intent has five payload fields and no schema_version, owner has exactly five members with version in schema, verdict has exactly its six enumerated members and no derived proven. Never reuse a legacy ingestion decoder for strict new documents or tighten legacy loading.
- [ ] Implement fail-closed enumeration under daemon/receipts and daemon/cleanup-intents; any unreadable/malformed item returns persistence_failed and retains evidence. Receipt writes preserve reservation identity and observed daemon ID, retain evidence through stopping and prohibit terminal rewrites.
- [ ] Enforce initial cleanup-intent publication with explicit null completion/outcome and one immutable completion; supervisor store permits initial atomic publication and identical repeat only, no deletion or changed repeat. Keep supervisor publication permission in its caller, not a guessed repository proof.
- [ ] Preserve AtomicFiles publication ordering; failure at any atomic step cannot authorize a launch or signal. Directory/filename UUID validation precedes any lifecycle consumer.
- [ ] Run M2 plus each category-removal control in disposable copies, the no-spawn storage test and delivered store compatibility controls. Commit only owned files with `feat(storage): persist strict immutable daemon launch evidence`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/storage/test_reservation_codecs.py tests/storage/test_launch_receipts.py tests/storage/test_cleanup_intents.py tests/storage/test_supervisor_release_intents.py tests/storage/test_repositories.py --basetemp /tmp/oe2 -q`.
**Exit evidence:** new strict documents refuse every specified malformed category; legacy bytes/tolerance unchanged; terminal/intent immutability and zero side effects are observed.

## Task 3: Verify owned endpoints with anchored entries and fresh peer evidence

**Implementer tier:** Frontier
**Lane:** verifier
**Depends on:** none

**Files:**

- Create: `src/airplay_linux/process/ownership.py` — VerifiedEndpoint, endpoint verifier and dormant EvidenceAuthority.
- Modify: `src/airplay_linux/backend/control.py:1-945` — OwnershipProof/OwnershipAuthority/HandshakeAuthority contracts; leave public selection/admission activation for Task 9.
- Create: `tests/process/test_ownership.py`.

**Interfaces:**

- Produces `VerifiedEndpoint(daemon: ProcessIdentity, socket_entry: tuple[int,int], socket_inode: int, peer_uid: int, generation: int)` and `verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]`.
- Produces `OwnershipProof(kind: Literal['handshake','evidence'], daemon: ProcessIdentity, socket_entry: tuple[int,int], generation: int, handshake: Handshake | None)`; evidence has handshake=None; identifiers/handshake are repr=False.
- Produces OwnershipAuthority, EvidenceAuthority and HandshakeAuthority with exactly `allows(self, operation: BackendOperation) -> bool`, `async prove(self, record: DaemonRecord) -> Result[OwnershipProof]`, `async authorize(self, record: DaemonRecord, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Result[OwnershipProof]`, `async confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Result[None]`, `async release(self, record: DaemonRecord) -> Result[None]`.
- Consumes delivered `DispatchFence = Callable[[], Result[None]]`, procfs typed identity/parent/UID reads and injected endpoint observations. Receipt-shaped fixtures follow spec §3.4; generation comes from a separately supplied all-receipts source, never receipt.endpoint_generation as a tautology.
- Consumes frozen termination boundary `async terminate(identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Result[None]`; type-only annotations and injected process evidence avoid a foundation-lane runtime dependency.

### Required tests and assertions

- Add `tests/process/test_ownership.py::test_different_socket_argv_refuses_before_control_or_signal` (M3) and `tests/process/test_ownership.py::test_peer_start_ticks_must_equal_receipt` (M4).
- Add `tests/process/test_ownership.py::test_generation_uses_max_across_terminal_receipts_not_recency` and `tests/process/test_ownership.py::test_multiple_current_receipts_refuse_without_cleanup`.
- Add `tests/process/test_ownership.py::test_stale_terminal_receipt_cannot_authorize_cleanup` with sole-max failed/independently absent positive control and stale-nonterminal refusal.
- Add `tests/process/test_ownership.py::test_duplicate_unix_path_rows_refuse` and `tests/process/test_ownership.py::test_unlinked_old_listener_and_replacement_entry_refuse`.
- Add `tests/process/test_ownership.py::test_verifier_failure_has_no_process_side_effects`: every refusal sees zero status/mutation/spawn/signal/unlink. This supplies this lane's committed no-spawn test.

### Test kernel

owned_endpoint_evidence installs independently mutable procfs/socket observations used by the actual verifier; it exposes receipt, current_generation, argv and separate io_calls/signal_calls/launch_calls lists. io_calls counts application status/mutation bytes, excluding verifier peer observations. Only argv changes below; every other fact remains a passing control.

```python
from airplay_linux.process.ownership import verify_owned_endpoint
from airplay_linux.domain.resources import Ok, Err, ErrorCode

def test_different_socket_argv_refuses_before_control_or_signal(owned_endpoint_evidence):
    e = owned_endpoint_evidence
    assert isinstance(verify_owned_endpoint(e.receipt, current_generation=e.current_generation), Ok)
    e.argv = (b'doubletake', b'-daemonize', b'-socket', b'/wrong/socket', b'-cred-backend', b'keyring')
    result = verify_owned_endpoint(e.receipt, current_generation=e.current_generation)
    assert isinstance(result, Err) and result.error.code == ErrorCode.ownership_mismatch
    assert e.io_calls == e.signal_calls == e.launch_calls == []
```

### Implementation steps

- [ ] Build owned_endpoint_evidence fixtures with independently variable directory, entry, peer credentials, unique kernel row, peer FD, PID/ticks/UID, argv, boot and generation. Add named tests, then confirm their relevant RED results.
- [ ] Implement component-wise O_NOFOLLOW|O_DIRECTORY traversal from /, owner/mode=0700 and reserved directory dev/inode checks; stat basename relative to original dirfd and retain that anchor throughout verification.
- [ ] Connect only to obtain SO_PEERCRED; send no control bytes. Require exact owner UID, exactly one /proc/net/unix path row and an FD held by that peer to the row's kernel socket inode. Keep entry inode and kernel socket inode distinct.
- [ ] Require peer PID/ticks equal recorded daemon, /proc UID equal reservation UID, exact NUL-separated -daemonize and -socket arguments, no conflicting second socket, current boot and all-receipts generation. Restat the anchored entry after reads; replacement/unlinked old listeners fail.
- [ ] Return stale_event for superseded generation/current-entry proof; preserve spec §7 ownership_mismatch/stale_socket distinctions. Close every temporary descriptor/connection on all branches; verifier has no lifecycle writes or cleanup side effects.
- [ ] Implement pure allows plus proof/authorize/confirm: join observed daemon instance to receipt daemon_instance_uuid, re-prove around I/O, reevaluate original fence, compare original daemon/entry/generation. Fixture authority retains all delivered owner/instance/daemon/socket/protocol comparisons and nonrecursive raw status I/O.
- [ ] Implement release against an injected exact receipt/reservation source: choose independent stage evidence, require only the applicable stopping receipt or supervisor release intent, pass strict=True and fresh post-open closure, and return success only after exact exit/entry cleanup. Failed cleanup-outcome records alone grant no signal authority.
- [ ] Run M3/M4 and generation/row/entry mutations against positive controls; validate zero side effects. Commit with `feat(ownership): verify receipt-backed daemon endpoints`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/process/test_ownership.py --basetemp /tmp/oe3 -q`.
**Exit evidence:** one fully valid anchored endpoint proves; each isolated evidence failure refuses before I/O/signals; fixture authority retains delivered equality checks; production selection remains dormant.

## Task 4: Enforce stage-specific pidfd teardown and cleanup-outcome writing

**Implementer tier:** Frontier
**Lane:** teardown
**Depends on:** none

**Files:**

- Modify: `src/airplay_linux/process/procfs.py:1-386` — TerminationEvidence and fresh stage-specific fact extraction.
- Modify: `src/airplay_linux/process/linux.py:1-880` — strict terminate implementation and exact-exit bounds.
- Create: `src/airplay_linux/process/teardown.py` — cleanup-outcome writer using the frozen store port; no new authorization mechanism.
- Modify: `src/airplay_linux/testing/adapters.py:1-1869` — fake process termination keywords and independent signal observations.
- Test: `tests/process/test_linux_runner.py`.
- Create: `tests/process/test_teardown_intents.py`.

**Interfaces:**

- Produces `TerminationEvidence(stage: Literal['pre_bind','bound'], expected_identity: ProcessIdentity, expected_uid: int, expected_socket_name: str, expected_supervisor: ProcessIdentity | None, expected_entry: tuple[int,int] | None, expected_socket_inode: int | None)` in procfs; all required, identifying fields repr=False.
- Implements `async terminate(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Result[None]` on LinuxProcessRunner and FakeProcess; preserve run/spawn/identity/owned/subscribe signatures and non-owned defaults.
- Produces CleanupOutcomeWriter with `__init__(self, store: DaemonCleanupIntentStore) -> None`, `async record(self, intent: DaemonCleanupIntent) -> Result[None]`, `async complete(self, intent: DaemonCleanupIntent, *, completed_at: float, outcome: Literal['stopped'] | ErrorCode) -> Result[None]`. It only records/rechecks one initial outcome and its one completion; it cannot signal, unlink, rewrite receipts or authorize teardown.
- Consumes `async write(self, record: DaemonCleanupIntent) -> Result[None]` and `async all(self) -> Result[tuple[DaemonCleanupIntent, ...]]` via a postponed structural port. Independent tests supply exact frozen intent values and a recording store; integration uses Task 2's actual JsonDaemonCleanupIntentStore.

### Required tests and assertions

- Add `tests/process/test_linux_runner.py::test_strict_receipt_revalidation_after_pidfd_open_refuses_without_signals` (M4): callback first returns Err after pidfd open; record call order and both signal channels.
- Add `tests/process/test_linux_runner.py::test_strict_no_pidfd_never_signals_numeric_pid` (M5): otherwise valid live candidate, pidfd unavailable, dependency_incompatible and independently observed zero numeric/pidfd signals.
- Add `tests/process/test_linux_runner.py::test_strict_revalidates_identity_after_pidfd_open`: recycle ticks during open; mismatch sends no signal.
- Add `tests/process/test_teardown_intents.py::test_cleanup_outcome_writer_cannot_authorize_signal_or_spawn`: initial/one completion, changed repeats refused, failed receipt bytes untouched, zero launch/signal/unlink.
- Extend owned-stage parametrization in owned test files for every missing/inconsistent field, pre-bind ticks/UID/argv/supervisor-parent mismatch and bound entry/peer/row/FD mismatch; absence and unreadable must have distinct outcomes.

### Test kernel

strict_signal_observer supplies real runner logic with injected pidfd/procfs/signal boundaries, valid identity/owner_uuid/runtime_directory/evidence/deadline, a callable revalidate, and independent numeric_signals/pidfd_signals lists. Opening support is the sole changed fact.

```python
from airplay_linux.domain.resources import Err, ErrorCode

async def test_strict_no_pidfd_never_signals_numeric_pid(strict_signal_observer):
    s = strict_signal_observer
    s.pidfd_available = False
    result = await s.runner.terminate(s.identity, s.owner_uuid, s.runtime_directory,
                                     s.deadline, strict=True, evidence=s.evidence,
                                     revalidate=s.revalidate)
    assert isinstance(result, Err) and result.error.code == ErrorCode.dependency_incompatible
    assert s.numeric_signals == s.pidfd_signals == []
```

### Implementation steps

- [ ] Add strict_signal_observer with independent numeric-signal and pidfd-send hooks plus descriptor-close and event-order recording. Add the exact named tests and confirm a mechanism-specific failing run.
- [ ] Define stage completeness: pre_bind requires exact child identity/UID/daemonize+socket argv and live recorded-supervisor parent, with null entry/kernel inode; bound requires full anchored endpoint evidence and entry/kernel inode, retaining supervisor when the source has one.
- [ ] Reject incomplete strict input/callback before signalling; classify proved absence separately from unreadable. For a live exact candidate require pidfd availability, then re-read PID/ticks/UID after opening and invoke the synchronous same-stage callback.

```python
# After opening pidfd and checking fresh PID/start_ticks/UID:
checked = revalidate()
if not isinstance(checked, Ok):
    return checked
if checked.value != evidence:
    return Err(error=diagnostic(ErrorCode.ownership_mismatch))
# Descriptor cleanup is in finally; only this surviving path may send signals.
```

- [ ] Compare stable stage/identity/UID/socket/supervisor/entry/kernel facts; a stage change or any callback refusal closes pidfd with zero signals. Never pass a receipt into the process port or treat a pre-open callback as post-open revalidation.
- [ ] Send TERM through pidfd, observe exact exit, escalate KILL through the same descriptor if necessary and honor delivered kill_timeout plus the original deadline; non-child release need not waitpid. Strict failure never enters numeric fallback.
- [ ] Implement CleanupOutcomeWriter using immutable record replacement after checking identity/reason/intent_at and completion state; actual persistence remains the store's atomic operation. Its input is an outcome, never a third pre-signal intent. No filesystem deletion or success inference from record existence.
- [ ] Run M4/M5 with unchanged controls, no-spawn outcome test and delivered non-owned termination controls. Commit with `feat(process): require stage-revalidated pidfd owned teardown`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/process/test_linux_runner.py tests/process/test_teardown_intents.py --basetemp /tmp/oe4 -q`.
**Exit evidence:** post-open callback is observed, strict no-pidfd never signals, exact exit is bounded, outcome writing cannot grant teardown and non-owned callers retain optional fallback.

## Task 5: Serialize shared-root writers and preserve durable authority ownership

**Implementer tier:** Frontier
**Lane:** launch
**Depends on:** 2, 3, 4, 1

**Files:**

- Create: `src/airplay_linux/process/authority.py` — owner DTO, SharedRootAuthority and AuthorityLease.
- Modify: `src/airplay_linux/process/daemon_registry.py:1-505` — required public construction seams, once-per-operation lease and locked reconciliation.
- Create: `tests/process/test_authority.py`.
- Test: `tests/process/test_daemon_registry.py`.

**Interfaces:**

- Re-exports canonical `AuthorityOwner(schema: Literal['airplay-linux/daemon-authority-owner/v1'], instance_uuid: InstanceId, app: ProcessIdentity, boot_id: str, acquired_at: float)` from domain.records; exact five required durable members, identical constructor/type used by the Task 2 codec.
- Produces `SharedRootAuthority.acquire(self, instance: AuthorityOwner) -> Awaitable[Result[AuthorityLease]]`, `SharedRootAuthority.clear_owner(self, expected: AuthorityOwner) -> Awaitable[Result[None]]`, and `AuthorityLease.unlock(self) -> None`; lease fields are owner, _lock_fd, _authority, _unlocked=False, handles/owner repr=False.
- Extends registry public construction with required named seams `app_instance_uuid: InstanceId`, `receipts: LaunchReceiptStore`, `cleanup_intents: DaemonCleanupIntentStore`, `supervisor_intents: Callable[[str], SupervisorReleaseIntentStore]`, `authority: SharedRootAuthority`, `launcher: LaunchCapability`, `ownership: OwnershipAuthority`; retain delivered process/store/factory/root/clock/ID/scheduler/restart arguments and fixture defaults.
- Produces `async _reconcile_locked(self) -> Result[None]` behind `async ensure(self) -> Result[DaemonRecord]`, `restart(self) -> Result[DaemonRecord]`, `reconcile_previous(self) -> Result[None]`, `shutdown(self) -> Result[None]`, preserving public signatures and closed production selection.
- Consumes Task 2 store `write(record: LaunchReceipt) -> Awaitable[Result[None]]`/`all() -> Awaitable[Result[tuple[LaunchReceipt, ...]]]`, cleanup's own `write(record: DaemonCleanupIntent) -> Awaitable[Result[None]]`/`all() -> Awaitable[Result[tuple[DaemonCleanupIntent, ...]]]`, supervisor `write(record: SupervisorReleaseIntent) -> Awaitable[Result[None]]`/`read() -> Awaitable[Result[SupervisorReleaseIntent | None]]`, and `encode_authority_owner(record: AuthorityOwner) -> bytes`/`decode_authority_owner(payload: bytes) -> AuthorityOwner`.
- Consumes Task 3 `verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]`, `OwnershipAuthority.allows(self, operation: BackendOperation) -> bool`, `prove(self, record: DaemonRecord) -> Awaitable[Result[OwnershipProof]]`, `authorize(self, record: DaemonRecord, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Awaitable[Result[OwnershipProof]]`, `confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Awaitable[Result[None]]`, `release(self, record: DaemonRecord) -> Awaitable[Result[None]]`.
- Consumes Task 1 launcher `production_graph: bool`, `available(self) -> bool`, `argv(self, socket_path: str) -> tuple[str, ...]`, `ambient_socket(self) -> str | None`; Task 4 `ProcessPort.terminate(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Awaitable[Result[None]]`. CleanupOutcomeWriter has `record(self, intent: DaemonCleanupIntent) -> Awaitable[Result[None]]`, `complete(self, intent: DaemonCleanupIntent, *, completed_at: float, outcome: Literal['stopped'] | ErrorCode) -> Awaitable[Result[None]]`; no bare-PID release is introduced.

### Required tests and assertions

- Add `tests/process/test_authority.py::test_shared_flock_serializes_owner_publication` (M6): two genuinely independent authorities/registries and a publication barrier; at most one launch, loser ownership_mismatch; removing flock permits both to reach publication.
- Add `tests/process/test_authority.py::test_owner_survives_unlock_and_blocks_second_instance` and `tests/process/test_authority.py::test_stale_clear_owner_cannot_remove_successor`.
- Add `tests/process/test_authority.py::test_flock_deadline_and_backoff_map_concurrent_writer`: one 5.0 s clock budget, 0.05 s retry, no reset; fixed expiry mapping and zero side effects.
- Add `tests/process/test_authority.py::test_authority_owner_codec_rejection_matrix`: valid five-member control and each malformed category; malformed ownership never permits replacement/teardown.
- Add `tests/process/test_daemon_registry.py::test_shutdown_acquires_once_and_cancellation_unlocks`: internal reconciliation, no nested lease, unresolved owner retained.
- Add `tests/process/test_authority.py::test_unresolved_owner_blocks_production_launch`: closed runtime boundary plus independent registry ownership refusal, zero production direct/namespace launches. Rerun the shared phase-guard proof.

### Test kernel

two_registry_barrier exposes independent authority_a/authority_b, live distinct owner_a/owner_b, shared owner_path and launch_calls. Its valid owner_a document has already been durably published under the actual authority lock as an admitted isolated launch; observation counters start after that setup. Tests use actual files/flock with injected identity evidence; no in-process substitute owns the lock.

```python
from airplay_linux.domain.resources import Ok, Err, ErrorCode

async def test_owner_survives_unlock_and_blocks_second_instance(two_registry_barrier):
    c = two_registry_barrier
    first = await c.authority_a.acquire(c.owner_a)
    assert isinstance(first, Ok)
    before = c.owner_path.read_bytes()
    first.value.unlock()
    first.value.unlock()
    assert c.owner_path.read_bytes() == before
    second = await c.authority_b.acquire(c.owner_b)
    assert isinstance(second, Err) and second.error.code == ErrorCode.ownership_mismatch
    assert c.launch_calls == []
```

### Implementation steps

- [ ] Add barrier/fake-clock/owner-succession tests; confirm RED on missing cross-process authority, not an in-process lock artifact. Use separate open lock descriptors and registry instances under one temporary root.
- [ ] Implement authority.lock O_NOFOLLOW open and flock(LOCK_EX|LOCK_NB), refusing reentry; use monotonic deadline=now+5.0 with min(0.05, remaining) backoff. Map CONCURRENT_WRITER_DETECTED to ownership_mismatch on this path.
- [ ] Read authority.owner while locked; different proved-live owner or unreadable PID/boot/owner evidence refuses. Provably absent owner permits only locked reconciliation of its retained endpoint obligations, never blind owner replacement.
- [ ] Atomic owner publication occurs when launch is admitted. Preserve owner bytes across ordinary idempotent unlock; finally releases only the local flock on success, refusal, exception or cancellation.
- [ ] Implement compare-clear under the held matching lease only after proved daemon/endpoint absence; compare exact expected owner, and make completed clear/cancellation incapable of deleting a successor.
- [ ] Wrap registry ensure/restart/reconcile/shutdown once each; shutdown calls _reconcile_locked directly while locked. Derive unique current/max generation from all receipt states and retain all ambiguity/failed/supervisor-intent obligations.
- [ ] Integrate foundation types and owner codec directly against the canonical DTO; retain strict legacy/new separation. Keep public production activation closed; a unit fake launcher cannot make the shipped factory select a public daemon.
- [ ] Run M6 and owner lifetime/deadline/cancellation controls, storage owner rejection and phase no-spawn tests. Commit with `feat(authority): serialize daemon ownership across applications`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/process/test_authority.py tests/process/test_daemon_registry.py tests/acceptance/test_no_production_spawn.py --basetemp /tmp/oe5 -q`.
**Exit evidence:** flock falsification is detected; owner survives unlock; stale clear cannot remove a successor; cancellation preserves obligations; no production activation.

## Task 6: Build the isolated idle launcher and every crash/release workflow

**Implementer tier:** Frontier
**Lane:** launch
**Depends on:** 5

**Files:**

- Modify: `src/airplay_linux/process/launcher.py` — complete idle argv adapter, retaining captured pure eligibility.
- Modify: `src/airplay_linux/process/daemon_registry.py` — receipt state machine, durable observation joins, ready ordering and cleanup/recovery.
- Modify: `src/airplay_linux/process/supervisor.py:1-380` — dormant public child/intent workflow and delivered fixture branch.
- Create: `tests/process/test_owned_launcher.py`.
- Test: `tests/process/test_daemon_registry.py`, `tests/process/test_supervisor.py`.

**Interfaces:**

- Consumes `LaunchCapability.argv(self, socket_path: str) -> tuple[str, ...]`; public argv is exactly `(executable, '-daemonize', '-socket', socket_path, '-cred-backend', 'keyring')`, no receiver/capture/secret. Fixture foreground argv stays separate.
- Implements registry `async _launch(self) -> Result[DaemonRecord]`, `_stop(self, record: DaemonRecord) -> Result[None]`, `_reconcile_locked(self) -> Result[None]`; prepares authority-backed lifecycle internals while Task 9 owns switching every public admission/consumer site.
- Prepares `async _authenticate(self, record: DaemonRecord, *, allow_partial: bool = False, release: bool = False) -> Result[OwnershipProof | None]`: prove/status for readiness; release delegates/awaits authority.release and returns Ok(None), never a fabricated proof. Production selection remains closed.
- Implements supervisor `run(self) -> int`, `async _run(self) -> int`, `async _request_release(self, record: DaemonRecord, features: BackendFeatures) -> Result[None]`, `async _publish_release_intent(self, record: DaemonRecord, reason: Literal['application-exited','parent-mismatch']) -> Result[SupervisorReleaseIntent]` in a dormant public branch.
- Consumes receipt `write(self, record: LaunchReceipt) -> Awaitable[Result[None]]`, `all(self) -> Awaitable[Result[tuple[LaunchReceipt, ...]]]`; cleanup `write(self, record: DaemonCleanupIntent) -> Awaitable[Result[None]]`, `all(self) -> Awaitable[Result[tuple[DaemonCleanupIntent, ...]]]`; supervisor `write(self, record: SupervisorReleaseIntent) -> Awaitable[Result[None]]`, `read(self) -> Awaitable[Result[SupervisorReleaseIntent | None]]`.
- Consumes `SharedRootAuthority.acquire(self, instance: AuthorityOwner) -> Awaitable[Result[AuthorityLease]]`, `clear_owner(self, expected: AuthorityOwner) -> Awaitable[Result[None]]`, `AuthorityLease.unlock(self) -> None`; registry construction requires `app_instance_uuid: InstanceId`, `receipts: LaunchReceiptStore`, `cleanup_intents: DaemonCleanupIntentStore`, `supervisor_intents: Callable[[str], SupervisorReleaseIntentStore]`, `authority: SharedRootAuthority`, `launcher: LaunchCapability`, `ownership: OwnershipAuthority`.
- Consumes OwnershipAuthority `allows(self, operation: BackendOperation) -> bool`, `prove(self, record: DaemonRecord) -> Awaitable[Result[OwnershipProof]]`, `authorize(self, record: DaemonRecord, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Awaitable[Result[OwnershipProof]]`, `confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Awaitable[Result[None]]`, `release(self, record: DaemonRecord) -> Awaitable[Result[None]]`; `verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]`.
- Consumes `ProcessPort.terminate(self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Awaitable[Result[None]]`; CleanupOutcomeWriter `record(self, intent: DaemonCleanupIntent) -> Awaitable[Result[None]]`, `complete(self, intent: DaemonCleanupIntent, *, completed_at: float, outcome: Literal['stopped'] | ErrorCode) -> Awaitable[Result[None]]`.

### Required tests and assertions

- Add `tests/process/test_owned_launcher.py::test_pre_bind_verification_failure_observes_exact_child_death`, `tests/process/test_owned_launcher.py::test_pre_bind_unreadable_identity_retains_without_signals_or_replacement`, `tests/process/test_owned_launcher.py::test_bound_mismatch_refuses_and_retains_cleanup_obligation` (M7).
- Add `tests/process/test_owned_launcher.py::test_failed_receipt_survives_cleanup_retry_and_restart` (M8) and `tests/process/test_owned_launcher.py::test_entry_replacement_during_first_bind_never_becomes_ready`.
- Add `tests/process/test_owned_launcher.py::test_stopping_from_reserved_and_failed_persists_intent_before_signal` and `tests/process/test_owned_launcher.py::test_each_actual_crash_boundary_retains_safe_outcome`.
- Add `tests/process/test_daemon_registry.py::test_receipt_joins_application_and_observed_daemon_instance_without_token`, `tests/process/test_daemon_registry.py::test_restart_reconstructs_unique_observation_gap_without_config_pipe`, `tests/process/test_daemon_registry.py::test_observation_join_refuses_ambiguous_restart_records`.
- Add `tests/process/test_daemon_registry.py::test_missing_cleanup_intent_is_rederived_without_authorising_teardown` and `tests/process/test_daemon_registry.py::test_supervisor_intent_recovery_terminalises_without_resignalling`.
- Add `tests/process/test_supervisor.py::test_public_config_and_fixture_record_schema_preserve_identity_channels` and `tests/process/test_supervisor.py::test_delivered_daemon_record_codec_compatibility_control`.
- Add `tests/process/test_supervisor.py::test_failed_launch_release_after_app_death_records_cleanup_intent_only` and independent `tests/process/test_supervisor.py::test_ready_app_death_publishes_release_intent_observes_death_and_recovers_terminal_receipt`.
- Add `tests/process/test_supervisor.py::test_detached_and_bound_app_death_publish_release_intent_before_signal` and `tests/process/test_supervisor.py::test_supervisor_release_intent_requires_app_absence_and_matching_own_daemon`.
- Add `tests/process/test_supervisor.py::test_release_intent_unavailable_refuses_without_signals_and_exits` and `tests/process/test_supervisor.py::test_supervisor_observation_alone_is_not_release_intent`.
- Retain/run `tests/process/test_supervisor.py::test_killing_supervisor_cannot_leave_a_daemon_that_ignores_term`; classify its known flake with actual output, preserving its assertions.
- Add `tests/process/test_owned_launcher.py::test_isolated_launcher_is_unreachable_from_guarded_production`: unit launcher success is separate from real-runtime phase-guard zero direct/namespace launches.

### Test kernel

reopen_receipt_store provides failed_path and an async drive(action: str) method mapping the listed actions to real registry/store operations with independently injected exact absence. Each reopen constructs fresh repositories/registry; drive never edits receipt bytes itself.

```python
async def test_failed_receipt_survives_cleanup_retry_and_restart(reopen_receipt_store):
    c = reopen_receipt_store
    before = c.failed_path.read_bytes()
    for action in ('publish_intent', 'complete_intent', 'retry_cleanup', 'reopen_registry'):
        await c.drive(action)
        assert c.failed_path.read_bytes() == before
```

### Implementation steps

- [ ] Add candidate_failure_cases, reopen_receipt_store, application_daemon_identity_cases, bind replacement barrier and ten actual crash-boundary fixtures. Fail at missing lifecycle behavior before writing implementation.
- [ ] Under both locks, publish reserved before spawn with max(all generations)+1; observe supervisor's exact direct backend child and copy only the durable observation's daemon UUID into detached receipt. Live launch pipe must match launch_uuid/generation; restarted correlation exclusively enumerates app/runtime/{starting,running,stopping} records.
- [ ] Reopen at the null-daemon-UUID gap with no pipe: exactly one fresh valid observation reconstructs identities; zero/multiple including stale competitors refuses. Supplying stale/conflicting pipe bytes cannot change restart outcome. Preserve DaemonRecord codec/schema and distinguish all three identity channels.
- [ ] Publish bound only after anchored peer/row/FD/ticks/UID/argv/entry proof. Read accepted idle, confirm current proof/child, publish ready atomically, then recheck exact child/current proof after the write before emitting ready. Failure/death never exposes ready.
- [ ] Exercise crashes at reservation write, spawn, supervisor publication, bind, receipt write, status poll, stopping write, signal, exit proof and unlink, plus cleanup-intent publication/completion and death during ready. Record the real boundary reached in each test.
- [ ] Before launch-failure signals, registry writes stopping while receipt is nonterminal; defer failed until exact cleanup succeeds or refuses. Proved exact pre-bind child cleanup observes death; unreadable and bound mismatch send no signal/unlink and block replacement. All five stopping origins retain evidence, and failed never rewrites to stopping.
- [ ] Implement supervisor app-absence/parent-mismatch predicates and independent pre-bind/bound proof; only its matching own observation and eligible reserved/detached/bound/ready/no-receipt origins permit atomic release-intent.json. Live/unreadable app, wrong joins or stopping/terminal origin refuses new publication.
- [ ] Public config keeps protocol_version/record_root/executable/features and adds launch_uuid/endpoint_generation; include/consume owner_token only for fixture authority. Supervisor writes only delivered observations plus its one immutable intent, never a receipt.
- [ ] On any intent publication-step failure send zero signals, publish cleanup_required with reason intent-unavailable, return typed refusal and exit retaining parent-death protection. A later observed parent-death cleanup cannot convert refusal to success.
- [ ] Recover supervisor intent under shared lease: record cleanup outcome, stopped on proved absence or failed plus obligation otherwise; retain identical intent file and terminal bytes; never re-signal, fabricate missing reservation fields or treat stopping observation/outcome intent as signal permission.
- [ ] Use strict same-stage post-open callbacks for every owned release, exact exit and anchored same-entry cleanup before compare-clearing owner. Retry/reopen preserves failed receipt bytes and all uncertainty; target Stop never enters whole-daemon teardown.
- [ ] Validate M7/M8, healthy-ready versus failed controls and guard no-spawn. Commit with `feat(launch): stage owned idle daemon lifecycle and crash recovery`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/process/test_owned_launcher.py tests/process/test_daemon_registry.py tests/process/test_supervisor.py tests/acceptance/test_no_production_spawn.py --basetemp /tmp/oe6 -q`.
**Exit evidence:** actual crash windows have safe observed outcomes; failed/terminal evidence persists; durable supervisor intent precedes actual signals; recovery never signals; production still guarded.

## Task 7: Prepare complete source production, fresh inventory and wired verification

**Implementer tier:** Frontier
**Lane:** source
**Depends on:** 1

**Files:**

- Modify: `vendor/doubletake/0001-capture-report-selected-source.patch`, `vendor/doubletake/0002-status-report-selected-source.patch`, `vendor/doubletake/0003-capability-tokens.patch` — revised complete producer/stream propagation/third token in order.
- Modify: `src/airplay_linux/backend/contract.py` — BackendSource/BackendStream.source, strict decode and complete recognition.
- Create: `src/airplay_linux/display/inventory.py`, `src/airplay_linux/capture/source.py`.
- Modify: `src/airplay_linux/display/kscreen.py:1-1179`, `src/airplay_linux/capture/physical.py:1-427` — shared parsing/whitelist and connector projection.
- Modify: `src/airplay_linux/transport/doubletake.py:1-892`, `src/airplay_linux/runtime.py` — same verifier object wiring, behind closed production activation.
- Modify: `docs/backend-fork.md:1-108` — all three build steps and revised producer evidence, explicitly not an activation claim.
- Test: `tests/backend/test_contract.py`; create `tests/backend/test_carried_source_patch.py`, `tests/display/test_inventory.py`, `tests/capture/test_reported_source.py`, `tests/runtime/test_owned_source.py`.

**Interfaces:**

- Produces `BackendFeatures.source_reporting_complete: bool = False`, `BackendSource(type: int, width: int, height: int, x: int, y: int)` all required, `BackendStream.source: BackendSource | None = None`, and `_decode_source(value: object) -> BackendSource | Err`; Task 1 already supplies the false feature default, this task implements recognition.
- Produces `MonitoredOutput(name: str, enabled: bool, connected: bool, output_type: int | str | None, size: Size | None, position: tuple[int,int] | None, virtual: bool | None)`; all fields required, missing geometry explicit.
- Produces `KScreenInventory.__init__(self, process: ProcessPort, *, timeout: float = 5.0) -> None`, `async observe(self) -> Result[tuple[MonitoredOutput, ...]]`; each observation performs exactly one kscreen-doctor -j.
- Produces ReportedSourceVerifier with `physical_inventory_available: bool`, `async verify(self, requested: str, reported: BackendSource | None) -> Result[None]`; KScreenReportedSourceVerifier holds inventory plus affirmative physical inventory availability.
- Produces `verify_reported_source(requested: str, reported: BackendSource, layout: tuple[MonitoredOutput, ...]) -> Result[None]` and the same-provider KscreenConnectorInventory projection preserving delivered connector interface.
- Extends `DoubleTakeTransport.__init__(self, control_factory: Callable[[DaemonRecord], BackendControl], registry: DaemonRegistry, capture: CapturePort, credentials: CredentialStore, receivers: ReceiverStore, fences: TrustFenceRegistry, features: BackendFeatures, clock: Clock, scheduler: Scheduler, ids: IdFactory, *, source_verifier: ReportedSourceVerifier | None = None) -> None`.
- Consumes `derive_composition_capabilities(probed: BackendFeatures, launcher: LaunchCapability, credential_proof: CredentialProofVerdict, source_verifier: ReportedSourceVerifier | None, ambient_socket: str | None) -> BackendFeatures`; source_verifier must be the exact instance supplied to transport, not an independently fabricated positive feature.

### Required tests and assertions

- Add `tests/backend/test_contract.py::test_source_position_must_be_present_and_integral` and `tests/backend/test_carried_source_patch.py::test_complete_token_requires_complete_producer`.
- Add `tests/display/test_inventory.py::test_single_fresh_inventory_supplies_geometry_origin_and_connector_projection`.
- Add `tests/capture/test_reported_source.py::test_same_size_other_position_is_not_requested_source`, `tests/capture/test_reported_source.py::test_enabled_foreign_or_virtual_geometry_collision_refuses`.
- Add `tests/capture/test_reported_source.py::test_disabled_geometry_match_does_not_create_ambiguity`, `tests/capture/test_reported_source.py::test_enabled_disconnected_or_unknown_geometry_collision_refuses`.
- Add `tests/capture/test_reported_source.py::test_negative_coordinate_physical_layout_is_accepted`, `tests/capture/test_reported_source.py::test_stale_unreadable_or_unknown_origin_layout_refuses`.
- Add `tests/runtime/test_owned_source.py::test_source_preparation_cannot_activate_production`: actual wired verifier/complete reporting still records zero backend/unshare dispatch with the closed phase guard. Rerun M1 and shared phase-guard proof.

### Test kernel

The source decoder uses the exact Go nested keys; no fixture or requested connector can fill a missing coordinate. Extend this body with every malformed category and independent Y omissions.

```python
from airplay_linux.backend.contract import BackendSource, _decode_source
from airplay_linux.domain.resources import Err

def test_source_position_must_be_present_and_integral():
    raw = dict(Type=1, Width=1920, Height=1080, X=0, Y=-1080)
    assert _decode_source(raw) == BackendSource(type=1, width=1920, height=1080, x=0, y=-1080)
    for axis in ('X', 'Y'):
        missing = {k: v for k, v in raw.items() if k != axis}
        assert isinstance(_decode_source(missing), Err)
        for bad in (None, True, False, '0', 0.5):
            assert isinstance(_decode_source({**raw, axis: bad}), Err)
```

### Implementation steps

- [ ] Write complete_source_documents, portal_source_property_cases, single_inventory_read and fresh_layout_cases; include explicit zero/negative coordinates, unknown/disconnected/virtual enabled collisions and disabled duplicates. Observe the intended RED failures.
- [ ] Revise 0001 to require source type, positive size and explicitly present integral position before usable source; type-only/size-only/missing position is unusable, never fabricated (0,0). Preserve 0002 per-stream propagation and align it if required by producer representation; revise 0003 to advertise status-source-complete only with complete producer.
- [ ] Decode nested wire source keys exactly Type/Width/Height/X/Y, exact int excluding bool, type 1..2**32-1 and positive dimensions; unknown/partial/null/malformed source returns dependency_incompatible. An absent stream source remains None. Complete positive nonmonitor types may decode but verification rejects them.
- [ ] Recognize patched help plus successful capabilities-v1/status-source/status-source-complete together; nonzero command exit, partial tokens or wrong surface cannot assert completeness. Preserve two-token source_reporting, but that build cannot enable Mirror.
- [ ] Parse one fresh KScreen document for geometry and origin, reusing delivered signed geometry and exact-true flag rules. Share PHYSICAL_OUTPUT_TYPES={1,2,3,4,5,6,7,14} and delivered textual/name rules; retain compatibility exports. Connector projection needs no geometry to preserve delivered capture inventory behavior.
- [ ] For verification exclude disabled entries only; enabled entries of any type/origin/connection count before requested-name/physical checks. Missing enabled geometry refuses complete comparison. Require exactly one matching size+signed-position, requested name, connected and known physical type; reject stale/unreadable/window/region/unknown evidence as unsupported_source.
- [ ] Wire one shared inventory provider and the actual verifier instance through runtime/transport while retaining the activation guard. Do not substitute display.kscreen's broader physical heuristic or cached requested geometry for observation.
- [ ] Prepare an offline local patch-chain rebuild in a disposable source copy if cached upstream/dependencies are available: apply 0001→0002→0003, build with GOPROXY=off/GOSUMDB=off/GOTOOLCHAIN=local, record source revision, patch SHA-256 values, exact build command and binary SHA-256. Never fetch or overwrite /home/bin here; absence of a verifiable revised build remains a named activation precondition.
- [ ] Update backend-fork build instructions to include all three patches and two-token refusal; carry concrete local build identity when obtained, otherwise retain the gate. Task 9 publishes activation disclosure only after this complete-source evidence is verified.
- [ ] Validate source-specific mutations and no-spawn control. Commit with `feat(source): prepare complete physical source verification`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/backend/test_contract.py tests/backend/test_carried_source_patch.py tests/display/test_inventory.py tests/capture/test_reported_source.py tests/runtime/test_owned_source.py tests/acceptance/test_no_production_spawn.py --basetemp /tmp/oe7 -q`.
**Exit evidence:** producer/decoder/inventory agree on complete signed geometry; all enabled collisions refuse; same verifier is wired; complete-source preparation cannot activate production.

## Task 8: Prove credential service through one isolated file-backed witness

**Implementer tier:** Frontier
**Lane:** probe
**Depends on:** 5, 7

**Files:**

- Create: `src/airplay_linux/process/credential_probe.py` — ProbeConfig/ProbeVerdict, CredentialServiceProbe, observable child run seam and production main.
- Modify: `src/airplay_linux/runtime.py` — exact ordered bootstrap/proof orchestration before registry, guard still closed.
- Modify: `src/airplay_linux/testing/adapters.py` — inner/outer process recorders and deterministic witness support, retaining Task 4 terminate keywords.
- Create: `tests/process/test_credential_probe.py`, `tests/runtime/test_owned_probe.py`.
- Test: `tests/acceptance/test_no_production_spawn.py` — extend existing direct/namespace observation test with the full witness and single-leg controls.

**Interfaces:**

- Produces `ProbeConfig(probe_uuid: UUID, reservations: tuple[DaemonReservation, DaemonReservation], backend_executable: str, record_root: Path)` ordered positive/negative, distinct launch UUIDs and leg-private paths, no credential/token/ambient environment payload.
- Re-exports canonical `ProbeVerdict(schema: Literal['airplay-linux/credential-service-proof'], version: int, service_leg: Literal['passed','failed','not_run'], backend_positive_leg: Literal['passed','failed','not_run'], backend_negative_leg: Literal['passed','failed','not_run'], failure: ErrorCode | None)` from domain.records, with version=1 and exactly those six wire members.
- Produces `CredentialServiceProbe.check(self) -> Awaitable[Result[CredentialProofVerdict]]` and synchronous child seam `run(config: ProbeConfig, *, process: ProcessPort, namespace: Callable[[tuple[str, ...]], Result[ProcessIdentity]]) -> ProbeVerdict`; main supplies real dependencies and calls this same seam.
- Consumes Task 1 transient CredentialProofVerdict and pure derive_composition_capabilities exact signature; Task 7 actual ReportedSourceVerifier/physical_inventory_available; Task 2 JsonLaunchReceiptStore(files: AtomicFiles) write/all plus `encode_probe_verdict(record: ProbeVerdict) -> bytes`, `decode_probe_verdict(payload: bytes) -> ProbeVerdict`.
- Consumes `verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]`, OwnershipAuthority `allows(self, operation: BackendOperation) -> bool`, `prove(self, record: DaemonRecord) -> Awaitable[Result[OwnershipProof]]`, `authorize(self, record: DaemonRecord, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Awaitable[Result[OwnershipProof]]`, `confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Awaitable[Result[None]]`, `release(self, record: DaemonRecord) -> Awaitable[Result[None]]`, bound to a child-local receipt source without constructing application registry.
- Consumes strict `ProcessPort.terminate(identity, owner_uuid, runtime_directory, deadline, *, strict=False, evidence: TerminationEvidence | None=None, revalidate: Callable[[], Result[TerminationEvidence]] | None=None) -> Awaitable[Result[None]]`; child writer supplies stopping receipt and same-stage closure before strict teardown. Application shared authority is an installed phase prerequisite but is never opened by the child.

### Required tests and assertions

- Add `tests/process/test_credential_probe.py::test_probe_verdict_codec_rejection_matrix`, `tests/process/test_credential_probe.py::test_probe_evidence_and_teardown_stay_in_child_namespace`.
- Add `tests/process/test_credential_probe.py::test_probe_run_seam_observes_outer_unshare_and_inner_backend_launch`: unchanged real production graph under a test-local activation-guard removal records exactly one outer namespace invocation and two direct inner launches, including actual negative env/completion/absences.
- Add `tests/process/test_credential_probe.py::test_each_backend_probe_leg_has_exactly_one_inner_launch`: positive/negative standalone helper subtests each record exactly one direct launch and distinct matching receipt; these do not substitute for the full two-leg control.
- Add `tests/process/test_credential_probe.py::test_verdict_file_requires_dirfd_digest_exact_size_and_zero_exit`, `tests/process/test_credential_probe.py::test_probe_bootstrap_precedes_registry_and_does_not_require_keyring`.
- Add `tests/process/test_credential_probe.py::test_probe_socket_idle_confirmation_and_termination_bounds` with a fake clock and independent positive/negative failures.
- Add `tests/runtime/test_owned_probe.py::test_installed_probe_stays_closed_before_activation`: unchanged production guard still yields zero outer/inner launches even with all probe/source/lifecycle seams installed. Extend Task 1 recorder classification through actual child run injection.

### Test kernel

recording_probe_processes exposes async build_complete_production_and_probe(), outer_unshare, inner_launches, and each launch's leg/launch_uuid/env/exit_code/socket_exists/credentials_file_exists. The builder uses real build_runtime with injected final port and calls the real synchronous child run seam in its own child/test thread; no nested event-loop replacement or fabricated witness.

```python
async def test_probe_run_seam_observes_outer_unshare_and_inner_backend_launch(recording_probe_processes):
    c = recording_probe_processes
    await c.build_complete_production_and_probe()
    assert len(c.outer_unshare) == 1 and len(c.inner_launches) == 2
    positive, negative = c.inner_launches
    assert (positive.leg, negative.leg) == ('positive', 'negative')
    assert positive.launch_uuid != negative.launch_uuid
    assert negative.env['DBUS_SESSION_BUS_ADDRESS'] == c.unreachable_bus
    assert negative.exit_code != 0
    assert not negative.socket_exists and not negative.credentials_file_exists
```

### Implementation steps

- [ ] Write namespace_probe_witness, recording_probe_processes, probe_verdict_documents/files, bootstrap_event_log and probe_fake_clock fixtures; confirm relevant failures. Use real production orchestration and injected process/bus/evidence boundaries; no alternate SessionRig graph.
- [ ] Install bootstrap in exact order: derivable owned control → wired source verifier → constructible receipt store+endpoint verifier+strict teardown → resolvable unshare. Before it, require the phase installation boundary's seven lifecycle seams including shared authority and credential-proof producer. Bootstrap never consults keyring or registry._compatible.
- [ ] Parent creates unique daemon/probe/<probe_uuid> mode=0700/no-follow/owned root and opens a dirfd before dispatching exactly one `unshare -rn <python> -m airplay_linux.process.credential_probe --socket <private-socket> --record-root <parent-created-probe-root>`. Production guard still blocks this call outside controlled tests until Task 9.
- [ ] In the namespace, service introspection occurs once with existing client only, no unlock/create. Positive backend has reachable bus, own reserved receipt/UUID, exact direct child, socket+idle within 3.0 s, 2.0 s idle confirmation, durable stopping and strict observed teardown within 3.0 s grace before negative begins.
- [ ] Negative is a fresh distinct UUID/receipt/path launch with unreachable DBUS_SESSION_BUS_ADDRESS, observed nonzero backend completion, no control socket and no credentials file. Route both actual direct invocations through the same injected inner ProcessPort; use spawn plus exact completion/subscribe evidence so an automatic nonstrict run-timeout cleanup cannot bypass the owned strict path. On a hung leg attempt cleanup only with valid same-stage evidence and stopping intent, otherwise retain uncertainty. Record leg/UUID/receipt/bus selection/completion only in test observations, never durable environment dumps.
- [ ] Keep all peer/row/FD/identity evidence and same-stage post-pidfd-open closures namespace-local. Child owns only its probe receipts, never application receipts/authority. Any setup/cleanup uncertainty retains root/files and yields no trusted successful proof.
- [ ] Publish strict verdict.json (1..4096 bytes) and verdict.sha256 (lowercase SHA-256 plus one newline, exactly 65 bytes) after witness/teardown; completed failed-leg verdict may be retained, child exits zero only after files fully publish. Negative backend nonzero is distinct from namespace-child zero exit.
- [ ] Parent verifies root owner/mode/UUID and relative O_NOFOLLOW regular owned files, exact st_size/read counts, read <=4097, digest, strict schema/leg consistency and zero child exit. Stdout/stderr never supplies a fallback. A well-formed failed verdict yields keyring false with named leg and retained evidence.
- [ ] On all-pass proof archive both verified receipts and verdict under archives/probe/<probe_uuid>; only successful archive publication permits root/file removal. Archive failure preserves originals. Only after proof handling derive capabilities once and construct registry; its delivered keyring requirement remains downstream.
- [ ] Kill negative-leg recorder bypass, stdout fallback, each root/file/digest/exit omission and deadline-reset mutations. Validate full witness plus each leg and unchanged no-spawn guard. Commit with `feat(credentials): add isolated durable credential service proof`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/process/test_credential_probe.py tests/runtime/test_owned_probe.py tests/acceptance/test_no_production_spawn.py --basetemp /tmp/oe8 -q`.
**Exit evidence:** one outer/two inner witness with negative actual environment/completion, file-only parent proof, namespace-local strict cleanup, probe before registry and unchanged production still disabled.

## Task 9: Activate every public consumer in one commit under Amendment 1

**Implementer tier:** Frontier
**Lane:** activation
**Depends on:** 6, 7, 8

**Files:**

- Modify: `src/airplay_linux/runtime.py:1-627`, `src/airplay_linux/backend/contract.py:1-918`, `src/airplay_linux/backend/control.py:1-945`, `src/airplay_linux/policy/capabilities.py:1-558`, `src/airplay_linux/transport/doubletake.py:1-892`, `src/airplay_linux/process/daemon_registry.py:1-505`, `src/airplay_linux/process/supervisor.py:1-380`.
- Modify: `README.md:1-65`, `docs/backend-fork.md:1-108` — guarantee/refusals/residuals and verified complete-source build identity.
- Test: `tests/backend/test_control.py`, `tests/transport/test_doubletake.py`, `tests/policy/test_capabilities.py`, `tests/process/test_daemon_registry.py`, `tests/acceptance/test_no_production_spawn.py`, `tests/process/test_credential_probe.py`.
- Create: `tests/acceptance/test_owned_daemon_epoch.py`.

**Interfaces:**

- Consumes/activates `derive_composition_capabilities(probed: BackendFeatures, launcher: LaunchCapability, credential_proof: CredentialProofVerdict, source_verifier: ReportedSourceVerifier | None, ambient_socket: str | None) -> BackendFeatures` exactly once after structural eligibility and file-verified proof, before registry construction; `CredentialServiceProbe.check(self) -> Awaitable[Result[CredentialProofVerdict]]` and `ReportedSourceVerifier.verify(self, requested: str, reported: BackendSource | None) -> Awaitable[Result[None]]` remain the exact installed probe/source seams.
- Consumes complete registry construction seams `app_instance_uuid: InstanceId`, `receipts: LaunchReceiptStore`, `cleanup_intents: DaemonCleanupIntentStore`, `supervisor_intents: Callable[[str], SupervisorReleaseIntentStore]`, `authority: SharedRootAuthority`, `launcher: LaunchCapability`, `ownership: OwnershipAuthority`; supervisor receives matching directory-bound intent store. Shared authority and evidence authority remain distinct.
- Activates `PublishedDoubletakeControl.__init__(self, process: ProcessPort, profile: PublishedDoubletakeProfile, *, record: DaemonRecord | None = None, authority: OwnershipAuthority | None = None, timeout: float = 5.0, clock: Clock | None = None) -> None`.
- Activates control `handshake(self, owner_token: SensitiveText | str, instance_uuid: InstanceId) -> Awaitable[Result[OwnershipProof]]`, `_revalidate_ownership(self, operation: BackendOperation, target: ReceiverTarget | None, fence: DispatchFence | None) -> Awaitable[Result[OwnershipProof]]`, `_ownership_check(self, status: StatusReply) -> Result[None]`.
- Activates `request(self, operation: BackendOperation | str, target: ReceiverTarget | None, payload: BackendPayload, context: OperationContext | None, *, dispatch_fence: DispatchFence | None = None, ownership_proof: OwnershipProof | None = None) -> Awaitable[Result[BackendReply]]` on BackendControl and concrete control; _available remains synchronous.
- Consumes OwnershipAuthority `allows(operation) -> bool`, `prove(record) -> Awaitable[Result[OwnershipProof]]`, `authorize(record, operation, target, fence) -> Awaitable[Result[OwnershipProof]]`, `confirm(record, proof) -> Awaitable[Result[None]]`, `release(record) -> Awaitable[Result[None]]`; DispatchFence remains Callable[[], Result[None]].
- Activates transport `_admit(self, target: ReceiverTarget, context: OperationContext) -> Awaitable[Result[_Daemon]]`, `_start(self, stream: _Stream) -> Awaitable[Result[StreamHandle]]`, `_request(self, daemon: _Daemon, operation: BackendOperation, target: ReceiverTarget, payload: BackendPayload, context: OperationContext, *, cleanup: bool = False) -> Awaitable[Result[BackendReply]]`, forwarding OwnershipProof and the original fence.
- Activates registry `_compatible(self) -> bool`, `_authenticate(self, record: DaemonRecord, *, allow_partial: bool = False, release: bool = False) -> Awaitable[Result[OwnershipProof | None]]` and supervisor `_run/_request_release` selection; strict ProcessPort.terminate signature/intent ordering are unchanged from spec §4.3.

### Required tests and assertions

- Add `tests/backend/test_control.py::test_connect_does_not_consult_each_former_pairing_prerequisite`, `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags` and `tests/backend/test_control.py::test_replacement_between_authorization_and_dispatch_refuses`.
- Add `tests/transport/test_doubletake.py::test_source_and_challenge_share_one_post_connect_deadline`, `tests/transport/test_doubletake.py::test_unconfirmed_start_disconnect_takes_cleanup_timeout_precedence`.
- Add `tests/process/test_daemon_registry.py::test_every_owned_termination_passes_strict_true`: registry stop/shutdown, evidence release, public supervisor and namespace probe all supply complete stage/callback; invoke after pidfd open and independently vary every stage fact.
- Add `tests/acceptance/test_no_production_spawn.py::test_missing_source_verifier_blocks_all_production_launches` (M9) and `tests/acceptance/test_no_production_spawn.py::test_each_missing_activation_element_blocks_launch`; update phase guard proof to preserve preactivation control and test complete activated positive control separately.
- Add `tests/acceptance/test_owned_daemon_epoch.py::test_public_shape_complete_graph_uses_evidence_authority`, `tests/acceptance/test_owned_daemon_epoch.py::test_public_status_without_legacy_metadata_is_accepted`.
- Add `tests/acceptance/test_owned_daemon_epoch.py::test_public_shape_target_controls_and_whole_shutdown_are_distinct`, `tests/acceptance/test_owned_daemon_epoch.py::test_changed_current_receipt_blocks_every_mutation_before_io`, `tests/acceptance/test_owned_daemon_epoch.py::test_wrong_fence_refuses_before_io`.
- Add `tests/acceptance/test_owned_daemon_epoch.py::test_connect_challenge_disconnects_only_exact_target`, `tests/acceptance/test_owned_daemon_epoch.py::test_each_missing_composition_prerequisite_is_named`.
- Add `tests/acceptance/test_owned_daemon_epoch.py::test_legacy_fixture_selects_handshake_authority`, `tests/acceptance/test_owned_daemon_epoch.py::test_crash_restart_requires_reconciliation_and_new_generation`.

### Predicate and test kernels

The pure EvidenceAuthority.allows connect branch below checks capability class only. Mutating control _available delegates to the selected authority; caller target/trust/fence validation and awaited authorization/confirmation remain mandatory. Pairing branches retain their own table from the steps below.

```python
# EvidenceAuthority.allows:
if operation is BackendOperation.connect:
    return (features.owned_daemon_control and features.multi_target
            and (features.physical_source or features.virtual_source))
# PublishedDoubletakeControl._available, mutating branch:
return (self._record is not None and self._authority is not None
        and self._authority.allows(operation))
```

public_production_graph exposes async connect(target, *, fence), target, valid_fence, and an independently mutable current-receipt source. authorization_dispatch_barrier pauses after authorization, changes only that source and records mutating_io, never replacing control.request.

```python
from airplay_linux.domain.resources import Err, ErrorCode

async def test_replacement_between_authorization_and_dispatch_refuses(authorization_dispatch_barrier):
    c = authorization_dispatch_barrier
    c.replace_after_authorize = True
    result = await c.connect()
    assert isinstance(result, Err) and result.error.code == ErrorCode.stale_event
    assert c.mutating_io == []
```

### Implementation steps

- [ ] Write public_shape_control/public_production_graph and pairing_operation_requirement_cases with public old/pairing flags false and all five legacy status ownership fields absent. Observe relevant RED failures while guard remains closed.
- [ ] Finish every spec §9 consumer in this one commit: policy ADMISSION_BACKEND_CAPABILITIES and EnvironmentProbe mode rubric; transport _admit/_start/_request; registry _compatible/_authenticate readiness+release; supervisor _run/_request_release; control _available(connect and other authority operations)/handshake/_revalidate_ownership/_ownership_check; runtime _control_factory/_capability_gates/startup detail; README guarantee/refusals.
- [ ] Public admission becomes owned_daemon_control and keyring with provider/trust/target fences unchanged. Select EvidenceAuthority only for installed public receipt-backed composition; select HandshakeAuthority only for a profile affirming owner_handshake. Missing record/authority refuses before I/O. Preserve fixture flags/comparisons without manufacturing public legacy metadata.
- [ ] Apply Amendment 1 §1 exactly: `owned_daemon_control and multi_target and (physical_source or virtual_source) and trust-not-blocked and dispatch-fence-valid`. Remove each of owned control, wired verifier/source, clean trust and valid fence independently upstream; each refuses before request I/O and without a new production spawn.
- [ ] Preserve every pairing predicate independently: begin=target_bound_pairing+cancel_pairing; submit/prepare=target_bound_pairing+keyring+provisional_commit; commit=keyring+provisional_commit; mark_invalid/revoke=revoke_pairing; cancel=cancel_pairing. Prove each with unrelated former-connect flags false, then remove each own prerequisite. Never add a shared all-pairing-flags superset; public pairing remains closed.
- [ ] Control request validates operation/payload/capabilities/fence, awaits authorize, synchronously compares proof/current receipt and reevaluates the original fence immediately at dispatch with no intervening application-owned await, performs I/O, then awaits confirm even on I/O refusal/error. Failed confirmation takes precedence; old proof never exposes ready or a usable reply.
- [ ] Public status accepts absent owner/instance/daemon/socket/protocol fields only under prove/confirm; fixture retains exact equalities. _authenticate(release=True) awaits strict authority release and returns Ok(None); shutdown never emits an unfenced external ctl command.
- [ ] After connect acknowledgement use one 5.0 s deadline and 0.1 s polling for exact-target complete source and credential challenge, never reset by alternating observations. Challenge in ack/status disconnects only that target, returns dependency_incompatible when confirmed and never prompts/submits/pairs/marks ready.
- [ ] Absent/mismatched/unverifiable source disconnects only exact target and returns unsupported_source when confirmed. Unconfirmed disconnect in either case takes cleanup_timeout precedence, retains stream/cleanup obligation, preserves other targets and respects delivered cancellation/target-replacement fences.
- [ ] At upstream installation boundaries remove each of all seven elements: ownership capability, receipt production, endpoint verifier, strict teardown, shared authority, credential-proof producer, wired source verifier. Each names the missing element and records zero outer/inner launches. Missing unshare separately blocks bootstrap; missing completed verdict is downstream witness failure, not a circular bootstrap requirement.
- [ ] Verify complete installed graph invokes probe once before registry, one outer/two inner actual launches, then derives keyring and checks registry compatibility. Downstream failed verdict retains attempted-launch observations and never admits streams. Real build_runtime process override must reach every final boundary.
- [ ] Require revised 0001→0002→0003 complete-source producer/decoder/verifier and verifiable local rebuild identity from Task 7 before activation. Publish exact build command/patch digests/binary identity in backend-fork; a current two-token binary remains gated. Prepared artifacts activate as one reviewed set in this commit.
- [ ] Publish cooperative single-user guarantee and five residuals: hostile same-UID out of scope; same-user entry/argv impersonation indistinguishable; reported geometry does not attest compositor pixels; receiver may later require re-pairing that app cannot service; remembered trust still does not survive restart. Startup names authority, all credential legs, verifier wiring/completeness/inventory, missing prerequisites and withheld pairing flags.
- [ ] Remove the closed production activation guard only after all named positive/omission/strict/source/challenge checks pass. Keep committed tests proving incomplete compositions never launch. Commit all owned activation files together with `feat(runtime): activate verified owned daemon Mirror composition`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/backend/test_control.py tests/transport/test_doubletake.py tests/policy/test_capabilities.py tests/process/test_daemon_registry.py tests/process/test_credential_probe.py tests/acceptance/test_no_production_spawn.py tests/acceptance/test_owned_daemon_epoch.py --basetemp /tmp/oe9 -q`.
**Exit evidence:** one activation commit covers all 17 spec §9 rows; public old flags/metadata remain false/absent; complete offline graph succeeds; each missing upstream element refuses before any new production/probe launch.

## Task 10: Establish default offline tripwires and kill the complete mutation matrix

**Implementer tier:** Frontier
**Lane:** acceptance
**Depends on:** 9

**Files:**

- Create: `tests/conftest.py` — default offline interception before resolution/client creation/connect.
- Create: `tests/acceptance/test_offline_tripwires.py`.
- Test: `tests/acceptance/test_owned_daemon_epoch.py`, `tests/acceptance/test_no_production_spawn.py` — strengthen final production-graph refusal/observation assertions without a second activation.
- Modify: `pyproject.toml:33-48` — register opt-in live capability markers only if the delivered marker list needs extension; retain existing pytest settings.

**Interfaces:**

- Consumes real `build_runtime(..., overrides={'process': recorder})`; final ProcessPort.run/spawn and inner `run(config: ProbeConfig, *, process: ProcessPort, namespace: Callable[[tuple[str, ...]], Result[ProcessIdentity]]) -> ProbeVerdict` are the observation seams, not fixture composition replacements.
- Consumes installed `derive_composition_capabilities(...) -> BackendFeatures`, `verify_owned_endpoint(receipt: LaunchReceipt, *, current_generation: int) -> Result[VerifiedEndpoint]`, `verify_reported_source(requested: str, reported: BackendSource, layout: tuple[MonitoredOutput, ...]) -> Result[None]`, and OwnershipAuthority prove/authorize/confirm/release exact §4 signatures.
- Consumes `ProcessPort.terminate(identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str, deadline: float, *, strict: bool = False, evidence: TerminationEvidence | None = None, revalidate: Callable[[], Result[TerminationEvidence]] | None = None) -> Awaitable[Result[None]]`; no production interface or gate changes in acceptance.
- Produces test-only interception for attempted live backend/ctl execution, session-bus-client construction and live socket leases before lookup/connect. Optional marker bypass requires explicit opt-in and exact capability skip text: `owned daemon control`, `credential service proof`, `physical source verification`.

### Required tests and assertions

- Add `tests/acceptance/test_offline_tripwires.py::test_unmarked_live_boundary_attempts_are_failures` (M10): actively attempt every prohibited boundary even with dependency absent; observe failure on attempt, never pass/skip from lookup failure.
- Retain all nine Task 9 public/fixture acceptance functions and the no-spawn seven-element/missing-verifier controls; no replacement of actual graph or negative-leg observations.
- Existing `tests/acceptance/test_no_production_spawn.py::test_each_missing_activation_element_blocks_launch` is this task's committed no-production-spawn proof; strengthen it if tripwire installation reveals an unobserved boundary.

### Test kernel

offline_boundary_tripwires exposes a refusal_type exception, an attempted list, and async attempts keyed below, each calling its real adapter entry under private temporary paths/unreachable bus/nonexistent backend locator. Interception logs/raises before resolution or connection; fixture absence and a removed interception cannot pass on dependency errors.

```python
import pytest

@pytest.mark.parametrize('boundary', ('backend', 'ctl', 'session_bus', 'socket_lease'))
async def test_unmarked_live_boundary_attempts_are_failures(offline_boundary_tripwires, boundary):
    c = offline_boundary_tripwires
    with pytest.raises(c.refusal_type):
        await c.attempts[boundary]()
    assert c.attempted == [boundary]
```

### Implementation steps

- [ ] Add active negative tripwire tests and confirm they fail when each interception is absent. Install default interception at execution/bus-client/socket-lease entry, before dependency resolution or connection; allow fake boundaries and explicitly marked opt-in cases only.
- [ ] Exercise public-shape launch/prove/idle/connect/source/ready, A/B target Stop, target mute/unmute, whole shutdown, crash restart and refusal matrix through production composition. Keep legacy HandshakeAuthority acceptance separate and require explicit selected-authority observations.
- [ ] In disposable copies run every M row below from a passing unchanged control; ensure copy imports resolve to that copy, not source via PYTHONPATH/pytest pythonpath. Record mutant, exact killing node, assertion and unchanged control outside the checkout. Discard each copy after verification.

| Mutation | Required named killing function(s) and observed failure |
| --- | --- |
| M1 | `tests/backend/test_contract.py::test_owned_control_is_composition_only`: raw ownership true must fail despite complete derived positive control. |
| M2 | `tests/storage/test_launch_receipts.py::test_receipt_generation_roundtrip_and_missing_member_refused`: lost generation/member fails round trip/refusal, including explicit-null daemon UUID. |
| M3 | `tests/process/test_ownership.py::test_different_socket_argv_refuses_before_control_or_signal`: changing only socket argv fails zero-I/O ownership assertion. |
| M4 | `tests/process/test_ownership.py::test_peer_start_ticks_must_equal_receipt`; `tests/process/test_linux_runner.py::test_strict_receipt_revalidation_after_pidfd_open_refuses_without_signals`; separate post-open identity-reuse control retained. |
| M5 | `tests/process/test_linux_runner.py::test_strict_no_pidfd_never_signals_numeric_pid`: numeric fallback removal guard is killed by an independent actual numeric-signal observer. |
| M6 | `tests/process/test_authority.py::test_shared_flock_serializes_owner_publication`: removal lets both independent instances reach publication, violating at-most-one launch. |
| M7 | `tests/process/test_owned_launcher.py::test_pre_bind_verification_failure_observes_exact_child_death`; `tests/process/test_owned_launcher.py::test_pre_bind_unreadable_identity_retains_without_signals_or_replacement`; `tests/process/test_owned_launcher.py::test_bound_mismatch_refuses_and_retains_cleanup_obligation`. |
| M8 | `tests/process/test_owned_launcher.py::test_failed_receipt_survives_cleanup_retry_and_restart`: deletion/rewrite fails exact failed-byte preservation through intent completion/retry/reopen. |
| M9 | `tests/acceptance/test_no_production_spawn.py::test_missing_source_verifier_blocks_all_production_launches`: removing only verifier exposes physical flag or a direct/namespace launch attempt. |
| M10 | `tests/acceptance/test_offline_tripwires.py::test_unmarked_live_boundary_attempts_are_failures`: bypassing each interception fails on a real attempted live boundary even with missing dependencies. |

- [ ] Kill additional source mutations using their Task 7 named tests: absent/nonintegral position, producer token without complete producer, sign loss, origin-first filtering, disabled counting, enabled disconnected/unknown exclusion, missing enabled geometry and separate inventory origin read.
- [ ] Kill receipt/generation/identity/recovery mutations using Task 2/3/6 named tests: max-from-current/recency/UUID; choose ambiguous receipt/observation; accept stale pipe or require pipe after restart; treat unreadable as absence; erase terminal bytes; allow observation/outcome as signal intent; omit atomic supervisor intent, terminal-origin or own-daemon/app predicate; re-signal recovery or fabricate missing receipt. Healthy-ready and failed-receipt supervisor controls remain independent.
- [ ] Kill every strict codec category in all six families with named matrices and the legacy-store compatibility function. Include routing legacy data through new strict decoding; boolean integers, duplicate/missing/extra members, partial states, filename mismatch and trailing whitespace cannot survive.
- [ ] Kill authority lifetime/deadline/cancellation mutations with Task 5 named tests and bootstrap/file-verdict/namespace/deadline/negative-leg recorder mutations with Task 8 named tests. The full unchanged two-leg witness must pass and a hidden negative process port must fail its exact full-witness test; single-leg tests alone are insufficient.
- [ ] Kill Amendment 1/dispatch/transport mutations with Task 9 named tests: consult any former connect pairing flag, add shared pairing superset or omit one own prerequisite, skip final receipt/fence check or post-I/O confirmation, require public legacy metadata, reset shared source/challenge deadline, clean another target, publish ready early or invert cleanup_timeout precedence.
- [ ] Confirm exactly all 82 spec §10 named module-qualified functions are collected once, including the existing supervisor-flake node. For optional markers preserve exact capability skip reasons; never convert an unmarked attempted live path or mutation survivor into a skip.
- [ ] Install the executing checkout into `~/.venvs/airplay-linux` offline with `~/.venvs/airplay-linux/bin/python -m pip install --no-index --no-deps --no-build-isolation -e .`; then run targeted acceptance and the required full suite once with the environment/short basetemp below. If the known flake occurs, retain its output and diagnose it without blanket retry, assertion weakening or false PASS. Broaden/repeat only for a concrete new failure or changed code.
- [ ] Verify no mutation driver/ledger, temporary secret/root, backend binary or external source checkout was added to git. Commit only owned test/config files with `test(acceptance): enforce offline owned daemon boundary proofs`.

**Validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest tests/acceptance/test_offline_tripwires.py tests/acceptance/test_no_production_spawn.py tests/acceptance/test_owned_daemon_epoch.py --basetemp /tmp/oe10 -q`.
**Required full-suite validation:** `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8 ~/.venvs/airplay-linux/bin/python -m pytest --basetemp /tmp/oeall -q`.
**Exit evidence:** default tests fail on live attempts, all named functions collect once, unchanged controls pass and every required mutant fails at its claimed assertion; actual full-suite/flake output is recorded without claiming live Mirror or pairing verification.
