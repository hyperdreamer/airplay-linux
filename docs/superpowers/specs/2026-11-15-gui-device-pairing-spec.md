# GUI device pairing — approved-design specification

**Status:** technical specification for the approved cleanup-recovery design (epoch 7)

**Authority:** `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md`, finalized at
`addfdc541ad29c6111810871f3306b901ae911ae`; SHA-256
`ed15f8c24296a1bc9269f8dc22b3b9f7116edad5bfac5bde100a24df34cedf94`.

**Implementation target:** the integration worktree for run `pm-run-20261115-031248-7c91beef`.

**Delivery:** this specification and the documentation updates defined in §11. Implementation evidence is required before delivery.

## 1. Decision register

| ID | Approved decision | Contract location |
| --- | --- | --- |
| D-01 | Scope is an operator-started Mirror. A matching per-stream `pin_required` row is the only source of a credential request. | §§2–4 |
| D-02 | The pending Start owns the wait. The request creates no second Start, receiver lease, reservation, or stream. | §§3, 6 |
| D-03 | Submit the credential by the target-first `connect` form selected from the exact recognized help profile. Never use targetless `pin`. | §§4–5 |
| D-04 | Success means only that status reports this target as `streaming`. A complete command reply is not success. | §7 |
| D-05 | The application stores no credential or trust result. The receiver may best-effort persist derived pairing material. | §9 |
| D-06 | A missing complete command reply has an unknown outcome and requires a durable, secret-free recovery marker written before dispatch. | §8 |
| D-07 | A retained marker fences every Start to the same canonical target until the original daemon is proven terminated and a distinct replacement endpoint is verified. | §8 |
| D-08 | The relay capability is independent of transaction capabilities, composed keyring proof, and owned-daemon authority. | §5 |
| D-09 | The existing `ChallengeView` and pairing dialog may present this request, but this path never enters transactional pairing state. | §6 |
| D-10 | Address continuity, duplicate submission, frame delivery, diagnosis, and credential retention are not promised. | §§2, 7, 9 |
| D-11 | Relay dispatch uses the existing `CredentialDispatchFailure` classification and the adapter’s `dispatch_fence` at the child-creation boundary. | §§3.4, 5, 12.0–12.3 |
| D-12 | Relay command, event, completion, projection and status-read variants extend closed unions. Existing transactional pairing variants remain unchanged. | §§3.2–3.5, 6, 12.0 |
| D-13 | Recovery markers use a dedicated atomic store and are restored before Start admission. Unreadable or ambiguous marker evidence keeps Start admission blocked. | §8, §12.0 |

Design §12 is a mechanism specification and adds no user-visible promise beyond these decisions.

## 2. Scope and observable behavior

The feature answers a PIN or password prompt raised during a Mirror that the operator started. It does not start a stream to discover a prompt. Reconnects remain owned by the original Start operation.

A request exists only when a typed `StatusReply.streams` entry has the exact target of the active Start and has `state == "pin_required"` with a typed `credential_kind` of `"pin"` or `"password"`. Aggregate status fields never create a request. A prompt belonging to another target is ignored.

The receiver routes the credential by target address. The application therefore promises target-addressed delivery under address continuity. Neither component can establish that the device at that address is the same physical receiver that was present at Start time.

The receiver acknowledges a credential before authentication completes. It can remove a refused request, and it may best-effort save derived pairing material, but the submitted PIN or password itself is not saved by the application or receiver. The application makes no exactly-once trust claim, duplicate-submission guarantee, frame-delivery claim, or diagnosis from a non-streaming result.

## 3. Domain and transport contracts

### 3.1 Request record and identity

The transport publishes one transient `CredentialRequest` for the matching stream. Its safe fields are:

```text
CredentialRequest {
    receiver_id: ReceiverId
    target: ReceiverTarget
    credential_kind: CredentialKind
    start_operation_id: OperationId
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    generation: int
    observed_at: float
    start_options: RelayStartOptions
}

RelayStartOptions {
    target_volume_proven: bool
    audio: AudioPolicy | None
    initial_volume: VolumeLevel | None
}
```

The record contains no credential, receiver-side request identifier, provisional trust token,
release evidence, settlement state, or second lease. Its `start_operation_id` is the owning Start
identity and is captured at request creation. The existing transport event identity envelope also
carries operation context, endpoint fingerprint and generation, daemon instance UUID, and the
owning stream handle when one exists. Its local prompt identity is `(receiver_id,
start_operation_id, first credential_kind observed)`. The request’s `target.endpoint_fingerprint`
and the identity envelope fence it to the same endpoint. The status protocol exposes no request ID,
so successive requests of the same kind for the same target cannot be distinguished reliably.

`ReceiverTarget.canonical_host` is the target string compared against `BackendStream.target`. Do not match by display name, aggregate status, receiver selection, or a normalized alternative address. The start’s `audio` and `initial_volume` values are snapshotted when Start is admitted and remain the values used to build the credential command.

### 3.2 Request lifecycle event

Add one `credential_request` member to `TransportEventKind`. Its `CredentialRequestPayload`
contains `CredentialRequest` plus lifecycle `"live" | "ended"`; `ended` uses the fixed reason
`row_ended`. Emit `live` on the first matching prompt observation. Emit `ended` only when a later,
pre-submission status no longer contains the matching row. These are transport-to-session events,
not local UI callbacks.

Add `CredentialRequestPayload` to the closed `TransportPayload` union and add `SubmitCredential`,
`DismissCredentialPrompt`, `StopCredentialRecovery`, `DismissCredentialRecovery`, and
`RestartReceiverToRelease` to the closed `Command` union. `SubmitCredential` and
`DismissCredentialPrompt` route to `Route.mirror`; the three recovery-marker commands route to
`Route.recovery`. Keep `ResourceEvent` unchanged. The request event carries the full existing
`TransportEvent` identity envelope and requires `CredentialRequest.start_operation_id` to equal
`event.context.operation_id`.

```text
CredentialEffectOwner {
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    relay_generation: int
    marker_generation: int
}

CredentialSubmissionCompletion {
    submission_id: OperationId
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    relay_generation: int
    marker_generation: int
    outcome: Submitted | NoReply | Err
}

CredentialStatusCompletion {
    read_id: OperationId
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    relay_generation: int
    marker_generation: int
    result: Result[StatusReply]
}

CredentialSubmissionSettlement {
    submission_id: OperationId
    read_id: OperationId | None
    owner: CredentialEffectOwner
    marker_generation: int
    stop_operation_id: OperationId | None
    report_suppressed: bool
    phase: "awaiting_reply" | "observing" | "cleanup"
}

PendingStartCleanupOwner {
    receiver_id: ReceiverId
    target: ReceiverTarget
    start_operation_id: OperationId
    endpoint_fingerprint: str
    endpoint_generation: int
    session_generation: int
    trust_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    stop_operation_id: OperationId
    credential_submission_id: OperationId | None
    marker_generation: int | None
}
```

For `PendingStartCleanupOwner`, `credential_submission_id` and `marker_generation` are both `None`
exactly when no credential command has been dispatched. They are both present for a dispatched
submission and are copied from the retained settlement at Stop admission. The submission ID equals
the credential effect context’s `operation_id`, and the marker generation is the settlement’s current
expected generation at capture. The transport associates that submission ID with the pending
`_Stream` through the submission context’s `transport_lease_id`; it never accepts a different
submission for the same handle. A completed settlement may still use the handoff while its Start
acquisition is outstanding, subject to the same owner and target fences. The optional fields are
transport-admission evidence and do not replace settlement admission or marker-generation checks.


`SessionManager._route` sends `SubmitCredential` and `DismissCredentialPrompt` to `Route.mirror`;
all three recovery commands go to `Route.recovery`. After durable marker creation and before dispatch,
the actor records an actor-local `CredentialSubmissionSettlement` keyed by the submission ID and
full owner identity. It survives prompt dismissal and is not part of `SessionSnapshot`. The
settlement accepts one `CredentialSubmissionCompletion` only in `phase == "awaiting_reply"` and
only for its `submission_id`. A `Submitted` result enters `observing`, records the next `read_id`
before launching that status effect, and accepts only that one status completion. Each subsequent
poll replaces `read_id` before launch. `NoReply`, pre-spawn `Err`, terminal status, or a read failure
moves the settlement to `cleanup`. A duplicate or late submission/read completion cannot repeat a
marker transition or cleanup action. The mirror handler launches the submission as
`SessionContext.launch(Route.mirror, "credential_submit", submit_context, ...)` and returns
`Ok(CredentialSubmissionCompletion)` through that effect’s `EffectEvent.result`. It launches each
owned status read through a separately correlated `Route.mirror` effect and always returns
`Ok(CredentialStatusCompletion)`, placing the transport’s `Result[StatusReply]` inside
`CredentialStatusCompletion.result`. Thus an expected status-read error remains owner-correlated
and reaches the session failure path. `DoubleTakeTransport` contains request and decoder exceptions
as a fixed sanitized inner `Err`, so an expected read failure never escapes as an uncorrelated outer
`EffectEvent.result = Err` or raw exception. The `EffectEvent.operation_id` equals `submission_id` or
`read_id`, respectively, and its session generation equals the captured completion. Both completion
records repeat the full `CredentialEffectOwner` identity: challenge, receiver, exact
`ReceiverTarget`, owning Start ID, endpoint fingerprint and generation, session and trust
generations, owner lease, daemon instance, relay generation, and marker generation. No credential
completion is synthesized from a direct callback outside the per-receiver queue.

The mirror handler adds an explicit `credential_request` branch before transactional
`pairing_required`; it never routes a credential request or completion through the transactional
pairing handler. `DismissCredentialPrompt` is local-only and sends no receiver command. The mirror actor stores its
challenge ID as `dismissed_credential_prompt_id`, suppresses that prompt identity until the matching
row ends or the Start is replaced, resumes the owning Start’s suspended readiness budget, and
leaves the durable recovery marker unchanged. This suppression field is actor-only and never enters
`SessionSnapshot`. While suppressed, status polling continues without reopening the prompt. A
pre-submission matching-row end clears the suppression and closes the prompt with no PIN result.

Pending-Start admission is an explicit exception to the ordinary stream-ready gate. The transport
creates its exact `StreamHandle` after daemon ownership validation and before awaiting connect/status,
so `credential_request` may arrive while `SessionSnapshot.stream` is still `None`. Add
`accept_pending_credential_request` in `session/fencing.py`. It verifies that duplicated event and
payload fields agree, then requires the active Start’s context to match receiver, target, canonical
host, operation ID, endpoint fingerprint and generation, session generation, trust generation, owner
lease, daemon instance, credential kind, and relay generation. The Start must still have
`intent_stage == starting`, `transport == Connecting`, and the same operation ID and lease. The
event’s stream must name that same Start ID, lease, endpoint and daemon. This helper does not assign
`state.stream`, change `operation_id`, or relax `accept_transport` for any other event. If the exact
stream is already installed, use the ordinary stream-fenced admission path. A stale or replaced
Start is refused before the Mirror handler runs.

While that prompt is active and the Start remains pending, `_admit` accepts `SubmitCredential` and
`DismissCredentialPrompt` through a dedicated prompt-command gate. It requires the command’s
receiver and challenge to match the actor’s current `CredentialPromptState`, the prompt owner to
match the still-active Start, and the target not to be release-pending. This path does not require
`SessionSnapshot.stream` and does not change Start identity. The `PairingDialog` submit handler validates the raw text under §4.3 before constructing
`SensitiveText` or issuing `SubmitCredential`. A valid `SubmitCredential` durably creates the marker
and registers `CredentialSubmissionSettlement` before launching its effect. Prompt
dismissal is local-only, sets `report_suppressed` if submission already started, and resumes the
Start’s suspended readiness budget. It does not discard the settlement owner.

When Stop reaches a live credential request whose Start acquisition is still pending and
`state.stream is None`, it captures a `PendingStartCleanupOwner` before
`ctx.supersede("stop")`. If no credential command was dispatched, its submission and marker fields
are `None` and the handoff sends no credential. If the submission was dispatched, Stop copies the
exact `submission_id` and current marker generation from the retained
`CredentialSubmissionSettlement`. In either case it passes that owner to
`TransportPort.stop_pending_start` before ordered cleanup begins. The transport verifies the owner
against its pending `_Stream`, sets a per-lease readiness-stop signal without acquiring `stream.lock`,
and races that signal against the outstanding status read. It cancels only that readiness read,
releases the lock, transfers the existing `StreamHandle` exactly once to the Stop cleanup runtime,
and makes the original `transport_start` acquisition finish without publishing a normal Start
handle. A dispatched credential command and its owner-correlated status read remain live under their
original deadlines. `CleanupRuntime` waits for the settlement’s required marker/status transition,
the acquisition to settle, and the transferred handle before invoking target-specific
`stop_stream`. The handle never enters `SessionSnapshot.stream` and cannot report Start success.
Stop remains `cleanup_pending` and same-target Start stays blocked until the disconnect is
acknowledged or cleanup is explicitly retried. A pre-submit handoff wakes an unanswered
`pin_required` wait without submitting a credential or waiting for its readiness budget to expire.
When `SessionSnapshot.stream` is already installed, Stop uses the existing ordered cleanup path with
that exact handle instead of requesting a pending-start handoff.

Credential `EffectEvent` admission is owned by the actor-local `CredentialSubmissionSettlement`
registered after durable marker creation and before dispatch. At launch, `SessionContext.launch`
captures the immutable `CredentialEffectOwner` in internal `_Effect` metadata. `_admit` validates
the effect ID, marker identity and generation, and every completion field against that settlement.
For `credential_submit` and `credential_status` only, `_admit` consults the exact settlement before
its generic active-session generation gate. Without a matching settlement, ordinary operation,
session, endpoint, and trust checks apply. When the owning Start is still active, `_effect_current`
also requires its Start ID and current endpoint, session and trust generations to match. A dismissed
prompt does not invalidate its settlement. If dismissal or replacement suppresses reporting, the completion may settle the marker
and bounded status observation but cannot publish to a prompt or mutate a newer Start. When the
stream is absent, lease and daemon identity come from the captured owner; once installed, the stream
must match them. The completion’s trust generation must equal the captured owner.

If ordinary Stop supersedes the Start while submission is in flight, Stop records its operation ID
in the settlement and sets `report_suppressed = true` before calling `ctx.supersede("stop")`.
`_admit` takes the settlement path before its generic stale-session rejection only while that exact
Stop operation remains the current cleanup owner. `_effect_current` admits the pending submission
and resulting status completion only when every old-generation field matches the retained settlement.
This path correlates the fresh submission/read operation ID with the completion and compares old
session/trust values with the immutable owner rather than the superseding Stop snapshot. The mirror
effect handler hands the typed completion to ordered cleanup and does not mutate the retired prompt
or publish a credential report. A superseded settlement with no matching Stop owner is inert and
leaves its durable marker fence in place.

A settled `Submitted` result still receives its bounded post-reply status observation, then Stop
performs target-specific cleanup. Retire the marker only after both observation and cleanup settle.
A `NoReply` result first persists `release_pending`, advances the settlement’s expected marker
generation, publishes the warning, and then performs target-specific cleanup. Keep the marker and
target fence after Stop completes. A pre-spawn `Err` retires its marker only after any concurrent Stop cleanup settles. If no Stop is
active, the existing no-spawn retirement path may proceed immediately after dispatch refusal. If a
status completion carries `Err`, retain the marker and use the existing sanitized transport failure
path. During Stop settlement, that read failure does not cancel ordered target cleanup. The marker
remains fenced until a later successful exact-target observation and cleanup satisfy the retirement
contract. Stale or duplicate completions are inert after settlement retirement or marker-generation
change.

The `credential_request` event uses the existing transport-event route through `Route.reconnect`,
whose Mirror lifecycle delegation handles it after pending-Start admission. Submit and status
completions use `Route.mirror` and are handed to Stop settlement only for its exact owner. Recovery
commands remain on `Route.recovery`.

`StopCredentialRecovery` performs another status read and, only for exactly one matching target
row, sends a target-specific disconnect to the marker’s original daemon and socket. It works after
the stream handle is retired and never clears the marker. `DismissCredentialRecovery` performs the
marker store’s atomic `dismiss` transition. `RestartReceiverToRelease` starts §8.3 only for the
matching `release_pending` marker. Every recovery command includes marker generation. A stale
generation refuses without effects.

### 3.3 Status contract

All status reads use the typed `StatusReply` and its tuple of `BackendStream` entries. `StatusReply` validates the known state vocabulary, exact boolean fields, credential-kind vocabulary, and the coherence rule that `needs_credential` is true exactly when a typed kind is present. Unknown states, malformed kinds, or incoherent fields are rejected as invalid status input. Raw backend errors are not part of `StatusReply`.

The `repr` of `StatusReply` exposes only its public status fields. `owner_token`, daemon instance identity, socket path, protocol version, and process identity remain excluded. Status and source observations are evidence for reporting only. They do not grant command authority or establish request identity.

For request detection and connect progress, inspect only the stream row with `target == ReceiverTarget.canonical_host`. Require exactly one matching row in `pin_required` with a supported kind before publishing a request. Aggregate `state`, `needs_credential`, and `credential_kind` never create a request and never assign one target’s prompt to another. A successful `connect` acknowledgement’s `state` is also daemon-wide aggregate data. In particular, `pin_required` in that acknowledgement is not a refusal and is not evidence that the newly started target needs a credential. Treat it as accepted progress and enter the same target-row status wait used for `connecting`; publish a request only if that wait finds exactly one matching `pin_required` row with a supported kind. A malformed or unsuccessful acknowledgement retains the existing failure behavior. If rows are ambiguous, publish no request and fail closed.

### 3.4 Session command and submission result

Keep the existing transactional command shape and behavior unchanged:

```text
SubmitPairing {
    receiver_id: ReceiverId
    challenge_id: ChallengeId
    value: SensitiveText
}
```

Add a distinct actor command with this exact caller-owned input:

```text
SubmitCredential {
    receiver_id: ReceiverId
    challenge_id: ChallengeId
    value: SensitiveText
}
```

Its handler resolves the current trust-neutral prompt and owning Start, then allocates a fresh
submit `OperationId` and captures the full freshness tuple before launching the effect. It never
calls `PairingPort`, `PairingFlow`, or the transactional coordinator. A stale challenge ID,
replaced Start, mismatched receiver, release-pending target, or obsolete session generation is
rejected before effect launch. The captured Start ID, target, endpoint fingerprint and generation, session and trust generations,
transport lease, daemon instance, challenge ID, relay generation, and marker generation flow through
the transport call and completion.

The relay adapter boundary is a dedicated `CredentialRelayControl.submit_credential` method
implemented by `PublishedDoubletakeControl`. Its return type is
`CredentialSubmissionReply | CredentialDispatchFailure`, separate from the generic
`BackendControl.request -> Result[BackendReply]`. It uses the existing operation identity
`BackendOperation.submit_pairing`, `CredentialPayload`, exact profile command mapping and final
secret index, but does not route the result through the transactional coordinator or change the
existing `BackendReply` union.

`CredentialRelayControl` in `backend.control` has this exact method:

```text
async submit_credential(
    target: ReceiverTarget,
    payload: CredentialPayload,
    context: OperationContext,
    *,
    dispatch_fence: DispatchFence,
) -> CredentialSubmissionReply | CredentialDispatchFailure
```

`PublishedDoubletakeControl` implements it. It uses the configured `ProcessPort.run_credential`
child-admission boundary. `DoubleTakeTransport.submit_credential` calls this method directly and
maps its typed result. It never infers dispatch certainty from a generic `Err` returned by
`BackendControl.request`.

Define the closed `NoReplyReason` literal in `domain.resources` and use it in process, backend and
transport results:

```text
NoReplyReason = Literal[
    "command_timeout", "service_shutdown", "child_cleanup_uncertain",
    "incomplete_reply", "malformed_reply", "dispatch_cancelled",
]
```

The existing `CredentialDispatchFailure.dispatch_state` has this exact meaning:

```text
"no_spawn"       ProcessPort proves no child was created
"acknowledged"   a complete positive or negative receiver reply was decoded
"uncertain"      a child may have run but no complete receiver reply was decoded
```

Extend `CredentialDispatchFailure` with optional `reply_at`, `reply_accepted`, and `reason` fields.
The reply fields are null for `no_spawn` and `uncertain`, and are present for `acknowledged`.
`reason` is null for `no_spawn` and `acknowledged`, and required for `uncertain`. A complete
negative reply uses fixed `refusal="rejected"`; lost ownership confirmation after a complete
reply uses fixed `refusal="stale"`. Neither case retains receiver text. A complete positive reply
is represented by:

```text
CredentialSubmissionReply {
    reply_at: float
    accepted: bool  # exact decoded `ok` field
}
```

Before `ProcessPort.run_credential` is entered, the adapter checks the recognized profile, builds and
validates the exact `ProfileCommand`, verifies `_available`, revalidates owned-daemon authority,
and evaluates the supplied freshness fence. Any refusal on this path returns
`CredentialDispatchFailure(dispatch_state="no_spawn", ...)` and does not invoke the process port.
The process port’s `CredentialProcessNotSpawned` result maps to the same classification. Only the
process port may produce `uncertain` after child admission; the adapter never infers `no_spawn`
from a generic process error.

The transport maps the adapter’s fixed `CredentialSubmissionReply` or
`CredentialDispatchFailure` fields to `Submitted` or `NoReply` below. It never copies backend text.

Extend `ProcessPort` with one credential-specific invocation while leaving generic `run` callers
unchanged:

```text
CredentialProcessResult =
    CredentialProcessNotSpawned { diagnostic: Diagnostic }
  | CredentialProcessCompleted { output: CommandOutput }
  | CredentialProcessUncertain { reason: NoReplyReason }

async run_credential(
    spec: CommandSpec,
    timeout: float,
    dispatch_fence: Callable[[], Result[None]],
) -> CredentialProcessResult
```

The output field is transient and excluded from repr, serialization, journals, and retained
observations. `LinuxProcessRunner.run_credential` validates argv and secret indices, invokes the
supplied `dispatch_fence` synchronously in the same task immediately before awaiting
`asyncio.create_subprocess_exec`, and performs no intervening await or task scheduling between the
predicate and subprocess-creation call. A failed predicate returns `NotSpawned` without creating a
child. Command validation or executable-creation errors return `NotSpawned` only when the runner
proves no child exists. If process creation itself is interrupted ambiguously, return `Uncertain`.
After a child exists, every timeout, cancellation, cleanup uncertainty or lost completion returns
`Uncertain`, even when the child is later reaped, because the daemon may have applied the command.
Normal child exit returns `Completed` with captured bytes for backend decoding. Fake process
adapters implement the same admission and cancellation-classification contract. The process runner
owns and contains subprocess exceptions. It must return `NotSpawned` only when it can prove process
creation did not create a child, and `Uncertain` for any exception or cancellation after creation
may have begun or a child may exist. The adapter maps decoder exceptions after a completed child
to `CredentialDispatchFailure(dispatch_state="uncertain", reason="malformed_reply")`. No raw
process or decoder exception crosses the boundary.

The transport result records are:

```text
Submitted {
    reply_at: float
    accepted: bool
    post_dispatch_error: ErrorCode | None
}

NoReply {
    reason: NoReplyReason
}
```

`NoReply` carries the closed `NoReplyReason` from `CredentialProcessUncertain` unchanged. The
reason is selected from `command_timeout`, `service_shutdown`, `child_cleanup_uncertain`,
`incomplete_reply`, `malformed_reply`, or `dispatch_cancelled`. `CredentialSubmissionReply.reply_at`
is captured from the adapter’s monotonic clock immediately after decoding the complete envelope.
Its `accepted` value is the exact typed `ok` field. Neither reply record contains `AckReply`,
stdout, stderr, or receiver text.

For a completed child with exit status 0 or 1 and a complete JSON envelope whose `ok` field is an
exact boolean, record `reply_at` immediately after decode. `ok=true` yields the positive
`CredentialSubmissionReply`. `ok=false` yields `CredentialDispatchFailure` with acknowledged
state, false acceptance, and fixed rejection. Other exit codes, malformed JSON, incomplete
objects, or missing/wrong-type `ok` after child creation are uncertain, because process completion
does not establish that the command had no effect. Backend free-form `error` text is discarded.

The transport maps the adapter result to `Result[Submitted | NoReply]`. `no_spawn` maps to
sanitized `Err`; `acknowledged` maps to `Submitted {reply_at, accepted}` or to the same `Submitted`
with `post_dispatch_error` when post-reply ownership confirmation fails; `uncertain` maps to
`NoReply {reason}`. A generic `dependency_incompatible` or `daemon_failed` after child admission
can never be classified as `no_spawn`. When `post_dispatch_error` is set, status polling is
refused and the marker remains for the §8 release path.

The relay method requires a non-null `dispatch_fence` before any ownership I/O. It passes that
same predicate to `_revalidate_ownership`/`EvidenceAuthority.authorize`, which checks after its
awaited proof work, and then passes it into `ProcessPort.run_credential` for a final synchronous
check at the child-creation boundary. Keep generic and transactional `submit_pairing` callers on
their existing trust-coordinator fence path. A wrapper that accepts but drops the relay fence is
rejected before process I/O. There is no automatic same-prompt retry.

The relay transport port adds these methods beside the existing stream methods:

```text
async submit_credential(
    target: ReceiverTarget,
    value: SensitiveText,
    context: OperationContext,
    *, dispatch_fence: DispatchFence,
) -> Result[Submitted | NoReply]

async read_credential_status(
    target: ReceiverTarget,
    context: OperationContext,
) -> Result[StatusReply]

async stop_pending_start(
    owner: PendingStartCleanupOwner,
    stop_context: OperationContext,
) -> Result[StreamHandle]
```

`DispatchFence` is the existing callable alias from `backend.control`. `submit_credential` passes
the exact original predicate through to the adapter and process boundary. `read_credential_status`
is read-only, uses the pinned daemon record’s explicit socket path, and never creates a request
from aggregate fields. Recovery status reads use the marker identity through the same owned-daemon
adapter.

`stop_pending_start` is used whenever a Mirror Start has emitted a live credential request, its
`SessionSnapshot.stream` is still `None`, and its `transport_start` acquisition is outstanding. It
supports both a pre-submit owner, whose `credential_submission_id` and `marker_generation` are
`None`, and a dispatched-submission owner, whose fields identify the actor-local
`CredentialSubmissionSettlement` and the marker generation observed when Stop captured it. The
transport validates the complete `PendingStartCleanupOwner` against its exact pending `_Stream`,
including the optional submission operation ID recorded from the submission context, and validates
the current Stop context before signalling. It sets a per-lease readiness-stop event without
acquiring `stream.lock`. `_await_connecting_stream` checks this event before its normal
operation-current test and races it against the outstanding read-only status request. If Stop wins,
it cancels only that readiness read. It never cancels the credential command or its owner-correlated
post-reply status read. `_start` exits through a dedicated handoff branch that skips its
failure-cleanup tail and does not drop the existing handle. `stop_pending_start` waits for that task
to release `stream.lock`, then transfers the `StreamHandle` exactly once to the caller. A duplicate
or stale handoff cannot obtain the handle. The original `start_stream` acquisition completes as
`Err(stale_event)` and never drops or publishes the transferred handle. A dispatched-submission
handoff does not start a second command timeout or alter the original `CONTROL_TIMEOUT` deadline.
The settlement keeps its original completion, bounded post-reply status observation, and
marker-transition ordering. `CleanupRuntime` waits for the settlement to reach its cleanup phase
and for the handoff/acquisition barrier before invoking target-specific `stop_stream`. A submitted
reply therefore receives its required status observation before disconnect cleanup; `NoReply`
persists `release_pending` before disconnect cleanup; and a pre-spawn `Err` follows its no-spawn
retirement rule. The handoff and settlement remain bounded by their existing deadlines and cleanup
bound. Stop passes the transferred handle through the existing ordered `stop_stream` cleanup stage.
The existing `stop_stream` path verifies target-specific disconnects against the exact target row in
an owned `StatusReply`. The receiver’s successful `disconnect TARGET` acknowledgement carries
`overallStateLocked()`, which may remain `pin_required` because a different stream still waits for a
credential. Ack state never proves that the stopped target remains. After a complete targeted
disconnect response, including a negative response for an already-absent stream, read status from
the same owned daemon and treat cleanup as settled only when no row has the exact canonical target.
A still-present matching row or failed status read leaves `cleanup_pending` and the existing target
fence active. Other rows and aggregate status do not affect this result. An incomplete disconnect
reply uses the existing uncertain cleanup path and never releases the recovery marker.

### 3.5 Completion fencing

Submission and status effects use fresh operation IDs and return owner-correlated completions through
`EffectEvent.result`. `CredentialSubmissionCompletion` repeats the full `CredentialEffectOwner`
tuple and carries only a sanitized `Submitted`, `NoReply`, or `Err`. `CredentialStatusCompletion`
repeats the same tuple and carries `result: Result[StatusReply]`, including sanitized transport and
status-decoding failures. In the normal path, the session handler compares each tuple field with the
active prompt and Start before publishing a report, changing deadline ownership, or touching the
recovery marker. If Stop superseded that Start, only the exact actor-local
`CredentialSubmissionSettlement` may consume the completion, with reporting suppressed and the
marker-generation transition described in §3.2. The credential flow never changes a newer session.

The transport’s local request and recovery operations are fenced by the original target and daemon identity. The receiver has no request ID, so this fencing narrows local races but does not establish receiver-side request identity or physical-device continuity.

## 4. Command construction and validation

### 4.1 Recognized profiles

The published `doubletake-ctl` surface has no version banner. Capability recognition uses the exact pinned help-table fingerprint, not a claimed version or an unrelated token. The two accepted control signatures are the pinned 0.5.0 table and the exact extended target-volume table from the pinned patch chain.

`credential_relay` is true only when the help surface is verified and its control signatures equal one of those two tables. All other, partial, malformed, or contradictory tables fail closed. Target scope is proven only by the exact `[target]` argument declaration and affirmative pinned scope wording for each relevant command. Any negation or unparseable table row invalidates that proof.

### 4.2 Exact argv forms

Every command uses the explicit socket path from the owned daemon record and a target-first credential form:

```text
doubletake-ctl -socket SOCKET connect TARGET CREDENTIAL
```

The legacy profile carries no audio or volume options. The extended profile may carry the snapshotted Start options in the recognized order:

```text
doubletake-ctl -socket SOCKET connect TARGET [--audio on|off] [--volume N] CREDENTIAL
```

Use the extended flags only when the exact extended signature and `target_volume` are positively proven. If the recognized profile cannot carry the original Start’s audio or initial-volume choices, refuse before spawn instead of silently changing them. The credential is always the final argv element. `ProfileCommand.secret_indices` contains exactly `len(argv) - 1`, and every redacted projection replaces that element with the fixed redaction token. The targetless historical `pin` command is never invoked.

The app-level payload is `CredentialPayload(value: SensitiveText, audio: AudioPolicy | None, initial_volume: VolumeLevel | None)`. Its representation never includes the value. `ProfileCommand.redacted_argv`, command journals, process fakes, probe recorders, diagnostics, and acceptance recorders must all use the declared secret index. `SourcePayload` and the existing transactional `PairingPayload` retain their redacted representations. `VolumePayload` continues to require an exact `VolumeLevel`, and `AudioGatePayload` continues to represent only `False`.

### 4.3 Input validation

The `PairingDialog` submit handler validates the transient raw text before constructing
`SensitiveText` or issuing `SubmitCredential`. Reject empty, NUL-containing, platform-unencodable,
and leading-dash input. Use the profile-independent
`relay_credential_representable(value)` predicate for the leading-dash check and remove its obsolete
`target_volume_proven` parameter. The control adapter repeats validation after revealing the value
at the backend boundary and before command construction or process spawn. This conservative rule
applies to both profiles because the extended parser reserves option-shaped operands. It does not
claim that the legacy parser rejects them. Invalid input returns fixed, secret-free feedback without
an actor command, marker write, command construction, or child process.

Do not add a PIN digit count, password format, maximum-length rule, normalization, or truncation. The exact operator-entered text is passed as one argv element after representability checks.

## 5. Capability and authorization

Keep capability sources independent.

| Capability or authority | Required evidence | Effect in this feature |
| --- | --- | --- |
| `credential_relay` | Verified help plus exactly one of the two pinned command tables | Allows construction of the target-bound credential command |
| `target_volume` | Exact extended table, required source tokens, and the pinned target-volume help token | Allows the extended audio and volume options |
| `keyring` | Runtime’s three-leg credential-service proof: service, backend-positive, and backend-negative legs | Participates in composed public Start admission; it is not inferred from the help object |
| `owned_daemon_control` | Production launcher graph, available verified launch snapshot, and no ambient socket | Participates in composed public Start admission and owned command authorization |
| Transaction flags | The probed profile leaves `target_bound_pairing`, `provisional_commit`, `cancel_pairing`, and `revoke_pairing` false | No transactional trust capability is added by `credential_relay` |

Public Start admission requires the composed `owned_daemon_control` and `keyring` capabilities. The probed help/profile object’s `keyring == false` does not override or negate the runtime’s separate three-leg keyring proof. Likewise, a profile’s `target_volume` result is independent of composed keyring authority.

The relay uses the existing `EvidenceAuthority` authorization path for the credential-bearing backend operation. Its receipt callback synchronously rereads the complete durable receipt set on each invocation. It must not use cached receipt proof, and post-pidfd-open validation cannot await repository access. The supervisor alone supplies its own release intent. Registry recovery may read that intent as evidence but never uses it to re-signal a departed supervisor’s daemon.

The existing `HandshakeAuthority` continues to require owner-handshake capability, the explicit profile socket path, matching daemon instance, owner token and protocol version, and a successful status proof. A mismatch maps to the existing typed `dependency_incompatible`, `stale_socket`, or `ownership_mismatch` path. `shutdown` retains its existing special handling.

For target-scoped `disconnect`, `mute`, and `unmute`, require an exact `[target]` signature and affirmative scope proof. This proof is independent of `credential_relay`. Mutating target operations continue to re-evaluate their `DispatchFence` after adapter-owned awaits and immediately before spawn. Pairing/keyring operations continue to use the trust coordinator’s generation fences where that existing path applies.

## 6. Prompt ownership and presentation

### 6.1 Trust-neutral projection

The credential prompt is transient actor state, not trust or durable session data:

```text
CredentialPromptState {
    request: CredentialRequest
    challenge: ChallengeView
    state: "editing" | "submitting" | "observing"
    submission_id: OperationId | None
    report: "connected" | "not_observed_streaming" | "outcome_unknown" | None
}
```

Add `credential_prompt` to `SessionSnapshot`, set it to `None` in
`SessionPersistence._safe_snapshot`, and omit it from the closed `SESSION_SCHEMA` fields in
`storage/codecs.py`. Keep the transactional `challenge`, trust state and phase unchanged. The
unknown-outcome state is held only by `CredentialRecoveryMarkerStore`.

Add transient `PaneState.credential_prompt: ChallengeView | None` and `PaneState.credential_recovery: CredentialRecoveryView | None` projection fields. Derive the credential prompt only when its request, challenge and owning Start identities match
the current session, cleanup is certain, no release-pending marker exists, and no transactional
challenge is present. Expose it when another pane is selected. A stale relay prompt projects to
`None` before evaluating the existing transactional challenge predicate.

Add `credential_recovery: CredentialRecoveryView | None` and `recovery_store_blocked: bool` to
`ViewInputs`. Obtain the exact-target recovery view and global store-health bit from
`CredentialRecoveryCoordinator`, never reconstruct them from `SessionSnapshot`. Match a recovery
view by the target’s exact `canonical_host`, not receiver ID alone. An offline saved receiver that
owns a marker still shows its blocked-target warning. A true `recovery_store_blocked` removes Start
actions for every pane until startup recovery reloads and validates all marker records.

`CredentialRecoveryView` is read-only data derived from the validated marker. It contains the
target label, marker state, automatic-check state, warning text and offered actions. While automatic
checks are enabled, title the view `Cleanup pending`. After dismissal disables checks, title it
`Target blocked` and state that this target remains fenced until receiver restart. `Cleanup pending` offers `Action.stop_credential_recovery` and `Action.dismiss_credential_recovery`.

The UI maps recovery actions to fixed labels and descriptions in `ui/window.py`:

```text
Action.stop_credential_recovery:
    label "Stop"
    description "Check this target again and disconnect it on the recorded receiver daemon"
Action.dismiss_credential_recovery:
    label "Dismiss recovery warning"
    description "Stop automatic checks; this target stays blocked until the receiver restarts"
Action.restart_receiver_to_release:
    label "Restart receiver to release"
    description "Stops every mirror owned by this app before it releases the target"
```

`release_pending` offers `Action.stop_credential_recovery` and
`Action.restart_receiver_to_release`. While its `automatic_status_checks` is true, it also offers
`Action.dismiss_credential_recovery`; the action disappears after dismissal. An unreadable marker
store projects a global Start-blocked notice without Start actions. The restart action appears
only for a validated `release_pending` marker and starts only after an explicit click. The ordinary
`Action.stop` continues to route to the existing cleanup handler.

Reuse `PairingDialog` through a separate relay mode and presenter map keyed by relay prompt identity.
Add `Action.submit_credential` to the dialog’s closed action vocabulary. The relay mode returns
`SensitiveText` to `SubmitCredential`; it never maps to `SubmitPairing` or calls `PairingPort`. Add
`Action.dismiss_credential_recovery` and `Action.restart_receiver_to_release`. The existing
`challenge` field, transactional pairing actions, dialog mode and presenter map remain unchanged.

### 6.2 Modal and prompt lifetime

Open the prompt for the receiver whose Start produced it, even when another pane is selected. Selection changes never retarget an open prompt. Repeated status reads for the same local prompt identity do not reopen it. A new kind or a replacement Start invalidates the old prompt and its challenge ID. In relay mode, offer only `submit_credential` and `Cancel`. Do not offer transactional retry, trust, revoke, or Forget actions. Do not render `PairingDialog`’s transactional challenge-expiry text for this prompt because the approved relay has no typing deadline.

The dialog shows the existing PIN or password wording. Its field is masked and marked sensitive so input methods do not retain prediction history. `PairingDialog.take_input()` returns one `SensitiveText` and clears the line edit in the same call. The input remains only in the widget until that call and in the transient command argument during dispatch.

Disable the field while submit is in flight. While the operator is typing, no command timeout or
status-wait timer runs. After a complete reply, keep the field read-only during the bounded status
observation. Cancel closes the prompt and sends no receiver command. Before submit, cancellation
resumes the owning Start’s suspended readiness budget. Ordinary Stop is different from dialog
Cancel. While a matching credential request is live, Stop uses `stop_pending_start` to cancel the
read-only readiness wait and hand the exact handle to ordered cleanup. This applies before and after
a credential command is dispatched. After submit, Cancel closes reporting but cannot recall the
dispatched command or discard `CredentialSubmissionSettlement`. If Stop supersedes that Start while
its snapshot handle is absent, the settlement supplies the submission ID and marker generation to
`stop_pending_start`; the handoff signals readiness exit without cancelling the command or its
owner-correlated status read. The original command deadline and post-reply observation remain in
force, and ordered cleanup waits for the settlement transition and exact handle handoff before
disconnecting. The Start’s ordinary readiness handling and unknown-outcome recovery remain owned by
their existing operations.

A torn-down window or any newer command that supersedes the prompt clears its widget contents and retires its local challenge. The `PairingDialog` remains an explicit modal window rather than a window-modal block, so the dashboard’s existing Retry control remains available. Existing owner-modal Stop, resolution, and orphan decisions retain their current modality. Audio and cleanup decisions remain inline beside the notice.

## 7. Deadlines and reporting

### 7.1 Deadline ownership

The Start operation owns the existing permission/readiness wait. While its matching credential request is live, suspend the `PERMISSION_WAIT_TIMEOUT` budget and record the pause offset. Resume it when submit is attempted, the matching row ends, or the operator cancels the prompt. Shift the deadline by the exact paused duration. An ordinary Stop overrides this suspension by cancelling the exact wait and transferring its handle into ordered cleanup. Stop does not wait for the resumed readiness deadline. Typing time is not charged to the Start readiness budget.

The existing first-stream acceptance bound is not restarted by prompt publication. The handshake deadline retains its existing source-ready ownership and is not repurposed as a credential deadline. A portal permission prompt remains governed by its existing human-bounded, cancellable path.

`CONTROL_TIMEOUT` begins when the operator submits and bounds the command call. If a complete reply
is decoded, start `CONNECT_STREAMING_TIMEOUT` at that reply and poll the same owned daemon’s typed
status. This report wait is independent of the resumed Start readiness budget. If it expires, close
the prompt and report the non-streaming observation. The pending Start may continue until its own
resumed deadline unless ordinary Stop has superseded it. After Stop, if the snapshot handle is still
absent, invoke the dispatched-submission form of `stop_pending_start` and signal readiness exit
without waiting for that resumed deadline. The original command deadline and post-reply status
observation still govern the settlement. A later streaming sample may affect the Start’s ordinary
readiness result, but it never reopens the prompt or retroactively changes its report.

A missing complete reply has no reply-based streaming wait. It enters §8 recovery. Status-read errors use the existing typed transport failure path. A status sample never creates a receiver request unless the matching stream row independently meets §2.

### 7.2 User-visible outcomes

| Observation | User-visible result | Prompt and Start behavior |
| --- | --- | --- |
| Before submit, the matching request row ends | No PIN result | Close prompt and resume Start |
| After submit, the row leaves `pin_required` but remains present | No result yet | Continue polling and keep prompt read-only |
| The exact target is reported `streaming` within the bounded wait | `Connected to <TV>.` | Close prompt; report only the receiver’s streaming state |
| The target is absent after submit, or is not `streaming` at the deadline | `The target was not observed streaming.` | Close prompt; do not diagnose why |
| No complete command reply is decoded, including service shutdown | `No complete command reply was received; the outcome is unknown.` | Close prompt and retain the recovery marker |
| The operator cancels | No additional command and no later prompt report | Close prompt; an already dispatched command may still take effect |

A complete negative reply is `Submitted` and is followed by status polling. Do not parse receiver error text or copy it into a diagnostic. A receiver refusing the credential does not leave the request open for retry. A new attempt requires a new operator Start.

`Connected to <TV>.` means only that the receiver’s status reported the named target in `streaming`. It does not claim that a frame was delivered, received, or displayed. A missing row or non-streaming sample does not prove the target never streamed between samples.

## 8. Unknown-outcome recovery and release

### 8.1 Durable marker

The marker is a dedicated durable record, separate from session snapshots:

```text
OriginalDaemonIdentity {
    launch_uuid: UUID
    instance_uuid: InstanceId
    endpoint_generation: positive int
    daemon_process: ProcessIdentity
    runtime_directory: private path
    socket_path: explicit private socket path
}

CredentialRecoveryMarker {
    schema_version: 1
    target: exact canonical target string
    receiver_id: originating ReceiverId
    start_operation_id: originating OperationId
    original_daemon: OriginalDaemonIdentity
    marker_generation: positive int
    state: "watching" | "release_pending" | "retired"
    automatic_status_checks: bool
    release_attempt: ReleaseAttempt | None
    retirement: MarkerRetirement | None
}

MarkerRetirement {
    kind: "no_spawn" | "terminal_reply" | "daemon_replaced"
    no_spawn: NoSpawnRetirement | None
    reply: TerminalReplyRetirement | None
    release: ReleaseEvidence | None
}

NoSpawnRetirement {
    failure: ErrorCode
    stop_cleanup_settled: bool
    observed_at: float
}

TerminalReplyRetirement {
    reply_at: float
    accepted: bool
    target_observation: "streaming" | "absent" | "not_streaming_at_deadline"
    observation_at: float
    stop_cleanup_settled: bool
}

ReleaseAttempt {
    attempt_id: OperationId
    replacement_runtime_directory: private path
    replacement_socket_path: explicit private socket path
    status: "intended" | "ready" | "terminal"
    replacement_instance_uuid: InstanceId | None
    replacement_endpoint_generation: positive int | None
    readiness: OwnedDaemonReadinessEvidence | None
    terminal_evidence: ReleaseAttemptTerminalEvidence | None
}

ReleaseAttemptTerminalEvidence {
    outcome: "not_spawned" | "replacement_terminated"
    replacement_launch_uuid: UUID | None
    process_evidence: "no_child" | "pid_absent" | "pidfd_exited"
    verified_at: float
}

ReleaseEvidence {
    old_daemon_termination: TerminationProof
    replacement_instance_uuid: InstanceId
    replacement_endpoint_generation: positive int
    replacement_runtime_directory: private path
    replacement_socket_path: explicit private socket path
    readiness: OwnedDaemonReadinessEvidence
}

OwnedDaemonReadinessEvidence {
    launch_uuid: UUID
    recovery_attempt_id: OperationId
    instance_uuid: InstanceId
    endpoint_generation: positive int
    runtime_directory: private path
    socket_path: explicit private socket path
    daemon_pid: int
    daemon_start_ticks: int
    socket_device: int
    socket_inode: int
    verified_at: float
}
```

A release attempt’s `attempt_id` is the `recovery_attempt_id` persisted in its replacement launch
receipt and readiness evidence. `status == "intended"` has no replacement identity, readiness, or
terminal evidence. `status == "ready"` requires matching replacement identity, endpoint
generation, readiness, and no terminal evidence. `status == "terminal"` requires
`ReleaseAttemptTerminalEvidence` and no ready replacement. Each transition updates the marker
atomically and increments its generation.

The marker contains no PIN, password, `SensitiveText`, credential-bearing payload, argv, command
output, free-form receiver response, or serialized prompt. `OriginalDaemonIdentity` copies the exact
launch UUID, daemon instance UUID, endpoint generation, daemon process PID/start-ticks identity,
private runtime directory, and socket path from the owned daemon record when the marker is created.
These fields let startup recovery resolve historical receipts without depending on the old
`DaemonRegistry._record` cache. Its on-disk filename is the SHA-256 of the exact canonical target
bytes, so path parsing never depends on a receiver-controlled string. The record still carries the
target and originating receiver identity, and the target fence uses the target string across
receiver IDs.

Add `CredentialRecoveryMarkerStore` beside `SessionStore`, `DaemonStore`, and the lifecycle
stores. Its methods are serialized by one store-owned async lock and compare the complete marker
identity plus `marker_generation` before writing:

```text
read_all() -> Result[tuple[CredentialRecoveryMarker, ...]]
confirm_all_durable() -> Result[None]
for_target(target) -> Result[CredentialRecoveryMarker | None]
create(marker) -> Result[CredentialRecoveryMarker]
mark_release_pending(identity, expected_generation) -> Result[CredentialRecoveryMarker]
dismiss(identity, expected_generation) -> Result[CredentialRecoveryMarker]
record_release_attempt(identity, expected_generation, attempt) -> Result[CredentialRecoveryMarker]
record_replacement_ready(identity, expected_generation, readiness: OwnedDaemonReadinessEvidence) -> Result[CredentialRecoveryMarker]
retire_no_spawn(identity, expected_generation, evidence: NoSpawnRetirement) -> Result[CredentialRecoveryMarker]
retire_after_reply(identity, expected_generation, evidence: TerminalReplyRetirement) -> Result[CredentialRecoveryMarker]
retire_after_replacement(identity, expected_generation, evidence: ReleaseEvidence) -> Result[CredentialRecoveryMarker]
```

Every successful transition increments `marker_generation`. `dismiss` is one atomic record write
that sets `state = "release_pending"` and `automatic_status_checks = false` together. New markers start in `watching` with `automatic_status_checks = true`. `mark_release_pending`
sets `state = "release_pending"` and preserves the current `automatic_status_checks` value. A Stop
racing `NoReply` therefore keeps checks enabled until dismissal. Neither this transition nor a later
Stop may re-enable checks after dismissal. `retire_no_spawn` accepts only a typed `no_spawn` result and records whether any concurrent Stop
cleanup settled. `retire_after_reply` accepts only an acknowledged reply plus a terminal target
observation and settled concurrent cleanup. `retire_after_replacement` accepts only exact old
termination proof and matching replacement readiness evidence for the durable release attempt.
All three write `state = "retired"`, disable checks and store the corresponding immutable
`MarkerRetirement` evidence in one atomic publication. A retired record remains as a tombstone.
A new marker for the target may replace it only with a strictly larger generation. A watching or
release-pending marker cannot be overwritten by another Start.

`for_target` compares the exact canonical target from the decoded record. The on-disk name is the
lowercase SHA-256 of its UTF-8 bytes, so receiver text is never used as a path. `read_all` rejects
unknown filenames, digest mismatches, duplicate target records, malformed records and unreadable
files as one typed error. It never skips corrupt evidence or reports an empty store on error.

Compose `JsonCredentialRecoveryMarkerStore` in `runtime.py` from the application’s existing
`AtomicFiles` root. Use a dedicated `credential-recovery/` directory and a strict versioned codec
in `storage/codecs.py`. Every visible entry must have a valid target digest, schema and record.
`read_all` returns a typed error for any malformed, duplicate, unreadable or digest-mismatched
entry. It never skips corrupt evidence or reports an empty store on error. The store uses the
existing `AtomicFiles.write` file-and-directory durability convention. Each transition rereads
and compares marker identity and generation under the store lock, then publishes the whole next
record. A normal return from `AtomicFiles.write` is the only confirmation that the entry and every
required directory were flushed. If the write reports an error after rename, exact next-generation
bytes are visible evidence of publication, not durability confirmation. Keep the old in-memory
fence and leave the operation unacknowledged, then retry `AtomicFiles.write` with the exact same
next-generation bytes under the store lock. Only a retry that returns successfully may advance the
coordinator cache, acknowledge dismissal or retirement, change the UI, or release any fence. If a
retry reports an error, retain the global or target fence. `read_all` may select the bytes to retry,
but reading visible next-generation bytes alone never clears the barrier. Any other bytes keep
Start admission blocked. The store reports storage and codec failures as
`Err(Diagnostic(code=persistence_failed, message="Recovery state could not be verified.",
recovery=(keep_blocked,)))`. Do not include paths, raw exceptions or marker contents in that
message. A transition conflict uses `ownership_mismatch` with the same `keep_blocked` action.
Retired marker records remain as tombstones so their generation cannot be reused.

`confirm_all_durable` is a startup-only store operation. Under the store lock, it rereads and
strictly validates every visible marker, including retired tombstones, then republishes each exact
canonical record through `AtomicFiles.write` without changing its generation. It returns success
only after every write confirms file and directory durability. A failed or ambiguous confirmation
keeps the process-wide Start barrier active. Startup must not trust a visible `retired` record or
release its target fence from readback alone. If an application exited after rename but before a
transition’s durability confirmation, a later startup can trust that transition only after
`confirm_all_durable` successfully flushes the exact validated bytes. A failure leaves Start blocked
until a subsequent successful confirmation.

`retire_no_spawn` and `retire_after_reply` require the evidence variants defined above.
`retire_after_replacement` requires exact old-daemon termination proof and replacement readiness
evidence matching the persisted release attempt.

Add `CredentialRecoveryCoordinator` to `ManagerDeps`. Construct it with the marker store and a closed
global Start-admission barrier. `StartupRecovery.run` invokes
`CredentialRecoveryCoordinator.load_before_startup()`, which reads and validates all marker records,
then calls `confirm_all_durable()` before publishing startup events, trusting retired tombstones, or
accepting any `Start`. The coordinator populates its target-keyed fence table only after this
confirmation succeeds. It then awaits `recover_before_registry_reconciliation()`, which reconciles
marker-bound attempts and returns the exact watching-daemon and release-replacement exemptions.
`StartupRecovery.run` passes those exemptions to
`DaemonRegistry.reconcile_previous(recovery_exemptions=...)`; no ordinary registry reconciliation
runs first. Any read, validation, or durability-confirmation error keeps the global Start-admission
barrier active and returns a sanitized recovery-store diagnostic for the dashboard.

For each marker, startup recovery binds receipts to `OriginalDaemonIdentity` by the complete
`launch_uuid`, instance UUID, endpoint generation, daemon PID/start ticks, runtime directory, and
socket path tuple. A watching marker resumes status checks only after the registry authenticates
that exact live daemon and socket. A stopped or otherwise unavailable original leaves the marker
and target fence intact and sends no command. For a release-pending marker with an attempt, startup
recovery first reconciles that exact attempt: it adopts a replacement only when one unique launch
receipt is bound to the attempt ID and records matching private endpoint, instance, generation,
process identity, socket identity, and successful owned-daemon readiness. It then writes
`record_replacement_ready` and `retire_after_replacement` durably before ordinary startup
reconciliation can stop that replacement. An attempt with a receipt proving terminal cleanup and
no ready replacement is closed as failed and retried with a new attempt ID and never-used endpoint.
An absent receipt is treated as no effect only when the attempt reservation and endpoint inspection
prove no child or socket was created. Any partial, duplicate, unreadable, or contradictory attempt
evidence keeps the target blocked and prevents a new launch. Ordinary `reconcile_previous` then processes every daemon record except an exact live original
returned as a `DaemonRecoveryExemption` for a watching marker and a matching ready replacement
returned after an interrupted release attempt. For the latter, reconciliation adopts the verified
replacement as the current registry record after the marker transition is durable. No unrelated
receipt or same-address process is exempted.

`CredentialRecoveryCoordinator` exposes these methods to session handlers:

```text
load_before_startup() -> Result[None]
recover_before_registry_reconciliation() -> Result[tuple[DaemonRecoveryExemption, ...]]
require_start_allowed(target, start_operation_id) -> Result[None]
start_dispatch_fence(target, start_operation_id) -> DispatchFence
begin_submission(marker) -> Result[CredentialRecoveryMarker]
credential_dispatch_fence(marker_identity, marker_generation) -> DispatchFence
```

`start_dispatch_fence` composes with the existing receiver/endpoint freshness fence for every
Mirror connect. It returns `persistence_failed` with `keep_blocked` while the global store-health
barrier is active. It returns `ownership_mismatch` with `keep_blocked` while a non-retired marker
or provisional same-target reservation belongs to another Start or is release-pending. `begin_submission`
reserves the canonical target in memory before awaiting the store’s durable `create`, then returns
the verified marker. Its credential dispatch fence allows only the exact owning Start and marker
generation in `watching` state. Both predicates read the coordinator table synchronously. All
table mutations occur through the coordinator, so callbacks cannot observe an uncommitted store
transition.

The coordinator is the sole marker writer. Its cache changes only after verified store
transitions. Marker data never enters the durable `SessionSnapshot` codec, activity records,
command journals, exports or callback representations.

Call `CredentialRecoveryMarkerStore.create` before invoking `CredentialRelayControl.submit_credential`.
The coordinator first reserves the target in memory, then waits for durable store success. If
publication reports an error, reread by target and retry the exact intended marker bytes through
`AtomicFiles.write` so the full durability confirmation runs again. Only a successful retry permits
the marker to become the verified cache value and the current dispatch to proceed. If the retry
fails, exact marker bytes keep the target fenced and the current dispatch is refused; proven
absence returns a pre-spawn error and dispatches nothing. An ambiguous reread keeps the process-local
global Start barrier until a successful write confirms the selected bytes. A typed `no_spawn` result
may call `retire_no_spawn` only after concurrent Stop cleanup settles. A `NoReply` retains the marker. For an acknowledged reply, call
`retire_after_reply` only after the post-reply status path is terminal and any concurrent Stop
cleanup has settled. Its evidence records reply time, acceptance bit, exact target observation and
observation time. If status reads or marker writes fail, retain the marker and target fence.
Retirement evidence is durable before the UI removes the warning or admits another Start.

If Stop overlaps a credential command and no complete reply arrives within `CONTROL_TIMEOUT`, use
`mark_release_pending` before publishing the warning. Neither a late disconnect reply nor an absent
sample clears the marker. An observation window expiring also does not clear it. Once the warning
is published, status observations and Stop cleanup do not change the marker state. Explicit
dismissal uses the single atomic `dismiss` operation, which disables automatic checks and preserves
the Start fence in the same write.

### 8.2 Watching and Stop

While `automatic_status_checks` is true, periodically read status from the original owned daemon while the warning is retained. If exactly one row for the recorded target is present, issue a target-specific disconnect. Never disconnect all streams. An absent row does not clear the marker. Duplicate or ambiguous target rows, failed status reads, failed disconnects, or unverified daemon identity retain the warning and report only the failed observation or cleanup. A disconnected observation and a successful disconnect do not change the marker state.

When ordinary `Stop` reaches a Start whose live credential request has not been submitted, capture
`PendingStartCleanupOwner` before `ctx.supersede("stop")` with no submission or marker generation.
Call `stop_pending_start` to wake the pending target-status wait, cancel only its read-only poll, and
transfer the exact handle to ordered cleanup. Do not wait for the suspended readiness budget, issue a
credential command, or create a recovery marker. The existing target-specific disconnect must settle
before Stop clears `cleanup_pending` and admits another Start.

When `Stop` reaches a Start with a dispatched credential command, first attach its operation to the
actor-local `CredentialSubmissionSettlement`, set `report_suppressed`, and then supersede the Start.
If `SessionSnapshot.stream` is still absent and `transport_start` remains outstanding, capture a
`PendingStartCleanupOwner` containing the settlement’s submission ID and current marker generation,
and call `stop_pending_start`. That handoff signals the readiness exit and transfers the exact handle
once even when the receiver has already acknowledged the credential and reports the target as
`connecting`. It does not cancel the child, its credential completion, or its owner-correlated status
read, and it does not start another `CONTROL_TIMEOUT`. The settlement retains the remainder of its
original command deadline. A complete reply starts the bounded post-reply status observation at its
actual reply time. A `NoReply` durably persists `release_pending` before cleanup. A pre-spawn `Err`
uses its no-spawn retirement rule. `CleanupRuntime` waits for this settlement transition and the
pending-start acquisition/handle handoff before issuing target-specific disconnect cleanup. The
settlement remains admissible after supersession and prompt dismissal, but it cannot publish a prompt
result or mutate the superseded Start. For an acknowledged reply, retire the marker only after the
required status observation and cleanup settle. If the command returns `NoReply`, publish the warning
and perform cleanup while the marker and target fence remain until release §8.3 succeeds. Stop may
finish after the original bounded command/status wait and cleanup attempt. If the prompt had already
been locally dismissed, its settlement still follows these rules. A Stop invoked after a prior
`NoReply` follows the same marker-transition and cleanup ordering. If marker persistence fails, keep
the in-memory target fence, report the storage failure, and refuse same-target Start.

`StopCredentialRecovery` is available after the ordinary stream handle has retired. It performs
another target-specific recovery attempt on the exact marker-owned daemon. A successful disconnect
or absent row does not retire the marker because the unresolved credential may still take effect.
It leaves the release fence active until proof in §8.3.

On application restart, restore the marker before admitting Start for its target. Resume watching only when the exact recorded owned daemon instance, endpoint generation, runtime directory, and socket path can be verified. Otherwise keep the warning and fence without sending a command to another daemon. No monitoring claim applies while the application is not running.

The visible warning is labelled `Cleanup pending`. It says that a delayed connection may still appear, Stop may need to be used again, and dismissal stops automatic checks while keeping the target blocked. It remains available after ordinary stream cleanup and across application restarts.

### 8.3 Dismissal and release-pending

Dismissing the warning durably sets `state = "release_pending"` and `automatic_status_checks = false`. Apply the UI transition only after the durable write succeeds. If the marker was already `release_pending` because Stop overlapped an unresolved command, dismissal still durably disables automatic checks. The same-target Start fence remains active. The blocked-target view retains the explicit `Restart receiver to release` action and states that it disconnects every mirror owned by the shared daemon.

Starting release requires the operator’s direct action on the clearly labelled control. The
recovery coordinator creates a fresh `attempt_id` and a unique private runtime directory/socket
path under the app-owned runtime root. It persists that `ReleaseAttempt` through
`CredentialRecoveryMarkerStore.record_release_attempt` before touching the daemon. Every later
attempt uses a different ID and path. A proposed path equal to the old endpoint, already assigned
to another daemon generation, non-private, or not durably recorded refuses the operation and keeps
the marker and target fence.

Add the following operation to the existing `DaemonRegistry` lifecycle boundary:

```text
restart_with_new_endpoint(
    expected_old: OriginalDaemonIdentity,
    attempt_id: OperationId,
    runtime_directory: PrivateRuntimePath,
    socket_path: PrivateSocketPath,
) -> Result[OwnedDaemonRecoveryRestart]
```

The registry holds its existing lifecycle lock throughout. It verifies the `expected_old`
identity against the current record when present, or resolves exactly one historical launch receipt
when the old record was already reconciled after an application restart. The receipt, cleanup
intent, supervisor pair, process identity, runtime directory, socket path, and endpoint generation
must all match `OriginalDaemonIdentity`.

The registry call is `termination_proof(identity) -> Result[TerminationProof]`. It acquires the
lifecycle lock once and delegates to
`_termination_proof_locked(identity, matching_receipt) -> Result[TerminationProof]`, which never
acquires the lock. `restart_with_new_endpoint` already owns the same lock and calls only the locked
helper after validating the exact historical receipt. The helper checks the persisted cleanup
intent and exact process evidence before constructing `TerminationProof`. The registry
reconciles all recorded receipts, daemon cleanup intents, supervisor pairs, crash-window ownership
scans and live endpoints, and refuses while any old pair, live endpoint, ambiguous process, or
ownership obligation remains. It then creates and validates the supplied unique private runtime
path, durably records the reservation and endpoint generation together with `attempt_id`, launches
and authenticates the replacement on the supplied explicit socket, and waits for normal owned-daemon
readiness. The launch receipt and readiness evidence both carry `recovery_attempt_id == attempt_id`.
It never falls back to ordinary `restart()` or the configured socket.

`OwnedDaemonRecoveryRestart` contains the exact old `TerminationProof`, replacement
`DaemonRecord`, endpoint-generation receipt and readiness `OwnershipProof`. The coordinator checks
all fields against the marker and `ReleaseAttempt`, persists `record_replacement_ready`, then
persists `retire_after_replacement` with the resulting `ReleaseEvidence`. Only after that durable write does it
remove the target fence and admit a same-target Start. The operation is idempotent by `attempt_id`. On application restart, reconcile the attempt’s
receipts before ordinary registry reconciliation. Reuse a matching ready replacement only when the
launch receipt’s `recovery_attempt_id`, endpoint, instance, generation, process identity, socket
identity, readiness, and exact old-daemon termination proof all match the persisted marker and
attempt. If the attempt is proven terminal without a ready replacement, persist its terminal
evidence and allocate a new attempt ID and never-used endpoint. If any lifecycle evidence remains
ambiguous, keep the marker blocked and do not create another daemon.

`DaemonRecoveryExemption` contains a marker identity and generation, a kind of
`"watching_original" | "release_replacement"`, and one exact daemon identity. A watching-original
exemption contains the complete `OriginalDaemonIdentity` of a live daemon authenticated for that
marker. A release-replacement exemption contains the verified `OwnedDaemonReadinessEvidence` for
the matching attempt whose release marker was durably retired. The registry accepts only exemptions
revalidated against the exact receipt and process identity. `reconcile_previous(recovery_exemptions=...)`
retains the exact watched original or adopts the exact ready replacement as the current registry
record. It still reconciles every other record. The startup path obtains exemptions from
`recover_before_registry_reconciliation` after loading markers and before ordinary reconciliation.

Every control command remains bound to the socket path in its daemon record. A stale client for
the old endpoint cannot reach the new daemon. The release check is bound to daemon instance and
endpoint generation, not PID alone. If termination, endpoint separation, registry reconciliation,
or replacement readiness cannot be proven, retain the marker and block Start.

This restart interrupts every mirror owned by the shared daemon. The warning must be shown before restart begins. If a host transition is required, do not perform reboot, shutdown, poweroff, halt, kexec, systemd power/sleep, display-manager or graphical-target changes, session termination, or signals to the PM process tree. Write `reboot-required.json` with evidence and provenance, then stop.

For an unknown outcome retained across application restart, restore the marker and its `automatic_status_checks` value. When checks remain enabled, resume observation only after verifying the exact original daemon identity. A dismissed marker keeps checks disabled. Retire that marker only after proving the original daemon terminated and verifying a distinct replacement endpoint. Otherwise keep the target blocked until the recovery-specific restart completes.

### 8.4 Stale recovery callbacks

Every recovery operation captures the marker identity and a local recovery generation. Retiring or replacing that operation fences all of its callbacks. A stale callback cannot issue another disconnect, clear a newer marker, update a newer session, or release a newer target fence.

A disconnect already sent cannot be recalled and may take effect late against the original daemon. Keep all same-address Starts blocked until that daemon is gone and the replacement is verified. Recovery, like credential submission, identifies a stream by target address rather than receiver request identity. An unrelated connection at the same address is indistinguishable under the accepted continuity assumption.

## 9. Failure classification and security

| Profile/input/capability or freshness refusal before process creation | `CredentialDispatchFailure(dispatch_state="no_spawn")` and transport `Err` | No process call; retire a newly created marker only after concurrent Stop cleanup has settled |
| `ProcessPort.run_credential` proves no child was created | `CredentialProcessNotSpawned` → `CredentialDispatchFailure("no_spawn")` → `Err` | Do not describe a receiver outcome |
| Child may have run but no complete envelope was decoded | `CredentialProcessUncertain` → `CredentialDispatchFailure("uncertain")` → `NoReply` | Report unknown outcome and retain the marker |
| Complete positive reply was decoded | `CredentialSubmissionReply(accepted=true)` → `Submitted` | Begin bounded status observation; reply alone is not success |
| Complete negative reply was decoded | `CredentialDispatchFailure("acknowledged", refusal="rejected")` → `Submitted(accepted=false)` | Begin bounded status observation; report only its resulting status |
| Complete reply decoded, then ownership confirmation failed | `CredentialDispatchFailure("acknowledged", refusal="stale")` → `Submitted` with `post_dispatch_error` | Refuse status reads; retain marker and fence |
| Recovery status read or target-specific disconnect fails | Existing sanitized transport failure | Keep warning and Stop action; do not claim cleanup succeeded |
| Recovery daemon identity or endpoint cannot be verified | `stale_socket` or `ownership_mismatch` | Send no command to another daemon and keep marker and fence |

The credential crosses one process boundary in the `doubletake-ctl` argv and is readable to same-UID processes during that call. This matches the existing cooperative same-user boundary. `SensitiveText` is immutable, redacted, and non-serializable. Unwrap it only for validation and the single command invocation. Do not retain it in session state, persistence, callbacks, logs, diagnostics, crash records, test output, or recovery state.

The credential flow persists only its secret-free recovery marker and the identity fields above. If the host-transition boundary is reached, `reboot-required.json` is an operational handoff containing evidence and provenance and is also secret-free. Neither record contains a credential. A unique canary credential must be absent from every retained record and observer, including failure paths and assertion output. Receiver-derived pairing material remains under the receiver’s existing best-effort behavior. The dashboard never claims that the credential was remembered.

## 10. Required deterministic tests

Tests use controlled status samples, fake command adapters, barriers, a fake clock and fake process/registry evidence. The acceptance path must not spawn a production daemon.

1. **Capability proof** — `credential_relay` is true for both pinned tables and false for altered, malformed, incomplete, or contradictory help. Verify exact target-scope wording and its fail-closed cases. Verify `target_volume` requires the extended table, source tokens, and target-volume help token.
2. **Authorization separation** — the relay branch is the only added permission. The four transaction flags remain false on the probed profile. Test composed keyring proof independently from profile keyring, and require composed `owned_daemon_control` plus `keyring` for public Start.
3. **Command construction** — assert exact legacy and extended argv, original audio/volume
   preservation, credential final position and secret index, and empty/NUL/unencodable rejection.
   Update `relay_credential_representable(value)` and require its structural AST expectation to be
   exactly `return not value.startswith("-")`, while preserving the no-retention check. Its unit
   cases reject
   `-secret`, `--audio`, `--volume`, and `-`. Exercise the legacy and extended control profiles and
   prove each rejects the same leading-dash inputs. Through the dialog submit path, prove an
   invalid leading-dash value is rejected before constructing `SensitiveText` or issuing the actor
   command, so no marker is created. Prove the backend adapter repeats the refusal before child
   creation. Mutate dialog prevalidation and backend revalidation independently, including both
   command profiles, and require each bypass to fail its control. Also prove no targetless `pin` and
   redaction in every observer.
4. **Dispatch classification and fencing** — verify `ProcessPort.run_credential` returns `NotSpawned` only when no child exists, `Completed` only after process exit, and `Uncertain` after a dispatched timeout/cancellation. Verify the backend maps `CredentialDispatchFailure` no-spawn to `Err`, acknowledged positive and negative replies to `Submitted`, and uncertain outcomes to `NoReply`. Place a barrier in awaited ownership validation, replace the target and marker generation, and prove the final `dispatch_fence` prevents process creation. Mutate callback forwarding, callback timing, process-start classification, and generic-error conversion, with controls for each.
5. **Request detection and closed-union integration** — create a request only from the exact
   matching stream row. Aggregate-only prompt fields, another target’s prompt, absent rows, and
   ambiguous duplicate rows create none. A matching pre-submit row end emits the `credential_request`
   event with lifecycle `ended`. Exercise the event arriving before the Start effect returns a handle
   and after the handle is installed. In the first ordering, prove
   `accept_pending_credential_request` accepts the exact active Start and rejects changed receiver,
   target, operation, endpoint, session, trust, lease, daemon, or relay identity without setting
   `state.stream`. Construct `SubmitCredential`, `DismissCredentialPrompt`, all recovery commands,
   the lifecycle payload, submission and status completion records through the actual SessionManager
   queue. Prove submission and status completions are accepted while the owning Start remains
   pending and rejected after any owner field changes. Exercise `CredentialStatusCompletion.result`
   with both `Ok(StatusReply)` and sanitized `Err` values, including malformed status, before and
   after handle installation. Prove an admitted status error retains the marker and a stale status
   failure is inert. Mutate `_effect_current` to impose generic Start/effect operation-ID equality or
   require `state.stream` for credential effects, and prove a valid pending completion is rejected.
   Prove transactional `pairing_required`, `RelaySubmitCompletion` and `SubmitPairing` remain
   unchanged.
6. **Prompt lifecycle** — open for the owning receiver while another pane is selected, keep the same prompt across repeated samples, suppress reopening after local dismissal while the row remains a request, invalidate it on Start replacement, and keep trust state and transactional pairing unchanged. Exercise cancel before submit and cancel after dispatch.
7. **Recovery marker store** — exercise strict codec rejection, exact target-digest filenames,
   duplicate-target rejection, create-before-dispatch ordering, process-local target reservation
   before the durable create await, failed writes, and the post-rename case where exact
   next-generation bytes are visible but directory durability confirmation failed. Prove readback
   alone does not commit the transition, retry writes the same bytes and repeats all durability
   confirmation, and no cache, UI, or fence change occurs until that retry succeeds. Cover
   expected-generation conflicts, startup restoration before Start admission, global Start blocking
   for unreadable evidence, startup durability confirmation of every loaded record including
   retired tombstones, and global Start blocking when that confirmation fails. Cover release-pending
   transition, atomic dismissal with automatic checks disabled, matching UI action visibility,
   no-spawn retirement, terminal-reply retirement, daemon-replacement retirement, and idempotent
   tombstone generations. Simulate a post-rename error followed by process restart and prove a
   visible retirement is not trusted until `confirm_all_durable` succeeds. Mutate marker write
   ordering, readback-as-commit, skipped startup durability confirmation, marker clearing after
   absent/disconnected status, corrupt-store-as-empty handling, dismissal non-atomicity, and
   evidence checks, with a passing control for each.
8. **Reporting** — cover every row in §7.2. Prove command reply alone is not success, a complete negative reply proceeds to status polling, later streaming after prompt closure is not retro-reported, and no result diagnoses a cause.
9. **Canary** — verify a unique credential never appears in diagnostics, session snapshots, recovery markers, journals, exports, process records, callback representations, assertion output, or retained observations.
10. **Acceptance** — operator Start, matching request, prompt, submit, status observation, and
    report through the assembled runtime without a production spawn. Use a two-target fake daemon
    where another target already has a PIN request, so the successful connect acknowledgement for
    this Start has aggregate `pin_required`. Prove the new target enters target-row polling, its prompt
    comes only from its own exact row, and the other target’s credential state does not fail the Start
    or acquire its prompt. Also prove a request can reach the queue before the Start effect returns
    its stream handle and that the Start remains the owner throughout. Hold a matching `pin_required`
    row live without submitting a credential, issue ordinary Stop, and prove `stop_pending_start`
    transfers the exact handle into ordered target-specific cleanup. The matching row disappears,
    while a second target remains in `pin_required` and the disconnect acknowledgement reports
    aggregate `pin_required`. Prove exact-row status confirms the Stop within the existing cleanup
    bound, no credential command is sent, and no Start handle is published to the snapshot. Mutate
    the stop-signal check, single-owner transfer, and exact-row verification independently, with a
    passing control for each. Also acknowledge this target’s credential, hold its receiver row in
    `connecting` while the `transport_start` acquisition remains pending and `state.stream` remains
    absent, issue Stop, and prove the dispatched-submission form of `stop_pending_start` signals the
    readiness exit and transfers the exact handle once. The original command deadline and any
    required post-reply status observation remain in force, no second credential command is sent,
    ordered cleanup is bounded, and no Start handle is published to the snapshot. Mutate the
    post-dispatch handoff, readiness-signal, single-owner transfer, and exact-row verification
    independently, with a passing control for each.
11. **Recovery ordering and races** — prove Stop racing a complete reply and Stop racing `NoReply`.
    In each case let Stop supersede the Start before delivering the credential completion and prove
    the exact `CredentialSubmissionSettlement` consumes it without publishing a prompt report. Cover
    a complete reply followed by bounded status observation and ordered target cleanup, and a
    `NoReply` that durably enters `release_pending` before cleanup. Include a dispatched submission whose receiver acknowledgement changes the target to
    `connecting` while the Start acquisition still owns the pending handle. Issue Stop before the
    snapshot handle is installed and prove the settlement retains its original command deadline,
    the pending-start readiness exit and exact-handle transfer happen once, and cleanup does not
    wait for the resumed Start readiness deadline. Deliver an owner-correlated status `Err` after Stop
    supersedes Start and prove cleanup continues
    while the marker stays fenced. Repeat after local prompt
    dismissal and deliver stale or duplicate callbacks after settlement retirement. Also test no-reply
    followed by a late target after initial cleanup and after the normal observation window, late
    disconnect effects, a second Stop after handle retirement, repeated and failed cleanup, an absent
    or disconnected sample must not clear the warning or fence, and persistence failures.
12. **Restart and release** — matching and mismatched daemon identity on restart; historical
    termination proof after `reconcile_previous` has cleared the in-memory registry record; old
    socket client isolation from replacement; failed endpoint-separation proof; exact old-daemon
    termination evidence; registry readiness ordering; unique private endpoint allocation and
    non-reuse; restart-attempt recovery after application interruption before reservation, during
    partial launch, after old termination, after replacement readiness receipt publication, and
    before marker retirement; `recovery_attempt_id` matching in marker, launch receipt, and readiness
    evidence; shared-daemon restart cost warning; restoration of watching and dismissed
    release-pending markers before ordinary registry reconciliation; dismissal retaining the
    same-target fence; and marker retirement only after replacement readiness is durably confirmed.
    Prove `termination_proof` does not re-acquire the lifecycle lock when invoked inside
    `restart_with_new_endpoint`, while external calls still take the lock. Mutate fallback to
    ordinary `restart()`, reused paths, nested lock acquisition, lost historical identity, startup
    reconciliation before marker restoration, and premature marker retirement, with a passing
    control for each.
13. **Callback fencing** — retire a recovery operation, start a newer one, then deliver the old status, disconnect, and restart completions. Each old completion must be inert.

Required mutation probes remove the matching-target check, remove credential representability validation, hard-code the secret index, skip redaction in one observer, report success without the follow-up status read, write the marker after dispatch, map an uncertain dispatch to `Err`, drop `dispatch_fence` forwarding, clear a release-pending marker after an absent/disconnected sample, omit a closed-union branch, and publish a stale relay completion. Each probe has a passing control and identifies the assertion that detects the mutation.

## 11. Documentation updates required for delivery

These are implementation deliverables named by the approved design. This task’s artifact remains this specification alone.

### `README.md`

Use wording equivalent to: “During an operator-started Mirror, a proven `credential_relay` profile can present a PIN/password prompt for the started target. The application reports success only when that target’s status reports `streaming`.” Do not say the application stores the value, that a command response establishes streaming, or that status identifies why streaming was not reached.

### `docs/backend-compatibility.md`

Document the target-bound `connect <target> [--audio on|off] [--volume N] <credential>` mapping, with the legacy form and exact final argv secret index. Identify both pinned help profiles that prove `credential_relay`. State the `EvidenceAuthority` receipt-refresh and endpoint requirements, and that targetless `pin` is unused.

Keep the probed profile’s four transaction flags false. Explain that runtime `keyring` comes from the independent three-leg credential-service proof and public Start admission requires composed `owned_daemon_control` and `keyring`. Name the composed capability report, transport and authority features, and bound command profile as inputs to the admission predicate. Keep transactional and remembered pairing disabled for this relay. Explain that profile `keyring == false` does not negate composed Start keyring, and that profile `target_volume` is a separate proof requiring the extended surface, source tokens, and target-volume token.

### `docs/operations.md`

Document the §2 limits and the recovery contract in §§8.1–8.4, including address continuity, unknown outcomes, Stop’s inability to atomically cancel an in-flight command, delayed connections, repeated target-specific Stop, dismissal retaining the Start fence, and the shared-daemon impact of `Restart receiver to release`. Do not promise monitoring while the application is stopped.

## 12. Integration contracts

Everything in this section fixes implementation boundaries. It adds no user-visible promise.

### 12.0 Existing repository seams

Use these existing seams as the implementation boundary:

| Contract | Existing location | Required extension |
| --- | --- | --- |
| Recovery marker and store | `src/airplay_linux/domain/records.py`, `domain/ports.py`, `storage/codecs.py`, `storage/repositories.py` | Define marker/release-evidence codecs, generation-checked atomic transitions, `CredentialRecoveryMarkerStore`, `JsonCredentialRecoveryMarkerStore`, `confirm_all_durable`, and fail-closed corruption, retry, and startup-confirmation behavior from §8.1 |
| Target recovery coordinator | `src/airplay_linux/runtime.py`, `session/recovery.py`, `session/engine.py::ManagerDeps` | Load markers before registry reconciliation; maintain target-keyed fences; recover exact interrupted attempts; pass revalidated watching-daemon exemptions to ordinary reconciliation; serialize marker creation against Start admission |
| Process child admission | `src/airplay_linux/domain/ports.py::ProcessPort`, `src/airplay_linux/process/linux.py::LinuxProcessRunner` | Add `run_credential` with the three `CredentialProcessResult` variants and synchronous fence check immediately before child creation |
| Backend dispatch | `src/airplay_linux/backend/contract.py::CredentialDispatchFailure`, `src/airplay_linux/backend/control.py::CredentialRelayControl`, `PublishedDoubletakeControl` | Add typed relay reply and error classification; require and forward relay fencing while preserving generic and transactional callers |
| Relay request and actor-domain records | `src/airplay_linux/domain/relay.py`, `src/airplay_linux/domain/records.py` | Define the Start-bound `CredentialRequest`, actor-local `CredentialPromptState`, `CredentialSubmissionSettlement`, `PendingStartCleanupOwner`, `Submitted`, `NoReply`, and `NoReplyReason`; make `relay_credential_representable(value)` reject leading dashes without a profile argument; call it from `PairingDialog` before creating `SensitiveText` or `SubmitCredential`, and repeat validation in the backend before command dispatch; update its no-retention structural contract in `tests/domain/test_credential_relay.py`; do not use the retired relay lease or phase-controller contract |
| Transport operations | `src/airplay_linux/domain/ports.py::TransportPort`, `src/airplay_linux/transport/doubletake.py::DoubleTakeTransport` | Add target-bound submission, owner-correlated status reads, and a `stop_pending_start` handle transfer for both pre-submit and dispatched-submission pending Starts; bind the optional submission ID to the pending stream lease; signal readiness exit without cancelling the credential command; verify target stops by exact status row rather than aggregate acknowledgement state; forward `dispatch_fence` unchanged; preserve marker-before-adapter ordering |
| Session and effect completion | `src/airplay_linux/session/engine.py`, `src/airplay_linux/session/fencing.py`, `src/airplay_linux/session/mirror.py::_transport_event`, `src/airplay_linux/session/cleanup.py` | Route commands through existing per-receiver queues; add pending-Start event, prompt-command and cleanup-handoff gates; capture `CredentialEffectOwner` and `CredentialSubmissionSettlement`; wrap status `Result` in its typed completion; validate owner generations after Stop supersedes Start |
| Prompt and recovery projection | `src/airplay_linux/domain/records.py::SessionSnapshot`, `session/persistence.py::_safe_snapshot`, `storage/codecs.py`, `src/airplay_linux/ui/state.py::PaneState`, `ui/presenter.py`, `ui/dialogs.py::PairingDialog` | Add transient prompt and recovery views/actions; omit prompt from safe snapshots and `SESSION_SCHEMA`; keep transactional dialog route separate |
| Endpoint-changing release | `src/airplay_linux/process/daemon_registry.py::DaemonRegistry`, `src/airplay_linux/session/recovery.py` | Add idempotent `restart_with_new_endpoint` bound to attempt ID, exact old-process termination evidence, historical launch-receipt resolution, lock-safe `_termination_proof_locked`, attempt-correlated launch receipts, unique private runtime/socket, and verified replacement readiness |
| Tests and delivery docs | existing backend, domain, storage, session, UI, transport and UI acceptance test targets; README and docs | Add the controls and mutations in §10 and update documentation in §11 after implementation |

Implement in dependency order. First add marker records, strict codecs/store, coordinator startup loading and target fencing. Next add the credential process result and backend dispatch classification with final fencing. Then add transport request/submission/status methods and the `credential_request` lifecycle event. Extend actor closed unions and effect completion after those producers exist. Add the transient prompt/recovery projections, relay dialog mode and recovery commands after session routing. Implement the endpoint-changing registry release operation before assembled-runtime recovery acceptance. Documentation updates follow verified behavior.

Preserve `RelaySubmitCompletion.result: PairingResult`, `PairingFlow`, `PairingPort`, `pairing_required`, `ChallengePayload`, and the existing `SubmitPairing` branch for transactional pairing. The new relay completion is the value in the existing `EffectEvent.result`, not a `TransportPayload` or new `ResourceEvent`. The request event is the sole new `TransportEventKind` and carries lifecycle `live | ended`.

### 12.1 Publishing the request

The existing connecting-stream wait publishes `CredentialRequest` only for the entry whose target
equals the active Start target. It records the request on that stream and continues the same wait.
Aggregate status fields never create a request, and another target’s prompt is ignored. The pending
Start remains the owner of the wait. No second Start, lease, or reservation is created.

The successful `connect` acknowledgement is not a per-target state report. If its aggregate state is
`pin_required`, continue into target-row polling rather than failing or opening a prompt from that
value. A matching row can then produce the request for this Start, while a different target’s row
cannot. The request event carries the pending handle and Start identity before `start_stream`
returns. `SessionManager._admit` applies `accept_pending_credential_request` while `state.stream` is
absent. The Mirror lifecycle records only the prompt and its owner identity, leaving the Start
operation, lease, transport state, and acquisition pending. When `transport_start` later returns, the
ordinary acquisition gate admits the handle only if its Start ID, endpoint, session, and lease still
match. The event may also arrive after that handle was installed, in which case ordinary
`accept_transport` applies. If either event ordering races with Start replacement, the stale request
is discarded.

### 12.2 Suspending and resuming deadlines

While the matching request is live, suspend the Start’s permission/readiness budget and record its
offset. Resume it when submit is attempted, the entry changes, or the prompt is cancelled. Shift the
deadline by the elapsed pause. An ordinary Stop overrides this suspension by calling
`stop_pending_start` with the saved Start owner and current Stop context. Before submission, this
cancels the exact read-only status poll without issuing a credential. After submission, the owner
also carries the settlement’s submission ID and marker generation. The dispatched-submission form
signals the same readiness exit even if the target row is already `connecting`, without cancelling
the credential command or starting another command timeout. Both forms transfer the exact handle
once and enter ordered cleanup without waiting for the resumed readiness budget. After a complete
command reply, measure `CONNECT_STREAMING_TIMEOUT` from that reply using the existing status read.
Keep that report wait independent of the resumed Start budget and preserve its marker transition
before cleanup. On report expiry, close the prompt and allow the pending Start to continue to its
own deadline unless Stop has superseded it. Stop remains available.

### 12.3 Submitting

Publish the existing `ChallengeView` type for a live request without touching transactional pairing. The actor’s distinct `SubmitCredential` command resolves that view to the current Start and calls `submit_credential(target, value) -> Result[Submitted | NoReply]`. Use the exact recognized target-first profile. The credential is final and secret-indexed. Local pre-spawn failures are `Err`; complete positive and negative replies are `Submitted`; only a dispatched command with no complete decoded reply is `NoReply`.

Cancellation closes the prompt and sends no additional receiver command. It does not cancel a command already dispatched or discard its actor-local settlement. Unknown-outcome recovery has a lifetime independent of the prompt. If Stop supersedes an active Start, the captured submission settlement handles completion after
supersession and suppresses reporting while preserving ordered cleanup. If the Start acquisition is
still pending, Stop also captures its `PendingStartCleanupOwner` and uses the dispatched-submission
form of `stop_pending_start`; this handoff does not cancel the command or its status observation.

### 12.4 Accepted fall-through

If the waiting entry disappears between the status read and command, the receiver may use the credential for a new connection. The design accepts this behavior under target-address continuity. The application still reports only what it observes afterwards and makes no duplicate-submission guarantee.

### 12.5 Presentation limit

The prompt opens for the receiver whose Start produced the request, regardless of selected pane. Selection changes do not retarget it. Cancel dismisses the prompt and is not a transactional pairing cancellation. The same target’s successive same-kind waits cannot be distinguished because status exposes no request identifier.

### 12.6 Input validation

Validate empty, NUL-containing, platform-unencodable, or leading-dash input in the `PairingDialog`
submit handler before constructing `SensitiveText` or issuing `SubmitCredential`. The handler uses the
profile-independent `relay_credential_representable(value)` predicate. The backend repeats
validation after revealing the value at the backend boundary and before command construction and
process spawn. Update the helper's unit cases and structural AST expectation in
`tests/domain/test_credential_relay.py`, and verify both legacy and extended control profiles reject
the same leading-dash input. Return fixed, secret-free invalid-input feedback and create no actor
command, marker, effect, or process for rejected input. Add no PIN-format restriction.

### 12.7 Wording

“Connected” means exactly that the receiver reports this target streaming. It does not mean frames are visible or that the credential was remembered. A non-streaming result names no cause. A later streaming status after the prompt closes is not retro-reported.

### 12.8 Shutdown and deferred findings

Do not add a `ShuttingDown` result or parse free-form shutdown text. If shutdown yields no complete command reply, classify it as `NoReply`, report “No complete command reply was received; the outcome is unknown.”, and retain the recovery marker. A complete negative reply remains `Submitted` and proceeds to the bounded status observation. Do not copy receiver text into a diagnostic.

The request event, owner-correlated status completion, `stop_pending_start` handoff, local prompt dismissal signal, and `SubmitCredential` actor command are the explicit integration seams required to connect the approved flow to the current transport and UI boundaries. They do not add receiver protocol state or transactional pairing behavior.
