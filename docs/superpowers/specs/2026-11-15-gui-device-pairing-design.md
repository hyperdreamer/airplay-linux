# Device pairing from the dashboard — mirror mode

**Status:** design under review (round 8)
**Run:** `pm-run-20261115-031248-7c91beef`
**Target branch:** `master` at `afc07b2`
**Superseded in part by Amendment D1 (§12):** the relay ownership, settlement and
presentation clauses of §§5.4–5.7 and 5.13 now read through §12, which removes the
dashboard's dispatch authority entirely.

**Supersedes:** SD-1 term (1) and the pairing-submission clause of owned-daemon control
Amendment 1 §3

## 0. Revision record

Round 4 resolved 9 items and raised 8 (3 blockers, all in the virtual-display path). The
operator reduced scope to **mirror mode only** and deferred virtual-display pairing with
its reason recorded as a deliverable note. This round:

- removes the virtual path and its three blockers from the contract (F22, F2/G3, F14);
- makes the submission's success **provisional** and the start the only completion point
  (F23, R5);
- defines one fenced, idempotent **release transition** with settlement before any
  disconnect (F24, R7, G8);
- defines the **termination proof** predicates and its history-tolerant lookup (F25, F4,
  G4);
- keeps retained outcomes inside the credential phase with the original expiry (F26, R13,
  G2);
- adds a **monotonic presentation revision** so repeated invalid input still reopens (F27,
  F9, G9);
- carries the start's audio and initial-volume choices into the credential command (F28);
- restricts the relay projection to the live challenge and to the fully idle state (F29).

Round 5 resolved 11 items and raised 4 (1 blocker). This round:

- makes **submission eligibility** require an unresolved waiting request, so provisional
  success cannot be answered twice (F30, R5, H2);
- names the **actual receipt fields** that prove teardown instead of an outcome value that
  does not exist (F31, F4, G4, H4);
- admits **retry from `retained`** and direct discharge by later termination evidence
  (F32, R7, G8, H3);
- classifies **uncertain dispatch** and forbids treating a completed task or an absence
  snapshot as settlement (F33).

Round 6 resolved 8 items and left **one** blocker: an uncertain command could be settled by
a *second* submission's progress. This round adds the exclusivity predicate that closes it
(F34), and the presentation, admission and fence all enforce the same predicate.

Round 7 resolved that blocker and left **one** conjunct missing: the release transition set
a fence flag that the shared submission predicate never read. This round adds
`lease.submit_fenced` to `can_submit`, keeps it set across retained retries, and adds the
release-versus-submit interleaving test (F35).

## 1. Decision record

| Question | Decision |
| --- | --- |
| Scope | Enter the PIN in the app; the receiver tool keeps the credential; no app credential store; no forget action |
| Prompt timing | The prompt opens as soon as the receiver asks |
| Who can prompt | Only a receiver whose start the operator initiated, or the reconnect of that start |
| Vanished waiting entry | Accepted: the targeted submit then starts that same receiver's connection with the PIN; it cannot reach another receiver because the command names the target |
| Modes | **Mirror only.** Virtual-display pairing is deferred; a virtual start that meets a credential request keeps today's refusal |

## 2. Background

### 2.1 The receiver tool owns credentials and answers a prompt

Installed `doubletake`/`doubletake-ctl` 0.5.0 plus the source and target-volume patches
publish `-cred-backend file|keyring`, `-creds`, `-pair`, `pin` (targetless),
`connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]` (targeted), and a
`status` reply whose aggregate fields describe one prompt while a per-stream array carries
each `target`, `state` and `credential_kind`.

Saved record: `{pairing_id, ed25519_public, ed25519_seed, pairing_protocol,
restore_token}` — no expiry. A receiver that forgets the pairing makes the daemon's
pair-verify fail; it reconnects and asks again. Production launch pins
`-cred-backend keyring`.

### 2.2 The application refuses pairing in three places

`_await_connecting_stream`, `_poll_reported_source` and `_after_connect` each turn a
prompting reply into `dependency_incompatible`; `begin_pairing` requires five capabilities
the public profile lacks and returns `keyring_unavailable`; the actor then leaves the pane
`Untrusted`/`CleanupRequired` with no challenge.

### 2.3 The GUI exists and is tested

`PairingDialog` renders offered actions and wipes secrets on every exit; the presenter
opens prompts from observed challenges; the projection models pairing actions.

### 2.4 Why the targetless `pin` is not used

`pin` maps to `client.Connect("", 0, arg)`; the daemon resolves an empty target as
`pending[0]`, refusing only for zero or several waiters — a receiver can begin waiting in
between. `connect <target> <credential>` resolves `d.streams[target]` and either resumes
that entry through `entry.credentialCh <- req.Pin`, or, when the entry has vanished,
continues into normal connection setup **for that same target** with the credential. Both
branches are target-bound.

## 3. Goals

A mirror start that meets a PIN request is answered from the dashboard and reaches
streaming; the app stores no credential; a receiver that asks again prompts again; the
receiver tool is unchanged.

## 4. Non-goals

Virtual-display pairing (deferred, §9); in-app forget/revoke; app-owned remembered trust;
capability tokens, Go patches or rebuilds; replacing the transactional trust path; wire
protocol changes.

## 5. Architecture

### 5.1 Capability and the one new mapping

`BackendFeatures.credential_relay` is true only when the decoded ctl surface equals a
pinned table and pinned verification succeeded. `try_argv` returns a typed command so
redaction metadata travels with it:

```python
@dataclass(frozen=True)
class ProfileCommand:
    argv: tuple[str, ...]
    secret_indices: tuple[int, ...] = ()
```

`submit_pairing` maps to the targeted credential form, carrying the original start's
proved options (F28):

| Case | argv |
| --- | --- |
| default | `connect <target> <credential>` |
| audio proven, audio off | `connect <target> --audio off <credential>` |
| audio + volume proven, initial volume set | `connect <target> --audio on --volume <n> <credential>` |

`secret_indices` is computed from the completed argv, never assumed. The existing payload
validation is reused unchanged: an initial volume is refused when audio is present and
disabled. All other operations return `secret_indices=()`. `begin_pairing`,
`cancel_pairing`, `prepare_pairing`, `commit_pairing` and `revoke_pairing` stay unmapped.

### 5.2 Authorization and fencing

`EvidenceAuthority.allows` gains exactly one branch:

```python
if operation is BackendOperation.submit_pairing:
    return (f.credential_relay and f.owned_daemon_control and f.multi_target
            and f.physical_source)
```

(`physical_source` rather than `physical_source or virtual_source`, because this delivery
is mirror-only.) The four transaction flags stay false. `submit_pairing` joins
`FENCE_REQUIRED_OPERATIONS`; a missing `dispatch_fence` is a refusal (`stale_target`),
never a skipped check. The fence re-evaluates receiver identity, endpoint generation,
session generation, owner lease, the waiting predicate, and the `can_submit` predicate of
§5.4 immediately before the spawn.

### 5.3 Observation

A `CredentialRequest` is derived only from a `streams[]` entry with
`state == pin_required`, `target == this stream's target` and a known `credential_kind`.
Aggregate fields are a hint to re-read status; they never identify a receiver and never
fail a start. Another target's prompt is ignored. A `pin_required` acknowledgement is a
hint to read status.

The request lives on the stream record and carries `receiver_id`, `target`,
`credential_kind`, `endpoint_generation`, `session_generation`, `owner_lease`,
`daemon_instance_uuid`, `generation`, `observed_at`, and the original start's option
snapshot used by §5.1. A kind change closes the current generation as `Stale` and mints a
new one with a fresh attempt counter.

### 5.4 Ownership, barrier, and the single completion point (F23, R5)

Relay ownership is the session's mirror start intent for that receiver:
`(receiver_id, endpoint_generation, session_generation, owner_lease)`. A challenge is
minted only while that intent exists.

**Submission is an owned acquisition.** Before the credential child spawns, the stream
records `submit_inflight = generation`; it clears only after the reply *and* one status
read that resolves the target's entry. While it is set: absence of the target's entry is
not failure; the same intent adopts whatever entry exists afterwards (resumed or freshly
created) provided target, endpoint generation and daemon instance match; release and
cancel join the barrier.

**Submission success is provisional (F23).** A submission that observes `streaming`
records `success_pending = (generation, attempt, observed_at)` and **does not close the
challenge**. The start is the only completion point.

**Eligibility while success is pending (F30).** `success_pending` retires the credential
input without closing the challenge: the prompt stops accepting a value, the pane shows
"Pairing with <TV>; waiting for it to start.", and only `cancel` remains available. No
presentation revision is advanced for a provisional success, so no fresh dialog can open
for the same generation. A second submission is impossible while `success_pending` is set
or while any accepted progress (`prepared source`, `streaming`) is observed. This removes
the interval in which the prompt could reopen between observing `streaming` and returning
the handle while source verification is still outstanding.

**One eligibility predicate (F34).** Exactly one function decides whether a submission may
start, and presentation, command admission and the spawn fence all call it:

```
can_submit(lease) =
      a request generation is live for this target
  and the request is unresolved (not superseded, not stale)
  and submit_inflight is clear
  and lease.submit_fenced is false               -- set by release, cleared only on settle
  and dispatch_state(lease) ∈ {none, settled}      -- never `uncertain`
  and success_pending is not set
  and no accepted progress (prepared source, streaming) is observed
```

`submit_fenced` is the same flag the release transition sets before it settles the barrier,
so beginning a release makes the lease ineligible **atomically**: a submission that would
otherwise pass every other conjunct fails on this one, at presentation, at command
admission and at the spawn fence alike. The flag stays set for the whole release attempt,
including a `retained` outcome and its later retries, and is cleared only when the lease is
`released` or a new generation is minted by a new start.

`dispatch_state` is per lease, not per coroutine: a command whose client task returned while
its child is still live stays `uncertain`, so it keeps `can_submit` false. A second
submission is therefore impossible while any dispatch for that lease is unresolved, and
progress can only be attributed to the one command that was admitted.

| Event | Effect |
| --- | --- |
| start returns its handle, challenge live or `success_pending` set | commit `Trusted`, close the challenge as `succeeded`, admit the handle |
| start returns its handle, no challenge and no `success_pending` | unchanged existing path |
| start fails while `success_pending` is set | discard `success_pending`; challenge closes with the start's code; cleanup |
| start fails, no `success_pending` | challenge closes with the start's code; cleanup |
| submission reports `Ended`/`Stale` while the start still waits | challenge closes; start fails through the existing readiness failure |

A closed-as-`succeeded` challenge never causes the owner's result to be discarded; the
existing `Trusted` handler's immediate challenge clear is retained only after the trust
commit that this table names. Tests cover both arrival orders and the
`success_pending`-then-timeout case.

### 5.5 Outcomes

| Outcome | Evidence | Challenge | Trust | Code |
| --- | --- | --- | --- | --- |
| `Trusted` (provisional) | this target's entry is `streaming` | **kept**, input retired, cancel only | `Pairing` | none |
| `Waiting` | entry still `pin_required`, same kind, attempts remain | kept | `Pairing` | `receiver_rejected` |
| `Ended` | no entry after the barrier settles | closed | `Untrusted` | `receiver_rejected` |
| `Expired` | challenge lifetime elapsed | closed | `Untrusted` | `pairing_expired` |
| `Stale` | generation, lease or kind changed | closed | `Untrusted` | `stale_target` |
| `Rejected` | daemon refused (§5.11) | kept while attempts and owner live | `Pairing` | per §5.11 |

`PairingResult` gains `waiting: bool = False`. The success kind stays `Trusted`.

### 5.6 Actor routes: zero trust-store calls

Relay mode makes no `prepare_pairing`, `commit_pairing`, `cancel_pairing`,
`mark_invalid`, `forget_trust`, `revoke_pairing` or `retry_revocation` call; a store spy
asserts it on every route.

| Route | Behavior |
| --- | --- |
| operator cancel | fence submissions, settle the barrier, run the single release (§5.7), close the challenge, `Untrusted` |
| challenge expiry | same release path, `pairing_expired` |
| `credential_invalid` backend event | `Ended`; release; no `mark_invalid`, no revocation |
| endpoint replaced / receiver removed | `Stale`; release; `Untrusted` |
| shutdown | release path only; no revocation |
| late submit completion | accepted only with matching generation, endpoint generation and owner lease |

`Waiting` and `Rejected` retain `Pairing` only while the owner and lease are live;
`Untrusted` applies when the challenge closes.

### 5.7 One fenced, idempotent release transition (F24, R7, G8)

A single `release(lease, evidence)` transition owns every release. It is idempotent: a
stream carries `release_state ∈ {none, settling, released, retained}`, and a second caller
observes the settled state instead of acting again. Exactly **one** transport child is
spawned per release attempt, so disconnect counts are assertable.

Order is fixed:

1. fence further submissions by setting `lease.submit_fenced = true` — the conjunct the
   shared `can_submit` predicate reads, so no submission can pass admission afterwards;
2. **settle** any in-flight credential child, bounded by `CONTROL_TIMEOUT`; a barrier that
   does not settle ends the attempt as `retained` with `cleanup_timeout` and no disconnect;
3. only then evaluate evidence and, if needed, dispatch one `disconnect <target>`;
4. record the outcome.

Release evidence, in order of preference:

1. a successful `disconnect <target>` acknowledgement;
2. a fresh status proving the target has **no** entry, same daemon instance and endpoint
   generation;
3. a **termination proof** for the exact original daemon instance (§5.8);
4. otherwise `retained` with `cleanup_timeout`; the lease stays held.

A replacement daemon's absence of the stream is never sufficient on its own. Cancellation,
failed-start cleanup and ordered cleanup all enter this one transition; none of them may
disconnect independently.

**Retry from `retained` (F32).** `retained` is a live obligation, not a terminal state, so
the transition admits retries on a defined schedule rather than refusing them:

- a later `release(lease, evidence)` call is admitted when the previous attempt has
  settled as `retained` and no disconnect of that lease is outstanding;
- a concurrent caller **joins** the current attempt instead of starting a second one;
- newly available termination evidence (§5.8) discharges a retained lease **directly** to
  `released` without a disconnect, because nothing is left to disconnect;
- `retained` never permits a second disconnect while a previous one is unacknowledged;
- the existing cleanup retry path for a held resource is the caller, so no new lease is
  allocated for a retry.

The transition is serialized per lease, so concurrent cancellation and owner failure
produce one attempt and one terminal record.

### 5.8 Termination proof (F25, F4, G4)

```python
registry.termination_proof(record: DaemonRecord) -> Result[TerminationProof]
```

A `TerminationProof` is valid only when **all** hold:

1. it names the exact `DaemonRecord` identity — instance UUID, pid, `start_ticks` and
   runtime directory;
2. the registry holds a terminal record for that instance that establishes **teardown**, in
   the vocabulary the code actually uses: a `LaunchReceipt` with `state == "stopped"`, or
   `state == "failed"` accompanied by a `DaemonCleanupIntent` whose `outcome` records a
   completed teardown. A receipt with `state == "failed"` and no completed cleanup intent —
   including the `failed-to-start` record published by `_fail_owned(..., refuse=True)` — is
   **not** evidence of termination. There is no `terminated` outcome value; the proof names
   the fields above and nothing else;
3. positive process evidence: the recorded pid is gone, or a pidfd for the recorded pid
   reports exit, with the `start_ticks` match established before that observation;
4. the receipt names the process whose termination it records.

The lookup is **history-tolerant**: it resolves the receipt by instance identity and does
not apply the current-generation admission that rejects an original record once a newer
generation occupies the same runtime directory (`daemon_registry.py:393`).

Consumption: active leases pass the proof as `release` evidence; retired stream records
(`_retired`) consume the same proof to clear their obligation and leave `_retired`. A
proof for any other instance is refused with `stale_event`. Tests: a live process with a
`failed-to-start` receipt yields no proof; an original daemon that terminated after a
replacement yields a proof that releases both its active and its retired lease.

### 5.9 Phases, suspension, and timers (F26, R13, G2)

One `RelayPhaseController` per relay-admitted stream is the single owner of every relay
deadline; the transport's poll consults it instead of its local `accepted`, `permission`
and `handshake` values, and the coordinator's scheduler is not used in relay mode.

| Phase | Entered when | Budget | Other clocks |
| --- | --- | --- | --- |
| `permission` | connect acknowledged | 300 s allowance | running |
| `credential` | a request is live for this target | 120 s challenge lifetime, absolute | permission/handshake suspended |
| `permission` (resumed) | the request is **gone** (entry no longer `pin_required`) and no prepared source yet | remaining allowance | running |
| `handshake` | a prepared source is observed | 20 s | running |
| `source` | streaming observed | 5 s | running |

Retained outcomes (`Waiting`, retained `Rejected`, spawn failure) **stay in the credential
phase** with the original absolute expiry: the submission result does not resume the
permission clock and does not rearm the challenge expiry. The permission clock resumes
only on evidence that the request has progressed, and its remaining allowance is preserved
by shifting the suspended deadline by the elapsed credential time.

The challenge expiry callback is scheduled once, tagged with the phase generation;
transitions increment the generation and a callback with a stale generation is ignored.
Only one budget is active at a time, so simultaneous expiry is impossible. The handshake
budget starts at the prepared-source boundary, not when the daemon leaves `pin_required` —
the pinned daemon sets `connecting` on credential receipt (`daemon.go:749`) while capture
permission is still ahead (`daemon.go:1133`).

Tests: a credential phase longer than 20 s does not fail the start; repeated failures
across the original expiry boundary still expire at the original deadline; a stale
callback is ignored; boundaries immediately before, at and after each budget.

### 5.10 Attempts, exhaustion, and termination (R5, F10)

- Exactly **three consumed submissions** per challenge generation.
- Consuming: `Waiting`, `Rejected`, spawn failure, daemon-shutdown refusal.
- Not consuming: client-side invalid input, which never reaches the transport.
- Exhaustion is atomic: close the challenge, fail the owner, fence submissions, settle the
  barrier, then terminate the still-waiting stream through the release transition.
- A daemon-shutdown refusal makes the owner terminal, so its challenge closes rather than
  being retained.

Terminal relay failures run the existing ordered cleanup with relay mode set: every
trust-store operation is bypassed, while capture release and display removal
(`session/cleanup.py:482`, `:509`) precede clearing ownership, and the completion
transition (`:820`) supplies the idle postcondition — `intent_active=False`,
`transport=Idle`, `display=None_`, `stream=None`, `physical_source=None`,
`display_handle=None`, `ownership_uuid=None`, `intent_stage=idle`. Only after that
postcondition does the pane project `Available`. Relay failures use a dedicated handler
that schedules no reconnect; `pairing_expired`, `receiver_rejected`, `stale_target` and
`daemon_failed` are terminal for it.

### 5.11 Daemon refusals

The adapter classifies the daemon's fixed strings before any reduction; the transport
never parses text.

| Daemon `error` | Code | Effect |
| --- | --- | --- |
| `daemon is shutting down` | `daemon_failed` | attempt consumed; owner terminal; challenge closed; release |
| `no device is waiting for a credential` | `stale_target` | `Ended`; challenge closed |
| `multiple devices are waiting for credentials; specify a target` | `stale_target` | `Stale` if ever observed |
| `credential prompt is not ready for <target>` | `stale_target` | challenge closed |
| any other non-OK acknowledgement | `daemon_failed` | attempt consumed; retained only while the owner is live |

A malformed status reply is retried within the active phase budget; if it stays malformed,
the owner fails with `transport_failed` and the challenge closes.

### 5.12 Credential handling and metadata propagation (F18, R14)

`CommandSpec.argv` stays `tuple[str, ...]` and gains `secret_indices: tuple[int, ...] = ()`.

- `PublishedDoubletakeControl._run` accepts a `ProfileCommand` and constructs
  `CommandSpec(argv=..., env={}, secret_indices=...)`; an index outside `argv`, a duplicate
  index, or a non-empty set for any operation other than `submit_pairing` is refused before
  the spawn.
- `redacted_argv` renders those indices as `<redacted>`.
- `testing.adapters.redact` and retained `FakeProcess.runs` observations store a redacted
  projection; they serialize dataclass fields directly, so they must project rather than
  rely on a display property.
- Diagnostics, receipts, records and the PM ledger never carry argv.

Tests: a production-runner interception asserting delivery at the declared index while every
projection shows `<redacted>`; a canary asserting the value appears in no diagnostic,
record, ledger, journal or export.

### 5.13 Presentation (F27, F29, F9, G9)

Prompts are keyed by a **monotonic presentation revision**, incremented for every completed
submission result — including client-side invalid input, which consumes no attempt. The
presenter consumes each revision once and serializes prompt replacement, so repeated
snapshot delivery cannot duplicate a dialog and two consecutive invalid submissions each
reopen exactly one.

Projections:

| State | Actions |
| --- | --- |
| live relay challenge, waiting request unresolved and dispatch settled | `submit_pairing`, `cancel` |
| live relay challenge, `success_pending` or accepted progress | `cancel` only |
| fully idle `Available` (post-cleanup) | `start` |
| connecting / streaming | the existing active-state actions (`stop`, etc.) |
| cleanup pending | the existing cleanup actions |

No `pair` (it requires remembered trust) and no `retry` (refused while trust is `Pairing`)
while a relay challenge is live.

### 5.14 Dispatch classification (F33)

A credential command ends in exactly one of three states, and only the first two settle the
barrier:

| State | Evidence | Barrier |
| --- | --- | --- |
| `no_spawn` | the spawn was refused before any child existed, or the adapter refused pre-spawn | settled; attempt consumed |
| `acknowledged` | the child exited and its reply was decoded | settled |
| `uncertain` | the child was spawned but no decoded reply was obtained, including a runner `cleanup_timeout` with a live child | **unsettled** |

Uncertain dispatch retains the barrier and the lease. It may be settled only by one of:

1. a decoded reply arrives for that exact command; or
2. a status read proves the target's entry has moved past the credential wait for this
   generation **while that command is the only dispatch admitted for the lease** — which
   the eligibility predicate guarantees, because no second submission can be admitted while
   dispatch is `uncertain`; or
3. the exact original daemon instance has a termination proof (§5.8).

Neither the command task completing nor a snapshot showing the target absent is accepted
as settlement: the daemon executes a request independently of the client connection, so a
late `connect` can still create the entry after either observation. While dispatch is
uncertain, no disconnect is dispatched and the pane reports that the receiver's state is
uncertain rather than claiming success or failure.

## 6. Data flow

```
Operator presses Start on receiver T (mirror)
      │
      ▼
start_stream(T) ──► ack (hint) ──► status: T's own entry is pin_required
      │                               aggregate fields ignored
      ▼
credential phase: other clocks suspended; owner lease recorded; request minted
      │
      ▼
prompt opens, T selected ──► operator types PIN ──► SubmitPairing
      │
      ▼
fence ──► barrier set ──► connect T [--audio..] [--volume..] <PIN>
      │                        reply ──► status read ──► barrier clear
      │                        ├── entry waiting → same session resumed
      │                        └── entry vanished → T's connection starts with the PIN
      ▼
request gone ──► permission (remaining) ──► prepared source ──► handshake 20 s
      │
      ▼
start returns its handle ──► commit Trusted ──► close challenge ──► admit handle
```

## 7. Operator-visible errors

| Condition | What the operator sees |
| --- | --- |
| TV asks for a PIN | "Pairing with <TV>. Enter the PIN shown on that TV." |
| Wrong PIN, still waiting | Fresh prompt: "That PIN was not accepted." |
| Empty or malformed entry | Same prompt, inline message, no attempt consumed |
| Third consumed submission | "Pairing failed." Start returns after release |
| Cancel | "Pairing cancelled." That TV only |
| TV forgot us later | The same prompt on the next start |
| Keyring unavailable at startup | Credential-service diagnostic |
| Receiver replaced mid-attempt | "That receiver changed; start again." |
| Daemon shutting down | "The receiver service is stopping; start again." |
| Virtual display asks for a PIN | Existing refusal, unchanged in this delivery |

## 8. Security

The PIN crosses one process boundary in the argv of `doubletake-ctl`; a same-UID process
can read it during that call, consistent with the cooperative same-user boundary already
documented. Nothing is stored by the application. The credential is bound to a named
receiver twice: the application's fence re-checks that target's waiting entry, and the
daemon resolves `d.streams[target]` itself.

## 9. Compatibility, deferrals, and superseded contracts

- Installed four-token pair works unchanged. Profiles proving the four transaction
  capabilities keep the transactional path.
- **Deferred:** virtual-display pairing. Reason: a virtual start returns its handle on a
  `connecting` acknowledgement, before the receiver asks, so no waiting attempt exists to
  resume and relay would require a new readiness owner. Until that delivery, a virtual
  start that meets a credential request keeps the current refusal and names it. The
  deferral is recorded as a deferred open item and listed in the delivery summary, not
  asked as a question.
- **Superseded — SD-1 term (1)**, for mirror starts: a credential challenge is answerable
  by relay. The four transaction capabilities remain absent and unclaimed.
- **Amended — Amendment 1 §3**: `submit_pairing` in relay mode is the one named exception
  with replaced requirements: targeted receiver, proven relay capability, mandatory fence,
  zero trust-store writes, three-attempt bound, ownership barrier, provisional success, and
  the target-bound fall-through. All other pairing operations keep their delivered
  requirements.
- **Documentation delivered with the change**: `docs/backend-compatibility.md`,
  `README.md`, and `docs/operations.md` (argv residual), each updated for mirror-only relay
  and the virtual deferral. Tests cite these files.

## 10. Test strategy

1. Capability — relay true for both pinned tables; false for unknown/later help and when
   the `pin` row is absent.
2. Authorization — accepted with all four transaction flags false; refused when relay or
   physical source is unproven.
3. Fencing — omitted fence spawns nothing; entry vanished during the awaited check refuses.
4. Observation — B waiting does not fail A; aggregate prompt without a matching entry mints
   nothing; a kind change mints a new generation and counter.
5. Command — argv carries audio off and initial volume as specified; the secret index is
   computed for each variant; a non-waiting target is refused; the fall-through starts the
   same target only.
6. Barrier and adoption — absence during `submit_inflight` is not failure; the created entry
   is adopted; cancel and release join the barrier; an unsettled barrier retains.
7. Provisional success — both arrival orders commit once; `success_pending` then start
   timeout cleans up; a satisfied challenge never discards the owner result.
8. Release — one disconnect per attempt; idempotent second caller; settle-before-disconnect;
   all four evidences; replacement with and without proof; retired lease consumption.
9. Phases — suspension preserves the permission allowance; retained outcomes expire at the
   original deadline; stale callback ignored; prepared-source handshake start.
10. Exhaustion — three refusals, three spawn failures, and a mixed sequence each close the
    challenge with the third outcome's code and reach the idle postcondition.
11. Cleanup — capture and display release precede ownership clearing; Start dispatches only
    after the postcondition.
12. Presentation — two consecutive invalid submissions reopen twice; repeated snapshot
    delivery does not duplicate; live challenge shows Submit/Cancel; active stream keeps
    Stop; Start only when fully idle.
13. Credential metadata — credential delivered at the declared index; every projection,
    journal and retained observation redacted; canary absent everywhere.
14. Acceptance — one runtime scenario with the fake daemon: start → prompt → submit →
    streaming, no production spawn, and a virtual start still refusing unchanged.
15. Eligibility (F30) — submission-first ordering with source verification delayed: the
    prompt retires its input, no second dialog opens, and an attempted second submission is
    impossible; cancel still works.
16. Receipt vocabulary (F31) — a `stopped` receipt proves termination; a `failed` receipt
    with a completed cleanup intent proves it; a `failed` receipt without one does not.
17. Retained retry (F32) — a retained lease is retried on a later call, a concurrent caller
    joins, later termination evidence discharges directly, and no second disconnect is
    spawned while one is outstanding.
18. Uncertain dispatch (F33) — a spawned child with no reply keeps the barrier and the
    lease; a completed task and an absence snapshot each fail to settle it; a decoded reply,
    a progressed entry under exclusive dispatch, and a termination proof each settle it.
19. Exclusivity (F34) — the delayed-C1/C2 sequence: with C1 uncertain, no second submission
    is offered or admitted, and a progress observation is attributed to C1 alone; cancel
    during uncertainty retains rather than disconnecting; C1's later execution against a
    absent entry is therefore impossible because no disconnect was ever dispatched.
20. Release-vs-submit interleaving (F35) — a submission attempted after release sets
    `submit_fenced` but before its disconnect is refused at admission and again at the
    spawn fence; no credential child is spawned, and the disconnect's acknowledgement
    therefore covers the only entry that can exist.

Mutation probes:

| Mutation | Killed by |
| --- | --- |
| targetless `pin` | command test |
| drop audio/volume options | command test |
| derive the request from aggregate status | observation test |
| remove the relay branch from `allows` | authorization test |
| make the fence optional | fencing test |
| treat absence during the barrier as failure | barrier test |
| close the challenge on provisional success | provisional-success test |
| discard the owner result after success | provisional-success test |
| disconnect before settling | release test |
| spawn two disconnects | release test |
| accept a `failed-to-start` receipt as proof | termination-proof test |
| reuse the current-generation receipt lookup | termination-proof test |
| resume the permission clock on a retained outcome | phase test |
| rearm the challenge expiry on retry | phase test |
| key prompts on (generation, attempt, outcome) | presentation test |
| project `Retry` during a challenge | presentation test |
| clear ownership before capture/display release | cleanup test |
| journal raw argv | canary test |
| reopen the prompt while success is pending | eligibility test |
| accept `failed` without cleanup intent as termination | receipt-vocabulary test |
| refuse retry from `retained` | retained-retry test |
| settle uncertain dispatch on task completion | uncertain-dispatch test |
| settle uncertain dispatch on an absence snapshot | uncertain-dispatch test |
| admit a second submission while dispatch is uncertain | exclusivity test |
| attribute progress to a lease with more than one admitted dispatch | exclusivity test |
| omit `submit_fenced` from `can_submit` | release-vs-submit interleaving test |
| clear `submit_fenced` on a retained release | release-vs-submit interleaving test |

## 11. Risks

1. **Receiver lockout after repeated wrong PINs** — three attempts, no automatic retries.
2. **ARGV visibility** — §8; documented as part of delivery.
3. **Fall-through starts a connection** — accepted, target-bound, adopted by the same
   intent, and tested with the original audio and volume options preserved.
4. **Deferred virtual pairing** — a virtual start still refuses when a receiver asks; this
   is visible to the operator and stated in the delivery summary.

## 12. Amendment D1 — the dashboard holds no dispatch authority

### 12.1 Why the previous shape failed

Sections 5.4–5.7 gave the dashboard actor a share of dispatch authority: it kept its own
uncertainty barrier, its own monotonic latches over progress, and its own settlement
attribution, synchronised with the transport by message. Six review rounds of the actor found
only disagreements between those two copies — an older view applied late, a request kind
replaced underneath a pending view, a completion delivered before its settlement, a release
fence published with otherwise unchanged content. Ordering the messages (amendment A1) moved
the problem rather than solving it: ordering what has *arrived* does not make it *current*, and
a successful start handle carries no ordering at all.

The seam is wrong, not the code. Two controllers arbitrating one question by message cannot be
made correct by more message rules.

### 12.2 The rule

**The transport is the only authority on dispatch.** What is outstanding, whether input is
eligible, when a command is settled, and when a lease is released are all decided by the
transport and are never inferred, cached, latched or re-derived by the actor.

**The dashboard renders.** The actor projects the newest `RelayLeaseView` it has been given and
nothing else. It may *display* an offer of input; it never *authorises* one.

**Admission is decided at submission.** The dashboard may refuse early for its own reasons
(no live challenge, dialog not open, client-side input invalid), but its acceptance is never
authority to spawn. Every relay submission is admitted or refused by the transport at the
moment it is submitted, using its own current state. A submission the dashboard offered from a
stale view is refused there with `stale_target` (or `no_spawn` when nothing was reserved), the
prompt closes on the next view, and the pane shows "That receiver changed; start again."

**Consequence for staleness.** A stale view can only mislead the display for as long as the
transport takes to refuse. Targeting prevents the credential from reaching a *different*
receiver, and the transport revalidates ownership immediately before the spawn.

**What the guarantee does not cover (correction).** The receiver resolves a credential by
target address. When the named target's waiting entry has vanished it starts a new
connection to that same address with the credential, and it receives no owner lease,
request generation, receiver identity or challenge token. The protocol therefore cannot
distinguish the original device from a different device that has since taken the same
address. The honest statement is: *the credential is delivered to the named target under
an address-continuity assumption.* The application narrows the window (identity and
fingerprint revalidation immediately before the spawn) and detects replacement when the
receiver's reported identity changes, but it does not prove session continuity. This
residual is recorded here and in `docs/operations.md`; it is not claimed as a guarantee.

### 12.3 What the dashboard still owns

Only facts that belong to the receiver actor and are not dispatch state:

1. **Presentation revision** — monotonically increasing per receiver, keyed to
   `(challenge_id, presentation_revision)`, so a repeated snapshot cannot open a second dialog
   and two distinct invalid submissions each reopen once (§11 of the specification).
2. **Submission identity and dedup** — a fresh `submission_id` allocated before client input
   validation, registered pending, tombstoned on completion for the actor's lifetime, so a
   duplicate cannot apply twice and two identical invalid submissions remain distinguishable.
3. **Projection and notice** — rendering the view, the challenge, the pane actions and the
   typed notice, including the terminal notice that survives ordered cleanup.

These are actor-lifetime identity facts with no transport counterpart, so they cannot diverge
from a transport copy.

### 12.4 What this removes

Removed from the actor and from the contract: the uncertainty barrier, the per-field monotonic
latches, settlement fingerprinting and consumption, cross-generation attempt-counter
comparison, `view_seq`, and the ordered-view rule of amendment A1. None of them is replaced by
another actor-side mechanism.

Removed from the transport's public view: nothing. `RelayLeaseView` keeps its existing fields;
`command_id` and `unresolved_dispatch_ids` remain transport-private.

### 12.5 Transport obligations this makes load-bearing

Because the dashboard no longer pre-checks anything, the transport must be correct on its own,
and the following are now the *only* barriers:

1. **Reservation before spawn** — the fence, the final ownership revalidation and the
   reservation happen in one uninterrupted sequence immediately before the child starts, with
   no await and no scheduling gap between them (already required by §8).
2. **Exclusive dispatch** — `can_submit` on the transport's current state refuses a second
   command while one is unresolved, including after its caller returned.
3. **Release before retirement** — a release fences further submissions before it settles
   anything, and one disconnect at a time (already required by §9).
4. **Typed refusal** — every refusal is one of `no_spawn`, `acknowledged` or `uncertain`, with
   its fixed diagnostic, so the dashboard's closure is always explained.

### 12.6 Presentation under refusal

A refusal that closes the challenge publishes a view with the challenge cleared and the typed
notice. A refusal that keeps the challenge (`Waiting`, retained `Rejected`) publishes a view
that still names it. The dashboard does not decide which of those it is; it renders the view.
If the view is stale by the time it is rendered, the user may type into a field that will be
refused — that is the accepted cost, and the refusal is the explanation.

### 12.7 Tests this amendment requires

1. A submission offered from a stale view is refused by the transport and closes the prompt;
   no child is spawned, and the pane shows the stale notice.
2. Two identical invalid submissions produce two revisions and two dialogs (actor identity),
   while neither reaches the transport.
3. A duplicate completion applies once, with no revision and no dialog.
4. The transport refuses a second command while one is unresolved, across request replacement
   and after a caller timeout.
5. Release fences submissions before settling and spawns exactly one disconnect.
6. A successful handle commits trust exactly once, before the start effect's blocked-trust
   check, and a later matching completion does not clear or discard the admitted handle.

### 12.8 Corrigenda after review (D1-01 … D1-06)

**Relay outcome carried with the handle (D1-01).** Trust is committed on the exact handle the
start returns, and the decision must not depend on a challenge view that may be stale, cleared
or arrive later. The transport therefore publishes one owner-bound record with the handle:

```python
@dataclass(frozen=True, kw_only=True)
class RelayStartOutcome:
    lease: LeaseId
    daemon_instance_uuid: InstanceId
    endpoint_generation: int
    session_generation: int
    request_generation: int | None   # the challenge generation this start completed, if any
    succeeded: bool
```

`complete_relay_start` commits `trust=Trusted` exactly once when `succeeded` is true, clears
the provisional flags and admits the handle, before the start effect's blocked-trust check.
When `succeeded` is false, the ordinary start path applies unchanged. Both arrival orders are
covered: a handle first with a live challenge, and a provisional success first followed by the
handle. The record is owner-bound, so a handle for a different lease or daemon cannot commit.

**Bounded staleness (D1-03).** The actor owns a per-receiver publication order it assigns
itself: every closing, refusing or terminal view it publishes advances a local
`closing_epoch`. A later view that would reopen a prompt for a challenge the actor has already
closed at or below that epoch is dropped before any state change. This uses the actor's own
lifecycle, not dispatch state, so it is not a second authority — it only prevents the actor
from undoing its own published closures.

**Refusal taxonomy (D1-04).** Classification answers *what is known about the command*;
effect answers *what the dashboard does*. They are separate columns and both are fixed:

| Transport admission outcome | Typed result | Challenge effect | Attempt charged | Child may exist | Notice |
| --- | --- | --- | --- | --- | --- |
| unchanged owner and challenge still eligible | accepted | retained until completion | no | yes | none |
| named target's waiting entry vanished before reservation | `stale_target`, `no_spawn` | closed | no | no | receiver changed |
| another command unresolved for this lease | `stale_target`, `no_spawn` | retained | no | no | uncertainty |
| release fence active | `stale_target`, `no_spawn` | closed | no | no | receiver changed |
| client invalid input | `InvalidInput` | retained | no | no | inline validation |
| receiver service shutting down | `daemon_failed`, `no_spawn` | closed | yes | no | service stopping |
| decoded refusal | `receiver_rejected`, `acknowledged` | retained | yes | yes | PIN not accepted |
| no decoded reply after reservation | `cleanup_timeout`, `uncertain` | retained | yes | unknown | uncertainty |

A refusal closes only the challenge it names; an older challenge's refusal never closes a
replacement.

**Linearization point (D1-06).** The boundary is the *synchronous reservation* performed by the
transport immediately before runner entry — after the final ownership revalidation and with no
await or scheduling gap. Child creation inside the runner is asynchronous and is not evidence
that a start occurred; the reservation stays unresolved until a positive `no_spawn`, a decoded
reply plus the required status, exclusive progress, or exact-instance termination evidence.

**Sections that must be rewritten (D1-05).** §§5.4 (shared-predicate and `submit_fenced`
paragraphs), 5.5 (outcome-to-challenge table), 5.6 (actor routes and the late-completion fence),
5.7 (fence-as-admission), 5.10 (cross-component atomicity), 5.11 (daemon reply to challenge
effect) and 5.13 (projected action table as authority) are superseded by §12 and must be
rewritten to transport admission, actor presentation, and the §12.8 taxonomy. The specification
§§6, 13, 14 and 20 must be reconciled with the same rules, and the A1 `view_seq` requirements
removed.

**Plan re-derivation (D1-07).** The plan, execution graph, interface pins and lane projections
describe the pre-pivot contract and must be re-derived from the amended design and the revised
specification before any lane is dispatched. Reusable without change: the target-bound command
construction (T-002), the historical termination proof (T-003) and the pure relay records
(T-001), subject to whatever field changes the revised specification requires.
