"""Resolution policy never guesses a receiver mode.

A remembered choice is reused only through the storage layer's continuity filter,
only while its own evidence is trustworthy, and only while current reliable
reports do not exclude it. Otherwise a single reliable advertised or queried mode
may be used; anything else becomes an explicit typed decision for the user. The
tests pin precedence, contradiction of a remembered size, identity replacement in
both discovery arrival orders, and the rule that a preference is written only
after a confirmed setup.
"""

from __future__ import annotations

import hashlib

import pytest

from airplay_linux.discovery.identity import IdentityResolver, target_for
from airplay_linux.domain.identity import ReceiverId, ReceiverRecordSet
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import (
    Ok,
    ReceiverCapabilities,
    ResolutionDecision,
    ResolutionEvidence,
    Size,
)
from airplay_linux.policy.resolution import (
    ResolutionRequired,
    accepted_mode_mismatch,
    remember_after_success,
    resolve_resolution,
)
from airplay_linux.storage import PreferenceAccess
from airplay_linux.testing.builders import service, target

FHD = Size(width=1920, height=1080)
UHD = Size(width=3840, height=2160)
HD = Size(width=1280, height=720)
SVGA = Size(width=1024, height=768)
DIGEST = hashlib.sha256(b"one-record-set").hexdigest()
FINGERPRINT = ReceiverId(f"airplay-fingerprint:{DIGEST}")
AMBIGUOUS = ReceiverId(f"airplay-ambiguous:{DIGEST}")
OTHER = ReceiverId("airplay-device:b")


# --------------------------------------------------------------------------- fixtures


def record_set(**overrides: object) -> ReceiverRecordSet:
    built = target()
    defaults: dict[str, object] = {
        "records": built.service_records,
        "receiver_id": built.receiver_id,
        "identity_kind": "device",
        "canonical_address": built.canonical_host,
        "endpoint_fingerprint": built.endpoint_fingerprint,
        "endpoint_generation": built.endpoint_generation,
        "persistable": True,
        "continuity_verified": True,
        "replacement_of": None,
        "ambiguous": False,
    }
    return ReceiverRecordSet(**(defaults | overrides))


def decision(
    candidate: ReceiverRecordSet | None = None,
    size: Size = FHD,
    source: str = "user",
    origin: str = "picker",
    receiver_id: ReceiverId | None = None,
) -> ResolutionDecision:
    candidate = record_set() if candidate is None else candidate
    return ResolutionDecision(
        size=size,
        source=source,  # type: ignore[arg-type]
        evidence=ResolutionEvidence(
            origin=origin,
            reliable=True,
            receiver_id=candidate.receiver_id if receiver_id is None else receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def saved(
    remembered: ResolutionDecision | None = None, **overrides: object
) -> SavedReceiver:
    candidate = record_set()
    endpoint = target_for(candidate)
    assert isinstance(endpoint, Ok)
    defaults: dict[str, object] = {
        "receiver_id": candidate.receiver_id,
        "service_metadata": candidate.records,
        "display_name": "Living Room",
        "last_endpoint": endpoint.value,
        "continuity_fingerprint": candidate.endpoint_fingerprint,
        "credential_ref": None,
        "remembered_resolution": decision(receiver_id=candidate.receiver_id)
        if remembered is None
        else remembered,
    }
    return SavedReceiver(**(defaults | overrides))


def reported(
    *modes: Size,
    reliable: bool = True,
    evidence: str = "advertised-modes",
) -> ReceiverCapabilities:
    return ReceiverCapabilities(modes=tuple(modes), reliable=reliable, evidence=evidence)


def advertisement(kind: str = "airplay", **overrides: object):
    defaults: dict[str, object] = {
        "instance": "Living Room",
        "model": "AppleTV6,2",
        "device_id": None,
    }
    return service(kind=kind, **(defaults | overrides))


def resolved_target(candidate: ReceiverRecordSet):
    result = target_for(candidate)
    assert isinstance(result, Ok)
    return result.value


def saved_for(
    candidate: ReceiverRecordSet,
    size: Size = FHD,
    source: str = "user",
) -> SavedReceiver:
    return SavedReceiver(
        receiver_id=candidate.receiver_id,
        service_metadata=candidate.records,
        display_name="Living Room",
        last_endpoint=resolved_target(candidate),
        continuity_fingerprint=candidate.endpoint_fingerprint,
        credential_ref=None,
        remembered_resolution=ResolutionDecision(
            size=size,
            source=source,  # type: ignore[arg-type]
            evidence=ResolutionEvidence(
                origin="picker",
                reliable=True,
                receiver_id=candidate.receiver_id,
                endpoint_fingerprint=candidate.endpoint_fingerprint,
            ),
        ),
    )


# ------------------------------------------------------------------ remembered mode


def test_a_remembered_mode_wins_over_reliable_current_reports():
    candidate = record_set()
    result = resolve_resolution(
        candidate,
        saved(),
        reported(UHD, FHD, evidence="advertised-modes"),
        reported(HD, evidence="query_modes"),
    )
    # Both reports are reliable, and the remembered FHD is still among the
    # advertised modes, so continuity and current compatibility agree. Memory
    # outranks the multi-mode picker and the newer query.
    assert result == ResolutionDecision(
        size=FHD,
        source="stored",
        evidence=ResolutionEvidence(
            origin="remembered",
            reliable=True,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def test_a_remembered_mode_survives_a_compatible_device_endpoint_change():
    resolver = IdentityResolver()
    original = resolver.update((advertisement(device_id="00:11:22:33:44:55"),))[0]
    stored = saved_for(original)
    moved = resolver.update(
        (
            advertisement(
                device_id="001122334455",
                host="renamed.local",
                addresses=("192.0.2.11",),
                port=7100,
            ),
        )
    )[0]
    assert moved.receiver_id == original.receiver_id
    assert moved.continuity_verified and moved.replacement_of is None
    assert moved.endpoint_fingerprint != original.endpoint_fingerprint

    result = resolve_resolution(moved, stored, None, None)

    assert isinstance(result, ResolutionDecision)
    assert result.size == FHD
    assert result.source == "stored"
    # The reused decision describes the endpoint that is current now, not the
    # endpoint the preference happened to be saved at.
    assert result.evidence.endpoint_fingerprint == moved.endpoint_fingerprint


def test_a_saved_record_without_a_remembered_resolution_is_not_reported_as_rejected():
    candidate = record_set()
    stored = saved(remembered=None, remembered_resolution=None)
    result = resolve_resolution(candidate, stored, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=False)


def test_a_saved_record_without_a_remembered_resolution_still_uses_reliable_evidence():
    candidate = record_set()
    stored = saved(remembered=None, remembered_resolution=None)
    result = resolve_resolution(candidate, stored, reported(HD), None)
    assert isinstance(result, ResolutionDecision)
    assert result.size == HD and result.source == "discovery"


def test_a_reliable_report_that_excludes_the_remembered_size_requires_a_decision():
    # Current reliable evidence is the authority on compatibility. A receiver
    # that no longer reports the remembered mode must not be sent a reservation
    # at that size; the contradiction is a pre-output decision, and the
    # post-setup accepted-mode path is not a substitute for it.
    result = resolve_resolution(record_set(), saved(), reported(HD), None)
    assert result == ResolutionRequired(choices=(HD,), remembered_rejected=True)


def test_a_remembered_size_excluded_by_several_reliable_modes_is_a_decision():
    result = resolve_resolution(record_set(), saved(), reported(UHD, HD), None)
    assert result == ResolutionRequired(choices=(UHD, HD), remembered_rejected=True)


def test_a_remembered_size_that_reliable_evidence_still_supports_is_reused():
    result = resolve_resolution(record_set(), saved(), reported(FHD, HD), None)
    assert isinstance(result, ResolutionDecision)
    assert result.size == FHD and result.source == "stored"


def test_an_unreliable_report_cannot_replace_a_remembered_mode():
    result = resolve_resolution(
        record_set(), saved(), reported(HD, reliable=False), None,
    )
    assert isinstance(result, ResolutionDecision)
    assert result.size == FHD and result.source == "stored"


# ------------------------------------------------------------------ reliable evidence


def test_a_single_reliable_discovery_mode_is_used_with_exact_evidence():
    candidate = record_set()
    result = resolve_resolution(candidate, None, reported(FHD), None)
    assert result == ResolutionDecision(
        size=FHD,
        source="discovery",
        evidence=ResolutionEvidence(
            origin="discovery",
            reliable=True,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def test_a_reliable_pre_stream_query_is_used_when_discovery_has_no_mode_evidence():
    candidate = record_set()
    result = resolve_resolution(
        candidate, None, None, reported(HD, evidence="query_modes"),
    )
    assert result == ResolutionDecision(
        size=HD,
        source="discovery",
        evidence=ResolutionEvidence(
            origin="query",
            reliable=True,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def test_discovery_precedes_the_pre_stream_query():
    result = resolve_resolution(
        record_set(), None, reported(HD), reported(UHD, evidence="query_modes"),
    )
    assert isinstance(result, ResolutionDecision)
    assert result.size == HD
    assert result.evidence.origin == "discovery"


def test_an_unreliable_discovery_report_does_not_block_a_reliable_query():
    result = resolve_resolution(
        record_set(), None, reported(SVGA, reliable=False), reported(HD),
    )
    assert isinstance(result, ResolutionDecision)
    assert result.size == HD
    assert result.evidence.origin == "query"


def test_a_reliable_report_without_modes_is_not_evidence():
    result = resolve_resolution(record_set(), None, reported(), None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=False)


def test_multiple_equally_reliable_modes_require_an_explicit_choice():
    result = resolve_resolution(record_set(), None, reported(UHD, FHD, HD), None)
    # Reported order is preserved for presentation; no mode is chosen for the user.
    assert result == ResolutionRequired(choices=(UHD, FHD, HD), remembered_rejected=False)


def test_duplicate_reported_modes_are_one_choice():
    result = resolve_resolution(record_set(), None, reported(FHD, FHD), None)
    assert isinstance(result, ResolutionDecision)
    assert result.size == FHD


# --------------------------------------------------------------------- explicit picker


@pytest.mark.parametrize(
    ("discovered", "queried"),
    [
        (None, None),
        (reported(FHD, reliable=False), None),
        (None, reported(FHD, reliable=False)),
        (reported(reliable=True), None),
    ],
)
def test_unknown_or_unreliable_information_requires_an_explicit_picker(discovered, queried):
    result = resolve_resolution(record_set(), None, discovered, queried)
    assert result == ResolutionRequired(choices=(), remembered_rejected=False)


def test_an_unreliable_report_never_becomes_a_picker_choice_or_a_generic_default():
    result = resolve_resolution(record_set(), None, reported(FHD, reliable=False), None)
    assert isinstance(result, ResolutionRequired)
    # No supported-mode list can be shown from untrustworthy information, and no
    # generic 1080p fallback is substituted for the missing evidence.
    assert result.choices == ()
    assert FHD not in result.choices


@pytest.mark.parametrize(
    ("discovered", "queried"),
    [
        (
            reported(UHD, FHD, evidence="advertised-modes"),
            reported(HD, reliable=False, evidence="query_modes"),
        ),
        (
            reported(HD, reliable=False, evidence="advertised-modes"),
            reported(UHD, FHD, evidence="query_modes"),
        ),
    ],
)
def test_unreliable_secondary_modes_never_join_a_reliable_picker(discovered, queried):
    # A reliable multi-mode source and an unreliable secondary source can coexist.
    # Only the trustworthy source's modes may be offered, in either arrival order;
    # the untrusted HD must not become a user-confirmable picker option.
    result = resolve_resolution(record_set(), None, discovered, queried)
    assert result == ResolutionRequired(choices=(UHD, FHD), remembered_rejected=False)


def test_nonpositive_dimensions_cannot_become_a_decision_at_all():
    with pytest.raises(ValueError):
        Size(width=0, height=1080)
    with pytest.raises(ValueError):
        Size(width=1920, height=-1)


def test_an_explicit_positive_dimension_is_accepted_without_receiver_evidence():
    chosen = Size(width=2560, height=1440)
    candidate = record_set()
    result = resolve_resolution(candidate, None, None, None, user=chosen)
    assert result == ResolutionDecision(
        size=chosen,
        source="user",
        evidence=ResolutionEvidence(
            origin="picker",
            reliable=True,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def test_an_explicit_user_choice_supersedes_a_remembered_and_a_reported_mode():
    chosen = Size(width=2560, height=1440)
    result = resolve_resolution(record_set(), saved(), reported(FHD), None, user=chosen)
    assert isinstance(result, ResolutionDecision)
    assert result.size == chosen and result.source == "user"


def test_a_non_size_user_choice_is_rejected_rather_than_coerced():
    with pytest.raises(TypeError):
        resolve_resolution(  # type: ignore[arg-type]
            record_set(), None, None, None, user=(1920, 1080),
        )


def test_the_required_picker_has_no_default_and_validates_its_choices():
    with pytest.raises(TypeError):
        ResolutionRequired(choices=[FHD])  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ResolutionRequired(choices=((1920, 1080),))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ResolutionRequired(choices=(FHD, FHD))


def test_receiver_names_and_models_never_produce_a_resolution():
    named = record_set(
        records=(service(instance="Living Room 4K TV", model="AppleTV6,2"),),
    )
    result = resolve_resolution(named, None, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=False)


def test_a_reliable_query_that_excludes_the_remembered_size_requires_a_decision():
    candidate = record_set()
    stored = saved(remembered=decision(candidate, size=FHD, source="discovery", origin="discovery"))
    result = resolve_resolution(
        candidate, stored, reported(FHD, reliable=False), reported(HD),
    )
    # The unreliable discovery report cannot support the memory, and the
    # reliable query names a different mode: the user decides, the memory is
    # reported as rejected, and no silent substitution happens.
    assert result == ResolutionRequired(choices=(HD,), remembered_rejected=True)


def test_a_remembered_decision_with_unreliable_evidence_is_not_reused():
    candidate = record_set()
    untrusted = ResolutionDecision(
        size=FHD,
        source="discovery",
        evidence=ResolutionEvidence(
            origin="discovery",
            reliable=False,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )
    result = resolve_resolution(candidate, saved(remembered=untrusted), None, None)
    # Read-side trust is re-checked: an unreliable stored record is treated as
    # absent instead of being re-stamped reliable=True.
    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_an_unreliable_remembered_decision_is_ignored_not_laundered():
    candidate = record_set()
    untrusted = ResolutionDecision(
        size=UHD,
        source="user",
        evidence=ResolutionEvidence(
            origin="picker",
            reliable=False,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )
    result = resolve_resolution(candidate, saved(remembered=untrusted), reported(FHD), None)
    assert isinstance(result, ResolutionDecision)
    assert result.size == FHD and result.source == "discovery"
    assert result.evidence.origin == "discovery"


# ----------------------------------------------------------- identity continuity


def test_an_ambiguous_identity_never_inherits_a_remembered_mode():
    stored = saved(receiver_id=AMBIGUOUS, remembered=decision(record_set(), receiver_id=AMBIGUOUS))
    ambiguous = record_set(
        receiver_id=AMBIGUOUS,
        identity_kind="ambiguous",
        ambiguous=True,
        persistable=False,
    )
    assert PreferenceAccess.for_record_set(ambiguous, stored) is None
    result = resolve_resolution(ambiguous, stored, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_a_fallback_identity_without_verified_continuity_inherits_no_remembered_mode():
    stored = saved(
        receiver_id=FINGERPRINT, remembered=decision(record_set(), receiver_id=FINGERPRINT),
    )
    unproven = record_set(
        receiver_id=FINGERPRINT,
        identity_kind="fingerprint",
        continuity_verified=False,
    )
    result = resolve_resolution(unproven, stored, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_an_endpoint_replacement_is_a_decision_when_it_arrives_after_the_original():
    resolver = IdentityResolver()
    original = resolver.update((advertisement(),))[0]
    stored = saved_for(original)
    replacement = resolver.update((advertisement(addresses=("192.0.2.11",)),))[0]
    assert replacement.receiver_id == original.receiver_id
    assert replacement.replacement_of == original.receiver_id
    assert not replacement.continuity_verified
    assert PreferenceAccess.for_record_set(replacement, stored) is None

    result = resolve_resolution(replacement, stored, None, None)

    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_an_endpoint_replacement_is_a_decision_when_it_arrives_before_the_original():
    resolver = IdentityResolver()
    original = resolver.update((advertisement(),))[0]
    stored = saved_for(original)
    # A later process sees only the replacement advertisement. Its continuity
    # must be judged against the saved endpoint, not against an equal label hash.
    fresh = IdentityResolver()
    fresh.seed_saved((stored,))
    replacement = fresh.update((advertisement(addresses=("192.0.2.11",)),))[0]
    assert replacement.receiver_id == stored.receiver_id
    assert replacement.replacement_of == stored.receiver_id
    assert not replacement.continuity_verified

    result = resolve_resolution(replacement, stored, None, None)

    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_a_new_device_identifier_at_the_same_endpoint_is_a_new_identity():
    stored = saved()
    replacement = record_set(receiver_id=OTHER, replacement_of=stored.receiver_id)
    result = resolve_resolution(replacement, stored, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


def test_a_replacement_identity_can_still_use_its_own_reliable_advertised_mode():
    candidate = record_set(replacement_of=OTHER)
    result = resolve_resolution(candidate, saved(), reported(HD), None)
    assert isinstance(result, ResolutionDecision)
    assert result.size == HD
    assert result.source == "discovery"
    assert result.evidence.receiver_id == candidate.receiver_id


def test_a_replacement_identity_with_multiple_modes_reports_the_rejected_memory():
    candidate = record_set(replacement_of=OTHER)
    result = resolve_resolution(candidate, saved(), reported(HD, UHD), None)
    assert result == ResolutionRequired(choices=(HD, UHD), remembered_rejected=True)


def test_a_remembered_decision_that_names_another_receiver_is_not_reused():
    candidate = record_set()
    stored = saved(remembered=decision(candidate, receiver_id=OTHER))
    result = resolve_resolution(candidate, stored, None, None)
    assert result == ResolutionRequired(choices=(), remembered_rejected=True)


# ------------------------------------------------------ remembering after success


def test_a_verified_setup_returns_the_decision_to_remember():
    candidate = record_set()
    chosen = decision(candidate, size=UHD, source="user")
    remembered = remember_after_success(candidate, chosen, UHD)
    assert remembered == ResolutionDecision(
        size=UHD,
        source="user",
        evidence=ResolutionEvidence(
            origin="verified",
            reliable=True,
            receiver_id=candidate.receiver_id,
            endpoint_fingerprint=candidate.endpoint_fingerprint,
        ),
    )


def test_a_failed_setup_does_not_write_a_preference():
    candidate = record_set()
    assert remember_after_success(candidate, decision(candidate), None) is None


def test_a_non_size_observation_is_rejected_rather_than_read_as_success():
    candidate = record_set()
    with pytest.raises(TypeError):
        remember_after_success(  # type: ignore[arg-type]
            candidate, decision(candidate), (1920, 1080),
        )


def test_an_observed_size_that_differs_from_the_decision_is_not_remembered():
    candidate = record_set()
    assert remember_after_success(candidate, decision(candidate, size=FHD), HD) is None


def test_a_setup_that_observed_the_decided_size_is_remembered_across_an_endpoint_change():
    resolver = IdentityResolver()
    original = resolver.update((advertisement(device_id="00:11:22:33:44:55"),))[0]
    moved = resolver.update(
        (advertisement(device_id="001122334455", addresses=("192.0.2.11",)),)
    )[0]
    remembered = remember_after_success(moved, decision(original, size=FHD), FHD)
    assert remembered is not None
    assert remembered.evidence.endpoint_fingerprint == moved.endpoint_fingerprint

    reused = resolve_resolution(moved, SavedReceiver(
        receiver_id=moved.receiver_id,
        service_metadata=moved.records,
        display_name="Living Room",
        last_endpoint=resolved_target(moved),
        continuity_fingerprint=moved.endpoint_fingerprint,
        credential_ref=None,
        remembered_resolution=remembered,
    ), None, None)
    assert isinstance(reused, ResolutionDecision)
    assert reused.size == FHD and reused.source == "stored"


@pytest.mark.parametrize(
    "withheld",
    [
        {"ambiguous": True},
        {"identity_kind": "ambiguous"},
        {"persistable": False},
        {"replacement_of": OTHER},
        {"receiver_id": AMBIGUOUS},
    ],
)
def test_an_unpersistable_identity_is_never_remembered(withheld: dict[str, object]):
    candidate = record_set(**withheld)
    assert remember_after_success(candidate, decision(candidate), FHD) is None


def test_a_fallback_identity_is_remembered_only_after_verified_continuity():
    unproven = record_set(
        receiver_id=FINGERPRINT, identity_kind="fingerprint", continuity_verified=False,
    )
    assert remember_after_success(unproven, decision(unproven), FHD) is None

    proven = record_set(
        receiver_id=FINGERPRINT, identity_kind="fingerprint", continuity_verified=True,
    )
    assert remember_after_success(proven, decision(proven), FHD) is not None


def test_a_decision_for_another_receiver_is_never_remembered():
    candidate = record_set()
    foreign = decision(candidate, receiver_id=OTHER)
    assert remember_after_success(candidate, foreign, FHD) is None


def test_remembering_an_already_remembered_decision_is_idempotent():
    candidate = record_set()
    once = remember_after_success(candidate, decision(candidate), FHD)
    twice = remember_after_success(candidate, once, FHD)
    assert once is not None
    assert twice == once


# ---------------------------------------------------------------- accepted mode


def test_unreliable_accepted_size_cannot_reconfigure():
    decision_to_check = ResolutionDecision(
        size=Size(width=1920, height=1080),
        source="user",
        evidence=ResolutionEvidence(
            origin="picker",
            reliable=True,
            receiver_id=ReceiverId("airplay-device:a"),
            endpoint_fingerprint=None,
        ),
    )
    assert not accepted_mode_mismatch(
        decision_to_check, Size(width=3840, height=2160), False,
    )


def test_a_reliable_accepted_size_difference_is_a_mismatch():
    assert accepted_mode_mismatch(decision(record_set()), UHD, True) is True


def test_a_reliable_accepted_size_equal_to_the_decision_is_not_a_mismatch():
    assert accepted_mode_mismatch(decision(record_set()), FHD, True) is False


@pytest.mark.parametrize("source", ["stored", "discovery", "user"])
def test_a_reliable_accepted_difference_is_a_mismatch_for_every_decision_source(source: str):
    candidate = record_set()
    assert accepted_mode_mismatch(decision(candidate, source=source), HD, True) is True
    assert accepted_mode_mismatch(decision(candidate, source=source), FHD, True) is False


def test_the_accepted_mode_check_never_edits_the_decision_or_the_reports():
    candidate = record_set()
    original = decision(candidate)
    accepted = UHD
    assert accepted_mode_mismatch(original, accepted, True) is True
    # The decision and the accepted size are frozen DTOs and the check is a pure
    # predicate: policy reports the difference and lets the session transact.
    assert original.size == FHD and original.source == "user"
    assert accepted == UHD
