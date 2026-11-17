# GUI device pairing — route B specification

**Status:** specification for the approved route-B design
**Authority:** [2026-11-15 GUI device pairing design](2026-11-15-gui-device-pairing-design.md), especially §§6.4, 7, 9–10 and 12
**Scope:** Mirror mode, during an operator-started mirror only
**Delivery artifact:** this specification and its named documentation updates; implementation evidence is required before delivery

## 1. Observable behavior

When the started receiver's own status row asks for a PIN or password, the application presents the existing credential dialog for that receiver. The operator may submit one value. The application reports success only after a status read reports that same target as `streaming` within `CONNECT_STREAMING_TIMEOUT` of the command reply.

A successful command reply alone is not success. A status row that disappears or remains non-streaming does not establish why the target did not reach `streaming`. A later `streaming` observation after the prompt has closed is not reported retroactively.

This path does not create an application credential record and does not call the transactional pairing coordinator or credential store.

## 2. Domain and transport contracts

### 2.1 Request event and target admission

Define `CredentialKind` and `CredentialRequest` once in `src/airplay_linux/domain/messages.py`; this is the sole route-B request payload. The `CredentialRequest` fields are exactly `target` and `kind`:

```python
CredentialKind = Literal["pin", "password"]

@dataclass(frozen=True)
class CredentialRequest:
    target: ReceiverTarget
    kind: CredentialKind
```

Add one member, `TransportEventKind.credential_request`, add `CredentialRequest` to the `TransportPayload` union, and use it as the event payload. The event envelope carries the active start's existing `OperationContext`; its `stream` is `None` because no `StreamHandle` exists while the matching status row is waiting for a credential.

`DoubletakeTransport._await_connecting_stream` emits this event only when exactly one `BackendStream` row matches the active start's `ReceiverTarget.canonical_host`, that row is `pin_required`, and its typed `credential_kind` is `pin` or `password`. Aggregate `StatusReply.needs_credential` and `StatusReply.credential_kind` never create a prompt. A missing, invalid, ambiguous, or different-target row creates no request.

The request is recorded in the pending stream observation for that matching status row and the current start operation; it does not depend on a `StreamHandle`. Repeated reads of the same row may publish the event again; the session identity rule in §3 coalesces them.

`accept_transport` and `_admit` add a dedicated stream-less admission branch for this event. It accepts only when all of these match current session state: receiver, active Mirror start operation, session generation, endpoint generation and fingerprint, daemon instance UUID, canonical target, and `CredentialRequest.target`. It rejects events for another start, mode, receiver, target, endpoint, daemon instance, or session generation. This branch does not install a stream handle or alter readiness.

The event remains on the existing route:

```text
_route(TransportEvent) -> Route.reconnect
ReconnectLifecycle -> MirrorLifecycle._transport_event
```

`ReconnectLifecycle` passes `credential_request` to the Mirror handler before applying network-loss or target-sync handling. The Mirror handler publishes the existing `ChallengeView` and stores only the non-secret request and its prompt identity in the current session context. It does not call `PairingPort`.

### 2.2 Submission and status read

Extend `TransportPort` with these session-facing operations:

```python
async def submit_credential(
    target: ReceiverTarget,
    value: SensitiveText,
) -> CredentialOutcome: ...

async def read_status(
    target: ReceiverTarget,
    context: OperationContext,
) -> Result[StatusReply]: ...

def dismiss_credential_request(
    target: ReceiverTarget,
    context: OperationContext,
) -> None: ...
```

`dismiss_credential_request` is a local notification to the pending `start_stream` wait. It sends no command to `doubletake` and exists so a local Cancel can resume the suspended readiness budget required by §12.2. The existing port has no notification edge for that transition; adding this method makes the specified Cancel behavior observable to the coroutine that owns the wait.

`CredentialOutcome` is:

```python
@dataclass(frozen=True)
class Submitted:
    reply_at: float  # monotonic time at which a complete command response was decoded

@dataclass(frozen=True)
class NoReply:
    pass

CredentialOutcome = Submitted | NoReply
```

`Submitted` means that a complete response was decoded, including a negative response. It does not mean the receiver accepted the credential. No response text or credential value appears in either outcome. A decoded negative response proceeds to status observation just like any other decoded response.

The pinned daemon's shutdown response has only `OK: false`, `State: idle`, and free-form `Error` text. There is no typed shutdown field in the receiver response. The adapter does not inspect that text; `ShuttingDown` is therefore omitted from the union. A decoded shutdown response is `Submitted`; if the target does not report `streaming`, it follows the generic non-streaming row in §5.

`read_status` uses the same current target and owned control endpoint as the start. It returns the validated `StatusReply`; the session considers only that reply's uniquely matching per-stream row. A status read error follows the existing transport failure path and is not translated into a credential result.

### 2.3 Capability and authorization

The `credential_relay` capability is true only when the verified public help contract's control signatures equal `_PINNED_050_CTL_COMMANDS` or `_VOLUME_CTL_COMMANDS`. Verification still requires the corresponding pinned sender surface and the consistency rules in `features_from_help`. It is false for an unknown, partial, or near-match surface. Version text, a `pin` token, keyring configuration, and prose are not capability evidence.

The command builder uses the existing authorization check:

```python
EvidenceAuthority.allows(BackendOperation.submit_pairing)
```

For this operation, authorization requires `credential_relay`, `owned_daemon_control`, `multi_target`, and `physical_source`. It does not require the four separate flags `keyring`, `provisional_commit`, `cancel_pairing`, or `revoke_pairing`; those remain false for the route-B profile. `target_bound_pairing` also remains false and is not used as the route-B capability.

Revalidate the target and current endpoint immediately before command construction. A status row disappearing between detection and submission does not suppress the operator's target-addressed command; the subsequent status read controls the report.

### 2.4 Command construction and secret position

Use the pinned profile command exactly:

```text
doubletake-ctl -socket <path> connect <target> [--audio on|off] [--volume N] <credential>
```

The target is the same canonical target used by the started Mirror operation. The pending start retains its original `StartRequest.audio` and `StartRequest.volume` values for command construction; select them from the admitted receiver/start context, never from the selected pane. Preserve its audio choice; include `--volume N` only when an initial volume was requested and `target_volume` is proven. Omit absent optional arguments.

Build the final argv first, then set the single secret position to the credential's actual final index:

```python
secret_indices = (len(argv) - 1,)
```

The targetless `pin <credential>` form is never emitted. Run one command for one valid operator submission. Do not add the credential to diagnostics, results, status requests, command descriptions, or activity fields.

### 2.5 Input validation

Validate before constructing a command or calling `submit_credential`:

- The value is non-empty. Do not trim or normalize it.
- It contains no NUL.
- The platform can encode it as one argv element.
- It does not begin with `-`, for either credential kind.
- For `kind == "pin"`, it consists of exactly four ASCII digits.

A rejected value produces the fixed, secret-free `ErrorCode.invalid_input` result, `The submitted value was rejected. Enter a new value to try again.`, and sends no command. The prompt remains available for local correction; the route exposes no `Action.retry` command. Whitespace is not stripped before validation.

## 3. Prompt identity and lifecycle

The prompt identity is exactly:

```text
(receiver_id, start_operation_id, first_observed_credential_kind)
```

The first kind observed for that start is latched. Repeated status rows do not reopen or change the open prompt. A different selected pane never retargets it. For `kind == "pin"`, show `Enter the PIN shown on that TV.`; for `kind == "password"`, use the existing password wording. Replacing the local start invalidates the old identity before any later event or submit can use it.

The published `ChallengeView` is a local presentation/action view, not a receiver challenge. Its `challenge_id` is a fresh `IdFactory` value used only to reject stale UI actions; it is never sent to the receiver. Populate its receiver, endpoint, operation, session, trust generation, and kind from the admitted event and current session. Set `created_at` from the monotonic clock and `expires_at` to `None`; the status contract supplies no request expiry. Make `ChallengeView.expires_at` optional and omit the expiry sentence in `PairingDialog` when it is `None`.

`SubmitPairing` keeps its existing public command shape and `Route.trust`. `PairingFlow` checks whether its current `ChallengeView` maps to the active route-B request identity. If so, it validates locally and calls `TransportPort.submit_credential`; it does not call `PairingPort.submit_pairing`, mutate trust state, or access the credential store. Otherwise the established pairing flow is unchanged.

The route-B dialog offers only Submit and Cancel. During submission, make the field read-only and disable Submit. Cancel remains a local dismiss action before and during submission: it closes the dialog and sends no receiver command. If a command is already running, dismissal does not cancel it; its bounded status report continues in the pane. A command result closes the prompt. No dialog action resends a value after a decoded result or `NoReply`; another credential submission requires a new operator Start.

When the matching status row changes or disappears before submission, close the prompt and resume the pending start. The pending start continues under its restored deadline; its existing result path determines any later message. On app shutdown, clear the field and discard the in-memory request view through the existing shutdown path.

## 4. Deadline ownership and interleaving

The pending `start_stream` operation remains the owner of its readiness wait. The request event does not start a second Start operation or add a timer.

| Existing bound | Existing owner and start | Route-B behavior |
| --- | --- | --- |
| `CONTROL_TIMEOUT` (5 seconds) | Control command wait, begun when the command is submitted | Unchanged; it does not run while the operator is typing. |
| `CONNECT_STREAMING_TIMEOUT` acceptance bound (20 seconds) | Start wait before any matching target row has appeared | Unchanged. A request event can occur only after the target row has appeared. |
| `PERMISSION_WAIT_TIMEOUT` (300 seconds) | Start wait while the matching stream lacks a source | Suspend while the request identity is live. Save remaining duration; resume when submission is begun, the matching row changes or disappears, Cancel dismisses the prompt, or the local start is replaced. Shift the deadline by the elapsed pause. |
| `CONNECT_STREAMING_TIMEOUT` handshake bound (20 seconds) | Start wait after a matching stream has a source | It is not active while the credential row lacks a source. If a credential request is observed while this bound is active, suspend and restore its remaining duration by the same rule. |
| Post-command streaming bound, `CONNECT_STREAMING_TIMEOUT` (20 seconds) | Session report task, begun at `Submitted.reply_at` | Poll `read_status` at `SOURCE_POLL_INTERVAL`. This deadline is independent of the restored start deadline. Accept `streaming` only when observed before the deadline. |

Beginning submission resumes the start's suspended budget before starting the control command. The post-command deadline begins at the monotonic `reply_at`, not when the actor later drains the result event. If the result reaches the actor after that deadline, perform one final status read and apply the deadline rule; a status first observed at or after the deadline cannot produce the connected message.

A local Cancel calls `dismiss_credential_request(target, context)`, closes the prompt, and sends no backend command. A replaced start invalidates the old prompt and dismisses its wait before the new start is admitted.

## 5. Exact reporting strings

Use the design's literal strings and this observation table. `<TV>` is the existing receiver display name.

| Observation | Message | Prompt | Source status |
| --- | --- | --- | --- |
| Matching target row reports `streaming` before the post-command deadline | `Connected to <TV>.` | Closed | Consistent with §§6.4 and 12.7. |
| Matching target is present but has not reported `streaming` when the post-command deadline expires | `The mirror did not reach streaming in time.` | Closed | Consistent with §§6.4 and 12.7. |
| Matching target is absent from a post-command status reply | `The mirror did not reach streaming in time.` | Closed | Consistent with §§6.4 and 12.7. |
| No complete command response was decoded | `The receiver did not answer.` | Closed | §6.4 gives this string; §12.7's generic-only sentence also applies to failures. See Finding MSG-01. |
| Receiver responds with a decoded shutdown error but no typed shutdown field | `The mirror did not reach streaming in time.` | Closed | The §6.4 shutdown-specific string is not emitted because its condition cannot be read without interpreting free-form text. |
| Status read fails | Existing transport failure message and handling | Existing path | §7 says the existing transport failure path is unchanged. |
| Matching request row changes or disappears before submission | No pairing-specific message at that point; close the prompt and resume Start | Closed | The eventual Start result follows its existing path. §7's specific sentence conflicts with §12.7; see Finding MSG-02. |

The approved design also gives the shutdown-specific source string `The receiver service is stopping; start again.` in §6.4 and the pre-submit request-change string `That receiver did not answer; start again.` in §7. They are recorded here as source-design text, not as emitted messages. The first has no typed source. The second infers a receiver outcome that the later §12.7 wording does not permit.

## 6. Redaction and retention

Project the credential to the fixed redaction marker before every retained observation is created. Redaction at display time alone is insufficient. No retained value, exception, repr, diagnostic, activity record, command journal, process record, probe recorder, acceptance recorder, export, or test assertion may contain the submitted value.

The command's `secret_indices` are authoritative for argv projection. Keep `SensitiveText` repr redacted and non-picklable. Clear the dialog field on close, hide, cancellation, completion, and application shutdown. The transport result carries no credential.

The delivery gate includes a unique-canary suite. It must test every observer named above and verify that a deliberate failing assertion also prints no canary. The work is not complete while any observer lacks its canary case.

## 7. Required tests and mutation evidence

Each test row names the behavior and the mutation that must make it fail. Run the acceptance path with fake control/process boundaries; it must not spawn the production receiver tool.

| Design §10 item | Required test | Mutation it kills |
| --- | --- | --- |
| Capability | `test_credential_relay_is_proven_only_by_both_pinned_control_tables` | Derive `credential_relay` from version text, a partial token set, a targetless `pin` token, or any control signature set other than `_PINNED_050_CTL_COMMANDS` and `_VOLUME_CTL_COMMANDS`. |
| Authorization | `test_submit_pairing_authority_uses_credential_relay_branch` | Remove the `EvidenceAuthority.allows(BackendOperation.submit_pairing)` branch, omit one of its four required facts, or gate it on `keyring`, `provisional_commit`, `cancel_pairing`, or `revoke_pairing`. |
| Command | `test_connect_command_preserves_target_audio_volume_and_secret_index` | Drop `relay_credential_representable`, hard-code the secret index, emit `pin`, or alter optional audio/volume argv order. |
| Input | `test_invalid_credential_is_rejected_before_command_construction` | Allow empty, NUL, non-encodable, leading-dash, or malformed PIN input to reach the command runner. |
| Request detection | `test_credential_request_requires_matching_stream_target` | Remove the equality check between the active target and the per-stream target, or derive a request from aggregate status. |
| Prompt lifecycle | `test_prompt_tracks_start_receiver_not_selected_pane` | Use the selected pane instead of the started receiver; reopen on repeated status; allow the old start after replacement; route Cancel to a receiver command; or leave the field editable while submission is in flight. |
| Reporting | One fake-daemon test for every observation row in §5 | Report `Connected` from a command response without the follow-up status read; accept another target or aggregate status; ignore the deadline; or parse free-form shutdown text into a distinct result. |
| Canary | `test_credential_canary_is_absent_from_all_retained_observers` with one probe per observer and a failing-assertion control | Skip redaction in any single observer or print the raw value through a failed assertion. |
| Acceptance | `test_mirror_credential_request_submit_and_report_without_production_spawn` | Bypass the new event route, call the transactional pairing path, or substitute command acknowledgement for the status-based report. |

The acceptance sequence is: operator Start; matching per-stream request; `Route.reconnect` admission; Mirror prompt publication; local validation; one target-bound command; bounded status polling; exact report. Each test asserts receiver, start operation, session generation, endpoint identity, and credential kind where applicable.

## 8. Documentation updates

### README.md

Replace README's outdated pairing-capability statements. Replace the stale source-acquisition statement that treats a credential challenge as `dependency_incompatible` with the route-B behavior and its `credential_relay` gate. Keep unrelated activation prerequisites intact.

Use wording equivalent to:

> During an operator-started Mirror, a proven `credential_relay` profile can present a PIN/password prompt for the started target. The application reports success only when that target's status reports `streaming`.

Do not claim that the application stores the value, that a command response establishes streaming, or that status identifies why streaming was not reached.

### docs/backend-compatibility.md

Remove the statement that credential-bearing `connect` and `pin` forms are outside the application's secret-safe contract. Document the exact target-bound `connect <target> [--audio on|off] [--volume N] <credential>` mapping, the final argv secret index, the two pinned help profiles that prove `credential_relay`, and the `EvidenceAuthority` requirements. State that targetless `pin` remains unused and the four independent transaction flags remain false. Do not infer capability from the keyring selector or version text.

### docs/operations.md

Add exactly these five operator limits:

1. The command addresses a target value; a different receiver at that address may receive it.
2. The application reports its own target-specific status observation, not what appears on the display.
3. The application does not write the submitted value to application metadata; any receiver-side storage is outside the application's observation.
4. When the target does not report `streaming`, the application does not assign a reason.
5. The field disables duplicate actions from this dialog while its command is in flight; commands issued elsewhere are outside that UI control. Stop keeps its existing behavior.

## 9. Findings carried from the approved design

### Finding SHUTDOWN-01 — no typed discriminator

The pinned receiver returns shutdown as a free-form error string and an idle state. No typed field distinguishes shutdown from other decoded negative responses. This specification drops `ShuttingDown`, ignores the free-form error, and maps a decoded response without a later `streaming` observation to the generic message in §5. No code may create a shutdown-specific diagnostic from receiver text.

### Finding MSG-01 — command no-reply wording

Design §6.4 assigns `The receiver did not answer.` to the no-decoded-reply observation. Design §12.7 says every failure reports only that streaming was not reached in time. Both cannot be the sole emitted message for this row. The §5 table preserves the exact §6.4 text and marks the conflict; it does not silently choose precedence. This conflict is a design finding, not permission to add a third interpretation.

### Finding MSG-02 — request disappears before submission

Design §7 assigns `That receiver did not answer; start again.` when a later status no longer shows the waiting row. Design §12.7 bars an inferred outcome and limits the message to the streaming observation. The normative behavior here is limited to closing the prompt and resuming Start; the specific §7 sentence is not emitted. The eventual Start result uses its existing path. The conflicting §7 wording remains recorded above for design-owner disposition.

### Finding VIEW-01 — presentation view has richer fields than the request

The transport request supplies only target and kind, while the existing `ChallengeView` requires a local challenge ID and timestamp fields. This specification defines those as local presentation metadata, keeps the prompt identity in §3 authoritative, and makes `expires_at` optional because the receiver provides no expiry. The local challenge ID is never sent to the receiver.

### Finding CANCEL-01 — local resume notification

The approved behavior requires Cancel to dismiss the prompt and resume the pending start's suspended budget. `TransportPort` currently has no callback for that local transition. `dismiss_credential_request` in §2.2 is the required local-only notification; it performs no backend I/O and is covered by the deadline and prompt-lifecycle tests.
