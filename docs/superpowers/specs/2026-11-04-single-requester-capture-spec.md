# Single-requester screen capture — implementation specification

Status: draft for review. Scope: **Mirror only**. No backend (`doubletake`) change, no fork rebuild, no
restore-token pass-through.

## 1. Problem

The dashboard offers a "Physical monitor" dropdown that cannot select anything. `SelectSources` has no
monitor parameter; the portal decides what to capture from its own permission store, and the backend opens
a **second**, independent ScreenCast session whose remembered approval is what actually reaches the
receiver. On this host the two requests were replayed as different monitors from the same store (the app
got DP-3, the daemon DP-1), so the verifier's connector-equality rule could only ever refuse.

The dropdown therefore has two possible meanings — "use the screen the desktop remembers" and "mirror this
specific screen" — and implements neither.

## 2. Decisions

| # | Decision |
| --- | --- |
| D1 | The backend is the **sole** ScreenCast requester in Mirror. The application opens no capture session of its own. |
| D2 | Remembered capture state is **never consumed**. Before every user-initiated Start the application removes the desktop's ScreenCast approvals, so the portal must ask. |
| D3 | The application **adopts** the monitor the backend actually captured, subject to the physical-origin guard. Connector equality between request and report is dropped. |
| D4 | Two-phase admission wait: the permission phase is unbounded but cancellable; the handshake phase stays bounded. |
| D5 | The pane reports the captured screen. Mirror has no source selector. |

The manual revocation control added in `32cc6ab` is **removed**: with D2 it duplicates what Start already
does, and a toolbar action that only repeats an automatic step is the same kind of false affordance as the
source selector. Revocation *without* starting is not offered; if it is ever wanted it belongs in Settings,
not in the operating surface.

Rejected, with reasons:

- **Token pass-through (A).** Needs a backend flag, a control-protocol field, a new capability token and a
  Go 1.25 rebuild; and it only works while neither process has a portal app id, because tokens are scoped
  per app id.
- **"Last captured" memory.** A proxy for the remembered approval, not the approval itself; it can skip the
  clear and land on a different monitor.
- **Two modes / "Advanced Mirror" tab.** D2 collapses both into one behaviour; a second mode would differ
  only in an invisible detail.
- **Clearing only when the pick differs.** Undecidable — the application cannot attribute store rows to the
  backend.

## 3. Contracts

### 3.1 Start request and presenter

- `StartRequest(mode=Mode.MIRROR, source=None, audio=…)` is the only Mirror start. `resources.py` already
  permits `source=None`; `Mode.MIRROR` no longer implies a `PhysicalMonitorSource`.
- `Presenter._start_request` returns that request for `Action.start` in Mirror without consulting
  `_source_for`; a missing source list is no longer a rejection reason.
- Before dispatching that Start, and only for `Action.start` offered by the pane in Mirror:
  1. `store.forget_all()` — the existing `FlatpakScreenCastPermissions`;
  2. on `Err`, record `screen.forget_failed` and reject the Start with `dependency_missing` or
     `persistence_failed` (the store's own code); **nothing is dispatched**;
  3. on `Ok`, record `screen.forget` with the removed count and dispatch.
- `Action.retry`, reconnect, audio retry and cleanup retry **never** clear. They are not new user decisions.
- Consequence to state in `docs/operations.md`: the dashboard is the only actor that performs Mirror
  starts; a headless composition does not clear anything.

### 3.2 Session actor

`session/mirror.py` loses the capture acquisition steps:

- `_start` drops the `isinstance(request.source, PhysicalMonitorSource)` requirement and the
  `capture_select` / `capture_verify` launches.
- `_begin_stream` calls `transport.start_stream(target, None, policy, context)`.
- `_start_after_trust` no longer requires `ctx.state.physical_source`; the pairing continuation starts the
  stream directly.
- A Mirror session has no capture source to wait for, so `ReconnectLifecycle._has_source` no longer
  requires one: a desktop capture is startable and reconnectable without a local lease.
- `physical_source` is applied from the admitted handle after `transport_start` succeeds:
  `PhysicalMonitorSource(connector=handle.source_connector, portal_restore_token=None,
  capture_lease_id=None)`, only when `handle.source_connector is not None`.
- `PhysicalCaptureProvider` remains composed and reachable but is no longer exercised by Mirror. Its
  `select`/`verify`/`release` stay for a future token path.

### 3.3 Transport and verifier (adopt mode)

- `TransportPort.start_stream` accepts `source: CaptureSource | None`, where `None` means "mirror whatever
  the desktop grants". This is a **frozen-file amendment** (`domain/ports.py`); the alternative — a new
  `CaptureSource` variant — is the same amendment with a larger surface, so `None` is preferred.
- With `source is None` the transport:
  - skips the pre-connect `capture.verify`;
  - always performs reported-source verification after the daemon reports streaming;
  - records `stream.source_connector` from the observation (the field already exists on `StreamHandle`).
- `verify_reported_source(requested, …)` gains adopt mode: `requested=None` skips
  `INVALID_REQUESTED_CONNECTOR` and `DIFFERENT_CONNECTOR` only. Every other rule is unchanged — unique
  enabled position match, size corroboration (`native = logical × scale`), physical connector type,
  connected, not virtual, not helper-owned, not in a durable ownership record.
- `SourceRefusal.DIFFERENT_CONNECTOR` is retained for the comparison path and unused by Mirror.

### 3.4 Two-phase admission wait

Replaces `_await_connecting_stream`'s single budget. Poll `status` every `SOURCE_POLL_INTERVAL`.

| Phase | Entry condition | Bound | Outcome |
| --- | --- | --- | --- |
| A — permission | a matching stream exists without `source` | `PERMISSION_WAIT_TIMEOUT` (300 s); cancelled by abandonment, target replacement, daemon loss, shutdown | credential prompt → `dependency_incompatible`; `source` appears → phase B |
| B — handshake | a matching stream carries `source` while not `streaming` | `CONNECT_STREAMING_TIMEOUT` (20 s) from the moment `source` appears | `streaming` → verification; expiry → `unsupported_source` |
| C — never accepted | no stream for the target has ever appeared | `CONNECT_STREAMING_TIMEOUT` (20 s) | expiry → `unsupported_source` |

- Phase A is the desktop's prompt. The daemon publishes the connecting stream before it has a capture
  source, which is exactly the observable that distinguishes a pending prompt from a failed start.
- A stream that disappeared after being seen fails at once: the backend dropped the capture.
- One unanswered `status` read is normal while the backend is blocked on the prompt;
  `STATUS_FAILURE_BUDGET` (3 consecutive) bounds a control that cannot answer at all.
- No new `ErrorCode` is introduced: every phase reuses `unsupported_source`.

### 3.5 Presentation

- Mirror pane: no `Physical monitor` selector. `ViewInputs.physical_sources` is unused in Mirror (kept for
  Virtual's resolution inputs and for `list_physical`).
- `source_enabled` is false in Mirror; `Action.start` is offered whenever the session is startable.
- After the stream is admitted the pane shows `Captured: <connector>` from `snapshot.physical_source`.
- While a Mirror start is connecting with no source yet, the pane says `Waiting for the screen prompt…`:
  the dialog belongs to the portal, so naming the phase is all this application can do.
- A guard failure keeps its fixed catalog text plus the typed `detail` already carried
  (`ambiguous_position`, `size_mismatch`, `owned_output`, …) so the reason is diagnosable.

## 4. Ordering

```text
Idle --Start--> [clear approvals] --Ok--> dispatch Start
                                   --Err-> Idle + rejection, nothing dispatched
Start -> Connect(receiver) -> daemon connect -> phase A (permission) -> phase B (handshake)
      -> reported-source adopt+guard -> Streaming + "Captured: X"
```

Clearing happens once, before dispatch. Nothing clears while a stream exists: the backend's approval must
stay valid for the life of the session so a reconnect does not silently lose its capture source.

## 5. Error contract

| Condition | Code | Side effect |
| --- | --- | --- |
| store cannot be read / `flatpak` missing | `dependency_missing` | Start refused, nothing dispatched, activity `screen.forget_failed` |
| removal rejected by the store | `persistence_failed` | same |
| permission phase cancelled (Stop/Cancel) | `stale_event` or the cancel path's own code | ordered cleanup, no signal left |
| daemon reports a credential prompt | `dependency_incompatible` | ordered cleanup, `Pair` remains disabled |
| handshake exceeds 20 s | `unsupported_source` | ordered cleanup |
| reported source fails the guard | `unsupported_source` + typed `detail` | ordered cleanup |
| target replaced, daemon lost | `stale_event` | ordered cleanup |

## 6. Invariants

1. **No capture request before Start.** A fresh composition — including capability probing — performs zero
   ScreenCast calls. Instrumented (`PortalClient.select_monitor`/`_open_peer`, fresh state root): no request
   at launch. The owned daemon does start with the application, because it is the control plane; it only
   asks the portal when a `connect` arrives. A launch-time dialog seen during triage came from a client
   that had left a request pending, not from composition.
2. **One requester.** No code path may open a ScreenCast session for Mirror other than the backend.
3. **One prompt per Start**, and only one: the user answers the portal dialog once.
4. **No remembered state is consumed**: whatever the store holds is voided before the next Start.
5. The physical-origin guard is never relaxed: a window, a region, a helper-owned virtual output, a
   disconnected output, an ambiguous position or a corroboration failure is refused.

## 7. Testing

Deterministic fakes only; no real receiver, no real portal.

- `tests/capture/test_reported_source.py`
  - `test_adopt_mode_accepts_a_unique_physical_output`
  - `test_adopt_mode_still_refuses_ambiguous_position`
  - `test_adopt_mode_still_refuses_virtual_and_owned_outputs`
  - `test_adopt_mode_still_refuses_disconnected_and_unknown_types`
  - `test_adopt_mode_still_corroborates_the_current_mode_size`
- `tests/transport/test_doubletake.py`
  - `test_desktop_capture_adopts_the_reported_connector`
  - `test_desktop_capture_waits_past_the_connect_budget_for_permission`
  - `test_desktop_capture_handshake_expires_after_the_stream_appears`
  - `test_desktop_capture_prompt_refuses_immediately`
  - `test_desktop_capture_still_refuses_a_non_physical_output`
- `tests/ui/test_presenter.py`
  - `test_mirror_start_clears_the_remembered_screen_first`
  - `test_mirror_retry_does_not_clear_the_remembered_screen`
  - `test_mirror_start_refuses_when_the_store_cannot_be_cleared`
  - `test_mirror_pane_offers_no_source_selector`
  - `test_mirror_pane_reports_the_captured_connector`
- `tests/app/test_live_lifecycle.py`
  - `test_no_capture_request_before_start`

Acceptance (manual, on the real receiver): press Start → exactly one Screen Sharing dialog → pick DP-1 →
the TV shows DP-1 and the pane reads `Captured: DP-1`; press Stop then Start again → the dialog returns;
the store holds no approval the application relied on.

## 8. Non-goals

- Making the dashboard's selector authoritative (that is the rejected A).
- Any `doubletake` fork change, rebuild, or capability-token bump.
- Remembering the last captured screen, or any new durable state.
- Virtual display, pairing/PIN submission, and multi-receiver capture.

## 9. Open questions

1. `domain/ports.py` and `domain/resources.py` are recorded as frozen. D3 needs the `start_stream` signature
   amendment and no `ErrorCode` change; approval of that amendment is a prerequisite for §3.3.
2. Whether the portal writes an approval row even when the user leaves KDE's checkbox unticked. If it does,
   D2 still holds (the row is voided before the next Start) but the store is not literally empty between
   sessions.
3. Multi-receiver: two receivers starting Mirror concurrently in phase A means two prompts. Acceptable, or
   should starts serialize behind one prompt?
