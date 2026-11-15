# Dashboard device pairing specification — mirror mode

## 1. Authority, scope and fixed decisions

The authority is [2026-11-15-gui-device-pairing-design.md](2026-11-15-gui-device-pairing-design.md), read in its entirety, including F23–F35 and its mutation table. The operator has approved and closed that design. Its retained “under review” status text does not reopen it. This specification translates its decisions into Python interfaces and observable transitions. It authorizes no receiver change, installation or live receiver experiment.

The approved scope is quoted verbatim:

> “Enter the PIN in the app; the receiver tool keeps the credential; no app credential store; no forget action”
>
> “**Mirror only.** Virtual-display pairing is deferred; a virtual start that meets a credential request keeps today's refusal”

The approved deferral and reason are quoted verbatim:

> “**Deferred:** virtual-display pairing. Reason: a virtual start returns its handle on a `connecting` acknowledgement, before the receiver asks, so no waiting attempt exists to resume and relay would require a new readiness owner. Until that delivery, a virtual start that meets a credential request keeps the current refusal and names it. The deferral is recorded as a deferred open item and listed in the delivery summary, not asked as a question.”

| Fixed operator decision | Required behavior |
| --- | --- |
| Prompt timing | Open automatically when an operator-started receiver's owned mirror start observes its own credential request, regardless of which pane is selected. |
| Prompt authority | Only an operator-initiated mirror start, or the reconnect of that intent, owns a relay challenge. Discovery alone, another receiver's request and an idle Pair command confer no relay authority. |
| Target disappeared after validation | Accept the receiver's targeted `connect T credential` fall-through. Adopt the resulting entry into the same start intent, retaining the original audio and initial-volume options. |
| Credential custody | `doubletake` keeps pairing credentials in its keyring backend. The application keeps transient input only; it creates no application credential record or remembered-trust claim. |
| Success | Submission success is provisional. The start's admitted handle is the only trust-completion point. |
| Attempts and clocks | Three consumed submissions per request generation; a 120 s absolute credential lifetime; no automatic credential retry. |
| Compatibility | Preserve the transactional pairing path for profiles that prove its capabilities, including fence-less submission without relay callbacks. Relay submission requires its fence and both callbacks. The installed four-token pair needs no rebuild or new token. |
| Submission identity | The receiver actor allocates a fresh `submission_id` for every admitted submission, including client InvalidInput; consume each completion once for the actor's lifetime. |
| Credential representation | Apply the existing input syntax plus the selected CLI's representability check before dispatch. An option-shaped credential on the extended CLI is client InvalidInput and consumes no attempt. |
| Release acknowledgement | A successful, exact targeted disconnect acknowledgement proves release after settlement; its aggregate state may describe another receiver. |
| Evidence clocks | Copy cleanup completion from persisted wall-clock evidence. All transient observation/deadline timestamps remain monotonic; never compare across these domains. |

Non-goals are virtual-display relay, in-app forget/revoke, application-owned remembered trust, capability tokens, Go patches or rebuilds, wire changes, and replacement of transactional trust. The existing transactional operations remain available only through their existing proven profile. Relay does not enable their flags.

## 2. Reference baseline and source locators

Repository paths below refer to the checkout against which this specification was written. `R/` denotes the read-only receiver root `/tmp/pr76-drift.i1Kj73/doubletake-5e2f1cbe3e1070bcefec8e549c7baf884727bc00`. Source locators describe existing code, not proposed line numbers.

| Existing contract | File and line | Consequence for this change |
| --- | --- | --- |
| Feature record and exact tables | `src/airplay_linux/backend/contract.py:104`, `:60`, `:70`, `:846` | Add one independently derived feature; preserve complete-table recognition and the existing verification predicate. |
| Payloads and argv mapping | `src/airplay_linux/backend/control.py:99`, `:122`, `:410`, `:530` | Carry start options in the relay payload and return typed commands from both profile implementations. |
| Required fences and final dispatch check | `src/airplay_linux/backend/control.py:196`, `:878` | Require the relay submission fence by profile; keep the existing unconditional set and transactional fence-less behavior; preserve check-after-await ordering. |
| Runner boundary and error reduction | `src/airplay_linux/backend/control.py:722`, `:974`, `:1233` | Preserve dispatch evidence and classify fixed daemon errors before generic reduction. |
| Public authority | `src/airplay_linux/process/ownership.py:318` | Add exactly the relay authorization branch in §5. |
| Stream ownership and retirement | `src/airplay_linux/transport/doubletake.py:215`, `:461`, `:703`, `:1095` | Store relay state on the existing stream lease; retain obligations across daemon replacement. |
| Credential refusals during readiness | `src/airplay_linux/transport/doubletake.py:788`, `:917`, `:1042` (`_start` definition at `:998`) | Replace mirror refusal with target-specific observation. Virtual refusal remains. |
| Current independent disconnect | `src/airplay_linux/transport/doubletake.py:1106` | Route it and failed-start cleanup through the single release transition. |
| Transactional coordinator and input validation | `src/airplay_linux/trust/coordinator.py:283`, `:339`, `:354` | Preserve its fence-less submit call; reuse PIN/password syntax with relay's CLI representability check; relay bypasses its attempt accounting, timers and credential transactions. |
| Existing submit operation identity | `src/airplay_linux/session/pairing.py:229` | The challenge operation ID is reused, so it cannot deduplicate distinct relay submissions; allocate a separate actor-owned submission ID. |
| Challenge and result records | `src/airplay_linux/domain/operations.py:133`, `:156`, `:176` | Publish a relay view without manufacturing a backend token or credential reference. |
| Command and process ports | `src/airplay_linux/domain/ports.py:276`, `:301` | Carry redaction indices to the production runner without changing argv's type. |
| Actual process invocation | `src/airplay_linux/process/linux.py:250`, `:540` | The runner executes the original argv, never the redacted projection. |
| Receipt and cleanup vocabulary | `src/airplay_linux/domain/records.py:126`, `:146` | Completed teardown means `stopped`, not an invented `terminated` value. |
| Current-generation receipt lookup | `src/airplay_linux/process/daemon_registry.py:393` | Historical termination queries use a separate lookup. |
| Refused startup versus completed cleanup | `src/airplay_linux/process/daemon_registry.py:630`, `:743` | A failed receipt alone is not proof of termination. |
| Persisted cleanup clock | `src/airplay_linux/process/daemon_registry.py:376`, `:735`, `:759` | `_now()` uses `time.time()`; copy cleanup completion unchanged and compare only with persisted wall-clock intent time. |
| Process absence classification | `src/airplay_linux/process/procfs.py:187`, `:254` | Unreadable process evidence is not absence. |
| Existing start event bridge | `src/airplay_linux/session/virtual.py:465`, `:703` | Reuse the bridge for the pending mirror acquisition; do not weaken generic stream-event admission. |
| Reconnect handle admission | `src/airplay_linux/session/reconnect.py:375`, `:400`, `:413` | Commit relay trust before the generic blocked-trust branch; terminal relay failure schedules no reconnect. |
| Existing immediate Trusted reduction | `src/airplay_linux/session/pairing.py:405` | Preserve it for transactions; relay uses the provisional-success rule. |
| Ordered resource cleanup and idle commit | `src/airplay_linux/session/cleanup.py:482`, `:509`, `:820` | Capture/display release precedes ownership clearing. |
| Existing command admission | `src/airplay_linux/session/engine.py:522` | Add the shared relay eligibility check to SubmitPairing admission. |
| Existing pairing actions and start gate | `src/airplay_linux/ui/state.py:558`, `:859` | Replace relay Retry/Pair projection and require the full idle postcondition for Start. |
| Existing prompt identity | `src/airplay_linux/ui/presenter.py:1238`, `:1264` | Replace challenge-equality deduplication with presentation-revision consumption. |
| Direct dataclass projection and retained runs | `src/airplay_linux/testing/adapters.py:155`, `:1074`, `:1231` | Explicitly project secret-bearing argv before retaining it. |
| Targeted resume/fall-through | `R/internal/daemon/daemon.go:715`, `:742`, `:760` | Targeted credential connect can resume or recreate only T. |
| Targeted disconnect acknowledgement | `R/internal/daemon/daemon.go:1691` | A successful targeted detach returns `overallStateLocked()`; B can remain streaming after A is released. |
| Targetless pin | `R/cmd/doubletake-ctl/main.go:52` | Never emit this form. |
| Connecting precedes capture preparation | `R/internal/daemon/daemon.go:749`, `:1133` | `connecting` after a credential is not the handshake boundary. |
| Carried audio/volume ctl syntax | `vendor/doubletake/0004-target-volume.patch:153`, `:163`, `:178`, `:219` | The carried patch and recognized table establish option syntax; its parser rejects or interprets credentials beginning with `-` as options and supplies no end-of-options escape. |

## 3. Exact file and symbol changes

The signatures in this section are the implementation surface. Records referenced here are fully defined in §4. Existing unrelated branches retain their behavior. All new state is transient unless it is existing receipt/cleanup evidence.

### 3.1 Domain files

`src/airplay_linux/domain/relay.py` is new. It owns the secret-free relay records, enums, the single submission predicate and the CLI representability predicate. The latter examines transient text without retaining it. It imports no transport, Qt, process runner or storage implementation.

```python
def can_submit(lease: RelayLeaseView) -> bool: ...
def relay_credential_representable(
    value: str, *, target_volume_proven: bool,
) -> bool: ...
```

`src/airplay_linux/domain/operations.py` adds `ChallengeView.presentation_revision: int = 0` and `PairingResult.waiting: bool = False`. Their complete field signatures are:

```python
@dataclass(frozen=True, kw_only=True)
class ChallengeView:
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    endpoint_fingerprint: str
    endpoint_generation: int
    operation_id: OperationId
    session_generation: int
    trust_generation: int
    created_at: float
    expires_at: float
    credential_kind: Literal["pin", "password"]
    presentation_revision: int = 0

@dataclass(frozen=True, kw_only=True)
class PairingResult:
    kind: Literal[
        "Trusted", "InvalidInput", "ReceiverRejected", "Cancelled",
        "CredentialUnavailable", "TrustCleanupRequired", "TransportFailure",
    ]
    credential_ref: OpaqueCredentialRef | None
    diagnostic: Diagnostic | None
    waiting: bool = False
```

`PairingResult.kind` retains its existing Literal vocabulary; §11 defines its relay mappings. A relay challenge is a `ChallengeView` derived directly from `CredentialRequest`; it never constructs a `PairingChallenge` with a fabricated `backend_token`. `PairingChallenge` and its transactional token requirement remain unchanged. ChallengeView retains its generation validation and rejects a non-integer, bool or negative presentation_revision.

`src/airplay_linux/domain/records.py` adds `SessionSnapshot.relay: RelayLeaseView | None = None` and `SessionSnapshot.relay_notice: RelayNotice | None = None`. The snapshot is a projection; it owns no task, lock, credential or command argv. The terminal notice survives ordered cleanup until the next explicit Start or operator action clears it; it is transient and omitted from persistent session codecs.

`src/airplay_linux/domain/messages.py` adds `RelayPairingPayload` and `RelaySubmitCompletion`, defined in §4, to the applicable payload/result vocabulary. `TransportEventKind.pairing_required` carries `RelayPairingPayload` for relay updates; it is not a new backend wire event. Existing `ChallengePayload` remains transactional.

`src/airplay_linux/domain/ports.py` changes `CommandSpec` as follows. Its constructor retains existing keyword-only fields and defaults.

```python
@dataclass(frozen=True, kw_only=True, repr=False)
class CommandSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None = None
    pass_fds: tuple[int, ...] = ()
    secret_indices: tuple[int, ...] = ()

    @property
    def redacted_argv(self) -> tuple[str, ...]: ...
    def __repr__(self) -> str: ...
    def __post_init__(self) -> None: ...
```

`__post_init__` keeps the immutable env mapping and validates indices as specified in §15. ProcessPort is unchanged:

```python
async def run(self, spec: CommandSpec, timeout: float,
              stdin: bytes | None = None) -> Result[CommandOutput]: ...
async def spawn(self, spec: CommandSpec) -> Result[ProcessIdentity]: ...
```

No persistent codec admits `CredentialRequest`, `RelayLeaseView`, `RelaySubmitCompletion` or `TerminationProof`. Existing session codecs retain their explicit persistent field allowlist and omit `SessionSnapshot.relay` and live challenges.

### 3.2 Backend files

`src/airplay_linux/backend/contract.py` adds `BackendFeatures.credential_relay: bool = False`, covered by the existing exact-bool validation, and the sanitized `CredentialDispatchFailure` record in §4. It adds this derivation helper and calls it from `features_from_help`:

```python
def _credential_relay_proven(
    *, verified: bool, signatures: dict[str, str] | None,
) -> bool: ...

def features_from_help(
    sender_help: str, ctl_help: str, *,
    advertised: frozenset[str] = frozenset(),
) -> BackendFeatures: ...
```

`src/airplay_linux/backend/control.py` adds `ProfileCommand` and extends `CredentialPayload` as defined in §§4–5. Both profile implementations return the typed command. The public convenience `argv` method also returns the typed command so that callers cannot accidentally strip metadata.

```python
class PublishedDoubletakeProfile:
    def try_argv(self, operation: BackendOperation | str,
                 target: ReceiverTarget | None,
                 payload: BackendPayload) -> Result[ProfileCommand]: ...
    def argv(self, operation: BackendOperation | str,
             target: ReceiverTarget | None,
             payload: BackendPayload) -> ProfileCommand: ...

class ApplicationContractProfile(PublishedDoubletakeProfile):
    def try_argv(self, operation: BackendOperation | str,
                 target: ReceiverTarget | None,
                 payload: BackendPayload) -> Result[ProfileCommand]: ...

class PublishedDoubletakeControl:
    async def _run(self, command: ProfileCommand, *,
                   operation: BackendOperation) -> Result[CommandOutput]: ...
    async def request(
        self, operation: BackendOperation | str,
        target: ReceiverTarget | None, payload: BackendPayload,
        context: OperationContext | None, *,
        dispatch_fence: DispatchFence | None = None,
        ownership_proof: OwnershipProof | None = None,
        dispatch_started: DispatchStarted | None = None,
        dispatch_status: DispatchStatus | None = None,
    ) -> Result[BackendReply]: ...

def _credential_rejection(
    error: object, target: ReceiverTarget,
) -> CredentialDispatchFailure: ...
def requires_dispatch_fence(
    operation: BackendOperation, *, relay_submission: bool = False,
) -> bool: ...
```

`DispatchStarted = Callable[[], Result[None]]` is a synchronous, transport-owned reservation callback. `DispatchStatus = Callable[[StatusReply], Result[None]]` updates the exact candidate request from the adapter's final awaited status read. Add the optional `dispatch_started` and `dispatch_status` keywords to `BackendControl.request` and `UnsupportedControl.request` with the same types as above (the protocol keeps `operation: BackendOperation`). Relay submission requires the fence and both callbacks; transactional submission keeps all three optional and supplies none in its existing coordinator path. The release-disconnect callback is the separate §9 case; other operations leave the new callbacks unset. These callbacks separate the repeatedly callable, read-only dispatch fence from the status observation and one reservation immediately before runner entry. They add no backend operation or wire field. For relay submission the adapter calls `dispatch_started` exactly once after its final successful fence check, with no intervening await; an Err prevents runner entry. An authority may call the read-only fence more than once during authorization without reserving a dispatch.

Update every internal `try_argv` consumer, including `_revalidate_ownership` and `HandshakeAuthority.prove` (`backend/control.py:814`, `:1114`), to preserve the command object through `_run`; every call supplies its actual operation. `ApplicationContractProfile` preserves its fixture-only executable and payload channel; its commands have empty secret indices. Transactional credentials remain outside its argv.

### 3.3 Process and transport files

`src/airplay_linux/process/ownership.py` changes the named authorization branch and uses §5.2's profile-dependent required-fence check in `authorize`; other ownership behavior is unchanged:

```python
class EvidenceAuthority:
    def allows(self, operation: BackendOperation) -> bool: ...
    async def authorize(
        self, record: DaemonRecord, operation: BackendOperation,
        target: ReceiverTarget | None, fence: DispatchFence | None,
    ) -> Result[OwnershipProof]: ...
```

`src/airplay_linux/process/daemon_registry.py` adds the historical query; it observes evidence and never launches, stops, signals, reconciles or rewrites an instance:

```python
class DaemonRegistry:
    async def termination_proof(
        self, record: DaemonRecord,
    ) -> Result[TerminationProof]: ...
```

`src/airplay_linux/process/linux.py` keeps `LinuxProcessRunner.run` and `_exec` signatures and executes `spec.argv`. An Err with `dependency_missing` returned by this runner's pre-exec failure path certifies that no child was created. No other Err, cancelled waiter or exception is a no-spawn certificate. For relay, `_run` passes the existing timeout to the runner and does not wrap it in a second cancelling `wait_for`. The transport retains and shields the control-request task through its own bounded waiter; §8 defines the later completion observer. No new ProcessPort method is required.

`src/airplay_linux/transport/relay.py` is new. It owns deadline arithmetic and the mutable per-lease relay state described in §4. It does not own trust storage or release a transport independently.

```python
class RelayPhaseController:
    def __init__(self, *, clock: Clock, scheduler: Scheduler,
                 expire: Callable[[int, ErrorCode], None]) -> None: ...
    def connect_acknowledged(self, observed_at: float) -> None: ...
    def request_live(self, request: CredentialRequest) -> None: ...
    def observe_progress(self, *, generation: int, observed_at: float,
                         waiting: bool, prepared: bool, streaming: bool) -> None: ...
    def status_decoded(self, valid: bool) -> None: ...
    def deadline(self) -> float | None: ...
    def check(self, now: float) -> ErrorCode | None: ...
    def _expire(self, generation: int, deadline: float) -> None: ...
    def close(self) -> None: ...
```

`src/airplay_linux/transport/doubletake.py` extends `_Stream` with `relay: RelayLease | None = None`; it retains its existing target, context, source, handle and lease. The source annotation admits its existing desktop-capture value `None`. These are the exact changed/new relay entry points:

```python
class DoubleTakeTransport:
    def _relay_view(self, stream: _Stream) -> RelayLeaseView: ...
    def _observe_credential_request(
        self, stream: _Stream, status: StatusReply, observed_at: float,
    ) -> CredentialRequest | None: ...
    def _relay_submit_fence(
        self, stream: _Stream, generation: int,
    ) -> DispatchFence: ...
    def _reserve_relay_dispatch(
        self, stream: _Stream, candidate: CredentialDispatch,
    ) -> Result[None]: ...
    def _relay_dispatch_status(
        self, stream: _Stream, generation: int, status: StatusReply,
    ) -> Result[None]: ...
    async def _request(
        self, daemon: _Daemon, operation: BackendOperation,
        target: ReceiverTarget, payload: BackendPayload,
        context: OperationContext, *, cleanup: bool = False,
        dispatch_started: DispatchStarted | None = None,
        dispatch_status: DispatchStatus | None = None,
    ) -> Result[BackendReply]: ...
    async def _submit_relay(
        self, stream: _Stream, challenge_id: ChallengeId,
        value: SensitiveText, context: OperationContext,
    ) -> PairingResult: ...
    async def release(
        self, lease: LeaseId, evidence: ReleaseEvidence,
    ) -> Result[None]: ...
    async def _release_attempt(
        self, stream: _Stream, evidence: ReleaseEvidence,
    ) -> Result[None]: ...
    def _reserve_release_dispatch(
        self, stream: _Stream, attempt: int,
    ) -> Result[None]: ...
    async def _await_connecting_stream(self, stream: _Stream) -> Result[None]: ...
    async def _verify_reported_source(self, stream: _Stream) -> Result[None]: ...
    async def _claim_streaming_readiness(self, stream: _Stream) -> Err | None: ...
    async def _poll_reported_source(
        self, stream: _Stream, deadline: float,
    ) -> Result[None]: ...
    async def _start(
        self, stream: _Stream, connected: asyncio.Future,
    ) -> Result[StreamHandle]: ...
    async def _stop_owned(
        self, stream: _Stream, context: OperationContext,
    ) -> Result[None]: ...
    async def begin_pairing(
        self, target: ReceiverTarget, context: OperationContext,
    ) -> Result[PairingChallenge]: ...
    async def submit_pairing(
        self, challenge_id: ChallengeId, value: SensitiveText,
        context: OperationContext,
    ) -> PairingResult: ...
    async def cancel_pairing(
        self, receiver_id: ReceiverId, challenge_id: ChallengeId | None,
        context: OperationContext,
    ) -> Result[None]: ...
    async def stop_stream(
        self, handle: StreamHandle, context: OperationContext,
    ) -> Result[None]: ...
    async def _shutdown(self) -> Result[None]: ...
```

The private relay submit and public submit methods return `PairingResult`; only the actor constructs `RelaySubmitCompletion`, adding its allocated `submission_id` and the identity captured for that invocation (§12). It does not reconstruct identity from whichever challenge happens to be current at completion. `_request` forwards the original fence and both dispatch callbacks unchanged for relay. A control lacking either new keyword is refused before invocation on that route. `_reserve_relay_dispatch` first refuses a `pre_spawn_retired` candidate, then calls the same `can_submit` on the current view, then sets the barrier/dispatch fields synchronously; it returns `stale_target` without mutation if ineligible. Repeated read-only fence checks cannot reserve twice. `begin_pairing` remains the transactional operation; relay request observation directly publishes its public challenge. `cancel_pairing` selects relay release when the named challenge belongs to a relay lease, otherwise the existing coordinator.

The `_start` readiness loop does not hold `stream.lock` while waiting for a credential or user permission. Keep the existing lock through the initial connect acquisition only, and release it before entering the human/readiness phases. Release joins that original acquisition under its same settlement deadline before it considers disconnect; the initial connect cannot create an entry after a release acknowledgement. Short mutations of relay state are synchronous. Release has its own shared attempt task; neither submission nor readiness holds a mutex needed by release while awaiting it. Failed start, Stop, cancellation, abandonment and shutdown call `release`; no relay branch in these callers dispatches disconnect directly. `_release_attempt` is an internal continuation of this same transition; it is never an independently callable cleanup route.

### 3.4 Session, UI and testing files

`src/airplay_linux/session/engine.py` retains the exact admission signature and adds the §6 call for relay SubmitPairing:

```python
def _admit(self, context: SessionContext,
           message: Command | ResourceEvent,
           route: Route) -> tuple[Route, Command | ResourceEvent] | None: ...
```

`src/airplay_linux/session/pairing.py` adds the relay reduction and completion functions; existing pairing methods select the relay branch before their transactional branch:

```python
class PairingFlow:
    def observe_relay(self, ctx: SessionContext,
                      event: TransportEvent) -> bool: ...
    def complete_relay_start(self, ctx: SessionContext,
                             handle: StreamHandle) -> bool: ...
    def fail_relay(self, ctx: SessionContext,
                   error: Diagnostic) -> None: ...
    def _submit(self, ctx: SessionContext, message: SubmitPairing) -> None: ...
    async def _submit_result(
        self, message: SubmitPairing, context: OperationContext, *,
        relay: RelayLeaseView | None = None,
        submission_id: OperationId | None = None,
    ) -> Result[object]: ...
    def _submit_effect(self, ctx: SessionContext,
                       message: EffectEvent) -> bool: ...
    def discovery_changed(self, ctx: SessionContext) -> bool: ...
    def expire(self, ctx: SessionContext) -> bool: ...
    def mark_invalid_from_backend(self, ctx: SessionContext) -> bool: ...
```

`src/airplay_linux/session/mirror.py` changes `MirrorLifecycle._effect(self, ctx: SessionContext, message: EffectEvent) -> bool` and `_transport_event(self, ctx: SessionContext, event: TransportEvent) -> bool` to select these relay reductions. `src/airplay_linux/session/reconnect.py` changes the corresponding `ReconnectLifecycle` methods to those same fully annotated signatures and adds `_connection_failed(self, ctx: SessionContext, error: Diagnostic) -> None` with the dedicated terminal relay branch before existing reconnect classification.

`src/airplay_linux/session/virtual.py` retains its bridge and routes a mirror `RelayPairingPayload` through `PairingFlow.observe_relay` in `_effect(self, ctx: SessionContext, message: EffectEvent) -> bool`. Its existing pre-ready validation remains mandatory. The bridge's explicit interface is:

```python
async def _with_backend_events(
    self, ctx: SessionContext, operation: OperationContext,
    work: Callable[[], Awaitable[Result[Any]]], *, acquisition: bool = False,
) -> Result[Any]: ...
```

This change to the shared bridge does not admit virtual relay. It delivers secret-free request, eligibility and retirement updates for the same pending mirror start. Raw stream-less events still fail generic `accept_transport`.

`src/airplay_linux/session/cleanup.py` adds a relay branch to `OrderedCleanup._stop(self, ctx: SessionContext, queued: StartRequest | None) -> None` and `_continue(self, ctx: SessionContext) -> None`: route trust cancellation to release without revocation and retain the existing ordered resource stages. `src/airplay_linux/session/phase.py` keeps `derive_phase(state: SessionSnapshot) -> SessionPhase`; relay Pairing is visible until completion, while cleanup remains higher priority.

`src/airplay_linux/ui/state.py` keeps `project(snapshot: SessionSnapshot, inputs: ViewInputs) -> PaneState`, `_decide(state: SessionSnapshot, inputs: ViewInputs, ownership: OwnershipRecord | None, orphans: tuple[RecoveryItem, ...]) -> _Decision` and `_can_start(state: SessionSnapshot, inputs: ViewInputs) -> bool`, and adds `_relay_notice(state: SessionSnapshot, title: str) -> str | None`. They apply §14 and import the one `can_submit`. `_relay_notice` selects only the fixed typed notice table in §14. No Qt code inspects transport internals.

`src/airplay_linux/ui/presenter.py` retains these interfaces but changes prompt identity, relay prompt discovery and serialization as specified in §14. `_render_now` evaluates each owned relay receiver's own pane for prompts, including unselected receivers; the selected pane still controls dashboard rendering and the existing transactional prompt rule.

```python
class Presenter:
    def _render_now(self) -> None: ...
    def _open_pairing(self, receiver_id: ReceiverId,
                      challenge: ChallengeView,
                      pane: PaneState | None = None) -> None: ...
    def _maybe_open_pairing(self, receiver_id: ReceiverId,
                            pane: PaneState) -> None: ...
    async def _pairing_prompt(
        self, receiver_id: ReceiverId, challenge: ChallengeView, *,
        actions: frozenset[Action], retained_output: str | None,
    ) -> None: ...
```

`src/airplay_linux/ui/dialogs.py` keeps the existing `PairingDialog` secret-wiping exits and action-based construction. The relay projection controls its submit/cancel availability; no new credential store or dialog-owned retry counter is added.

`src/airplay_linux/testing/adapters.py` changes `redact(value: Any) -> Any`, `FakeProcess.run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None) -> Result[CommandOutput]`, and `ProbeProcessRecorder.observe_invocation(self, method: str, spec: CommandSpec) -> None` to use §15's projection before retention. `Journal.record(self, name: str, **arguments: Any) -> None` continues to call `redact`. All other recorder subclasses retain projected commands too.

`src/airplay_linux/diagnostics/catalog.py` and `ErrorCode` retain their existing vocabulary and signatures. Fixed operator notices in §14 are UI copy selected from `RelayNotice`, while diagnostics retain the exact codes in §11; no free-form daemon error is a catalog message. `runtime.py:196` continues to derive composition features with `replace(probed, ...)`, preserving the proved `credential_relay` bit. No capability report promotes `remembered_pairing` because relay is available. `trust/coordinator.py`, persistent storage formats, Go source and vendored patches require no behavioral change.

## 4. Complete transient data contracts

The following declarations define field names, field types and defaults. UUID aliases are the existing domain types; `submission_id` is a fresh `OperationId`, separate from the challenge/start operation and transport command ID. `generation` and `presentation_revision` are exact nonnegative integers; a newly live request uses a positive generation. All transient times, including `observed_at`, `admitted_at`, `identity_verified_at`, challenge creation/expiry and release observations/deadlines, are monotonic seconds from the injected `Clock`. The sole copied evidence exception is `TerminationProof.cleanup_completed_at`: it is exactly the persisted `DaemonCleanupIntent.completed_at` wall-clock value, or None, never a converted or newly sampled time. Persisted `intent_at` is in that same wall-clock domain. Never compare or subtract either persisted value against a monotonic timestamp; §10's completion-order comparison stays within wall time. Records contain no credential value.

```python
CredentialKind = Literal["pin", "password"]
RequestState = Literal["live", "succeeded", "Ended", "Expired", "Stale", "cancelled"]
DispatchState = Literal["none", "no_spawn", "acknowledged", "uncertain", "settled"]
ReleaseState = Literal["none", "settling", "released", "retained"]
RelayPhase = Literal["none", "permission", "credential", "handshake", "source", "closed"]
RelayNotice = Literal[
    "credential_requested", "pin_rejected", "pairing_failed", "pairing_cancelled",
    "receiver_changed", "daemon_stopping", "uncertain", "waiting_start",
]

@dataclass(frozen=True, kw_only=True)
class RelayStartOptions:
    target_volume_proven: bool
    audio: AudioPolicy | None
    initial_volume: VolumeLevel | None

@dataclass(frozen=True, kw_only=True)
class CredentialRequest:
    receiver_id: ReceiverId
    target: ReceiverTarget
    credential_kind: CredentialKind
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    generation: int
    observed_at: float
    start_options: RelayStartOptions

@dataclass(frozen=True, kw_only=True)
class SuccessPending:
    generation: int
    attempt: int
    observed_at: float

@dataclass(frozen=True, kw_only=True)
class RelayLeaseView:
    receiver_id: ReceiverId
    target: ReceiverTarget
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    owner_live: bool
    generation: int
    request: CredentialRequest | None
    request_state: RequestState
    request_waiting: bool
    challenge: ChallengeView | None
    submit_inflight: int | None
    submit_fenced: bool
    dispatch_state: DispatchState
    success_pending: SuccessPending | None
    prepared_source: bool
    streaming: bool
    consumed_submissions: int
    release_state: ReleaseState
    phase: RelayPhase
    expires_at: float | None
    notice: RelayNotice | None

@dataclass(frozen=True, kw_only=True)
class RelayPairingPayload:
    lease: RelayLeaseView

@dataclass(frozen=True, kw_only=True)
class RelaySubmitCompletion:
    submission_id: OperationId
    challenge_id: ChallengeId
    receiver_id: ReceiverId
    endpoint_generation: int
    session_generation: int
    owner_lease: LeaseId
    daemon_instance_uuid: InstanceId
    generation: int
    result: PairingResult

@dataclass(kw_only=True, repr=False)
class CredentialDispatch:
    command_id: OperationId
    generation: int
    attempt: int
    admitted_at: float
    state: DispatchState
    request_task: asyncio.Task[Result[BackendReply]] | None
    reply_decoded: bool
    status_resolved: bool
    charged: bool
    spawn_admitted: bool = False
    pre_spawn_retired: bool = False

@dataclass(frozen=True, kw_only=True)
class CredentialDispatchFailure(Err):
    # error: Diagnostic is inherited from Err.
    dispatch_state: Literal["no_spawn", "acknowledged", "uncertain"]
    refusal: Literal["shutdown", "ended", "stale", "rejected"] | None = None

@dataclass(frozen=True, kw_only=True)
class TerminationProof:
    daemon_instance_uuid: InstanceId
    daemon: ProcessIdentity
    runtime_directory: str
    launch_uuid: UUID
    receipt_state: Literal["stopped", "failed"]
    cleanup_outcome: Literal["stopped"] | None
    cleanup_completed_at: float | None
    process_evidence: Literal["pid_absent", "pidfd_exited"]
    identity_verified_at: float
    observed_at: float

@dataclass(frozen=True, kw_only=True)
class ReleaseEvidence:
    reason: ErrorCode | None
    observed_at: float
    disconnect_ack: AckReply | None = None
    status: StatusReply | None = None
    daemon_instance_uuid: InstanceId | None = None
    endpoint_generation: int | None = None
    termination: TerminationProof | None = None
```

`RelayLease` is the mutable transport-only record. Its complete field inventory is the fields of `RelayLeaseView`, plus `dispatch: CredentialDispatch | None`, `unresolved_dispatch_ids: set[OperationId]`, `phase_controller: RelayPhaseController`, `release_task: asyncio.Task[Result[None]] | None`, `release_attempt: int`, `disconnect_task: asyncio.Task[Result[BackendReply]] | None`, and `disconnect_outstanding: bool`. Initial values are: `owner_live=True`, `generation=0`, `request=None`, `request_state="Stale"`, `request_waiting=False`, `challenge=None`, `submit_inflight=None`, `submit_fenced=False`, `dispatch_state="none"`, `success_pending=None`, `prepared_source=False`, `streaming=False`, `consumed_submissions=0`, `release_state="none"`, `phase="none"`, `expires_at=None`, `notice=None`, all task/dispatch references `None`, `unresolved_dispatch_ids=set()`, `release_attempt=0`, `disconnect_outstanding=False`. Identity fields come from the existing start's stream, context, lease and admitted daemon. The set records admitted, unresolved command IDs for §8's exclusive-progress guard; it is independent of the single retained dispatch reference and never appears in public/persistent views. Presentation revision and submission-completion deduplication belong only to the receiver actor (§§12, 14), not to this mutable transport record.

A view is a frozen copy of these fields, never a second authority. `_relay_view` runs after checking the phase controller's current time; it retires an elapsed request before producing eligibility. `owner_live` means the original mirror intent is still active, has not failed/cancelled, and matches the full owner tuple. `request_waiting=True` requires the latest valid target observation to be `pin_required` with this request's kind. Progress sets it false; terminal retirement also sets it false. A live challenge can remain after this waiting predicate becomes false, with input retired. Terminal rejected/start-failure closures use request_state `Ended` and retain their exact diagnostic code; cancellation, expiry, stale identity and successful completion use their specifically named states.

| Record/state | Lifetime and retirement |
| --- | --- |
| `RelayStartOptions` | Snapshot once from the admitted original connect. `audio=None` and `initial_volume=None` when target-volume options were not proved. Never reread changed GUI preferences for a submission. Retire with the stream obligation. |
| `CredentialRequest` | Mint only from this target's matching `streams[]` entry. Same-kind repeated observations preserve the object, generation, original `observed_at`, counter and absolute expiry. Retire on success commit, terminal failure, cancel, expiry, owner/endpoint/daemon loss, or kind replacement. |
| Challenge | `created_at=request.observed_at`, `expires_at=created_at+120.0`, `credential_kind=request.credential_kind`; identity fields come from the owner; `challenge_id` is freshly allocated. `trust_generation` is the actor's current fence value. No backend token is minted. |
| Kind replacement | Mark the old request `Stale`; wipe/close its dialog; increment generation; allocate a new challenge with a fresh counter and expiry for the new kind while the same owner remains live. Outstanding command evidence remains on the lease, so the new request is ineligible until it settles. |
| Dispatch | Allocate a secret-free candidate before awaited preflight; admit it to the lease only at the final reservation. Retain its request task through all late replies. After settlement retain only sanitized identity/state needed for idempotence. Drop completed task and all raw input references after reduction. A returned client task does not retire a spawned dispatch. |
| `success_pending` | Set once for matching generation/attempt on streaming. Keep the challenge, retire input. Clear on the start's trust commit or any terminal owner failure. |
| Released stream | Clear request/input, cancel phase callback, mark release state `released`, remove from `_streams`/`_retired`, and retain the existing released-handle idempotence entry. |
| Retained stream | Keep the exact original daemon identity, lease, dispatch and release obligation in `_streams` or `_retired`. Never clear ownership because a new daemon is running. |
| Presentation revision | Monotonic for the receiver actor's lifetime; not persisted as remembered trust. Increment for each new challenge and each completed non-provisional submission result, including repeated identical InvalidInput results. |
| Submission identity/deduplication | Allocate one fresh `submission_id` on actor admission before input validation. Preserve it in every completion, including local InvalidInput. Pending identities and completed-ID tombstones are secret-free; retain tombstones for the receiver actor's whole lifetime, across challenge/lease replacement and cleanup. Never reuse or evict an ID while that actor can receive a duplicate. Dispose them with the actor after publication closes; never persist them. |
| `TerminationProof` | Transient query result for one original record. Consume through release for that exact instance; never serialize it or use it to control a replacement. |

## 5. Capability, authorization and command construction

### 5.1 Exactly one relay derivation predicate

Reuse `verified`, `_PINNED_050_CTL_COMMANDS`, `_VOLUME_CTL_COMMANDS`, and the decoded `signatures` from `backend/contract.py:846`. Do not infer this feature from version text, a `-pair` flag, a prose mention, `keyring`, or the volume token alone.

```python
def _credential_relay_proven(*, verified: bool,
                             signatures: dict[str, str] | None) -> bool:
    return verified and (
        signatures == _PINNED_050_CTL_COMMANDS
        or signatures == _VOLUME_CTL_COMMANDS
    )
```

The recognized legacy and extended tables both contain the `pin` row, even though relay never invokes it. A missing row, additional row, changed argument signature, unverified sender, inconsistent token/table pair or failed existing source verification makes `credential_relay=False`. Preserve the current target-scope and source-token verification that feeds `verified`. This adds no requirement for a new token. Default/unknown profiles have `credential_relay=False`. Fixture transactional profiles do not inherit relay by setting four unrelated flags.

### 5.2 Authorization and profile-dependent fence requirements

`EvidenceAuthority.allows` contains exactly this new branch, retaining every existing branch:

```python
if operation is BackendOperation.submit_pairing:
    return (f.credential_relay and f.owned_daemon_control and f.multi_target
            and f.physical_source)
```

The transaction flags `target_bound_pairing`, `provisional_commit`, `cancel_pairing` and `revoke_pairing` remain false for public relay. The independently proved `keyring` witness remains an existing daemon-start prerequisite; it is not an invented pairing-transaction capability. `physical_source or virtual_source` is not a permitted replacement for this branch.

The unconditional fence-required set retains its delivered members. Relay submission adds a profile-dependent requirement through the named helper:

```python
FENCE_REQUIRED_OPERATIONS = frozenset({
    BackendOperation.connect, BackendOperation.disconnect,
    BackendOperation.mute, BackendOperation.unmute,
    BackendOperation.volume, BackendOperation.audio_off,
    BackendOperation.start_audio, BackendOperation.restart_audio,
})

def requires_dispatch_fence(
    operation: BackendOperation, *, relay_submission: bool = False,
) -> bool:
    return operation in FENCE_REQUIRED_OPERATIONS or (
        operation is BackendOperation.submit_pairing and relay_submission
    )
```

`relay_submission` is derived internally from the actual operation and the recognized public profile (`doubletake-0.5` or `doubletake-0.5+source`) with proved `credential_relay`; it is not a caller-provided bypass or inferred from the payload/fence presence. `PublishedDoubletakeControl.request` passes this value to the helper. `EvidenceAuthority.authorize` uses the same public-profile classification for its required-fence check. `HandshakeAuthority.authorize` retains the helper's default False on the transactional path. All supplied fences still receive the existing final post-await evaluation.

| Submission profile | Required fence/callbacks | Preserved admission |
| --- | --- | --- |
| Proved public relay | `dispatch_fence`, `dispatch_started`, `dispatch_status` all required. | §5 relay authority and final status/fence/reservation sequence. |
| Existing transactional/application-contract profile | No new fence or callback requirement; existing fence-less coordinator submission remains admitted. | Existing own-feature requirements, owner handshake, target/token/payload path and supplied-fence checks remain unchanged. |

For relay `submit_pairing`, `dispatch_fence=None` returns `stale_target` before any I/O. Missing either dispatch callback also returns a sanitized no_spawn `stale_target` before I/O. A control wrapper that drops the keyword is caught here. A transport wrapper that cannot accept the keywords is refused before invocation. Existing missing-fence codes for other operations are unchanged. The unchanged `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags` is a mandatory compatibility gate: its fence-less submit with just its own required flags succeeds, and disabling each required flag still refuses without I/O. Do not retrofit relay callbacks/fences into that test or the transactional coordinator to make it pass.

For a relay request, the adapter performs this exact sequence: validate command/callbacks; await ownership authorization; perform one owned status read against the same adapter/DaemonRecord, including that read's ownership confirmation; pass the decoded result to `dispatch_status`; revalidate the mutation's ownership proof; call the read-only fence; call `dispatch_started`; invoke `_run`. The status read uses `request(status, None, EmptyPayload(), None)` without dispatch callbacks, so it cannot recurse into this relay branch. Preflight malformed status is retried under the same active phase budget as ordinary malformed polling; it neither creates nor consumes a submission. A failed identity check or a valid status proving no matching wait refuses no_spawn before a credential child exists. `_relay_dispatch_status` accepts only the original generation and one matching same-kind waiting entry, updates `request_waiting`, and otherwise returns `stale_target` without manufacturing a request for a different owner. Status observations from the ordinary poll can still mint a genuine changed-kind generation. The final fence rechecks the resulting current view after every await. No await separates its successful evaluation, the reservation callback and runner entry.

### 5.3 Typed command and start options

```python
@dataclass(frozen=True, repr=False)
class ProfileCommand:
    argv: tuple[str, ...]
    secret_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None: ...
    @property
    def redacted_argv(self) -> tuple[str, ...]: ...
    def __repr__(self) -> str: ...

@dataclass(frozen=True, kw_only=True, repr=False)
class CredentialPayload:
    value: SensitiveText | str
    audio: AudioPolicy | None = None
    initial_volume: VolumeLevel | None = None

    def __repr__(self) -> str: ...
```

The public relay mapping accepts `CredentialPayload` only for `submit_pairing`, with a nonempty named target, proved `credential_relay` and the original `RelayStartOptions`. Before creating a dispatch candidate or performing ownership/status I/O, actor input validation applies `relay_credential_representable` (§11.2); the transport's direct-entry validation and profile command construction repeat the same predicate defensively. In a proved extended-table profile, `target_volume_proven=True` identifies the parser even when this start uses no audio/volume tokens: any credential beginning with ASCII `-` is unrepresentable. Refuse it as client InvalidInput/invalid_input, with no transport dispatch, attempt charge or no_spawn attempt classification. The legacy positional CLI and transactional payload path keep their existing password domain. Do not add quoting, an invented `--` delimiter, targetless pin, or a receiver patch to work around this limitation. A source selector is not included. `connect` continues to refuse its own `ConnectPayload.credential`; relay is the sole public credential-bearing operation. All other public pairing operations remain unmapped: `begin_pairing`, `cancel_pairing`, `prepare_pairing`, `commit_pairing`, `mark_invalid`, `revoke_pairing`.

Let `P=("doubletake-ctl", "-socket", socket_path, "connect", target.canonical_host)`. Construct option tokens using the existing ConnectPayload validation unchanged; append the credential only after every option token has been constructed.

| Proved original options | Complete argv | Secret index, for verification only |
| --- | --- | --- |
| No audio/volume option proof | `P + (credential,)` | 5 |
| Audio disabled | `P + ("--audio", "off", credential)` | 7 |
| Audio enabled, no initial volume | `P + ("--audio", "on", credential)` | 7 |
| Audio enabled, level n | `P + ("--audio", "on", "--volume", str(n), credential)` | 9 |
| Existing validation permits volume without an explicit audio object | `P + ("--volume", str(n), credential)` | 7 |

The implementation computes `secret_indices=(len(completed_argv)-1,)`; none of the numbers in the last column is an implementation constant. Volume remains an exact integer 0..100 wrapped in `VolumeLevel`. An initial volume with an explicitly disabled audio policy returns `invalid_input`; missing proof for supplied option tokens returns `unsupported_operation`. Relay only supplies combinations captured from a successfully validated start. Every other operation returns `secret_indices=()`.

`ProfileCommand.argv` and `CommandSpec.argv` are the actual execution arguments. `<redacted>` is an observation value, never a credential passed to the receiver.

## 6. Observation, ownership and the one submission predicate

Relay admission requires an existing mirror start stream (`PhysicalMonitorSource` or desktop source `None`), the authority in §5, and a live owner tuple:

```text
(receiver_id, endpoint_generation, session_generation, owner_lease)
```

The daemon instance UUID is additionally pinned for every observation and dispatch. The exact `ReceiverTarget` retains its endpoint fingerprint and canonical host. A reconnect obtains its own lease for the same operator intent; an old lease cannot answer the reconnect's challenge.

`_observe_credential_request` examines only `status.streams`. Exactly one matching entry with `entry.target == stream.target.canonical_host`, `entry.state == "pin_required"` and `entry.credential_kind in {"pin", "password"}` can mint a request. Aggregate `state`, `needs_credential`, `credential_kind`, `target` and a prompting AckReply are hints for another status read. They never select a receiver or fail another start. Multiple matching entries are malformed evidence, not a choice between entries; apply §11's status-retry policy. Unknown credential kind is malformed status, not a new authentication mode.

All three consumers import this exact function from `domain/relay.py`; they do not copy or weaken its Boolean expression:

```python
def can_submit(lease: RelayLeaseView) -> bool:
    r = lease.request
    c = lease.challenge
    return (
        # A request generation is live for exactly this target and owner.
        r is not None
        and c is not None
        and lease.owner_live
        and lease.request_state == "live"
        and lease.request_waiting
        and r.generation == lease.generation
        and r.receiver_id == lease.receiver_id == c.receiver_id
        and r.target == lease.target
        and r.endpoint_generation == lease.endpoint_generation == c.endpoint_generation
        and r.session_generation == lease.session_generation == c.session_generation
        and r.owner_lease == lease.owner_lease
        and r.daemon_instance_uuid == lease.daemon_instance_uuid
        # No superseded or stale challenge is answerable.
        and c.credential_kind == r.credential_kind
        and c.endpoint_fingerprint == lease.target.endpoint_fingerprint
        # The acquisition and release barriers are shared by all callers.
        and lease.submit_inflight is None
        and not lease.submit_fenced
        and lease.dispatch_state in {"none", "settled"}
        # Progress retires input before trust completion.
        and lease.success_pending is None
        and not lease.prepared_source
        and not lease.streaming
    )
```

`request_state="live"` is retired synchronously at expiry or exhaustion before a new view is emitted. Thus `can_submit` performs no I/O, timer scheduling or independent deadline calculation. A source/status reader first verifies ownership and phase time, then updates the lease, then projects it. A stale UI snapshot cannot grant transport authority.

| Call site, in order of use | Required evaluation order | False result |
| --- | --- | --- |
| Presentation | Apply cleanup priority; select the live relay view; call `can_submit(view)`; only then expose Submit or open an input dialog. | No Submit/input dialog. Preserve Cancel for a live challenge. |
| Command admission | Existing receiver/challenge/full-context checks; verify the snapshot's relay owner; call `can_submit(snapshot.relay)`; then accept SubmitPairing and validate input. | Typed `stale_target` refusal, no transport invocation and no consumed attempt. |
| Final spawn fence | Await adapter ownership revalidation; verify exact current receiver/target, endpoint, session, owner lease and original daemon; refresh target waiting evidence from the awaited status check; call `can_submit(_relay_view(stream))`; invoke the synchronous `dispatch_started` reservation; enter `_run`. | `stale_target`, `no_spawn`; if a valid submission reached this transport attempt, consume it once. |

The final check requires a current matching waiting entry of the same kind. If that entry vanishes during the awaited ownership/status check, refuse before spawn. If it vanishes after the final check and before the daemon handles the command, the approved targeted fall-through applies. The fence remains read-only because `EvidenceAuthority.authorize` also calls it (`process/ownership.py:434`). Only the adapter's final `dispatch_started` call reserves `submit_inflight`; this callback rechecks `can_submit` and mutates the lease without awaiting. The adapter does not call the eligibility fence again after reservation against that command's own barrier. Two callers that passed earlier snapshots still serialize at this synchronous final boundary: the first reservation makes the second check false.

Release sets `submit_fenced=True` synchronously before its first await. It remains true through `settling`, `retained`, retries and uncertain disconnects. Only a confirmed `released` transition, or allocation of a new lease/generation by a new start, clears it. A kind change within a releasing lease does not clear it.

## 7. Relay phase controller and deadline arithmetic

Only `RelayPhaseController` owns relay deadlines. The existing transactional coordinator scheduler is not invoked in relay mode. `_await_connecting_stream`, `_verify_reported_source`, `_claim_streaming_readiness` and `_poll_reported_source` consult this controller; they do not maintain independent `accepted`, `permission`, `handshake` or source-expiry owners for the same relay stream. `_poll_reported_source` uses its deadline argument only for the unchanged non-relay path; relay uses the controller's source deadline, including at final readiness acceptance. CONTROL_TIMEOUT=5.0 bounds control/barrier waits, not the human credential lifetime. Poll cadence remains SOURCE_POLL_INTERVAL=0.1.

Controller state fields are `phase: RelayPhase="none"`, `generation: int=0`, `active_deadline: float | None=None`, `permission_deadline: float | None=None`, `handshake_deadline: float | None=None`, `suspended_at: float | None=None`, `request_generation: int | None=None`, `status_malformed: bool=False`, and `scheduled: CancelHandle | None=None` (`domain/ports.py:67`). It also retains `clock: Clock`, `scheduler: Scheduler` and `expire_callback: Callable[[int, ErrorCode], None]` from construction. `status_decoded(valid)` sets status_malformed to `not valid` without changing time; the transport calls it for each completed status decode so that expiry emits the malformed-status override in §11. No timer closure captures a credential.

| Trigger | Precondition | Effect and budget | Trust | Challenge | Dispatch | Code |
| --- | --- | --- | --- | --- | --- | --- |
| Connect acknowledgement | Relay start admitted, phase none | Enter permission; `permission_deadline=observed_at+300.0`; activate that deadline. | Existing until request | None | none | none |
| Matching request first observed | Owner live, request generation new | Enter credential; absolute deadline `request.observed_at+120.0`; save `suspended_at=observed_at`; suspend permission/handshake callback; schedule credential expiry once. | Pairing | Live, input eligible only via §6 | Unchanged | none |
| Same waiting request / InvalidInput / Waiting / retained Rejected / no_spawn | Same generation live | Stay credential; preserve deadline, suspended_at and callback. | Pairing | Retained | Per §8 | Result code from §11 |
| Kind replacement | Same owner live, changed known kind | Retire old phase callback; enter new credential generation with fresh 120.0 s lifetime and counter; preserve suspended allowance without counting the old credential interval as permission time. | Pairing | Old Stale; new live | Outstanding old dispatch remains authoritative | stale_target for old generation |
| Request gone, no prepared source | Fresh matching target entry no longer pin_required | Resume permission; shift its saved deadline by elapsed credential time; clear suspended_at. | Pairing while challenge retained | Live, input withheld by observed progress rules | Per §8 | none |
| Prepared source | Fresh exact-target source first observed | Resume any suspended arithmetic; enter handshake with `observed_at+20.0`; do not start this clock on connecting alone. | Pairing if challenge exists | Live, input retired | Per §8 | none |
| Streaming | Fresh exact-target streaming | Enter source with `observed_at+5.0`; repeated streaming observations do not rearm it; verify physical source within this deadline. | Pairing until handle admission | Live, input retired | Per §8 | none |
| Active deadline reached | `now >= active_deadline`, matching phase generation | Retire phase; fail owner; fence and release. | Untrusted | Expired for credential; closed for other failure | Barrier retained until §8 settles | pairing_expired for credential; unsupported_source for permission/handshake/source |
| Start admitted or terminal cleanup begins | Controller not closed | Increment phase generation; cancel timer; enter closed; no active deadline. | Trusted on admitted handle, otherwise Untrusted | succeeded or terminal | Per §8 | none or terminal cause |
| Stale expiry callback | Callback generation/deadline differs | No mutation. | Unchanged | Unchanged | Unchanged | none |
| Early expiry callback | Matching generation/deadline but now < deadline | No expiry or deadline mutation; the original scheduled deadline remains authoritative. | Unchanged | Unchanged | Unchanged | none |

Suspension uses this arithmetic, not a restarted full allowance:

```python
# enter credential at t0:
suspended_at = t0
# permission_deadline/handshake_deadline retain their absolute pre-suspension values
# leave credential at t1, after proving that the request progressed:
elapsed = t1 - suspended_at
if permission_deadline is not None:
    permission_deadline += elapsed
if handshake_deadline is not None:
    handshake_deadline += elapsed
suspended_at = None
```

For consecutive kind generations, shift suspended deadlines by the elapsed old-generation interval before assigning the new suspended_at. Permission therefore loses none of either credential interval. Retained results never execute this resume block. Leaving pin_required for connecting without a prepared source resumes only the remaining permission allowance. On first prepared-source evidence the 20 s handshake budget begins. Accepted progress latches on the lease for submission eligibility; a transient later snapshot does not re-enable credential input.

Every phase change increments `controller.generation`, cancels the previous active timer, and tags the new callback with `(generation, active_deadline)`. `_expire` first requires both values to equal current state and `clock.now() >= deadline`. Stale or early callbacks return. New observations at exactly a deadline lose to expiry: check time before accepting progress/results. Zero budgets overlap; only the current phase's callback can expire the owner. Source verification's existing 5 s bound is the source phase's deadline, not a second independently rearmed clock.

Malformed status does not suspend, reset or extend any deadline. If every valid observation has disappeared, the active phase still bounds the owner. Its terminal code is `transport_failed` when the unresolved condition at expiry is malformed status, as required by §11.

## 8. Credential dispatch, barrier and settlement

A dispatch is identified by `command_id`, request `generation`, original owner tuple, target and daemon instance. It owns an `attempt=consumed_submissions+1` until its eventual classification; charge an attempt at most once using `charged`. It never stores the secret. `submit_inflight` belongs to the lease, not the submitting coroutine. A candidate awaiting preflight is not yet an admitted dispatch and cannot overwrite `lease.dispatch`. Its local `spawn_admitted` flag is set only by the final reservation. The credential-control result waiter starts its CONTROL_TIMEOUT at that reservation; preflight status reads keep their individual control bound and retry malformed evidence within the active phase budget. If the owner/caller retires before reservation, synchronously set `pre_spawn_retired=True`; the callback permanently refuses that candidate even if its awaited preflight later completes. Owner cancellation/phase expiry retires the candidate without a submission charge. An actual adapter/launch refusal of a live, valid-input submission is no_spawn and charges once. After reservation, a waiter ending without a decoded reply is uncertain and retains the barrier. This ordering distinguishes proven prevention of a future spawn from merely cancelling a task. The generation's consumed counter is read at reservation or no-spawn charging, rather than trusting an earlier candidate's ordinal.

At the final successful spawn fence, the adapter calls `dispatch_started` to set `spawn_admitted=True`, `submit_inflight=generation`, add this `command_id` to `unresolved_dispatch_ids`, install the candidate as the lease's dispatch and enter `dispatch_state="uncertain"` before invoking the runner. This is the conservative unresolved classification during runner entry, including a launch whose completion has not yet been observed. If pre-spawn refusal proves no child existed, classify no_spawn. The complete control-request task is held strongly and shielded from the client waiter's cancellation; a waiter may return after CONTROL_TIMEOUT, while the observer remains responsible for a late decoded reply. `_run` does not impose a cancelling outer timeout on relay in addition to the runner's own bound. If the runner itself has returned without a decoded reply, the lease remains uncertain; only the other permitted settlement evidences can discharge it. Do not reduce cleanup uncertainty to an ordinary Rejected result or discard the request observer.

There are exactly three command classifications: `no_spawn`, `acknowledged`, `uncertain`. `none` is the lease's initial state and `settled` is its normalized state after the classification's barrier conditions finish. They are not additional command outcomes.

| Trigger | Precondition | Effect | Trust | Challenge | Dispatch | Code |
| --- | --- | --- | --- | --- | --- | --- |
| Preflight status malformed | Candidate not reserved, owner live | Retry under current phase budget; do not reserve/charge; candidate is retired if that budget expires. | Pairing | Live until phase expiry | none or already settled | none until transport_failed at budget expiry |
| Candidate retired before spawn | Cancellation/owner retirement, spawn_admitted false | Set pre_spawn_retired; reject any later reservation; no child can exist for this candidate. | Per terminal route | Closed if owner terminal | No admitted dispatch to settle | Terminal route's code |
| Valid submission enters runner | Final fence passed, no other dispatch | Set barrier; hold command/task; no disconnect allowed. | Pairing | Live, input temporarily unavailable | uncertain | none |
| Adapter/pre-exec refuses | Positive evidence no child existed | Classify no_spawn; consume once; settle barrier immediately; retain request only while owner/live expiry/attempt rules allow. | Pairing or Untrusted on terminal outcome | Retained or closed | no_spawn → settled | Exact refusal code |
| Child exits, reply decoded | Same command identity | Classify acknowledged; initiate one status read resolving this target; retain barrier through that read. | Pairing | Live | acknowledged | Reply code, after §11 mapping |
| Reply plus resolving status | Same target/daemon/endpoint/generation | Adopt resumed/recreated entry; set status_resolved; clear submit_inflight; normalize settled; apply §11 result. | Pairing unless terminal | Per result | settled | Per §11 |
| Runner timeout/cancel/no decoded reply | Child may have executed, including live-child cleanup_timeout | Keep barrier/task/lease; withhold Submit and disconnect; surface uncertainty. | Pairing while owner live | Kept, cancel only | uncertain | cleanup_timeout |
| Control waiter ends after decoding but before resolving status | Same command acknowledged, status still unresolved | Preserve acknowledged classification and barrier; continue bounded-by-phase resolving reads; never reopen input or misclassify as a new uncertain command. | Pairing | Kept, cancel only | acknowledged | cleanup_timeout for the incomplete control wait |
| Client coroutine finishes | Dispatch uncertain | No settlement and no clearing of submit_inflight. | Unchanged | Unchanged | uncertain | none |
| Fresh target absent | Dispatch uncertain | No settlement; no Ended result and no disconnect. | Pairing or Untrusted if owner already closed | Kept or terminal as already recorded | uncertain | none |
| Late decoded reply | Exact command identity | Move to acknowledged and perform resolving status read; generation-retired results affect old cleanup only. | No new-generation trust mutation | Current challenge untouched by stale result | acknowledged, then settled on resolved read | Per §11 if current |
| Exclusive progressed entry | Dispatch uncertain; §8 settlement predicate holds | Settle exact command; clear barrier; adopt progress; continue original owner if live. | Pairing | Input retired | settled | none |
| Exact termination proof | Dispatch uncertain or acknowledged with unresolved status | Settle without status/disconnect; let release discharge exact original lease. | Untrusted for failed owner | Closed | settled | daemon_failed for owner loss; release succeeds |

The public adapter returns `CredentialDispatchFailure`, an Err subtype, for every relay credential failure and relay release-disconnect failure. Its `error` is a sanitized Diagnostic, `dispatch_state` is the proved classification, and `refusal` is the closed credential fixed-string classification or None (always None for disconnect). A pre-I/O adapter refusal and certified runner pre-exec failure are no_spawn. A decoded ok=false reply is acknowledged even though it failed. A child with an absent/malformed envelope or no decoded reply is uncertain. Decode the relay acknowledgement before post-command ownership confirmation: confirmation failure suppresses publication but does not erase an already established acknowledged classification. The matching request coroutine, not a process-wide last-error variable, carries this evidence. Non-relay errors retain their existing Err behavior.

The barrier has these precise settlement rules:

1. `no_spawn` settles immediately because no credential acquisition could have occurred.
2. `acknowledged` settles only after the decoded reply **and** a valid fresh status read resolves the target's entry, including confirmed absence on the same daemon and endpoint. Repeated malformed status leaves it unsettled.
3. `uncertain` settles only by an exact late decoded reply followed by rule 2, or a fresh exact-target progress observation under exclusive dispatch, or a valid exact-instance TerminationProof. A decoded reply is dispatch settlement evidence; the ordinary post-reply target-resolution read remains part of the acquisition barrier.

The exclusive-progress predicate is all of: the sole retained `CredentialDispatch.command_id` equals the command being settled; its generation equals the observed request generation; `relay.unresolved_dispatch_ids == {command.command_id}` (the named **single-unresolved-dispatch guard**); target, endpoint generation, session generation, owner lease and daemon UUID match; status was read after command admission; exactly one target entry exists; that entry is no longer pin_required and is connecting with no credential kind, prepared, or streaming. Add command IDs only at final reservation; remove an ID only on that exact command's permitted settlement or exact-instance termination. Keep them through task completion, timeouts, request replacement and retained release. The normal admission predicate prevents a second unresolved dispatch. Independently, if controlled evidence contains two unresolved IDs, the progress observer must retain uncertainty, the barrier and the lease even when every other progress predicate holds; it cannot attribute the observed progress to either command. A bare aggregate change is insufficient. New progress caused by a different lease or daemon is insufficient. Accepted progress is latched, and no second credential command is admitted until the first settles and §6 permits it.

A later connecting entry created by targeted fall-through belongs to the original start when all owner/target/daemon fences still match. Target absence during `submit_inflight` is never readiness failure. After a decoded reply and a valid resolving read settle the barrier, absence is `Ended`. A different receiver at the address is never adopted. The phase controller remains the only readiness clock.

## 9. Single release transition

`DoubleTakeTransport.release(lease, evidence)` is the sole release transition for a relay lease. It is serialized by a shared `release_task` per lease, rather than by competing cleanup coroutines. Each admitted attempt increments `release_attempt` once. A caller arriving during settling awaits the same shielded task and receives its result; cancellation of one caller does not cancel the shared attempt.

The first synchronous action of an admitted release is `submit_fenced=True`, before any await or status read. Publish the fenced view. Then join the initial connect acquisition and the credential barrier under one settlement deadline `clock.now()+CONTROL_TIMEOUT`; lock waiting does not restart that allowance. Polling or a late reply can settle the credential barrier only through §8. At the bound, unresolved acquisition/dispatch gives `retained`, `cleanup_timeout` and zero disconnects.

After settlement, evaluate valid existing evidence; if no existing absence/termination evidence already discharges the obligation and a disconnect can be safely addressed to the original daemon/target, dispatch exactly one `disconnect T` for this attempt. For this release disconnect, pass `dispatch_started=lambda: _reserve_release_dispatch(stream, attempt)` and no dispatch_status callback. The reservation checks that this is the same settling release attempt, submit_fenced is set, the credential barrier is settled, and no disconnect is outstanding; otherwise it returns cleanup_timeout without spawning. It sets disconnect_outstanding synchronously immediately before runner entry. The adapter preserves `CredentialDispatchFailure` classification for a disconnect carrying this callback; an ordinary disconnect's existing diagnostic behavior is unchanged. At most one transport child is emitted per release attempt; an attempt discharged by absence or termination emits zero. No call to cancel, failed-start cleanup, Stop or shutdown emits an additional child outside this transition.

Evidence preference for recording a released outcome is:

| Kind | Exact acceptance predicate | Effect |
| --- | --- | --- |
| `disconnect_ack` | Successful decoded AckReply for BackendOperation.disconnect, target T, same original authenticated daemon/lease; the acquisition barrier settled first. Aggregate `state` is unrestricted: connecting, pin_required or streaming may describe another receiver and does not invalidate this targeted acknowledgement. | released; record acknowledgement as strongest available evidence. |
| `target_absent` | Valid fresh StatusReply obtained after barrier settlement, no entry for T, original daemon UUID and endpoint generation match, ownership proof current for that original endpoint. | released without another disconnect. |
| `daemon_terminated` | Valid §10 proof for the exact original DaemonRecord. | released; no disconnect needed. |
| `unconfirmed` | No accepted evidence, unsafe target addressing, unsettled barrier, failed status or unacknowledged disconnect. | retained, cleanup_timeout; lease held. |

A release status is fresh only if its read began after the barrier settled in this attempt and its recorded `observed_at` is at or after that settlement. Evidence from an earlier retained attempt can discharge only if it is an exact later disconnect acknowledgement or exact-instance terminal proof; an old absence snapshot is reread. A replacement daemon's target absence has no authority over the original lease.

| Trigger | Precondition | Effect | Trust | Challenge | Dispatch | Code |
| --- | --- | --- | --- | --- | --- | --- |
| First release | release_state none | Fence synchronously; set settling; join barrier. | Untrusted on terminal relay route | Close with route cause | Preserve until settlement | Route cause; none for ordinary Stop |
| Concurrent caller | settling and shared task exists | Join same attempt; no child, counter or terminal-record duplication. | Unchanged | Unchanged | Unchanged | Same result as joined attempt |
| Barrier misses CONTROL_TIMEOUT | settling | Set retained; keep submit_fenced; no disconnect. | Untrusted | Closed | uncertain/acknowledged remains unresolved | cleanup_timeout |
| Barrier settles, valid evidence exists | settling | Record preferred evidence; set released; clear submit_fenced; remove exact obligation once. | Untrusted, or existing trust for ordinary post-success Stop | Closed | settled | none for release |
| Barrier settles, no discharge evidence | Original target safely addressable; no outstanding disconnect | Spawn one fenced disconnect; mark disconnect_outstanding until its exact reply, no_spawn certificate or discharge evidence resolves it. | Unchanged | Closed | settled credential dispatch | none while awaiting |
| Disconnect acknowledgement | Exact outstanding attempt, decoded successful reply | Clear outstanding flag; set released once. | Unchanged | Closed | settled | none |
| Decoded disconnect refusal | Exact outstanding attempt, decoded unsuccessful reply | Clear outstanding flag, retain obligation and fence; later call may retry. | Unchanged | Closed | settled credential dispatch | cleanup_timeout for unconfirmed release |
| Certified disconnect no_spawn | Exact release attempt, positive pre-spawn prevention | Clear outstanding flag if reserved, retain obligation/fence; later call may retry. | Unchanged | Closed | settled credential dispatch | cleanup_timeout for unconfirmed release |
| Disconnect lacks acknowledgement | Attempt wait ends | Set retained; retain outstanding flag/task even if its client task has completed without a decoded reply; keep fence. | Untrusted on terminal route | Closed | settled credential dispatch | cleanup_timeout |
| Later release call | retained, prior attempt settled, no disconnect outstanding | Admit a retry on same lease, keep fence, repeat evidence/barrier sequence. | Unchanged | Closed | Preserve | none until attempt result |
| Later release call with outstanding disconnect | retained and outstanding | Join/observe that command; spawn no second disconnect; stay retained absent new accepted proof. | Unchanged | Closed | Preserve | cleanup_timeout if still unresolved |
| Later termination proof | retained or settling, exact original instance | Settle barrier; discharge directly to released; emit no disconnect. Retire observer safely. | Untrusted | Closed | settled | none for release |
| Repeated release after release | released or exact handle in _released | Return Ok(None); no I/O and no second terminal record. | Unchanged | Closed | settled | none |
| Wrong-instance proof | Any held lease | Reject evidence; retain existing obligation; do not clear fence. | Unchanged | Unchanged | Unchanged | stale_event for evidence rejection |

The cleanup retry path already associated with the held resource makes later calls. There is no new retry timer, lease, credential submission or automatic reconnect. `disconnect_outstanding` is cleared only by a decoded response for that exact disconnect, a positive no_spawn certificate, or accepted absence/termination evidence that discharges the lease. A decoded unsuccessful response ends that command without proving release; a later call can retry. If an unacknowledged disconnect child later finishes without a reply, its task completion does not clear the exclusion. The next release call joins/observes it or accepts fresh absence/exact termination evidence; it cannot spawn a second disconnect. A waiter timeout and a concurrent RetryCleanup never clear this flag.

## 10. TerminationProof and historical receipt lookup

The logical query is the approved `registry.termination_proof(record: DaemonRecord) -> Result[TerminationProof]`; the Python method is async because receipt repositories are async (§3). It queries the original record, not `registry.ensure()` or the currently running daemon.

The query uses all receipts and cleanup intents under the existing registry observation authority. It selects exactly one receipt by `receipt.daemon_instance_uuid == record.instance_uuid`, then joins its recorded daemon identity and runtime directory to `record`. It never uses `_receipt_for` or `_current_generation`, never requires the selected receipt to have the maximum endpoint generation, and never substitutes a `reserved` receipt selected by runtime path. A newer generation in the same directory does not invalidate historical terminal evidence. Do not require that the old runtime directory/socket still exist or still have the old inode after teardown; their names identify the historical record and confer no authority over a replacement.

Every proof satisfies all these predicates:

| Predicate | Exact requirement |
| --- | --- |
| Exact original identity | `record.daemon is not None`; proof UUID equals record.instance_uuid; proof.daemon equals record.daemon (pid and start_ticks); proof.runtime_directory equals record.runtime_directory. |
| Unique historical join | Exactly one selected LaunchReceipt; its daemon_instance_uuid, daemon and runtime_directory equal the record; its socket path composition equals record.socket_path. Receipt instance_uuid is the application instance and is not substituted for daemon_instance_uuid. |
| Terminal vocabulary | `receipt.state == "stopped"`, or `receipt.state == "failed"` with exactly one matching `DaemonCleanupIntent` selected by launch_uuid, `intent.outcome == "stopped"`, and `intent.completed_at is not None`. |
| Completion timing | A completed cleanup intent has persisted wall-clock `completed_at >= intent_at`. Copy `intent.completed_at` exactly into `proof.cleanup_completed_at` for the selected launch_uuid; no conversion or comparison to the proof's monotonic identity_verified_at/observed_at or any deadline is permitted. An ErrorCode outcome or noncompleted intent is not completed teardown. |
| Receipt names the process | `receipt.daemon == record.daemon`; matching pid/start_ticks is established before observing absence/exit. A receipt naming a different process is refused even if that process is gone. |
| Positive exit evidence | Either an authoritative procfs read reports that recorded pid absent, or a pidfd opened for the recorded pid reports exit after an exact start_ticks match was established for that open. |
| No inferred absence | Permission errors, unreadable procfs, no process observation, cached daemon.alive=False, a completed client task, socket absence or target absence do not establish process exit. A recycled pid with a different start_ticks is not accepted as a matching pidfd exit. |

For the absent-pid branch, first bind the receipt's attested `ProcessIdentity` to the original record, then read that recorded pid through the existing exact absence classifier. For the pidfd branch, read an exact live identity, open the pidfd, revalidate the same pid/start_ticks, then observe exit on that descriptor; close the descriptor on every path. The proof records `identity_verified_at <= observed_at`. It never signals the process.

Return `Ok(proof)` only after every predicate passes. Return `stale_event` for a different instance/process/runtime identity or ambiguous identity join. Return `cleanup_timeout` when the exact process is still live, exit evidence is unreadable/incomplete, or a failed receipt lacks completed cleanup. Propagate typed repository failures; never translate them to proof. A live failed-to-start receipt from `_fail_owned(..., refuse=True)` yields no proof even if it is the latest receipt.

Active leases pass the proof to `release` after barrier settlement, or use it to settle the barrier itself. `_retired` stream records run the same release transition with the same proof and leave `_retired` only on confirmed release. No generic daemon-exit event or successful shutdown return bulk-drops these records. Iterate exact original records after shutdown/restart; one instance's proof cannot release another instance's obligations. Mark each released handle and publish its stopped/cleanup completion once.

## 11. Outcomes, errors and attempt accounting

`PairingResult` retains the existing kind vocabulary and adds the defaulted waiting field. Relay always has `credential_ref=None`. `waiting=True` means the challenge is retained while its owner is live; it does not grant submission eligibility. `can_submit` remains authoritative during uncertainty or progress.

| Relay outcome | PairingResult.kind | waiting | Diagnostic code | Challenge and trust |
| --- | --- | --- | --- | --- |
| `Trusted`, provisional | Trusted | False | none | Keep challenge, set success_pending, retire input, Pairing; no revision increment. |
| `Waiting`, same kind, attempts remain | ReceiverRejected | True | receiver_rejected | Keep Pairing and original credential expiry; advance revision. |
| `Ended`, target absent after settled barrier | ReceiverRejected | False | receiver_rejected | Close Ended; Untrusted; fail owner and release. |
| `Expired` | Cancelled | False | pairing_expired | Close Expired; Untrusted; release. |
| `Stale`, owner/generation/lease/kind mismatch | Cancelled | False | stale_target | Retire matching old challenge; Untrusted if current owner is terminal; release old obligation. |
| `Rejected`, owner live, attempts remain | TransportFailure | True | Typed daemon/spawn code below | Keep Pairing and absolute expiry; advance revision. |
| `Rejected`, terminal or exhausted | TransportFailure | False | Exact final rejection's code | Close; Untrusted; fail owner; release. |
| `Waiting`, third consumed submission | ReceiverRejected | False | receiver_rejected | Close Ended; Untrusted; fail owner; release. |
| Client-side malformed/empty/unrepresentable value | InvalidInput | True | invalid_input | Keep challenge and counter; advance revision once for its submission_id; no transport call. |
| Operator cancellation | Cancelled | False | none | Close cancelled; Untrusted; release; fixed cancellation notice. |
| Uncertain dispatch | TransportFailure | True while owner live | cleanup_timeout | Keep challenge and barrier; Cancel only, uncertainty notice; no claim of receiver rejection or success. |
| Failed release after a terminal outcome | TransportFailure | False | cleanup_timeout | Challenge stays closed; Untrusted; retained cleanup obligation. Preserve terminal cause separately for the owner outcome. |

The phase/controller and original start result own expiry/source failure. They do not fabricate a submission completion merely to change the counter. All retained outcomes require a live owner, current lease and unelapsed generation. An outcome from a retired generation has no effect on its replacement challenge, trust, revision or attempts; it can settle only the original command/release obligation.

### 11.1 Fixed daemon errors

Only `backend/control.py` classifies error strings. Compare exact strings before generic acknowledgement reduction, at the same time as decoding the valid `ok=false` envelope. The transport receives typed codes and sanitized classification, never text to parse. The three different stale outcomes are distinguished by the adapter's sanitized refusal classification in the current command observer; no backend text is retained.

| Daemon error | ErrorCode | Semantic outcome | Consumes an attempt |
| --- | --- | --- | --- |
| `daemon is shutting down` | daemon_failed | Rejected; owner terminal immediately; close and release even before the third attempt. | Yes, once. |
| `no device is waiting for a credential` | stale_target | Ended; close and fail the owner. | No further retained-attempt charge; terminal Ended. |
| `multiple devices are waiting for credentials; specify a target` | stale_target | Stale; close and fail the owner if ever observed. This remains a regression guard against targetless mapping. | No further retained-attempt charge; terminal Stale. |
| Exact `credential prompt is not ready for ` plus this target.canonical_host | stale_target | Stale; close and fail the owner. | No further retained-attempt charge; terminal Stale. |
| Any other valid non-OK acknowledgement | daemon_failed | Rejected; retain only while owner live and attempts remain. | Yes, once. |

`_credential_rejection(error, target)` returns `CredentialDispatchFailure(error=diagnostic(code), dispatch_state="acknowledged", refusal=...)` with the exact table classification. A different target suffix or non-string error falls into the last row. Do not broaden matching to substrings, exception text or regex guesses. Valid `ok=false` responses with exit 0 or 1 are decoded refusals; other exit statuses retain existing daemon failure handling. A child with no decoded envelope is uncertain, not acknowledged rejection.

### 11.2 Invalid input, malformed status and charges

Input syntax matches the existing coordinator: exact SensitiveText; nonempty text; for pin, exactly four ASCII digits; for password, nonempty text. Relay additionally applies the named `relay_credential_representable(value, target_volume_proven=...)` predicate to the transient revealed text before invoking the transport: it returns False exactly when the proved extended CLI is selected and `value.startswith("-")`. That parser interprets `--audio`/`--volume` as options and rejects other leading-`-` tokens (`0004-target-volume.patch:178`); it has no end-of-options credential form. A password such as `--audio` or `-secret` therefore produces client InvalidInput/invalid_input, preserving the challenge, expiry and zero consumed attempts, with one completion/revision for its submission_id. This restriction applies even if the original start supplied no option tokens. Legacy positional CLI and transactional submissions retain their existing syntax. No credential is rewritten or logged. Invalid input is never sent to `_run`, never increments consumed_submissions, and advances presentation_revision once per distinct completed submission_id, using §12's deduplication. Do not import the transactional coordinator's `_reject` counter behavior into relay.

A valid status must decode with the existing typed decoder and contain no duplicate target entries. Malformed JSON, unknown credential kind, invalid field types or structurally invalid streams cause another status read within the active phase budget. Relay does not use the local three-status-failure counter to end the human phase early. The poll remains cancellable, bounded by the active deadline, and paced at the existing interval. If no resolving valid status arrives before that budget, close the challenge, fail the owner with `transport_failed`, and release; malformed status consumes no credential attempt. Keep an acknowledged dispatch barrier unresolved until a resolving status or exact termination proof exists.

Exactly three **consumed** submissions are admitted to a live generation. Charge once for Waiting, retained/terminal Rejected, positive no_spawn, and daemon-shutdown refusal. A known pre-exec dependency failure retains its typed code (`dependency_missing`, `dependency_incompatible`, `invalid_input`, `unsupported_operation`, or `stale_target` as applicable), rather than becoming a receiver-rejected message. Do not charge again when a late observation repeats the outcome. Provisional Trusted, malformed status, client InvalidInput, duplicate/refused command admission, and unresolved uncertainty do not charge. Terminal Ended/Stale/Expired close independently of the retained-attempt counter.

On the third charge, in one actor/lease transition: close the challenge, retire owner eligibility, set submit_fenced, preserve the third result's code, and enter ordered cleanup through release. The owner is marked failed before waiting on cleanup. Its externally settled failed-start result follows confirmed release or reports retained cleanup_timeout without pretending that resources are gone. A generic daemon refusal on the third attempt therefore reports daemon_failed; a third spawn refusal reports its spawn code; a third Waiting reports receiver_rejected. Cleanup uncertainty is displayed with cleanup_timeout while the terminal cause remains available for the failed owner.

## 12. Actor integration, completion and cleanup

Relay updates reach the actor through the existing pending-start bridge (`session/virtual.py:465`). The event still names the pending start's handle/lease, original context, endpoint fingerprint and daemon instance. Before publishing a view, the bridge/actor verifies receiver, online target, mode MIRROR, active intent, original start operation, session generation, endpoint generation/fingerprint, trust generation, transport lease and daemon UUID. It compares against the pending acquisition's identity before handle admission. It does not set `snapshot.stream` merely to make a pre-ready event pass generic admission.

`PairingFlow.observe_relay` stores the projected relay/challenge and Pairing trust, and copies the typed notice to `snapshot.relay_notice`, without calling `begin_pairing`, changing the start operation/session/lease, or invalidating the trust generation of its still-running owner. It does not launch another start. Transactional events retain the original behavior. On every admitted relay SubmitPairing, `_submit` synchronously allocates a fresh `submission_id: OperationId` **before** client input validation and captures the immutable admitted `RelayLeaseView`. This ID is independent of `context.operation_id` (which existing pairing code replaces with the challenge's operation ID), `challenge_id`, attempt count, result contents and transport `command_id`. Client InvalidInput constructs a completion with this same allocated ID without a transport call. Otherwise pass `_submit_result(..., relay=captured_view, submission_id=submission_id)`; both keywords default to None for the unchanged transactional result path. Relay requires both values. Before awaiting the public PairingPort submission, capture every completion identity field from the admitted view and `message.challenge_id`, and retain the allocated ID in that invocation's transient closure/result wrapper. The actor never wraps a late PairingResult with the current challenge's identity.

The actor's pairing runtime owns `_relay_pending_submissions`, a secret-free map from submission_id to that captured identity tuple (all `RelaySubmitCompletion` fields except result), and `_relay_completed_submission_ids: set[OperationId]`. Register the pending identity before validation/launch. A completion must match a pending ID and its captured full identity; unknown/mismatched IDs are refused as stale_event. On the first matching delivery, atomically remove its pending entry and add its ID to the completed set **before** reducing trust, challenge, attempts or presentation revision. Apply the current owner/generation checks to publication; a retired completion can settle only its original cleanup obligation and still receives a completed-ID tombstone. A duplicate completed ID has no actor mutation, charge, new revision or dialog. Completed IDs remain until the receiver actor is disposed after publication is closed, across cleanup and every lease/challenge replacement; do not discard them merely because a request or transport task ended. A disposed actor publishes no completions, and a new actor allocates fresh IDs. Neither collection stores input/argv or is persisted. Two distinct admitted submissions with identical InvalidInput results have different IDs and each reduce once.

`complete_relay_start` validates the returned StreamHandle against that owner and the transport's current relay generation. Immediately before `_start` returns a successful handle, the transport marks its private request `succeeded` and retires its private input without publishing a premature trust update; the actor commits public success only on acquisition delivery. If a public challenge is live or success_pending is set, `complete_relay_start` commits `trust=Trusted`, closes the challenge as `succeeded`, clears success_pending/input, then admits the exact handle. It updates snapshot.relay to the succeeded/input-retired view. This commit runs before `ReconnectLifecycle._effect`'s `_BLOCKED_TRUST` check. It is idempotent. If a handle arrives first, a later matching Trusted submission result sees the committed generation and settles without clearing or discarding the admitted handle. If submission arrives first, it remains provisional until this same commit. A satisfied challenge does not invalidate the start effect's acquisition identity.

A start without a challenge/success_pending uses the existing admission path. A failed start always clears success_pending and closes the current challenge with that start's exact code. A current Ended/Stale submission makes the waiting owner fail through its existing readiness failure path; a result from an already retired generation only settles old obligations. Failure during source verification after provisional success never publishes Trusted.

Relay-specific cancel, expiry, endpoint replacement, receiver removal, credential_invalid, owner failure and shutdown use `fail_relay`/release. None calls `prepare_pairing`, `commit_pairing`, transactional `cancel_pairing`, `mark_invalid`, `forget_trust`, `revoke_pairing`, or `retry_revocation` on a credential store/coordinator. The public relay `cancel_pairing` method is routing, not a credential-store transaction. `credential_invalid` means Ended and release; trust becomes Untrusted without revocation. No relay failure becomes RevocationPending or TrustCleanupRequired merely because transport cleanup is held.

The dedicated terminal branch runs before ReconnectLifecycle's generic daemon_failed/local-restart and network-retry classification. `pairing_expired`, `receiver_rejected`, `stale_target` and `daemon_failed` from relay are terminal. Other failed relay-start outcomes also finish that intent through cleanup; they do not automatically resubmit a credential. An operator may Start again after the idle postcondition.

OrderedCleanup remains the resource owner. Release settles transport acquisition before disconnect; then its existing capture-release and display-removal stages precede clearing ownership. These stages also release late resources in the acquisition ledger. Do not clear the ledger or `intent_active` as proof that a retained stream vanished. A retained transport obligation leaves cleanup pending and retains its lease. No reconnect or new start reuses that lease.

The final idle postcondition is conjunctive:

```python
idle_postcondition = (
    not state.intent_active
    and state.transport is TransportState.Idle
    and state.display is DisplayState.None_
    and state.stream is None
    and state.physical_source is None
    and state.display_handle is None
    and state.ownership_uuid is None
    and state.intent_stage is IntentStage.idle
    and not state.cleanup_pending
    and state.transport_lease_id is None
    and state.challenge is None
    and no_outstanding_acquisitions
    and no_pending_resources
    and no_retained_relay_obligation
)
```

Only after this commit does an online receiver project Available and allow Start. Existing offline/capability gates still apply. Whole-application shutdown retains its existing 15 s bound; it does not bypass release or report a retained lease as released to meet that bound.

## 13. Complete owner/challenge transition table

This table covers actor-visible transitions; §§7–9 cover every internal phase, dispatch and release transition. `unchanged` means no mutation to that dimension. Codes are ErrorCode values; `none` means no diagnostic is emitted. All rows require their exact owner/generation fence unless the row explicitly rejects a stale event.

| Trigger | Precondition | Effect | Resulting trust | Challenge state | Dispatch state | Code emitted |
| --- | --- | --- | --- | --- | --- | --- |
| Operator mirror Start / owned reconnect | Existing start gates pass | Allocate owner lease; connect with proved options; wait for readiness. | Existing | None | none | none |
| Prompting ack / aggregate-only prompt | No matching streams entry | Read status; do not mint or fail. | unchanged | unchanged | unchanged | none |
| B prompts while A starts | Only B matches pin_required | A continues its own phase; A mints nothing. | unchanged for A | None for A | unchanged | none |
| First exact-target waiting observation | Owner live, known kind | Mint request/challenge generation, counter zero, revision increment; enter credential. | Pairing | Live | none/settled | none |
| Repeated same-kind waiting observation | Generation live | Preserve request creation, attempts, expiry and revision. | Pairing | Live | unchanged | none |
| Known kind changes | Owner live | Retire old generation and dialog; mint new generation/counter; retain unresolved dispatch exclusion. | Pairing | Old Stale; new live | unchanged until settlement | stale_target for old generation |
| Client invalid input | Shared predicate passed, invalid syntax or CLI representation | No child/counter; consume the submission_id completion once; revision increment; fresh prompt once. | Pairing | Live | settled or none | invalid_input |
| Submit refused at command admission | Predicate false or context stale | No invocation, attempt or replacement dialog. | unchanged | unchanged | unchanged | stale_target |
| Final fence fails | Admitted valid input, waiting/identity/fence changed | no_spawn; charge once; retire if stale/terminal/exhausted, otherwise retain as applicable. | Pairing if retained, otherwise Untrusted | Per §11 | settled | stale_target or exact pre-spawn code |
| Positive spawn failure, charges 1 or 2 | Owner/request still live | Retain original expiry; revision increment. | Pairing | Live | settled | Typed spawn code |
| Waiting result, charges 1 or 2 | Reply/status settled, same kind waiting | Charge once; retain expiry; revision increment. | Pairing | Live | settled | receiver_rejected |
| Other retained daemon refusal | Owner live, charges below 3 | Charge once; retain expiry; revision increment. | Pairing | Live | settled after resolving status | daemon_failed |
| Third consumed outcome | Generation current | Close atomically; fail owner; fence, settle and release. | Untrusted | Closed with third code | Preserve until settled | Third outcome's code; cleanup_timeout if release held |
| Target absent while barrier held | Dispatch unresolved | Keep acquisition; no Ended result. | Pairing unless owner already terminal | unchanged | uncertain/acknowledged | none |
| Target absent after reply/status settlement | Current owner waits | Ended; close and fail owner; release. | Untrusted | Ended | settled | receiver_rejected |
| Dispatch uncertain | Child has no decoded reply | Keep lease/barrier; uncertainty notice, Cancel only. | Pairing | Live, input unavailable | uncertain | cleanup_timeout |
| Prepared-source progress | Fresh current target source | Latch progress; retire input; start handshake phase. | Pairing | Live, cancel only | settled if exclusive settlement applies | none |
| Streaming observed by submission | Current generation and target | Set success_pending once; source phase; no revision increment. | Pairing | Live, cancel only | settled | none |
| Handle before submit result | Exact owner succeeds | Commit Trusted and succeeded once, then admit handle. | Trusted | succeeded, removed from public view | settled or later exact completion | none |
| Submit success before handle | success_pending live | Keep Pairing until handle completion. | Pairing | Live, cancel only | settled | none |
| Handle after success_pending | Exact owner succeeds | Commit Trusted and succeeded once; admit handle. | Trusted | succeeded, removed | settled | none |
| Late matching success after commit | Same satisfied generation | Settle old invocation; retain admitted handle/trust. | Trusted | succeeded | settled | none |
| Start timeout/failure after success_pending | Owner readiness fails | Discard pending success; close with start code; release and ordered cleanup. | Untrusted | Closed | Preserve until settled | Start failure code |
| Start failure without pending success | Current owner | Close live challenge if present; release and cleanup. | Untrusted | Closed/none | Preserve until settled | Start failure code |
| Operator Cancel | Live relay challenge, including uncertain/progress | Fence, close, fail owner, join release/barrier. | Untrusted | cancelled | Preserve until settled | none; cancellation notice; cleanup_timeout if retained |
| Credential lifetime expires | now at/after absolute expiry | Fence, close, fail owner and release; no rearm. | Untrusted | Expired | Preserve until settled | pairing_expired |
| credential_invalid | Matching relay owner | Ended; release, no trust-store invalidation/revocation. | Untrusted | Ended | Preserve until settled | receiver_rejected |
| Receiver/endpoint replaced or removed | Original owner invalid | Mark old generation Stale; release only original obligation. | Untrusted | Stale | Preserve until settled | stale_target |
| Daemon shutdown refusal | Decoded fixed string | Charge once; owner terminal regardless of count; close and release. | Untrusted | Closed | acknowledged → settled after resolving evidence | daemon_failed |
| Original daemon exits/replaced | Exact owned instance | Retire stream, close owner; query exact-instance proof; retain without proof. | Untrusted | Stale/closed | Preserve until §8 proof | daemon_failed; cleanup_timeout if retained |
| Application shutdown | Relay obligation exists | Close challenge; run release and ordered resources; no revocation. | Untrusted | cancelled/closed | Preserve until settled | cleanup_timeout only if held |
| Late result, wrong generation/endpoint/owner | Identity mismatches | Ignore publication; allow exact old cleanup observer to settle its own obligation. | unchanged | Current unchanged | Current unchanged | stale_event for rejected completion |
| Cleanup incomplete | Any resource/acquisition retained | Hold cleanup state/lease; withhold Start. | Untrusted on terminal relay | Closed | Preserve | cleanup_timeout |
| Final ordered cleanup commit | All resource stages/effects confirmed | Apply complete idle postcondition; drop public relay projection. | Untrusted after failed/cancelled relay | None | No held dispatch | none, preserving terminal notice/cause |
| Virtual credential request | Mode VIRTUAL | Existing refusal/cleanup; never mint relay or invoke credential connect. | Existing virtual failure semantics | No relay challenge | No relay dispatch | dependency_incompatible; cleanup_timeout precedence if held |

## 14. Presentation revision and exact projection

`ChallengeView.presentation_revision` is the prompt key. The receiver actor is its sole writer, storing `_relay_presentation_revision: int = 0` on its pairing runtime; this counter survives lease and challenge replacement for that actor. Transport views carry the challenge identity with revision 0 until the actor projects it. On a new challenge the actor increments the counter and assigns it to the view; on later updates for the same challenge it preserves the assigned revision except for a completed submission result. It replaces both `snapshot.challenge` and `snapshot.relay.challenge` with that same projected view, so an incoming transport update cannot reset the counter. The presenter consumes `(receiver_id, challenge_id, presentation_revision)` once; the revision itself increases monotonically for that receiver. A repeated snapshot or the same result delivered twice cannot open a second dialog. Two completed InvalidInput submissions increment twice even though generation, attempt count and outcome text are identical. This replaces the insufficient `(generation, attempt, outcome)` key.

The explicit provisional-success exception in design §5.4 takes precedence over its general “every completed submission result” language: provisional Trusted does **not** increment the revision. A new request increments it; every other current completed submission result increments it once, after §12 claims its actor-allocated submission_id. A terminal result can advance it without opening a dialog because no live eligible input remains. Duplicate/stale completions do not advance it. Deduplicate by submission_id for the actor's lifetime, never by result equality, context.operation_id, generation/attempt/outcome, or the present challenge. Progress and release publish updated action eligibility without synthesizing an input revision.

Prompt replacement is serialized per receiver. Invalidate/wipe the old dialog and await its completion before opening its replacement; use the pending key in completion callbacks so an old dialog's finally block cannot remove a newer pending prompt. Re-read the captured receiver's current pane/action set before applying a returned answer. For relay, `_render_now` projects every receiver with an owned live relay challenge and calls `_maybe_open_pairing` with that receiver's own pane; `_maybe_open_pairing` applies §6/revision consumption without a selected-receiver guard on this route. Thus Start A → select B → A's matching request opens A's prompt immediately, without selecting A, while B remains selected. Selection changes neither delay an eligible relay revision nor retarget its answer. Preserve the existing selection rule for transactional prompts. Ordinary dismissal leaves the challenge live and does not itself mint a revision or call Cancel. Every close, hide, reject, replacement, expiry and invalidation wipes typed input and any untaken handoff.

| Projected state | Actions | Input/dialog | Primary text or notice |
| --- | --- | --- | --- |
| Live relay challenge and can_submit(view) | submit_pairing, cancel | One dialog for the unconsumed revision | PIN: “Pairing with <TV>. Enter the PIN shown on that TV.” Password uses the existing password wording. |
| Live relay challenge, dispatch in flight/uncertain | cancel | No credential input; close/wipe any old prompt | Fixed notice: “The receiver's state is uncertain; waiting to confirm the pairing request.” Use cleanup_timeout diagnostic when the control wait expires. |
| Live relay challenge, success_pending or accepted prepared/streaming progress | cancel | Input retired; no new dialog | “Pairing with <TV>; waiting for it to start.” |
| Live relay challenge, submit_fenced/release begins | cancel only while still live; then cleanup actions | No credential input | Existing stopping/cleanup notice. |
| Fully idle Available after §12, online and capable | start and existing non-pairing idle actions | No relay dialog | Existing Start label. |
| Connecting without live relay challenge | Existing active-state actions, including stop | None | Existing Connecting presentation. |
| Streaming after handle admission | Existing active-state actions, including stop and proved audio controls | None | Existing Mirror presentation. |
| Cleanup pending/retained | Existing cleanup actions, including retry_cleanup where already offered | None | Existing cleanup notice; never Available/Start. |
| Offline or capability-gated | Existing offline/capability actions | None | Existing reason. |

A live relay challenge never offers pair, retry, forget or retry_revocation. The shared predicate controls Submit in the UI, actor admission and final fence. Do not infer eligibility merely from `waiting=True`, TrustState.Pairing or a live ChallengeView. A completed stream retains Stop; relay rules do not replace active-stream actions with idle actions.

| Condition | Exact operator message/policy |
| --- | --- |
| Waiting after wrong PIN | “That PIN was not accepted.” |
| Client invalid input | Existing inline input error, no attempt consumed; next eligible revision opens once. |
| Third consumed submission | “Pairing failed.” Start settles after release, or cleanup remains explicit. |
| Operator Cancel | “Pairing cancelled.” |
| Receiver replaced | “That receiver changed; start again.” |
| Daemon shutting down | “The receiver service is stopping; start again.” |
| Keyring unavailable at startup | Existing credential-service diagnostic, not a relay prompt. |
| Receiver forgot pairing later | On the next owned mirror start, mint the same kind of fresh request; no application trust persistence suppresses it. |
| Virtual prompt | Existing named refusal, with the deferred capability documented. |

These are fixed context messages selected from typed state, not raw daemon strings. Existing ErrorCode values remain sufficient. `RelayNotice` is assigned exactly as follows: new request → credential_requested; retained Waiting → pin_rejected; terminal third charge or other owner failure → pairing_failed; Cancel/shutdown cancellation → pairing_cancelled; endpoint replacement/removal → receiver_changed; decoded shutdown refusal → daemon_stopping; unresolved dispatch → uncertain; accepted progress/success_pending → waiting_start. Each assignment is emitted in the relay view before its result completion is delivered. A password Waiting uses the existing password rejection wording instead of the PIN-specific sentence. `_relay_notice` substitutes only the sanitized display title for <TV>. The actor preserves the terminal notice through final idle cleanup, clears it on the next explicit Start, and never reconstructs shutdown-versus-other-refusal from daemon_failed alone. Successful handle admission clears the notice.

## 15. Credential metadata propagation and retention boundary

The only production reveal path is transient input → CredentialPayload → ProfileCommand.argv → CommandSpec.argv → LinuxProcessRunner._exec. The receiver tool owns storage. The application never puts the value into a request/lease, event, diagnostic, receipt, cleanup intent, persistent record, PM ledger, exception message or export. No argv of any kind is added to diagnostics, receipts, persistent records or the PM ledger.

For both command records, indices are a tuple of unique exact integers (`type(i) is int`, so bool is invalid), each `0 <= i < len(argv)`. `redacted_argv` replaces precisely those positions with `<redacted>`. Invalid metadata construction fails with a fixed error that contains no argv/value; `PublishedDoubletakeControl` returns `invalid_input` before any I/O. At `_run`, repeat validation at the adapter boundary, require an empty tuple for every operation except submit_pairing, and require the public relay submit command's one declared index to identify its completed argv's final credential. Do not accept a submit command whose raw secret has no metadata.

```python
# Within _run after operation/metadata validation:
spec = CommandSpec(argv=command.argv, env={},
                   secret_indices=command.secret_indices)
# Actual process receives spec unchanged; repr/observation uses spec.redacted_argv.
```

| Consumer | Required projection/handling |
| --- | --- |
| ProfileCommand.__repr__, CommandSpec.__repr__ | Render redacted_argv only. Preserve neither raw repr nor stringified argv in an exception. |
| testing.adapters.redact | Special-case ProfileCommand and CommandSpec **before** generic dataclass recursion. Explicitly project argv to redacted_argv, then recursively project the remaining declared fields. A safe display property alone cannot protect direct field serialization. |
| Journal.record / entries / calls | Retain only the projected shape; recursive containers containing a command follow the same special case. |
| FakeProcess.runs and spawned command observations | Retain a CommandSpec copy whose argv is redacted_argv and whose indices/env/cwd/pass_fds preserve the sanitized metadata. Execution/script logic can inspect the original spec only during the active invocation. |
| ProbeProcessRecorder.invocations / outer_unshare / other recorder subclasses | Determine operation kind from transient argv if needed; retain only the projected argv/spec. repr=False alone is insufficient. |
| Acceptance ProcessObservation recorder | Same rule; remove its raw `spec.argv` retention (`tests/acceptance/test_no_production_spawn.py:62`) for any secret-bearing command. |
| Private fake ctl/daemon journals and optional captures | Existing raw argv/request capture in `tests/fixtures/processes/fake_doubletake.py:748` and `:756` is not used unchanged for credential tests. Project credential indices before journal/capture; do not capture a payload credential field. Persist only that input was supplied, plus target/options/command ID. |
| Production runner | Use original argv with declared indices, no shell. Retain process identity/status only. Never log the value or use raw stderr/stdout as diagnostic text. |
| Diagnostics/activity/exports/PM ledger | Fixed code and allowlisted fields only; no argv. Transient test canary is searched against all emitted artifacts. |

The production-runner interception test verifies delivery by a Boolean comparison at the declared index inside the intercepted execution boundary. It retains only the comparison result and a redacted projection; failure messages never interpolate the raw spec or canary. The canary source value in the test's local variable is not itself an exported artifact.

The same-UID argv residual is accepted and documented: while ctl runs, another same-user process can inspect its arguments. Redaction protects application observations and storage; it does not claim operating-system argv secrecy.

## 16. Fixtures, exact acceptance tests and mutation inventory

### 16.1 Fixtures and evidence rules

Use the existing offline tripwires in `tests/conftest.py:55` and the no-production-spawn guard. No production doubletake, compositor, portal, network receiver or Secret Service is required by these tests. The fake daemon implements the published per-target behavior, including target-bound resume and absent-target fall-through, in a private temporary runtime. It does not advertise the application-contract fixture as a public backend.

| Fixture | Required behavior/data |
| --- | --- |
| `relay_help_tables` | Existing pinned sender/legacy ctl captures and the existing extended volume table/token setup; exact missing-pin, later-row, wrong-signature and unverified combinations. |
| `relay_clock` / `relay_scheduler` | Existing FakeClock/FakeScheduler adapted to trigger immediately before, at and after 300, 120, 20 and 5 s deadlines; explicitly retain a stale callback for generation testing. |
| `relay_daemon` | At least targets A and B, per-target streams, scripted credential kind/progress, matching original daemon UUID, and a replacement UUID. Credential receipt sets connecting before prepared source; absence between final fence and command execution creates only the named target. Targeted disconnect of A returns a successful acknowledgement with aggregate state streaming while B remains streaming. |
| `relay_dispatch_barriers` | Deterministic gates for ownership/status await, final fence, child creation, decoded reply, post-reply status, source verification and disconnect acknowledgement; delayed C1 command execution remains possible after its client task returns. |
| `relay_store_spy` | Fail immediately on every relay credential-store/coordinator transaction listed in §12; expose a zero-call ledger on success, refusal, cancel, expiry, exhaustion, replacement, invalidation and shutdown. |
| `relay_receipts` | Exact stopped, failed+completed-stopped intent, failed without intent, failed with incomplete/error intent, live failed-to-start, original terminal plus newer current generation, wrong pid/start_ticks/runtime/instance, and unreadable/absent/pidfd-exited process evidence. Supply otherwise-valid stopped and failed+completed-stopped records crossed independently with live and unreadable process evidence, plus absent/pidfd-exited positive controls. Persisted cleanup times are wall-clock values deliberately far from the injected monotonic observation times. |
| `relay_runner_intercept` | Execute PublishedDoubletakeControl._run through the real LinuxProcessRunner.run with `_exec` intercepted by a private deterministic subprocess double; check the actual declared credential position transiently. No replacement fake `_run` can satisfy this test. |
| `relay_presenter` | Real projection/presenter and controlled dialogs; repeated snapshots, delayed dialog destruction, replay of one InvalidInput completion followed by a distinct identical InvalidInput completion, Start A then select B before A's request, and progress while source verification is held. |
| `relay_multiple_dispatch_observation` | Retain uncertain C1 as the sole dispatch reference, then inject a second unresolved ID C2 into that lease's unresolved_dispatch_ids without dispatching another child. Deliver post-admission exact-target connecting progress with no credential kind or prepared source and every other §8 predicate satisfied. A single-ID control uses the same observation and settles. This is controlled observer evidence, not a second production admission route. |
| `relay_forbidden_spawn_probe` | Use offline_boundary_tripwires' inert backend/ctl attempts against its absent paths, with LinuxProcessRunner._exec replaced by a counting stub that raises FileNotFoundError without lookup or process creation. Leave the run/spawn tripwire under test installed and do not replace it with a second refusal wrapper; no subprocess boundary can be reached even by the mutant. |
| `relay_artifacts` | Collect diagnostics, activity/export, journals, retained fake observations, storage files, receipt/intent files and PM ledger output into a canary scan. Actual received secret is never written to this collection. |

Each mutation probe runs against a passing named baseline in a disposable copy and changes only the mechanism named in its row. A collection/import failure, missing dependency, pre-existing red baseline or timeout in the harness is not a killed mutation. Assert both the expected state and child count at deterministic barriers; an eventual final-state check alone cannot prove release/submission ordering.

### 16.2 Required tests

The approved design's §10 provides twenty exact **strategy labels**, not Python test-function names. The following table preserves those labels verbatim and assigns concrete pytest identifiers in this specification. The absence of upstream literal identifiers is recorded as finding D-01 in §19. Each identifier below is a required top-level test, appearing exactly once; parametrization supplies variants without duplicating the definition.

| Design label | File and exact test identifier | Behavior pinned | Mutation(s) it must kill |
| --- | --- | --- | --- |
| Capability | `tests/backend/test_credential_relay.py::test_relay_capability` | Relay true for both exact verified tables; false for missing pin, unknown/later help, failed verification and inconsistent advertisement. | Infer relay from partial table, mere connect/pin presence or unverified surface. |
| Authorization | `tests/backend/test_credential_relay.py::test_relay_authorization` | Public submission accepted with all four transaction flags false; missing relay, owned control, multi_target or physical_source refuses. Virtual_source alone refuses. | Remove relay branch from allows; replace physical gate with physical-or-virtual. |
| Fencing | `tests/backend/test_credential_relay.py::test_relay_fencing` | Relay alone requires the fence and both callbacks; each missing/dropped input gives stale_target and no I/O; entry vanishes during awaited check, no credential child. Transactional fence-less submission is separately gated below. | Make relay fence/callbacks optional; check only before awaited ownership/status. |
| Observation | `tests/transport/test_credential_relay.py::test_relay_observation` | B waiting cannot fail A; aggregate-only prompt mints nothing; matching known-kind change produces new generation/counter/expiry. | Derive request from aggregate status; preserve old kind counter/generation. |
| Command | `tests/backend/test_credential_relay.py::test_relay_command` | Targeted argv, audio off/on and initial volume preserved; computed secret index for every form; volume with disabled audio invalid; nonwaiting target refused; vanished-after-fence entry recreates only T. | Emit targetless pin; drop audio/volume; hard-code one secret index; remove final waiting check. |
| Barrier and adoption | `tests/transport/test_credential_relay.py::test_relay_barrier_and_adoption` | Mid-submit absence does not fail; new matching entry adopted; cancel/release join barrier; unresolved barrier retains. | Treat absence during barrier as failure; drop ownership when submit waiter finishes. |
| Provisional success | `tests/session/test_credential_relay.py::test_relay_provisional_success` | Handle-first and submit-first each commit once; satisfied challenge never discards handle; pending success then timeout cleans up without Trusted. | Close challenge on provisional success; discard owner result after success; publish Trusted before handle. |
| Release | `tests/transport/test_credential_relay.py::test_relay_release` | One child per required disconnect attempt; second caller joins/idempotent; settle before disconnect; acknowledgement, absence, termination and unconfirmed branches; replacement with/without proof; active/retired consumption. | Disconnect before settlement; spawn two disconnects; accept replacement absence; drop retired obligation on daemon event. |
| Phases | `tests/transport/test_credential_relay.py::test_relay_phases` | Suspension preserves remaining permission; >20 s credential wait survives; retained outcomes keep original expiry; stale callback ignored; prepared source starts handshake; all before/at/after budget boundaries. | Resume permission on retained result; rearm credential expiry; start handshake when connecting; accept stale phase callback. |
| Exhaustion | `tests/session/test_credential_relay.py::test_relay_exhaustion` | Three waiting/refusal outcomes, three no-spawn failures and mixed charges close on third exact code and reach idle after release. Invalid input does not charge. | Reset counter on retry; allow fourth consumed submission; use generic rather than third code; count client invalid input. |
| Cleanup | `tests/session/test_credential_relay.py::test_relay_cleanup` | Capture/display release precede ownership clearing; retained resources block idle and Start; Start dispatches only after full postcondition. | Clear ownership before capture/display; project Start with pending acquisition or retained lease. |
| Presentation | `tests/ui/test_credential_relay.py::test_relay_presentation` | Two consecutive invalid submissions reopen twice; repeated snapshot doesn't duplicate; eligible challenge Submit/Cancel; pending success Cancel; active stream Stop; Start only idle. | Key dialogs on generation/attempt/outcome; project Retry/Pair during challenge; remove pending-key serialization. |
| Credential metadata | `tests/backend/test_credential_relay.py::test_relay_credential_metadata` | Metadata validated/propagated for each argv form; every dataclass, journal and retained-observation projection redacted. | Drop secret_indices in _run; rely on repr/property while serializing raw fields; retain raw FakeProcess.runs. |
| Acceptance | `tests/acceptance/test_credential_relay.py::test_relay_acceptance` | build_runtime's real composition: Start → prompt → submit → streaming; no production spawn; same fixture proves virtual credential refusal unchanged. The forbidden-spawn negative control below pins the guard itself. | Disconnect/restart instead of resume; bypass real actor composition; accidentally enable virtual relay. |
| Eligibility (F30) | `tests/ui/test_credential_relay.py::test_relay_eligibility` | Submission-first with source verification held: retire input, no second dialog or submission; Cancel remains effective. | Reopen prompt while success_pending; ignore prepared/streaming progress; clear challenge early. |
| Receipt vocabulary (F31) | `tests/process/test_relay_termination.py::test_relay_receipt_vocabulary` | With positive exit evidence, stopped proves and failed plus completed stopped intent proves; failed with no/incomplete/error intent refuses; no terminated vocabulary. The independent exit predicate has its own four negative cases below. | Accept failed without cleanup; accept any completed_at with ErrorCode outcome; test nonexistent terminated field/value. |
| Retained retry (F32) | `tests/transport/test_credential_relay.py::test_relay_retained_retry` | Later call retries same retained lease; concurrent caller joins; later exact proof discharges directly; outstanding disconnect prevents a second child. | Refuse retained retries; allocate a new lease; spawn while disconnect outstanding; require disconnect despite valid proof. |
| Uncertain dispatch (F33) | `tests/transport/test_credential_relay.py::test_relay_uncertain_dispatch` | No decoded reply retains barrier/lease despite completed task and absence; late reply+resolved status, exclusive progress and proof each settle. | Settle on task completion; settle on absence; release barrier before post-reply status. |
| Exclusivity (F34) | `tests/transport/test_credential_relay.py::test_relay_exclusivity` | Delayed C1 uncertain; C2 neither offered nor admitted nor spawned; progress attributed only to C1; Cancel retains with zero disconnect; delayed C1 cannot recreate after an app disconnect because no disconnect ran. The controlled two-ID observer case below independently pins progress exclusivity. | Admit C2 while uncertain; disconnect during uncertain C1. |
| Release-vs-submit interleaving (F35) | `tests/transport/test_credential_relay.py::test_relay_release_vs_submit_interleaving` | Gate release after setting submit_fenced and before disconnect; assert presentation/admission refuse; independently exercise a submission that passed earlier admission but reaches final fence now; zero credential children; one disconnect covers sole entry; retained retry keeps fence. | Omit submit_fenced from can_submit; clear it on retained release; check only at admission. |

The following separately named tests pin requirements explicitly called out in the design prose and mutation inventory. They supplement the twenty labels; they do not rename them.

| File and exact test identifier | Behavior pinned | Mutation(s) it must kill |
| --- | --- | --- |
| `tests/process/test_relay_termination.py::test_relay_termination_proof` | Live failed-to-start process gives no proof; original daemon termination after replacement proves both active and retired lease release; wrong instance/process/runtime refused. | Accept failed-to-start receipt as termination; reuse current-generation `_receipt_for`; substitute replacement identity. |
| `tests/process/test_relay_termination.py::test_relay_termination_requires_exit` | Four otherwise-valid negative cases: stopped+live, stopped+unreadable, failed-with-completed-stopped-cleanup+live, failed-with-completed-stopped-cleanup+unreadable. Each returns cleanup_timeout and no proof, retaining the original active/retired lease with no alternate exit or release evidence available; for each terminal record, absent-pid and exact pidfd-exited controls return proof. | Remove positive process-exit verification; **each of the four negative cases must independently kill this same mutation** while all identity/join/terminal/timing predicates pass. |
| `tests/process/test_relay_termination.py::test_relay_termination_clock_domains` | Completed cleanup's persisted wall-clock value is copied exactly; identity_verified_at/observed_at remain injected monotonic values. A wall-clock completion numerically greater than observed_at still proves when its own intent-time ordering and exit predicates pass. | Convert/resample cleanup_completed_at as monotonic; compare cleanup completion against observed_at, identity_verified_at or a monotonic deadline. |
| `tests/transport/test_credential_relay.py::test_relay_release_while_other_receiver_streams` | Release A after its barrier settles, with a successful exact disconnect-A acknowledgement reporting aggregate streaming; A is released once, B's stream/lease stays live, and no disconnect-B is sent. Withhold post-ack absence/proof fallback so the acknowledgement alone must discharge A. | Restore an aggregate-state conjunct to disconnect-ack acceptance; require daemon idle or disconnect B to confirm A. |
| `tests/transport/test_credential_relay.py::test_relay_exclusive_progress_requires_single_dispatch` | Same valid exact-target connecting observation with no credential kind or prepared source settles uncertain C1 with unresolved IDs {C1}; controlled IDs {C1,C2} with the same retained C1 and every other predicate true preserve uncertain, submit_inflight and the lease, with no disconnect or success publication. | Attribute progress to a lease with more than one admitted unresolved dispatch by removing only the §8 single-unresolved-dispatch guard. |
| `tests/acceptance/test_credential_relay.py::test_relay_forbidden_spawn_guard` | Independently probe the installed run/spawn tripwires with inert forbidden backend/ctl calls. Require the exact OfflineBoundaryRefusal, the corresponding attempted-boundary record and zero calls to the inert _exec stub. | Remove production-spawn guard: delete only check_process from the corresponding offline_boundary_tripwires guarded_run/guarded_spawn wrapper. The mutant reaches the inert stub and fails the refusal/zero-call assertion without launching anything. |
| `tests/session/test_credential_relay.py::test_relay_submission_completion_deduplication` | First current InvalidInput completion S1 increments revision once; replay S1 makes no change; a second admitted submission S2 with identical value, result and generation but a different submission_id increments once more. Exactly two replacement prompts and zero consumed attempts; S1's tombstone remains through later cleanup/challenge replacement. | Omit submission_id; deduplicate by outcome/generation/attempt or reused context.operation_id; reduce a duplicate twice; clear completed IDs at challenge/lease retirement. |
| `tests/ui/test_credential_relay.py::test_relay_prompt_for_unselected_started_receiver` | Start A, select B, then observe A's owned request: A's labelled prompt opens immediately while B stays selected, with no need to revisit A. Submit/Cancel from it targets A; repeated snapshots do not duplicate it and B gains no challenge or credential child. | Gate relay prompt scan/opening on selected receiver; scan selected pane only; retarget the captured answer to B. |
| `tests/session/test_credential_relay.py::test_relay_option_shaped_password` | Extended CLI with a live password challenge rejects -secret, --audio, --volume and -- as client InvalidInput before any transport/control call, including when the start used no option tokens; preserve counter/expiry, advance revision once per distinct ID. Ordinary password succeeds through validation; legacy positional and transactional password validation remain unchanged. | Skip representability validation; count an option-shaped password as a no_spawn attempt; check only when audio/volume tokens were emitted; change the credential or use an invented delimiter. |
| `tests/backend/test_credential_relay.py::test_relay_production_runner_credential_index` | Intercept actual LinuxProcessRunner execution under PublishedDoubletakeControl for all option variants; true secret reaches declared position; all retained projections contain `<redacted>`. | Execute redacted argv; drop indices before production run; assert fake-runner projection without exercising production boundary; fixed index for every variant. |
| `tests/storage/test_relay_secrets.py::test_relay_canary` | Canary absent from diagnostic, record, ledger, journal, export, repr and every retained observation, including nested dataclass projections and private-fixture captures. | Journal raw argv; retain raw invocation/spec; dump backend error/output or request containing credential. |
| `tests/backend/test_credential_relay.py::test_relay_metadata_refusals` | Negative/out-of-range/bool/duplicate indices, missing relay index, and indices on any other operation refuse before I/O. | Accept invalid metadata; permit credential indices on connect/status. |
| `tests/backend/test_credential_relay.py::test_relay_daemon_error_mapping` | Parametrize each exact fixed error plus wrong-target suffix, non-string and other non-OK acknowledgements; terminal shutdown even on first attempt. | Reduce all errors to receiver_rejected; parse error in transport; retain shutdown challenge; fuzzy target match. |
| `tests/transport/test_credential_relay.py::test_relay_malformed_status` | Repeated malformed status retries until active phase bound; no charge or expiry reset; terminal transport_failed; valid resolving read recovers. | Fail on local third status error; extend deadline on decode error; clear barrier using malformed reply. |
| `tests/session/test_credential_relay.py::test_relay_zero_trust_store_calls` | Store spy covers success, cancel, expiry, credential_invalid, replacement/removal, exhaustion, late result and shutdown; fresh next start can prompt again. | Route relay through prepare/commit/cancel/mark_invalid/forget/revoke/retry_revocation; infer durable app trust. |
| `tests/session/test_credential_relay.py::test_relay_terminal_no_reconnect` | Each terminal relay code fails its owner and cleans up without scheduling reconnect/restart; a later operator Start uses a new lease. | Reuse generic daemon_failed local-restart or retry scheduler; reuse old lease. |
| `tests/session/test_credential_relay.py::test_relay_stale_completion` | Old kind/generation, endpoint, session, owner lease and daemon result cannot mutate replacement request/trust/revision; old barrier can still settle for cleanup. | Wrap late result with current challenge identity; accept partial generation fence; discard old cleanup observer. |
| `tests/ui/test_credential_relay.py::test_relay_prompt_replacement_wipes_input` | Delayed old-dialog destruction cannot erase current pending key; cancel/replacement/expiry/dismissal wipes field and untaken value; captured receiver stays fixed. | Remove pending-key ownership check; retain typed secret; retarget answer to selection. |
| `tests/backend/test_credential_relay.py::test_relay_transactional_compatibility` | Existing transaction-capable profile and application-contract command path retain coordinator/token/payload behavior, including successful submit with no dispatch_fence, dispatch_started or dispatch_status; public relay leaves four transaction flags false and requires all three dispatch inputs. | Add submit_pairing to the unconditional fence-required set; require relay callbacks on transactional submit; replace transactional path with relay; promote transaction flags to permit relay. |
| `tests/backend/test_control.py::test_pairing_operation_flag_truth_table_rejects_only_missing_own_flags` | **Existing compatibility gate, unchanged.** Each pairing operation succeeds with only its own required flags and existing handshake; fence-less submit succeeds with target_bound_pairing/keyring/provisional_commit; removing any own flag refuses before I/O. Preserve its existing command-count assertions. | Impose a global submit fence/callback requirement or new unrelated capability prerequisite on the delivered transactional profile. |
| `tests/acceptance/test_credential_relay.py::test_relay_documented_contract` | Read README.md, docs/backend-compatibility.md and docs/operations.md; assert mirror relay, target-bound argv residual, receiver storage and named virtual deferral claims, plus the extended CLI's option-shaped-password limitation/no-attempt policy; check obsolete unconditional refusals are removed in current sections. | Leave “never places a PIN/password in process arguments”, “No credential prompt ... offered” or unqualified unserviceable-pairing claims as current behavior; omit the representability limitation. |

The Release test parametrizes all four release evidences. The A-released/B-streaming test accepts the targeted acknowledgement alone: hold subsequent status/proof unavailable until after the assertion, so an aggregate-state rejection cannot be masked by an absence fallback. Receipt vocabulary supplies distinct stopped, failed-completed and failed-incomplete cases; a single green stopped case cannot cover the other branches. `test_relay_termination_requires_exit` parametrizes the two otherwise-valid terminal-record forms against live and unreadable process evidence; every one of the four cases must fail under removal of exit verification, independently of terminal-vocabulary guards. Its absent/pidfd-exited positive controls prevent blanket refusal from passing. Clock-domain controls use persisted intent_at=1_800_000_000.0 and completed_at=1_800_000_001.0 with monotonic identity_verified_at=10.0 and observed_at=11.0; the proof preserves these domains and values.

The interleaving test must independently reach both admission and spawn-fence assertions: the first refusal must not prevent the second probe from exercising the final fence. The C1/C2 admission test records credential and disconnect child counts before allowing C1's delayed execution; an eventual failure state alone is not sufficient evidence. Its normal refusal of C2 cannot kill removal of the independent progress-exclusivity guard. The named multiple-dispatch control instead keeps the retained C1 reference and all other progress predicates valid, injecting only unresolved IDs {C1,C2}; assert uncertainty/barrier retention immediately after that progress observation. Removing only the set-equality guard must settle C1 and fail that assertion. No second child is needed or permitted for this control.

Likewise, a normal acceptance path making zero forbidden calls cannot prove interception. `test_relay_forbidden_spawn_guard` must call the installed wrapper through the existing offline_boundary_tripwires attempt, independently for backend spawn and ctl run, with no other wrapper masking this guard. Catch only the fixture's exact refusal_type, assert its corresponding backend/ctl attempt record, and assert the inert _exec stub was never reached. When only that wrapper's check_process call is removed, the stub counts one invocation and raises FileNotFoundError, the runner returns its ordinary typed missing-command failure, and the expected-refusal assertion fails. This is the assigned kill; a downstream tripwire, missing executable, timeout, or incidental test error is not. Both the passing control and mutant remain unable to execute a host program.

## 17. Documentation and compatibility delivered with implementation

This task writes this specification only. The eventual implementation ships the following documentation updates and cites all three files from `test_relay_documented_contract`.

| File | Existing claim to change | Required replacement claim |
| --- | --- | --- |
| `README.md:53` | Receiver pairing cannot be serviced; unqualified remembered-trust limitation. | An owned mirror start/reconnect can answer receiver PIN/password requests in the dashboard; doubletake owns saved credentials; the application adds no persistent trust or forget action. Preserve the separate limitation of the transactional adapter's restart contract. |
| `README.md:93` | Challenge polling shares one 5 s deadline, stops exact target and returns dependency_incompatible; no prompt offered. | Target-specific relay pauses readiness clocks, uses the 120 s absolute challenge lifetime and three consumed attempts, commits trust only on handle admission, and performs ordered cleanup before Start. Virtual requests keep the named existing refusal. |
| `docs/backend-compatibility.md:100` | Application never puts PIN/password in process arguments. | The one exception is proved, fenced, targeted mirror submit_pairing using credential-bearing connect; typed indices redact observations, with the cooperative same-user argv visibility residual. Other pairing operations remain unmapped. |
| `docs/backend-compatibility.md:132` | Credential-bearing connect/pin outside the entire application contract. | Public relay maps targeted connect only; targetless pin remains unused. Both exact tables can prove relay, while ownership/physical-source prerequisites and transaction flags stay independent. Document original option propagation, target-bound fall-through and the extended CLI's inability to represent a credential beginning with `-`: client invalid_input before dispatch, no consumed attempt, with no invented delimiter or receiver patch. |
| `docs/operations.md:19` | Application metadata's credential-reference wording alone describes all pairing. | Distinguish existing transactional references from relay: no relay credential/reference persisted; the backend uses keyring, asks again if the receiver forgets, and startup still requires the credential-service witness. |
| `docs/operations.md:121` | Fresh-process remembered trust discussed without identifying backend relay custody. | Separate backend-retained credentials from unproved application transactional cross-process trust. Explain live PIN workflow, three attempts, original expiry, Cancel, uncertain/retained cleanup and terminal Start availability. Document the extended CLI limitation for leading-`-` passwords and their client InvalidInput/no-attempt handling. |
| `docs/operations.md` | No credential-argv residual or virtual-relay deferral explanation. | State same-UID process visibility during the ctl call, redacted application observations, no application secret store, and the exact mirror-only virtual deferral reason from §1. |

All three current-behavior sections explicitly state the virtual-display deferral and its reason. The delivery summary lists it as deferred, without asking the operator to revisit it. Historical source/build receipts remain dated history and are not rewritten as new live evidence. Offline acceptance is not a live receiver success claim.

For mirror starts only, this delivery supersedes SD-1 term (1) and replaces Amendment 1 §3's submission prerequisites with the approved targeted receiver, proved relay capability, mandatory fence, zero trust-store writes, three-attempt bound, acquisition barrier, provisional success and target-bound fall-through. Every other pairing operation keeps its delivered requirements. No new capability token or receiver rebuild accompanies this change.

## 18. Mechanical invariants

1. Relay authority exists only for an operator-owned mirror start/reconnect and its exact receiver, endpoint, session, daemon and owner lease; its eligible prompt opens regardless of pane selection.
2. Exactly one `can_submit` implementation is imported by presentation, command admission and final spawn fencing; all conjuncts in §6 remain present.
3. Release sets submit_fenced before any await; retained outcomes/retries never clear it; no credential child can spawn after that fence.
4. At most one unresolved credential dispatch exists per lease. Task completion and target absence never settle an uncertain dispatch; independently, progress cannot settle when unresolved_dispatch_ids contains more than that exact command.
5. Acknowledged submission clears its acquisition barrier only after a resolving status read or exact termination proof; no_spawn is the explicit no-acquisition exception.
6. No disconnect occurs before credential settlement; at most one disconnect child exists per release attempt, and none is spawned while a previous disconnect is outstanding.
7. Only release clears a transport obligation; repeated/concurrent callers cannot duplicate a child or completion record. A successful exact disconnect acknowledgement is accepted regardless of aggregate state for another receiver.
8. Termination proof binds the original UUID/pid/start_ticks/runtime, terminal receipt vocabulary and positive exit evidence; current-generation replacement admission is absent from its lookup. cleanup_completed_at copies persisted wall time; every transient timestamp stays monotonic and no comparison crosses domains.
9. A failed receipt without a completed stopped cleanup intent is never termination proof. No `terminated` receipt/outcome value is introduced.
10. Submission Trusted remains Pairing with a live challenge until the start's admitted handle commits Trusted; either arrival order commits once and preserves the handle.
11. Accepted progress and success_pending retire input immediately; no second submission/dialog can occur during source verification.
12. Three consumed submissions exhaust a generation; client InvalidInput (including an unrepresentable extended-CLI credential) and unresolved uncertainty do not consume or reset attempts.
13. Retained Waiting, Rejected and spawn failure preserve the generation's original absolute expiry and suspended permission allowance.
14. Exactly one phase deadline is active; stale generation callbacks cannot expire a newer phase/request; equality with a deadline means expired.
15. Relay calls no credential-store/coordinator transaction or revocation operation, including on invalidation, shutdown and late-result paths.
16. Capture/display release and all pending acquisition settlement precede ownership clearing; Available/Start implies every idle conjunct in §12.
17. Presentation revision is monotonic; each eligible revision opens at most once. Each actor-allocated submission_id reduces at most once for the actor's lifetime, while distinct current InvalidInput submissions each produce their own next revision/dialog.
18. Raw credential exists only in the transient input/execution path. Every observer projects declared indices; diagnostics/receipts/records/PM ledger contain no argv.
19. Both pinned verified tables can prove credential_relay; no inferred ownership/source/transaction permission follows from that bit.
20. Virtual pairing continues to refuse; transactional pairing keeps its existing profile path and fence-less submission. Relay alone requires its submission fence and both callbacks; the existing backend requirement truth-table test remains a compatibility gate. All acceptance tests remain offline.

## 19. Authority findings and naming provenance

**D-01 — No literal pytest identifiers in the approved test strategy.** Design §10 names twenty strategy labels and informal mutation killers such as “command test” and “release-vs-submit interleaving test”; it supplies no `test_...` identifiers. §16 preserves every label and defines concrete identifiers as specification nomenclature. These identifiers are not presented as quotations from the approved design. No test behavior or product decision is invented to fill this naming gap.

**D-02 — A background helper locator does not exist in the supplied checkout.** Design §2.2 names `_after_connect` as the third prompting refusal. This checkout has no such symbol; the corresponding acknowledgement refusal is in `DoubleTakeTransport._start` at `src/airplay_linux/transport/doubletake.py:1042` (`_start` is defined at `:998`). §§2–3 identify the actual implementation site. This is a source-locator discrepancy, not an unresolved behavior or a scope change.

The design's explicit no-revision rule for provisional success (§5.4) is the applicable exception to its general completed-result revision rule (§5.13). Its exactly-one-disconnect wording applies to attempts needing disconnect; its explicit absence/proof branches discharge with zero. These precedence rules preserve the approved behavior and add no operator decision.

**Revision note — spec-review-1 (F-01–F-09).** All nine findings are accepted and resolved; no approved product decision is reopened.

| Finding | Revision and owning sections |
| --- | --- |
| F-01 | Profile-dependent relay fence/callback requirement; transactional fence-less compatibility and unchanged backend requirement gate: §§1–3, 5.2, 16, 18. |
| F-02 | Targeted disconnect acknowledgement accepts another receiver's aggregate state; A-released/B-streaming test: §§1–2, 9, 16, 18. |
| F-03 | Actor-allocated submission_id, actor-lifetime completion deduplication and duplicate-then-distinct InvalidInput test: §§1–4, 11–14, 16, 18. |
| F-04 | Prompt follows the operator-started receiver regardless of selection; Start-A/select-B/prompt-A test: §§1, 3.4, 14, 16, 18. |
| F-05 | Named pre-dispatch CLI representability check, no-attempt InvalidInput, option-shaped-password test and required limitation documentation: §§1–3, 5.3, 11.2, 13, 16–18. |
| F-06 | Four otherwise-valid terminal-record/live-or-unreadable exit cases, each assigned removal of positive exit verification, plus exit-positive controls: §16. |
| F-07 | Single-unresolved-dispatch guard with controlled two-ID progress evidence, and inert forbidden-spawn/run interception controls assigned the guard-removal mutations: §§4, 8, 16, 18. |
| F-08 | Persisted wall-clock cleanup_completed_at, monotonic transient times, forbidden cross-domain comparisons and clock-domain test: §§1–2, 4, 10, 16, 18. |
| F-09 | Correct refusal locator doubletake.py:1042 inside _start, defined at :998: §§2, 19 (D-02). |
