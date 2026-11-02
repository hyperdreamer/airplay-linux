# Owned-daemon epoch — implementation specification

## 1. Scope, authority and provenance

This specification refines the approved owned-daemon design for Mirror against the locally patched `doubletake` 0.5.0 surface. Virtual mode, whole-screen inference and additional pairing authority are outside this epoch.
The thirteen reviewed rulings R1–R13 address findings F-001 through F-011, including the round-2 closures of F-005 and F-009, and are resolved elaborations in §12, normative throughout this specification; evidence or prerequisites that do not satisfy these contracts still fail closed.

| Priority | Authority | Binding effect |
| --- | --- | --- |
| 1 | `docs/superpowers/plans/2026-10-30-owned-daemon-control.md` | Phase order, P1/P2 and the production activation invariant; read with its amendment notice. |
| 2 | `docs/superpowers/plans/2026-10-30-owned-daemon-control-amendment-1.md` | Operator-authorised reply **`A`**, run `pm-run-20261030-040814-aefef9c0`, including new **§6 Clarification 1.1**, committed at `dfe780a`; governs **only the connect predicate** and its mandatory challenge handling. |
| 3 | `docs/superpowers/specs/2026-10-30-owned-daemon-epoch-design.md` | Approved design, including its review resolutions and conditional complete-source producer requirement. |
| 4 | `docs/handoff-owned-daemon-epoch.md`, `docs/operations.md`, `docs/backend-compatibility.md`, `docs/backend-fork.md` | Operational constraints, delivered limitations and carried-patch provenance; subordinate to the contract/design. |
| 5 | Delivered source at the commit below | Existing interfaces and behaviour retained unless explicitly migrated here. |

Approved source identity, pinned in full:

```text
commit de04d236c67c9c3ca30890f4c6bc2810e55be511  (de04d23)
blob   f0708be96bb7e3d115228d675308e8b00abc536c  (f0708be9)
sha256:b55faca175746411b31009c45f94ff086f73422ecd438bcef4f9ae7d40b5614c
run    pm-run-20261030-040814-aefef9c0; epoch 1
```

The design's heading still says “draft for review round 3”; the approval supplied with this assignment identifies the exact commit/blob/digest above. That content, rather than a later working-tree design, is the reference.
No production detached launch, including a credential-probe launch, is reachable before receipt production, verification, strict pidfd teardown and shared-root reconciliation exist. Activation is one phase-4 commit.
The host-transition child boundary is absolute: never execute `reboot`, `shutdown`, `poweroff`, `halt`, `kexec`, any `systemctl` power or sleep verb, `init 0|6` or `telinit 0|6`; never restart or stop the display manager or `graphical.target`; never terminate the session or signal the PM's process tree. If a host transition is required, write `reboot-required.json` with `{schema:1, trigger, evidence:[{path,sha256}], provenance:{run_id,lane,attempt,dispatch_key,observed_boot_id}}`, report it and stop.
Every phase commit carries the production-boundary no-spawn proof: phases 1–3 prohibit activation; phases 4–5 prove refusal for each incomplete composition and admit only the complete eligible case.
`foreground_control` and `owner_handshake` retain their meanings and remain false for the public backend. Neither field is relabelled as evidence ownership.
The guarantee is cooperative single-user ownership evidence; a hostile same-UID process is out of scope. Operator text must never call this authenticated ownership.
All following Python interfaces use delivered `Result[T] = Ok[T] | Err`, `ErrorCode`, frozen keyword-only value records and exact booleans. Snippets are interfaces, not implementation bodies.

## 2. Module and artifact inventory

Paths below are relative to the repository. “New” names locate the specified seams; they do not authorise an additional runtime mechanism.

| File | Responsibility in this epoch |
| --- | --- |
| `src/airplay_linux/backend/contract.py` | Add composition-only ownership flag, complete-report flag, `BackendSource`, `BackendStream.source`, strict source decoding and three-token recognition. |
| `src/airplay_linux/domain/records.py` | Define `DaemonReservation`, `LaunchReceipt` with application/daemon correlation, `DaemonCleanupIntent` and `SupervisorReleaseIntent`; retain the complete delivered `DaemonRecord` schema, including `owner_token`, as the observation/fixture channel. |
| `src/airplay_linux/domain/ports.py` | Add `LaunchReceiptStore`, `DaemonCleanupIntentStore` and the per-runtime-directory `SupervisorReleaseIntentStore`; extend `ProcessPort.terminate` with keyword-only `strict=False` and a receipt-free `revalidate` callback. |
| `src/airplay_linux/storage/codecs.py` | Explicit reservation/receipt/cleanup-intent/supervisor-release-intent/authority-owner/verdict allowlists, schema/version checks, state-dependent completeness checks, the new-document-only R9/R13 rejection matrices and safe `RecordError` reasons; every delivered record keeps its codec, validation and tolerance. |
| `src/airplay_linux/storage/repositories.py` | `JsonLaunchReceiptStore`, `JsonDaemonCleanupIntentStore`, `JsonSupervisorReleaseIntentStore`, atomic replacement and fail-closed new-document enumeration; immutable terminal receipts and supervisor intents, once-completed cleanup intents, no teardown deletion API; delivered stores unchanged. |
| `src/airplay_linux/storage/__init__.py` | Export the new repository/codecs alongside existing stores. |
| `src/airplay_linux/process/ownership.py` (new) | `VerifiedEndpoint`, read-only `verify_owned_endpoint`, and `EvidenceAuthority`. |
| `src/airplay_linux/backend/control.py` | `OwnershipProof`, `OwnershipAuthority`, `HandshakeAuthority`; authority-bound public control and pre/post I/O checks. |
| `src/airplay_linux/process/authority.py` (new) | `AuthorityOwner`, `SharedRootAuthority`, `AuthorityLease`, non-reentrant flock with 5.0 s acquisition / 0.05 s backoff and durable owner lifetime. |
| `src/airplay_linux/process/launcher.py` (new) | `LaunchCapability` and production implementation; eligibility evidence and the isolated idle backend argv. |
| `src/airplay_linux/process/credential_probe.py` (new) | `CredentialServiceProbe`; observable `run(config, *, process, namespace)` child seam and production `main()`; one outer namespace invocation with positive/negative backend launches, distinct launch UUIDs and child-owned receipts in a private probe root; per-leg inner process/environment/completion observations; dirfd/digest/exit-gated verdict file; receipt-backed stage-specific strict probe teardown. (R1, R4, R6, R7, R11) |
| `src/airplay_linux/process/procfs.py` | Reuse typed absent/present/terminated/unreadable identity evidence; supply stage-specific pre-bind and bound termination evidence, peer ticks/UID/argv/FD/socket-row evidence without guessing. (R1) |
| `src/airplay_linux/process/linux.py` | Enforce stage-dependent strict pidfd termination, post-open identity checks and same-stage `revalidate()` before the first signal, with bounded exact-exit observation. (R1, R8) |
| `src/airplay_linux/process/daemon_registry.py` | Sole application receipt writer; unique durable application/runtime/lifecycle observation join and pipe-free restart recovery; max-generation derivation; authority lease, launch ordering, cleanup-intent recording, supervisor-intent conversion/terminalisation without re-signalling, and serialized shared-daemon shutdown. (R4, R5, R8, R10, R12) |
| `src/airplay_linux/process/supervisor.py` | Public idle argv, delivered observation schema and config payload plus `launch_uuid`/`endpoint_generation`, direct-child observation and parent-death protection; atomically publish its own release intent before application-death signals, or refuse with `intent-unavailable` and exit; never write receipts. Token used only by the fixture authority; public authority release retained. (R10) |
| `src/airplay_linux/capture/source.py` (new) | Pure `verify_reported_source` over `tuple[MonitoredOutput, ...]`, `ReportedSourceVerifier` port and production `KScreenReportedSourceVerifier`. |
| `src/airplay_linux/display/inventory.py` (new) | `MonitoredOutput` and `KScreenInventory`: one fresh `kscreen-doctor -j` read supplies geometry and origin; uniqueness population is enabled outputs of any type/origin, including virtual and unknown, while disabled entries cannot create ambiguity. (R2) |
| `src/airplay_linux/display/kscreen.py`, `src/airplay_linux/capture/physical.py` | Reuse delivered signed-geometry parsing and physical whitelist; `KscreenConnectorInventory` projects connector names from the same inventory provider for `PhysicalCaptureProvider`. |
| `src/airplay_linux/transport/doubletake.py` | Forward ownership proof/fence; poll exact-target source/challenge on one 5.0 s deadline at 0.1 s intervals before ready; target-only cleanup and §7 failure precedence. |
| `src/airplay_linux/policy/capabilities.py` | Migrate admission rubric; keep provider, audio and trust requirements, and each pairing operation's own delivered prerequisite list. |
| `src/airplay_linux/runtime.py` | Sole production composition, ordered eligibility, capability derivation, authority choice and explicit final-process-boundary test injection. |
| `src/airplay_linux/testing/adapters.py` | Fake receipt/authority/process evidence needed by tests, including recording the outer credential-probe invocation and both inner legs' launch identities, environment selection and completion; no production selection of an application-contract fixture. (R6, R11) |
| `vendor/doubletake/0001-capture-report-selected-source.patch` | Complete type/size/position producer; no fabricated coordinates. |
| `vendor/doubletake/0002-status-report-selected-source.patch` | Preserve per-capture-group source in each stream's status. |
| `vendor/doubletake/0003-capability-tokens.patch` | Advertise `status-source-complete` only with the revised complete producer. |
| `README.md`, `docs/backend-fork.md` | Activation disclosure, refusal conditions, all three patches in build order, rebuild command and rebuilt binary identity. |
| Test modules named in §10 | Required future implementation verification; this drafting task does not execute them. |

## 3. Data contracts and storage

### 3.1 Common validation and privacy

For the new durable documents only, all durable fields are required constructor arguments and required wire members; nullable means an explicit `null`, not a decoder default. Schema fields never silently default when read.
Use exact `int` checks, rejecting `bool`; PID is in 1..2**31-1, inodes are positive, UID/device/start ticks are nonnegative, generation is positive, and these durable integers are at most 2**63-1. Timestamps are finite `float` values (finite JSON int/float decoded to float); UUIDs use the delivered canonical UUID codec and process identities have exactly `pid` and `start_ticks`.
Runtime paths are absolute, NUL-free, within the owned runtime root, and traversed without symlinks; paths use delivered `MAX_TEXT=4096`. `socket_name` is one nonempty basename, never `/`, `.` or `..`, at most `MAX_IDENTIFIER=256` characters. Protocol tokens are nonempty NUL-free identifier text of at most 256 characters.
`runtime_directory`, socket paths/names, boot IDs, process/peer identities, UUIDs and stored implementation handles are `field(repr=False)` on new records; proof-held legacy handshakes are also `repr=False`. Safe custom representations may expose state/kind and fixed codes only.
No new durable record or probe verdict carries an owner token, PIN, password, secret, credential reference, portal token, raw backend output or environment dump. Legacy handshake objects stay transient and retain their delivered redaction.
The delivered `DaemonRecord` schema and codec remain unchanged, including `owner_token`; this existing observation/fixture record is outside the new-record prohibition. Its token is marked unused on the public evidence path and never copied into reservations, receipts, either intent type, authority owners or verdicts. The public config pipe keeps the delivered `protocol_version`, `record_root`, `executable`, `features` members and adds `launch_uuid` and `endpoint_generation`; `owner_token` is included and consumed only for `HandshakeAuthority`.
Use explicit codec projections, never `asdict` to persist or diagnose these records. This section's new-document rules and the R9/R13 rejection matrices apply only to reservation, application/probe launch receipt, cleanup intent, supervisor release intent, authority owner and credential verdict. For those documents, unknown keys, schemas/versions, missing members, duplicate members, wrong leaf types, bool-as-int, nonfinite values, non-positive required integers, inconsistent states, partial stage combinations, filename/launch-identity mismatches and trailing bytes (including trailing whitespace) are refusals. Each new type has an exact valid control and category-specific malformed cases; malformed new storage is a typed refusal with no launch, signal, unlink or owner replacement. `DaemonRecord` and every other existing record retain their delivered codecs, validation and tolerance, including existing duplicate-member and trailing-whitespace behavior; the new codecs must never re-validate a legacy document. The separate named legacy-store byte-round-trip control in §10 pins that compatibility alongside strict new-type controls.

### 3.2 `BackendFeatures` and decoded source

| Record/member | Exact type/default | Validation and meaning |
| --- | --- | --- |
| `BackendFeatures.owned_daemon_control` | `bool = False` | Exact bool; derivable only by production composition, never by help/probe/token decoding. |
| `BackendFeatures.source_reporting_complete` | `bool = False` | Exact bool; matching patched help plus all three advertised tokens required. |
| `BackendFeatures.source_reporting` | delivered `bool = False` | Two-token reporting channel; does not establish completeness or physical origin. |
| `BackendFeatures.keyring`, `.physical_source` | delivered required `bool` fields | Raw public decoding always false; composition derives affirmative values from proof/wiring. |
| `BackendSource` | `type: int; width: int; height: int; x: int; y: int` | Five required fields, no defaults; exact integers. Source acceptance additionally requires type 1 and positive dimensions; coordinates remain signed. |
| `BackendStream.source` | `BackendSource \| None = None` | Omitted wire `source` means unverified; never synthesize a source from requested connector, stream dimensions or zero coordinates. |

The carried Go `SourceInfo` has no JSON member tags: the nested wire keys are exactly `Type`, `Width`, `Height`, `X`, `Y`; Python fields are lowercase. No case-folded or lowercase wire alias is implied.
`_decode_source(value: object) -> BackendSource | Err` accepts exactly that five-member mapping with exact integer values, `Type` in 1..2**32-1 and positive dimensions; empty/partial objects, non-mappings, explicit `null`, unknown keys, zero/negative type or dimensions and malformed members return `dependency_incompatible`. `_decode_stream` handles an absent member separately as `None`.
A complete window/region or unknown positive type may decode but cannot pass `verify_reported_source`; direct calls to that function must also recheck dimensions. Neither absent nor malformed source enables a gate.
`status-source-complete` without `capabilities-v1`, `status-source` and the exact patched surface is insufficient. Tokens on a nonzero capabilities-command exit are ignored.

### 3.3 `DaemonReservation`

`DaemonReservation` is `@dataclass(frozen=True, kw_only=True)`. Every field below is required, without a constructor default.

| Field | Python type | Required invariant |
| --- | --- | --- |
| `schema_version` | `int` | Exactly 1. |
| `launch_uuid` | `UUID` | Unique per attempt, immutable across transitions and retries of publication. |
| `instance_uuid` | `InstanceId` | Application instance that requested the launch: runtime's `app_instance_uuid`, as design §5.1; never the per-daemon observation UUID. |
| `app` | `ProcessIdentity` | Exact owning application PID/start ticks. |
| `owner_uid` | `int` | Effective application UID. |
| `boot_id` | `str` | Canonical UUID text read from the current boot ID. |
| `runtime_directory` | `str` | Private 0700 directory under the owned runtime root. |
| `runtime_device`, `runtime_inode` | `int`, `int` | Directory `fstat` identity captured at reservation. |
| `endpoint_generation` | `int` | `max(endpoint_generation)` across all durable receipts for endpoint `(runtime_directory, socket_name)` + 1; never reset merely because no receipt is currently live, and never inferred from recency or UUID ordering. If the maximum is `2**63-1`, reservation refuses rather than wrapping. (R8) |
| `socket_name` | `str` | Basename inside the anchored directory. |
| `protocol_version` | `str` | Control-protocol token; not a claim that public status echoes it. |

### 3.4 `LaunchReceipt`

`LaunchReceipt` has every reservation field with identical type/validation, plus the following required fields. Nullable values must be explicitly supplied even for `reserved`.

| Field | Python type | Required invariant |
| --- | --- | --- |
| `state` | `Literal['reserved','detached','bound','ready','stopping','stopped','failed']` | Transitions only through §5.1; terminal receipts are immutable, including during later failed-launch cleanup. |
| `daemon_instance_uuid` | `InstanceId \| None` | Initially null, then copied from the supervisor observation channel's `DaemonRecord.instance_uuid`; required with detached identities and immutable once observed; distinct from application `instance_uuid` and `launch_uuid`. |
| `supervisor`, `daemon` | `ProcessIdentity \| None`, `ProcessIdentity \| None` | Both initially null; a daemon cannot be non-null without a supervisor. |
| `entry_device`, `entry_inode` | `int \| None`, `int \| None` | Socket directory-entry identity, both null or both present. |
| `socket_inode` | `int \| None` | Kernel socket inode from the unique path row in `/proc/net/unix`; **not** `entry_inode`. |
| `peer_pid`, `peer_start_ticks`, `peer_uid` | `int \| None`, `int \| None`, `int \| None` | All null or all present; agree with the verified daemon and owner UID. |
| `status_state` | `str \| None` | Last accepted typed state in `{idle,discovering,connecting,streaming,pin_required}`, never free backend text; ready requires `idle`. |
| `credential_service` | `Literal['keyring_proven','file_refused','unknown']` | Evidence label only; initially explicitly `unknown`. |
| `failure` | `ErrorCode \| None` | Fixed failure code for `failed`; non-null on failure, null before failure. |
| `updated_at` | `float` | Finite store-clock value for each committed transition. |

`reserved` starts with all observed fields null; `detached` requires the observed daemon-instance UUID and exact supervisor/daemon pair; `bound` additionally requires entry, socket and peer identity; `ready` additionally requires accepted idle status and proven credential service.
The stopping workflow is reachable from `reserved`, `detached`, `bound`, `ready` and `failed`. A nonterminal receipt may transition to `stopping` while retaining all earlier evidence, including the all-null pre-spawn case. A `failed` receipt stays byte-identical: its later cleanup outcome is recorded through `DaemonCleanupIntent`, never a failed→stopping receipt rewrite or a third pre-signal intent. A terminal receipt retains every fact observed before termination/failure.
A codec can validate field consistency; the registry must validate predecessor/transition constraints under both locks. Historical transition provenance is not fabricated by decoding a lone record.
The unchanged delivered `DaemonRecord` members are `schema_version`, `instance_uuid`, `owner_token`, `app`, `supervisor`, `daemon`, `protocol_version`, `socket_path`, `lifecycle`, `runtime_directory`. For receipt observation, enumerate durable records and select exactly one with `app == receipt.app`, `runtime_directory == receipt.runtime_directory`, and `lifecycle` in `{starting, running, stopping}`; zero or multiple matches is `CleanupRequired`, never adopt or choose by recency/UUID. If `receipt.daemon_instance_uuid` is set, the selected record's `instance_uuid` must equal it. Any supervisor/daemon identities already in the receipt must match exactly. If they are not yet published in the receipt, the selected record must supply both exact identities; validate each against fresh PID/start-tick/UID evidence and require the daemon's live parent relationship to the recorded supervisor before advancing. The config pipe is used only for in-process correlation during the launch that owns it: its `launch_uuid` and `endpoint_generation` must then match the saved reservation. After any restart, correlation is exclusively the durable unique-record rule above; the registry must neither require nor accept pipe data for correlation. A unique valid record can reconstruct the null-`daemon_instance_uuid` observation-to-receipt gap without the original pipe; zero/multiple matches, including any stale competing record, still refuse. The app/runtime/launch/daemon join never authorises control by itself. (R5, R8, R12)

`DaemonCleanupIntent` is a frozen keyword-only lifecycle record with exactly the required payload fields `launch_uuid: UUID`, `reason: ErrorCode`, `intent_at: float`, `completed_at: float | None`, `outcome: Literal['stopped'] | ErrorCode | None`, without constructor defaults. Its schema is `airplay-linux/daemon-cleanup-intent`, version 1 (§3.6). Initial `completed_at` and `outcome` are explicitly null; its one completion update supplies both, with finite timestamps and an allowlisted outcome. Identity/reason/intent time never change, and completion is immutable. It records immutable failed-receipt cleanup and registry recovery of supervisor release; it is not a third alternative to the strict pre-signal intents in §6.3. A failed receipt without a permitted durable pre-signal intent cannot be signalled; absence may still be proved and recorded without rewriting it. The supervisor's own `DaemonRecord.lifecycle='stopping'` remains observation only. No intent authorises a signal, unlink or replacement by itself; missing intent never proves cleanup completed.

`SupervisorReleaseIntent` is `@dataclass(frozen=True, kw_only=True)` with exactly the required payload `{schema_version, launch_uuid, instance_uuid, app, supervisor, daemon, reason, intent_at}`, without defaults. `schema_version: int` is exactly 1; `launch_uuid: UUID` and `instance_uuid: InstanceId` match the saved reservation's launch/application identities (and the receipt when present); `app`, `supervisor` and `daemon` are non-null `ProcessIdentity`; `reason: Literal['application-exited','parent-mismatch']`; `intent_at: float` is finite. Its schema is `airplay-linux/supervisor-release-intent`, version 1. It is one immutable document at `<runtime_directory>/release-intent.json`, atomically written by that directory's supervisor before its first signal. Publication requires all three: (a) the recorded application identity is proved absent or mismatched, never merely unreadable; (b) the receipt is `reserved`, `detached`, `bound` or `ready`, or no receipt exists yet; (c) the supervisor's own delivered `DaemonRecord` names the same exact daemon identity, with matching app/supervisor/runtime correlation and all identities already published in the receipt. It grants no receipt-writing permission. Registry recovery converts it to `DaemonCleanupIntent` and terminalises the correlated nonterminal receipt as `stopped` when exact absence is proved, `failed` otherwise, retains the supervisor file as evidence and never re-signals. An already terminal receipt stays byte-identical; if no receipt exists, retain the missing-receipt obligation without fabricating reservation fields. If publication cannot complete, the supervisor sends no signal, publishes `cleanup_required` with fixed reason `intent-unavailable` and exits so its parent-death protection applies; this refusal is never successful cleanup. (R10)

### 3.5 In-memory ownership, authority, credential and launcher values

All values in this table are transient unless explicitly identified as the owner document; none has a secret-bearing new field. Required fields have no defaults except where shown.

| Type | Exact fields/properties | Invariants |
| --- | --- | --- |
| `VerifiedEndpoint` | `daemon: ProcessIdentity; socket_entry: tuple[int,int]; socket_inode: int; peer_uid: int; generation: int` | Entry pair is device/inode; peer daemon/ticks, kernel inode, UID and maximum endpoint generation all verified; no open descriptor ownership escapes this value. (R8) |
| `TerminationEvidence` | `stage: Literal['pre_bind','bound']; expected_identity: ProcessIdentity; expected_uid: int; expected_socket_name: str; expected_supervisor: ProcessIdentity \| None; expected_entry: tuple[int,int] \| None; expected_socket_inode: int \| None` | Strict teardown requires one stage-complete value matching its receipt, or, for R10's no-receipt supervisor path, the saved reservation and its own daemon observation. `pre_bind` requires supervisor identity and live matching child ticks, `/proc/<pid>` UID, exact `-socket <path>` and `-daemonize` argv, plus child relationship to the live recorded supervisor; entry and kernel inode are null. `bound` requires entry device/inode and kernel socket inode plus the full §6.1 peer/row/FD/ticks/UID/argv/entry chain; supervisor may be null only when absent from the receipt. Post-open revalidation must reproduce the same stage and identities. (R1) |
| `OwnershipProof` | `kind: Literal['handshake','evidence']; daemon: ProcessIdentity; socket_entry: tuple[int,int]; generation: int; handshake: Handshake \| None` | Required fields; evidence has `handshake=None`; handshake kind requires the delivered transient `Handshake`. All identifying members are `repr=False`. |
| `AuthorityOwner` | `schema: Literal['airplay-linux/daemon-authority-owner/v1']; instance_uuid: InstanceId; app: ProcessIdentity; boot_id: str; acquired_at: float` | Exact five design §9 members; no defaults; atomic durable owner; schema token includes version 1. Other values follow §3.1/reservation validation. |
| `AuthorityLease` | `owner: AuthorityOwner; _lock_fd: int; _authority: SharedRootAuthority; _unlocked: bool = False` | Process-local, no codec; all handles/owner `repr=False`; represents one acquisition and cannot be transferred or re-entered. |
| `ProbeConfig` | `probe_uuid: UUID; reservations: tuple[DaemonReservation, DaemonReservation]; backend_executable: str; record_root: Path` | Required child inputs; reservations are ordered positive then negative, with distinct `launch_uuid` values and leg-private runtime/socket paths under the same probe root; `record_root` is the parent-created private root and is `repr=False`; no credentials, owner token or ambient environment. |
| `CredentialProofVerdict` | `service_leg: Literal['passed','failed','not_run']; backend_positive_leg: Literal['passed','failed','not_run']; backend_negative_leg: Literal['passed','failed','not_run']; failure: ErrorCode \| None` | Required fields; `.proven: bool` is derived from three passed legs plus no failure; all-pass with failure or any failed leg without failure is invalid; fixed leg names, never stderr. Parent-side value is created only after strict file-backed `ProbeVerdict` decoding. |
| `ProbeVerdict` | `schema: Literal['airplay-linux/credential-service-proof']; version: int; service_leg: Literal['passed','failed','not_run']; backend_positive_leg: Literal['passed','failed','not_run']; backend_negative_leg: Literal['passed','failed','not_run']; failure: ErrorCode \| None` | Child-published wire value; version exactly 1, exact required members and fixed enums; contains no derived `proven`, secrets or backend output. |
| `MonitoredOutput` | `name: str; enabled: bool \| None; connected: bool; output_type: int \| str \| None; size: Size \| None; position: tuple[int,int] \| None; virtual: bool \| None; scale: float \| None = None; output_id: str \| None = None` | Required fields except the shown defaults, from one fresh inventory read; unknown enabled state and missing geometry/scale on enabled entries remain explicit and refuse verification. Signed position is the primary identity key; scale corroborates current-mode size. Every enabled output participates in uniqueness before origin/name filtering; disabled entries do not. |
| `SourceObservation` | `reported: BackendSource \| None; matched_connector: str \| None; reason: SourceRefusal \| None = None` | Typed transient evidence. Acceptance requires a reported source and nonempty matched connector with no reason; refusal requires a typed, sanitized `SourceRefusal` and no matched connector. `.accepted` is derived from `reason is None`. |
| `SourceVerificationRefusal` | `Err` subclass with `observation: SourceObservation` | Retains a refused observation; error code is `unsupported_source` and diagnostic detail is the fixed `observation.reason.value`, never raw backend/parser text. |
| `KScreenReportedSourceVerifier` | `inventory: KScreenInventory; physical_inventory_available: bool; ownerships: object \| None = None; last_observation: SourceObservation \| None = None` | Injected shared inventory and affirmative inventory evidence; each verification reads one fresh geometry/origin tuple before filtering. When supplied, `ownerships.all()` must yield `Ok(tuple)` for the helper-ownership seam; failure refuses. `verify` retains its observation in `last_observation`. |

### 3.6 Wire and repository contracts

| Document | Schema / version | Storage/publication |
| --- | --- | --- |
| Reservation | `airplay-linux/daemon-reservation`, version 1 | Published as the reservation projection of the initial receipt; design requires no second authoritative current-pointer document. |
| Receipt | `airplay-linux/launch-receipt`, version 1 | `<state_root>/daemon/receipts/<launch_uuid>.json`; a `reserved` receipt durably represents the reservation before spawn. |
| Cleanup intent | `airplay-linux/daemon-cleanup-intent`, version 1 | `<state_root>/daemon/cleanup-intents/<launch_uuid>.json`; one atomic document for immutable failed-receipt cleanup or supervisor-intent recovery, initial publication plus one completion update; not a substitute for §6.3's pre-signal intent. |
| Supervisor release intent | `airplay-linux/supervisor-release-intent`, version 1 | `<runtime_directory>/release-intent.json` in the per-instance 0700 directory the supervisor already owns; supervisor-only atomic publication before its first signal under §3.4's conditions; immutable and retained by recovery, never a receipt write. |
| Shared-root owner | `airplay-linux/daemon-authority-owner/v1`; version encoded in schema, no extra `version` member | `<state_root>/daemon/authority.owner`, atomic replace while its flock is held; exactly the five §3.5 members. |
| Probe receipt | `airplay-linux/launch-receipt`, version 1 | `<state_root>/daemon/probe/<probe_uuid>/receipts/<launch_uuid>.json`; two receipts for the complete witness, one per distinct positive/negative launch UUID; same receipt schema/codec, child-only writer inside its private root. The parent archives both verified receipts only after successful exit/teardown and retains the root on failure. |
| Credential verdict | `schema: 'airplay-linux/credential-service-proof'`, `version: 1` | `<state_root>/daemon/probe/<probe_uuid>/verdict.json` plus sibling `verdict.sha256`; child-owned files read by parent through an opened probe-root dirfd, never stdout. Success archives both verified probe receipts and the verdict under `<state_root>/archives/probe/<probe_uuid>/`; failure retains the private probe root. |

Reservation/receipt codecs use the delivered JSON envelope `schema` and `version: 1`, and explicitly encode the required `schema_version: 1` payload member; both version checks must agree. The supervisor-release-intent envelope likewise uses its own schema and `version: 1`, plus exactly the eight §3.4 payload members including `schema_version: 1`. The cleanup-intent envelope uses its own schema and `version: 1` plus exactly its five §3.4 payload members; it adds no `schema_version` payload field. The verdict is one strict UTF-8 JSON document in `verdict.json`, bounded to 4096 bytes, and the sibling `verdict.sha256` contains exactly the expected lowercase SHA-256 hex digest plus one newline (65 bytes). Parent opens the parent-created probe root as a no-follow directory, verifies its owner/mode and expected probe UUID path, then opens both names relative to that dirfd with `O_NOFOLLOW`; each must be a regular file owned by the expected UID. It checks the verdict size is 1..4096 before reading at most 4097 bytes, checks each exact `st_size` against bytes read and the digest, then decodes `ProbeVerdict`; child exit status must be zero. Missing, oversized, short, changed, malformed, extra-member, trailing-byte or digest-mismatched evidence cannot produce a parent verdict. Child stdout/stderr are diagnostics only and never parsed as verdict data.
Owner, cleanup-intent, supervisor-release-intent and verdict decoding rejects unknown schema/version, missing/extra/duplicate members and invalid values. These are new-document checks only; legacy loading remains on the delivered codecs (§3.1). Encode `failure` as its fixed catalog value or null; never serialize derived `proven`.
`AtomicFiles` publication is temp write → file fsync → rename → directory fsync. A failed step returns `persistence_failed`, preserves available evidence and cannot authorise the next lifecycle step. This same protocol applies to supervisor release intent; any incomplete step invokes the `intent-unavailable` zero-signal exit rule. Its store is anchored to the already-owned runtime directory, accepts only identical repeat publication, and has no rewrite, completion or deletion API.
Probe and application receipt stores use the same codec but different roots and exactly one writer each: the application registry alone writes `<state_root>/daemon/receipts`; the parent creates a unique `<state_root>/daemon/probe/<probe_uuid>` as private mode 0700 before launch and passes it as `--record-root`; only the namespace child writes receipts below that probe root. The child cannot open or write the application receipt/authority tree. After zero child exit, successful file verification, both legs' exact teardown/absence evidence and successful archive publication of both receipts and verdict, the parent removes the probe files and root; on archive failure it retains the originals. Any failed or uncertain path retains the private probe root and all files/receipts present. Receipt enumeration fails closed on any unreadable/malformed record; it must not quarantine-and-ignore ownership evidence as the session store does. No API deletes an application receipt during teardown.

## 4. Exact Python interfaces

Signatures below fix names/types and the propagation boundary. §12 records the resolved elaborations; missing evidence remains a refusal.

### 4.1 Composition, launcher and reported-source seams

```python
def derive_composition_capabilities(
    probed: BackendFeatures, launcher: LaunchCapability,
    credential_proof: CredentialProofVerdict,
    source_verifier: ReportedSourceVerifier | None, ambient_socket: str | None,
) -> BackendFeatures: ...

class LaunchCapability(Protocol):
    production_graph: bool
    def available(self) -> bool: ...
    def argv(self, socket_path: str) -> tuple[str, ...]: ...
    def ambient_socket(self) -> str | None: ...

class KScreenInventory:
    def __init__(self, process: ProcessPort, *, timeout: float = 5.0) -> None: ...
    async def observe(self) -> Result[tuple[MonitoredOutput, ...]]: ...

class ReportedSourceVerifier(Protocol):
    physical_inventory_available: bool
    async def verify(self, requested: str, reported: object) -> Result[SourceObservation]: ...

def verify_reported_source(
    requested: str, reported: object, layout: tuple[object, ...], *,
    owned_outputs: tuple[object, ...] = (),
) -> Result[SourceObservation]: ...

class CredentialServiceProbe:
    async def check(self) -> Result[CredentialProofVerdict]: ...

def run(
    config: ProbeConfig, *, process: ProcessPort,
    namespace: Callable[[tuple[str, ...]], Result[ProcessIdentity]],
) -> ProbeVerdict: ...

def verify_owned_endpoint(
    receipt: LaunchReceipt, *, current_generation: int,
) -> Result[VerifiedEndpoint]: ...
```

`derive_composition_capabilities` is pure: it combines captured launcher/inventory evidence, the verdict and actual wired-verifier identity; it performs no probing, filesystem writes or process I/O. Invoke once in `runtime._compose_production` after ordered eligibility/proof handling.
`current_generation` makes design §6 step 8's registry input explicit; callers supply the durable maximum endpoint generation derived from **all** receipts, never the receipt's own value as a tautology. Evidence reads are injectable for offline tests without replacing the verifier algorithm.
`ProductionLaunchCapability.available` requires verified `daemonize`, `socket`, `cred-backend`, a private absolute writable owned nonsymlink root, no ambient socket, and production-graph origin. `argv` refuses invalid/outside-root paths with fixed `ValueError` text before I/O.
The only public backend argv is `(executable, '-daemonize', '-socket', socket_path, '-cred-backend', 'keyring')`. The fixture foreground argv stays separate.
For this source-verifier contract, the binding [owned-daemon control plan](../plans/2026-10-30-owned-daemon-control.md), T5a, supersedes the earlier `Result[None]` wording with the `Result[SourceObservation]` API.
`ReportedSourceVerifier.verify` returns `Ok(SourceObservation)` for a unique acceptable match or `SourceVerificationRefusal` (`Err`, `unsupported_source`) retaining the typed observation and a fixed `SourceRefusal` reason. Missing/unreadable/stale/ambiguous/unacceptable evidence cannot accept cached geometry or the requested connector as observation. `KScreenInventory.observe` runs `kscreen-doctor -j` exactly once per fresh observation and parses both origin and geometry; a non-object output entry refuses the whole read (`INVENTORY_UNAVAILABLE`) without returning partial records. Unknown enabled state or unreadable enabled-output geometry/scale refuses verification; the pure verifier reports `UNREADABLE_LAYOUT` for those incomplete typed layouts. `KscreenConnectorInventory` and `PhysicalCaptureProvider` retain their delivered connector interface through a projection from this same provider (§6.2).
The pure verifier's keyword-only `owned_outputs` tuple carries ownership records: an `active` or `removing` record matching the candidate connector or non-null output ID refuses with `OWNED_OUTPUT`. The async verifier obtains that tuple from injected `ownerships.all()`; failure to read it refuses with `OWNERSHIP_UNAVAILABLE`. Observation itself has no stream side effects; the activated T7 transport consumes each stream's result, publishes accepted connector/reported geometry in ready events, and preserves the sanitized refusal reason in failed-event diagnostics subject to §7's cleanup precedence.
Before registry construction, an isolated bootstrap check requires exactly: `owned_daemon_control` derivable, the source verifier wired, receipt store + endpoint verifier + strict teardown constructible, and `unshare` resolvable. It does not consult `keyring` or `DaemonRegistry._compatible()`. The phase activation/installation boundary still requires every lifecycle seam, including shared authority and the credential-proof producer, before this check can dispatch the probe (§5.3, §10.3).
`CredentialServiceProbe.check` runs once after that check; its outer process boundary launches exactly one `unshare -rn ... credential_probe` child, then reads only the child-owned file verdict through the verified probe-root dirfd (§3.6). Inside that namespace, the module-level `run(config, *, process, namespace)` is the same production/test seam: it owns the isolated receipt store and executes the positive and negative backend legs through the injected inner `ProcessPort`; `main()` supplies production dependencies. A complete witness has exactly two inner backend `-daemonize` launches, each with its own launch UUID and receipt; a single-leg subtest has exactly one inner launch. The positive leg has a reachable bus, 3.0 s total to socket+idle, then 2.0 s idle confirmation and strict teardown with 3.0 s grace. The negative leg uses an unreachable `DBUS_SESSION_BUS_ADDRESS` and must complete nonzero with no socket or credentials file. The inner recorder observes each leg's actual invocation, selected environment and completion (§6.3); no wrapper or alternate process port may hide the negative leg. It returns a typed failed verdict/refusal when a leg or namespace prerequisite fails; only the three passed legs establish `.proven`. It must not turn failed child cleanup into a successful verdict. Only after file verification and proof handling/capability derivation is the application registry constructed; its delivered `keyring` compatibility requirement remains downstream.

### 4.2 Ownership authorization and implementations

```python
class OwnershipAuthority(Protocol):
    def allows(self, operation: BackendOperation) -> bool: ...
    async def prove(self, record: DaemonRecord) -> Result[OwnershipProof]: ...
    async def authorize(
        self, record: DaemonRecord, operation: BackendOperation,
        target: ReceiverTarget | None, fence: DispatchFence | None,
    ) -> Result[OwnershipProof]: ...
    async def confirm(self, record: DaemonRecord, proof: OwnershipProof) -> Result[None]: ...
    async def release(self, record: DaemonRecord) -> Result[None]: ...

class HandshakeAuthority(OwnershipAuthority):
    # Implements exactly the five signatures above for the application-contract fixture.
    ...

class EvidenceAuthority(OwnershipAuthority):
    # Implements exactly the five signatures above against current receipt/peer evidence.
    ...

class SharedRootAuthority:
    async def acquire(self, instance: AuthorityOwner) -> Result[AuthorityLease]: ...
    async def clear_owner(self, expected: AuthorityOwner) -> Result[None]: ...

class AuthorityLease:
    def unlock(self) -> None: ...
```

`DispatchFence` remains the delivered `Callable[[], Result[None]]`; it is a closure over the unchanged target, receiver ID, endpoint generation and fingerprint, not an unstructured boolean or new wire credential.
`allows` is synchronous and pure: it checks capability class only. A required matching fence and current proof are additional `authorize` obligations, never replaced by `allows=True`.
`EvidenceAuthority` is bound to the receipt/generation source and the same process port used for strict teardown: registry consumers use the registry's source, and the namespace probe constructs its isolated receipt-backed source before any registry exists. `prove` joins `DaemonRecord.instance_uuid` to `receipt.daemon_instance_uuid`, verifies that receipt and returns evidence kind; it never accepts backend owner metadata as a substitute. `release` selects `TerminationEvidence` from that receipt's stage, or, on R10's no-receipt supervisor path, from the saved reservation and the supervisor's own matching daemon observation with independent fresh stage verification; it supplies `strict=True` and a synchronous `revalidate` closure returning freshly verified same-stage evidence after pidfd open; the process port never carries a receipt type. Before any signal it requires exactly the applicable §6.3 durable intent: registry-written receipt `stopping`, or the supervisor-published `SupervisorReleaseIntent` under §3.4. The authority's per-runtime-directory supervisor-intent seam is writable only by the supervisor and readable by registry recovery; `DaemonCleanupIntentStore` records failed cleanup/recovery outcome and cannot substitute for pre-signal intent. Only the registry writes application receipts. No-receipt supervisor release still requires independently stage-complete child/endpoint evidence; the intent alone cannot supply it.
`HandshakeAuthority` is selected only for a profile affirming `owner_handshake`; it preserves delivered owner/instance/daemon/socket/protocol comparisons and fence requirements. Its raw status I/O must avoid recursion through `prove`.
Only authority `prove`/`confirm` establish proof validity; `authorize` re-proves and evaluates the caller's fence. `confirm` rechecks peer/entry after I/O and compares the original daemon, entry and generation.
`release` returns success only after strict exact-exit and endpoint cleanup evidence. Public `shutdown` is an internal release operation, never an unfenced external ctl shutdown command.
`acquire` refuses another proved-live owner and unreadable ownership with `ownership_mismatch`; flock acquisition has one 5.0 s deadline with 0.05 s backoff, after which `CONCURRENT_WRITER_DETECTED` maps to `ownership_mismatch` on this path. `clear_owner` requires the held lease, exact expected owner and proved teardown absence. A stale expected owner cannot remove a successor.
`unlock` is idempotent local flock release only; it never removes owner evidence. All mutations release the lease in `finally`; cancellation propagates after retaining any unresolved lifecycle obligation.

### 4.3 Storage, process and control interfaces

```python
def encode_daemon_reservation(record: DaemonReservation) -> bytes: ...
def decode_daemon_reservation(payload: bytes) -> DaemonReservation: ...
def encode_launch_receipt(record: LaunchReceipt) -> bytes: ...
def decode_launch_receipt(payload: bytes) -> LaunchReceipt: ...
def encode_daemon_cleanup_intent(record: DaemonCleanupIntent) -> bytes: ...
def decode_daemon_cleanup_intent(payload: bytes) -> DaemonCleanupIntent: ...
def encode_supervisor_release_intent(record: SupervisorReleaseIntent) -> bytes: ...
def decode_supervisor_release_intent(payload: bytes) -> SupervisorReleaseIntent: ...

class LaunchReceiptStore(Protocol):
    async def write(self, record: LaunchReceipt) -> Result[None]: ...
    async def all(self) -> Result[tuple[LaunchReceipt, ...]]: ...

class JsonLaunchReceiptStore(LaunchReceiptStore):
    def __init__(self, files: AtomicFiles) -> None: ...
    async def write(self, record: LaunchReceipt) -> Result[None]: ...
    async def all(self) -> Result[tuple[LaunchReceipt, ...]]: ...

class DaemonCleanupIntentStore(Protocol):
    async def write(self, record: DaemonCleanupIntent) -> Result[None]: ...
    async def all(self) -> Result[tuple[DaemonCleanupIntent, ...]]: ...

class JsonDaemonCleanupIntentStore(DaemonCleanupIntentStore):
    def __init__(self, files: AtomicFiles) -> None: ...
    async def write(self, record: DaemonCleanupIntent) -> Result[None]: ...
    async def all(self) -> Result[tuple[DaemonCleanupIntent, ...]]: ...

class SupervisorReleaseIntentStore(Protocol):
    async def write(self, record: SupervisorReleaseIntent) -> Result[None]: ...
    async def read(self) -> Result[SupervisorReleaseIntent | None]: ...

class JsonSupervisorReleaseIntentStore(SupervisorReleaseIntentStore):
    def __init__(self, files: AtomicFiles) -> None: ...
    async def write(self, record: SupervisorReleaseIntent) -> Result[None]: ...
    async def read(self) -> Result[SupervisorReleaseIntent | None]: ...

class ProcessPort(Protocol):
    async def terminate(
        self, identity: ProcessIdentity, owner_uuid: UUID,
        runtime_directory: str, deadline: float, *, strict: bool = False,
        evidence: TerminationEvidence | None = None,
        revalidate: Callable[[], Result[TerminationEvidence]] | None = None,
    ) -> Result[None]: ...

class PublishedDoubletakeControl:
    def __init__(
        self, process: ProcessPort, profile: PublishedDoubletakeProfile, *,
        record: DaemonRecord | None = None, authority: OwnershipAuthority | None = None,
        timeout: float = 5.0, clock: Clock | None = None,
    ) -> None: ...
    def _available(self, operation: BackendOperation) -> bool: ...
    async def handshake(
        self, owner_token: SensitiveText | str, instance_uuid: InstanceId,
    ) -> Result[OwnershipProof]: ...
    async def _revalidate_ownership(
        self, operation: BackendOperation, target: ReceiverTarget | None,
        fence: DispatchFence | None,
    ) -> Result[OwnershipProof]: ...
    def _ownership_check(self, status: StatusReply) -> Result[None]: ...
    async def request(
        self, operation: BackendOperation | str, target: ReceiverTarget | None,
        payload: BackendPayload, context: OperationContext | None, *,
        dispatch_fence: DispatchFence | None = None,
        ownership_proof: OwnershipProof | None = None,
    ) -> Result[BackendReply]: ...
```

Codecs for new documents raise delivered `RecordError` with allowlisted reasons; new repository methods translate codec/filesystem failures into `persistence_failed`. Receipt `write` cannot rewrite immutable terminal evidence, change reservation identity fields or replace an observed `daemon_instance_uuid`; cleanup-intent `write` allows only initial publication and its one completion update, never a completed-record rewrite. `JsonSupervisorReleaseIntentStore` is constructed over `AtomicFiles` anchored to the verified owned runtime directory and addresses only `release-intent.json`; `write` is supervisor-only, atomically publishes once after the supervisor proves §3.4's prerequisites, and permits an identical repeat only. Registry recovery uses `read` and never rewrites/removes that file. New codec entrypoints accept only their named new document; existing `DaemonStore` and every other delivered record/store keep their codecs, validation and tolerance without a new-codec ingestion pass (R13).
`ProcessPort.run`, `spawn`, `identity`, `owned` and `subscribe` keep delivered signatures. `LinuxProcessRunner` and fake process adapters implement the new termination keywords. Owned call sites explicitly pass `strict=True`, stage-complete `TerminationEvidence` derived from the receipt or R10's independently verified no-receipt reservation/observation source, and a synchronous `revalidate` closure that returns freshly checked evidence for that same stage; after opening pidfd and before the first signal, the runner rechecks PID/start ticks/UID, invokes the callback, compares stage and stable identities, and refuses on any non-`Ok` or mismatch while closing the descriptor with zero signals (§6.3). Optional defaults preserve non-owned delivered callers.
`BackendControl.handshake` and `.request` acquire the same return/keyword changes as the concrete adapter. Legacy fixture adaptation preserves its comparisons and presents `OwnershipProof(kind='handshake')` to migrated consumers.
Public `handshake` delegates to `authority.prove(record)`; legacy token parameters remain for fixture compatibility and are not public evidence. Missing authority/record refuses mutations with `dependency_incompatible` before I/O.
`_ownership_check` retains equality checks only for `HandshakeAuthority`. For evidence authority it demands no owner/instance/daemon/socket/protocol members from status; accepted status remains covered by `prove`/`confirm`.
`request` validates operation, payload, capabilities and required fence before I/O; then awaits `authorize`, synchronously compares the proof with the current receipt/generation and re-evaluates the original fence at dispatch, performs I/O and immediately awaits `confirm` before exposing any result or notice.
Confirmation also covers an I/O refusal/error; a failed confirmation takes precedence over an otherwise usable reply. There is no intervening application-owned await between the final proof/fence checks and dispatch. Replacement invalidates the proof and cannot publish `ready`.

### 4.4 Transport, registry and supervisor changes

```python
class DoubleTakeTransport:
    def __init__(
        self, control_factory: Callable[[DaemonRecord], BackendControl],
        registry: DaemonRegistry, capture: CapturePort, credentials: CredentialStore,
        receivers: ReceiverStore, fences: TrustFenceRegistry, features: BackendFeatures,
        clock: Clock, scheduler: Scheduler, ids: IdFactory, *,
        source_verifier: ReportedSourceVerifier | None = None,
    ) -> None: ...
    async def _admit(self, target: ReceiverTarget, context: OperationContext) -> Result[_Daemon]: ...
    async def _start(self, stream: _Stream) -> Result[StreamHandle]: ...
    async def _request(
        self, daemon: _Daemon, operation: BackendOperation, target: ReceiverTarget,
        payload: BackendPayload, context: OperationContext, *, cleanup: bool = False,
    ) -> Result[BackendReply]: ...

class DaemonRegistry:
    async def ensure(self) -> Result[DaemonRecord]: ...
    async def restart(self) -> Result[DaemonRecord]: ...
    async def reconcile_previous(self) -> Result[None]: ...
    async def shutdown(self) -> Result[None]: ...
    def _compatible(self) -> bool: ...
    async def _launch(self) -> Result[DaemonRecord]: ...
    async def _authenticate(
        self, record: DaemonRecord, *, allow_partial: bool = False, release: bool = False,
    ) -> Result[OwnershipProof | None]: ...
    async def _reconcile_locked(self) -> Result[None]: ...
    async def _stop(self, record: DaemonRecord) -> Result[None]: ...

class DaemonSupervisor:
    def run(self) -> int: ...
    async def _run(self) -> int: ...
    async def _request_release(self, record: DaemonRecord, features: BackendFeatures) -> Result[None]: ...
    async def _publish_release_intent(
        self, record: DaemonRecord,
        reason: Literal['application-exited','parent-mismatch'],
    ) -> Result[SupervisorReleaseIntent]: ...
```

Registry construction adds required named seams `app_instance_uuid: InstanceId`, `receipts: LaunchReceiptStore`, `cleanup_intents: DaemonCleanupIntentStore`, `supervisor_intents: Callable[[str], SupervisorReleaseIntentStore]`, `authority: SharedRootAuthority`, `launcher: LaunchCapability`, and `ownership: OwnershipAuthority` for the public profile; delivered process/store/factory/root/clock/ID/scheduler/restart arguments retain their types/defaults. The supervisor-intent factory is keyed by the verified runtime directory and supplies registry reads only; the supervisor receives the matching store for its one publication. The application ID is runtime's existing `InstanceId`; the delivered `DaemonRecord.instance_uuid` remains per daemon and is copied into the receipt's `daemon_instance_uuid` from observation.
The shared authority and evidence authority have different responsibilities: the former serializes writers across processes; the latter proves/control-authorizes one endpoint. Neither is replaceable by the other.
`_authenticate(release=False)` returns a current proof plus status reachability evidence; initial readiness separately requires idle. `release=True` awaits authority release and returns `Ok(None)`, not a fabricated proof. Fixture semantics remain in the handshake authority.
`_admit` requires `owned_daemon_control and keyring` and a valid proof; `_Daemon` stores/forwards `OwnershipProof` rather than extracting legacy token fields. `_request` passes `ownership_proof` and the original dispatch fence unchanged.
Supervisor public start checks `owned_daemon_control`, uses launcher argv, observes its exact child and calls public authority `prove`/`release`; its fixture branch keeps delivered foreground launch and handshake comparisons. It never writes receipts. Its config pipe and unchanged observation schema follow §3.1/§3.4, with token handling confined to the fixture authority; its own `DaemonRecord.lifecycle='stopping'` write remains observation only. On proved application death or parent mismatch, `_publish_release_intent` atomically writes §3.4's supervisor intent before the first signal, only for the specified nonterminal/no-receipt origins and matching own daemon observation. If that write cannot complete, `_request_release` refuses, publishes `cleanup_required` / `intent-unavailable` and exits without signalling; parent-death protection then applies, without converting refusal into cleanup success.
`_stop`/`shutdown`, `EvidenceAuthority.release` and namespace-probe teardown each supply a closure re-running design §6 steps 4–7 against their receipt after pidfd open. The public supervisor obtains this closure through `EvidenceAuthority.release`; the no-receipt case must still independently establish the same stage's facts. `_request_release` propagates typed refusal instead of swallowing it. Registry and supervisor retain cleanup obligations when release cannot be confirmed. Registry reconciliation of a supervisor intent writes `DaemonCleanupIntent`, terminalises a nonterminal receipt as `stopped` on proved absence or `failed` otherwise, retains the original supervisor file and never re-signals; an existing failed receipt stays byte-identical.
Transport `_start` uses one 5.0 s post-connect deadline at 0.1 s poll intervals for both source and challenge, with the start/cleanup result determined by §7. Pairing request signatures and each operation's delivered prerequisite list in §8 are unchanged by Amendment 1 §3.
`async def build_runtime(config: RuntimeConfig, overrides: Mapping[str, Any] | None = None) -> Result[Runtime]` remains the entrypoint. A `process` override must reach `_compose_production` and every production/probe launch boundary without selecting the delivered `deps`-override/no-op-registry path.

## 5. State machines and ordering

### 5.1 Receipt state and derived current

Under the registry's local lock and the shared-root authority lease, enumerate all receipts and group by endpoint `(runtime_directory, socket_name)`. An endpoint's current launch is the unique receipt in `{reserved, detached, bound, ready, stopping}` and must have the maximum `endpoint_generation` among **all** its durable receipts; no pointer file exists. A missing or non-unique current receipt is not inferred from recency or UUID order.
Zero current receipts means no current launch, not proof of no live process. Terminal failed evidence, supervisor observations and absence checks still constrain replacement.
Two or more current receipts for an endpoint mean `ownership_mismatch` plus `cleanup_required`: no adoption, replacement, signal or unlink of either candidate. A nonterminal receipt that is below the all-receipts maximum is stale but still blocks every live authorization, teardown and replacement; it cannot be promoted or ignored. A live authorization requires exactly one nonterminal receipt for the endpoint and that receipt must equal the maximum generation across every durable receipt. Cleanup using a terminal receipt is permitted only when its generation is that maximum and no nonterminal receipt exists for the endpoint; a stale terminal receipt beside a newer generation is never a signal or unlink authority. The next reservation always uses that same all-receipts maximum plus one and refuses on integer exhaustion.

| Transition | Precondition | Atomic payload change / failure outcome |
| --- | --- | --- |
| none → `reserved` | No current launch; prior uncertainty resolved; generation is `max(endpoint_generation)` over every durable receipt for this endpoint, terminal included, plus 1. | All reservation fields, explicit initial observation values; write failure means no spawn. |
| `reserved` → `detached` | Supervisor observation names its daemon-instance UUID and both identities; daemon is the exact spawned child. | Copy `DaemonRecord.instance_uuid` into `daemon_instance_uuid` and publish supervisor/daemon; application `instance_uuid` stays unchanged; mismatch retains evidence and refuses readiness. |
| `detached` → `bound` | Anchored entry, live peer and unique kernel socket row verify. | Entry pair, kernel inode, peer triplet; failure cleans only the exact provable child or retains uncertainty. |
| `bound` → `ready` | Proof still current, exact child still live, status accepts `idle`, credential evidence valid. | Status/credential label; durable ready precedes ready notice. |
| `reserved\|detached\|bound\|ready` → `stopping` | Shutdown/reconciliation admitted, including reserved before spawn. | Registry alone writes durable receipt stop intent, preserving all evidence, before any signal; no preceding launched-state restriction. |
| `reserved\|detached\|bound\|ready` or no receipt → supervisor release workflow | Recorded app proved absent/mismatched; own `DaemonRecord` names the same exact daemon; §3.4 correlation holds. | Supervisor atomically writes `release-intent.json` before its first signal, without changing a receipt. Unavailable intent means zero signals, `cleanup_required` / `intent-unavailable`, exit and retained obligation. |
| Supervisor intent → registry recovery | Shared lease held; exact intent/launch correlation checked and fresh absence evidence classified. | Write `DaemonCleanupIntent`; transition the correlated nonterminal receipt to `stopped` when absence is proved, `failed` otherwise; retain the supervisor file and never re-signal. Missing receipt retains its obligation; terminal receipt bytes never change. |
| `failed` → stopping workflow | Cleanup re-derived from independent verifier evidence; failed receipt immutable. | Publish `DaemonCleanupIntent` and complete it once from exact cleanup/absence evidence; it is not pre-signal intent. Without either permitted §6.3 intent send no signal and retain the obligation; never change receipt bytes. |
| `stopping` → `stopped` | Exact process absence and safe entry cleanup proven. | State/time only; receipt retained forever by teardown. |
| Any nonterminal → `failed` | Typed failure. | Failure code/time, every observed identity retained; terminal evidence immutable. |
| `failed\|stopped` → any receipt state | Forbidden. | Failed-launch stopping is recorded only in the separate cleanup intent; explicit archival outside teardown is separate work and requires its own archival receipt. |

The supervisor writes its delivered `DaemonRecord` lifecycle, including `stopping`, and the separate R10 release intent only; neither is a receipt write. After a crash, the registry reconstructs receipt observations from exactly one durable `DaemonRecord` with matching `app` and `runtime_directory` and lifecycle in `{starting, running, stopping}`, plus independent verifier evidence and agreement with any already-published identities. The config pipe is only for in-process correlation in its owning launch; after any restart this durable join is exclusive and the registry must neither require nor accept pipe data for correlation. A unique valid record fills a null `daemon_instance_uuid` without the original pipe; zero/multiple matches, including a stale competitor, refuse without promotion. A stale PID or record alone cannot promote a receipt. (R12)
A failed receipt cannot authorise a replacement or be erased after successful cleanup, retry or restart. The stopping workflow records failed cleanup through `DaemonCleanupIntent`, leaving terminal receipts immutable; that outcome record and a supervisor-only stopping observation never substitute for §6.3's two pre-signal alternatives. A missing intent after a crash supplies no permission or evidence of completion: re-derive verifier facts and retain uncertainty. A found supervisor release intent takes the recovery row above, including its no-re-signal rule; unproved absence yields terminal `failed` plus `cleanup_required`, never reported success. Recovery remains safe across a crash between cleanup-intent publication, receipt terminalisation and the intent's one completion update.

### 5.2 Shared owner and lease lifecycle

`acquire` uses `<state_root>/daemon/authority.lock`, `flock(LOCK_EX|LOCK_NB)` and one 5.0 s acquisition deadline with 0.05 s backoff, then inspects `<state_root>/daemon/authority.owner` while locked. Expiry produces `CONCURRENT_WRITER_DETECTED`, mapped to `ownership_mismatch` on the daemon path; retries never reset the deadline.
A proved-live different application owner refuses even between operations, when no flock is held. Provably absent prior owner permits reconciliation under the lock; it does not permit bypassing endpoint verification or discarding a live daemon.
Unreadable owner/PID/boot evidence retains `CleanupRequired`; never replace it by assuming absence. Publishing a new owner is atomic and happens when launch is admitted.
Each launch/reconcile/restart/shutdown acquires once and releases in `finally`. `shutdown` calls `_reconcile_locked`, never public `reconcile_previous` while already holding a lease; nested acquisition refuses.
Ordinary `unlock` leaves the durable owner intact for the daemon lifetime. `clear_owner(expected)` compares the exact owner and requires teardown/reconciliation absence; stale release preserves a successor.
Cancellation unlocks the flock and preserves owner/receipt obligations; after successful `clear_owner`, cancellation does not repeat removal or affect a successor.

### 5.3 Production launch and failure windows

| Ordered boundary | Required observation/publication | Failure disposition |
| --- | --- | --- |
| Structural eligibility | Phase activation requires installed lifecycle seams, shared authority and credential-proof producer; isolated bootstrap requires exactly derivable owned control → wired source verifier → constructible receipt store + endpoint verifier + strict teardown → resolvable `unshare`. | Stop at first absence; typed gated detail; zero backend/probe launches. Bootstrap never requires the proof it produces or constructs the registry. |
| Credential witness | One outer namespace invocation, service introspection once, two inner backend launches with distinct launch UUIDs/receipts: positive reachable-bus socket+idle within 3.0 s plus 2.0 s idle confirmation and strict teardown; negative unreachable-bus startup observed nonzero with no socket/file; 3.0 s termination grace. | Probe runs once before registry construction; capture both legs at the inner process boundary, including negative environment/completion. Keyring false on failure; name failed leg; cleanup or retained probe obligation, no stream admission. Registry `_compatible()` checks keyring only downstream. |
| Authority + reservation | Acquire lease, derive current/generation, publish initial reserved receipt before spawn. | No spawn on authority, ambiguity or publication refusal. |
| Spawn | Supervisor starts only the idle argv, no target/capture/secret arguments. | Retain failed evidence; no process if spawn never happened. |
| Identity publication | Observe supervisor and direct backend child; backend identity equals spawned child. | Exact child pidfd cleanup if provable; otherwise no signals, cleanup required and replacement blocked. |
| Socket entry | Resolve through anchored dirfd, capture entry device/inode. | No unlink of a replacement/foreign entry; retain evidence. |
| Peer proof + bound receipt | `SO_PEERCRED`, unique socket row, FD holder, ticks/UID/argv, unchanged entry, boot/generation. | Exact candidate cleanup only with independent identity proof; failed receipt retained. |
| Idle + ready publication | Accepted public-shape idle status, current proof after I/O, atomic ready, then recheck exact child/proof after the write before the notice. | Non-idle/status/write/death failure never emits ready; exact cleanup or retained uncertainty. |
| Running/failed → stopping workflow | §6.3's permitted pre-signal intent, strict signals with post-open stage revalidation, exact exit proof, anchored unlink; `DaemonCleanupIntent` records failed cleanup but is no third signal intent. | Any unconfirmed step retains obligation and cleanup-required notice; no replacement; failed/stopped receipts stay immutable. |
| Application death / parent mismatch → supervisor release | Prove §3.4's app/receipt/own-daemon conditions, atomically publish supervisor intent, then strict exact-child teardown. | Intent unavailable: no signals, `cleanup_required` / `intent-unavailable`, supervisor exit and parent-death protection; never successful cleanup. Recovery records cleanup outcome and terminal receipt, retains intent, never re-signals. |

Inject crashes at **reservation write, spawn, supervisor publication, bind, receipt write, status poll, stopping write, signal, exit proof and unlink**. Tests must identify the actual boundary, not just a lifecycle label; failed-launch cases include intent publication and its one completion update.
The pre-spawn window leaves failed receipt and no child; spawn→bind cleans the verified direct child; bind→receipt can complete only from verified entry/peer; receipt→running requires idle; running/failed→stopping follows §6.3's intent rule. On a launch failure requiring signals, the registry publishes `stopping` before signalling and defers terminal `failed` until exact cleanup completes or refuses; once failed is durable it remains immutable, and a cleanup-outcome intent cannot create a new signalling path.

### 5.4 Connect and credential-challenge lifecycle

Only Amendment 1 governs the changed predicate: `owned_daemon_control and multi_target and (physical_source or virtual_source) and trust-not-blocked and dispatch-fence-valid`.
`CleanupRequired`/`RevocationPending`, alias-target cleanup and endpoint/receiver/fingerprint freshness retain delivered refusal. Transport admission independently retains composition-proven keyring and provider requirements.
A connect must carry no credential. After its acknowledgement, use one 5.0 s total deadline, polling at 0.1 s intervals, to observe both a challenge and complete source evidence for the exact target's stream; neither observation restarts the deadline, and no `ready` or successful published stream precedes verification.
Challenge in acknowledgement **or status** → `_stop_owned` for that exact target → apply §7's start/cleanup failure precedence, with typed start refusal `dependency_incompatible`; no prompt, submit or pairing attempt.
Unverifiable/mismatched/ambiguous/absent source or source-wait failure → exact-target `_stop_owned` → apply the same §7 rule with typed start refusal `unsupported_source`. A different target's report cannot satisfy the wait.
Cancellation or target replacement retains delivered acquisition barriers and cleanup ownership. No stale start may disconnect a successor; unconfirmed cleanup remains an obligation rather than reported success.
All pairing operations retain **their own** delivered prerequisites (§8), as preserved by Amendment 1 §3; no operation acquires an “all four former connect flags” requirement. Remembered trust, provisional-commit compensation, cancellation, revocation, audio restart and mode query retain delivered requirements; public flags remain false.

## 6. Evidence algorithms

### 6.1 Read-only endpoint verification

Run the following checks in order; every refusal closes temporary FDs/connections and performs no write, signal or unlink.

1. Open the runtime path from `/` using component-wise `O_NOFOLLOW|O_DIRECTORY`; require owner UID, mode 0700 and reserved directory device/inode.
2. Stat the socket basename relative to that dirfd without following links; require socket type and owner UID, matching bound receipt entry identity when already recorded.
3. Connect an `AF_UNIX` socket to the anchored entry, read `SO_PEERCRED`, require owner UID, then close without sending a control request.
4. Require exactly one `/proc/net/unix` row for the exact absolute path; require that peer PID holds an FD to that row's **kernel** socket inode. Zero/duplicate rows or only foreign holders refuse.
5. Require present identity evidence for the peer, peer PID/ticks equal to the receipt's daemon, and `/proc/<pid>` UID equal to the reserved UID. Unreadable evidence is not absence.
6. Parse NUL-separated argv; require `-daemonize` and exactly the recorded absolute path as `-socket` value, without a second different socket value. Do not accept substring matches.
7. Restat through the original dirfd; require the unchanged entry pair. Replaced/unlinked listener or entry refuses even if an old daemon still owns another socket FD.
8. Derive endpoint generation from all durable receipts for `(runtime_directory, socket_name)`, terminal included. A live authorization requires exactly one nonterminal receipt at the maximum generation. A terminal cleanup candidate is eligible only if it is itself at that maximum and no nonterminal receipt exists; an older terminal receipt beside a newer generation is stale and cannot signal or unlink. Require current boot ID and that derived generation. A superseded generation is `stale_event`; only successful checks produce `VerifiedEndpoint`.

`authorize` and `confirm` repeat peer/row/identity/argv/entry checks around I/O. A bound current receipt changed in generation or entry identity invalidates every pending mutation with `stale_event` as specified by design §14.3.

### 6.2 Source verification and producer completeness

The producer must receive source type, positive width/height and an explicitly present pair of integral positions before reporting usable source. Missing position is never `(0,0)`; explicit `(0,0)` is valid.
`display/inventory.py` produces `tuple[MonitoredOutput, ...]` from **one** fresh `kscreen-doctor -j` read, with geometry and origin taken from that same JSON document. Reuse delivered geometry parsing (including explicit signed positions) and the exact-true enabled/connected checks and known physical connector types; do not join separate observations or manufacture missing geometry.
Before uniqueness, exclude disabled outputs only. Count every **enabled** entry matching the explicit `(x,y)` position, including disconnected, virtual and unknown-type entries; exactly one enabled position match is required. Unknown enabled state or missing/unreadable geometry/scale on any enabled entry prevents a complete comparison and refuses rather than silently dropping that entry; any non-object output entry refuses the entire inventory read without partial records. Disabled entries cannot create ambiguity. Corroborate the unique match's current-mode size with `(round(reported.width / scale), round(reported.height / scale))` using its positive finite observed scale. The match must name the requested connector, be neither virtual nor helper-owned, and meet the delivered physical rule: connected **and** known physical connector type. The enabled requirement is already established by membership in the uniqueness population. Negative x/y are valid; source dimensions must be positive.
Share the delivered `capture.physical.PHYSICAL_OUTPUT_TYPES` `{1,2,3,4,5,6,7,14}` and its delivered textual equivalents/name checks through this inventory provider; preserve compatibility exports rather than duplicating rules. `KscreenConnectorInventory` projects the same provider's tuple to connector names for `PhysicalCaptureProvider`, retaining the delivered name/origin checks even when geometry is unavailable for capture inventory. `virtual` is carried evidence, never a substitute for the known-type rule; do not use `display.kscreen`'s broader physical heuristic as physical authority.
A stale/unreadable observation, absent source, window/region, another physical connector, zero/multiple matches or unknown origin refuses. Capture verification and audio/portal permission remain additional delivered prerequisites.
The activation commit revises patches 0001 and 0003 together with the consumer, records all 0001→0002→0003 build steps and the rebuilt binary identity. The installed two-token build cannot enable Mirror.

### 6.3 Strict teardown and credential probe

Classify stage and identity first: proved absent succeeds without signals; live evidence must match the expected identity and UID; mismatch or unreadable evidence refuses. A strict pre-bind candidate requires `TerminationEvidence(stage='pre_bind')`, the exact live child ticks/UID/argv and recorded live-supervisor parent relationship. Only after those child-identity facts positively match may a later bind/readiness verification failure trigger exact-child cleanup. A strict bound candidate requires `TerminationEvidence(stage='bound')` and the complete §6.1 endpoint chain. A mismatch in either stage is never converted into a candidate for cleanup.
Revalidate identity after opening the descriptor, including ticks/UID, then invoke the supplied synchronous `revalidate()` closure before the first signal. `strict=True` requires stage-complete `TerminationEvidence` and a closure that re-runs the same stage against current evidence: pre-bind repeats the exact child/supervisor/argv checks, while bound repeats design §6 steps 4–7 against the receipt (socket-row/FD holder, daemon ticks/UID, exact argv, unchanged anchored entry). The returned evidence must retain the original stage and identities. Any non-`Ok`, including an `Err` first observed after open, closes the descriptor and sends zero signals; a pre-open check cannot satisfy this obligation. The port carries no receipt type.
The strict-teardown durable pre-signal intent is exactly one of (i) the receipt's `stopping` state written by the registry (the isolated child receipt writer for a probe), or (ii) the supervisor's `SupervisorReleaseIntent`, published under §3.4's conditions. Both must precede the first signal; neither authorises any other action. A delivered `DaemonRecord` stopping observation or `DaemonCleanupIntent` alone is never a substitute. After the applicable intent and independent stage proof, send SIGTERM through pidfd, wait for descriptor-observed exact exit, then SIGKILL through pidfd if needed and require exit within delivered `LinuxProcessRunner.kill_timeout`. Non-child teardown does not require `waitpid`; a failed receipt remains unchanged when its cleanup-outcome intent is completed once. A pre-bind verification failure cleans only the exact recorded child and must observe its death before resolving the obligation. Unreadable identity produces zero signals, retains uncertainty and blocks replacement; a bound identity/endpoint mismatch refuses and retains the cleanup obligation without signalling or unlinking. Supervisor intent publication failure sends no signal, publishes `cleanup_required` / `intent-unavailable` and exits with parent-death protection; refusal is not success. Registry recovery of that intent writes `DaemonCleanupIntent`, terminalises the nonterminal receipt as `stopped` on proved absence or `failed` otherwise, retains the supervisor file and never re-signals.
Use delivered `TERMINATION_GRACE = 3.0` for owned teardown, including probe teardown, and the existing overall shutdown bound of 15 seconds; never restart a full bound after each await. Probe socket+idle has 3.0 s total followed by 2.0 s idle confirmation; post-connect and flock bounds are specified in §5.
Only proven exit permits rechecking and unlinking the same anchored stale entry, marking a nonterminal stopping receipt stopped or completing failed-launch cleanup without a receipt rewrite, and compare-clearing owner. A still-listening/replaced entry is retained, never unlinked based merely on recorded process death.
`strict=True` and same-stage post-open revalidation are required from registry `_stop`/shutdown, evidence-authority release, public supervisor `_request_release` and probe teardown. Evidence is receipt-backed wherever a receipt exists; R10's no-receipt supervisor path independently verifies the saved reservation and own daemon observation without fabricating a receipt. Non-owned delivered callers keep their existing optional numeric fallback.
Probe launch is `unshare -rn <python> -m airplay_linux.process.credential_probe --socket <private-socket> --record-root <parent-created-probe-root>`; the parent creates a unique root, verifies mode 0700/UID/no-symlink, and opens it as a dirfd before launch. One outer invocation contains both backend legs. The namespace child uses a receipt store rooted only there and writes a separate reservation/receipt for each distinct positive/negative launch UUID, with leg-private runtime/socket paths, while observing each direct backend child, peer and socket-row evidence in that same namespace; it never writes the application registry root. The namespace child writes a strict `ProbeVerdict` and sibling digest after both legs' execution and strict exact teardown/absence checks, whether the completed witness passed or failed; it exits zero only after both files are fully published. The parent verifies both files and exit status before constructing `CredentialProofVerdict`. Only an all-passed verdict with both receipts is archived and permits probe-root removal. A well-formed failed-leg verdict remains in the private root as evidence; setup, publication or cleanup failure/refusal produces no trusted verdict and retains every file/receipt present. The namespace child never writes to the application registry root; stdout/stderr never encode the verdict.
The service leg is one existing-client Secret Service introspection, without unlock/create. The positive backend leg uses a reachable bus, requires keyring construction and idle status, and completes strict teardown before the negative backend leg begins. The negative leg is a fresh launch with an unreachable `DBUS_SESSION_BUS_ADDRESS`, requires observed nonzero completion, no control socket at its private path and no credentials file. Its expected nonzero backend exit is distinct from the namespace child's required zero exit after completed witness publication. Observe both direct backend `-daemonize` invocations through the same injected inner `ProcessPort` (`run` and `spawn` instrumentation): record leg, distinct launch/receipt identity, the selected bus environment and actual exit/absence evidence. This test observation does not add an environment dump to any durable record or verdict. The complete witness has one outer `unshare` and two inner backend launches; each single-leg subtest has one inner launch.
Every probe launch follows child-local receipt and strict teardown rules; the private probe root provides its single-writer boundary and the child never opens application shared authority/receipt state. It reports only an allowlisted verdict and retains failed evidence on failure. It follows §4.1's isolated bootstrap before registry construction: exactly derivable owned control, wired source verifier, constructible receipt store + verifier + strict teardown and resolvable `unshare`; `keyring` is an output and is checked by registry compatibility downstream. The installed phase guard and all lifecycle seams still precede any probe. §10.3 records the outer `unshare` separately and observes each direct backend `-daemonize` spawn at the process port that performs it; no nested argv inference substitutes for that observation.
Per-receiver Stop sends only authorized `disconnect <exact target>`. Shared daemon termination occurs only in serialized whole-daemon lifecycle cleanup, never as a receiver Stop side effect.

## 7. Error and side-effect contract

Diagnostics use the delivered fixed catalog. Details may name an allowlisted prerequisite/leg or the fixed `intent-unavailable` reason, never backend exception text, paths beyond the runtime directory, owner tokens, PINs, passwords, credentials/references or environment content. Durable metadata has the same secret prohibition; per-leg environment/completion observations belong to the test recorder, not an environment dump in a new document.
“Retain” below includes receipt, owner/cleanup obligation and `cleanup_required` notice when a launch may exist. “Exact cleanup” requires §6.3's applicable durable pre-signal intent (registry-written receipt `stopping` or supervisor release intent), independent identity proof and strict pidfd teardown with post-open stage revalidation; neither intent authorises another action. Cleanup-outcome records and observations cannot replace that pre-signal requirement. Supervisor-intent recovery never re-signals.
Start/cleanup failure precedence: when a start's exact-target disconnect cannot be confirmed, return `cleanup_timeout`, retain the stream and cleanup obligation, and give that result precedence over the typed start refusal. When disconnect is confirmed, return the typed refusal: `dependency_incompatible` for a credential challenge, `unsupported_source` for a mismatched, absent or otherwise unverifiable reported source. §5.4 and the rows below use this one rule.

| Condition | `ErrorCode` / outward result | Written / signalled / unlinked |
| --- | --- | --- |
| Launcher surface/private root/production graph absent; ambient socket configured | `dependency_incompatible` | Gate detail only; no backend/probe launch, signal or unlink. |
| Another proved-live owner; flock not acquired within 5.0 s / 0.05 s backoff; nested acquire | `ownership_mismatch`; daemon-path mapping of `CONCURRENT_WRITER_DETECTED` on flock expiry | Preserve owner; no spawn, signal or unlink. |
| Owner/identity/boot evidence unreadable or contradictory | `ownership_mismatch` | Retain cleanup required; no blind removal or signals. |
| Multiple nonterminal receipts | `ownership_mismatch` | Retain all; no adoption/replacement/teardown. |
| Unknown schema/version, missing/extra/partial or otherwise malformed new document; failed atomic publication | `persistence_failed` at new store boundary | R9/R13 cover only reservation, receipt, cleanup intent, supervisor release intent, authority owner and credential verdict. Preserve bytes/evidence; no next lifecycle side effect; terminal receipts, supervisor intents and completed cleanup intents stay immutable. Legacy codecs/validation/tolerance remain delivered and are never replaced by a new-codec pass. |
| Supervisor intent predicates not proved: recorded app identity still matching/live or unreadable, ineligible receipt state or mismatched own daemon observation | Relevant `ownership_mismatch` / `persistence_failed` refusal | No new supervisor intent or signals from this path; retain obligation, never use `DaemonRecord` alone as intent. |
| Supervisor release intent cannot be atomically published | `persistence_failed`; `cleanup_required` with reason `intent-unavailable` | Zero supervisor pidfd/numeric signals; retain evidence and exit, allowing parent-death protection to apply; never return successful cleanup. |
| Registry finds supervisor release intent | `Ok(None)` only after proved absence and safe cleanup; otherwise relevant refusal plus `cleanup_required` | Convert to `DaemonCleanupIntent`, terminalise the nonterminal receipt as `stopped` on proved absence or `failed` otherwise, retain supervisor file, never re-signal. Missing receipt/uncertainty remains an obligation; existing terminal bytes stay unchanged. |
| Superseded receipt generation or changed current receipt entry (§14.3) | `stale_event` | Invalidate proof, retain evidence; no mutating I/O or new launch. |
| Missing/replaced/unlinked socket entry or entry changes during verification | `stale_socket` | No signal/unlink of foreign entry; exact candidate cleanup only if independently proven. |
| UID, runtime identity, daemon PID/ticks, argv or peer mismatch | `ownership_mismatch` | Verifier reads only; candidate failure path retains or exact-cleans, never signals replacement. |
| Zero/duplicate Unix path rows; peer not holder; ambiguous/unreadable FDs | `ownership_mismatch` | No best-candidate selection, signal or unlink. |
| Pidfd unavailable for live exact candidate | `dependency_incompatible` | Retain obligation; zero pidfd and numeric signals; no unlink. |
| Missing or incomplete stage-specific `TerminationEvidence`, or callback returns a different stage/identity | `dependency_incompatible` for incomplete strict input; relevant evidence refusal for changed evidence | Close any opened pidfd; zero pidfd/numeric signals; retain the cleanup obligation. |
| Identity changes or receipt revalidation returns non-`Ok` after pidfd open | Relevant evidence refusal (`ownership_mismatch` for identity mismatch) | Close descriptor; zero pidfd/numeric signals; retain obligation and immutable failed evidence. |
| Startup timeout, child exit, non-idle readiness or failed status reachability | `daemon_failed` | Never ready; retain failed evidence; exact cleanup or uncertainty blocks replacement. |
| Missing unshare/backend evidence or failed credential witness | `ownership_mismatch` in proof, `dependency_incompatible` at admission | Bootstrap absence means zero outer/inner launches; a complete admitted witness records one outer invocation and both positive/negative backend launches with separate UUIDs/receipts, including negative environment and completion. Admitted failure leaves keyring false, fixed failed-leg detail, probe cleanup/retention and no stream; registry keyring check is downstream. |
| Strict wait/kill/cleanup not confirmed | `cleanup_timeout` | Retain stop obligation and cleanup-required notice; no false stopped/clear-owner. |
| External shutdown request or missing authority/proven record/fence | `dependency_incompatible` | No ctl shutdown or mutation; no signal/unlink. |
| Receiver/endpoint/fingerprint fence mismatch | `stale_target` | Refuse before mutating I/O; preserve successor and its stream. |
| Trust cleanup/revocation pending | `trust_cleanup_required` | No connect/pairing/launch; retain delivered trust obligation. |
| Required upstream public composition element absent | `dependency_incompatible` | Name missing element; zero launches, including the probe, at both process boundaries; phases 1–3 require zero `unshare` invocations. |
| Source decode malformed | `dependency_incompatible` at decoder; unverified start uses the §7 precedence rule | Never ready; exact-target disconnect if connect already happened. |
| Complete source token/verifier/inventory absent; wrong, absent or ambiguous reported source | `unsupported_source` for start, subject to the §7 precedence rule | Physical flag false where prerequisite absent; no new launch if structurally ineligible; count every enabled geometry match before connected/type/requested-name checks, exclude disabled entries from uniqueness; exact-target cleanup after connect. |
| Credential challenge on connect acknowledgement or status | Start/cleanup result follows the §7 precedence rule | No prompt/secret/pairing or ready publication; exact-target cleanup, preserving other targets. |
| Post-connect 5.0 s source/challenge deadline expires (0.1 s polls) | Start/cleanup result follows the §7 precedence rule | No ready; exact-target cleanup; retain any unconfirmed stream/obligation. |
| Pairing operation lacks one of its own delivered prerequisites (§8) | Delivered `dependency_incompatible` | Zero request I/O; Amendment 1 §3 preserves each operation's requirement list, with public pairing flags false. |
| Unsupported operation/payload/secret-bearing public CLI form | Delivered `unsupported_operation` / `invalid_input` / `dependency_incompatible` as applicable | Refuse before I/O; no credential argv or metadata. |
| Public receiver rejection / mute failure / audio unavailable | Delivered `receiver_rejected` / `audio_mute_failed` / `audio_unavailable` | Existing target cleanup/audio policy; no gate widening or backend text. |

A failed mutation confirmation returns the relevant evidence refusal before exposing its response. It cannot undo already performed backend I/O; retained cleanup handles that uncertainty without claiming success.

## 8. Capability derivation and operation truth tables

Let `L` mean captured launcher eligibility including production graph, private root and no ambient socket; `C` means all three credential legs passed, with two distinct backend launches/receipts inside one namespace invocation; `R` means exact patched surface plus all three tokens; `V` means the actual transport verifier object is wired; `I` means affirmative physical monitor inventory.
Pure derivation is `owned_daemon_control = L`, `source_reporting_complete = R`, `physical_source = R and V and I`, `keyring = C`. Eligible execution additionally requires all structural lifecycle seams and no ambient socket. Ownership capability alone is not launch admission.

| Evidence case | owned_daemon_control | source_reporting_complete | physical_source | keyring |
| --- | --- | --- | --- | --- |
| Help/raw probe, published surface | false | false | false | false |
| Help/raw probe, patched two-token surface | false | false | false | false |
| Help/raw probe, exact complete three-token surface | false | true | false | false |
| Complete production composition, `L=C=R=V=I=true` | true | true | true | true |
| Same except ambient socket configured | false | true | true if V/I remain wired | C; execution still refuses before probe/launch |
| Same except launcher/private root/production origin absent | false | R | R and V and I | C; no probe when structurally ineligible |
| Same except complete token absent | L | false | false | C; Mirror gated |
| Same except verifier removed | L | R | false | C; zero launches at ordered eligibility |
| Same except physical inventory absent | L | R | false | C; Mirror gated |
| Same except one credential leg fails/not run | L | R | R and V and I | false; zero stream admission |
| Unknown/partial help or tokens without matching surface | false in raw probe | false | false | false |

`C` in ineligible rows describes supplied pure input only; runtime must not execute a probe to obtain it after structural failure. No table row infers credentials from `-cred-backend keyring`, help, a version or previous success.
`source_reporting=True` remains valid for the exact two-token build. Neither the current carried chain nor installed binary at the pinned source advertises completeness; activation must supply the revised build evidence.

| Operation | Public authority capability requirement | Additional request boundary |
| --- | --- | --- |
| status/discover/devices | `allows=True` | Accepted endpoint replies covered by prove/confirm; no legacy ownership fields required. |
| disconnect | owned control and multi_target | Exact target and matching dispatch/cleanup fence. |
| mute/unmute | owned control and multi_target and target_mute | Exact target and matching fence. |
| connect | Amendment 1 predicate verbatim (§5.4) | Clean trust, valid fence, composition keyring admission and post-connect challenge/source verification. |
| shutdown | owned control | No target/fence; internal authority release path only, strict teardown. |
| begin_pairing | Delivered `target_bound_pairing and cancel_pairing` | Owned proof plus delivered trust/target fences; public flags false. |
| submit_pairing / prepare_pairing | Delivered `target_bound_pairing and keyring and provisional_commit` | Owned proof and unchanged challenge/transaction fences. |
| commit_pairing | Delivered `keyring and provisional_commit` | Owned proof and unchanged provisional-commit compensation. |
| cancel_pairing | Delivered `cancel_pairing` | Owned proof and exact challenge/cancellation fences. |
| revoke_pairing / mark_invalid | Delivered `revoke_pairing` | Owned proof and unchanged revocation/cleanup fences. |
| start_audio / restart_audio; query_modes | Delivered `audio_restart`; delivered `query_modes` | Both flags remain false publicly; existing typed refusals remain. |

The delivered pairing rows are recorded exactly, with each operation checked against its own list: begin requires `target_bound_pairing and cancel_pairing`; submit/prepare require `target_bound_pairing and keyring and provisional_commit`; commit requires `keyring and provisional_commit`; mark_invalid/revoke require `revoke_pairing`; cancel requires `cancel_pairing`. These row predicates are independent: never combine them into a shared superset or require unrelated former-connect flags. Amendment 1 §3 preserves every list, while new **§6 Clarification 1.1** changes only connect; public pairing remains closed. The §10 matrix tests each operation with all unrelated former-connect pairing flags false, then removes each required flag independently.

| Lifecycle / storage evidence case | Required result |
| --- | --- |
| App proved absent/mismatched; receipt `reserved\|detached\|bound\|ready` or absent; own `DaemonRecord` names the same exact daemon | Supervisor may atomically publish its release intent; independent stage proof and strict post-open checks still precede signals. Registry-only receipt writes remain intact. |
| Registry receipt already `stopping` | Its durable stop intent satisfies the registry alternative; the supervisor does not create a new R10 intent for this state. |
| Recorded app identity still matching/live or unreadable, receipt terminal or own daemon mismatched | No new supervisor release intent and no signals through that path; retain required cleanup. |
| Supervisor intent publication fails | `cleanup_required` / `intent-unavailable`, zero supervisor signals, exit with parent-death protection; refusal is never cleanup success. |
| Registry recovers supervisor intent; exact daemon absence proved / not proved | Record `DaemonCleanupIntent`; nonterminal receipt becomes `stopped` / `failed`, retain supervisor file and never re-signal; uncertainty retains `cleanup_required`. |
| Full credential witness / single backend-leg subtest | One outer `unshare` plus two inner backend launches / exactly one inner launch per subtest; distinct full-witness UUIDs/receipts and observed negative environment/nonzero/no-socket/no-file completion. |
| Restart at null-daemon-UUID gap; unique durable match / zero or multiple matches | Reconstruct with independent verification / refuse, under §5.1's exclusive recovery rule; a stale competitor cannot be selected away. |
| New document / legacy document | Apply the named strict matrix / retain delivered codec, validation and tolerance; never pass legacy bytes through new codecs. The delivered-store legacy control round-trips byte-identically. |

## 9. Migration checklist — every design §4.4 consumer

Each row is required in the same activation commit; fixture-only old-flag reads must be visibly separated from public admission.

| File | Symbol/site | Delivered behaviour that changes |
| --- | --- | --- |
| `src/airplay_linux/policy/capabilities.py` | `ADMISSION_BACKEND_CAPABILITIES` | Old foreground/handshake/keyring triplet → owned control/keyring pair. |
| `src/airplay_linux/policy/capabilities.py` | Mode rubric (`EnvironmentProbe` report) | Same admitted/Mirror formula shape, composition-derived inputs and physical-source proof. |
| `src/airplay_linux/transport/doubletake.py` | `DoubleTakeTransport._admit` | Old triplet/Handshake field extraction → owned/keyring plus OwnershipProof. |
| `src/airplay_linux/process/daemon_registry.py` | `DaemonRegistry._compatible` | Old triplet → owned/keyring; lifecycle seams still required. |
| `src/airplay_linux/process/supervisor.py` | `DaemonSupervisor._run` start check | Public owned-control start/idle argv; fixture triplet remains in fixture branch. |
| `src/airplay_linux/backend/control.py` | `PublishedDoubletakeControl._available` | Handshake flag as mutation/shutdown gate → authority.allows and authorization. |
| `src/airplay_linux/backend/control.py` | `_available(connect)` | Old pairing-inclusive predicate → Amendment 1; no other operation changes predicate. |
| `src/airplay_linux/backend/control.py` | `handshake` | Public handshake refusal → authority.prove(record), returns evidence proof. |
| `src/airplay_linux/backend/control.py` | `_revalidate_ownership` | Backend identity equality probe → authority.authorize before I/O and confirm after I/O. |
| `src/airplay_linux/backend/control.py` | `_ownership_check` | Public status ownership-metadata comparisons removed; fixture comparisons retained. |
| `src/airplay_linux/transport/doubletake.py` | `_start`, `_request` | Handshake requirement → same OwnershipProof forwarded; verified exact-target source before ready. |
| `src/airplay_linux/process/daemon_registry.py` | `_authenticate` readiness | Handshake/status identity metadata → authority.prove plus status reachability/idle readiness. |
| `src/airplay_linux/process/daemon_registry.py` | `_authenticate(release=True)` | Handshake + ctl shutdown → authority.release with strict pidfd. |
| `src/airplay_linux/process/supervisor.py` | `_request_release` | Public handshake + ctl shutdown → prove/release, typed failure retained; fixture path retained. |
| `src/airplay_linux/runtime.py` | `_control_factory` | Token/instance/protocol profile binding → current receipt/authority/fence source; legacy bindings fixture-only. |
| `src/airplay_linux/runtime.py` | `_capability_gates`, startup detail | Missing old flags → authority name, credential failed leg and source-verifier state. |
| `README.md` | Ownership/Mirror guarantee and refusals | Cooperative same-user evidence guarantee, exact refusal preconditions and residual disclosure. |

## 10. Test specification

Tests below are required implementation artifacts, not claims of executed verification. New test names are exact; fixture names define the required deterministic evidence seam. Mutations run in disposable copies, with a passing unchanged control and observed mechanism-specific failure.

### 10.1 M1–M10 named killing tests

| ID | Exact module :: test function | Fixture | Mutation to kill and assertion pinning it |
| --- | --- | --- | --- |
| M1 | `tests/backend/test_contract.py::test_owned_control_is_composition_only` | `composition_capability_cases` | Set owned flag true in `probe_backend`; assert raw probe/decode false, eligible production derivation true, ambient case false, public old flags false. |
| M2 | `tests/storage/test_launch_receipts.py::test_receipt_generation_roundtrip_and_missing_member_refused` | `receipt_generation_37` | Omit encoded endpoint_generation; assert exact 37 round-trip, required daemon_instance_uuid member (explicit null before observation), missing member raises RecordError, reopening derives the same current receipt/generation. |
| M3 | `tests/process/test_ownership.py::test_different_socket_argv_refuses_before_control_or_signal` | `owned_endpoint_evidence` | Accept different socket argv; vary only socket value with UID/ticks/entry/row/peer fixed valid; assert ownership_mismatch and zero status/mutation/signal observations. |
| M4 | `tests/process/test_ownership.py::test_peer_start_ticks_must_equal_receipt`; `tests/process/test_linux_runner.py::test_strict_receipt_revalidation_after_pidfd_open_refuses_without_signals` | `owned_endpoint_evidence`; `strict_signal_observer` | Case 1 ignores ticks equality: vary only observed ticks, assert ownership_mismatch. Case 2 skips the post-open callback: closure returns `Err` only after pidfd is open, assert invocation order, returned refusal and zero pidfd/numeric signals. Retain the separate identity-reuse case. |
| M5 | `tests/process/test_linux_runner.py::test_strict_no_pidfd_never_signals_numeric_pid` | `strict_signal_observer` | Enable numeric fallback; otherwise-owned live identity with pidfd unavailable must return dependency_incompatible; independent numeric-signal observer sees zero, not merely a mocked terminate call. |
| M6 | `tests/process/test_authority.py::test_shared_flock_serializes_owner_publication` | `two_registry_barrier` | Remove flock; two independent authority/registry instances share a root and pause before owner publication; assert at most one launch, loser ownership_mismatch. Guard removal lets both reach publication. |
| M7 | `tests/process/test_owned_launcher.py::test_pre_bind_verification_failure_observes_exact_child_death`; `tests/process/test_owned_launcher.py::test_pre_bind_unreadable_identity_retains_without_signals_or_replacement`; `tests/process/test_owned_launcher.py::test_bound_mismatch_refuses_and_retains_cleanup_obligation` | `candidate_failure_cases` | Split strict teardown outcomes: after exact pre-bind child ticks/UID/argv/supervisor evidence positively matches, a later bind/readiness verification failure observes death of only that child; unreadable identity sends zero signals and retains uncertainty/blocking; bound mismatch refuses without signal/unlink and retains the obligation. Mutations that collapse stages or treat unreadable/mismatched evidence as absence must fail the corresponding case. |
| M8 | `tests/process/test_owned_launcher.py::test_failed_receipt_survives_cleanup_retry_and_restart` | `reopen_receipt_store` | Delete/rewrite receipt on failure/cleanup; reopen after failure, cleanup-intent publication/completion, successful exact cleanup, retry and fresh registry restart; same failed bytes remain and never authorise replacement. |
| M9 | `tests/acceptance/test_no_production_spawn.py::test_missing_source_verifier_blocks_all_production_launches` | `public_production_graph` | Bypass verifier eligibility/force ownership admission; remove only wired verifier with all other fakes valid; assert physical_source false, typed refusal, recorder.run and recorder.spawn have zero launch attempts. |
| M10 | `tests/acceptance/test_offline_tripwires.py::test_unmarked_live_boundary_attempts_are_failures` | `offline_boundary_tripwires` | Permit default live paths; unmarked test actively attempts backend execution, real session-bus creation and live socket lease; each fails on the attempt even if dependency absent. |

### 10.2 Additional design §14.1 regression obligations

| Exact module :: test function | Fixture / pinned mechanism |
| --- | --- |
| `tests/backend/test_control.py::test_connect_does_not_consult_each_former_pairing_prerequisite` | `public_shape_control`; independently vary target_bound_pairing, provisional_commit, cancel_pairing, revoke_pairing while valid owned/source/trust/fence inputs remain; connect admits with each false. |
| `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags` | `pairing_operation_requirement_cases`; each of begin, submit, prepare, commit, mark_invalid, revoke and cancel admits when its own delivered flags are true even if every unrelated former-connect pairing flag is false; independently remove each required flag and require zero I/O. Mutation adding a shared superset/all-pairing-flags gate or dropping one required flag is killed. Public pairing remains closed; Amendment 1 changes connect only. |
| `tests/process/test_daemon_registry.py::test_receipt_joins_application_and_observed_daemon_instance_without_token` | `application_daemon_identity_cases`; application ID, launch UUID and observed daemon ID are distinct; receipt records runtime InstanceId plus copied daemon_instance_uuid, mismatched observation refuses, no receipt serialization/repr contains owner_token. |
| `tests/process/test_daemon_registry.py::test_restart_reconstructs_unique_observation_gap_without_config_pipe` | `application_daemon_identity_cases`; reopen a fresh registry at the gap where receipt `daemon_instance_uuid` is null and exactly one durable app/runtime record has lifecycle starting/running/stopping. No original pipe exists; fresh exact child/supervisor/UID evidence yields the expected copied daemon UUID/identities and receipt advance. Supplying stale or conflicting pipe bytes cannot change that durable result. Mutations requiring a pipe or accepting its data as restart correlation fail. |
| `tests/process/test_daemon_registry.py::test_observation_join_refuses_ambiguous_restart_records` | `application_daemon_identity_cases`; zero or multiple app/runtime/lifecycle candidate records refuse, including a null-daemon-UUID observation-to-receipt crash gap with a stale competing record; no recency/UUID choice or receipt promotion, even if supplied pipe data claims a winner. Mutations selecting the newest UUID/record or using pipe data to discard the competitor must be killed. |
| `tests/process/test_supervisor.py::test_public_config_and_fixture_record_schema_preserve_identity_channels` | `authority_config_cases`; delivered DaemonRecord schema/codec unchanged including owner_token; public pipe has delivered non-token members plus launch_uuid/endpoint_generation and token is unused; fixture pipe/handshake still use owner_token. |
| `tests/backend/test_contract.py::test_source_position_must_be_present_and_integral` | `complete_source_documents`; missing X/Y, bool/string/null/unknown members refuse; explicit zero and negative positions survive decoding. |
| `tests/capture/test_reported_source.py::test_same_size_other_position_is_not_requested_source` | `fresh_layout_cases`; same dimensions, different signed position cannot alias requested monitor. |
| `tests/capture/test_reported_source.py::test_enabled_foreign_or_virtual_geometry_collision_refuses` | `fresh_layout_cases`; separately add an enabled foreign physical connector and an enabled virtual/unknown-origin output at the exact source geometry; each makes uniqueness fail. Mutation filters by requested name/origin before counting, and must be killed by both controls. |
| `tests/capture/test_reported_source.py::test_disabled_geometry_match_does_not_create_ambiguity` | `fresh_layout_cases`; one enabled requested physical output plus a disabled exact-geometry duplicate passes. Mutation counts disabled entries and must fail this test. |
| `tests/capture/test_reported_source.py::test_enabled_disconnected_or_unknown_geometry_collision_refuses` | `fresh_layout_cases`; separately add an enabled disconnected output and an enabled unknown-type output at the exact geometry; both count and make uniqueness fail. Mutations that filter either state before counting must be killed. |
| `tests/display/test_inventory.py::test_single_fresh_inventory_supplies_geometry_origin_and_connector_projection` | `single_inventory_read`; assert one kscreen-doctor -j call, MonitoredOutput fields from that document, preserved signed/missing geometry and unknown/virtual/disabled entries, same-provider KscreenConnectorInventory projection with exact delivered name/type/connected/enabled rules for PhysicalCaptureProvider; no second origin read. |
| `tests/capture/test_reported_source.py::test_negative_coordinate_physical_layout_is_accepted` | `fresh_layout_cases`; unique connected known-type physical at negative x/y passes; removing sign support kills test. |
| `tests/capture/test_reported_source.py::test_stale_unreadable_or_unknown_origin_layout_refuses` | `fresh_layout_cases`; stale/unreadable evidence and disconnected/unknown-type candidate independently refuse. |
| `tests/process/test_ownership.py::test_generation_uses_max_across_terminal_receipts_not_recency` | `endpoint_receipt_history`; mix ready/stopped/failed generations and UUID/timestamp order; next reservation uses all-receipts maximum + 1; a maximum of `2**63-1` refuses without wrapping or spawning. Mutations deriving from current-only, newest timestamp or UUID must fail. |
| `tests/process/test_ownership.py::test_multiple_current_receipts_refuse_without_cleanup` | `endpoint_receipt_history`; two nonterminal receipts at the endpoint maximum return `ownership_mismatch` plus `cleanup_required`; no adoption, replacement, signal or unlink of either candidate. Mutations selecting either receipt must fail. |
| `tests/process/test_ownership.py::test_stale_terminal_receipt_cannot_authorize_cleanup` | `endpoint_receipt_history`; older failed/stopped receipt beside a newer generation, or any nonterminal receipt below the all-receipts maximum, gives zero signals/unlinks and blocks replacement; a sole failed max-generation receipt with no nonterminal and independently proved exact absence is the positive cleanup/intent-completion control. Mutations ignoring terminal generation, stale nonterminal evidence or nonterminal presence must fail. |
| `tests/process/test_ownership.py::test_duplicate_unix_path_rows_refuse` | `owned_endpoint_evidence`; two kernel socket rows for one path, even with one plausible peer, refuse. |
| `tests/process/test_ownership.py::test_unlinked_old_listener_and_replacement_entry_refuse` | `owned_endpoint_evidence`; old process FD cannot prove new pathname/entry; no signal or unlink. |
| `tests/process/test_owned_launcher.py::test_entry_replacement_during_first_bind_never_becomes_ready` | `bind_replacement_barrier`; replace only entry between first and second stat; no bound/ready publication, exact cleanup or retained uncertainty. |
| `tests/backend/test_control.py::test_replacement_between_authorization_and_dispatch_refuses` | `authorization_dispatch_barrier`; mutate receipt generation/entry after awaited proof; final fence/current-proof check prevents mutating I/O; post-I/O replacement separately fails confirm. |
| `tests/process/test_linux_runner.py::test_strict_revalidates_identity_after_pidfd_open` | `strict_signal_observer`; recycle ticks during open; zero pidfd/numeric signals and ownership_mismatch. |
| `tests/process/test_authority.py::test_owner_survives_unlock_and_blocks_second_instance` | `two_registry_barrier`; ordinary unlock leaves owner bytes, next instance refuses before launch. |
| `tests/process/test_authority.py::test_stale_clear_owner_cannot_remove_successor` | `owner_succession`; stale expected owner cannot delete successor; cancellation after successful clear is inert. |
| `tests/process/test_authority.py::test_flock_deadline_and_backoff_map_concurrent_writer` | `authority_fake_clock`; 5.0 s total, 0.05 s retry backoff, no deadline reset, CONCURRENT_WRITER_DETECTED maps to ownership_mismatch and zero launches/signals/unlinks. |
| `tests/process/test_daemon_registry.py::test_shutdown_acquires_once_and_cancellation_unlocks` | `lease_event_log`; shutdown uses locked internal reconcile, cancellation always unlocks without erasing unresolved owner. |
| `tests/process/test_daemon_registry.py::test_every_owned_termination_passes_strict_true` | `strict_callsite_cases`; registry stop/shutdown, EvidenceAuthority.release, public supervisor and namespace probe each pass strict=True, stage-complete `TerminationEvidence` from its receipt or R10's independently verified no-receipt source, and a callback returning freshly verified same-stage evidence. Assert invocation after pidfd open; independently vary pre-bind child ticks/UID/argv/supervisor parent and bound peer/row/FD/entry evidence; any mismatch has zero signals, while the valid pre-bind verification-failure control observes exact child death. |
| `tests/storage/test_reservation_codecs.py::test_reservation_codec_rejection_matrix` | `reservation_documents`; exact valid projection control plus unknown schema/version, missing/extra/duplicate member, wrong leaf type, bool-as-int, non-positive required integer, nonfinite value, and trailing bytes including whitespace all raise safe `RecordError`; filename identity is checked by the receipt store. Removing any rejection must fail. |
| `tests/storage/test_launch_receipts.py::test_receipt_codec_rejection_matrix_by_state` | `receipt_state_documents`; valid control per state plus wrong leaf/bool-as-int/non-positive, unknown schema/version, missing/extra/duplicate, partial state combination, filename/launch UUID mismatch and trailing bytes refuse; every malformed consumed receipt produces no launch/signal/unlink. |
| `tests/storage/test_cleanup_intents.py::test_cleanup_intent_codec_rejection_matrix` | `cleanup_intent_documents`; valid initial/completed controls plus unknown schema/version, missing/extra/duplicate, wrong leaf/bool-as-int, invalid outcome/time, filename/launch UUID mismatch and trailing bytes refuse; mutation accepting any malformed variant fails. |
| `tests/storage/test_supervisor_release_intents.py::test_supervisor_release_intent_codec_rejection_matrix` | `supervisor_release_intent_documents`; exact eight-payload-member/schema/version control plus unknown schema/version, missing/extra/duplicate members, wrong leaf/bool-as-int, invalid process identity, reason or nonfinite time and trailing bytes including whitespace refuse. Store controls reject changed repeat publication; supervisor-publication controls reject wrong runtime/launch/application/daemon correlation without signals or receipt writes; removing any rejection fails. |
| `tests/process/test_authority.py::test_authority_owner_codec_rejection_matrix` | `authority_owner_documents`; valid exact five-member control plus unknown schema, missing/extra/duplicate, wrong leaf/bool-as-int, invalid identity/time and trailing bytes refuse; no owner replacement or teardown follows malformed input. |
| `tests/process/test_credential_probe.py::test_probe_verdict_codec_rejection_matrix` | `probe_verdict_documents`; valid exact schema/version/leg control plus unknown schema/version, missing/extra/duplicate, wrong leaf/bool-as-int, invalid leg/failure combination and trailing bytes refuse; stdout cannot supply fallback data. |
| `tests/process/test_supervisor.py::test_delivered_daemon_record_codec_compatibility_control` | `daemon_record_documents`; delivered valid round-trip and existing rejection/tolerance behavior remain unchanged, including exact schema/fields, duplicate-member/trailing-whitespace behavior and owner_token handling; no new fields, strict-ingestion wrapper or codec migration. |
| `tests/storage/test_repositories.py::test_legacy_store_roundtrip_is_byte_identical_while_new_types_are_strict` | `legacy_and_new_document_cases`; write a delivered `DaemonRecord` through `JsonDaemonStore`, read/rewrite it through that same store and compare exact original encoded bytes, including owner_token. Separately load legacy duplicate-member/trailing-whitespace cases and assert delivered outcomes with zero calls to new decoders; cover every other existing record family's delivered validation/tolerance without routing it through new codecs. In the same named test, valid controls and duplicate/trailing/missing-member cases for all six new families pass/refuse under their own strict codecs. Mutations routing a legacy document through a new strict decoder or weakening new-type rejection fail. |
| `tests/storage/test_launch_receipts.py::test_all_partial_states_and_unknown_versions_refuse` | `receipt_state_documents`; missing fields, unsupported schema/version, daemon without supervisor, detached without daemon_instance_uuid, bound without identities, ready without peer/idle; reserved→stopping is valid and failed/stopped receipt rewrites refuse. |
| `tests/storage/test_launch_receipts.py::test_failed_cleanup_intent_roundtrip_and_one_completion_preserve_receipt` | `failed_cleanup_documents`; exact airplay-linux/daemon-cleanup-intent v1 members, explicit null initial completion/outcome, strict decoding, one completion update only, failed/stopped bytes unchanged. |
| `tests/process/test_owned_launcher.py::test_stopping_from_reserved_and_failed_persists_intent_before_signal` | `stopping_origin_cases`; all five origins reserved/detached/bound/ready/failed retain a defined stopping workflow. Nonterminal origins publish registry receipt stopping before first signal; failed bytes are immutable and their cleanup intent records independently proved absence/completion, never a third signal-intent alternative. A live failed case without permitted intent sends zero signals and retains cleanup_required. |
| `tests/process/test_daemon_registry.py::test_missing_cleanup_intent_is_rederived_without_authorising_teardown` | `reopen_receipt_store`; crash leaves immutable failed receipt and no cleanup intent; verifier disagreement yields zero signals/replacement. Successful absence re-derivation publishes/completes the outcome record without signals; the record alone cannot admit live teardown. Supervisor stopping observation never writes a receipt or substitutes for §6.3's intent. |
| `tests/process/test_supervisor.py::test_failed_launch_release_after_app_death_records_cleanup_intent_only` | `supervisor_failed_receipt`; authority-held failed cleanup records `DaemonCleanupIntent`, completes it once on independently observed absence, preserves failed receipt bytes/schema and never creates an R10 intent for a terminal origin. A still-live failed case without permitted pre-signal intent refuses with zero signals; supervisor lifecycle stopping remains observation. This fixture cannot satisfy the independent healthy-ready control. |
| `tests/process/test_supervisor.py::test_ready_app_death_publishes_release_intent_observes_death_and_recovers_terminal_receipt` | `supervisor_healthy_ready_receipt`, independent of `supervisor_failed_receipt`; ready daemon is live before app death. Observe supervisor app-absence proof, atomic release-intent publication before its first pidfd signal, strict post-open checks and exact daemon death. Reopen registry: `DaemonCleanupIntent` plus receipt `stopped`, retained identical supervisor file, no recovery signals or supervisor receipt writes. |
| `tests/process/test_supervisor.py::test_detached_and_bound_app_death_publish_release_intent_before_signal` | `supervisor_nonterminal_release_cases`; separate detached/pre-bind and bound/full-endpoint controls have live exact children, each publishes the matching supervisor intent before first signal, observes exact death, then terminalises on registry recovery without re-signalling. |
| `tests/process/test_supervisor.py::test_supervisor_release_intent_requires_app_absence_and_matching_own_daemon` | `supervisor_intent_predicate_cases`; reserved/detached/bound/ready and no-receipt origins permit publication only with proved absent/mismatched app and the same daemon in the supervisor's own `DaemonRecord`; recorded app identity still matching/live or unreadable, wrong own daemon, wrong launch/application and stopping/failed/stopped origins refuse new publication. The no-receipt case writes no receipt; recovery retains missing-receipt evidence without fabrication. Stage proof remains independently required for any signal. |
| `tests/process/test_supervisor.py::test_release_intent_unavailable_refuses_without_signals_and_exits` | `supervisor_intent_publication_failures`; fail each atomic-publication step with otherwise valid ready-release evidence. Assert `cleanup_required` with exact reason `intent-unavailable`, typed refusal, zero supervisor pidfd/numeric signals and exit with parent-death protection retained; any subsequently observed parent-death cleanup never turns refusal into success. |
| `tests/process/test_supervisor.py::test_supervisor_observation_alone_is_not_release_intent` | `supervisor_healthy_ready_receipt`; independent file/event observer requires fully durable `release-intent.json` before the first actual signal, even when own `DaemonRecord.lifecycle='stopping'` is valid. Mutation replacing the supervisor intent with only its `DaemonRecord` observation must fail this named test; unchanged intent-backed release observes exact daemon death. |
| `tests/process/test_daemon_registry.py::test_supervisor_intent_recovery_terminalises_without_resignalling` | `supervisor_release_recovery_cases`; proved absence gives stopped; present/mismatched/unreadable absence evidence gives failed plus cleanup_required. Assert cleanup-intent publication/one completion, original supervisor file retention, immutable terminal bytes on repeat recovery and zero signals in every case; crash/reopen between each recovery write remains idempotent. |
| `tests/process/test_owned_launcher.py::test_each_actual_crash_boundary_retains_safe_outcome` | `crash_boundary_cases`; all ten §5.3 boundaries, publication failures/death during ready, no ready or replacement without exact evidence. |
| `tests/process/test_credential_probe.py::test_probe_evidence_and_teardown_stay_in_child_namespace` | `namespace_probe_witness`; service once, positive reachable-bus idle and observed strict exact death, fresh negative unreachable-bus nonzero/no-socket/no-file completion; distinct launch UUIDs/receipts under one probe root, namespace-consistent peer/row and strict retained failure with post-open receipt closure; no host evidence substitution. |
| `tests/process/test_credential_probe.py::test_probe_run_seam_observes_outer_unshare_and_inner_backend_launch` | `recording_probe_processes`; full unchanged two-leg witness through the real production graph records exactly one outer `unshare` naming `credential_probe` and exactly two direct inner backend `-daemonize` launches. Assert positive/negative identities have distinct launch UUIDs and receipt files in the probe root; inner records include the negative invocation's unreachable `DBUS_SESSION_BUS_ADDRESS`, nonzero completion and absent socket/credentials file. Do not infer inner launches from nested argv or count `unshare` as a substitute. Mutation routing only the negative leg outside the observed `ProcessPort` must fail this named full-witness test while the unchanged two-leg control passes. |
| `tests/process/test_credential_probe.py::test_each_backend_probe_leg_has_exactly_one_inner_launch` | `recording_probe_processes`; invoke each production backend-leg helper as a separate single-leg subtest: exactly one direct inner launch and its own UUID/receipt; positive requires reachable bus, idle and strict teardown; negative records unreachable bus, nonzero exit, no socket/file. Neither subtest substitutes for the complete two-leg witness control. |
| `tests/process/test_credential_probe.py::test_verdict_file_requires_dirfd_digest_exact_size_and_zero_exit` | `probe_verdict_files`; valid file/digest/zero-exit is the positive control; wrong root owner/mode/UUID, missing/oversized/short/trailing/mutated file, symlink, wrong-owner/nonregular file, wrong digest, wrong `st_size`, malformed schema/fields or nonzero child exit each refuses, regardless of stdout text. Mutations accepting stdout or skipping any root/file/exit check are killed. |
| `tests/process/test_credential_probe.py::test_probe_bootstrap_precedes_registry_and_does_not_require_keyring` | `bootstrap_event_log`; independently omit each exact bootstrap prerequisite, including unshare resolution, and assert zero launches; complete installed composition runs probe once before registry construction and only then derives keyring / checks _compatible. |
| `tests/process/test_credential_probe.py::test_probe_socket_idle_confirmation_and_termination_bounds` | `probe_fake_clock`; one 3.0 s socket+idle deadline, separate 2.0 s idle confirmation and 3.0 s termination grace; expiry retains typed failure/cleanup evidence and never produces proven verdict. |
| `tests/transport/test_doubletake.py::test_source_and_challenge_share_one_post_connect_deadline` | `source_challenge_fake_clock`; 5.0 s total and 0.1 s poll interval, alternating source/challenge observations never reset the deadline, exact-target verification precedes ready. |
| `tests/transport/test_doubletake.py::test_unconfirmed_start_disconnect_takes_cleanup_timeout_precedence` | `start_refusal_cleanup_cases`; challenge and mismatched/absent source crossed with confirmed/unconfirmed disconnect; assert §7 result and retention, no ready/pairing, other targets unchanged. |
| `tests/backend/test_carried_source_patch.py::test_complete_token_requires_complete_producer` | `portal_source_property_cases`; type-only/size-only/missing-position producer reports unusable; revised complete chain alone advertises third token; two-token build remains gated. |
| `tests/acceptance/test_owned_daemon_epoch.py::test_public_status_without_legacy_metadata_is_accepted` | `public_production_graph`; absent all five legacy ownership fields, EvidenceAuthority proves and public status succeeds. |

### 10.3 No-spawn proof and activation matrix

`tests/acceptance/test_no_production_spawn.py` instruments the final injected process port's **run and spawn** methods through real `build_runtime(..., overrides={'process': recorder})`; the probe additionally injects its own recording `ProcessPort` through `credential_probe.run`. Replacing the graph with `SessionRig.deps` is not evidence.
`test_process_observations_classify_direct_and_unshare_daemon_launches` records backend `-daemonize` only at the process port that directly invokes it and records every outer `unshare` invocation separately. The complete probe run seam reports exactly one outer namespace invocation and exactly two inner direct backend launches, positive and negative, each with its own UUID/receipt and with the negative environment and nonzero/no-socket/no-file completion recorded. Single-leg subtests require exactly one inner launch each. Never infer a launch from nested argv or count `unshare` as a substitute. Routing the negative leg outside the observed inner `ProcessPort` must fail the named §10.2 full-witness test while the unchanged two-leg control passes. Delivered help/version/capability/inventory probes are recorded as non-launch observations, never silently omitted.
`test_phase_guard_blocks_eligible_production_launch` uses `fully_eligible_launch_fakes`, not today's incompatible help profile: in phases 1–3 assert zero launches **and zero `unshare` invocations**; removing only the activation guard must make the same setup attempt launch.
`test_each_missing_activation_element_blocks_launch` parameterizes all **seven** enumerated design §14.2 elements at their upstream installation/wiring boundary: owned-daemon capability, receipt production, endpoint verifier, strict teardown, shared authority, credential-proof producer, wired source verifier. The design says “six” but lists seven; none may be omitted. Missing credential proof here means its production/wiring is absent, not requiring a completed verdict before the probe that produces it. Missing/unresolvable `unshare` is additionally covered as a bootstrap prerequisite.
For every omitted upstream element assert a typed refusal naming it and zero launches, **including the probe**, at the outer and inner process recorders. The complete installed composition is the positive control: the isolated bootstrap (§4.1) executes the credential probe **exactly once before registry construction**, recording one outer `unshare` and both inner backend legs, then derives capabilities and leaves `_compatible()`'s keyring requirement downstream. A failed/not-run verdict after an admitted probe is a downstream witness failure; all attempted outer/inner launches remain recorded and no registry stream is admitted. Help/inventory observations never satisfy a positive launch assertion.
Amendment acceptance also removes owned capability, reported-source verifier, clean trust and valid fence independently before connect; each refuses before request I/O with no new production spawn.

| Exact test in `tests/acceptance/test_owned_daemon_epoch.py` | Required public/fixture composition and assertions |
| --- | --- |
| `test_public_shape_complete_graph_uses_evidence_authority` | Public old flags false, pairing flags false, status lacks owner/instance/daemon/socket/protocol; selected authority explicitly recorded as EvidenceAuthority; launch/prove/idle/connect/source/ready succeed with valid fence. |
| `test_public_shape_target_controls_and_whole_shutdown_are_distinct` | A/B streams; disconnect A does not terminate daemon or B; mute/unmute exact targets; whole shutdown alone releases shared daemon by strict teardown. |
| `test_changed_current_receipt_blocks_every_mutation_before_io` | Parameterize generation and entry-inode change and every mutation; stale_event, no mutation I/O or replacement launch. |
| `test_wrong_fence_refuses_before_io` | Receiver/endpoint generation/fingerprint mismatch; stale_target and zero mutation I/O. |
| `test_connect_challenge_disconnects_only_exact_target` | Challenge in acknowledgement and status separately; apply §7 precedence with confirmed/unconfirmed disconnect, no pairing/prompt/ready, retained stream/obligation where required, B unchanged. |
| `test_each_missing_composition_prerequisite_is_named` | Owned control, credential-proof production/wiring, verifier and authority removed individually upstream; zero launches including probe; physical_source false when verifier missing; completed verdict is consumed downstream. |
| `test_legacy_fixture_selects_handshake_authority` | Separate application-contract fixture test; delivered equalities and refusals preserved, OwnershipProof kind handshake; cannot satisfy public acceptance. |
| `test_crash_restart_requires_reconciliation_and_new_generation` | Production graph with fake backend/process evidence, crash, retained receipt, reconciliation, strict cleanup and monotonically advanced generation before restart. |

### 10.4 Offline boundaries and suite hygiene

Phase 5 uses a fake backend speaking delivered **public-shape** control, fake fresh KScreen inventory provider and fake Secret Service witness through production composition for launch, verify, target Stop, whole shutdown, crash restart and refusal matrix.
Tripwires intercept live `doubletake`/ctl execution, live session-bus clients and live socket leases before lookup/connect, not after dependency failure; missing dependencies must not turn an attempted live path into a pass or skip.
Unmarked default tests actively exercise each prohibited boundary. Marked optional acceptance remains opt-in; each capability skip names exactly `owned daemon control`, `credential service proof` or `physical source verification` as applicable.
Use `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8`, short `--basetemp /tmp/x`, and real-process tests against the repository installed into `~/.venvs/airplay-linux`. Baseline 3787 passed/12 skipped is historical evidence, not this task's result.
Record the known `tests/process/test_supervisor.py::test_killing_supervisor_cannot_leave_a_daemon_that_ignores_term` flake explicitly; no blanket retries, weakened assertions or converted skips to mask it.

## 11. Phase completion and operator-visible residuals

| Phase | Completion boundary |
| --- | --- |
| 1: T1–T2 | Raw capability false, strict codecs/storage, activation guard/no-spawn plus guard-removal control; no production launcher exposure. |
| 2: T3–T5 | Endpoint proof, strict pidfd and cross-process authority installed; M3–M6 and crash/uncertainty refusals; same no-spawn invariant. |
| 3: T6 | Isolated idle launcher exercised offline at all actual crash boundaries; M7/M8, retained receipts and exact cleanup; production guard closed. |
| 4: T7 | Single activation commit contains all §9 consumers, Amendment 1 handling, credential proof, complete-source producer/decoder/verifier/rebuild record and operator disclosure; seven-element omission proof. |
| 5: T8 | Production-composition fake acceptance, M10/default tripwires, complete refusal matrix and suite hygiene evidence; no live-device claim. |

| Residual (design §16) | Required operator-visible location |
| --- | --- |
| A hostile same-UID process is out of scope. | README ownership guarantee and startup capability detail, in those words. |
| Evidence cannot distinguish a same-user impersonator reproducing the entry and command line; private directory/peer checks only narrow the attack. | README explanation beside the cooperative ownership guarantee; startup authority detail links/names that limitation. |
| Complete reported geometry proves the reported identity matches the requested unique physical connector, not that the compositor drew that monitor. | README Mirror/source-verification guarantee and startup source-verifier detail. |
| Amendment 1 permits starting to a receiver the app cannot re-pair; later credential demand makes subsequent starts typed refusals. | README connect/pairing limitation and startup withheld-pairing detail. |
| Remembered trust still does not survive restart on the adapter. | Existing `docs/operations.md` residual remains applicable; README/startup remembered-trust detail continues to say so. |

Startup detail names `EvidenceAuthority`/`HandshakeAuthority`, each credential leg verdict, source-verifier wiring/completeness/inventory and each exact missing structural prerequisite. It must not claim that successful offline acceptance proves live Mirror, pairing or compositor attestation.

## 12. Resolved elaborations

Each ruling ID records a design-authority ruling, the design sentence it implements, its decision and a one-line argument that the elaboration changes no design decision and relaxes no gate.

| ID | Design sentence implemented | Resolved decision and preservation argument |
| --- | --- | --- |
| Q1 | §5.1: “application instance that owns the launch”; §5.3: “The supervisor keeps writing its own `DaemonRecord` lifecycle (the delivered observation channel) and never writes a receipt”. | Keep the complete delivered `DaemonRecord` schema including `owner_token` and fixture handshake. Receipt `instance_uuid` is runtime's application `InstanceId`; add `daemon_instance_uuid` copied from the observation channel. Never record a token in a receipt. Public config keeps delivered non-token members plus `launch_uuid`/`endpoint_generation`; token payload/use is fixture-only and the public record field is marked unused (§§2–5, §10). **Argument:** The correlation implements the application's ownership identity while preserving the observation/fixture channel; no design decision changes and no gate relaxes. |
| R1 | §8 step 3: “re-validate the identity *after* opening (start ticks, UID, socket peer/argv where the receipt supplies it)”. | Extend `ProcessPort.terminate` with `strict=True`, stage-complete `TerminationEvidence` and `revalidate: Callable[[], Result[TerminationEvidence]]`; after pidfd open, repeat the same pre-bind child/supervisor/argv evidence or bound §6.1 chain and refuse on any non-`Ok`, stage change or identity mismatch before the first signal. EvidenceAuthority, registry stop/shutdown, public supervisor release and namespace probe supply this callback; the port carries no receipt. M7 separates exact-child cleanup, unreadable identity and bound mismatch. **Argument:** This makes the required post-open check executable without weakening the gate or changing design authority. |
| R2 | §15 F-008: “uniqueness across **all** outputs, physical-origin rule retained”; §7.2: “enabled, connected, known physical connector type”. | Build geometry and origin from one fresh inventory read. Count every enabled output with matching complete geometry regardless of connected state, virtual/unknown origin or requested name; exclude disabled entries from uniqueness. Require exactly one match, then require requested connector, connected and known physical type. Missing enabled geometry refuses. **Argument:** The uniqueness population preserves collisions across enabled outputs while retaining the physical-origin rule; no gate is relaxed. |
| R3 | §4.6: “Every pairing operation, remembered trust, provisional-commit compensation, cancellation and revocation keeps its delivered requirements and stays refused”; Amendment 1 §3 preserves those requirements. | Keep the exact independent lists: begin = `target_bound_pairing and cancel_pairing`; submit/prepare = `target_bound_pairing and keyring and provisional_commit`; commit = `keyring and provisional_commit`; mark_invalid/revoke = `revoke_pairing`; cancel = `cancel_pairing`. No shared superset and no unrelated former-connect flags; new Amendment 1 §6 Clarification 1.1 changes only connect. The per-operation truth table proves each requirement and unrelated-flag independence. **Argument:** The amendment remains scoped to connect, with every pairing gate and public refusal preserved. |
| R4 | §10.3: “An incomplete composition therefore performs **zero** launches, including the probe.” | The parent creates a unique private mode-0700 probe root; only the namespace child writes probe receipts there, never the application registry/authority tree. After strict teardown the child writes its verdict and digest, then exits; parent validates the opened-root files only after zero exit, archives on success and removes the root only after archive publication. Failure or uncertainty retains every available probe file/receipt. **Argument:** Isolated ownership and retention add no launch path and preserve the incomplete-composition zero-launch boundary. |
| R5 | §5.1: “application instance that owns the launch”; §5.3: “The supervisor keeps writing its own `DaemonRecord` lifecycle (the delivered observation channel) and never writes a receipt”. | Join exactly one observation by application identity, runtime directory and eligible lifecycle; require receipt/observation daemon UUID and any already-published supervisor/daemon identities to agree. R12 scopes the original launch's pipe check and supplies the exclusively durable restart rule. Zero/multiple candidates, including a stale competitor at the observation-to-receipt crash gap, are `CleanupRequired`; never select by recency/UUID or promote from the record alone. Keep delivered `DaemonRecord` schema unchanged. **Argument:** The join resolves restart ambiguity without making the observation channel authoritative. |
| R6 | §10.3: “Classify resolved backend plus `-daemonize` as launch direct or inside unshare; record help/version/inventory as non-launch.” | Add the explicit `run(config, *, process, namespace)` seam in `credential_probe.py`. Observe outer `unshare` at the parent process port and both direct backend launches at the injected child `ProcessPort`, with per-leg accounting under R11; tests may not infer inner launches by searching nested argv or count the outer wrapper as a backend launch. **Argument:** Direct-boundary observation proves the same production no-spawn contract; it changes neither launch eligibility nor phase activation. |
| R7 | §3.6: “Child stdout/stderr are diagnostics only and never parsed as verdict data.” | The child publishes a strict file-backed `ProbeVerdict` and sibling SHA-256 digest in its private root. Parent opens relative to that root dirfd with `O_NOFOLLOW`, validates ownership, regular-file type, 4096-byte verdict cap, exact sizes/digest/schema and zero child exit; stdout/stderr cannot supply or repair a verdict. Success archives both verified receipts and the verdict; failure retains files. **Argument:** The evidence channel becomes verifiable while the credential-proof gate stays closed on any malformed or unverified outcome. |
| R8 | §5.3: “`failed` and `stopped` receipts are immutable terminal evidence and are **never deleted by teardown**”; §8: “A durable `stopping` intent is written **before** the first signal.” | Derive endpoint generation as the maximum across all durable receipts, including terminal, plus one for reservation. A live authorization requires exactly one nonterminal receipt at that maximum; terminal cleanup requires the terminal candidate at the maximum and no nonterminal receipt. Older terminal evidence is never cleanup authority; any stale lower-generation nonterminal blocks authorization and replacement. Failed cleanup uses the separate once-completed `DaemonCleanupIntent`, never a receipt rewrite; integer exhaustion refuses. **Argument:** Monotonic history and immutable terminal evidence strengthen provenance without adding recovery authority. |
| R9 | §3.1: “Use explicit codec projections, never `asdict` to persist or diagnose these records.” | Give each new document its own exact valid control and malformed rejection matrix: reservation projection; application/probe receipts in every state; cleanup intent; supervisor release intent; authority owner; file verdict. Reject schema/version/key/type/state/identity errors, bool-as-int, nonfinite or non-positive required values, duplicate members and any trailing bytes; malformed new input produces no lifecycle side effect. R13 preserves all delivered records' codecs, validation and tolerance and supplies a separate byte-identical legacy-store control. **Argument:** Strict new codecs do not change legacy decoding or re-validate legacy documents. |
| R10 | §5.3: “The registry is the only writer of receipts”; §8: “A durable `stopping` intent is written **before** the first signal”; §4.4 retains supervisor `prove`/`release`. | Add supervisor-only `SupervisorReleaseIntent` (`airplay-linux/supervisor-release-intent`, version 1) at `<runtime_directory>/release-intent.json`, atomically before its first signal and only for proved application absence/mismatch, a `reserved\|detached\|bound\|ready` receipt or no receipt, and its own matching daemon observation. The strict pre-signal intent alternatives are exactly registry-written receipt `stopping` or this supervisor intent; observations and cleanup-outcome records cannot substitute, and neither alternative authorises any other action. Registry recovery writes `DaemonCleanupIntent`, terminalises the receipt as `stopped` on proved absence or `failed` otherwise, retains the file and never re-signals. Publication refusal yields `cleanup_required` / `intent-unavailable`, no signals and supervisor exit with parent-death protection; it is never successful cleanup. **Argument:** A separate permitted writer completes application-death release while preserving registry-only receipts, independent proof and observed cleanup. |
| R11 | §4.2: “backend positive leg” must reach `status == idle`; “backend negative leg” must “exit nonzero, create no control socket and create no credentials file”. | The complete witness performs two inner backend launches in one outer namespace invocation: each leg has a distinct `launch_uuid` and receipt in the probe root; the inner recorder captures the negative leg's unreachable-bus environment and completion evidence. The full-witness test asserts one outer `unshare` and two direct `-daemonize` launches; single-leg subtests assert one inner launch. Routing only the negative leg outside the observed `ProcessPort` must fail the full-witness test while the unchanged two-leg control passes. **Argument:** Per-leg accounting observes both required executions without changing eligibility or credential proof. |
| R12 | §5.3: “after a crash the registry reconstructs the receipt state from that record plus the verifier”. | The config-pipe correlation boundary is stated in §§3.4/5.1: only the owning launch uses it in process; every restart uses exclusively the unique durable app/runtime/lifecycle join and neither requires nor accepts pipe data. Require a pipe-free unique-gap reconstruction control and a stale competing-record refusal. **Argument:** Durable reconstruction preserves zero/multiple-match refusal and independent verification without changing `DaemonRecord`. |
| R13 | §5.3: “the delivered observation channel”; §5.2: “Codec rules: reject an **unknown version**, a **missing field**, and any **partial record**”. | Restrict rejection matrices to the six new document families: reservation, receipt, cleanup intent, supervisor release intent, authority owner and credential verdict. `DaemonRecord` and every other existing record retain delivered codecs, validation and tolerance; new codecs never re-validate a legacy document. A named test requires byte-identical legacy round-trip through the delivered store alongside strict new-type controls. **Argument:** The new strict boundary preserves delivered compatibility and creates no new legacy rejection rule. |
| Q6 | §4.6: “The bounded post-connect wait of §7 is the same wait that detects a challenge”; §9: “`flock`, non-blocking, bounded retry”; §8: “teardown never reports success it did not observe”. | Post-connect: 5.0 s total / 0.1 s polls, one deadline for source and challenge. Flock: 5.0 s / 0.05 s backoff, then CONCURRENT_WRITER_DETECTED → ownership_mismatch. Probe: 3.0 s socket+idle, 2.0 s idle confirmation, 3.0 s termination grace. The single §7 rule fixes start/cleanup failure precedence and retention, referenced by §5 (§§2, 4–7, 10). **Argument:** Fixed bounds and precedence implement bounded waits and observed cleanup without weakening typed refusals; no design decision changes and no gate relaxes. |

