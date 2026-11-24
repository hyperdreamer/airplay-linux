# KDE Plasma 6 X11 Mirror support — design

Date: 2026-11-24
Status: revision 4 — design review rounds 1–3 addressed
Topic slug: `kde-plasma6-x11-mirror`

## Background

Mirror mode sends one physical monitor to an AirPlay receiver. The mode's
capability is computed in `src/airplay_linux/policy/capabilities.py`:

```python
mirror = bool(environment_supported and mode_ready and inventory and physical_source)
```

`environment_supported` is `environment == KDE_PLASMA_6_WAYLAND`, and
`classify_environment` returns the token `x11` for **every** X11 session before it
looks at the desktop at all:

```python
if session == "x11":
    return DesktopEnvironment.X11.value
```

So a KDE Plasma 6 X11 session and an arbitrary GTK X11 session are the same
token, and Mirror is refused before any backend, capture, or monitor evidence is
consulted.

### Read-only evidence collected for this design

Live host session (from `loginctl show-session` and the running
`plasmashell` environment):

```text
loginctl session 3: Desktop=KDE  Type=x11  State=active
XDG_SESSION_TYPE=x11      XDG_CURRENT_DESKTOP=KDE   XDG_SESSION_DESKTOP=KDE
KDE_SESSION_VERSION=6     KDE_FULL_SESSION=true     DESKTOP_SESSION=plasmax11
DISPLAY=:0                WAYLAND_DISPLAY=<unset>
plasma-desktop 6.7.5      plasma-workspace 6.7.5    kscreen 6.7.5
flatpak present at /usr/bin/flatpak
```

Display inventory (`kscreen-doctor -j`, read-only): `eDP-1` connected, enabled,
`type` 7 (panel), `scale` 1, 2240x1400 at +0+0; every other connector
disconnected.

Backend contract (`doubletake --help`, `doubletake -capabilities`,
`doubletake-ctl --help`, read-only, binaries at `/data/home/bin`):

```text
capabilities-v1
status-source
status-source-complete
target-volume
```

That is exactly the recognized `doubletake-0.5+source` profile with complete
source reporting, multi-target control, and target volume, so
`features_from_help` yields `source_reporting=True`,
`source_reporting_complete=True`, `multi_target=True`, `target_volume=True`, and
`credential_relay=True`, while `virtual_source` and the four transactional
pairing flags stay False.

`tests/environment/test_installed_contracts.py` passes 3 / skips 2 against those
installed binaries when `/data/home/bin` is on `PATH`. Its assertions classify
whatever the installed binaries actually print; supporting fixture-based cases
elsewhere in the suite do not substitute for that read-only observation.

### What the evidence does **not** prove

The installed binary carries a native X11 capture path — `probeX11CaptureSource`,
`detectPrimaryMonitor`, `[CAPTURE] primary monitor: %dx%d at +%d+%d`,
`[CAPTURE] cropping ximagesrc to x=%d..%d y=%d..%d`, `-x11-window-id`,
`-x11-window-name` — alongside the ScreenCast portal path. The carried source
reporting patches attach `SourceInfo` in `portalStreamSource` and expose it per
stream in `ctl status`, and `probeX11CaptureSource` exists for the X11 path.

Static inspection cannot prove that the X11 path sets `source_type`, `size` and
`position` for a monitor on a real X11 session. That payload is only observable
at runtime, and this workflow must not start a receiver stream. The design
therefore claims exactly this, and no more:

- the session gate is the only *classification* blocker;
- if the X11 path reports a complete monitor source, the existing verifier
  accepts it;
- if it does not, the existing verifier refuses with `unsupported_source`, and
  Mirror fails closed rather than accepting an unverified source.

End-to-end proof that the installed X11 capture emits complete source metadata is
a **live-host** verification, recorded as a residual risk and outside this
delivery.

### Why the user sees the wrong message today

On the live host the report is `environment="x11"`, `mirror=False`, and its
diagnostics contain `unsupported_operation` (detail `x11`) plus
`dependency_incompatible` (detail `virtual_source`). The UI ranking in
`src/airplay_linux/ui/state.py::_capability_diagnostic` is:

1. a diagnostic whose detail names the mode's own keywords
   (`physical`, `capture`, `portal` for Mirror);
2. a concrete cause code;
3. the first `dependency_incompatible`.

The token `x11` matches no Mirror keyword and is not a concrete cause code, so
Mirror renders the fixed catalog copy for `dependency_incompatible`:

> The installed backend version does not expose the required operations. Update
> doubletake and retry, or go back and choose a capability that is available.

That blames a correctly installed backend for a session-profile refusal. The
`unsupported_operation` copy has the same defect.

## Goals

1. KDE Plasma 6 X11 passes the Mirror session-environment gate when every other
   evidence probe already passes.
2. KDE Plasma 6 Wayland keeps its current Mirror and Virtual display decisions.
3. Virtual display stays limited to KDE Plasma 6 Wayland; it never becomes
   available on X11.
4. KDE Plasma 5 (X11 or Wayland), unverified KDE sessions, non-KDE X11, GNOME,
   and unknown sessions stay gated for both modes.
5. Every existing Mirror safety requirement is retained unchanged.
6. A session-profile refusal is named as a session-profile refusal in the UI, and
   never as a backend incompatibility, when the backend is compatible.

## Non-goals

- Plasma 5 X11 or Wayland Mirror support.
- Any desktop other than KDE Plasma 6.
- Any change to receiver-side behaviour, the credential store, the backend
  protocol, or the installed backend build.
- Any change to discovery, receiver listing, pairing, audio, or volume.
- Any new capture provider or fallback path.
- Re-ordering the existing connect/verify sequence in
  `DoubleTakeTransport._start` (see D6 and the risk list).

## Design

### D1 — Session vocabulary distinguishes KDE Plasma 6 on X11

`DesktopEnvironment` gains four X11 members, and `classify_environment` consults
the desktop identity for X11 exactly as it already does for Wayland:

| Session | Desktop tokens | `KDE_SESSION_VERSION` | Token |
| --- | --- | --- | --- |
| wayland | kde/plasma | `6` | `kde-plasma-6-wayland` (unchanged) |
| wayland | kde/plasma | `5` | `kde-plasma-5-wayland` (unchanged) |
| wayland | kde/plasma | other/empty | `kde-wayland-unverified` (unchanged) |
| wayland | gnome | — | `gnome-wayland` (unchanged) |
| x11 | kde/plasma | `6` | `kde-plasma-6-x11` (new) |
| x11 | kde/plasma | `5` | `kde-plasma-5-x11` (new) |
| x11 | kde/plasma | other/empty | `kde-x11-unverified` (new) |
| x11 | gnome | — | `gnome-x11` (new) |
| x11 | none recognized | — | `x11` (unchanged) |
| anything else | — | — | `unknown` (unchanged) |

The desktop token rule is the existing one and is shared by both branches: split
`XDG_CURRENT_DESKTOP` on `:` and require a `kde` or `plasma` token. The Wayland
branch's ordering, tokens, and results are byte-identical to today's behaviour;
`XDG_SESSION_DESKTOP` is deliberately **not** added, so no Wayland session is
reclassified.

### D2 — Two independent environment admissions

One boolean currently drives both modes. It becomes two constants:

```python
MIRROR_ENVIRONMENTS = frozenset({
    DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value,
    DesktopEnvironment.KDE_PLASMA_6_X11.value,
})
VIRTUAL_DISPLAY_ENVIRONMENTS = frozenset({
    DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value,
})
```

and the mode booleans become:

```python
mirror = bool(
    environment in MIRROR_ENVIRONMENTS and mode_ready and inventory and physical_source
)
virtual_display = bool(
    environment in VIRTUAL_DISPLAY_ENVIRONMENTS
    and mode_ready and kscreen and helper is not None and virtual_source
)
```

Nothing else in either predicate changes.

`additional_virtual_outputs` stays False on X11 because it is derived from
`virtual_display`, which is False there. There is exactly one suppressing
condition, not two: the helper may still exist and report
`distinct_outputs=True`, and the derived boolean must not be read as evidence
that the probe refused it.

**Adapter guard, stated accurately.** `display/creation.py::_session_is_kde_wayland`
refuses a session whose `XDG_SESSION_TYPE` is set to something other than
`wayland`, and refuses a session whose desktop tokens are set and do not contain
`kde`. An **unset** session type passes that guard. It is a second, weaker guard
for the creator transaction, not a proof that virtual display is unreachable on
X11. The mode-level guarantee comes from `virtual_display` (D2) and the helper
and `kscreen-doctor` evidence; the adapter guard is not modified and not relied
on as the primary control.

### D3 — The report says which modes the session profile blocks

`CapabilityReport` gains one defaulted field:

```python
session_blocked_modes: frozenset[Mode] = frozenset()
```

with `__post_init__` requiring a `set` or `frozenset` whose every member is a
`Mode` (anything else is a `TypeError`, matching the existing `volume` check) and
normalising the accepted value to a `frozenset` through `object.__setattr__`, so
a frozen report cannot be mutated afterwards through an aliased mutable set.
Because it is defaulted, every existing construction — tests, fixtures, the
acceptance rig — keeps working unchanged and means "the session blocks nothing".

The probe computes it once:

```python
session_blocked = frozenset(
    mode for mode, admitted in (
        (Mode.MIRROR, environment in MIRROR_ENVIRONMENTS),
        (Mode.VIRTUAL, environment in VIRTUAL_DISPLAY_ENVIRONMENTS),
    ) if not admitted
)
```

so on `kde-plasma-6-wayland` it is empty, on `kde-plasma-6-x11` it is
`{Mode.VIRTUAL}`, and everywhere else it is `{Mode.MIRROR, Mode.VIRTUAL}`.

**Invariant scope.** The field is a projection input, not a constructor
invariant: a hand-built report may set it without carrying the matching
diagnostic. The probe is the only producer that keeps the two in step, and it
always emits the diagnostic whenever the field is non-empty. Consumers must
handle the mismatch by synthesising the catalog copy (D5), which is why the
constructor validates only type and membership.

### D4 — A session-profile refusal has its own typed diagnostic

`ErrorCode` gains `unsupported_session`, with fixed catalog copy that names the
requirement rather than the backend. The exact copy is the approved Option B
wording from Step 2, recorded verbatim (199 printable characters):

> Mirror needs KDE Plasma 6 on Wayland or X11; Virtual display needs KDE Plasma 6
> on Wayland. This session does not provide the selected mode's desktop support.
> Log in to a supported session and retry.

Recovery actions: `retry`, `back`.

`tests/diagnostics/test_safety.py::test_every_error_code_is_pinned_or_deliberately_unpinned`
requires every member of `ErrorCode` to be either pinned to a design-matrix row
or listed in `UNPINNED_CODES` with prose. `unsupported_session` has no row in the
design's action matrix, so it is added to `UNPINNED_CODES` with a comment stating
that the session profile is a startup precondition with no recovery row, and that
its `retry`/`back` actions follow the existing capability-failure convention.

The probe emits exactly one such diagnostic — detail = the environment token —
**iff** `session_blocked_modes` is non-empty. The two are produced by the same
branch, so a probe-built report cannot claim a session block without its
explanation. The diagnostic replaces today's `_capability_failure(environment)`
note for unsupported sessions; every other diagnostic in `_probe` is emitted
exactly as before. In particular, a recognized production profile still withholds
`virtual_source` and the transactional pairing flags, so a
`dependency_incompatible` covering those surfaces remains present on every
session. The session diagnostic is distinguishable by its code, not by the
absence of that code.

### D5 — The UI names the session requirement for a session-blocked mode

Two rules are added to the pane construction. Both are narrow.

**Rule 1 — diagnostic choice.** `_capability_diagnostic` gains one rule ahead of
the existing ranking, keyed only on the selected mode, and the session diagnostic
is removed from the ordinary candidate pool for every mode it does not block:

```python
blocked = inputs.selected_mode in report.session_blocked_modes
if blocked:
    for item in report.diagnostics:
        if item.code is ErrorCode.unsupported_session:
            return item
    return _catalog(ErrorCode.unsupported_session, state.receiver_id)
candidates = tuple(
    item for item in report.diagnostics
    if item.code is not ErrorCode.unsupported_session
    and item.code not in _FOREIGN_LANE_CODES
)
```

The exclusion is the important half. The probe emits the session diagnostic
before the other probe diagnostics, so leaving it in the pool would let the
existing final `candidates[0]` fallback return it for an **admitted** mode — for
example Mirror on Plasma 6 X11 with a missing backend probe, whose only other
diagnostic is `dependency_missing`. With the exclusion, an admitted mode's
candidate pool is exactly today's pool and today's ranking, byte for byte.

Rationale, in both directions:

- A session-blocked mode can never start in this session, whatever the backend
  offers. Naming the backend first would be false, and acting on it (updating
  `doubletake`) cannot help.
- For an **admitted** mode the session rule never fires, so every existing
  preference is preserved: the mode's own provider still wins over a foreign
  lane, a concrete cause still wins over a generic incompatibility, and a missing
  `krfb-virtualmonitor` is still never blamed for a Mirror refusal.

**Rule 2 — a stored rejection may not mask a session-profile refusal.**
`Presenter._pane` currently replaces the projected diagnostic with any stored
`_rejections` entry for that receiver. `_fetch_sources` records one on a failed
`capture.list_physical()`, and `_start_session` records one when the remembered
screen memory cannot be cleared. Both would then show
`unsupported_source` — "The backend cannot verify this physical monitor. Select a
supported source or update doubletake." — even when the selected mode is
session-blocked and the real cause is the session profile.

The presenter therefore applies a stored rejection only when the selected mode is
**not** session-blocked. `_pane` already builds the projection inputs, so the
mode comes from that same object:

```python
def _pane(self, receiver_id: ReceiverId) -> PaneState:
    report = self._report()
    inputs = self._inputs(receiver_id, report)
    pane = project(self._snapshot(receiver_id), inputs)
    rejection = self._rejections.get(receiver_id)
    if rejection is not None and inputs.selected_mode not in report.session_blocked_modes:
        return replace(pane, diagnostic=rejection)
    return pane
```

`inputs.selected_mode` is the receiver-scoped mode produced by `_idle_mode`; the
presenter has no global selected-mode attribute and this rule must not introduce
one.

This is the smallest change that makes requirement 7 hold in the paths that
actually reach the screen. It does not suppress real rejections for an admitted
mode, and it does not touch phase, badges, or the action set.

Everything else in the pane is unchanged: `_capability_blocked` still keys on the
mode boolean, so a session-blocked mode keeps the existing pattern — `Retry`
offered, `Start` withheld, mode selector still enabled, capability notice shown.

### D6 — Safety requirements retained, and what "retained" means exactly

Mirror on X11 must clear the same evidence as on Wayland. Nothing in this design
relaxes any of it:

| Requirement | Where it still holds |
| --- | --- |
| owned-daemon control + keyring + multi-target | `mode_ready` is unchanged and still required by `mirror` |
| complete source reporting + wired verifier + physical inventory | `physical_source` is unchanged and still required by `mirror` |
| fresh monitor inventory | `inventory` (`CapturePort.list_physical`) is unchanged and still required by `mirror` |
| endpoint fencing, credential proof, ownership | `runtime.derive_composition_capabilities` and `_capability_gates` are untouched |
| physical-source verification per start | `DoubleTakeTransport._claim_streaming_readiness` + `KScreenReportedSourceVerifier` are untouched; a window, a virtual output, an ambiguous position, or a size mismatch is still refused with `unsupported_source` |
| owned cleanup | `session/cleanup.py` and the teardown authority are untouched |
| no operator-visible silent fallback | a refused source fails the start; it is never adopted as the stream |

Three properties must be stated precisely, because the naive reading is wrong:

**4. "No silent fallback" means no substitution, not a pre-emptive pin.**
Requirement 6 says "use the existing physical capture and source-verification
architecture" and "do not **silently fall back** to a different monitor, virtual
output, application window, or unrestricted desktop capture". Mirror satisfies
that literally: on a refusal the start fails and is torn down; nothing is
substituted for the unverified source, and the operator sees a typed diagnostic.
Two stronger readings were considered and are deliberately not adopted:

- *Verification before `connect`.* The reported source only exists after the
  backend starts, so an app-side barrier would need a backend contract that
  accepts a source selector — a backend protocol change, which requirement 9
defers to its own design route.
- *Connector pinning in adopt mode.* Choosing the connector app-side needs the
  ScreenCast portal, which the KDE backend serves over Wayland only; on X11 the
  backend decides. Pinning would change what Mirror means and would change
  Wayland behaviour, contrary to requirements 2 and 8.

Both readings are recorded as deferred items so the operator can promote either
one as a separate feature.
sends `StartRequest(mode=MIRROR, source=None)`, so
`DoubleTakeTransport._poll_reported_source` verifies with `requested=None`. In
that adopt mode `verify_reported_source` accepts any **uniquely matching physical**
monitor — it proves the source is a real, enabled, connected, non-virtual
physical output at a known position with a matching size, and it does **not**
compare it with an operator-selected or primary connector. There is no
connector-pinning guarantee to preserve, on either compositor. "No silent
fallback" therefore means: no window, no virtual output, no foreign/ambiguous
position, and no unverifiable source is ever accepted — not that a specific
connector is enforced.

**2. Verification follows the backend's connect, not the reverse.**
`_start` dispatches `connect` to the backend, waits for the daemon's status to
report that target streaming, and only then calls
`_claim_streaming_readiness`, which runs `_verify_reported_source` and returns the
refusal **before** `stream.ready = True` and before the `ready` transport event is
published. A refusal therefore fails and tears down the start; it does not
prevent the backend from having begun capture and transmission. This ordering is
identical on Wayland and X11 and is not changed by this design. Closing the gap
(verify before connect) would require pinning the source before the backend
reports it, would change shared Mirror behaviour on Wayland, and is out of scope.

**3. Mirror start has a Flatpak screen-memory precondition.**
`Presenter._start_session` clears the desktop's remembered ScreenCast approvals
before every Mirror start and refuses the start with `dependency_missing` when the
permission store is unavailable (`_clear_remembered_screen` over
`FlatpakScreenCastPermissions.forget_all`). The live host has `flatpak` at
`/usr/bin/flatpak`, so this precondition is satisfied there; a host without it
refuses Mirror on both compositors. It is recorded as a precondition, not a
regression.

The transient in-session label produced by `PaneState.awaiting_capture`
("Waiting for the screen prompt…") is also unchanged. On an X11 start the operator
sees no portal prompt, so the label is briefly inaccurate before it becomes
"Captured: <connector>". Changing that label is a shared Mirror-path UI change and
is deferred (see the risk list).

### D7 — Documentation

`README.md` requirements, `policy/capabilities.py`'s module docstring, and
`docs/operations.md` state the two environment guarantees:

- Mirror: KDE Plasma 6, Wayland **or** X11.
- Virtual display: KDE Plasma 6 Wayland only.

`docs/backend-compatibility.md` is factual about the pinned help contract and is
not edited.

### D8 — Pairing reachability follows from the mode becoming available

On X11 today the pane can never reach the `available` decision for either mode,
so `Action.pair` is unreachable there. Once Mirror is admitted, the pane reaches
`_can_pair`, and Pair is offered when `remembered_pairing` is independently true.

This is the intended consequence of the mode becoming available, not a capability
change: `remembered_pairing`, the pairing actor, the credential relay, and the
trust state machine are all untouched, and the button's availability obeys the
same rule as on Wayland. The design records it explicitly so the planner does not
treat it as a regression, and pins it with a test that a session-blocked mode
still offers only Retry while an admitted mode's actions come from the unchanged
`_can_start`/`_can_pair` rules.

## Error handling

| Situation | Result |
| --- | --- |
| Plasma 6 X11, every probe green | `mirror=True`, `session_blocked_modes={VIRTUAL}`, one `unsupported_session` diagnostic |
| Plasma 6 X11, `physical_source` unproven | `mirror=False`, diagnostics include `dependency_incompatible("physical_source")`; the UI shows that, not the session copy, because Mirror is session-admitted |
| Plasma 6 X11, inventory probe fails | `mirror=False`, `unsupported_source` cause; a stored rejection shows that cause, not the session copy, because Mirror is session-admitted |
| Plasma 6 X11, Virtual selected | `virtual_display=False`, UI shows `unsupported_session`, and a stored rejection does not override it |
| Plasma 5 / unverified KDE / GNOME / other X11 | both modes `False`, both modes show `unsupported_session`, rejections do not override it |
| Report claims a session block but carries no session diagnostic | UI shows the catalog copy for `unsupported_session` |
| `session_blocked_modes` is not a set of `Mode` | `TypeError` at construction (fail closed, never silently ignored) |

## Test strategy

Deterministic, offline, injected probes only — no real session, D-Bus, executable
lookup, receiver, or helper.

`tests/policy/test_capabilities.py`

1. `classify_environment` matrix: `kde-plasma-6-x11`, `kde-plasma-5-x11`,
   `kde-x11-unverified`, `gnome-x11`, unchanged `x11` for an unrecognized X11
   desktop, and the five unchanged Wayland rows. The existing matrix case that
   asserted `x11` for the KDE-tokened mapping is replaced by a genuinely
   non-KDE X11 mapping.
2. Plasma 6 X11 with complete positive evidence: `mirror is True`,
   `virtual_display is False`, `additional_virtual_outputs is False`,
   `session_blocked_modes == frozenset({Mode.VIRTUAL})`.
3. Plasma 6 X11: exactly one `unsupported_session` diagnostic, and no
   `dependency_incompatible` whose detail names a **Mirror** prerequisite
   (`physical_source`, `owned_daemon_control`, `keyring`, `multi_target`). A
   `dependency_incompatible` for `virtual_source` is still expected and asserted,
   because the recognized profile never affirms it.
4. Each missing prerequisite on Plasma 6 X11 stays independently diagnosable and
   keeps Mirror refused: `keyring`, `owned_daemon_control`, `multi_target`,
   `physical_source`, an absent capture provider, an empty inventory, and an
   inventory probe failure.
5. Every unsupported environment keeps both modes refused and reports
   `session_blocked_modes == frozenset({MIRROR, VIRTUAL})`.
6. Plasma 6 Wayland: the existing full matrix is unchanged, including that its
   `session_blocked_modes` is empty.
7. `can_start` admits Mirror and refuses Virtual on Plasma 6 X11.
8. `CapabilityReport(session_blocked_modes=...)` accepts `frozenset`/`set` of
   `Mode` and rejects a non-set or a non-`Mode` member. Construct from a mutable
   `set`, mutate that set, and assert the report's field is unchanged, so the
   alias-immunity requirement has its own assertion.

`tests/runtime/test_capability_report.py` (production composition, X11)

9. Simulated Plasma 6 X11 with the real fixture help bytes: `mirror=True`,
   `report.environment == "kde-plasma-6-x11"`,
   `session_blocked_modes == {VIRTUAL}`.
10. Negative production cases on the same X11 environment, each keeping
    `mirror=False` with its own cause: withheld `status-source-complete`
    (`physical_source`), a failing credential leg (`keyring`), and a missing
    backend probe (its own typed diagnostic). These mirror the existing
    `test_withheld_source_token_keeps_mirror_refused` and
    `test_credential_relay_does_not_replace_the_composed_keyring_start_gate`
    patterns on X11, so a collapsed negative cause cannot pass unnoticed.
10a. Composition-level ownership and admission evidence on X11: a failing
    `_probe_bootstrap` prerequisite keeps `mirror=False`, records its own
    `gated_capabilities` entry with that exact label, and does not surface the
    session diagnostic for Mirror.
10b. Composition-level verifier and inventory evidence on X11: an installation
    with no source verifier, and one whose verifier reports
    `physical_inventory_available=False`, each keep `mirror=False` with the
    distinct `source_verifier` / `physical_inventory` gate and the
    `dependency_incompatible` cause that belongs to it.
11. The same composition on Wayland still produces `mirror=True` and
    `virtual_display=False` (krfb absent), i.e. unchanged.

`tests/ui/test_state_projection.py`

12. Mirror selected on a Plasma 6 X11 report blocked by `physical_source` shows
    `physical_source`, not `unsupported_session`.
12a. **The fallback case that motivates the candidate-pool exclusion.** A report
    whose only diagnostics are the emitted `unsupported_session` (because the
    session blocks Virtual) followed by `dependency_missing`, with
    `session_blocked_modes == {VIRTUAL}` and `mirror=False`, must show
    `dependency_missing` when Mirror is selected. This is the exact shape where
    the mode-keyword pass cannot match and the old `candidates[0]` fallback would
    have returned the session diagnostic; a fix that only adds the early return,
    without removing the code from the pool, fails this test.
13. Mirror selected on a session-blocked report (Plasma 5, unverified KDE, GNOME,
    other X11) shows `unsupported_session` and not a backend incompatibility;
    `Start` withheld, `Retry` offered, `capability_blocked` True.
14. Virtual selected on the Plasma 6 X11 report shows `unsupported_session`.
15. A report that declares a session block but carries no session diagnostic
    still shows the catalog copy for `unsupported_session`.
16. All existing capability-diagnostic preference tests keep passing.

`tests/ui/test_presenter.py`

17. A stored rejection (`unsupported_source` from a failed source fetch) does not
    override the session diagnostic for a session-blocked mode, and still does
    override for an admitted mode.
18. An admitted Mirror mode on Plasma 6 X11 offers the unchanged
    `_can_start`/`_can_pair` action set, proving pairing availability follows the
    existing rules rather than a new one.

`tests/diagnostics/test_safety.py`

19. The new code is recorded in `UNPINNED_CODES` with its prose, so
    `test_every_error_code_is_pinned_or_deliberately_unpinned` and the
    "every error code has a helpful diagnostic" and "messages are distinct" tests
    pass. A test asserts the copy is at most 240 characters, printable, and free
    of placeholder syntax.

`tests/backend/test_contract.py` and
`tests/environment/test_installed_contracts.py`

20. Backend profile recognition is unchanged and does not depend on the desktop
    token: the same help/`-capabilities` bytes yield the same profile under any
    session environment, and the installed-contract selection still classifies the
    installed binaries.

## Design-review findings and disposition

Round 1 returned 14 findings. Each was re-checked against the sources.

| Finding | Severity | Disposition |
| --- | --- | --- |
| F1 connect precedes source verification | Critical | **Accepted as fact, out of scope.** Verified in `_start` → `_claim_streaming_readiness`. Pre-existing on Wayland; requirement 6 mandates the existing architecture and requirement 8 forbids changing Wayland behaviour. Recorded in D6 (2) and the risk list; a fix would be a separate feature. |
| F2 adopt mode does not pin a connector | Important | **Accepted.** D6 (1) corrected. |
| F3 X11 source payload unproven | Important | **Accepted.** New "What the evidence does not prove" section; the guarantee is fail-closed refusal, and live proof is a recorded residual risk. |
| F4 Flatpak screen-memory precondition and the transient prompt label | Important | **Accepted as fact.** Verified `_start_session`/`_clear_remembered_screen`/`awaiting_capture`; `flatpak` is present on this host. Recorded in D6 (3) as a precondition; the transient label is deferred. |
| F5 session diagnostic can win for an admitted mode | Important | **Rejected.** The D5 rule is `selected_mode in session_blocked_modes`; on Plasma 6 X11 that set is `{VIRTUAL}`, so Mirror is unaffected. Pinned by tests 12 and 13. |
| F6 a stored rejection masks the session diagnostic | Important | **Accepted.** New D5 Rule 2 and tests 17. |
| F7 adapter guard accepts an unset session type | Important | **Accepted.** D2 corrected; the mode boolean is the primary control. |
| F8 Pair becomes reachable once Mirror is admitted | Important | **Accepted.** New D8 plus tests 13 and 18; pairing capability itself is unchanged. |
| F9 negative evidence not proven through production composition | Important | **Accepted.** Test item 10 adds X11 production negative cases. |
| F10 "no `dependency_incompatible`" conflicts with `virtual_source` | Important | **Accepted.** Test item 3 reworded to name Mirror prerequisites only. |
| F11 new code breaks the pinned-or-excused inventory | Important | **Accepted.** D4 and test item 19 record the `UNPINNED_CODES` entry. |
| F12 environment token does not reach the diagnostics export | Minor | **Accepted.** Removed; the export normalizes unknown labels and carries codes, not details. |
| F13 "two independent reasons" for `additional_virtual_outputs` | Minor | **Accepted.** D2 corrected to the single derived condition. |
| F14 invariant scope ambiguous | Minor | **Accepted.** D3 now states the field is a projection input, not a constructor invariant. |

Round 2 returned six findings. Each was re-checked against the sources.

| Finding | Severity | Disposition |
| --- | --- | --- |
| G1 connect precedes verification (F1 upheld) | Critical | **Rejected on the operator's own wording.** Requirement 6 says "use the existing physical capture and source-verification architecture" and "do not **silently fall back**". A refusal fails and tears down the start; nothing is substituted for the unverified source. A pre-connect barrier needs a backend contract that accepts a source selector, which requirement 9 defers. Recorded in D6 (4) and deferred. |
| G2 adopt mode does not pin a connector (F2 upheld) | Important | **Rejected on the operator's own wording.** Requirement 1 says Mirror "must continue to mean" the existing path. Connector pinning changes that meaning and changes Wayland behaviour (requirements 2 and 8). Recorded in D6 (4) and deferred. |
| G3 the session diagnostic can still win as the fallback for an admitted mode | Important | **Accepted — a real defect.** The probe emits the session diagnostic first, so the existing `candidates[0]` fallback could return it for an admitted Mirror mode with a missing backend probe. D5 Rule 1 now removes it from the candidate pool for any mode it does not block, and new test 12 pins that. |
| G4 production negatives omit ownership, verifier, and inventory evidence | Important | **Accepted.** Test items 10a and 10b added. |
| G5 `self._selected_mode` is not a presenter attribute | Minor | **Accepted.** D5 Rule 2 sample rewritten against `_inputs(...).selected_mode`. |
| G6 a mutable set can mutate a frozen report | Minor | **Accepted.** D3 normalises to `frozenset` through `object.__setattr__`. |

Round 3 overturned both contested rejections and confirmed the four repairs, then
returned two test-coverage findings.

| Finding | Severity | Disposition |
| --- | --- | --- |
| H1 test 12 does not pin the fallback case | Important | **Accepted.** Test item 12a added: the missing-backend shape that the old `candidates[0]` fallback would have answered with the session diagnostic. |
| H2 no alias-mutation assertion | Minor | **Accepted.** Test item 8 now mutates the source set after construction. |

## Risks

- **The X11 source payload is not proven offline.** This is the largest risk. If
  the installed X11 path does not report complete monitor metadata, the existing
  verifier refuses and Mirror does not work on X11 — but nothing unsafe is
  accepted. Live verification is deliberately excluded from this workflow.
- **Connect precedes verification (F1).** Unchanged, pre-existing, identical on
  both compositors. A refused source may be briefly transmitted before teardown.
- **Flatpak screen-memory precondition (F4).** `flatpak` must be installed for any
  Mirror start, on either compositor.
- **Transient "Waiting for the screen prompt…" label (F4).** Deferred; it is a
  shared Mirror-path label, not a session-gate diagnostic.
- **Scaled X11 outputs.** If a host's output scale is not 1, the backend's
  reported pixel size and the KScreen size can disagree and
  `verify_reported_source` refuses with `SIZE_MISMATCH`. Existing fail-closed
  behaviour, not weakened here.
- **Diagnostic precedence.** Rule 1 is inert for admitted modes and Rule 2 only
  suppresses a rejection where the mode is session-blocked; tests 12, 13, and 17
  pin both directions.
- **Token growth.** Four new tokens reach `ActivityLog`'s sanitized capability
  snapshot, whose allowlist is `{"KDE Plasma 6 Wayland"}`; every token still
  normalizes to `Unknown` there. Unchanged, out of scope.

## Deferred items

| Item | Why deferred |
| --- | --- |
| Verify the reported source before the backend connects | Changes shared Mirror transport behaviour on Wayland; needs a backend contract that accepts a source selector (requirement 9) |
| Pin the connector in adopt mode | Needs the ScreenCast portal to choose the connector, which the KDE backend serves over Wayland only, and it changes what Mirror means on Wayland |
| `awaiting_capture` label on an X11 start | Shared Mirror-path UI text, not a session-gate diagnostic |
| Live end-to-end X11 capture proof against a receiver | Excluded from this workflow by the operator's restriction |

## Decisions recorded in collaborative design

| Decision | Choice |
| --- | --- |
| Session vocabulary | separate X11 members for KDE Plasma 6, KDE Plasma 5, unverified KDE, and GNOME; `x11` retained for an unrecognized X11 desktop |
| Mode admission | two environment sets; Mirror admits Wayland and X11, Virtual display admits Wayland only |
| Session-blocked reporting | `CapabilityReport.session_blocked_modes` (defaulted, so existing constructions are unchanged) |
| Session diagnostic | new `ErrorCode.unsupported_session`, emitted only when the session blocks a mode |
| Diagnostic precedence | session rule first for a session-blocked mode; today's ranking otherwise; a stored rejection may not mask it |
| Operator-visible copy | **Option B** (the wording in D4) — chosen by the operator in Step 2 |

## Addenda

- The disregarded historical commit `c6e0e933e3a158f58c4761109ed67c4a49ea82b1`
  was not read, cherry-picked, or used as authority. This design is derived from
  the current HEAD, the current sources, the current tests, and the read-only
  host evidence recorded above.
