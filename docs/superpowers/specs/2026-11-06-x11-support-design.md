# X11 mirror support (KDE Plasma 6 on X11) — design

- **Status:** design revision 2 after independent review round 1 (2 blockers, 3 majors, all
  resolved); awaiting review round 2.
- **Date:** 2026-11-06 (revision 2)
- **Delivery target:** `/data/home/guest/Development/airplay-linux`, branch `master`, no usable
  remote (the configured `origin` reports `Repository not found`).
- **Mockup record:** `docs/superpowers/specs/assets/2026-11-06-x11-mode-reason-mockup.png`,
  option **A** chosen by the operator (line under the mode selector).

## Revision history

**Revision 2** answers review round 1. Every finding was reproduced against the code before it was
accepted:

| Round-1 item | Resolution |
| --- | --- |
| B1 `--check-environment` cannot report `mirror=True`; it composes its probe without a capture provider (`app.py:93`, `capabilities.py:461,519`) | §9 now uses the dashboard for mirror availability and the command only for the session label; §5.7 documents the command's bootstrap limitation as pre-existing and unchanged |
| B2 the X11 virtual-display reason never reaches the screen (`window.py:775` renders `message` only; `catalog.py:87` is backend wording) | §5.5 defines a closed refusal vocabulary (`ModeRefusal`) carried on the report, and one pane line rendered under the mode selector; the operator chose placement A from the mockup |
| M3 portal source *binding* on X11 is advertised, not proven (`capture/portal.py:311-315`) | §1 now separates "established" from "unproven", and §10 keeps the live run as the proof |
| M4 `tests/runtime/test_capability_report.py:156` asserts the environment token | §6 inventory includes it; §8 pins the new expectation |
| M5 `tests/environment/test_live_kde.py` creates a live virtual output (`:137-158`) | that module stays Wayland-only; §8 adds a separate mirror-only `tests/environment/test_live_x11.py` |

**Revision 1** was the first design: scope A (mirror on KDE Plasma 6 X11), live acceptance bar,
per-mode environment requirements, unchanged capture adapters, virtual display Wayland-only.

## 1. Background

The application's only supported session profile is *KDE Plasma 6 on Wayland*. One predicate
enforces it: `src/airplay_linux/policy/capabilities.py:388` compares the classified session
against `KDE_PLASMA_6_WAYLAND`, and both mode booleans are derived from that single result
(`:519` for mirror, `:522` for virtual display).

`classify_environment` (`:131`) returns the flat token `x11` for **every** X11 session before it
looks at the desktop at all (`:147`): the desktop name and the Plasma version are discarded. A KDE
Plasma 6 X11 user and an i3 X11 user are therefore indistinguishable, and neither can be admitted.

**Established on the target machine** (KDE Plasma 6.7.5, `XDG_SESSION_TYPE=x11`,
`XDG_CURRENT_DESKTOP=KDE`, `KDE_SESSION_VERSION=6`, `DESKTOP_SESSION=plasmax11`,
`XDG_SESSION_ID=261`):

| Evidence | Observation |
| --- | --- |
| `airplay-linux --check-environment` | `CapabilityReport(environment='x11', mirror=False, virtual_display=False, audio=True, …)` with one diagnostic carrying `detail='x11'` |
| `kscreen-doctor -j` | 6 outputs; `eDP-1` connected, enabled, panel `type=7`, `size 2240x1400`, `pos {x:0,y:0}`, `priority 1`; `HDMI-1` and `DP-1..4` disconnected |
| `kscreen-doctor -o` | the same `eDP-1` geometry (`0,0 2240x1400`), scale 1, rotation 1 |
| `busctl --user get-property … ScreenCast AvailableSourceTypes` | `7` (monitor, window and virtual each offered) |
| `… ScreenCast version` | `5`; backend `org.freedesktop.impl.portal.desktop.kde` running; PipeWire active |
| `command -v krfb-virtualmonitor` | missing |
| `avahi-browse -rt _airplay._tcp` | receivers present, including `jj` (`AppleTV3,2`, 192.168.0.192) |

**Established:** the physical-monitor inventory that mirror needs exists on X11 with connector
names, types, sizes and positions; the portal advertises monitor sources; the virtual-display
helper is genuinely absent.

**Not established, and treated as unproven:** that KWin's X11 portal path returns a stream whose
`source_type` is `MONITOR` and whose `mapping_id` equals the `kscreen-doctor` connector name. The
binding code (`capture/portal.py:311-315`) accepts nothing else, so until the live run in §9
succeeds this design claims only that no *code-level* environment predicate blocks X11 — not that
capture already works.

**A second, independent defect** surfaced while establishing this: `CapabilityReport.environment`
carries the *token* (`x11`, `kde-plasma-6-wayland`), while the diagnostics boundary whitelists
*labels* (`diagnostics/activity.py:179` accepts only `KDE Plasma 6 Wayland`; `:592` falls back to
`Unknown`). Verified directly:

```
report.environment = x11                              -> diagnostics label: Unknown
CapabilityReport(environment="kde-plasma-6-wayland")   -> diagnostics label: Unknown
CapabilityReport(environment="KDE Plasma 6 Wayland")   -> diagnostics label: KDE Plasma 6 Wayland
```

Every environment, Wayland included, is recorded as `Unknown` in the diagnostics export today, and
the dashboard shows a raw token. X11 support cannot be reported honestly without fixing that, so it
is part of this design (§5.4).

## 2. Goals

1. On a KDE Plasma 6 X11 session, mirror mode is offered and works end to end: select a physical
   monitor, stream it to an AirPlay receiver, stop cleanly.
2. The session profile is named exactly, both in the dashboard and in the diagnostics export.
3. Virtual display on X11 is refused with a reason the user can read on the pane, naming what is
   missing.
4. Every existing admission proof keeps its current meaning; nothing is inferred from the session
   type that was previously proven by evidence.
5. The full automated suite stays green, and Wayland behaviour does not change.

**Acceptance (agreed with the operator): live end-to-end.** A real receiver must show this panel
within one session on this machine (§9), not merely a passing capability report.

## 3. Non-goals

- GNOME on X11, XFCE, i3, or any X11 desktop other than KDE Plasma 6. They are classified and
  refused by name.
- KDE Plasma 5 on X11: classified and refused, exactly as Plasma 5 Wayland is today.
- Virtual display on X11. The provider stays Wayland-only (§5.6).
- A direct X11 capture path (`XGetImage`, `XShm`, `XComposite`, `ffmpeg x11grab`, desktop-specific
  scripts). The portal is the only capture mechanism (§5.3).
- Changing the backend (`doubletake`) or its capability derivation.
- Making `--check-environment` compose production capture evidence; it is a bootstrap view for both
  session types today and stays one (§5.7).
- Multi-output X11 behaviour beyond the proofs the existing verifier already applies.

## 4. Decisions taken with the operator

| Decision | Choice | Consequence |
| --- | --- | --- |
| Scope | Mirror only, KDE Plasma 6 X11 | No new capture mechanism, no virtual display work |
| Bar for done | Live end-to-end against a real receiver | §9 defines the run; a report-only pass is not acceptance |
| Virtual display on X11 | Refused, with its own readable reason | One new pane line (§5.5), helper and provider untouched |
| Where that reason appears | Option A: a line under the mode selector | `PaneState.mode_reason`, mockup recorded above |

## 5. Design

### 5.1 Session profiles

`DesktopEnvironment` gains three X11 members, mirroring the existing Wayland branch instead of
collapsing:

| Profile token | Condition |
| --- | --- |
| `kde-plasma-6-x11` | `XDG_SESSION_TYPE=x11`, desktop names KDE/Plasma, `KDE_SESSION_VERSION=6` |
| `kde-plasma-5-x11` | same, with `KDE_SESSION_VERSION=5` |
| `kde-x11-unverified` | same, with no reported version |
| `x11` (unchanged) | any other X11 session |

The desktop test reuses the existing normalisation of `XDG_CURRENT_DESKTOP` (colon-separated
tokens, case-insensitive, trimmed). The version comes from `KDE_SESSION_VERSION` only: a
`DESKTOP_SESSION` string is not evidence of a Plasma version. Every other profile keeps its current
token and meaning, so `gnome-wayland` is still refused by the mirror gate.

Rationale: the gate is per mode (§5.2), so the profile must be as specific as the Wayland branch. If
X11 collapsed to one token, a future widening for Plasma 6 X11 would silently admit every X11
desktop.

### 5.2 Per-mode environment requirement

Replace the single `environment_supported` boolean with one requirement per mode:

```
MIRROR_ENVIRONMENTS            = {kde-plasma-6-wayland, kde-plasma-6-x11}
VIRTUAL_DISPLAY_ENVIRONMENTS   = {kde-plasma-6-wayland}
```

`mirror` keeps requiring every other proof it already requires — backend mode admission, a proven
physical-source capability, and a non-empty physical inventory. `virtual_display` keeps requiring
its Wayland profile, `kscreen-doctor`, a probed helper contract, and its backend capability.

The environment diagnostic becomes per mode rather than per session: one diagnostic per unmet mode
requirement, naming the requirement and the observed profile.

| Profile | Environment diagnostics added |
| --- | --- |
| `kde-plasma-6-wayland` | none — both requirements are met |
| `kde-plasma-6-x11` | one, naming `KDE Plasma 6 on Wayland` as the virtual-display requirement |
| `kde-plasma-5-x11`, `x11`, `gnome-wayland`, `unknown` | one per unmet requirement (two) |

An admitted capability never carries a refusal beside it: on `kde-plasma-6-x11` mirror is available
and the only environment diagnostic belongs to virtual display. `note()` already deduplicates
identical diagnostics, so the table above is the complete set.

### 5.3 Capture and inventory are unchanged

No adapter changes are planned. The mirror path already proves, in order: backend
`physical_source`, a physical connector inventory from `kscreen-doctor -j`, a portal stream whose
`source_type` is `MONITOR` and whose `mapping_id` names the requested connector, and a reported
position/size that matches the observed layout. Those proofs are what make the X11 case safe, and
they are the same proofs on both session types.

If X11 evidence cannot satisfy a proof, the existing typed refusal is returned — for example
`missing_position` when a layout carries no position, or `unsupported_source` when the portal offers
something that is not a monitor. There is no fallback path, no retry through a different mechanism,
and no guess. If the live run (§9) shows the KDE X11 portal cannot bind a monitor stream, the
outcome is a named refusal and a follow-on design for a direct X11 capture path; it is not a quiet
substitution inside this feature.

### 5.4 Environment label

`CapabilityReport.environment` becomes the human label, and classification keeps its token for
gating:

| Token | Label |
| --- | --- |
| `kde-plasma-6-wayland` | `KDE Plasma 6 Wayland` |
| `kde-plasma-6-x11` | `KDE Plasma 6 X11` |
| `kde-plasma-5-wayland` / `kde-plasma-5-x11` | `KDE Plasma 5 Wayland` / `KDE Plasma 5 X11` |
| `kde-wayland-unverified` / `kde-x11-unverified` | `KDE Wayland (version unverified)` / `KDE X11 (version unverified)` |
| `gnome-wayland` | `GNOME Wayland` |
| `x11` | `X11 (other desktop)` |
| `unknown` | `Unknown` |

One mapping function is the only place a label exists, so a profile cannot be added without one. The
diagnostics whitelist becomes exactly the label set, which makes the export agree with the dashboard
and removes today's `Unknown` for Wayland. A label outside the set still normalises to `Unknown`,
so the boundary stays closed. `runtime.py:404` deliberately skips `environment` when building the
gated-capability map, so the gate list is unaffected.

### 5.5 Why a mode is unavailable, in words the user can read

Today the pane renders only `Diagnostic.message` (`ui/window.py:775`), and the message is fixed per
error code (`diagnostics/catalog.py:87` gives `unsupported_operation` backend wording). A refused
mode therefore explains nothing specific, and an X11 user would be told to "update doubletake" for a
Wayland-only feature. The design closes that gap with a closed vocabulary rather than by exposing
diagnostic internals:

- **`domain/resources.py`** gains `ModeRefusal(StrEnum)` with exactly
  `session`, `dependency`, `backend`, `inventory`, `helper`, and `CapabilityReport` gains
  `mirror_refusal: ModeRefusal | None` and `virtual_display_refusal: ModeRefusal | None`.
- **`policy/capabilities.py`** computes both from the same evidence that sets the booleans, with the
  documented precedence `session → dependency → backend → inventory → helper`. The invariant
  `report.mirror is (report.mirror_refusal is None)` (and its virtual-display twin) is enforced by
  test, so the two forms of the same fact cannot drift.
- **`ui/state.py`** gains `PaneState.mode_reason: str | None`, filled by `project()` from a table
  that is total over `(Mode, ModeRefusal)`; when the visible mode is available the field is `None`.
- **`ui/window.py`** renders it as one muted line under the mode selector, hidden when `None`
  (mockup option A).

Fixed sentences (pinned byte-for-byte by the specification):

| Refusal | Mirror | Virtual display |
| --- | --- | --- |
| `session` | `Mirror needs KDE Plasma 6 on Wayland or X11.` | `Virtual display needs KDE Plasma 6 on Wayland.` |
| `dependency` | `A required component is missing; see the diagnostics.` | same sentence |
| `backend` | `The installed backend does not provide monitor capture.` | `The installed backend does not provide virtual outputs.` |
| `inventory` | `No physical monitor could be verified in this session.` | `No output inventory could be verified in this session.` |
| `helper` | — (not reachable for mirror) | `The virtual-display helper (krfb-virtualmonitor) is not available or unsupported.` |

No diagnostic token (`x11`, `physical_source`, `owned_daemon_control`) ever reaches the user through
this line. The existing diagnostic area keeps its current behaviour.

### 5.6 Virtual display stays Wayland-only

`KdeVirtualDisplayProvider`, `VirtualHelper` and `display/creation.py` are untouched. The
`_session_is_kde_wayland` defence at `display/creation.py:319` remains, and the capability gate
refuses first, so an X11 user never reaches a helper probe as if it were supported.

### 5.7 `--check-environment` stays a bootstrap view

That command composes `EnvironmentProbe` with a process and the virtual helper only (`app.py:88-95`),
so it has no capture provider and no composed backend evidence. It reports `mirror=False` on
**every** session type, Wayland included, and this design does not change it. Its role in the
acceptance run is to show the recognised session label; mirror availability is read from the running
dashboard, which composes the production graph. §8 pins this documented limitation with a test so it
cannot silently become a false claim in either direction.

## 6. Change inventory

| File | Change |
| --- | --- |
| `src/airplay_linux/policy/capabilities.py` | X11 profile tokens; `classify_environment` branch; `MIRROR_ENVIRONMENTS` / `VIRTUAL_DISPLAY_ENVIRONMENTS`; per-mode gating and environment diagnostics; label mapping; `ModeRefusal` computation |
| `src/airplay_linux/domain/resources.py` | `ModeRefusal`; `CapabilityReport.mirror_refusal` / `.virtual_display_refusal`; docstring: `environment` is a display label |
| `src/airplay_linux/diagnostics/activity.py` | `_SAFE_ENVIRONMENTS` becomes the label set |
| `src/airplay_linux/ui/state.py` | `PaneState.mode_reason`; total `(Mode, ModeRefusal) → sentence` table |
| `src/airplay_linux/ui/window.py` | one muted reason line under the mode selector, hidden when `None` |
| `docs/superpowers/specs/assets/2026-11-06-x11-mode-reason-mockup.png` | chosen mockup (option A) |
| `tests/policy/test_capabilities.py` | environment matrix; mirror admitted on `kde-plasma-6-x11`; virtual display refused there with its own detail; refusal/boolean invariant |
| `tests/runtime/test_capability_report.py` | expected environment label (was the token) and refusal fields |
| `tests/diagnostics/test_safety.py` | the label set is accepted; unknown labels still normalise to `Unknown` |
| `tests/ui/test_state_projection.py`, `tests/ui/test_presenter.py`, `tests/ui/test_dashboard_widgets.py` | `mode_reason` mapping totality; the line renders; the line hides when the mode is available |
| `tests/environment/test_live_x11.py` | new, opt-in, mirror-only live checks (session profile, inventory parse, portal introspection) |
| `tests/environment/test_live_kde.py` | unchanged — stays Wayland-only, because it creates a live virtual output |
| `docs/operations.md` | one paragraph: X11 mirror is supported on Plasma 6; virtual display is not |

No new dependency, command or D-Bus interface is introduced.

## 7. Error handling

| Situation | Observed behaviour |
| --- | --- |
| Plasma 6 X11, all mirror evidence present | mirror available; no environment diagnostic; mode-reason line hidden |
| Plasma 6 X11, virtual display selected | pane line `Virtual display needs KDE Plasma 6 on Wayland.` |
| Plasma 6 X11, backend or inventory proof missing | that proof's own diagnostic, and the mode-reason line naming it (`backend` or `inventory`) |
| Plasma 5 X11, other X11 desktop, unknown session | one environment diagnostic per unmet mode requirement; pane line `Mirror needs KDE Plasma 6 on Wayland or X11.` |
| Portal returns a non-monitor or unbindable stream on X11 | existing `unsupported_source` / `non_monitor_source` refusal |
| Layout lacks a position on X11 | existing `missing_position` refusal |

No new `ErrorCode` is added. No refusal is downgraded to a warning.

## 8. Test strategy

1. **Classification matrix** (unit): every profile token from its environment mapping, including X11
   with and without KDE, `KDE_SESSION_VERSION=5`, a missing version, mixed case, padded whitespace,
   and non-string values falling back to `unknown`.
2. **Label mapping** (unit): total over the profile set, labels unique, and the diagnostics set equal
   to the mapped labels.
3. **Refusal vocabulary** (unit): `mirror_refusal is None iff mirror`, same for virtual display; the
   documented precedence is exercised for every refusal value.
4. **Mode admission** (policy): mirror admitted for `kde-plasma-6-x11` when every other proof is
   present; refused when any single proof is removed, naming that proof; virtual display refused for
   `kde-plasma-6-x11` with `session`; both refused for `kde-plasma-5-x11`, `x11`, `gnome-wayland`.
5. **Pane reason** (projection): the `(Mode, ModeRefusal)` table is total and its sentences match
   §5.5 byte-for-byte; `mode_reason` is `None` for an available mode; the widget renders the line and
   hides it when `None`.
6. **Diagnostics** (boundary): each new label survives normalisation verbatim; an unknown label stays
   `Unknown`; the export never carries a token.
7. **Bootstrap limitation** (runtime): `--check-environment`'s report keeps `mirror=False` on an X11
   profile with the "physical capture provider" diagnostic, documenting the unchanged limitation.
8. **Live** (opt-in, `tests/environment/test_live_x11.py`): the session classifies as
   `kde-plasma-6-x11` on a real Plasma 6 X11 session; `kscreen-doctor -j` parses into a non-empty
   physical inventory; the portal advertises `MONITOR`; no helper is required. It never creates an
   output and never starts a stream.
9. **Regression**: the full existing suite, unchanged expectations for Wayland, virtual display and
   every refusal already pinned.

## 9. Live acceptance run

Run on this machine, in one session, with the operator watching:

1. `airplay-linux --check-environment` reports the session label `KDE Plasma 6 X11` (this command
   never proves capture — §5.7).
2. `airplay-linux` starts; the dashboard shows environment `KDE Plasma 6 X11` and offers mirror with
   `eDP-1` (2240x1400) listed. Selecting Virtual display shows
   `Virtual display needs KDE Plasma 6 on Wayland.`
3. Start mirror to `jj` (AppleTV3,2, 192.168.0.192); accept the desktop prompt for that monitor.
4. The TV shows this panel; moving a window is visible on the TV.
5. Stop; the receiver returns to idle, no helper process survives, and the portal session closes.

A failure at step 3 or 4 is a finding for this design, not a reason to relax a proof. The exact
failing command or diagnostic is recorded with its output.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| KWin's X11 portal path may report a `mapping_id` that does not equal the `kscreen-doctor` connector name | the binding proof (`capture/portal.py:311-315`) refuses rather than substitutes; §9 establishes the truth before the feature is claimed |
| The backend proof on this host comes from the owned-daemon composition, not from help text; `--check-environment` cannot show it | §9 uses the real application; a backend shortfall is reported as a host condition, never fixed by weakening the gate |
| Changing `CapabilityReport.environment` from token to label touches diagnostics, UI and runtime tests | the tests already assert labels; the mapping test makes the change total and mechanical, and §6 lists every affected file |
| X11 `kscreen-doctor -j` omits the `geometry` key that `-o` prints | the parser consumes `pos` and `size` (`display/kscreen.py:409,411`), which X11 reports; a missing field yields the existing typed refusal |
| A closed refusal vocabulary could drift from the booleans it explains | the `is None iff boolean` invariant is enforced by test in §8.3 |

## 11. What must not regress

- Mirror and virtual display on KDE Plasma 6 Wayland behave exactly as today.
- Every existing refusal keeps its code and its text where the situation is unchanged.
- Virtual display never becomes available on an X11 session, by any path.
- The portal remains the only capture mechanism; no shell command gains a new executable.
- The diagnostics boundary stays closed: no caller text reaches the export, and an unrecognised
  label is still `Unknown`.
- No diagnostic token is rendered in the pane's reason line.
- Capture-source verification keeps refusing windows, virtual outputs, unknown connectors and
  ambiguous matches.
