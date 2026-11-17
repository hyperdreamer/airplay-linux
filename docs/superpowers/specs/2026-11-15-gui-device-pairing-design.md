# Device pairing from the dashboard — minimal design

**Status:** design, scope reset (epoch 2)
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
success claim is that the mirror reached `streaming`; the only failure claim is that it
did not.

## 2. What this design does not promise

These are stated here because earlier drafts promised them and could not keep the promise:

- **Not exactly-once trust.** The application records no trust. The receiver tool stores the
  credential; the dashboard reports what it observes.
- **Not session continuity.** The receiver routes a credential by the target's address. If the
  device at that address has been replaced since the start, the receiver cannot tell, and
  neither can this application. The design therefore promises *target-addressed delivery under
  address continuity*, and nothing stronger.
- **Not a duplicate-submission guarantee.** The dashboard disables its own field while a
  submission is in flight, which prevents the ordinary double-click. It cannot prove that a
  second command never reaches the receiver.
- **Not a diagnosis.** A failure is reported as "did not start"; the application does not claim
  to know whether the credential was wrong, the receiver timed out, or the connection dropped.
- **Not a release guarantee.** Stopping a mirror is the existing behaviour, unchanged.

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
      ▼
operator types the credential ──► submit
      │
      ▼
client checks the value is representable as a command argument
      │
      ▼
transport sends:  connect T [--audio on|off] [--volume N] <credential>
      │
      ▼
await the reply (bounded), then read status once
      │
      ├── T no longer waiting      → report what the receiver reports, then close the prompt
      ├── T still waiting          → report "that PIN was not accepted", keep the prompt
      └── no reply / ambiguous     → report "the receiver did not answer", close the prompt
```

There is no lease, no reservation, no settlement state and no release transition. The receiver
tool owns all of it.

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

T-003's historical termination proof is already integrated and stays in the codebase as a
reviewed transport capability. **It is not used by this feature** and this delivery makes no
claim that depends on it.

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

The value is checked before anything is sent: non-empty, and for a PIN exactly four ASCII
digits. A value the receiver tool would parse as an option (leading `-`) is refused as invalid
input rather than sent, because the tool would consume it as a flag.

The command is built by the existing profile: `connect <target> [--audio on|off]
[--volume <n>] <credential>`, carrying the operator's original audio and initial-volume choices,
with the credential's index declared as a secret position.

### 6.4 Reporting

After the reply, one status read decides the message:

| Observation | Message | Prompt |
| --- | --- | --- |
| this target is reported `streaming` within the wait | "Connected to <TV>." (the receiver reports a streaming session) | closed |
| not `streaming` when the wait expires | "The mirror did not reach streaming in time." | closed |
| this target absent from the status | "The mirror did not reach streaming in time." | closed |
| no decoded reply to the command | "The receiver did not answer." | closed |
| receiver service reports it is shutting down | "The receiver service is stopping; start again." | closed |

**Why only these two outcomes.** The receiver acknowledges a credential before it authenticates
and removes the waiting stream when pairing fails, so "no longer waiting" is not proof of
success and a rejection cannot be told apart from a timeout by the application. `streaming` is
the one state the receiver only reports when a stream is actually carrying media, so it is the
one success claim this design is entitled to make. Everything else is reported as "did not
start", with no invented cause.

**No same-prompt retry.** A refused credential does not leave the waiting request open, so the
prompt closes and the operator starts again. The dashboard does not pretend a retry is possible.

The dashboard reports; it does not adjudicate. There is no attempt counter beyond the
receiver's own policy: the operator can retype, and the receiver decides whether to accept it.
The dialog's existing "Cancel" closes the prompt and leaves the receiver as it was.

### 6.5 Bounded waits

Two bounds, both pre-existing constants: the control command timeout for the submit, and
`CONNECT_STREAMING_TIMEOUT` for the wait that follows it, polled with the existing status read.
No new timers. A person typing is not subject to either bound: both start when the operator
submits, not when the prompt opens.

## 7. Failure handling

| Condition | Behaviour |
| --- | --- |
| status cannot be read | existing transport failure path, unchanged |
| the receiver asks but a newer status no longer shows it | prompt closes with "That receiver did not answer; start again." |
| the operator cancels | prompt closes; no command is sent |
| the credential is refused (the receiver removes the stream) | "The mirror did not start - check the PIN.", prompt closed |
| the receiver service is shutting down | message shown, prompt closes |
| the app is closed mid-prompt | existing shutdown path; no credential retained anywhere |

## 8. Security

The credential crosses one process boundary in the argv of `doubletake-ctl`, readable by a
same-UID process during that call. That matches the existing cooperative same-user boundary: any
such process can drive the daemon socket directly. The application stores nothing, logs nothing,
and retains nothing — enforced by the canary tests that this delivery must finish and integrate - they exist in the
quarantined core lane and are known to be incomplete.

The address-continuity limit of §2 is the honest statement of what targeting can and cannot
prove.

## 9. Compatibility

The installed four-token pair works unchanged; no rebuild, no new capability token beyond
`credential_relay`, which is derived from the pinned command surface. Profiles that prove the
four transaction capabilities keep their existing transactional pairing path untouched.

Documentation delivered with the change: `docs/backend-compatibility.md` and `README.md` lose the
"cannot service pairing" claim; `docs/operations.md` gains the three limits of §2.

## 10. Tests

1. **Capability** — `credential_relay` true for both pinned tables, false otherwise.
2. **Authorization** — the relay branch is the only new permission; four transaction flags false.
3. **Command** — exact argv with and without audio/volume; secret index computed from the built
   command; option-shaped credential refused client-side with no command sent.
4. **Request detection** — a request is derived only from the matching stream entry; another
   receiver's prompt produces nothing.
5. **Prompt lifecycle** — opens for the started receiver while another pane is selected; does
   not reopen for the same request; cancel sends nothing.
6. **Reporting** — one test per row of §6.4, driven by the fake daemon.
7. **Canary** — a unique credential value appears in no diagnostic, record, journal, export or
   retained observation, and no failing assertion prints it.
8. **Acceptance** — start → request → prompt → submit → report, with no production spawn.

Mutation probes: drop the target-equality check on request detection; drop the representability
check; hard-code the secret index; skip redaction in one observer; report "accepted" without the
follow-up status read.

## 11. Risks

1. **Address reuse** — accepted and documented (§2). Revalidation immediately before the command
   narrows the window; it cannot close it.
2. **Duplicate submission** — the dashboard disables its field while a submission is in flight;
   a second command from elsewhere is the receiver's problem, not this application's.
3. **Receiver lockout after repeated wrong PINs** — the operator can retype; the receiver decides.
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
submit_credential(target, value) -> Submitted | NoReply | ShuttingDown
```

which builds the T-002 profile command (`connect <target> [--audio on|off] [--volume N]
<credential>`, credential index declared as a secret) and runs it once. It reports only which of
three sanitized things happened; it does not interpret the receiver's error text and does not
carry a credential in any result.

The session then performs the bounded wait of §12.2 and derives the §6.4 message from the
observed state. Cancellation closes the prompt and sends nothing.

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

A value is refused before dispatch when it is empty, contains a NUL, or would be parsed as an
option by the profile in use (`connect` terminates the pinned CLI's flag parsing, so a leading
dash is positional there; the extended volume grammar is different). Refusal is a fixed,
secret-free invalid-input result and sends no command.

### 12.7 Wording (F-10, F-13)

"Connected" means exactly: *the receiver reports this target streaming*. It does not mean frames
are visible, and it does not mean the credential was remembered. A failure says only that
streaming was not reached in time; it names no cause, because the observations cannot establish
one. A later status that reaches streaming after the prompt closed is not retro-reported.
