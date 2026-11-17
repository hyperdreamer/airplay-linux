# GUI device pairing — route B specification

**Status:** specification for the approved route-B design
**Authority:** [2026-11-15 GUI device pairing design](2026-11-15-gui-device-pairing-design.md), especially §§6.4, 7, 9–10 and 12, with the design-owner resolutions recorded below
**Reviewed spec:** integration commit `72d530e39b1619b4226b77ee998513c4b9699a44`, path `docs/superpowers/specs/2026-11-15-gui-device-pairing-spec.md`
**Reviewed design:** integration commit `72d530e39b1619b4226b77ee998513c4b9699a44`, path `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md`
**Integration worktree:** `/data/home/henry-arch/.local/state/pi/project-manager/worktrees/2f85929c2b2e9a7bef4df2b51d4f73eee4d6202cfdab6e68ef6ff9c831e8f436/pm-run-20261115-031248-7c91beef/integration`
**Review:** `reports/route-b-spec-review-1.md` under the run directory `/data/home/henry-arch/.local/state/pi/project-manager/runs/2f85929c2b2e9a7bef4df2b51d4f73eee4d6202cfdab6e68ef6ff9c831e8f436/pm-run-20261115-031248-7c91beef`
**Scope:** Mirror mode, during an operator-started mirror only
**Delivery artifact:** this specification and its named documentation updates; implementation evidence is required before delivery

## 1. Observable behavior

When the started receiver's own status row asks for a PIN or password, the application presents the existing credential dialog for that receiver. The operator may submit one value. The application reports success only after a status read reports that same target as `streaming` within `CONNECT_STREAMING_TIMEOUT` of the command reply.

A successful command reply alone is not success. A status row that disappears or remains non-streaming does not establish why the target did not reach `streaming`. After submission, the prompt stays open and read-only through the terminal status report, subject to explicit dismissal or invalidation as specified in §3. A later `streaming` observation after the prompt has closed is not reported retroactively.

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

Add `TransportEventKind.credential_request` and `TransportEventKind.credential_request_ended`, with `CredentialRequest` and the following end payload in the `TransportPayload` union:

```python
@dataclass(frozen=True)
class CredentialRequestEnded:
    target: ReceiverTarget
    kind: CredentialKind  # first observed kind of the request being ended
```

Both events use the existing `TransportEvent` envelope: `receiver_id`, `endpoint_fingerprint`, `endpoint_generation`, and `daemon_instance_uuid` identify the admitted start; `context` is that start's existing `OperationContext`; `stream=None`. Their payloads are respectively `CredentialRequest(target, kind)` and `CredentialRequestEnded(target, kind)`. Neither event requires a `StreamHandle` or carries a command completion.

`DoubletakeTransport._await_connecting_stream` emits `credential_request` only when exactly one `BackendStream` row matches the active start's `ReceiverTarget.canonical_host`, that row is `pin_required`, and its typed `credential_kind` is `pin` or `password`. Aggregate `StatusReply.needs_credential` and `StatusReply.credential_kind` never create a prompt. A missing, invalid, ambiguous, or different-target row creates no request.

The request is recorded in the pending stream observation for that matching status row and the current start operation; it does not depend on a `StreamHandle`. Repeated reads of the same row may publish the event again; the session identity rule in §3 coalesces them.

`accept_transport` and `_admit` add a dedicated stream-less admission branch for both events. It accepts only when all of these match current session state: receiver, active Mirror start operation, session generation, endpoint generation and fingerprint, daemon instance UUID, canonical target, and the payload's `target`. For `credential_request_ended`, the payload kind must also identify the latched request being ended. It rejects events for another start, mode, receiver, target, endpoint, daemon instance, or session generation. This branch does not install a stream handle or alter readiness.

Both events remain on the existing route:

```text
_route(TransportEvent) -> Route.reconnect
ReconnectLifecycle -> MirrorLifecycle._transport_event
```

`ReconnectLifecycle` passes both `credential_request` and `credential_request_ended` to the Mirror handler before applying network-loss or target-sync handling. For a request, the Mirror handler publishes the existing `ChallengeView` and stores only the non-secret request and its prompt identity in the current session context, exposing the presentation fields specified in §3. It does not call `PairingPort`.

When a validated status reply shows that a previously live request no longer has exactly one matching row in `pin_required` with a valid credential kind, `_await_connecting_stream` emits `credential_request_ended` for the original target and latched kind. A different valid kind alone does not replace the first kind or create another prompt. A status read error is not evidence of request end. The transport restores its suspended budgets at this observation and continues the same pending start; the end event neither resolves `start_stream` nor fabricates a `ready`, `failed`, or `stopped` event. In particular, request disappearance alone must not trigger an immediate missing-row completion in that wait. Subsequent normal start evidence or the restored deadline determines its result.

Before submission, the Mirror handler consumes this event by clearing the matching route-B prompt and request, with no pairing-specific report and no new Start. Repeated end events are idempotent, and retired request events cannot reopen the same start's prompt. Once submission has begun, the event can retire the transport's waiting-row observation but cannot close the read-only reporting prompt or decide its result; §3's report lifetime applies. `dismiss_credential_request` is the opposite, local-to-transport notification and is not a substitute for this event.

### 2.2 Submission and status read

Extend `TransportPort` with these session-facing operations:

```python
async def submit_credential(
    target: ReceiverTarget,
    value: SensitiveText,
) -> Result[CredentialOutcome]: ...

async def read_status(
    target: ReceiverTarget,
    context: OperationContext,
) -> Result[StatusReply]: ...

def dismiss_credential_request(
    target: ReceiverTarget,
    context: OperationContext,
) -> None: ...
```

`dismiss_credential_request` is a local notification to the pending `start_stream` wait. It sends no command to `doubletake` and exists so a local Cancel can resume the suspended readiness budgets required by design §12.2 and §4 below. The existing port has no notification edge for that transition; adding this method makes the specified Cancel behavior observable to the coroutine that owns the wait.

The return uses the project's `Result` from `domain/resources.py`: `Ok(value=...) | Err(error=diagnostic)`. Target/endpoint revalidation, authorization, capability/profile checks, command construction, and process launch can fail locally before any receiver response; these return a sanitized `Err(error=diagnostic)`, never a fabricated `Submitted` or `NoReply`. They retain the existing local diagnostic path and do not start post-command status polling. Local input rejection remains correctable as specified in §2.5; a failure of the dispatched transport operation ends that submission and closes its prompt with the local diagnostic.

`CredentialOutcome` remains exactly the two receiver-side observations, wrapped as `Ok(value=Submitted(reply_at=...))` or `Ok(value=NoReply())`:

```python
@dataclass(frozen=True)
class Submitted:
    reply_at: float  # monotonic time at which a complete command response was decoded

@dataclass(frozen=True)
class NoReply:
    pass

CredentialOutcome = Submitted | NoReply
```

`Submitted` means that a complete response was decoded, including a negative response. It does not mean the receiver accepted the credential. `NoReply` means the command was launched but its bounded response wait ended without any complete decoded response; it is not a catch-all for local failures. No response text or credential value appears in either outcome. A decoded negative response proceeds to status observation just like any other decoded response. The adapter must preserve this distinction even when an existing control helper represents both a decoded negative acknowledgement and a local failure with `Err`.

The pinned daemon's shutdown response has only `OK: false`, `State: idle`, and free-form `Error` text. There is no typed shutdown field in the receiver response. The adapter does not inspect that text; `ShuttingDown` is therefore omitted from the union. A decoded shutdown response is `Submitted`; a subsequent terminal non-streaming observation follows the generic row in §5, while a status-read failure keeps the existing transport failure path.

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

The target is the same canonical target used by the started Mirror operation. The pending start retains its original `StartRequest.audio` and `StartRequest.volume` values for command construction; select them from the admitted receiver/start context, never from the selected pane. When `target_volume` is proven, carry both choices: include `--audio on|off` and, when an initial volume was requested, `--volume N`, in that order. When `target_volume` is unproven, omit **both** `--audio` and `--volume`, even if the original start supplied them; emit `connect <target> <credential>`. The original audio choice cannot be preserved on that profile, and no initial-volume preservation is claimed. Omit an absent initial volume on the proven profile.

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

A rejected value produces `Err(error=diagnostic)` with the fixed, secret-free `ErrorCode.invalid_input` message, `The submitted value was rejected. Enter a new value to try again.`, and sends no command. The prompt remains available for local correction; the route exposes no `Action.retry` command. Whitespace is not stripped before validation.

## 3. Prompt identity and lifecycle

The prompt identity is exactly:

```text
(receiver_id, start_operation_id, first_observed_credential_kind)
```

The first kind observed for that start is latched. Repeated status rows do not reopen or change the open prompt. A different selected pane never retargets it. For `kind == "pin"`, show `Enter the PIN shown on that TV.`; for `kind == "password"`, use the existing password wording. Replacing the local start invalidates the old identity before any later event or submit can use it.

The published `ChallengeView` is a local presentation/action view, not a receiver challenge. Its `challenge_id` is a fresh `IdFactory` value used only to reject stale UI actions; it is never sent to the receiver. Populate its receiver, endpoint, operation, session, trust generation, and kind from the admitted event and current session. Set `created_at` from the monotonic clock and `expires_at` to `None`; the status contract supplies no request expiry. Make `ChallengeView.expires_at` optional and omit the expiry sentence in `PairingDialog` when it is `None`.

### 3.1 Route-B presentation allowance

Expose the admitted non-secret request as `SessionSnapshot.route_b_request: CredentialRequest | None = None`, alongside the existing `SessionSnapshot.challenge`. Add `SessionSnapshot.route_b_submission_pending: bool = False`: set it only after local validation succeeds and before dispatch, and keep it true through the command and status report. The request marker is retained for that report lifetime even if the receiver's waiting row has ended; it is not a claim that the receiver is still asking. Clear both route-B fields and the corresponding challenge on terminal report, local terminal failure, dismissal, start replacement, or shutdown; before submission, the admitted request-ended event also clears them. None of these route-B transitions changes `TrustState` or advances trust generation.

Name the pure presentation predicate `_route_b_prompt_live(state: SessionSnapshot) -> bool` in `ui/state.py`. Its exact predicate is:

```python
def _route_b_prompt_live(state: SessionSnapshot) -> bool:
    request = state.route_b_request
    challenge = state.challenge
    return (
        request is not None
        and challenge is not None
        and state.mode is Mode.MIRROR
        and state.target == request.target
        and challenge.receiver_id == state.receiver_id == request.target.receiver_id
        and challenge.operation_id == state.operation_id
        and challenge.session_generation == state.session_generation
        and challenge.endpoint_generation == request.target.endpoint_generation
        and challenge.endpoint_fingerprint == request.target.endpoint_fingerprint
        and challenge.trust_generation == state.trust_generation
        and challenge.credential_kind == request.kind
    )
```

The session only publishes this marker for the current admitted identity and clears it on invalidation. The predicate tests neither `state.trust` nor `state.phase`; a live route-B request therefore exposes its challenge independently of `TrustState`. Add `PaneState.route_b_prompt: bool = False`, set exactly from this predicate, and `PaneState.route_b_submission_pending: bool = False`, set to `route_b_prompt and snapshot.route_b_submission_pending`. `_actionable_challenge` returns `state.challenge` on this route-B predicate before its existing transactional guards, and returns `None` for a route-B marker whose predicate fails. Thus `PaneState.challenge` contains the local view throughout the live prompt/report lifetime. Without a route-B marker, the transactional pairing projection, including its existing trust/phase guards and action rules, is unchanged.

On the route-B branch, `PaneState.actions` replaces the pairing-prompt actions with `{Action.submit_pairing, Action.cancel}` before submission and `{Action.cancel}` while submission/reporting is pending; never offer `Action.retry` for this request. Preserve the existing non-prompt pane actions, including Stop, and pass only the route-B prompt actions to the dialog. This allowance takes precedence over trust/phase-based prompt suppression; it does not grant a new Start or change the existing Stop behavior. `Presenter._snapshot_arrived` checks the projected route-B prompt for the arriving receiver, including an unselected receiver. `_maybe_open_pairing` bypasses its selected-pane restriction only when `pane.route_b_prompt` is true, and `_open_pairing` retains its exact challenge check and duplicate suppression. `_pairing_prompt` admits a route-B submit only while the current projection still has that challenge, `route_b_prompt` is true, `route_b_submission_pending` is false, and `Action.submit_pairing` is offered. Cancellation uses the matching route-B identity. These checks and the session admission retain all receiver/start/generation fences; they add no `TrustState.Pairing` requirement. The transactional presenter path is unchanged.

### 3.2 Submission and closure

`SubmitPairing` keeps its existing public command shape and `Route.trust`. `PairingFlow` checks whether its current `ChallengeView` maps to the active route-B request identity. If so, it validates locally and calls `TransportPort.submit_credential`; it does not call `PairingPort.submit_pairing`, mutate trust state, or access the credential store. Without a route-B marker, the established pairing flow is unchanged. A stale route-B marker is rejected, not passed to the transactional flow. The actor rechecks the route-B identity and pending flag before dispatch so a second UI submission cannot send another command.

The route-B dialog offers only Submit and Cancel. Once a valid submission is dispatched, keep the same prompt open with the field read-only and Submit disabled until its report reaches a terminal outcome. Hand the value to the submit action and clear the field without accepting or closing the route-B dialog; the presenter's pending-prompt ownership remains until closure. A decoded command reply is an intermediate observation: `Ok(Submitted)` begins status polling and does not close the prompt. Neither a start completion nor a request-ended event substitutes for the report's status observation. Publish the terminal report from §5, then close and clear the prompt. `Ok(NoReply)` ends the report without a status poll; a local `Err` uses §2.2. No dialog action resends a value after a decoded result or `NoReply`; another credential submission requires a new operator Start.

Cancel remains a local dismiss action before and during submission/reporting: it closes the dialog and sends no receiver command. If a command is already running, dismissal does not cancel it; any remaining bounded work may finish internally, but closing retires this report's publication authority. No later outcome is reported in the pane or by reopening the prompt. Start replacement and shutdown likewise invalidate the old report before later results can publish. After terminal closure, later status observations cannot revise or repeat that report.

When the matching status row changes or disappears before submission, the routed `credential_request_ended` event closes the prompt and resumes the pending start as specified in §2.1. The pending start continues under its restored deadlines; its existing result path determines any later Start message, without fabricating a credential report. On app shutdown, clear the field and discard the in-memory request view through the existing shutdown path.

## 4. Deadline ownership and interleaving

The pending `start_stream` operation remains the owner of its readiness wait. The request event does not start a second Start operation or add a timer.

| Existing bound | Existing owner and start | Route-B behavior |
| --- | --- | --- |
| `CONTROL_TIMEOUT` (5 seconds) | Control command wait, begun when the command is submitted | Unchanged; it does not run while the operator is typing. |
| `CONNECT_STREAMING_TIMEOUT` acceptance bound (20 seconds) | Start wait before any matching target row has appeared | Unchanged. A request event can occur only after the target row has appeared. |
| `PERMISSION_WAIT_TIMEOUT` (300 seconds) | Overall start-readiness ceiling, begun at entry to `_await_connecting_stream` | Continues to run after a matching row and then a source appear. Suspend while the request awaits an operator response. Save remaining duration; resume when submission is begun, the matching row changes or disappears, Cancel dismisses the prompt, or the local start is replaced. Shift the deadline by the elapsed pause. |
| `CONNECT_STREAMING_TIMEOUT` handshake bound (20 seconds) | Additional start bound after a matching stream has a source | Applies in addition to the overall ceiling; the earlier deadline wins. It is not active while the row lacks a source. Suspend an active handshake budget with the overall budget and restore its own remaining duration on the same resume transition. |
| Post-command streaming bound, `CONNECT_STREAMING_TIMEOUT` (20 seconds) | Session report task, begun at `Submitted.reply_at` | Poll `read_status` at `SOURCE_POLL_INTERVAL`. This deadline is independent of the restored start deadline. Accept `streaming` only when observed before the deadline. |

For each active readiness deadline, suspension saves `remaining = deadline - pause_started_at`; resumption sets `deadline = resumed_at + remaining` exactly once. It neither resets a budget nor revives an expired one. If the source first appears during the pause, initialize the handshake's 20-second budget in the paused state; both budgets begin counting their remaining time on resumption. A source appearing near the overall ceiling cannot grant an extra 20 seconds beyond it.

Beginning submission resumes both suspended readiness budgets before starting the control command. Keeping the prompt read-only during the report does not suspend them again. The post-command deadline begins at the monotonic `reply_at`, not when the actor later drains the result event. If the result reaches the actor after that deadline, perform one final status read and apply the deadline rule; a status first observed at or after the deadline cannot produce the connected message.

A local Cancel calls `dismiss_credential_request(target, context)`, closes the prompt, and sends no backend command. A replaced start invalidates the old prompt and dismisses its wait before the new start is admitted.

## 5. Exact reporting strings

Use this observation table as the design owner's resolved wording contract. `<TV>` is the existing receiver display name. There are exactly two route-B receiver-observation failure strings: `The receiver did not answer.` for no decoded command reply, and `The mirror did not reach streaming in time.` for every observed non-streaming terminal outcome. A local `Err` and a status-read failure keep their existing diagnostic paths; neither is reclassified as a receiver observation. An intermediate non-streaming row keeps the report pending until its terminal condition. Superseded wording in design §§6.4, 7 and 12.7 does not authorize another string.

| Observation | Message | Prompt | Reporting rule |
| --- | --- | --- | --- |
| Matching target row reports `streaming` before the post-command deadline | `Connected to <TV>.` | Close after reporting | Only this target's timely status establishes success. |
| Matching target is present but has not reported `streaming` when the post-command deadline expires | `The mirror did not reach streaming in time.` | Close after reporting | Terminal non-streaming observation. |
| Matching target is absent from a post-command status reply | `The mirror did not reach streaming in time.` | Close after reporting | Terminal non-streaming observation; no inferred cause. |
| No complete command response was decoded after dispatch (`Ok(NoReply)`) | `The receiver did not answer.` | Close after reporting | Terminal no-reply observation; no status poll. |
| Receiver responds with a decoded shutdown error but no typed shutdown field, and subsequent status is terminally non-streaming | `The mirror did not reach streaming in time.` | Close after reporting | The decoded reply is `Submitted`; only the subsequent status/deadline ends the report. |
| Status read fails | Existing transport failure message and handling | Close through the existing failure path | Preserve the transport failure; do not manufacture a non-streaming observation. |
| Matching request row changes or disappears before submission | No pairing-specific message at that point; close the prompt and resume Start | Closed | `credential_request_ended` leaves Start pending; its eventual result follows the existing path. |

After an explicit dismissal or other invalidation in §3, these report publications are suppressed. A terminal report is published once before closure; nothing is reported retroactively.

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
| Command | `test_connect_command_preserves_target_audio_volume_and_secret_index` | Drop `relay_credential_representable`, hard-code the secret index, emit `pin`, alter optional audio/volume argv order, emit either flag when `target_volume` is unproven, or omit either requested choice when it is proven. |
| Input | `test_invalid_credential_is_rejected_before_command_construction` | Allow empty, NUL, non-encodable, leading-dash, or malformed PIN input to reach the command runner. |
| Request detection | `test_credential_request_requires_matching_stream_target` | Remove the equality check between the active target and the per-stream target, or derive a request from aggregate status. |
| Request end | `test_credential_request_ended_closes_prompt_without_completing_start` | Drop the new end-event route; accept a stale or foreign end event; synthesize a start completion on request end; or leave the pre-submit prompt open after the matching row changes/disappears. |
| Presentation | `test_route_b_prompt_is_projected_and_admitted_without_pairing_trust` | Gate `_route_b_prompt_live`, `PaneState.challenge`, offered submit, or presenter admission on pairing trust/phase; require pane selection; mutate trust to show the prompt; or relax the transactional projection's existing guards. |
| Submission result | `test_submit_credential_distinguishes_local_err_from_receiver_outcomes` | Map a revalidation, authorization, profile, construction, or launch failure to `NoReply`/`Submitted`; poll status for `Err`/`NoReply`; or map a decoded negative reply to a local failure. |
| Prompt lifecycle | `test_prompt_tracks_start_receiver_not_selected_pane` | Use the selected pane instead of the started receiver; reopen on repeated status; allow the old start after replacement; route Cancel to a receiver command; or leave the field editable while submission is in flight. |
| Report lifetime | `test_route_b_prompt_remains_read_only_until_terminal_report` | Close on Submit or `Submitted`; close on request end during reporting; enable a second submit before the terminal status; or publish an outcome after dismissal, invalidation, or terminal closure. |
| Readiness deadlines | `test_route_b_suspends_and_restores_overall_and_handshake_budgets` | Stop the overall ceiling when a source appears; replace it with a fresh handshake budget; leave either budget running while typing; reset either remaining duration on resume; or suspend readiness again during reporting. |
| Reporting | One fake-daemon test for every observation row in §5 | Report `Connected` from a command response without the follow-up status read; accept another target or aggregate status; ignore the deadline; substitute either resolved failure string for the other; emit a pre-submit disappearance report; or parse free-form shutdown text into a distinct result. |
| Canary | `test_credential_canary_is_absent_from_all_retained_observers` with one probe per observer and a failing-assertion control | Skip redaction in any single observer or print the raw value through a failed assertion. |
| Acceptance | `test_mirror_credential_request_submit_and_report_without_production_spawn` | Bypass the new event route, call the transactional pairing path, or substitute command acknowledgement for the status-based report. |

The acceptance sequence is: operator Start; matching per-stream request; `Route.reconnect` admission; Mirror prompt publication; local validation; one target-bound command; bounded status polling; exact report followed by prompt closure. Each test asserts receiver, start operation, session generation, endpoint identity, and credential kind where applicable.

For presentation, exercise every `TrustState` with a current route-B request and a non-pairing phase: assert the projected challenge, submit/cancel actions, automatic opening for an unselected receiver, and successful submit admission without changing trust. A companion fixture without the route-B marker must retain the transactional trust/phase behavior. For request end, hold `start_stream` unresolved while a matching row becomes `connecting` and while it disappears; assert the routed prompt closure, resumed budgets, no credential report, no manufactured completion, and rejection of a late event from a replaced start.

Use a fake clock to establish distinct remaining overall and handshake durations, pause longer than both, and assert that each resumes with exactly its own remainder. Also let a source first appear with less than 20 seconds left on the overall ceiling and prove the ceiling still wins. A source that first appears during a pause starts its handshake budget paused. For report lifetime, hold a decoded reply followed by non-streaming status: the same prompt stays open and read-only until a timely streaming row, absence, deadline, or status failure ends the report; then a later streaming row emits nothing. Repeat with Cancel during the command and during polling to prove late publication is suppressed without a receiver cancel command. The command test covers both pinned profiles: the plain profile omits both flags even when the start requested them, and the volume profile preserves both supplied choices and the final secret index.

## 8. Documentation updates

### README.md

Replace README's outdated pairing-capability statements. Replace the stale source-acquisition statement that treats a credential challenge as `dependency_incompatible` with the route-B behavior and its `credential_relay` gate. Keep unrelated activation prerequisites intact.

Use wording equivalent to:

> During an operator-started Mirror, a proven `credential_relay` profile can present a PIN/password prompt for the started target. The application reports success only when that target's status reports `streaming`.

Do not claim that the application stores the value, that a command response establishes streaming, or that status identifies why streaming was not reached.

### docs/backend-compatibility.md

Remove the statement that credential-bearing `connect` and `pin` forms are outside the application's secret-safe contract. Document the exact target-bound `connect <target> [--audio on|off] [--volume N] <credential>` mapping, the final argv secret index, the two pinned help profiles that prove `credential_relay`, and the `EvidenceAuthority` requirements. State that targetless `pin` remains unused and the four independent transaction flags remain false. Document §2.4's profile split: without proven `target_volume`, both flags are omitted and the original audio choice cannot be preserved on that profile; with it, the original audio and any requested initial volume are carried. Do not infer capability from the keyring selector or version text.

### docs/operations.md

Add exactly these five operator limits:

1. The command addresses a target value; a different receiver at that address may receive it.
2. The application reports its own target-specific status observation, not what appears on the display.
3. The application does not write the submitted value to application metadata; any receiver-side storage is outside the application's observation.
4. When the target does not report `streaming`, the application does not assign a reason.
5. The field disables duplicate actions from this dialog while its command is in flight; commands issued elsewhere are outside that UI control. Stop keeps its existing behavior.

## 9. Findings carried from the approved design and owner resolutions

The design owner resolves review B-01 in §3.1, B-02 in §2.1, B-03 in §2.2, D-01 in §4, E-01 in §5, E-02 in §3.2, E-03 in §2.4, and F-01 in the header. These resolutions govern the named conflicts in the pinned design; all other design decisions remain in force. The header pins the reviewed baseline, not a claim that this uncommitted revision already exists in that commit.

### Finding SHUTDOWN-01 — no typed discriminator

The pinned receiver returns shutdown as a free-form error string and an idle state. No typed field distinguishes shutdown from other decoded negative responses. This specification drops `ShuttingDown`, ignores the free-form error, and maps a decoded response followed by a terminal non-streaming observation to the generic message in §5. Status-read failures keep the existing transport failure path. No code may create a shutdown-specific diagnostic from receiver text.

### Finding MSG-01 — command no-reply wording, resolved

The design owner selects `The receiver did not answer.` only for `Ok(NoReply)` and `The mirror did not reach streaming in time.` for every terminal observed non-streaming outcome. This is the precedence for design §§6.4 and 12.7. Local `Err` and status-read failures remain on their existing diagnostic paths. No competing receiver-failure wording is retained in this specification.

### Finding MSG-02 — request disappears before submission, resolved

The design owner selects prompt closure and resumption of the same pending Start, with no pairing-specific message. `credential_request_ended` supplies the transport-to-session notification. The eventual Start result uses its existing path. This supersedes design §7's pre-submit disappearance wording; no receiver outcome is inferred and no credential completion is fabricated.

### Finding VIEW-01 — presentation view has richer fields than the request

The transport request supplies only target and kind, while the existing `ChallengeView` requires a local challenge ID and timestamp fields. This specification defines those as local presentation metadata, keeps the prompt identity in §3 authoritative, and makes `expires_at` optional because the receiver provides no expiry. The local challenge ID is never sent to the receiver.

### Finding CANCEL-01 — local resume notification

The approved behavior requires Cancel to dismiss the prompt and resume the pending start's suspended budget. `TransportPort` currently has no callback for that local transition. `dismiss_credential_request` in §2.2 is the required local-only notification; it performs no backend I/O and is covered by the deadline and prompt-lifecycle tests.
