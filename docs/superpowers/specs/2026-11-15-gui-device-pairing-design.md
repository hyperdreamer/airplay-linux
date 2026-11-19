# Device pairing from the dashboard — minimal design

**Status:** design, cleanup-recovery pivot (epoch 6)
**Run:** `pm-run-20261115-031248-7c91beef`
**Supersedes:** every earlier section of this document. The elaborate relay design — leases,
phase controller, settlement arbitration, ordered views, exactly-once trust commit, release
state machine and termination proof — was withdrawn after seven review cycles. Its full text
remains in history; this document is the contract.

## 1. Scope

Mirror mode only. When the receiver that the operator started is waiting for a PIN or password,
the dashboard shows a field, sends the credential to the receiver tool, and reports what the
status says afterwards.

Delivered: **answer the PIN, and report what the receiver actually did.** The only
success claim is that the receiver reports the named target in `streaming`; a terminal
non-streaming observation says the target was not observed `streaming`. If no complete command
reply is decoded, the outcome is unknown.

## 2. What this design does not promise

These are stated here because earlier drafts promised them and could not keep the promise:

- **Not exactly-once trust.** The application records no trust. The receiver tool may best-effort
  persist derived pairing material; it does not store the submitted PIN or password itself. A
  save failure is only logged, so the dashboard reports what it observes and never claims the
  credential was remembered.
- **Not session continuity.** The receiver routes a credential by the target's address. If the
  device at that address has been replaced since the start, the receiver cannot tell, and
  neither can this application. The design therefore promises *target-addressed delivery under
  address continuity*, and nothing stronger.
- **Not a duplicate-submission guarantee.** The dashboard disables its own field while a
  submission is in flight, which prevents the ordinary double-click. It cannot prove that a
  second command never reaches the receiver.
- **Not a diagnosis.** A terminal non-streaming observation is reported as "not observed
  streaming"; a missing complete command reply is reported as "outcome unknown". Neither tells
  whether the credential was wrong, the receiver timed out, or the connection dropped.
- **Stop and an in-flight PIN command.** The receiver cannot atomically cancel a credential
  command already in flight. Stop waits up to `CONTROL_TIMEOUT` for its local result and runs
  target-specific cleanup. When no complete reply establishes the command's outcome, the
  application retains a visible, target-specific cleanup action and watches for a late connection
  as described in §6.6. An observed and disconnected connection does not prove that a different
  late effect is impossible. Stop is not atomic cancellation, and cleanup cannot prove that an
  unresolved command will never take effect. This limitation is stated in `docs/operations.md`.

Each of these is recorded in `docs/operations.md` as an operator-visible limit.

## 3. The one path

```
operator starts a mirror to receiver T
      │
      ▼
transport reads status ──► T's own stream entry says pin_required (kind: pin|password)
      │
      ▼
dashboard opens the prompt for T          (existing dialog, existing projection)
      │
      ├── before submit, request ends ──► close prompt; Start remains pending; no PIN result
      │
      ▼
operator types the credential ──► submit
      │
      ▼
client checks the value is representable as a command argument
      │
      ▼
transport sends the profile-selected target-first form, `connect T <credential>` on the exact
legacy signature or `connect T [--audio on|off] [--volume N] <credential>` on the exact extended
signature
      │
      ▼
await the reply (bounded), then poll status until streaming or the bound expires
      │
      ├── T is observed streaming  → report what the receiver reports, then close the prompt
      ├── no decoded command reply → report "No complete command reply was received; the
      │                               outcome is unknown.", close the prompt
      ├── after submit, T leaves pin_required but is still present → keep polling; no result yet
      └── wait expires or T absent → report "The target was not observed streaming.", close
```

There is no new receiver-side lease, reservation or settlement protocol. The secret-free local
recovery record in §6.6 does not establish ownership of a receiver-side connection.

## 4. What is reused unchanged

| Component | From | Why it still applies |
| --- | --- | --- |
| `BackendFeatures.credential_relay` and its derivation | T-002 | Proves the target-bound credential command exists in the pinned surface |
| `ProfileCommand` / `CommandSpec.secret_indices` and redacted projections | T-002, T-005 | The credential must never reach a journal, record or diagnostic |
| `EvidenceAuthority.allows(submit_pairing)` | T-002 | One authorization branch, four transaction flags still false |
| Observer redaction (`redact`, `FakeProcess`, `ProbeProcessRecorder`, acceptance recorder) | T-005 work, **not yet integrated** | Retained observations never contain the credential. This is the quarantined core lane's commit; the review of it found three retention paths and one leaky assertion. It must be finished and integrated as part of this delivery, not assumed present. |
| `PairingDialog`, projection, presenter prompt plumbing | existing `ui/` | The dialog already exists, wipes secrets on exit and renders offered actions |

## 5. What is dropped

`RelayLease`, `RelayPhaseController`, `CredentialDispatch`, `ReleaseEvidence`,
`TerminationProof`, the relay `can_submit` predicate with its ten conjuncts, the provisional
success record, the settlement evidence set, `view_seq` and the refusal taxonomy table.

T-003's historical relay-termination proof is already integrated and stays in the codebase as a
reviewed transport capability. **It is not used by this feature.** §6.6 uses the separate existing
owned-daemon lifecycle evidence to prove that the exact receiver process and its endpoint have
been retired before a replacement can release the target fence.

## 6. Behaviour

### 6.1 Detecting the request

A request exists when the `status` reply contains a stream entry whose `target` matches the
start's target and whose `state` is `pin_required`. Aggregate fields are ignored: they can
describe a different receiver.

The application does not start a stream to discover this. Nothing starts a stream except an
operator Start or the reconnect of that Start.

### 6.2 Presenting

The prompt opens for the receiver whose start produced the request, regardless of which pane is
selected. It shows the credential kind ("Enter the PIN shown on that TV." / the existing
password wording). One prompt per request; a repeated status does not reopen it.

### 6.3 Submitting

The application refuses a leading-dash credential as a conservative input rule shared across both
accepted profiles. The pinned legacy CLI would treat that argument positionally, while the
extended parser reserves dash-prefixed operands for its options. Refusing it before dispatch keeps
the application's validation consistent without claiming that the legacy parser rejects the value.

The command is target-first and selected only from the exact control signature already recognized
by the backend capability decoder. The legacy signature sends `connect <target> <credential>`
with no options. The extended target-volume signature may send
`connect <target> [--audio on|off] [--volume <n>] <credential>` in the recognized option order.
The application never infers the extended parser from a version string or an unrelated token.
When the recognized surface cannot carry the original Start's audio or initial-volume choices,
the existing capability rules refuse that pairing command rather than silently changing the
requested stream. The targetless `pin` command is never used.

### 6.4 Reporting

After the reply, status is polled until this target reports `streaming` or the bound expires;
the first of those two decides the message:

| Observation | Message | Prompt |
| --- | --- | --- |
| Before submit, the matching request row ends | No PIN result | Close prompt; resume Start |
| After submit, this target leaves `pin_required` but remains present | No result yet | Keep polling; keep prompt read-only |
| this target is reported `streaming` within the wait | "Connected to <TV>." (the receiver reports a streaming session) | closed |
| this target is absent after submission, or is not `streaming` when the wait expires | "The target was not observed streaming." | closed |
| no complete decoded command reply, including a service shutdown | "No complete command reply was received; the outcome is unknown." | closed |

**Why only these two outcomes.** The receiver acknowledges a credential before it authenticates
and removes the waiting stream when pairing fails, so "no longer waiting" is not proof of
success and a rejection cannot be told apart from a timeout by the application. `streaming` is
the one success claim this design is entitled to make: the receiver reports the named target in
its `streaming` state. This does not prove that any frame was sent, received or displayed.
Everything else is reported as an observation, with no invented cause. A missing row or a
non-streaming sample does not prove that streaming never occurred between samples.

**No same-prompt retry.** A refused credential does not leave the waiting request open, so the
prompt closes and the operator starts again. The dashboard does not pretend a retry is possible.

The dashboard reports; it does not adjudicate. There is no attempt counter beyond the
receiver's own policy. A new attempt requires a new Start; the receiver does not keep the
request open for a retry.
The dialog's existing "Cancel" closes the prompt and sends no additional command. Before
submission this leaves the receiver as it was; after submission it does not cancel a command
already dispatched, which may still take effect. Its reporting is dismissed, not its effects.

### 6.5 Bounded waits

Two pre-existing bounds apply at different points. `CONTROL_TIMEOUT` bounds the command call and
starts when the operator submits. If a complete command reply arrives, `CONNECT_STREAMING_TIMEOUT`
bounds the following status wait and starts at that decoded reply; the command's response time does
not consume that window. A missing complete reply has the unknown outcome in §6.4 and enters the
recovery path in §6.6, without starting a reply-based streaming wait. A person typing is not
subject to either bound. Recovery watching in §6.6 uses a separate periodic status read while the
warning remains.

### 6.6 Cleanup when a submitted command has an unknown outcome

Before dispatching a credential, the application durably records a secret-free recovery marker
for the receiver, exact target, start operation and owned daemon instance. The marker also pins
the daemon endpoint generation and private socket path so a later recovery cannot silently retarget
a replacement instance. No PIN, password or credential-bearing command is recorded. If that write
fails, the credential is not dispatched. An interruption after the write but before dispatch may
therefore leave a conservative warning. A marker is retired after a complete command outcome only after the resulting status path reaches
its terminal handling and any concurrent Stop's target-specific cleanup has completed. If Stop
overlapped the credential command and no complete reply arrives within `CONTROL_TIMEOUT`, the
marker becomes release-pending and neither a later disconnect reply nor an absent status sample
can clear it.

If a dispatched credential command has no complete local reply, or the application stops while
its outcome is still unknown, the marker remains. It is not cleared merely because the target
was absent, an observation window elapsed, or a target was seen and disconnected. If the operator
presses Stop while a command is still unresolved and that command produces no complete reply, the
marker becomes release-pending even if the later status read shows the target absent. The dashboard
shows a target-specific "Cleanup pending" warning with a Stop action and an explicit action to
dismiss the warning. This remains available after ordinary stream cleanup and across application
restarts until dismissed. The warning says that a delayed connection may still appear, that Stop
may need to be used again, and that dismissal stops automatic status checks while leaving the
target blocked. A complete local reply received before uncertainty is established permits the
marker to be retired only after any concurrent Stop's target-specific cleanup has completed. Once
the warning has been published, only explicit dismissal changes its recovery state. The
specification must define and test these transitions and their ordering against Stop, dispatch,
timeout and persistence.

While the warning is retained and the application is running, it periodically reads the original
owned daemon's status. If exactly the recorded target is present, it attempts a target-specific
disconnect. Stop explicitly requests another such observation and cleanup attempt, including
after the original stream handle has been retired; an idle-state no-op is not sufficient. A
failed status read, failed disconnect, ambiguous target, or unverified daemon identity keeps the
warning visible and reports only the failed observation or cleanup. It never targets all streams.
On restart the app restores the marker and resumes observation only when it can verify the same
owned daemon instance; otherwise it retains the warning without sending a command to a different
daemon. Background watching makes no claim about time when the app is not running.

Dismissing the warning changes the durable marker to "release pending" and stops automatic status
checks. It does not remove the target fence. A Start for that target remains unavailable until the
app proves that the exact original daemon process has terminated using the existing receipt-backed
termination evidence, and then verifies readiness of a distinct replacement daemon instance on a
distinct private control-socket path. The normal in-app restart path may reuse the configured
socket path when no previous supervisor-release record exists, so that path alone cannot release
the fence. The recovery restart must allocate and durably record a fresh private runtime directory
and socket path distinct from the original even when initiated within the same running app. Every
control command uses the explicit socket path from its daemon record, which keeps an old client
bound to the retired endpoint. The release check is bound to the original daemon instance and
endpoint generation, not a PID alone. If termination, endpoint separation, or replacement
readiness cannot be proved, the target stays blocked and the marker remains. The recovery restart
interrupts all mirrors owned by the shared daemon and must be presented as such before it starts.
Only after the replacement is verified is the marker retired and same-target Start re-enabled.

The dismissed-warning fence persists across application restarts. A later app instance restores
it and either proves the old daemon already terminated and verifies a distinct replacement, or
keeps the target blocked until the recovery-specific endpoint-changing restart completes. The
blocked-target view retains an explicit "Restart receiver to release" action and states that it
disconnects every mirror owned by the shared daemon. Starting that action requires the operator's
direct action on that clearly labelled control. Dismissal cannot authorize a late command to target
a newer same-address stream. Local callbacks from a retired recovery operation cannot issue
another disconnect or change a newer session. A disconnect already sent cannot be recalled and
may itself take effect late against the original daemon, so all starts for the target remain
fenced until the old daemon is gone. Like credential submission, recovery identifies a stream by
target address, not by receiver request identity: an unrelated connection at the same address
cannot be distinguished under the accepted continuity assumption.

## 7. Failure handling

| Condition | Behaviour |
| --- | --- |
| status cannot be read | the existing transport failure path; while recovery is pending, retain its warning and Stop action |
| before submit, a newer status no longer shows the matching request | prompt closes; Start resumes; no PIN result is reported |
| after submit, the matching row changes from `pin_required` to another present state | keep polling until `streaming`, target absence, or deadline |
| the operator cancels | prompt closes; no additional command is sent; a submitted command may still take effect |
| the credential is refused (the receiver removes the stream) | "The target was not observed streaming.", prompt closed |
| no complete command reply is decoded, including a service shutdown | "No complete command reply was received; the outcome is unknown."; retain the recovery marker |
| a recovery observation or disconnect fails | retain the warning and Stop action; do not claim cleanup succeeded |
| the operator dismisses the recovery warning | stop automatic status checks, persist release-pending marker, and keep same-target Start blocked |
| the operator selects "Restart receiver to release" | warn that all mirrors owned by the shared daemon will stop; use a recovery-specific restart that guarantees a distinct private endpoint |
| original daemon termination and distinct replacement endpoint are verified | retire the release-pending marker and permit same-target Start |
| daemon termination or replacement identity cannot be verified | retain the release-pending marker and keep same-target Start blocked |
| the app is closed mid-prompt | existing shutdown path; no credential retained anywhere |

## 8. Security

The credential crosses one process boundary in the argv of `doubletake-ctl`, readable by a
same-UID process during that call. That matches the existing cooperative same-user boundary: any
such process can drive the daemon socket directly. The application stores no credential. It
persists only the secret-free recovery marker and its target, operation, daemon-instance and
endpoint-generation identity. The marker cannot contain a PIN, password, command argv or
credential-bearing result. Canary tests prove no credential is retained in a diagnostic, record,
journal, export or recovery marker.

The address-continuity limit of §2 is the honest statement of what targeting can and cannot
prove.

## 9. Compatibility

The installed four-token pair works unchanged; no rebuild, no new capability token beyond
`credential_relay`, which is derived from the pinned command surface. Profiles that prove the
four transaction capabilities keep their existing transactional pairing path untouched.

Documentation delivered with the change: `docs/backend-compatibility.md` and `README.md` lose the
"cannot service pairing" claim; `docs/operations.md` describes the limits in §2 and §6.6.

## 10. Tests

1. **Capability** — `credential_relay` true for both pinned tables, false otherwise.
2. **Authorization** — the relay branch is the only new permission; four transaction flags false.
3. **Command** — exact argv for the recognized legacy and extended signatures; include audio and
   volume flags only for the positively recognized extended target-volume surface; verify the
   credential is final and secret-indexed; option-shaped input is refused before dispatch.
4. **Request detection** — a request is derived only from the matching stream entry; another
   receiver's prompt produces nothing.
5. **Prompt lifecycle** — opens for the started receiver while another pane is selected; does
   not reopen for the same request; cancel sends no additional command and cannot cancel an
   already dispatched command.
6. **Reporting** — one test per row of §6.4, driven by the fake daemon.
7. **Canary** — a unique credential value appears in no diagnostic, record, journal, export or
   retained observation, and no failing assertion prints it.
8. **Acceptance** — start → request → prompt → submit → report, with no production spawn.
9. **Unknown-outcome recovery** — prove durable marker-before-dispatch ordering and failed-write
   refusal; Stop racing a reply and a no-reply command; late target after initial cleanup and
   after the normal observation window; second Stop after handle retirement; repeated and failed
   cleanup; restart with matching and mismatched daemon identity; dismissal retaining the
   same-target Start fence; shared-daemon restart warning and action; release only after exact
   old-daemon termination and a distinct verified socket endpoint; stale command isolation from
   replacement daemon; restart with a pending release marker; and stale-callback fencing. A
   disconnected observation must not erase the warning or release fence.

Mutation probes: drop the target-equality check on request detection; drop the representability
check; hard-code the secret index; skip redaction in one observer; report "accepted" without the
follow-up status read.

## 11. Risks

1. **Address reuse** — accepted and documented (§2). Revalidation immediately before the command
   narrows the window; it cannot close it.
2. **Duplicate submission** — the dashboard disables its field while a submission is in flight;
   a second command from elsewhere is the receiver's problem, not this application's.
3. **Receiver lockout after repeated wrong PINs** — a wrong value ends that attempt; the operator
   starts again.
   The dashboard does not retry by itself and never submits without an operator action.

## 12. Route B integration contract

This section fixes the boundaries the reviews kept finding unowned. Everything here is mechanism;
no new promise is made.

### 12.1 Publishing the request (transport)

`_await_connecting_stream` currently fails with `dependency_incompatible` when it sees a prompt.
It instead publishes a `CredentialRequest(target, kind)` for the entry **whose target matches the
stream**, records it on the stream, and keeps waiting. Aggregate status fields never create a
request; another target's prompt is ignored.

The pending start remains the owner of the wait. The request does not create a second start, a
lease or a reservation.

### 12.2 Suspending and resuming the deadlines (F-08)

While a request is live for this stream, the start's readiness budget is **suspended**: the
transport stops counting it and records the offset. It resumes when the request leaves the live
state (a submission was attempted, the entry changed, or the prompt was cancelled), and the
remaining budget is shifted by the elapsed pause. A person typing is therefore never inside a
start deadline.

After a submission is attempted, a **bounded streaming wait** begins: `CONNECT_STREAMING_TIMEOUT`
measured from the command's reply, polled with the existing status read. That wait is independent
of the suspended start budget. On expiry the prompt closes and the pending start continues to its
own resumed deadline; the operator may Stop.

### 12.3 Submitting (session and transport)

The session publishes the existing `ChallengeView` for a live request without touching the
transactional coordinator. On submit it calls one transport operation:

```
submit_credential(target, value) -> Result[Submitted | NoReply]
```

which builds the T-002 target-first command from the exact recognized profile: legacy
`connect <target> <credential>`, or the extended target-volume form
`connect <target> [--audio on|off] [--volume N] <credential>`. The credential index is declared
as secret. An unrecognized or mismatched command surface refuses before dispatch. The operation
runs once and reports only which of three sanitized things happened; it does not interpret the
receiver's error text and does not carry a credential in any result.

The session then performs the bounded wait of §12.2 and derives the §6.4 message from the
observed state. Cancellation closes the prompt and sends no additional command; it does not
cancel an already dispatched command. The unknown-outcome recovery in §6.6 is independent of
that prompt's lifetime.

### 12.4 The fall-through is accepted (F-03)

If the waiting entry disappears between the status read and the command, the receiver may start a
new connection using the credential. That is the connection the operator asked for; the design
accepts it and does not claim otherwise. The application still reports only what it observes
afterwards, and §2 already states that a duplicate command cannot be prevented.

### 12.5 Presentation (F-06, F-07)

The prompt opens for the receiver whose start produced the request, regardless of which pane is
selected; selection changes do not retarget an open prompt. The dialog disables its field while a
submission is in flight and its Cancel dismisses the prompt (it is not a transactional pairing
cancel). Prompt identity is the pair `(receiver, start operation)` plus the first observed
credential kind; a repeated status cannot reopen it, and an old prompt is invalidated when the
local start is replaced.

**Documented limit:** the status exposes no request identifier, so two successive waits of the
same kind for the same target are indistinguishable. The design does not promise to tell them
apart.

### 12.6 Input validation (F-12)

A value is refused before dispatch when it is empty, contains a NUL or any character the
platform cannot encode as an argv element, or begins with `-`. A leading-dash value is refused before dispatch so the extended profile's option parser cannot
consume it as a flag. This conservative input rule applies to both accepted profiles; it does not
claim that the legacy parser rejects unknown options. Refusal is a fixed, secret-free invalid-input
result and sends no command.

### 12.7 Wording (F-10, F-13)

"Connected" means exactly: *the receiver reports this target streaming*. It does not mean frames
are visible, and it does not mean the credential was remembered. A failure says only that
streaming was not observed; it names no cause, because the observations cannot establish one.
A later status that reaches streaming after the prompt closed is not retro-reported.

### 12.8 Deferred to the specification (F-05, F-08, F-09)

Three findings are specification-level surface, not design decisions. They are recorded so
Step 4 defines them explicitly instead of leaving them to an implementer:

1. **A transport event carrying the request.** `TransportPort` has no event that carries a
   `CredentialRequest`, and the existing pairing event enters the transactional coordinator. The
   specification must add one event type, name it, and route it to the non-transactional path.
2. **A session-facing status read and a submission operation.** `TransportPort` exposes neither.
   The specification must add `submit_credential(target, value) -> Result[Submitted | NoReply]`
   (local errors are `Err`; only receiver observations are `Submitted` or `NoReply`) and a
   status read the session can poll during the bounded wait, and must
   enumerate which existing transport deadline each starts, suspends or leaves alone.
3. **Shutdown classification.** The typed acknowledgement path discards the receiver's free-form
   shutdown text, so Route B does not add a `ShuttingDown` outcome or parse that text. If shutdown
   yields no complete command reply it is `NoReply` and is reported as "No complete command reply
   was received; the outcome is unknown." A complete negative command reply remains `Submitted`
   and is followed by the bounded status observation. Copying receiver text into a diagnostic is
   not acceptable.
