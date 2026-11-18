# GUI Device Pairing (Route B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Let an operator answer a PIN or password request from the receiver started by a Mirror operation, then report only the target-specific status observation permitted by the approved route-B specification.

**Architecture:** Four lanes separate observer redaction, transport contracts and readiness waits, session/UI prompt ownership, and integrated acceptance plus documentation. Tasks 1 and 2 are independent and may run together; Task 3 depends on Task 2; Task 4 depends on Tasks 1–3. Maximum parallelism is two lanes.

**Tech Stack:** Python, asyncio, PySide6, pytest, and the existing doubletake process/control fakes.

## Global Constraints

- Implement the approved route-B specification at `docs/superpowers/specs/2026-11-15-gui-device-pairing-spec.md` and its approved design at `docs/superpowers/specs/2026-11-15-gui-device-pairing-design.md`; the design's epoch-2 scope reset supersedes its earlier relay design.
- The feature applies only to Mirror mode during an operator-started mirror. Report success only when the started target's status reports `streaming` within `CONNECT_STREAMING_TIMEOUT` of the decoded command reply.
- Do not introduce the withdrawn relay coordination, settlement, ordering, release, or trust model. The route-B path does not call the transactional pairing coordinator or credential store and does not alter `TrustState` or trust generation.
- Keep the established transactional pairing path byte-compatible for profiles that prove its transaction capabilities. Preserve its existing command mapping, payload serialization, secret indices, and transaction tests.
- Keep `credential_relay` independent from `keyring`, `target_bound_pairing`, `provisional_commit`, `cancel_pairing`, and `revoke_pairing`. Those four transaction flags and `target_bound_pairing` stay false on the help-derived `probed` profile.
- Runtime creates a separate `composed` capability object. Public Start admission continues to require composed `owned_daemon_control` and credential-proof-derived `keyring`; submit authorization requires exactly `credential_relay`, `owned_daemon_control`, `multi_target`, and `physical_source`.
- Match a request only from exactly one per-stream status row for the started target in `pin_required` state with typed kind `pin` or `password`; ignore aggregate credential fields.
- Build `connect <target> [--audio on|off] [--volume N] <credential>` with the credential at `secret_indices = (len(argv) - 1,)`. When `target_volume` is unproven, omit both option flags; when proven, preserve the original audio choice and any requested initial volume. Never emit targetless `pin`.
- Validate input before command construction or `submit_credential`: non-empty, no NUL, encodable as one argv item, no leading dash, and exactly four ASCII digits for a PIN. Do not trim or normalize it.
- Distinguish a local `Err` from `Ok(Submitted(reply_at=...))` and `Ok(NoReply())`. A decoded negative command reply is `Submitted`; only `NoReply` skips status polling. Do not infer a shutdown reason from free-form receiver text.
- Retained observers redact at creation using `CommandSpec.secret_indices`. No retained value, callback, repr, diagnostic, activity record, command/process journal, probe, acceptance record, export, or failing assertion may contain the submitted credential.
- Add exactly the five operator limits from specification §8 to `docs/operations.md`; do not add extra operator-limit claims.
- Run every validation command from its lane worktree with the explicit interpreter shown and a lane-specific `--basetemp`. Validation argv contains no shell metacharacters or git verbs. Do not include host power actions or session-terminating commands.

## Task 1: Close Observer Retention Paths

**Implementer tier:** Advanced
**Lane:** redaction
**Depends on:** none
**Owns:** `src/airplay_linux/testing/adapters.py`, `tests/testing/test_fakes.py`, `tests/process/test_credential_probe.py`, `tests/acceptance/test_no_production_spawn.py`, `tests/fixtures/processes/fake_doubletake.py`, `tests/adapter_contracts/test_keyring_contract.py`
**Exclusive resources:** `/tmp/ap-relay-redaction`
**Interfaces produced:** No production interface; retained process and request observations remain secret-free projections.
**Interfaces consumed:** `CommandSpec.secret_indices` and the existing redacted argv projection.
**Validation:** `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest -q tests/testing/test_fakes.py tests/process/test_credential_probe.py tests/acceptance/test_no_production_spawn.py tests/adapter_contracts/test_keyring_contract.py --basetemp=/tmp/ap-relay-redaction`

### Files

- Modify `src/airplay_linux/testing/adapters.py` to protect `FakeProcess` and `ProbeProcessRecorder` retained state and deferred adoption callbacks.
- Modify `tests/acceptance/test_no_production_spawn.py` so acceptance recorder argv is projected before retention.
- Modify `tests/fixtures/processes/fake_doubletake.py` and `tests/adapter_contracts/test_keyring_contract.py` so fake daemon request captures redact credential fields before writing JSONL.
- Add or update focused regressions in `tests/testing/test_fakes.py` and `tests/process/test_credential_probe.py`.

### Required review closure

- Close observer review F-1: a cancelled fake spawn must not leave a callback in `Deferred._callbacks` that captures raw `CommandSpec` or its canary. Keep only the redacted command projection and allowlisted launch metadata needed to adopt the fake identity.
- Close F-2: exercise `connect T --audio <CANARY> --unknown trailing`; an unvalidated option operand must not be retained as harmless argv when `credential_present` is false. Cover the `ctl-argv-env.jsonl`, `ctl-requests.jsonl`, `daemon-requests.jsonl`, and journal projections.
- Close F-3: daemon request capture must redact credential-bearing payload fields before serialization, including unexpected credential fields in an otherwise captured request.
- Close F-4: a deliberately failing redaction assertion must not print the canary through pytest assertion introspection or a custom failure message.

### Steps

- [ ] Add unique-canary regressions for cancelled spawn adoption, each retained argv/request/journal path, and safe failure output; confirm each regression exposes its named review defect.
- [ ] Change `FakeProcess` and both process recorders to store redacted projections at the retention boundary; ensure deferred callbacks do not retain raw command objects.
- [ ] Harden fake control and daemon capture parsing so ambiguous or unknown argument shapes and credential payload fields are never retained as ordinary values.
- [ ] Re-run the canary probes with positive controls proving the fake command and request really occurred; ensure assertion output reports only safe labels and booleans.
- [ ] Run the lane validation command and commit the completed observer-redaction slice.

### Tests

- Review F-1 through F-4 regressions above.
- Specification §6 redaction and §7 `test_credential_canary_is_absent_from_all_retained_observers` observer cases.
- Preserve existing no-production-spawn recorder classifications and credential-probe witness behavior.

### Commit message

`fix: close credential observer retention paths`

## Task 2: Implement Route-B Transport Contracts and Readiness Waits

**Implementer tier:** Capable
**Lane:** transport
**Depends on:** none
**Owns:** `src/airplay_linux/backend/contract.py`, `src/airplay_linux/backend/control.py`, `src/airplay_linux/domain/messages.py`, `src/airplay_linux/domain/operations.py`, `src/airplay_linux/domain/ports.py`, `src/airplay_linux/process/ownership.py`, `src/airplay_linux/runtime.py`, `src/airplay_linux/transport/doubletake.py`, `tests/backend/test_credential_relay.py`, `tests/backend/test_control.py`, `tests/backend/test_contract.py`, `tests/transport/test_doubletake.py`, `tests/adapter_contracts/test_transport_contract.py`, `tests/runtime/test_capability_report.py`, `tests/runtime/test_owned_probe.py`, `tests/app/test_composition.py`
**Exclusive resources:** `/tmp/ap-relay-transport`
**Interfaces produced:** `src/airplay_linux/domain/messages.py`: `CredentialKind = Literal["pin", "password"]`, `CredentialRequest(target: ReceiverTarget, kind: CredentialKind)`, `CredentialRequestEnded(target: ReceiverTarget, kind: CredentialKind)`; `src/airplay_linux/domain/operations.py`: `CredentialOutcome = Submitted(reply_at: float) | NoReply()`; `TransportPort.submit_credential(target: ReceiverTarget, value: SensitiveText) -> Result[CredentialOutcome]`; `TransportPort.read_status(target: ReceiverTarget, context: OperationContext) -> Result[StatusReply]`; `TransportPort.dismiss_credential_request(target: ReceiverTarget, context: OperationContext) -> None`; stream-less request/end events on the existing `TransportEvent` envelope.
**Interfaces consumed:** Existing `ReceiverTarget`, `OperationContext`, `SensitiveText`, `StatusReply`, `Result`, `TransportEvent`, `ProfileCommand`, and `CommandSpec.secret_indices` contracts.
**Validation:** `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest -q tests/backend/test_credential_relay.py tests/backend/test_control.py tests/backend/test_contract.py tests/transport/test_doubletake.py tests/adapter_contracts/test_transport_contract.py tests/runtime/test_capability_report.py tests/runtime/test_owned_probe.py tests/app/test_composition.py --basetemp=/tmp/ap-relay-transport`

### Files

- Modify backend feature derivation, authorization, command construction, domain message/operation/port types, runtime capability composition, and `DoubletakeTransport` in the paths listed under **Owns**.
- Add or update transport/backend contract tests in the listed test paths. Keep task-3 session routing and UI files out of this lane.

### Required behavior

- Derive `credential_relay` only from either exact pinned control signature table plus its verified sender surface and consistency checks. Version text, keyring configuration, prose, partial signatures, or a `pin` token alone prove nothing.
- Keep transaction flags and `target_bound_pairing` false on `probed`; compose a separate capability object without mutating it. Keep public Start admission on the composed `owned_daemon_control and keyring` gate. Keep submit authorization on exactly the four route-B facts in Global Constraints.
- Make `PublishedDoubletakeProfile` build the exact target-bound `connect` form. Preserve the final argv secret index. Carry both original audio and requested volume flags only when `target_volume` is proven; otherwise omit both. Refuse an option-shaped credential and never build targetless `pin`.
- Emit `credential_request` only from exactly one matching per-stream `pin_required` row with a valid typed kind. Ignore aggregate status. Emit `credential_request_ended` for the latched first kind only when a successful status observation no longer has exactly one valid matching row; a status error is not request end. Neither event resolves Start or invents a stream handle.
- Add `submit_credential`, `read_status`, and the local-only `dismiss_credential_request` port operations. A decoded positive or negative response becomes `Submitted` with monotonic `reply_at`; a launched command with no complete decoded response becomes `NoReply`; local failures remain sanitized `Err` and never start status polling.
- Suspend the existing permission and handshake readiness budgets while the request awaits an operator, preserving each remaining duration independently. Resume once on submission, request end, local dismissal, or start replacement. If a source first appears during the pause, initialize its handshake budget paused. Do not add timers, reset expired budgets, or let request disappearance complete Start.

### Steps

- [ ] Add §7 contract tests for pinned capability proof, submit authorization, exact command options and secret index, matching-row request detection, local-error versus receiver-outcome mapping, and independent readiness-budget suspension.
- [ ] Implement the request and request-ended payloads and event kinds with only the specified target/kind payload fields and existing start identity envelope.
- [ ] Implement exact profile composition, command mapping, result classification, status read, and local dismissal notification while preserving transactional profiles' existing byte-compatible path.
- [ ] Add the event detection and latched-kind end behavior to `_await_connecting_stream`; add independent save/restore handling for the two existing readiness deadlines.
- [ ] Run the lane validation command and commit the transport slice.

### Tests

- Specification §7: `test_credential_relay_is_proven_only_by_both_pinned_control_tables`, `test_submit_pairing_authority_uses_credential_relay_branch`, `test_connect_command_preserves_target_audio_volume_and_secret_index`, `test_credential_request_requires_matching_stream_target`, `test_submit_credential_distinguishes_local_err_from_receiver_outcomes`, and `test_route_b_suspends_and_restores_overall_and_handshake_budgets`.
- Preserve existing capability-report, source/ownership admission, and transaction contract tests; assert route-B capability does not alter transaction flags or transactional command bytes.

### Commit message

`feat: add target-bound credential transport`

## Task 3: Integrate Prompt Identity, Session Lifecycle, and UI Reporting

**Implementer tier:** Capable
**Lane:** session-ui
**Depends on:** Task 2
**Owns:** `src/airplay_linux/domain/messages.py`, `src/airplay_linux/domain/records.py`, `src/airplay_linux/domain/relay.py`, `src/airplay_linux/session/cleanup.py`, `src/airplay_linux/session/engine.py`, `src/airplay_linux/session/mirror.py`, `src/airplay_linux/session/pairing.py`, `src/airplay_linux/session/reconnect.py`, `src/airplay_linux/ui/state.py`, `src/airplay_linux/ui/presenter.py`, `src/airplay_linux/ui/dialogs.py`, `src/airplay_linux/runtime.py`, `tests/domain/test_credential_relay.py`, `tests/session/test_pairing_integration.py`, `tests/session/test_engine.py`, `tests/session/test_reconnect.py`, `tests/session/test_stop_matrix.py`, `tests/session/test_shutdown.py`, `tests/session/test_shutdown_drain.py`, `tests/session/test_lifecycle_regressions.py`, `tests/session/test_fencing.py`, `tests/session/test_mirror_and_trust.py`, `tests/ui/test_state_projection.py`, `tests/ui/test_presenter.py`, `tests/ui/test_dashboard_widgets.py`, `tests/trust/test_pairing_transactions.py`
**Exclusive resources:** `/tmp/ap-relay-session-ui`
**Interfaces produced:** `DismissRouteBPrompt(receiver_id: ReceiverId, start_operation: OperationId, prompt_key: ChallengeId)` on the `Command` union; `SessionSnapshot.route_b_request: CredentialRequest | None`; `SessionSnapshot.route_b_submission_pending: bool`; `PaneState.route_b_prompt: bool`; `PaneState.route_b_submission_pending: bool`; target-addressed prompt, submission, report, and retirement behavior.
**Interfaces consumed:** Task 2's `CredentialRequest`, `CredentialRequestEnded`, `CredentialOutcome`, `TransportPort` operations, and stream-less transport events.
**Validation:** `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest -q tests/domain/test_credential_relay.py tests/session/test_pairing_integration.py tests/session/test_engine.py tests/session/test_reconnect.py tests/session/test_stop_matrix.py tests/session/test_shutdown.py tests/session/test_shutdown_drain.py tests/session/test_lifecycle_regressions.py tests/session/test_fencing.py tests/ui/test_state_projection.py tests/ui/test_presenter.py tests/ui/test_dashboard_widgets.py tests/trust/test_pairing_transactions.py --basetemp=/tmp/ap-relay-session-ui`

### Files

- Modify session routing, event admission, `OrderedCleanup`, Mirror/reconnect lifecycle, projection, presenter, dialog, runtime handler composition, and route-B domain state in the paths listed under **Owns**.
- Replace route-A-only session projection and coordination records with the specification's route-B marker and prompt identity. Keep the independent process termination-evidence capability used by the daemon registry unchanged and unused by this feature.
- Add the identity-bearing dismiss command and dedicated `Route.route_b_prompt` handler; remove obsolete relay-only message/state types once their consumers have moved to route B.

### Required behavior

- Build prompt identity as `(receiver_id, start_operation_id, first_observed_credential_kind)`. Add the exact §3.1 `_route_b_prompt_live` predicate and project/open the prompt for its receiver even when another pane is selected. Keep the same challenge through command and report; set `expires_at=None` and omit expiry text.
- Validate before command construction or transport dispatch. The route-B dialog exposes Submit and Cancel; after valid dispatch it remains open and read-only with Submit disabled. Do not call `PairingPort`, change trust, or access the credential store.
- For `Submitted`, poll the started target's status until that target reports `streaming` before the post-command deadline or the deadline expires. Use only `Connected to <TV>.` for timely streaming, `The mirror did not reach streaming in time.` for terminal non-streaming observations, and `The receiver did not answer.` for `NoReply`. Preserve existing local and status-read diagnostics. Publish once, then close; a later observation cannot revise the report.
- Admit both events through the stream-less transport branch only when receiver, active Mirror start, session generation, endpoint generation and fingerprint, daemon instance, canonical target, and payload target match; for request-ended, also match the latched first kind. `ReconnectLifecycle` passes them to the Mirror handler before network-loss or target-sync handling; neither event installs a stream or alters readiness.
- Add `Route.route_b_prompt` to `_route` and register `MirrorLifecycle.route_b_prompt` through lifecycle factories and runtime handler composition. The handler owns the same prompt/report identity through submission and closure.
- Route request-end to prompt closure only before submission; keep the read-only reporting prompt open if it arrives during submission/reporting. Dismissal is local, sends no receiver command, resumes only the captured start's wait, and retires report publication before any late result can publish.
- Recheck receiver, start operation, request/view identity, prompt key, session and endpoint generations at public command admission and actor delivery. Stale dismissals return/discard as `stale_event` without state changes, effects, error publication, or notification.
- Classify stored route-B provenance before transactional handling during Stop cleanup, discovery invalidation, accepted start replacement, and shutdown. Retire and notify using the captured old target/context before the enclosing operation proceeds. The route-B view never enters transactional cancellation or changes trust. Preserve the established transactional pairing branch and its byte-compatible behavior.

### Steps

- [ ] Add §7 regressions for route-B projection without pairing trust, unselected receiver prompt ownership, pending read-only state, request-end behavior, stale dismiss admission, dismissal during command/poll, and lifecycle classification before cleanup.
- [ ] Add the dedicated dismiss command/route and public plus actor admission checks; capture the original target/context and retire publication authority synchronously before notifying transport.
- [ ] Replace the session's route-A relay view with the route-B request marker and pending flag; classify that stored provenance before every transactional challenge branch in Stop, discovery, replacement, and shutdown.
- [ ] Implement local input validation, route-B submission and bounded status reporting, then wire the exact prompt actions and field clearing into presenter/dialog lifecycle.
- [ ] Preserve and run transactional pairing regressions, run the lane validation command, and commit the session/UI slice.

### Tests

- Specification §7: `test_route_b_prompt_is_projected_and_admitted_without_pairing_trust`, `test_prompt_tracks_start_receiver_not_selected_pane`, `test_route_b_prompt_remains_read_only_until_terminal_report`, `test_credential_request_ended_closes_prompt_without_completing_start`, `test_dismiss_route_b_prompt_is_local_and_identity_admitted`, `test_stale_route_b_dismiss_after_start_replacement_changes_nothing`, `test_route_b_view_does_not_change_lifecycle_trust_or_cleanup`, and `test_invalid_credential_is_rejected_before_command_construction`.
- Keep the transaction control fixture in lifecycle tests and `tests/trust/test_pairing_transactions.py` byte-compatible.

### Commit message

`feat: integrate route-b pairing prompt lifecycle`

## Task 4: Prove Acceptance, Canary Coverage, Mutations, and Documentation

**Implementer tier:** Advanced
**Lane:** acceptance-docs
**Depends on:** Tasks 1, 2, and 3
**Owns:** `tests/acceptance/test_credential_relay.py`, `tests/ui_acceptance/test_multireceiver_flow.py`, `README.md`, `docs/backend-compatibility.md`, `docs/operations.md`
**Exclusive resources:** `/tmp/ap-relay-acceptance`
**Interfaces produced:** No new production interface; this lane proves the composed public Start-to-report behavior and publishes the approved operator documentation.
**Interfaces consumed:** Task 1's redacted observers, Task 2's transport/capability contracts, and Task 3's admitted prompt/report lifecycle.
**Validation:** `/data/home/guest/Development/airplay-linux/.venv/bin/python -m pytest -q tests/acceptance tests/ui_acceptance --basetemp=/tmp/ap-relay-acceptance`

### Files

- Add integrated route-B acceptance and canary coverage under `tests/acceptance/test_credential_relay.py`; use Task 1's safe recorder without changing its ownership or retention implementation.
- Update the three documentation files listed under **Owns** and no other documentation artifact.

### Required behavior

- Drive the real runtime composition and public operator Start through fake process/control, launcher, credential-proof, inventory, discovery, and ownership boundaries. Prove the complete positive composed path reaches a matching request and projected prompt without a production spawn, transactional pairing call, or credential-store call.
- Include independent negative controls for each §7 R2-03 evidence gate, invalid ownership/receipt proof, incomplete source reporting/verifier/inventory, `multi_target`, stale target/endpoint, foreign or ambiguous rows, and each failed or unrun credential-proof leg. Keep other evidence positive in each control.
- Add `test_credential_canary_is_absent_from_all_retained_observers` with one probe per retained observer, including a deliberate failing-assertion control that proves failure output contains no canary.
- Implement a fake-daemon reporting test for every observation row in specification §5. Do not invent a required function identifier where the specification supplies none.
- Run every mutation probe in specification §7 against a clean passing baseline in a disposable copy; record the specified killing test for each mutation, including one-observer-at-a-time redaction, target equality, representability, secret index, profile flags, report status read, lifecycle fencing, and deadline mutations.
- In `README.md`, replace the stale pairing-capability and source-acquisition claims with wording equivalent to: “During an operator-started Mirror, a proven `credential_relay` profile can present a PIN/password prompt for the started target. The application reports success only when that target's status reports `streaming`.” Keep unrelated composed Start prerequisites intact.
- In `docs/backend-compatibility.md`, document the target-bound command, final secret index, both pinned help profiles, exact authorization facts, the `probed`/`composed` capability distinction, the independent credential-proof-derived composed keyring gate, and the `target_volume` option split. State that targetless `pin` remains unused and capability is not inferred from a keyring selector or version text.
- In `docs/operations.md`, add exactly these five operator limits and preserve Stop's existing behavior:
  1. The command addresses a target value; a different receiver at that address may receive it.
  2. The application reports its own target-specific status observation, not the display.
  3. The application does not write the submitted value to metadata; receiver-side storage is outside the application's observation.
  4. A target that does not report `streaming` receives no assigned reason.
  5. The field disables duplicate actions from this dialog while in flight; commands issued elsewhere are outside UI control.

### Steps

- [ ] Add the public composition acceptance named by §7 and prove the positive route plus each independent negative control uses the real gates rather than manually injected request or forced capability state.
- [ ] Add the complete retained-observer canary suite and safe failing-assertion control; verify each boundary has a positive control showing the secret-bearing action really occurred.
- [ ] Add one fake-daemon case per §5 reporting row and the exact §7 acceptance sequence; verify prompt closure and no production spawn.
- [ ] Execute the §7 mutation inventory in disposable copies from a clean passing baseline and record the named killer for every mutation.
- [ ] Update `README.md`, `docs/backend-compatibility.md`, and `docs/operations.md` to the exact route-B claims; verify the operations section adds exactly five operator limits.
- [ ] Run the full lane validation command and commit the integrated acceptance/documentation slice.

### Tests

- Specification §7: `test_composed_mirror_start_reaches_credential_request_with_evidence_controls`, `test_credential_canary_is_absent_from_all_retained_observers`, and `test_mirror_credential_request_submit_and_report_without_production_spawn`.
- Specification §7 Reporting: one fake-daemon test per §5 observation row; the spec does not provide a required function name for this row.
- Run the complete acceptance and UI acceptance suites plus all §7 mutation probes; do not invoke live receiver/KDE checks or production process spawns.

### Known specification gap

Specification §7 requires one fake-daemon test for every §5 reporting observation row but does not name a required reporting test function. Preserve the row-by-row coverage requirement and report this missing identifier; do not invent one as a specification contract.

### Commit message

`test: prove route-b pairing acceptance and document limits`
