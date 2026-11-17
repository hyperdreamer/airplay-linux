"""Pure mirror-pairing relay contracts: records, eligibility and secret metadata.

These are the domain-level controls for specification §4, §6, §11.2 and §15. They
exercise the frozen records, the single submission predicate, the CLI
representability rule and the redaction metadata carried by ``CommandSpec``.
"""

from __future__ import annotations

import ast
from dataclasses import MISSING, FrozenInstanceError, fields, replace
from pathlib import Path
from types import FunctionType, MappingProxyType
from typing import get_args, get_type_hints
from uuid import UUID, uuid4

import pytest

from airplay_linux.domain import messages, relay
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    ChallengeId,
    ChallengeView,
    InstanceId,
    LeaseId,
    OpaqueCredentialRef,
    OperationId,
    PairingResult,
)
from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.ports import CommandSpec
from airplay_linux.domain.relay import (
    CredentialRequest,
    ReleaseEvidence,
    RelayLeaseView,
    RelayStartOptions,
    SuccessPending,
    TerminationProof,
    can_submit,
    relay_credential_representable,
)
from airplay_linux.domain.resources import AudioPolicy, Diagnostic, VolumeLevel
from airplay_linux.testing.builders import context, target

TARGET = target("a")
OWNER_LEASE = LeaseId(UUID(int=1))
DAEMON = InstanceId(UUID(int=2))
CHALLENGE = ChallengeId(UUID(int=3))
SUBMISSION = OperationId(UUID(int=4))


# --------------------------------------------------------------------------- fixtures


def request_for(**overrides: object) -> CredentialRequest:
    defaults: dict[str, object] = {
        "receiver_id": TARGET.receiver_id,
        "target": TARGET,
        "credential_kind": "pin",
        "endpoint_generation": TARGET.endpoint_generation,
        "session_generation": 1,
        "owner_lease": OWNER_LEASE,
        "daemon_instance_uuid": DAEMON,
        "generation": 1,
        "observed_at": 100.0,
        "start_options": RelayStartOptions(
            target_volume_proven=True,
            audio=AudioPolicy(enabled=True),
            initial_volume=VolumeLevel(value=30),
        ),
    }
    return CredentialRequest(**(defaults | overrides))


def challenge_for(request: CredentialRequest, **overrides: object) -> ChallengeView:
    defaults: dict[str, object] = {
        "challenge_id": CHALLENGE,
        "receiver_id": request.receiver_id,
        "endpoint_fingerprint": request.target.endpoint_fingerprint,
        "endpoint_generation": request.endpoint_generation,
        "operation_id": OperationId(UUID(int=5)),
        "session_generation": request.session_generation,
        "trust_generation": 2,
        "created_at": request.observed_at,
        "expires_at": request.observed_at + 120.0,
        "credential_kind": request.credential_kind,
    }
    return ChallengeView(**(defaults | overrides))


@pytest.fixture
def eligible_view() -> RelayLeaseView:
    """One live request/challenge for this owner with no progress or barrier."""
    request = request_for()
    challenge = challenge_for(request)
    return RelayLeaseView(
        receiver_id=TARGET.receiver_id,
        target=TARGET,
        endpoint_generation=TARGET.endpoint_generation,
        session_generation=1,
        owner_lease=OWNER_LEASE,
        daemon_instance_uuid=DAEMON,
        owner_live=True,
        generation=1,
        request=request,
        request_state="live",
        request_waiting=True,
        challenge=challenge,
        submit_inflight=None,
        submit_fenced=False,
        dispatch_state="none",
        success_pending=None,
        prepared_source=False,
        streaming=False,
        consumed_submissions=0,
        release_state="none",
        phase="credential",
        expires_at=request.observed_at + 120.0,
        notice="credential_requested",
    )


# ----------------------------------------------------------------------- eligibility


@pytest.mark.parametrize("changes", [
    {"owner_live": False}, {"request": None}, {"challenge": None},
    {"request_state": "Stale"}, {"request_waiting": False},
    {"submit_inflight": 1}, {"submit_fenced": True},
    {"dispatch_state": "uncertain"}, {"dispatch_state": "acknowledged"},
    {"prepared_source": True}, {"streaming": True},
])
def test_each_relay_eligibility_gate(eligible_view, changes):
    assert can_submit(eligible_view)
    assert not can_submit(replace(eligible_view, **changes))


# The predicate admits exactly one request state and exactly two dispatch states; every
# declared vocabulary member is exercised, so widening either set must fail by name.
LIVE_REQUEST_STATES = frozenset({"live"})
SUBMITTABLE_DISPATCH_STATES = frozenset({"none", "settled"})


@pytest.mark.parametrize("state", get_args(relay.RequestState))
def test_relay_eligibility_admits_exactly_the_live_request_state(eligible_view, state):
    assert can_submit(replace(eligible_view, request_state=state)) is (
        state in LIVE_REQUEST_STATES
    )


@pytest.mark.parametrize("state", get_args(relay.DispatchState))
def test_relay_eligibility_admits_exactly_the_settled_dispatch_states(eligible_view, state):
    assert can_submit(replace(eligible_view, dispatch_state=state)) is (
        state in SUBMITTABLE_DISPATCH_STATES
    )


@pytest.mark.parametrize("kind", get_args(relay.CredentialKind))
def test_relay_eligibility_accepts_each_matching_credential_kind(eligible_view, kind):
    matching = _challenge(_request(eligible_view, credential_kind=kind), credential_kind=kind)
    assert can_submit(matching) is True
    # Either side alone refuses, so neither comparison may be pinned to one kind.
    for other in get_args(relay.CredentialKind):
        if other == kind:
            continue
        assert can_submit(_challenge(matching, credential_kind=other)) is False
        assert can_submit(_request(matching, credential_kind=other)) is False


def _request(view: RelayLeaseView, **changes: object) -> RelayLeaseView:
    return replace(view, request=replace(view.request, **changes))


def _challenge(view: RelayLeaseView, **changes: object) -> RelayLeaseView:
    return replace(view, challenge=replace(view.challenge, **changes))


IDENTITY_MISMATCHES = (
    ("lease-generation", lambda view: replace(view, generation=2)),
    ("request-generation", lambda view: _request(view, generation=2)),
    ("request-receiver", lambda view: _request(view, receiver_id=ReceiverId("airplay-device:b"))),
    ("lease-receiver", lambda view: replace(view, receiver_id=ReceiverId("airplay-device:b"))),
    ("challenge-receiver", lambda view: _challenge(view, receiver_id=ReceiverId("airplay-device:b"))),
    ("request-target", lambda view: _request(view, target=target("b"))),
    ("lease-target", lambda view: replace(view, target=target("b"))),
    ("endpoint-generation", lambda view: replace(view, endpoint_generation=2)),
    ("request-endpoint-generation", lambda view: _request(view, endpoint_generation=2)),
    ("challenge-endpoint-generation", lambda view: _challenge(view, endpoint_generation=2)),
    ("session-generation", lambda view: replace(view, session_generation=2)),
    ("request-session-generation", lambda view: _request(view, session_generation=2)),
    ("challenge-session-generation", lambda view: _challenge(view, session_generation=2)),
    ("owner-lease", lambda view: replace(view, owner_lease=LeaseId(UUID(int=9)))),
    ("request-owner-lease", lambda view: _request(view, owner_lease=LeaseId(UUID(int=9)))),
    ("daemon-instance", lambda view: replace(view, daemon_instance_uuid=InstanceId(UUID(int=8)))),
    ("request-daemon-instance", lambda view: _request(view, daemon_instance_uuid=InstanceId(UUID(int=8)))),
    ("credential-kind", lambda view: _challenge(view, credential_kind="password")),
    ("request-credential-kind", lambda view: _request(view, credential_kind="password")),
    ("endpoint-fingerprint", lambda view: _challenge(view, endpoint_fingerprint="endpoint-b-v1")),
    ("target-fingerprint", lambda view: replace(view, target=replace(view.target, endpoint_fingerprint="other"))),
    # A host-only disagreement leaves receiver, fingerprint and generations equal.
    ("request-target-host", lambda view: _request(
        view, target=replace(view.target, canonical_host="192.0.2.11"),
    )),
    ("lease-target-host", lambda view: replace(
        view, target=replace(view.target, canonical_host="192.0.2.11"),
    )),
)


@pytest.mark.parametrize(("label", "mutate"), IDENTITY_MISMATCHES, ids=[name for name, _ in IDENTITY_MISMATCHES])
def test_relay_eligibility_rejects_one_identity_mismatch(eligible_view, label, mutate):
    assert can_submit(eligible_view)
    mismatched = mutate(eligible_view)
    assert mismatched != eligible_view, label
    assert not can_submit(mismatched), label


def test_relay_eligibility_accepts_settled_dispatch_and_no_side_effects(eligible_view):
    settled = replace(
        eligible_view, dispatch_state="settled", notice="uncertain",
        expires_at=None, success_pending=None, submit_inflight=None,
    )
    assert can_submit(settled)
    assert not can_submit(replace(settled, submit_fenced=True))
    # The predicate is a pure read: repeated evaluation never mutates the view.
    before = replace(eligible_view)
    assert can_submit(eligible_view) is True
    assert eligible_view == before


def test_relay_eligibility_requires_both_success_pending_and_input_retirement(eligible_view):
    pending = SuccessPending(generation=1, attempt=1, observed_at=130.0)
    assert can_submit(replace(eligible_view, success_pending=pending)) is False
    assert can_submit(replace(eligible_view, request_state="succeeded")) is False
    assert can_submit(replace(eligible_view, request_state="Ended")) is False


# --------------------------------------------------------------------- record shape


RELAY_RECORDS = {
    "RelayStartOptions": (
        ("target_volume_proven", "audio", "initial_volume"),
        {
            "target_volume_proven": "bool",
            "audio": "AudioPolicy | None",
            "initial_volume": "VolumeLevel | None",
        },
    ),
    "CredentialRequest": (
        (
            "receiver_id", "target", "credential_kind", "endpoint_generation",
            "session_generation", "owner_lease", "daemon_instance_uuid", "generation",
            "observed_at", "start_options",
        ),
        {
            "receiver_id": "ReceiverId",
            "target": "ReceiverTarget",
            "credential_kind": "CredentialKind",
            "endpoint_generation": "int",
            "session_generation": "int",
            "owner_lease": "LeaseId",
            "daemon_instance_uuid": "InstanceId",
            "generation": "int",
            "observed_at": "float",
            "start_options": "RelayStartOptions",
        },
    ),
    "SuccessPending": (
        ("generation", "attempt", "observed_at"),
        {"generation": "int", "attempt": "int", "observed_at": "float"},
    ),
    "RelayLeaseView": (
        (
            "receiver_id", "target", "endpoint_generation", "session_generation",
            "owner_lease", "daemon_instance_uuid", "owner_live", "generation", "request",
            "request_state", "request_waiting", "challenge", "submit_inflight",
            "submit_fenced", "dispatch_state", "success_pending", "prepared_source",
            "streaming", "consumed_submissions", "release_state", "phase", "expires_at",
            "notice",
        ),
        {
            "receiver_id": "ReceiverId",
            "target": "ReceiverTarget",
            "endpoint_generation": "int",
            "session_generation": "int",
            "owner_lease": "LeaseId",
            "daemon_instance_uuid": "InstanceId",
            "owner_live": "bool",
            "generation": "int",
            "request": "CredentialRequest | None",
            "request_state": "RequestState",
            "request_waiting": "bool",
            "challenge": "ChallengeView | None",
            "submit_inflight": "int | None",
            "submit_fenced": "bool",
            "dispatch_state": "DispatchState",
            "success_pending": "SuccessPending | None",
            "prepared_source": "bool",
            "streaming": "bool",
            "consumed_submissions": "int",
            "release_state": "ReleaseState",
            "phase": "RelayPhase",
            "expires_at": "float | None",
            "notice": "RelayNotice | None",
        },
    ),
    "TerminationProof": (
        (
            "daemon_instance_uuid", "daemon", "runtime_directory", "launch_uuid",
            "receipt_state", "cleanup_outcome", "cleanup_completed_at", "process_evidence",
            "identity_verified_at", "observed_at",
        ),
        {
            "daemon_instance_uuid": "InstanceId",
            "daemon": "ProcessIdentity",
            "runtime_directory": "str",
            "launch_uuid": "UUID",
            "receipt_state": "Literal['stopped', 'failed']",
            "cleanup_outcome": "Literal['stopped'] | None",
            "cleanup_completed_at": "float | None",
            "process_evidence": "Literal['pid_absent', 'pidfd_exited']",
            "identity_verified_at": "float",
            "observed_at": "float",
        },
    ),
    "ReleaseEvidence": (
        (
            "reason", "observed_at", "disconnect_ack", "status", "daemon_instance_uuid",
            "endpoint_generation", "termination",
        ),
        {
            "reason": "ErrorCode | None",
            "observed_at": "float",
            "disconnect_ack": "AckReply | None",
            "status": "StatusReply | None",
            "daemon_instance_uuid": "InstanceId | None",
            "endpoint_generation": "int | None",
            "termination": "TerminationProof | None",
        },
    ),
    "RelayPairingPayload": (
        ("lease",),
        {"lease": "RelayLeaseView"},
    ),
    "RelaySubmitCompletion": (
        (
            "submission_id", "challenge_id", "receiver_id", "endpoint_generation",
            "session_generation", "owner_lease", "daemon_instance_uuid", "generation",
            "result",
        ),
        {
            "submission_id": "OperationId",
            "challenge_id": "ChallengeId",
            "receiver_id": "ReceiverId",
            "endpoint_generation": "int",
            "session_generation": "int",
            "owner_lease": "LeaseId",
            "daemon_instance_uuid": "InstanceId",
            "generation": "int",
            "result": "PairingResult",
        },
    ),
}


@pytest.mark.parametrize(("name", "expected"), sorted(RELAY_RECORDS.items()))
def test_relay_record_field_inventory_is_complete(name, expected):
    expected_fields, expected_annotations = expected
    owner = messages if name.startswith(("RelayPairing", "RelaySubmit")) else relay
    record = getattr(owner, name)
    assert tuple(field.name for field in fields(record)) == expected_fields
    assert record.__annotations__ == expected_annotations
    assert all(field.kw_only for field in fields(record))
    assert record.__dataclass_params__.frozen


@pytest.mark.parametrize("alias,values", [
    ("CredentialKind", ("pin", "password")),
    ("RequestState", ("live", "succeeded", "Ended", "Expired", "Stale", "cancelled")),
    ("DispatchState", ("none", "no_spawn", "acknowledged", "uncertain", "settled")),
    ("ReleaseState", ("none", "settling", "released", "retained")),
    ("RelayPhase", ("none", "permission", "credential", "handshake", "source", "closed")),
    ("RelayNotice", (
        "credential_requested", "pin_rejected", "pairing_failed", "pairing_cancelled",
        "receiver_changed", "daemon_stopping", "uncertain", "waiting_start",
    )),
])
def test_relay_literal_vocabulary_is_exact(alias, values):
    assert get_args(getattr(relay, alias)) == values


def test_relay_defaulted_fields_keep_existing_contracts_compatible(eligible_view):
    options = RelayStartOptions(target_volume_proven=False, audio=None, initial_volume=None)
    assert (options.audio, options.initial_volume) == (None, None)
    # Every inventory record that must stay required is listed here explicitly.
    required = (
        RelayStartOptions, CredentialRequest, SuccessPending, RelayLeaseView,
        TerminationProof, messages.RelayPairingPayload, messages.RelaySubmitCompletion,
    )
    for record in required:
        assert all(field.default is MISSING and field.default_factory is MISSING
                   for field in fields(record)), record.__name__
    # ReleaseEvidence alone is defaulted; the two inventories are otherwise the same,
    # so no record may silently acquire a default by escaping this check.
    assert {record.__name__ for record in required} | {"ReleaseEvidence"} == set(RELAY_RECORDS)
    defaults = {field.name: field.default for field in fields(ReleaseEvidence)}
    assert defaults.pop("reason") is MISSING and defaults.pop("observed_at") is MISSING
    assert defaults == {
        "disconnect_ack": None, "status": None, "daemon_instance_uuid": None,
        "endpoint_generation": None, "termination": None,
    }
    release = ReleaseEvidence(reason=None, observed_at=12.0)
    assert (release.disconnect_ack, release.status, release.daemon_instance_uuid,
            release.endpoint_generation, release.termination) == (None, None, None, None, None)
    assert replace(release, endpoint_generation=1).endpoint_generation == 1


def test_relay_records_are_frozen_copies(eligible_view):
    with pytest.raises(FrozenInstanceError):
        eligible_view.owner_live = False
    copied = replace(eligible_view, notice="waiting_start")
    assert copied is not eligible_view
    assert copied.notice == "waiting_start"
    assert eligible_view.notice == "credential_requested"
    assert replace(eligible_view) == eligible_view
    assert can_submit(copied)


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "2", None])
@pytest.mark.parametrize("generation", [
    "generation", "endpoint_generation", "session_generation",
])
def test_relay_request_generations_are_exact_nonnegative_integers(generation, bad):
    with pytest.raises((TypeError, ValueError)):
        request_for(**{generation: bad})


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "2", None])
@pytest.mark.parametrize("generation", [
    "endpoint_generation", "session_generation", "generation", "consumed_submissions",
])
def test_relay_lease_generation_is_an_exact_nonnegative_integer(eligible_view, generation, bad):
    assert getattr(replace(eligible_view, **{generation: 0}), generation) == 0
    with pytest.raises((TypeError, ValueError)):
        replace(eligible_view, **{generation: bad})


def success_pending_for(**overrides: object) -> SuccessPending:
    defaults: dict[str, object] = {"generation": 1, "attempt": 1, "observed_at": 100.0}
    return SuccessPending(**(defaults | overrides))


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "2", None])
@pytest.mark.parametrize("generation", ["generation", "attempt"])
def test_success_pending_generations_are_exact_nonnegative_integers(generation, bad):
    assert getattr(success_pending_for(**{generation: 0}), generation) == 0
    with pytest.raises((TypeError, ValueError)):
        success_pending_for(**{generation: bad})


def completion_for(**overrides: object) -> messages.RelaySubmitCompletion:
    defaults: dict[str, object] = {
        "submission_id": SUBMISSION,
        "challenge_id": CHALLENGE,
        "receiver_id": TARGET.receiver_id,
        "endpoint_generation": TARGET.endpoint_generation,
        "session_generation": 1,
        "owner_lease": OWNER_LEASE,
        "daemon_instance_uuid": DAEMON,
        "generation": 1,
        "result": PairingResult(kind="Trusted", credential_ref=None, diagnostic=None),
    }
    return messages.RelaySubmitCompletion(**(defaults | overrides))


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "2", None])
@pytest.mark.parametrize("generation", [
    "endpoint_generation", "session_generation", "generation",
])
def test_relay_submit_completion_generations_are_exact_nonnegative_integers(generation, bad):
    assert getattr(completion_for(**{generation: 0}), generation) == 0
    with pytest.raises((TypeError, ValueError)):
        completion_for(**{generation: bad})


def test_release_evidence_endpoint_generation_is_a_generation():
    with pytest.raises((TypeError, ValueError)):
        ReleaseEvidence(reason=None, observed_at=1.0, endpoint_generation=-1)
    with pytest.raises((TypeError, ValueError)):
        ReleaseEvidence(reason=None, observed_at=1.0, endpoint_generation=True)


def test_pairing_result_waiting_defaults_false_and_keeps_its_kind_vocabulary():
    result = PairingResult(kind="Trusted", credential_ref=None, diagnostic=None)
    assert result.waiting is False
    assert replace(result, waiting=True).waiting is True
    declared = {field.name: field for field in fields(PairingResult)}
    assert declared["waiting"].default is False
    # ``Diagnostic`` is a postponed TYPE_CHECKING import, so supply it for evaluation.
    hints = get_type_hints(
        PairingResult, localns={"Diagnostic": Diagnostic, "OpaqueCredentialRef": OpaqueCredentialRef},
    )
    assert hints["waiting"] is bool
    assert get_args(hints["kind"]) == (
        "Trusted", "InvalidInput", "ReceiverRejected", "Cancelled",
        "CredentialUnavailable", "TrustCleanupRequired", "TransportFailure",
    )


def test_challenge_view_presentation_revision_validation():
    request = request_for()
    assert challenge_for(request).presentation_revision == 0
    assert replace(challenge_for(request), presentation_revision=7).presentation_revision == 7
    for bad in (True, False, 1.0, "1", None, ()):
        with pytest.raises(TypeError, match="^presentation_revision must be an integer$"):
            challenge_for(request, presentation_revision=bad)
    with pytest.raises(ValueError, match="^presentation_revision must be nonnegative$"):
        challenge_for(request, presentation_revision=-1)
    # Revision is a presentation key only; it neither grants nor removes authority.
    revisioned = replace(
        challenge_for(request), presentation_revision=9,
    )
    assert can_submit(replace(eligible_view_for_challenge(revisioned)))


def test_challenge_view_presentation_revision_is_annotated_exactly_int():
    # ``bool`` is an ``int`` subclass, so the annotation is pinned as a type identity,
    # not merely as an accepted value shape.
    hints = get_type_hints(ChallengeView, localns={"ReceiverId": ReceiverId})
    assert hints["presentation_revision"] is int
    view = challenge_for(request_for())
    assert type(view.presentation_revision) is int
    assert type(replace(view, presentation_revision=3).presentation_revision) is int


def eligible_view_for_challenge(challenge: ChallengeView) -> RelayLeaseView:
    request = request_for()
    return RelayLeaseView(
        receiver_id=TARGET.receiver_id, target=TARGET,
        endpoint_generation=TARGET.endpoint_generation, session_generation=1,
        owner_lease=OWNER_LEASE, daemon_instance_uuid=DAEMON, owner_live=True,
        generation=1, request=request, request_state="live", request_waiting=True,
        challenge=challenge, submit_inflight=None, submit_fenced=False,
        dispatch_state="none", success_pending=None, prepared_source=False,
        streaming=False, consumed_submissions=0, release_state="none",
        phase="credential", expires_at=request.observed_at + 120.0,
        notice="credential_requested",
    )


# ------------------------------------------------------------------- representability


@pytest.mark.parametrize(("value", "proven", "expected"), [
    ("1234", True, True),
    ("1234", False, True),
    ("0000", True, True),
    ("password", True, True),
    ("12-34", True, True),
    ("-secret", True, False),
    ("--audio", True, False),
    ("--volume", True, False),
    ("-", True, False),
    ("-secret", False, True),
    ("--audio", False, True),
    ("", True, True),
    ("", False, True),
])
def test_relay_credential_representable(value, proven, expected):
    assert relay_credential_representable(value, target_volume_proven=proven) is expected


def _module_function(path: Path, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    definitions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(definitions) == 1, name
    return definitions[0]


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _statements(definition: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    statements = list(definition.body)
    if statements and _is_docstring(statements[0]):
        statements.pop(0)
    return statements


def _dump(nodes: list[ast.stmt]) -> list[str]:
    return [ast.dump(node, include_attributes=False) for node in nodes]


def test_relay_credential_representable_is_keyword_only_and_retains_nothing():
    assert relay_credential_representable("-1234", target_volume_proven=True) is False
    with pytest.raises(TypeError):
        relay_credential_representable("-1234", True)
    # "Retains nothing" is executable structure, not merely an observable result:
    # after the docstring the body must be exactly the specified return, so any
    # inserted statement (for example retaining ``value`` in a global) fails here.
    definition = _module_function(Path(relay.__file__), "relay_credential_representable")
    # A decorator may retain the argument after the call returns: ``lru_cache`` keeps
    # the credential text as a cache key. No wrapper is admissible at all.
    assert definition.decorator_list == []
    expected = ast.parse('return not (target_volume_proven and value.startswith("-"))').body[0]
    assert _dump(_statements(definition)) == _dump([expected]), ast.dump(
        definition, include_attributes=False
    )
    # The executable twin of the structural check: the module name must still bind a
    # plain function with no memo, wrapper, closure or module-held argument.
    sentinel = "-" + uuid4().hex
    assert relay_credential_representable(sentinel, target_volume_proven=True) is False
    retained = relay_credential_representable
    assert type(retained) is FunctionType, type(retained)
    assert retained.__closure__ is None and retained.__code__.co_freevars == ()
    assert retained.__kwdefaults__ is None and retained.__dict__ == {}
    assert not hasattr(retained, "cache_info") and not hasattr(retained, "__wrapped__")
    assert sentinel not in repr(vars(relay))


# ``can_submit`` is the one admission predicate shared by presentation, command
# admission and the spawn fence. Its body is pinned statement for statement so no
# decorator, global assignment or retained module state can be added unnoticed.
CAN_SUBMIT_STATEMENTS = (
    "r = lease.request",
    "c = lease.challenge",
    '''
return (
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
    and c.credential_kind == r.credential_kind
    and c.endpoint_fingerprint == lease.target.endpoint_fingerprint
    and lease.submit_inflight is None
    and not lease.submit_fenced
    and lease.dispatch_state in {"none", "settled"}
    and lease.success_pending is None
    and not lease.prepared_source
    and not lease.streaming
)
''',
)


def test_can_submit_is_a_state_free_pinned_predicate():
    definition = _module_function(Path(relay.__file__), "can_submit")
    assert definition.decorator_list == []
    assert not any(
        isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(definition)
    ), ast.dump(definition, include_attributes=False)
    expected = [ast.parse(source).body[0] for source in CAN_SUBMIT_STATEMENTS]
    assert _dump(_statements(definition)) == _dump(expected), ast.dump(
        definition, include_attributes=False
    )


# ------------------------------------------------------------------ snapshot fields


def test_session_snapshot_relay_projection_is_transient_and_defaulted(eligible_view):
    idle = SessionSnapshot.idle(TARGET.receiver_id)
    assert idle.relay is None
    assert idle.relay_notice is None
    projected = replace(idle, relay=eligible_view, relay_notice="credential_requested")
    assert projected.relay is eligible_view
    assert projected.relay_notice == "credential_requested"
    assert replace(projected).relay == eligible_view
    with pytest.raises(FrozenInstanceError):
        projected.relay = None


def test_no_persistent_codec_admits_relay_records_or_snapshot_fields(eligible_view):
    from airplay_linux.storage import codecs

    assert not [name for name in dir(codecs) if "relay" in name.lower()]
    names = {name for name, _ in codecs._SESSION_FIELDS}
    assert "relay" not in names and "relay_notice" not in names and "challenge" not in names
    snapshot = replace(
        SessionSnapshot.idle(TARGET.receiver_id), relay=eligible_view,
        relay_notice="credential_requested",
    )
    payload = codecs.encode_session(snapshot)
    assert b"relay" not in payload
    restored = codecs.decode_session(payload)
    assert restored.relay is None and restored.relay_notice is None
    document = payload.replace(b"{", b'{"relay":null,', 1)
    with pytest.raises(codecs.RecordError):
        codecs.decode_session(document)


def _is_type_checking_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Name)
        and node.test.id == "TYPE_CHECKING"
    )


def _absolute_import_parts(node: ast.ImportFrom, package: str) -> list[str]:
    if not node.level:
        return node.module.split(".") if node.module else []
    package_parts = package.split(".")
    keep = len(package_parts) - (node.level - 1)
    if keep <= 0:
        return []
    parts = package_parts[:keep]
    if node.module:
        parts.extend(node.module.split("."))
    return parts


def _relay_import_names(nodes: list[ast.stmt], package: str) -> list[str]:
    names: list[str] = []
    for node in nodes:
        if _is_type_checking_guard(node):
            # Exempt exactly the type-only body. The test expression and every
            # other branch remain part of the executable, checked module.
            names.extend(_relay_import_names(node.orelse, package))
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Import):
                names.extend(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = ".".join(_absolute_import_parts(child, package))
                if base:
                    names.append(base)
                    names.extend(
                        f"{base}.{alias.name}"
                        for alias in child.names if alias.name != "*"
                    )
    return names


def _type_checking_binding_findings(tree: ast.Module) -> tuple[bool, list[str]]:
    """Report every rebinding of ``TYPE_CHECKING`` and prove the typing import.

    Exempting the guarded import block is only sound while the guard name is the
    ``typing.TYPE_CHECKING`` constant. A later ``TYPE_CHECKING = True`` would run the
    block, so any rebinding is a finding rather than an exemption.
    """
    bound_from_typing = False
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "TYPE_CHECKING" and alias.asname != "TYPE_CHECKING":
                    continue
                if (node.level == 0 and node.module == "typing"
                        and alias.name == "TYPE_CHECKING" and alias.asname is None):
                    bound_from_typing = True
                else:
                    findings.append(
                        f"line {node.lineno}: TYPE_CHECKING from {node.module!r} "
                        f"level {node.level} as {alias.asname!r}"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.name.split(".")[0] == "TYPE_CHECKING"
                        or alias.asname == "TYPE_CHECKING"):
                    findings.append(f"line {node.lineno}: import {alias.name!r}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and "TYPE_CHECKING" in node.names:
            findings.append(f"line {node.lineno}: declared global TYPE_CHECKING")
        elif (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"
                and isinstance(node.ctx, ast.Store)):
            findings.append(f"line {node.lineno}: TYPE_CHECKING assigned")
        elif isinstance(node, ast.arg) and node.arg == "TYPE_CHECKING":
            findings.append(f"line {node.lineno}: TYPE_CHECKING parameter")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == "TYPE_CHECKING":
            findings.append(f"line {node.lineno}: TYPE_CHECKING defined")
    return bound_from_typing, findings


def test_relay_type_checking_guard_is_the_typing_constant():
    tree = ast.parse(Path(relay.__file__).read_text(encoding="utf-8"))
    bound_from_typing, findings = _type_checking_binding_findings(tree)
    assert bound_from_typing, "the type-only import exemption needs typing.TYPE_CHECKING"
    assert findings == [], findings
    # Executable twin: at runtime the guard must still be the false typing constant.
    assert relay.TYPE_CHECKING is False
    guarded = [node for node in ast.walk(tree) if _is_type_checking_guard(node)]
    assert len(guarded) == 1, [ast.dump(node, include_attributes=False) for node in guarded]
    for node in guarded:
        assert not node.orelse
        assert all(isinstance(child, (ast.Import, ast.ImportFrom)) for child in node.body)


def test_relay_module_is_pure_and_imports_no_transport_qt_process_or_storage():
    source = Path(relay.__file__).read_text(encoding="utf-8")
    package = relay.__package__ or "airplay_linux.domain"
    modules = _relay_import_names(ast.parse(source).body, package)
    forbidden = (
        "asyncio", "PySide6", "qasync", "airplay_linux.transport",
        "airplay_linux.process", "airplay_linux.storage", "airplay_linux.backend",
    )
    for module in modules:
        assert not any(module == root or module.startswith(root + ".")
                       for root in forbidden), module


# ------------------------------------------------------------------ command metadata


@pytest.mark.parametrize("indices", [(-1,), (2,), (True,), (1, 1)])
def test_command_secret_indices_are_exact(indices):
    with pytest.raises(ValueError, match="^invalid secret indices$"):
        CommandSpec(argv=("ctl", "value"), env={}, secret_indices=indices)


@pytest.mark.parametrize("indices", [None, [1], {1}, "1", (1.0,), ("1",), (0, 0)])
def test_command_secret_indices_are_an_exact_integer_tuple(indices):
    with pytest.raises(ValueError, match="^invalid secret indices$"):
        CommandSpec(argv=("ctl", "value"), env={}, secret_indices=indices)


@pytest.mark.parametrize(("argv", "secret_indices", "redacted"), [
    (("1234", "--pin"), (0,), ("<redacted>", "--pin")),
    (("1234", "visible", "tail"), (0, 2), ("<redacted>", "visible", "<redacted>")),
])
def test_command_secret_indices_accept_every_populated_position(argv, secret_indices, redacted):
    # Index zero is a real position: the declared bound is ``0 <= index < len(argv)``.
    command = CommandSpec(argv=argv, env={}, secret_indices=secret_indices)
    assert command.argv == argv
    assert command.secret_indices == secret_indices
    assert command.redacted_argv == redacted
    assert "1234" not in repr(command)


def test_command_projection_preserves_execution_arguments():
    command = CommandSpec(argv=("ctl", "1234"), env={}, secret_indices=(1,))
    assert command.argv[1] == "1234"
    assert command.redacted_argv == ("ctl", "<redacted>")
    assert "1234" not in repr(command)


def test_command_projection_redacts_only_declared_positions():
    command = CommandSpec(
        argv=("doubletake-ctl", "-socket", "/tmp/s", "connect", "10.0.0.2",
              "--audio", "on", "--volume", "30", "-1234"),
        env={}, secret_indices=(9,),
    )
    assert command.redacted_argv[:9] == command.argv[:9]
    assert command.redacted_argv[9] == "<redacted>"
    assert len(command.redacted_argv) == len(command.argv)
    assert "1234" not in repr(command)
    assert "1234" not in repr({"spec": command})
    assert "1234" not in repr([command, command])
    assert command.secret_indices == (9,)


def test_command_projection_redacts_every_declared_position_not_only_the_last():
    argv = ("doubletake-ctl", "pair", "1111", "--audio", "on", "--pin", "2222", "tail")
    command = CommandSpec(argv=argv, env={}, secret_indices=(2, 6))
    assert command.argv == argv
    assert command.secret_indices == (2, 6)
    assert command.redacted_argv == (
        "doubletake-ctl", "pair", "<redacted>", "--audio", "on", "--pin",
        "<redacted>", "tail",
    )
    assert command.redacted_argv[-1] == "tail"
    for observation in (repr(command), repr({"spec": command}), repr([command, command])):
        assert "1111" not in observation
        assert "2222" not in observation


def test_command_without_declared_secrets_keeps_its_vector():
    command = CommandSpec(argv=("status",), env={"A": "1"})
    assert command.secret_indices == ()
    assert command.redacted_argv == ("status",)
    assert command.cwd is None and command.pass_fds == ()
    assert isinstance(command.env, MappingProxyType)


def test_command_env_is_detached_and_frozen():
    env = {"AIRPLAY_LINUX_OWNER_UUID": "owner"}
    command = CommandSpec(argv=("helper",), env=env, cwd="/tmp", pass_fds=(3,))
    env["AIRPLAY_LINUX_OWNER_UUID"] = "changed"
    assert command.env["AIRPLAY_LINUX_OWNER_UUID"] == "owner"
    assert command.cwd == "/tmp" and command.pass_fds == (3,)
    with pytest.raises(FrozenInstanceError):
        command.secret_indices = (0,)


# ------------------------------------------------------------- payload vocabularies


def test_relay_pairing_payload_joins_the_transport_payload_vocabulary(eligible_view):
    payload = messages.RelayPairingPayload(lease=eligible_view)
    assert isinstance(payload, messages.TransportPayload)
    assert payload.lease == eligible_view
    assert messages.TransportEventKind.pairing_required.value == "pairing_required"
    assert messages.RelayPairingPayload in get_args(messages.TransportPayload)
    assert messages.ChallengePayload in get_args(messages.TransportPayload)


def test_relay_submit_completion_carries_the_admitted_identity(eligible_view):
    result = PairingResult(kind="InvalidInput", credential_ref=None, diagnostic=None, waiting=True)
    completion = messages.RelaySubmitCompletion(
        submission_id=SUBMISSION, challenge_id=CHALLENGE,
        receiver_id=TARGET.receiver_id, endpoint_generation=TARGET.endpoint_generation,
        session_generation=1, owner_lease=OWNER_LEASE, daemon_instance_uuid=DAEMON,
        generation=1, result=result,
    )
    assert completion.submission_id == SUBMISSION
    assert completion.result is result
    assert completion.owner_lease == OWNER_LEASE
    assert completion.daemon_instance_uuid == DAEMON
    assert completion != replace(completion, generation=2)
    assert completion != replace(completion, submission_id=OperationId(UUID(int=6)))
    with pytest.raises(FrozenInstanceError):
        completion.generation = 2
    # The completion identity is the admitted operation context, never a new lease.
    admitted = context(generation=1, endpoint_generation=1, lease=1)
    assert admitted.session_generation == completion.session_generation
    assert admitted.transport_lease_id == completion.owner_lease
