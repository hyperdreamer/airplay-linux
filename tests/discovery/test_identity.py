"""Conservative identity matching must not transfer preferences to a lookalike."""

import asyncio
from dataclasses import replace
from itertools import permutations

import pytest

from airplay_linux.discovery.identity import (
    IdentityResolver,
    normalize_device_id,
    target_for,
)
from airplay_linux.discovery.zeroconf_adapter import ServiceChange, ZeroconfDiscovery
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import OpaqueCredentialRef
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Ok,
    ResolutionDecision,
    ResolutionEvidence,
    Size,
)
from airplay_linux.storage import PreferenceAccess
from airplay_linux.testing.builders import service
from airplay_linux.testing.time import FakeClock


DEVICE = "airplay-device:001122334455"
OTHER_DEVICE = "airplay-device:66778899aabb"


def advertisement(kind="airplay", **overrides):
    return service(
        kind=kind,
        **{
            "instance": "Living Room",
            "model": "AppleTV6,2",
            "device_id": None,
            **overrides,
        },
    )


def resolved_target(record_set):
    result = target_for(record_set)
    assert isinstance(result, Ok)
    return result.value


def saved_receiver(record_set):
    return SavedReceiver(
        receiver_id=record_set.receiver_id,
        service_metadata=record_set.records,
        display_name="Living Room",
        last_endpoint=resolved_target(record_set),
        continuity_fingerprint=record_set.endpoint_fingerprint,
        credential_ref=OpaqueCredentialRef("keyring-reference-canary"),
        remembered_resolution=ResolutionDecision(
            size=Size(width=1920, height=1080),
            source="user",
            evidence=ResolutionEvidence(
                origin="user",
                reliable=True,
                receiver_id=record_set.receiver_id,
                endpoint_fingerprint=record_set.endpoint_fingerprint,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("00:11:22:AA:BB:CC", "001122aabbcc"),
        ("00-11-22-aa-bb-cc", "001122aabbcc"),
        ("0011.22AA.BBCC", "001122aabbcc"),
        (" 001122AABBCC ", "001122aabbcc"),
        ("Receiver-ONE", "receiver-one"),
    ],
)
def test_device_id_normalization(raw, expected):
    assert normalize_device_id(raw) == expected


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_device_ids_merge_across_services_and_prefer_airplay_port(reverse):
    records = (
        advertisement(device_id="00:11:22:33:44:55", port=7100),
        advertisement(
            "raop", device_id="001122334455", port=5000,
            instance="001122334455@Living Room._raop._tcp.local.",
        ),
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 1
    receiver = merged[0]
    assert receiver.receiver_id == DEVICE
    assert receiver.identity_kind == "device"
    assert receiver.persistable and not receiver.ambiguous
    assert len(receiver.records) == 2
    target = resolved_target(receiver)
    assert (target.canonical_host, target.service_port, target.protocol) == (
        "192.0.2.10", 7100, "airplay",
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_device_ids_never_merge(reverse):
    records = (
        service(device_id="00:11:22:33:44:55"),
        service(kind="raop", device_id="66:77:88:99:aa:bb"),
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 2
    assert {r.receiver_id for r in merged} == {DEVICE, OTHER_DEVICE}


@pytest.mark.parametrize("id_kind", [None, "airplay", "raop"])
@pytest.mark.parametrize("reverse", [False, True])
def test_one_id_and_no_id_fallbacks_merge_only_equal_label_model_address(id_kind, reverse):
    records = tuple(
        advertisement(kind, device_id="00:11:22:33:44:55" if id_kind == kind else None)
        for kind in ("airplay", "raop")
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 1
    assert len(merged[0].records) == 2
    assert merged[0].persistable and merged[0].continuity_verified
    if id_kind:
        assert merged[0].receiver_id == DEVICE
    else:
        # SHA-256 of the normalized "living room|appletv6,2" pair.
        assert merged[0].receiver_id == (
            "airplay-fingerprint:"
            "d534649870ea0a62b1da013c791804b97a19f772c0c0376bdeb1c58043c8ac82"
        )


@pytest.mark.parametrize("reverse", [False, True])
def test_labels_models_and_raop_prefixes_are_normalized(reverse):
    records = (
        advertisement(instance="  Cafe\u0301._airplay._tcp.local.  ", model="  APPLETV6,2 "),
        advertisement(
            "raop", instance="001122aAbBcC@CAFÉ._raop._tcp.local.",
            model="AppleTV6,2",
        ),
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 1
    assert len(merged[0].records) == 2


@pytest.mark.parametrize("prefix", ["not-a-mac", "00112233445", "00112233445g"])
def test_invalid_raop_mac_prefix_is_part_of_the_label(prefix):
    records = (
        advertisement(),
        advertisement("raop", instance=f"{prefix}@Living Room._raop._tcp.local."),
    )
    assert len(IdentityResolver().update(records)) == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"instance": "Other Room"},
        {"model": "OtherModel"},
        {"addresses": ("192.0.2.11",)},
    ],
)
def test_fallback_requires_equal_label_model_and_current_address(overrides):
    records = (advertisement(), advertisement("raop", **overrides))
    assert len(IdentityResolver().update(records)) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_empty_labels_use_normalized_host_model_without_cross_service_guessing(reverse):
    records = (
        advertisement(instance="._airplay._tcp.local.", host="BÜCHER.local."),
        advertisement("raop", instance="001122334455@._raop._tcp.local.", host="xn--bcher-kva.local"),
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 2
    assert all(r.ambiguous and not r.persistable for r in merged)
    single = IdentityResolver().update(records[:1])[0]
    assert single.identity_kind == "endpoint"
    assert single.receiver_id == (
        "airplay-endpoint:"
        "c62d21aefd8d61c03f900ad6d34186a67beeb47b2cd232446c7da6854390775a"
    )


def test_idless_record_cannot_bridge_conflicting_device_id_groups_in_any_permutation():
    records = (
        advertisement(device_id="00:11:22:33:44:55", key="airplay-one"),
        advertisement(device_id="66:77:88:99:aa:bb", key="airplay-two"),
        advertisement("raop"),
    )
    for ordered in permutations(records):
        merged = IdentityResolver().update(ordered)
        assert len(merged) == 3
        stable = [r for r in merged if r.identity_kind == "device"]
        assert {r.receiver_id for r in stable} == {DEVICE, OTHER_DEVICE}
        assert all(len(r.records) == 1 for r in stable)
        uncertain = [r for r in merged if r.identity_kind == "ambiguous"]
        assert len(uncertain) == 1
        assert uncertain[0].ambiguous and not uncertain[0].persistable
        assert isinstance(target_for(uncertain[0]), Err)


def test_multiple_fallback_matches_are_ambiguous_in_every_permutation():
    records = (
        advertisement(key="airplay-one"),
        advertisement(key="airplay-two"),
        advertisement("raop"),
    )
    classifications = []
    for ordered in permutations(records):
        merged = IdentityResolver().update(ordered)
        assert len(merged) == 3
        assert all(r.ambiguous and not r.persistable for r in merged)
        assert all(r.receiver_id.startswith("airplay-ambiguous:") for r in merged)
        classifications.append(tuple((r.receiver_id, r.records[0].key) for r in merged))
    assert all(value == classifications[0] for value in classifications)


def test_identical_labels_at_two_addresses_stay_distinct_and_nonpersistable():
    records = (
        advertisement(key="airplay-one"),
        advertisement(key="airplay-two", addresses=("192.0.2.11",)),
    )
    merged = IdentityResolver().update(records)
    assert len(merged) == 2
    assert len({r.receiver_id for r in merged}) == 2
    assert all(r.ambiguous and not r.persistable for r in merged)


@pytest.mark.parametrize("overrides", [{"seen_at": 2.0}, {"txt": {"model": "AppleTV6,2", "features": "1"}}])
def test_ephemeral_ambiguity_hash_covers_complete_advertisements(overrides):
    records = (
        advertisement(key="airplay-one"),
        advertisement(key="airplay-two", addresses=("192.0.2.11",)),
    )
    original = {r.records[0].key: r for r in IdentityResolver().update(records)}
    changed = {r.records[0].key: r for r in IdentityResolver().update((replace(records[0], **overrides), records[1]))}
    assert all(r.ambiguous and not r.persistable for r in changed.values())
    assert changed["airplay-one"].receiver_id != original["airplay-one"].receiver_id
    assert changed["airplay-two"].receiver_id == original["airplay-two"].receiver_id


@pytest.mark.parametrize("reverse", [False, True])
def test_staged_arrival_does_not_grant_saved_fallback_access_to_ambiguous_graph(reverse):
    original = IdentityResolver().update((advertisement(), advertisement("raop")))[0]
    saved = saved_receiver(original)
    records = (
        advertisement(),
        advertisement("raop", key="raop-one"),
        advertisement("raop", key="raop-two"),
    )
    ordered = records[::-1] if reverse else records
    resolver = IdentityResolver()
    resolver.seed_saved((saved,))
    resolver.update(ordered[:1])
    merged = resolver.update(ordered)
    assert len(merged) == 3
    assert all(r.ambiguous and not r.persistable and not r.continuity_verified for r in merged)


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (("2001:0db8:0000::2", "192.0.2.20", "192.0.2.10"), "192.0.2.10"),
        (("2001:0db8:0000::2", "2001:db8::1"), "2001:db8::1"),
        (("fe80:0000:0000:0000::2%eth1", "fe80::1%eth0"), "fe80::1%eth0"),
    ],
)
def test_address_selection_is_canonical_and_order_independent(addresses, expected):
    fingerprints = set()
    for ordered in permutations(addresses):
        receiver = IdentityResolver().update((advertisement(addresses=ordered),))[0]
        assert receiver.canonical_address == expected
        assert resolved_target(receiver).canonical_host == expected
        fingerprints.add(receiver.endpoint_fingerprint)
    assert len(fingerprints) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_cross_service_match_canonicalizes_equivalent_ipv6_addresses(reverse):
    records = (
        advertisement(addresses=("fe80:0000:0000::1%eth0",)),
        advertisement("raop", addresses=("fe80::1%eth0",)),
    )
    merged = IdentityResolver().update(records[::-1] if reverse else records)
    assert len(merged) == 1
    assert merged[0].canonical_address == "fe80::1%eth0"


def test_ipv6_scopes_are_distinct_endpoint_evidence():
    records = (
        advertisement(addresses=("fe80::1%eth0",)),
        advertisement("raop", addresses=("fe80::1%eth1",)),
    )
    merged = IdentityResolver().update(records)
    assert len(merged) == 2
    assert all(r.ambiguous for r in merged)


def test_port_changes_advance_generation_and_do_not_resurrect_old_targets():
    resolver = IdentityResolver()
    record = advertisement(device_id="00:11:22:33:44:55")
    first = resolver.update((record,))[0]
    second = resolver.update((replace(record, port=7100),))[0]
    third = resolver.update((record,))[0]
    assert first.receiver_id == second.receiver_id == third.receiver_id == DEVICE
    assert second.endpoint_generation > first.endpoint_generation
    assert third.endpoint_generation > second.endpoint_generation
    assert first.endpoint_fingerprint == third.endpoint_fingerprint
    assert first.endpoint_fingerprint != second.endpoint_fingerprint
    assert resolved_target(first) != resolved_target(third)


def test_same_address_hostname_and_dns_order_changes_preserve_continuity():
    resolver = IdentityResolver()
    record = advertisement(addresses=("192.0.2.10", "2001:db8::1"))
    first = resolver.update((record,))[0]
    changed = replace(record, host="renamed.local.", addresses=("2001:0db8::1", "192.0.2.10"))
    second = resolver.update((changed,))[0]
    assert first.receiver_id == second.receiver_id
    assert second.continuity_verified
    assert second.endpoint_generation == first.endpoint_generation
    assert second.endpoint_fingerprint == first.endpoint_fingerprint


def test_address_overlap_preserves_continuity_when_canonical_address_changes():
    resolver = IdentityResolver()
    record = advertisement(addresses=("192.0.2.20", "2001:db8::1"))
    first = resolver.update((record,))[0]
    second = resolver.update((replace(record, addresses=("192.0.2.10", "2001:db8::1")),))[0]
    assert second.receiver_id == first.receiver_id
    assert second.continuity_verified
    assert second.replacement_of is None
    assert second.endpoint_generation > first.endpoint_generation


def test_changed_address_fallback_is_replacement_even_with_same_label_hash():
    resolver = IdentityResolver()
    record = advertisement()
    first = resolver.update((record,))[0]
    changed = replace(record, addresses=("192.0.2.11",))
    second = resolver.update((changed,))[0]
    repeated = resolver.update((changed,))[0]
    returned = resolver.update((record,))[0]
    assert second.receiver_id == first.receiver_id
    assert second.replacement_of == first.receiver_id
    assert not second.continuity_verified
    assert not repeated.continuity_verified
    assert not returned.continuity_verified
    assert second.endpoint_generation > first.endpoint_generation
    assert returned.endpoint_generation > second.endpoint_generation


def test_new_device_id_is_a_new_receiver_and_never_inherits_fallback_continuity():
    resolver = IdentityResolver()
    record = advertisement(device_id="00:11:22:33:44:55")
    first = resolver.update((record,))[0]
    second = resolver.update((replace(record, device_id="66:77:88:99:aa:bb"),))[0]
    assert second.receiver_id == OTHER_DEVICE
    assert second.replacement_of == first.receiver_id
    assert not second.continuity_verified


def test_stable_device_id_survives_host_port_and_address_changes():
    resolver = IdentityResolver()
    record = advertisement(device_id="00:11:22:33:44:55")
    first = resolver.update((record,))[0]
    second = resolver.update((replace(record, host="new.local", addresses=("192.0.2.11",), port=7100),))[0]
    assert second.receiver_id == first.receiver_id
    assert second.continuity_verified
    assert second.replacement_of is None
    assert second.endpoint_generation > first.endpoint_generation


def test_protocol_changes_and_service_removal_fence_old_endpoints():
    resolver = IdentityResolver()
    airplay = advertisement(device_id="00:11:22:33:44:55", port=7000)
    raop = advertisement("raop", device_id="001122334455", port=5000)
    first = resolver.update((airplay, raop))[0]
    audio_only = resolver.update((raop,))[0]
    assert resolved_target(audio_only).protocol == "raop"
    assert audio_only.endpoint_generation > first.endpoint_generation
    assert resolver.update(()) == ()
    # Repeated absence must not reset the fence or manufacture new generations.
    assert resolver.update(()) == ()
    returned = resolver.update((airplay, raop))[0]
    assert returned.endpoint_generation > audio_only.endpoint_generation
    assert returned.endpoint_generation > first.endpoint_generation
    offline = replace(returned, records=())
    failure = target_for(offline)
    assert isinstance(failure, Err)
    assert failure.error.code is ErrorCode.stale_target


def test_observation_time_does_not_change_identity_or_endpoint_generation():
    clock = FakeClock()
    resolver = IdentityResolver()
    record = advertisement(seen_at=clock.now())
    first = resolver.update((record,))[0]
    clock.advance(30)
    second = resolver.update((replace(record, seen_at=clock.now()),))[0]
    assert first.receiver_id == second.receiver_id
    assert first.endpoint_generation == second.endpoint_generation


@pytest.mark.parametrize("id_kind", [None, "airplay"])
@pytest.mark.parametrize("reverse", [False, True])
def test_cold_start_seeding_accepts_only_compatible_endpoint_without_loading_secrets(id_kind, reverse):
    records = tuple(
        advertisement(kind, device_id="001122334455" if id_kind == kind else None)
        for kind in ("airplay", "raop")
    )
    original = IdentityResolver().update(records)[0]
    saved = saved_receiver(original)
    resolver = IdentityResolver()
    resolver.seed_saved((saved,))
    assert resolver.update(()) == ()
    merged = resolver.update(records[::-1] if reverse else records)
    assert len(merged) == 1
    assert merged[0].receiver_id == saved.receiver_id
    assert merged[0].continuity_verified
    assert "keyring-reference-canary" not in repr(resolver)
    assert resolved_target(merged[0]).endpoint_generation >= saved.last_endpoint.endpoint_generation


@pytest.mark.parametrize("reverse", [False, True])
def test_cold_start_changed_address_cannot_reuse_saved_fallback_preferences(reverse):
    records = (advertisement(), advertisement("raop"))
    original = IdentityResolver().update(records)[0]
    resolver = IdentityResolver()
    resolver.seed_saved((saved_receiver(original),))
    changed = tuple(replace(record, addresses=("192.0.2.11",)) for record in records)
    ordered = changed[::-1] if reverse else changed
    first = resolver.update(ordered[:1])[0]
    assert not first.continuity_verified
    merged = resolver.update(ordered)[0]
    assert not merged.continuity_verified
    assert merged.replacement_of == original.receiver_id
    assert merged.endpoint_generation > original.endpoint_generation


def test_seeded_fallback_port_mismatch_and_ambiguous_evidence_are_rejected():
    original = IdentityResolver().update((advertisement(),))[0]
    saved = saved_receiver(original)
    resolver = IdentityResolver()
    resolver.seed_saved((saved,))
    changed = resolver.update((advertisement(port=7100),))[0]
    assert not changed.continuity_verified
    assert changed.replacement_of == original.receiver_id

    ambiguous = replace(saved, receiver_id=ReceiverId("airplay-ambiguous:unsafe"))
    resolver = IdentityResolver()
    resolver.seed_saved((ambiguous,))
    current = resolver.update((advertisement(),))[0]
    assert current.replacement_of is None
    assert current.receiver_id == original.receiver_id


@pytest.mark.parametrize("reverse", [False, True])
def test_cold_start_exact_fallback_survives_either_service_arriving_first(reverse):
    records = (advertisement(port=7000), advertisement("raop", port=5000))
    original = IdentityResolver().update(records)[0]
    resolver = IdentityResolver()
    resolver.seed_saved((saved_receiver(original),))
    ordered = records[::-1] if reverse else records
    first = resolver.update(ordered[:1])[0]
    if reverse:
        assert not first.continuity_verified
    complete = resolver.update(ordered)[0]
    assert complete.continuity_verified
    assert complete.replacement_of is None
    assert resolved_target(complete).protocol == "airplay"


@pytest.mark.parametrize("broken", ["missing_endpoint", "fingerprint", "metadata", "duplicate"])
def test_incomplete_or_conflicting_saved_evidence_cannot_grant_fallback_preferences(broken):
    original = IdentityResolver().update((advertisement(),))[0]
    saved = saved_receiver(original)
    if broken == "missing_endpoint":
        saved = replace(saved, last_endpoint=None)
    elif broken == "fingerprint":
        saved = replace(saved, continuity_fingerprint="unverified-endpoint")
    elif broken == "metadata":
        saved = replace(saved, service_metadata=(advertisement(instance="Other Room"),))
    resolver = IdentityResolver()
    resolver.seed_saved((saved, saved) if broken == "duplicate" else (saved,))
    current = resolver.update((advertisement(),))[0]
    repeated = resolver.update((advertisement(),))[0]
    assert current.receiver_id == original.receiver_id
    assert not current.continuity_verified
    assert not repeated.continuity_verified


def test_no_address_is_not_a_live_target_and_never_persists():
    receiver = IdentityResolver().update((advertisement(addresses=()),))[0]
    assert not receiver.persistable
    failure = target_for(receiver)
    assert isinstance(failure, Err)
    assert failure.error.code is ErrorCode.stale_target


@pytest.mark.parametrize("broken", ["fingerprint", "metadata", "duplicate"])
def test_rejected_saved_evidence_still_fences_the_saved_target(broken):
    records = (
        advertisement(device_id="00:11:22:33:44:55", port=7100),
        advertisement("raop", device_id="001122334455", port=5000),
    )
    original = IdentityResolver().update(records)[0]
    saved = saved_receiver(original)
    if broken == "fingerprint":
        saved = replace(saved, continuity_fingerprint="unverified-endpoint")
    elif broken == "metadata":
        saved = replace(saved, service_metadata=(advertisement(instance="Other Room"),))
    seeded = (saved, saved) if broken == "duplicate" else (saved,)
    resolver = IdentityResolver()
    resolver.seed_saved(seeded)
    restored = resolver.update(records)[0]
    assert restored.receiver_id == saved.receiver_id
    # Rejected evidence authorizes nothing, but it still describes an endpoint
    # this resolver has already issued: that endpoint may never be reissued.
    assert not restored.continuity_verified
    assert restored.endpoint_generation > saved.last_endpoint.endpoint_generation
    assert resolved_target(restored) != saved.last_endpoint
    moved = resolver.update(tuple(replace(record, port=record.port + 100) for record in records))[0]
    assert moved.endpoint_generation > restored.endpoint_generation
    assert resolved_target(moved) != saved.last_endpoint
    returned = resolver.update(records)[0]
    assert returned.endpoint_generation > moved.endpoint_generation
    assert resolved_target(returned) != saved.last_endpoint


def test_duplicate_saved_evidence_fences_the_highest_saved_generation():
    original = IdentityResolver().update((advertisement(),))[0]
    saved = saved_receiver(original)
    highest = replace(saved, last_endpoint=replace(saved.last_endpoint, endpoint_generation=4))
    resolver = IdentityResolver()
    resolver.seed_saved((saved, highest))
    restored = resolver.update((advertisement(),))[0]
    assert not restored.continuity_verified
    assert restored.endpoint_generation > highest.last_endpoint.endpoint_generation
    assert resolved_target(restored) != highest.last_endpoint


DEVICE_FIELDS = {"device_id": "00:11:22:33:44:55"}


def companion(model=""):
    return advertisement(
        "raop", model=model, port=5000,
        instance="001122334455@Living Room._raop._tcp.local.", **DEVICE_FIELDS,
    )


@pytest.mark.parametrize("order", ["added", "first", "removed"])
def test_companion_without_model_keeps_stable_device_continuity(order):
    airplay = advertisement(port=7100, **DEVICE_FIELDS)
    audio = companion()
    before, after = {
        "added": ((airplay,), (airplay, audio)),
        "first": ((audio,), (audio, airplay)),
        "removed": ((airplay, audio), (airplay,)),
    }[order]
    resolver = IdentityResolver()
    first = resolver.update(before)[0]
    saved = saved_receiver(first)
    second = resolver.update(after)[0]
    assert second.receiver_id == first.receiver_id == DEVICE
    # A companion that omits model metadata is missing evidence, not a conflict.
    assert second.continuity_verified
    assert second.replacement_of is None
    assert PreferenceAccess.for_record_set(second, saved) is saved


def test_conflicting_nonempty_companion_model_still_marks_a_replacement():
    airplay = advertisement(port=7100, **DEVICE_FIELDS)
    conflicting = companion(model="OtherModel")
    resolver = IdentityResolver()
    first = resolver.update((airplay,))[0]
    second = resolver.update((airplay, conflicting))[0]
    assert second.receiver_id == first.receiver_id == DEVICE
    assert not second.continuity_verified
    assert second.replacement_of == first.receiver_id


def _three_snapshots():
    """One stable device: model-less RAOP, then AirPlay names a model, then another."""
    audio = companion(model="")
    airplay = advertisement(port=7100, **DEVICE_FIELDS)
    renamed = replace(airplay, model="OtherModel")
    return audio, airplay, renamed


def test_the_strongest_positive_model_evidence_anchors_continuity():
    audio, airplay, renamed = _three_snapshots()
    resolver = IdentityResolver()
    first = resolver.update((audio,))[0]
    second = resolver.update((audio, airplay))[0]
    saved = saved_receiver(second)
    third = resolver.update((audio, renamed))[0]
    assert first.receiver_id == second.receiver_id == third.receiver_id == DEVICE
    # Missing companion metadata is missing evidence: it cannot contradict an
    # identity whose positive model evidence has not changed.
    assert second.continuity_verified
    assert second.replacement_of is None
    assert PreferenceAccess.for_record_set(second, saved) is saved
    # The original model-less anchor must not make every later model accepted.
    assert not third.continuity_verified
    assert third.replacement_of == second.receiver_id
    assert PreferenceAccess.for_record_set(third, saved) is None


def test_the_retained_model_evidence_is_the_anchor_not_the_latest_snapshot():
    audio, airplay, renamed = _three_snapshots()
    resolver = IdentityResolver()
    resolver.update((audio,))
    resolver.update((audio, airplay))
    conflicting = resolver.update((audio, renamed))[0]
    # Neither missing metadata nor repeating the conflicting value silently
    # promotes the newcomer to the anchor.
    missing = resolver.update((audio, replace(airplay, model="")))[0]
    repeated = resolver.update((audio, renamed))[0]
    # Re-advertising the strongest evidence ever observed is continuity again.
    restored = resolver.update((audio, airplay))[0]
    assert conflicting.receiver_id == missing.receiver_id == DEVICE
    assert not conflicting.continuity_verified
    assert not repeated.continuity_verified
    assert repeated.replacement_of == DEVICE
    assert restored.continuity_verified
    assert restored.replacement_of is None


# ------------------------------------------------------------------ adapter path


class _Subscription:
    """One inert browser handle for the in-memory client below."""

    def close(self):
        pass


class _Mdns:
    """A ``MdnsClient`` that only answers what the test scripted."""

    def __init__(self):
        self.callback = None
        self.answers = {}

    async def browse(self, types, callback):
        self.callback = callback
        return Ok(value=_Subscription())

    async def resolve(self, key):
        queued = self.answers.get(key)
        if not queued:
            return Err(error=diagnostic(ErrorCode.discovery_failed))
        return queued.pop(0)

    async def refresh(self):
        return Ok(value=None)

    async def close(self):
        return None


async def _deliver(mdns, record):
    mdns.answers.setdefault(record.key, []).append(Ok(value=record))
    mdns.callback(ServiceChange(
        service_type=record.service_type, key=record.key, state="added",
    ))
    for _ in range(3):
        await asyncio.sleep(0)


def test_the_adapter_also_withholds_continuity_after_model_evidence_conflicts():
    audio, airplay, renamed = _three_snapshots()

    async def scenario():
        mdns = _Mdns()
        adapter = ZeroconfDiscovery(IdentityResolver(), FakeClock(1.0), mdns)
        assert isinstance(await adapter.start(), Ok)
        await _deliver(mdns, audio)
        await _deliver(mdns, airplay)
        second = adapter.current(ReceiverId(DEVICE))
        await _deliver(mdns, renamed)
        third = adapter.current(ReceiverId(DEVICE))
        await adapter.close()
        return second, third

    second, third = asyncio.run(scenario())
    assert second is not None and third is not None
    assert second.record_set.continuity_verified
    assert second.record_set.replacement_of is None
    saved = saved_receiver(second.record_set)
    assert PreferenceAccess.for_record_set(second.record_set, saved) is saved
    assert not third.record_set.continuity_verified
    assert third.record_set.replacement_of == second.record_set.receiver_id
    assert PreferenceAccess.for_record_set(third.record_set, saved) is None


def test_saved_model_evidence_is_the_anchor_for_the_first_live_snapshot():
    original = advertisement(**DEVICE_FIELDS, model="AppleTV6,2")
    saved = saved_receiver(IdentityResolver().update((original,))[0])

    def restored(record):
        resolver = IdentityResolver()
        resolver.seed_saved((saved,))
        return resolver.update((record,))[0]

    # The saved snapshot named a model, so a conflicting live model is a
    # replacement even though no live snapshot has been accepted yet.
    conflicting = restored(replace(original, model="OtherModel"))
    assert conflicting.continuity_verified is False
    assert conflicting.replacement_of == saved.receiver_id
    # A live snapshot that omits the model is still missing evidence, not a
    # conflict with the saved one.
    missing = restored(replace(original, model=""))
    assert missing.continuity_verified
    assert missing.replacement_of is None
    assert PreferenceAccess.for_record_set(missing, saved) is saved
    # The saved model itself remains continuity.
    same = restored(original)
    assert same.continuity_verified
    assert PreferenceAccess.for_record_set(same, saved) is saved


def _identity_models(resolver, records):
    """Return (continuity_verified, replacement_of) for the single identity."""
    result = resolver.update(records)
    assert len(result) == 1, result
    only = result[0]
    return only.continuity_verified, only.replacement_of


def test_a_snapshot_contradicting_itself_never_authorizes_continuity():
    """Two of one snapshot's records naming different models is not evidence.

    Retaining both would make the contradiction agree with itself on every later
    snapshot, so a receiver could inherit the previous receiver's trust.
    """
    base = service(kind="raop", device_id="00:11:22:33:44:55", model="")
    other = service(kind="raop", device_id="00:11:22:33:44:55", model="OtherModel")
    apple = service(kind="airplay", device_id="00:11:22:33:44:55", model="AppleTV6,2")

    resolver = IdentityResolver()
    resolver.update((base,))
    verified, replacement = _identity_models(resolver, (other, apple))
    assert verified is False
    assert replacement == "airplay-device:001122334455"
    # Repetition must not launder the contradiction into agreement.
    verified, _ = _identity_models(resolver, (other, apple))
    assert verified is False


def test_a_contradictory_first_snapshot_is_never_authorized():
    other = service(kind="raop", device_id="00:11:22:33:44:55", model="OtherModel")
    apple = service(kind="airplay", device_id="00:11:22:33:44:55", model="AppleTV6,2")
    resolver = IdentityResolver()
    verified, replacement = _identity_models(resolver, (other, apple))
    assert verified is False
    assert replacement == "airplay-device:001122334455"


def test_model_evidence_survives_a_temporarily_unusable_endpoint():
    """An address-less snapshot must not erase the model this identity advertised.

    Retention otherwise depended on ``verified``, which also requires a currently
    usable endpoint; forgetting the model there let a later conflicting model pass.
    """
    base = service(kind="raop", device_id="00:11:22:33:44:55", model="")
    addr_less = service(
        kind="airplay", device_id="00:11:22:33:44:55", model="AppleTV6,2", addresses=(),
    )
    conflicting = service(kind="airplay", device_id="00:11:22:33:44:55", model="OtherModel")
    same = service(kind="airplay", device_id="00:11:22:33:44:55", model="AppleTV6,2")

    resolver = IdentityResolver()
    resolver.update((base,))
    resolver.update((base, addr_less))
    verified, replacement = _identity_models(resolver, (base, conflicting))
    assert verified is False
    assert replacement == "airplay-device:001122334455"

    # The legitimate case still verifies: the restored endpoint names the same model.
    resolver = IdentityResolver()
    resolver.update((base,))
    resolver.update((base, addr_less))
    verified, replacement = _identity_models(resolver, (base, same))
    assert verified is True
    assert replacement is None
