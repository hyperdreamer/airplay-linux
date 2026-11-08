# X11 mirror support - implementation specification

Authority: `docs/superpowers/specs/2026-11-06-x11-support-design.md`, approved revision 4 at commit `cbe95c4a71692876abea9520a03c0e7c6bb6a1e7`.

Paths in this specification are repository-relative. Citations point to the current worktree and identify the existing declaration, behavior, fixture, or test to extend; new declarations and tests do not yet have source line numbers.

## 1. Scope

Goal: admit portal-backed Mirror on KDE Plasma 6 X11 only when the same backend, monitor inventory, and source-verification proofs used by Wayland are present; report the session by a human-readable label; and give Virtual display on X11 a specific pane reason. Keep KDE Plasma 6 Wayland behavior and all existing evidence requirements intact. The feature is not accepted until the seven-step live run in section 11 succeeds against the real receiver.

Non-goals: support other X11 desktops or KDE Plasma 5; enable Virtual display on X11; add direct X11 capture or another capture fallback; change doubletake capability derivation or any capture/source-verification rule; make `--check-environment` a production capture-capability report; or extend multi-output behavior beyond the current verifier.

## 2. Session classification

Extend `DesktopEnvironment` with exactly these members and tokens, preserving every existing member: `KDE_PLASMA_6_X11 = "kde-plasma-6-x11"`, `KDE_PLASMA_5_X11 = "kde-plasma-5-x11"`, and `KDE_X11_UNVERIFIED = "kde-x11-unverified"`. The current enum and classifier are at `src/airplay_linux/policy/capabilities.py:79` and `src/airplay_linux/policy/capabilities.py:131`.

Read only `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`, and `KDE_SESSION_VERSION` for classification. For each value, use the existing `_text` rule: accept an exact built-in `str`; for any other value, including `None`, numbers, objects, and string subclasses, use empty text without coercion. `_text` is at `src/airplay_linux/policy/capabilities.py:126`.

Normalization is exact:

- `XDG_SESSION_TYPE`: strip surrounding whitespace, then lowercase. Do not colon-split it.
- `XDG_CURRENT_DESKTOP`: split on `:`, strip and lowercase each token, discard empty tokens, then compare whole tokens. The effective desktop class is KDE/Plasma if any token is exactly `kde` or `plasma`; otherwise GNOME if any token is exactly `gnome`; otherwise other. KDE/Plasma has precedence if both KDE/Plasma and GNOME tokens occur.
- `KDE_SESSION_VERSION`: strip surrounding whitespace; do not lowercase or parse numerically. Only the exact resulting strings `"5"` and `"6"` are verified versions. Every other value, including empty, non-string, `"7"`, `"06"`, and unparseable text, is unverified.

Branch precedence is X11 first, Wayland second, and all other session types last. A normalized X11 session is classified from its desktop/version and never falls through to Wayland handling. A session other than normalized `x11` or `wayland` is `unknown`, regardless of desktop/version. KDE/Plasma version matching is performed only for KDE/Plasma desktop class. GNOME is recognized only in the Wayland branch. A non-Mapping argument remains a `TypeError` as in `classify_environment` at `src/airplay_linux/policy/capabilities.py:131`.

The following is the complete truth table over the normalized categories. It covers all 27 combinations (9 desktop/version rows by 3 session columns). `other/empty` includes non-string data after the stated normalization.

| Effective desktop class | Normalized version | Session `x11` | Session `wayland` | Other/empty session |
| --- | --- | --- | --- | --- |
| KDE/Plasma | `6` | `kde-plasma-6-x11` | `kde-plasma-6-wayland` | `unknown` |
| KDE/Plasma | `5` | `kde-plasma-5-x11` | `kde-plasma-5-wayland` | `unknown` |
| KDE/Plasma | other/empty | `kde-x11-unverified` | `kde-wayland-unverified` | `unknown` |
| GNOME | `6` | `x11` | `gnome-wayland` | `unknown` |
| GNOME | `5` | `x11` | `gnome-wayland` | `unknown` |
| GNOME | other/empty | `x11` | `gnome-wayland` | `unknown` |
| Other | `6` | `x11` | `unknown` | `unknown` |
| Other | `5` | `x11` | `unknown` | `unknown` |
| Other | other/empty | `x11` | `unknown` | `unknown` |

The nine distinct tokens in the resulting profile set are `kde-plasma-6-wayland`, `kde-plasma-6-x11`, `kde-plasma-5-wayland`, `kde-plasma-5-x11`, `kde-wayland-unverified`, `kde-x11-unverified`, `gnome-wayland`, `x11`, and `unknown`.

## 3. Per-mode environment requirements

Define exactly these closed sets, using profile tokens rather than display labels:

```python
MIRROR_ENVIRONMENTS = {
    "kde-plasma-6-wayland",
    "kde-plasma-6-x11",
}
VIRTUAL_DISPLAY_ENVIRONMENTS = {"kde-plasma-6-wayland"}
```

Replace the current single-profile gate and its single environment diagnostic with the two per-mode membership checks and the per-mode diagnostics below. Do not retain the old generic environment diagnostic in addition to the per-mode diagnostics. The current check is at `src/airplay_linux/policy/capabilities.py:388`.

Retain all existing non-environment evidence and use these exact mode expressions (the inventory is the already validated tuple, and the helper is a valid `HelperFeatures` result or `None`):

```python
mirror = bool(
    mirror_environment_supported and mode_ready and inventory and physical_source
)
virtual_display = bool(
    virtual_display_environment_supported
    and mode_ready
    and kscreen
    and helper is not None
    and virtual_source
)
```

The existing expression positions and operands are at `src/airplay_linux/policy/capabilities.py:519` and `src/airplay_linux/policy/capabilities.py:522`. `mode_ready`, `physical_source`, `virtual_source`, inventory, `kscreen`, and helper retain their current evidence definitions at `src/airplay_linux/policy/capabilities.py:430`, `src/airplay_linux/policy/capabilities.py:433`, `src/airplay_linux/policy/capabilities.py:434`, `src/airplay_linux/policy/capabilities.py:460`, `src/airplay_linux/policy/capabilities.py:477`, and `src/airplay_linux/policy/capabilities.py:481`.

Each unmet mode environment requirement adds exactly one `Diagnostic` (`src/airplay_linux/domain/resources.py:139`) with:

- `code=ErrorCode.unsupported_operation`;
- `message="The installed backend does not expose this operation. Update doubletake or go back and choose an operation it reports as available."`;
- `recovery=(RecoveryAction.retry, RecoveryAction.back)`;
- `receiver_id=None`;
- `detail` exactly as in the table below.

The fixed catalog entry and recovery are at `src/airplay_linux/diagnostics/catalog.py:87`; the enum members are at `src/airplay_linux/domain/resources.py:94` and `src/airplay_linux/domain/resources.py:122`.

| Profile | Environment diagnostics added | Exact `detail` value(s), in order |
| --- | ---: | --- |
| `kde-plasma-6-wayland` | 0 | none |
| `kde-plasma-6-x11` | 1 | `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed kde-plasma-6-x11` |
| `kde-plasma-5-wayland` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed kde-plasma-5-wayland`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed kde-plasma-5-wayland` |
| `kde-plasma-5-x11` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed kde-plasma-5-x11`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed kde-plasma-5-x11` |
| `kde-wayland-unverified` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed kde-wayland-unverified`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed kde-wayland-unverified` |
| `kde-x11-unverified` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed kde-x11-unverified`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed kde-x11-unverified` |
| `gnome-wayland` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed gnome-wayland`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed gnome-wayland` |
| `x11` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed x11`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed x11` |
| `unknown` | 2 | `session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed unknown`; `session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed unknown` |

The shared prefix is the exact string `"session-profile "`. Store it as `SESSION_PROFILE_DETAIL_PREFIX` in `src/airplay_linux/domain/resources.py`; the same constant is consumed by policy diagnostic creation and UI filtering. The current `note()` equality deduplication is at `src/airplay_linux/policy/capabilities.py:391`; the two per-mode details must remain distinct.

## 4. `ModeRefusal`

Add `ModeRefusal(StrEnum)` in `src/airplay_linux/domain/resources.py` with exactly these members and string values:

| Member | Value |
| --- | --- |
| `session` | `session` |
| `dependency` | `dependency` |
| `admission` | `admission` |
| `backend` | `backend` |
| `inventory` | `inventory` |
| `helper` | `helper` |

Add `CapabilityReport.mirror_refusal: ModeRefusal | None = None` and `CapabilityReport.virtual_display_refusal: ModeRefusal | None = None`. The existing report and `__post_init__` are at `src/airplay_linux/domain/resources.py:237` and `src/airplay_linux/domain/resources.py:248`. Keep both defaults `None` so existing constructors, including `src/airplay_linux/ui/presenter.py:217` and `src/airplay_linux/session/testing.py:54`, remain valid without edits.

Enforce only this direction in `CapabilityReport.__post_init__`: `mirror is True` implies `mirror_refusal is None`, and `virtual_display is True` implies `virtual_display_refusal is None`. If either identity-true implication fails, raise `ValueError`. Do not enforce the converse. In particular, a hand-built report with an unavailable mode and a `None` refusal remains constructible. Every report produced by `EnvironmentProbe`, however, must set a refusal when it withholds a mode and `None` when the mode is available. This is a probe totality requirement, not a bidirectional constructor invariant.

The computation uses the existing evidence: per-mode environment membership; whether the mirror capture provider exists; `_command("kscreen-doctor")`; `mode_ready`; `physical_source`; `virtual_source`; whether the proven physical `inventory` is non-empty; and whether the helper probe produced a valid `HelperFeatures`. The exact current probe entry points are `src/airplay_linux/policy/capabilities.py:350`, `src/airplay_linux/policy/capabilities.py:357`, `src/airplay_linux/policy/capabilities.py:386`, and `src/airplay_linux/policy/capabilities.py:481`.

`_module("PySide6")` and `_module("zeroconf")` continue to produce their existing dependency diagnostics but do not gate either mode and do not yield `ModeRefusal.dependency`. Missing PySide6 already leaves both mode flags available in the current test at `tests/policy/test_capabilities.py:463`; missing discovery likewise leaves both modes available at `tests/policy/test_capabilities.py:329`. For mode refusal purposes, `dependency` means the mirror capture provider is absent (`self._capture is None`) or the virtual-mode `kscreen-doctor` command is absent. A `krfb-virtualmonitor` failure is represented by the helper probe and is `helper`, not `dependency`.

For each mode, evaluate the following rows in order and return the first matching refusal. If every row passes, return `None`. This is the complete decision table; simultaneous failed evidence always resolves by the earlier row.

| Mode | First matching condition | Refusal | Exact meaning |
| --- | --- | --- | --- |
| Mirror | `profile not in MIRROR_ENVIRONMENTS` | `session` | The mode's session profile is unsupported. |
| Mirror | profile supported and `self._capture is None` | `dependency` | No physical capture provider was composed. |
| Mirror | prior rows pass and `mode_ready is False` | `admission` | Backend admission or `multi_target` is unproven. `mode_ready` remains `admitted and backend.supports("multi_target")`; public/fixture admission retains `owned_daemon_control`/`keyring` and `foreground_control`/`owner_handshake`/`keyring` semantics at `src/airplay_linux/policy/capabilities.py:97`, `src/airplay_linux/policy/capabilities.py:102`, and `src/airplay_linux/policy/capabilities.py:430`. |
| Mirror | prior rows pass and `physical_source is False` | `backend` | Backend typed physical-source support is unproven. |
| Mirror | prior rows pass and `inventory` is empty | `inventory` | No validated non-empty physical monitor inventory was proven. `_physical_inventory` accepts only a tuple of `PhysicalMonitorSource` values at `src/airplay_linux/policy/capabilities.py:210`. |
| Virtual display | `profile not in VIRTUAL_DISPLAY_ENVIRONMENTS` | `session` | The mode's session profile is unsupported. |
| Virtual display | profile supported and `_command("kscreen-doctor") is False` | `dependency` | KScreen command availability is **not proven**: the lookup found nothing, raised, or returned a malformed value, and `_command()` at `src/airplay_linux/policy/capabilities.py:350` treats all three identically. |
| Virtual display | prior rows pass and `mode_ready is False` | `admission` | Backend admission or `multi_target` is unproven. |
| Virtual display | prior rows pass and `virtual_source is False` | `backend` | Backend typed virtual-source support is unproven. |
| Virtual display | prior rows pass and `helper is None` | `helper` | No valid helper contract was proven, including absent probe, error result, or malformed result. |

This order makes `backend` reachable only when `mode_ready` is true and the source-specific capability is false. A missing/failed backend probe has `mode_ready=False`, so it is `admission`; a source-specific backend flag missing with otherwise-ready admission is `backend`. An absent capture provider is `dependency`, not `inventory`; an existing provider that errors, returns a malformed inventory, or returns an empty inventory reaches `inventory`. On Virtual display, a `kscreen-doctor` lookup that does not positively resolve — absent, raising, or malformed — is `dependency`; helper errors are `helper` after the earlier session, command, admission, and backend checks.

The reachable refusal sets are closed: Mirror is exactly `{session, dependency, admission, backend, inventory}`; Virtual display is exactly `{session, dependency, admission, backend, helper}`. `EnvironmentProbe` must never produce any other mode/refusal pair. Do not add a constructor validator for unreachable pairs beyond the one-direction availability invariant above.

## 5. Environment label

Add the single token-to-label function `environment_label(profile: str) -> str` in `src/airplay_linux/policy/capabilities.py`, alongside the current profile enum/classifier at `src/airplay_linux/policy/capabilities.py:79` and `src/airplay_linux/policy/capabilities.py:131`. This function owns the complete mapping; all other UI/runtime/export code consumes its result and must not maintain another token-to-label map. A token not in the mapping returns exactly `"Unknown"`.

| Profile token | Label |
| --- | --- |
| `kde-plasma-6-wayland` | `KDE Plasma 6 Wayland` |
| `kde-plasma-6-x11` | `KDE Plasma 6 X11` |
| `kde-plasma-5-wayland` | `KDE Plasma 5 Wayland` |
| `kde-plasma-5-x11` | `KDE Plasma 5 X11` |
| `kde-wayland-unverified` | `KDE Wayland (version unverified)` |
| `kde-x11-unverified` | `KDE X11 (version unverified)` |
| `gnome-wayland` | `GNOME Wayland` |
| `x11` | `X11 (other desktop)` |
| `unknown` | `Unknown` |

Assign `CapabilityReport.environment` this display label while keeping the profile token in the local classification value for gating and `session-profile` details. The report field currently takes the classifier result at `src/airplay_linux/policy/capabilities.py:536`; its declaration is at `src/airplay_linux/domain/resources.py:238`.

Change `src/airplay_linux/diagnostics/activity.py:179` `_SAFE_ENVIRONMENTS` to exactly this label set: `KDE Plasma 6 Wayland`, `KDE Plasma 6 X11`, `KDE Plasma 5 Wayland`, `KDE Plasma 5 X11`, `KDE Wayland (version unverified)`, `KDE X11 (version unverified)`, `GNOME Wayland`, `X11 (other desktop)`, and `Unknown`. Keep the activity boundary's unknown-label behavior: a well-typed but unrecognized report label is replaced with `Unknown` before it is retained/exported, while non-string data continues to receive the fixed boundary rejection. The sanitizer is at `src/airplay_linux/diagnostics/activity.py:579`; existing fallback tests are at `tests/diagnostics/test_safety.py:2021` and `tests/diagnostics/test_safety.py:4399`.

`runtime._capability_gates` must continue to skip the `environment` entry when building gated capabilities; that guard is at `src/airplay_linux/runtime.py:404`.

## 6. Pane projection

Add required field `PaneState.mode_reason: str | None` to `src/airplay_linux/ui/state.py:136`. `Mode` is defined at `src/airplay_linux/domain/resources.py:14`. `project()` always supplies it. Determine the displayed mode using the existing `_visible_mode` rule at `src/airplay_linux/ui/state.py:944`, so an active session's mode remains authoritative. Set the field to `None` when the visible mode is available or its refusal field is `None`; otherwise use the exact `(visible_mode, refusal)` sentence table below. A false mode with no recorded refusal, as permitted for hand-built fixtures, also yields `None`.

Use one table named `_MODE_REFUSAL_SENTENCES`, total over exactly these ten reachable pairs and containing these byte-for-byte strings:

| Mode | Refusal | Exact sentence |
| --- | --- | --- |
| `Mode.MIRROR` | `ModeRefusal.session` | `Mirror needs KDE Plasma 6 on Wayland or X11.` |
| `Mode.MIRROR` | `ModeRefusal.dependency` | `A required system component is missing; see the diagnostics.` |
| `Mode.MIRROR` | `ModeRefusal.admission` | `The backend cannot start a stream from this application yet; a required start operation is not proven.` |
| `Mode.MIRROR` | `ModeRefusal.backend` | `The installed backend does not provide monitor capture.` |
| `Mode.MIRROR` | `ModeRefusal.inventory` | `No physical monitor could be verified in this session.` |
| `Mode.VIRTUAL` | `ModeRefusal.session` | `Virtual display needs KDE Plasma 6 on Wayland.` |
| `Mode.VIRTUAL` | `ModeRefusal.dependency` | `A required system component is missing; see the diagnostics.` |
| `Mode.VIRTUAL` | `ModeRefusal.admission` | `The backend cannot start a stream from this application yet; a required start operation is not proven.` |
| `Mode.VIRTUAL` | `ModeRefusal.backend` | `The installed backend does not provide virtual outputs.` |
| `Mode.VIRTUAL` | `ModeRefusal.helper` | `The virtual-display helper (krfb-virtualmonitor) is not available or unsupported.` |

Do not put unreachable pairs in this table. A pair outside it is still constructible, because §4 forbids a validator for unreachable pairs while `CapabilityReport` stays a plain dataclass, so the projection defines the fallback rather than assuming it cannot happen: the lookup is `_MODE_REFUSAL_SENTENCES.get((visible_mode, refusal))`, it never raises, and when it returns `None` the projection uses the fixed per-mode fallback sentence — `Mirror is not available in this session.` for `Mode.MIRROR`, `Virtual display is not available in this session.` for `Mode.VIRTUAL`. No diagnostic token, profile token, refusal enum, or backend detail is interpolated into `mode_reason`.

Change `_capability_diagnostic` in `src/airplay_linux/ui/state.py:1237` to exclude every diagnostic whose `detail` starts with `SESSION_PROFILE_DETAIL_PREFIX` before keyword matching, concrete-cause matching, dependency-incompatible matching, or first-candidate fallback. Preserve all current ordering and filtering for diagnostics that do not have that prefix.

The empty-candidate rule is exact:

1. Retain `non_environment_diagnostics`, the original report diagnostics after removing only `session-profile ` details.
2. Run the existing foreign-lane and ranked selection against that list.
3. If selection finds no candidate and `non_environment_diagnostics` is empty but at least one `session-profile ` diagnostic was removed, return `None`; do not synthesize `unsupported_operation`.
4. In every other no-candidate case, keep the existing `unsupported_operation` fallback.

Consequently an environment-only gate displays `mode_reason` and no error label. An empty diagnostics tuple still keeps the current fallback, and a non-environment audio-only report still returns the current `unsupported_operation` fallback. Keep `tests/ui/test_state_projection.py:1127` `test_a_mode_gate_never_reports_an_audio_diagnostic_alone` and its exact expectation that `pane.diagnostic.code is ErrorCode.unsupported_operation`; the surrounding existing specificity tests are at `tests/ui/test_state_projection.py:1041`, `tests/ui/test_state_projection.py:1062`, `tests/ui/test_state_projection.py:1084`, and `tests/ui/test_state_projection.py:1195`.

## 7. UI

Add one `QLabel` named `_mode_reason` with object name `modeReasonLabel` in `MainWindow._build_pane`. Insert it immediately after the `modeSelector` widget and before `noticeLabel`; the selector and following labels are currently laid out at `src/airplay_linux/ui/window.py:340`, `src/airplay_linux/ui/window.py:366`, and `src/airplay_linux/ui/window.py:372`.

Set its accessible name to exactly `Mode availability reason`. It is static, non-interactive text and has no tooltip, matching the notice/error label pattern (accessible names at `src/airplay_linux/ui/window.py:368` and `src/airplay_linux/ui/window.py:374`). Render `PaneState.mode_reason` verbatim, without adding a prefix or punctuation. Show it iff the field is not `None`; when `None`, clear its text and hide it. Use the muted reason-line styling shown by the chosen option A mockup. `_render_mode_reason(pane)` is called by `render_selected()` and `_render_no_selection()` clears/hides the label; those render paths are at `src/airplay_linux/ui/window.py:527` and `src/airplay_linux/ui/window.py:710`.

The field does not change the selector's enabled state, action set, notice, or diagnostic. The existing widget renders `PaneState.diagnostic.message` only at `src/airplay_linux/ui/window.py:773`; do not show diagnostic `detail` in the new label.

## 8. Error handling

`ModeRefusal` is an explanation in the capability report and pane only. Do not add an `ErrorCode`, change `can_start`, or change any existing recovery action. The mode policy `can_start()` continues to refuse an unavailable mode with `ErrorCode.unsupported_operation`; the gate is at `src/airplay_linux/policy/capabilities.py:570`. Catalog messages remain fixed at `src/airplay_linux/diagnostics/catalog.py:87`.

| Refusal/path | Capability/report effect | User-visible pane result |
| --- | --- | --- |
| Unsupported session profile | Add the environment diagnostic for each unmet mode as specified in section 3; mode refusal is `session`. | The exact session sentence from section 6. The `session-profile ` diagnostics are never selected as the error line. If they are the only diagnostics, the error label is hidden. |
| Mirror capture provider absent | Mirror false; refusal `dependency`; retain existing `unsupported_operation` diagnostic detail `physical capture provider`. | Mirror dependency sentence and the existing selected non-environment diagnostic, ranked by the unchanged `_capability_diagnostic` rules. |
| Virtual `kscreen-doctor` absent | Virtual false; refusal `dependency`; retain `_command_failure("kscreen-doctor")`, `dependency_missing`, and the command-specific catalog copy `The required command 'kscreen-doctor' is not installed or not on PATH. Install it and retry, or go back and choose a capability that is available.` | Virtual dependency sentence and that exact missing-command error message. |
| Backend start admission or `multi_target` unproven | The affected mode is false; refusal `admission`; retain the backend's current typed diagnostic(s), including its exact missing operation details. | Admission sentence plus the existing selected diagnostic. A failure of the backend probe also resolves to `admission` because `mode_ready` is false and precedes the backend-source test. |
| `physical_source` or `virtual_source` unproven after `mode_ready` | The corresponding mode is false; refusal `backend`; retain existing `dependency_incompatible` diagnostic and source detail. | Mode-specific backend sentence plus existing selected diagnostic. |
| No proven physical inventory after the mirror provider exists | Mirror false; refusal `inventory`; retain the provider's typed error or existing `physical capture source` unsupported-operation diagnostic. | Mirror inventory sentence plus the existing selected diagnostic. |
| Helper probe absent, failed, or malformed after earlier virtual checks pass | Virtual false; refusal `helper`; retain the probe's existing typed error or contract diagnostic. | Virtual helper sentence plus the existing selected diagnostic. |
| KDE Plasma 6 X11 with all Mirror evidence present | Mirror true and `mirror_refusal=None`; Virtual display remains false and has `session`. | No Mirror reason line. Selecting Virtual display shows `Virtual display needs KDE Plasma 6 on Wayland.` |
| Environment-only refusal | Report still contains the environment diagnostics; refusal is `session`. | `mode_reason` is shown alongside the existing capability notice `The selected mode is unavailable until its missing capability is fixed. Retry after fixing it, or go back and choose an available mode.` The result has `pane.diagnostic is None`, so no error label or fabricated doubletake error appears. |
| Non-environment refusal with no selectable diagnostics | No change to refusal. | Keep the existing synthetic `unsupported_operation` fallback, including the audio-only case pinned at `tests/ui/test_state_projection.py:1127`. |
| Portal binding/source verification fails after a Mirror start is attempted | No change to startup mode refusal. Keep the current typed `unsupported_source` result and fixed catalog copy `The backend cannot verify this physical monitor. Select a supported source or update doubletake.` `src/airplay_linux/capture/portal.py:300` requires exactly one stream, `src/airplay_linux/capture/portal.py:311` binds the `MONITOR` source type, and `src/airplay_linux/capture/portal.py:315` enforces the exact connector equality; `src/airplay_linux/display/inventory.py:61`, `src/airplay_linux/display/inventory.py:62`, and `src/airplay_linux/display/inventory.py:121` retain `missing_position`/`non_monitor_source` refusal details. | Existing capture error behavior; no direct X11 fallback and no new refusal token. |
| `PySide6` or `zeroconf` module unavailable | Keep their existing diagnostics; do not use them as mode refusals or alter a mode flag. | Existing shell/dependency behavior; no mode reason unless one of the mode-specific checks above also refuses. |
| `--check-environment` | Keep bootstrap composition without a capture provider, so its Mirror flag is false and it reports the physical capture provider limitation even on X11. Its construction remains at `src/airplay_linux/app.py:86` and `src/airplay_linux/app.py:93`. | It prints the report with the human-readable session label; it does not prove Mirror availability. |

Virtual display remains blocked on every X11 profile before a helper can make it appear supported. The existing call at `src/airplay_linux/display/creation.py:319` and predicate definition at `src/airplay_linux/display/creation.py:598` remain unchanged. The runtime environment gate exclusion at `src/airplay_linux/runtime.py:404` also remains unchanged.

## 9. Test fixtures and cases

Do not add live behavior to the ordinary suite. Existing policy tests inject all probe dependencies at `tests/policy/test_capabilities.py:164`; retain that offline pattern. Existing runtime report tests are at `tests/runtime/test_capability_report.py:144`, and diagnostics label tests are at `tests/diagnostics/test_safety.py:2021` and `tests/diagnostics/test_safety.py:4399`.

### New tests

| Test file | New test name | Required assertions |
| --- | --- | --- |
| `tests/policy/test_capabilities.py:451` | `test_classify_environment_covers_the_complete_profile_matrix` | Parameterize the exact 27 normalized combinations in section 2 and assert the exact profile token in every cell. |
| `tests/policy/test_capabilities.py:451` | `test_classify_environment_normalizes_all_three_session_values` | Pin trim/lower behavior for session type, colon-split/trim/lower exact desktop tokens, KDE-over-GNOME precedence, version trim, and the exact non-string rule for each of the three variables. Include session `" X11 "`, desktop `" gNoMe : pLaSmA "`, version `" 6 "`, version `"7"`, `"06"` and unparseable version text, plus `None`, an integer, an arbitrary object, and a `str` subclass separately for each variable. Assert session non-strings classify as `unknown`, a non-string desktop is treated as no desktop token, and a non-string KDE version yields the unverified profile supported by the remaining session/desktop evidence. |
| `tests/policy/test_capabilities.py:451` | `test_per_mode_environment_diagnostics_have_exact_details_and_counts` | Assert the 0/1/2 counts and every exact `detail` string from section 3 for all nine tokens, including both distinct details for every unsupported profile. For every such diagnostic, assert `ErrorCode.unsupported_operation`, the exact fixed message, recovery `(RecoveryAction.retry, RecoveryAction.back)`, and `receiver_id is None`. Assert the old shared environment diagnostic is absent. |
| `tests/policy/test_capabilities.py:263` | `test_mode_environment_sets_gate_mirror_and_virtual_display_independently` | With all other proofs present, assert Mirror true/Virtual false on `kde-plasma-6-x11`; both true on `kde-plasma-6-wayland`; both false on the seven other profiles. Remove `mode_ready`, `physical_source`, and non-empty inventory one at a time for Mirror; remove `mode_ready`, `kscreen`, valid helper result, and `virtual_source` one at a time for Virtual display; assert each affected mode is refused while the other mode remains available whenever its own evidence is complete. |
| `tests/policy/test_capabilities.py:276` | `test_mode_refusal_uses_the_closed_precedence_table` | Exercise every refusal in section 4 for both modes, then test all ten pairwise failure combinations per mode (20 total) to prove first-match precedence. Include an absent, failed, and malformed backend probe; no admission; missing `multi_target`; source-only backend failures; absent/empty/error/malformed capture inventory; an absent KScreen command, a finder that raises, and a finder that returns a malformed value (all three produce the Virtual display `dependency` refusal); and absent/error/malformed/valid helper results. Assert no unreachable pair is returned. |
| `tests/policy/test_capabilities.py:263` | `test_probe_mode_refusals_are_total_over_the_evidence_matrix` | Run the real `EnvironmentProbe.check()` over all nine profiles and the Cartesian product of nine backend cases (probe absent, `Err`, malformed raw result, malformed `Ok`, no admission, no `multi_target`, no `physical_source`, no `virtual_source`, all mode evidence present), six capture cases (provider absent, provider `Err`, malformed raw result, malformed `Ok`, empty tuple, one physical monitor), three KScreen-command outcomes (found, finder raises, finder returns a malformed value), and five helper cases (probe absent, `Err`, malformed raw result, malformed `Ok`, valid `HelperFeatures`). Inject module/command/backend/capture/helper/audio/keyring/D-Bus probes and the process runner so all 7,290 reports are deterministic and no host command, import, or D-Bus call occurs. For each report, assert `mode is True` iff its refusal is `None`; when false, assert the refusal belongs to that mode's exact closed set. Separately retain the existing missing-PySide6 and unavailable-discovery controls at `tests/policy/test_capabilities.py:463` and `tests/policy/test_capabilities.py:329`, asserting neither module result changes either mode. |
| `tests/policy/test_capabilities.py:736` | `test_bootstrap_probe_without_capture_keeps_mirror_false_on_x11` | Reproduce `--check-environment`'s `EnvironmentProbe.from_environment(process=..., helper=...)` shape with injected fakes and no `capture`; assert `classify_environment(...) == "kde-plasma-6-x11"`, `report.environment == "KDE Plasma 6 X11"`, `mirror is False`, Mirror refusal `dependency`, and diagnostic detail `physical capture provider`. |
| `tests/runtime/test_capability_report.py:144` | `test_capability_report_refusal_invariant_is_one_directional` | Construct separate reports with `(mirror=True, virtual_display=False)` and `(mirror=False, virtual_display=True)` without passing the new fields; assert both defaults are `None`. With `dataclasses.replace`, assert adding `ModeRefusal.session` to the available mode raises `ValueError` for each field. Assert an unavailable mode with a refusal and an unavailable mode with `None` both remain constructible. |
| `tests/diagnostics/test_safety.py:2021` | `test_environment_label_mapping_is_total_unique_and_allowlisted` | Assert the exact nine token-to-label pairs from section 5, uniqueness of the nine labels, and equality between the mapped-label set and `_SAFE_ENVIRONMENTS`; assert an unrecognized token/label exports exactly `Unknown`. |
| `tests/ui/test_state_projection.py:1041` | `test_mode_reason_table_covers_exactly_the_ten_reachable_pairs` | Assert all ten exact strings from section 6, that no other pair is present, and that each refusal appears only for its declared mode. |
| `tests/ui/test_state_projection.py:1041` | `test_mode_reason_is_none_for_available_or_unrecorded_visible_mode` | Assert `None` for an available visible mode, an unavailable hand-built report with no refusal, and the active session mode selected by `_visible_mode`. |
| `tests/ui/test_state_projection.py:1041` | `test_mode_reason_falls_back_for_a_constructible_unreachable_pair` | Construct `Mode.MIRROR` with `ModeRefusal.helper` and `Mode.VIRTUAL` with `ModeRefusal.inventory`; assert the projection returns exactly `Mirror is not available in this session.` and `Virtual display is not available in this session.` and never raises. |
| `tests/ui/test_state_projection.py:1062` | `test_session_profile_diagnostics_are_excluded_before_keyword_matching` | Put a `session-profile ` detail containing the selected mode's keyword before a non-environment diagnostic; prove the environment item is not selected and the remaining non-environment ranking is unchanged. |
| `tests/ui/test_state_projection.py:1127` | `test_environment_only_mode_refusal_has_no_synthetic_diagnostic` | For both selected modes, use only their `session-profile ` diagnostic; assert the correct `mode_reason` and `pane.diagnostic is None`. |
| `tests/ui/test_state_projection.py:1127` | `test_non_environment_empty_candidates_keep_unsupported_operation` | Assert the existing fallback remains when diagnostics are empty and when the only non-environment diagnostics are foreign-lane audio diagnostics. Preserve the existing `test_a_mode_gate_never_reports_an_audio_diagnostic_alone` unchanged. |
| `tests/ui/test_dashboard_widgets.py` (insert after the existing `test_notice_and_error_labels_render_the_projection_and_hide_when_empty`, anchored at `:1078`; that test is retained unchanged) | `test_mode_reason_label_renders_below_selector_and_hides_when_none` | Assert object name `modeReasonLabel`, accessible name `Mode availability reason`, empty tooltip, exact verbatim text, placement after `modeSelector` and before `noticeLabel`, and clear/hidden state for `None` and no selection. |
| `tests/environment/test_live_x11.py` (new file) | `test_live_x11_profile_classifies_as_kde_plasma_6_x11` | Under the required live guard, assert `classify_environment(os.environ) == "kde-plasma-6-x11"`. |
| `tests/environment/test_live_x11.py` (new file) | `test_live_x11_physical_inventory_is_nonempty` | Run the existing `KscreenConnectorInventory`/`KScreenInventory` parser against `kscreen-doctor -j`; assert a non-empty tuple of verified physical connectors. The source adapter is at `src/airplay_linux/capture/physical.py:96`, and the shared parser at `src/airplay_linux/display/inventory.py:156`. |
| `tests/environment/test_live_x11.py` (new file) | `test_live_x11_portal_advertises_monitor` | Run exactly `busctl --user get-property org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop org.freedesktop.portal.ScreenCast AvailableSourceTypes`; parse `u <integer>` and assert `(integer & SCREEN_CAST_MONITOR) != 0`, where `SCREEN_CAST_MONITOR == 1` at `src/airplay_linux/capture/portal.py:57`. The existing portal service/path/interface constants are at `src/airplay_linux/policy/audio.py:45`, `src/airplay_linux/policy/audio.py:46`, and `src/airplay_linux/policy/audio.py:49`. |

### Existing tests and exact expected changes

- `tests/policy/test_capabilities.py:451` is `test_other_or_unproven_desktops_are_reported_separately_and_gated`: its parametrized table keeps raw profile tokens for the classifier, while its report assertion at `:456` becomes `report.environment == environment_label(token)` for the same table, and the X11 row's token changes from `x11` to `kde-plasma-6-x11`. The table also gains a per-row expected `mirror` value: `True` for the `kde-plasma-6-x11` row, because the default fixture resolves every executable and has a proven physical monitor, so that row now satisfies the mirror requirement, and `False` for the other four rows. `report.virtual_display is False` and `ErrorCode.unsupported_operation in _codes(report)` remain true for every row including the X11 one, whose virtual-display environment diagnostic carries that code. Raw-token assertions remain only on `classify_environment`, in the new classifier matrix tests. `test_missing_pyside6_is_reported_without_disabling_discovery` and `test_unavailable_discovery_keeps_both_modes_usable` keep both mode booleans true with complete mode proofs.
- `tests/runtime/test_capability_report.py:144` changes the successful report environment expectation from `kde-plasma-6-wayland` to `KDE Plasma 6 Wayland`; assert `mirror_refusal is None` for the available Mirror mode. `tests/runtime/test_capability_report.py:170` asserts the withheld physical source yields `ModeRefusal.backend`; `tests/runtime/test_capability_report.py:187` asserts the unproven backend/start admission yields `ModeRefusal.admission`.
- `tests/diagnostics/test_safety.py:2021` retains the unknown-label fallback; add every valid label from section 5 and assert exact export values. Non-string environment rejection remains unchanged.
- `tests/diagnostics/test_safety.py:4399` is `test_the_retained_environment_is_the_label_the_export_prints`; its parametrized row `("KDE Plasma 6 X11", "Unknown")` at `:4394` becomes `("KDE Plasma 6 X11", "KDE Plasma 6 X11")`, one row is added for each remaining valid label in section 5, and the genuinely unrecognized rows (`GNOME 47 Wayland`, `kde plasma 6 wayland`, `""`) keep their `Unknown` expectation.
- `tests/ui/test_state_projection.py:1127` keeps the exact audio-only `unsupported_operation` fallback. Existing capability diagnostic preference tests at `tests/ui/test_state_projection.py:1041`, `tests/ui/test_state_projection.py:1062`, `tests/ui/test_state_projection.py:1084`, and `tests/ui/test_state_projection.py:1195` retain their current non-environment behavior.
- `tests/ui/test_presenter.py:818` extends `test_a_capability_retry_reprobes_instead_of_replaying_the_frozen_report` to prove the refreshed report's `mode_reason` is projected and rendered without using the stale report.
- `tests/policy/test_capabilities.py:266` (`test_complete_kde_plasma_6_wayland_enables_every_proven_capability`) and `tests/policy/test_capabilities.py:956` (`test_the_current_environment_without_krfb_keeps_shell_and_discovery_usable`) assert the raw profile token today and change to the display label `KDE Plasma 6 Wayland`; no other expectation in either test changes.
- `tests/ui/test_dashboard_widgets.py:500` and `tests/ui/test_dashboard_widgets.py:1104` retain current projection/environment rendering assertions; the existing `test_notice_and_error_labels_render_the_projection_and_hide_when_empty` at `:1078` is retained unchanged and the named label test is added beside it.
- `tests/environment/test_live_kde.py:75` remains unchanged and Wayland-only; it continues to be the virtual-output test lane.

The live X11 module contract is exact: module-scoped guard variable `AIRPLAY_LIVE_X11`; before any host command, skip unless it equals `"1"`, normalized `XDG_SESSION_TYPE` equals `"x11"`, colon-split/trimmed/lowercased `XDG_CURRENT_DESKTOP` contains exact token `kde` or `plasma`, and stripped `KDE_SESSION_VERSION` equals `"6"`. Use only those three session variables, matching the classifier. Every test requests the same module-scoped guard fixture. No helper/output is created, no capture selection is made, and no stream is started. The existing guard shape is at `tests/environment/test_live_kde.py:75`, `tests/environment/test_live_kde.py:81`, and `tests/environment/test_live_kde.py:84`.

The opt-in command is exactly:

```sh
AIRPLAY_LIVE_X11=1 pytest -q tests/environment/test_live_x11.py
```

## 10. Change inventory

This specification task modifies only `docs/superpowers/specs/2026-11-06-x11-support-spec.md`; no source, test, operations, design, or mockup file is edited here. The following inventory describes the implementation files for PM review.

| File | Exact symbol(s) added or changed |
| --- | --- |
| `src/airplay_linux/policy/capabilities.py:79` | Add the three `DesktopEnvironment` X11 members; add `MIRROR_ENVIRONMENTS`, `VIRTUAL_DISPLAY_ENVIRONMENTS`, and `environment_label(profile)`; extend `classify_environment`; replace the one shared environment gate in `EnvironmentProbe._probe`; add per-mode environment diagnostics and mode refusal computation; correct the module docstring at `src/airplay_linux/policy/capabilities.py:31` (another desktop can now enable Mirror) and the classifier docstring at `src/airplay_linux/policy/capabilities.py:134` (portal capture is no longer Wayland-only). Existing probe symbols are at `src/airplay_linux/policy/capabilities.py:131` and `src/airplay_linux/policy/capabilities.py:386`. |
| `src/airplay_linux/domain/resources.py:237` | Add `ModeRefusal`, `SESSION_PROFILE_DETAIL_PREFIX`, `CapabilityReport.mirror_refusal`, `CapabilityReport.virtual_display_refusal`, their `None` defaults, and the one-direction invariant in `CapabilityReport.__post_init__`; update the `environment` field docstring to specify a display label. |
| `src/airplay_linux/diagnostics/activity.py:179` | Replace `_SAFE_ENVIRONMENTS` with exactly the nine labels from section 5; retain `_normalized_capabilities` unknown fallback at `src/airplay_linux/diagnostics/activity.py:579`. |
| `src/airplay_linux/ui/state.py:136` | Add `PaneState.mode_reason`; add `_MODE_REFUSAL_SENTENCES`; set the field in `project()` at `src/airplay_linux/ui/state.py:367`; change `_capability_diagnostic()` at `src/airplay_linux/ui/state.py:1237` to apply the prefix exclusion and exact empty-candidate rule. |
| `src/airplay_linux/ui/window.py:310` | Add `_mode_reason` `QLabel`, object name `modeReasonLabel`, accessible name `Mode availability reason`; place it between `modeSelector` and `noticeLabel`; add `_render_mode_reason`; clear/hide on no selection. Current placement and rendering anchors are `src/airplay_linux/ui/window.py:340`, `src/airplay_linux/ui/window.py:366`, and `src/airplay_linux/ui/window.py:527`. |
| `tests/policy/test_capabilities.py:451` | Add the exact truth-table, normalization/non-string, per-mode diagnostics, gating, precedence, 7,290-report totality, and bootstrap-composition tests named in section 9; update existing profile expectations. |
| `tests/runtime/test_capability_report.py:144` | Update `environment` expectation to the label, assert `mirror_refusal is None` for available Mirror, and add `test_capability_report_refusal_invariant_is_one_directional`; keep refusal assertions for the withheld-source and unproven-admission reports. |
| `tests/diagnostics/test_safety.py:2021` | Add total label-map/allowlist checks while retaining unknown and non-string boundary behavior. |
| `tests/ui/test_state_projection.py:1041` | Add refusal sentence totality, `mode_reason`, prefix-ordering, environment-only, and non-environment fallback tests; retain the audio-only assertion. |
| `tests/ui/test_presenter.py:818` | Extend the capability-refresh test to assert refreshed mode reason projection. |
| `tests/ui/test_dashboard_widgets.py:1078` | Add the mode-reason widget render/placement/accessibility/visibility test and supply the new pane field in its direct `PaneState` fixture at `tests/ui/test_dashboard_widgets.py:167`. |
| `tests/environment/test_live_x11.py` | New opt-in, read-only X11 classification, physical inventory, and portal MONITOR-advertisement checks. |
| `docs/operations.md:21` | Add one paragraph that KDE Plasma 6 X11 supports portal-backed Mirror only and Virtual display remains Wayland-only. |

Do not change `src/airplay_linux/capture/portal.py:297`, `src/airplay_linux/capture/physical.py:96`, `src/airplay_linux/display/inventory.py:156`, or `src/airplay_linux/display/creation.py:319` and `src/airplay_linux/display/creation.py:598`; preserve portal-only capture, exact monitor/connector binding, current physical-inventory proof, and the Wayland-only creation defence. The existing verifier must continue to refuse windows, virtual outputs, unknown connectors, missing positions, and ambiguous mappings. Do not change `src/airplay_linux/app.py:93`, `src/airplay_linux/runtime.py:404`, `src/airplay_linux/diagnostics/catalog.py:87`, or `src/airplay_linux/policy/audio.py:45`; preserve the bootstrap probe composition, environment-gate exclusion, fixed catalog messages, and existing portal interface constants. Do not change `tests/environment/test_live_kde.py:137`; it remains the live virtual-output test. Do not add a dependency, production command, D-Bus interface, `ErrorCode`, or direct X11 capture path. Keep the selected option A asset `docs/superpowers/specs/assets/2026-11-06-x11-mode-reason-mockup.png` unchanged.

## 11. Acceptance

Run in one existing KDE Plasma 6 X11 desktop login session. The first two commands are process invocations; the remaining steps are dashboard/receiver observations. Do not restart the display manager or change the host session during the run.

1. [ ] Run exactly `airplay-linux --check-environment`. Confirm its output reports `KDE Plasma 6 X11`; do not count its `mirror=False` bootstrap report as a capture failure or as proof of capture.
2. [ ] Run exactly `airplay-linux`. Confirm the dashboard toolbar environment label is `KDE Plasma 6 X11` and Mirror offers `eDP-1` at `2240x1400`.
3. [ ] Select Virtual display. Confirm the line under the mode selector is exactly `Virtual display needs KDE Plasma 6 on Wayland.`
4. [ ] Select Mirror, choose `eDP-1` at `2240x1400`, start it to `jj` (`AppleTV3,2`, `192.168.0.192`), and accept the desktop ScreenCast prompt for that physical monitor.
5. [ ] Confirm the TV shows the current desktop panel.
6. [ ] Move a window on the desktop and confirm that motion is visible on the TV.
7. [ ] Stop Mirror. Confirm the receiver returns to idle, no virtual-display helper process survives, and the portal session is closed.

Any failure in steps 4-6 is recorded with the exact failing operation and output/diagnostic. Do not relax the source type, connector binding, inventory, backend, or session-profile proof to make the run pass.

## 12. Open questions for PM

None. The design's open implementation details requested for this specification are fixed in the normative tables above; the named `environment_label`, `_MODE_REFUSAL_SENTENCES`, `modeReasonLabel`, and new test names are part of this specification's contract.
