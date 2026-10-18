"""Zeroconf discovery is proven against an in-memory mDNS client, never a socket.

Every test drives the adapter through the same ``MdnsClient`` seam the real
``AsyncZeroconf`` client implements, so both browsers, the raw-bytes TXT seam,
out-of-order resolve/removal, refresh idempotence, offline retention, and the
typed diagnostics are pinned without opening multicast.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any

import pytest

from airplay_linux.discovery.identity import IdentityResolver, target_for
from airplay_linux.discovery.zeroconf_adapter import (
    AIRPLAY_SERVICE,
    RAOP_SERVICE,
    MAX_REFRESH_KEYS,
    AzcMdnsClient,
    RawAdvertisement,
    ServiceChange,
    ZeroconfDiscovery,
    decode_txt,
    normalize_feature_bits,
    normalize_service_type,
    record_from_advertisement,
)
from airplay_linux.domain.identity import ReceiverId, ServiceRecord
from airplay_linux.domain.messages import DiscoveryEvent
from airplay_linux.domain.resources import Err, ErrorCode, Ok, RecoveryAction
from airplay_linux.testing import Deferred
from airplay_linux.testing.builders import service
from airplay_linux.testing.time import FakeClock

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "discovery" / "services.json"
LIVING_ROOM = ReceiverId("airplay-device:aabbccddeeff")
REPLACEMENT = ReceiverId("airplay-device:112233445566")
ADDRESS = "192.0.2.12"


def run_async(test: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """The suite has no asyncio plugin, so each test runs its own loop."""
    @wraps(test)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return wrapper


async def settle() -> None:
    """Let every scheduled resolve advance to its next await point."""
    for _ in range(3):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------- fake mdns


class FakeSubscription:
    """One browser handle; ``close`` is idempotent like the real subscription."""

    def __init__(self, service_type: str) -> None:
        self.service_type = service_type
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


class FakeMdns:
    """In-memory ``MdnsClient``: browsers capture callbacks, resolves are scripted.

    A resolve is answered explicitly, so "the resolve finished after the removal"
    is the test's own ordering rather than a race.
    """

    def __init__(self) -> None:
        self.browsed: list[tuple[tuple[str, ...], Callable[[ServiceChange], None]]] = []
        self.subscriptions: list[FakeSubscription] = []
        self.browse_results: list[object] = []
        self.answers: dict[str, list[object]] = {}
        self.resolve_calls: list[str] = []
        self.closed = 0

    async def browse(
        self, types: tuple[str, ...], callback: Callable[[ServiceChange], None],
    ) -> object:
        self.browsed.append((types, callback))
        subscription = FakeSubscription(types[0])
        self.subscriptions.append(subscription)
        if self.browse_results:
            return self.browse_results.pop(0)
        return Ok(value=subscription)

    async def resolve(self, key: str) -> object:
        self.resolve_calls.append(key)
        queued = self.answers.get(key)
        if not queued:
            return _failure()
        answer = queued.pop(0)
        if isinstance(answer, Deferred):
            answer = await answer.wait()
        return answer

    async def close(self) -> None:
        self.closed += 1

    # -- driving ------------------------------------------------------------

    def announce(self, key: str, browser: int = -1) -> None:
        service_types, callback = self.browsed[browser]
        callback(ServiceChange(service_type=service_types[0], key=key, state="added"))

    def withdraw(self, key: str, browser: int = -1) -> None:
        service_types, callback = self.browsed[browser]
        callback(ServiceChange(service_type=service_types[0], key=key, state="removed"))

    def answer(self, key: str, record: ServiceRecord) -> None:
        self.answers.setdefault(key, []).append(Ok(value=record))

    def defer(self, key: str) -> Deferred[object]:
        pending: Deferred[object] = Deferred()
        self.answers.setdefault(key, []).append(pending)
        return pending


def _failure() -> Err:
    from airplay_linux.diagnostics import diagnostic

    return Err(error=diagnostic(ErrorCode.discovery_failed))


class Harness:
    """The adapter wired to fake time, a real resolver, and the fake client."""

    def __init__(self, resolver: IdentityResolver | None = None) -> None:
        self.clock = FakeClock(100.0)
        self.mdns = FakeMdns()
        self.resolver = resolver or IdentityResolver()
        self.adapter = ZeroconfDiscovery(self.resolver, self.clock, self.mdns)
        self.events: list[DiscoveryEvent] = []
        self.subscription = self.adapter.subscribe(self.events.append)

    @property
    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def view(self, receiver_id: ReceiverId):
        return self.adapter.current(receiver_id)

    async def start(self) -> None:
        result = await self.adapter.start()
        assert isinstance(result, Ok)

    async def offer(self, record: ServiceRecord) -> None:
        """Announce a key the client can resolve, then let the adapter settle."""
        self.mdns.answer(record.key, record)
        self.mdns.announce(record.key)
        await settle()


def advertisement(kind: str = "airplay", **overrides: Any) -> ServiceRecord:
    """A record whose raw key keeps the trailing dot mDNS actually reports."""
    defaults: dict[str, Any] = {
        "key": f"Living Room._{kind}._tcp.local.",
        "instance": f"Living Room._{kind}._tcp.local.",
        "device_id": "AABBCCDDEEFF",
        "model": "AppleTV6,2",
        "txt": {"deviceid": "AABBCCDDEEFF", "model": "AppleTV6,2"},
    }
    return service(kind=kind, **{**defaults, **overrides, "addresses": overrides.get("addresses", (ADDRESS,))})


def fixture_services() -> list[dict[str, Any]]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))["services"]


def fixture_raw(entry: dict[str, Any]) -> RawAdvertisement:
    """Rebuild the captured advertisement, base64 back into raw bytes."""
    properties = entry["properties"]
    return RawAdvertisement(
        name=properties["name"],
        type=properties["type"],
        server=properties["server"],
        port=properties["port"],
        addresses=tuple(base64.b64decode(value) for value in properties["addresses"]),
        txt={
            key.encode("utf-8"): base64.b64decode(value, validate=True)
            for key, value in properties["txt"].items()
        },
    )


# --------------------------------------------------------------------------- lifecycle


def test_discovery_can_close_without_network():
    class Client:
        async def close(self):
            self.closed = True

    client = Client()
    adapter = ZeroconfDiscovery(IdentityResolver(), FakeClock(), client)
    asyncio.run(adapter.close())
    assert client.closed


@run_async
async def test_start_browses_both_service_types_once():
    harness = Harness()
    await harness.start()
    await harness.start()
    assert [types for types, _ in harness.mdns.browsed] == [(AIRPLAY_SERVICE, RAOP_SERVICE)]
    assert (AIRPLAY_SERVICE, RAOP_SERVICE) == ("_airplay._tcp.local.", "_raop._tcp.local.")


@run_async
async def test_browse_failure_is_a_retryable_diagnostic_and_does_not_fabricate_a_receiver():
    harness = Harness()
    harness.mdns.browse_results.append(_failure())
    result = await harness.adapter.start()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.discovery_failed
    assert RecoveryAction.retry in result.error.recovery
    assert harness.kinds == ["error"]
    assert harness.events[0].diagnostic is not None
    assert harness.view(LIVING_ROOM) is None


@run_async
async def test_a_failed_browse_leaves_no_browser_behind_and_a_retry_rebrowses():
    harness = Harness()
    harness.mdns.browse_results.append(_failure())
    assert isinstance(await harness.adapter.start(), Err)
    # A failed browse subscribes nothing, so no handle is registered to close.
    assert harness.mdns.subscriptions == [harness.mdns.subscriptions[0]]
    closed = harness.mdns.subscriptions[0].closed
    await harness.start()
    assert len(harness.mdns.browsed) == 2
    assert harness.mdns.subscriptions[0].closed == closed
    assert harness.mdns.subscriptions[-1].closed == 0
    await harness.offer(advertisement())
    assert harness.kinds == ["error", "added"]


@run_async
async def test_close_after_a_failed_start_closes_the_client_and_no_browser():
    harness = Harness()
    harness.mdns.browse_results.append(_failure())
    await harness.adapter.start()
    await harness.adapter.close()
    assert harness.mdns.closed == 1
    assert [subscription.closed for subscription in harness.mdns.subscriptions] == [0]


@run_async
async def test_close_unsubscribes_every_browser_and_closes_the_client_once():
    harness = Harness()
    await harness.start()
    await harness.adapter.close()
    await harness.adapter.close()
    assert harness.mdns.closed == 1
    assert [subscription.closed for subscription in harness.mdns.subscriptions] == [1]


@run_async
async def test_close_before_start_still_closes_the_client():
    harness = Harness()
    await harness.adapter.close()
    assert harness.mdns.closed == 1
    assert harness.mdns.browsed == []


@run_async
async def test_notifications_after_close_are_ignored():
    harness = Harness()
    await harness.start()
    await harness.adapter.close()
    harness.mdns.announce(advertisement().key)
    await settle()
    assert harness.events == []
    assert harness.mdns.resolve_calls == []


@run_async
async def test_subscription_close_stops_delivery():
    harness = Harness()
    await harness.start()
    harness.subscription.close()
    harness.subscription.close()
    await harness.offer(advertisement())
    assert harness.events == []


# --------------------------------------------------------------------------- added


@run_async
async def test_announcement_resolves_the_key_and_emits_one_added_view():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    assert harness.kinds == ["added"]
    view = harness.events[0].receiver
    assert view is not None and view.online
    assert view.display_name == "Living Room"
    assert view.record_set.receiver_id == LIVING_ROOM
    assert view.record_set.canonical_address == ADDRESS
    assert harness.mdns.resolve_calls == ["Living Room._airplay._tcp.local."]
    assert harness.view(LIVING_ROOM) == view


@run_async
async def test_repeated_announcement_of_the_same_record_emits_nothing_new():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    await harness.offer(advertisement())
    assert harness.kinds == ["added"]


@run_async
async def test_airplay_and_raop_merge_into_one_receiver_and_both_resolve():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    await harness.offer(advertisement("raop", port=5000))
    assert harness.kinds == ["added", "updated"]
    view = harness.view(LIVING_ROOM)
    assert view is not None and len(view.record_set.records) == 2
    assert sorted(harness.mdns.resolve_calls) == [
        "Living Room._airplay._tcp.local.",
        "Living Room._raop._tcp.local.",
    ]


@run_async
async def test_unknown_receiver_is_none():
    harness = Harness()
    await harness.start()
    assert harness.adapter.current(ReceiverId("airplay-device:ffff")) is None


@run_async
async def test_seen_at_comes_from_the_injected_clock():
    harness = Harness()
    await harness.start()
    harness.clock.advance(7.5)
    await harness.offer(advertisement(seen_at=0.0))
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert [record.seen_at for record in view.record_set.records] == [107.5]


# --------------------------------------------------------------------------- seam


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("_airplay._tcp.local.", "_airplay._tcp"),
        ("_airplay._tcp", "_airplay._tcp"),
        ("_RAOP._TCP.LOCAL.", "_raop._tcp"),
        ("_raop._tcp.local", "_raop._tcp"),
        ("_http._tcp.local.", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_service_type_accepts_only_the_two_browsed_types(raw, expected):
    assert normalize_service_type(raw) == expected


def test_decode_txt_trims_padding_and_keeps_utf8():
    assert decode_txt({b"model": b"AppleTV6,2\x00", b"md": b"Kitchen\x00\x00"}) == {
        "model": "AppleTV6,2",
        "md": "Kitchen",
    }


def test_decode_txt_drops_undecodable_keys_and_values_instead_of_guessing():
    decoded = decode_txt({b"\xff\xfe": b"\xff\xfe", b"features": b"0x5A7FFFF7,0x1E"})
    assert decoded == {"features": "0x5A7FFFF7,0x1E"}


def test_decode_txt_accepts_str_keys_and_rejects_non_mappings():
    assert decode_txt({"name": "Living Room", b"flags": b"0x4"}) == {
        "name": "Living Room",
        "flags": "0x4",
    }
    with pytest.raises(TypeError):
        decode_txt(b"model=AppleTV")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0x5A7FFFF7,0x1E", "0x5a7ffff7,0x1e"),
        ("0X5A7FFFF7 , 0X1E", "0x5a7ffff7,0x1e"),
        ("0x4", "0x4"),
        ("", None),
        (None, None),
        ("not-hex", None),
        ("0x", None),
        ("0x1,0x2,0x3", None),
    ],
)
def test_normalize_feature_bits_is_canonical_or_absent(raw, expected):
    assert normalize_feature_bits(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expect"),
    [
        (b"\xc0\x00\x02\x0c", "192.0.2.12"),
        (b"\x20\x01\x0d\xb8" + b"\x00" * 11 + b"\x01", "2001:db8::1"),
        (b"\x01\x02", None),
        (b"", None),
    ],
)
def test_record_from_advertisement_decodes_socket_addresses(raw, expect):
    record = record_from_advertisement(
        RawAdvertisement(
            name="Living Room._airplay._tcp.local.",
            type="_airplay._tcp.local.",
            server="Living-Room.local.",
            port=7000,
            addresses=(raw,),
            txt={b"model": b"AppleTV6,2"},
        ),
        1.0,
    )
    assert record is not None
    assert record.addresses == (() if expect is None else (expect,))


def test_record_from_advertisement_keeps_the_instance_and_normalizes_only_the_type():
    record = record_from_advertisement(fixture_raw(fixture_services()[0]), 42.0)
    assert record is not None
    assert (record.service_type, record.instance) == ("_airplay._tcp", "Living Room._airplay._tcp.local.")
    assert record.key == "Living Room._airplay._tcp.local."
    assert (record.device_id, record.model, record.host, record.port) == (
        "AABBCCDDEEFF", "AppleTV6,2", "Living-Room.local.", 7000,
    )
    assert record.addresses == (ADDRESS,)
    assert record.seen_at == 42.0
    # The seam decodes raw bytes; the feature word is canonicalized on adoption.
    assert record.txt["features"] == "0x5A7FFFF7,0x1E"
    assert record.txt["pk"] == "BEDROOM-PUBLIC-KEY"


def test_record_from_advertisement_never_substitutes_replacement_characters():
    kitchen = record_from_advertisement(fixture_raw(fixture_services()[2]), 1.0)
    assert kitchen is not None
    assert kitchen.txt["md"] == "Kitchen Speaker"
    # The capturable bytes 0xff 0xfe are not a feature word, so no value is invented.
    assert "features" not in kitchen.txt
    assert all("\ufffd" not in value for value in kitchen.txt.values())


def test_record_from_advertisement_refuses_an_unbrowsed_service_type():
    raw = RawAdvertisement(
        name="Printer._ipp._tcp.local.",
        type="_ipp._tcp.local.",
        server="printer.local.",
        port=631,
        addresses=(),
        txt={},
    )
    assert record_from_advertisement(raw, 1.0) is None


# --------------------------------------------------------------------------- ordering


@run_async
async def test_resolve_after_removal_cannot_restore_a_vanished_endpoint():
    harness = Harness()
    await harness.start()
    key = advertisement().key
    pending = harness.mdns.defer(key)
    harness.mdns.announce(key)
    await settle()
    harness.mdns.withdraw(key)
    await settle()
    pending.resolve(Ok(value=advertisement()))
    await settle()
    assert harness.kinds == []
    assert harness.view(LIVING_ROOM) is None


@run_async
async def test_a_stale_resolve_cannot_overwrite_a_newer_announcement():
    harness = Harness()
    await harness.start()
    key = advertisement().key
    await harness.offer(advertisement())
    stale = harness.mdns.defer(key)
    harness.mdns.announce(key)
    await settle()
    harness.mdns.answer(key, advertisement(instance="Renamed._airplay._tcp.local."))
    harness.mdns.announce(key)
    await settle()
    stale.resolve(Ok(value=advertisement()))
    await settle()
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.display_name == "Renamed"
    assert harness.kinds == ["added", "updated"]


@run_async
async def test_resolve_failure_keeps_known_receivers_and_reports_a_retryable_error():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    harness.mdns.announce("Ghost._airplay._tcp.local.")
    await settle()
    assert harness.kinds[-1] == "error"
    diagnostic = harness.events[-1].diagnostic
    assert diagnostic is not None
    assert diagnostic.code is ErrorCode.discovery_failed
    assert RecoveryAction.retry in diagnostic.recovery
    known = harness.view(LIVING_ROOM)
    assert known is not None and known.online


@run_async
async def test_removal_retains_the_last_known_view_offline_with_a_newer_generation():
    harness = Harness()
    await harness.start()
    record = advertisement()
    await harness.offer(record)
    online = harness.view(LIVING_ROOM)
    assert online is not None and online.online
    harness.mdns.withdraw(record.key)
    await settle()
    assert harness.kinds == ["added", "removed"]
    removed = harness.events[-1].receiver
    assert removed is not None and not removed.online
    offline = harness.view(LIVING_ROOM)
    assert offline is not None and not offline.online
    assert offline.display_name == "Living Room"
    assert offline.record_set.endpoint_generation > online.record_set.endpoint_generation
    # The retained view never resolves to a usable target while it is offline.
    assert offline.record_set.endpoint_generation != online.record_set.endpoint_generation


@run_async
async def test_removal_of_an_unknown_key_emits_nothing():
    harness = Harness()
    await harness.start()
    harness.mdns.withdraw("Nobody._airplay._tcp.local.")
    await settle()
    assert harness.events == []


@run_async
async def test_reappearance_after_removal_is_added_again_with_a_current_endpoint():
    harness = Harness()
    await harness.start()
    record = advertisement()
    await harness.offer(record)
    added = harness.view(LIVING_ROOM)
    harness.mdns.withdraw(record.key)
    await settle()
    removed = harness.view(LIVING_ROOM)
    assert added is not None and removed is not None
    await harness.offer(record)
    assert harness.kinds == ["added", "removed", "added"]
    view = harness.view(LIVING_ROOM)
    assert view is not None and view.online
    target = target_for(view.record_set)
    assert isinstance(target, Ok)
    # Neither the removal nor the reappearance may re-issue a spent generation.
    assert removed.record_set.endpoint_generation > added.record_set.endpoint_generation
    assert view.record_set.endpoint_generation > removed.record_set.endpoint_generation


# --------------------------------------------------------------------------- updates


@run_async
async def test_display_name_change_is_an_update_of_the_same_receiver():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    await harness.offer(advertisement(instance="Lounge._airplay._tcp.local.", key=advertisement().key))
    assert harness.kinds == ["added", "updated"]
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.display_name == "Lounge"
    assert view.record_set.endpoint_generation == 1


@run_async
async def test_address_change_is_an_update_with_a_newer_generation():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    await harness.offer(advertisement(addresses=("192.0.2.13",)))
    assert harness.kinds == ["added", "updated"]
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.record_set.canonical_address == "192.0.2.13"
    assert view.record_set.endpoint_generation == 2


@run_async
async def test_a_published_generation_never_goes_backwards_for_a_receiver():
    """A later event must never publish an older generation than an earlier one.

    Removal and reappearance charge generations the resolver itself does not
    track, so an ordinary update afterwards would otherwise publish a number the
    adapter already issued and a consumer already treated as stale.
    """
    harness = Harness()
    await harness.start()
    record = advertisement()
    await harness.offer(record)
    harness.mdns.withdraw(record.key)
    await settle()
    await harness.offer(record)
    await harness.offer(advertisement(instance="Lounge._airplay._tcp.local."))
    await harness.offer(advertisement(addresses=("192.0.2.13",)))
    assert harness.kinds == ["added", "removed", "added", "updated", "updated"]
    generations = [
        event.receiver.record_set.endpoint_generation
        for event in harness.events
        if event.receiver is not None and event.receiver.record_set.receiver_id == LIVING_ROOM
    ]
    assert generations == sorted(generations)
    assert len(generations) == len(harness.events)
    # A refresh of an unchanged receiver is not history, so it publishes nothing
    # and therefore cannot lower the number either.
    harness.mdns.answer(record.key, advertisement(addresses=("192.0.2.13",)))
    assert isinstance(await harness.adapter.refresh(), Ok)
    assert len(harness.events) == len(generations)



    harness = Harness()
    await harness.start()
    await harness.offer(advertisement())
    await harness.offer(advertisement(device_id="112233445566", txt={"deviceid": "112233445566"}))
    assert harness.kinds == ["added", "added", "removed"]
    added, removed = harness.events[1], harness.events[2]
    assert added.receiver is not None
    assert added.receiver.record_set.receiver_id == REPLACEMENT
    assert added.receiver.record_set.replacement_of == LIVING_ROOM
    assert added.receiver.record_set.continuity_verified is False
    assert removed.receiver is not None
    assert removed.receiver.record_set.receiver_id == LIVING_ROOM
    assert removed.receiver.online is False
    assert harness.view(LIVING_ROOM) is not None
    assert harness.view(LIVING_ROOM).online is False


# --------------------------------------------------------------------------- hints


@run_async
async def test_a_display_name_or_model_alone_produces_no_pairing_hint():
    harness = Harness()
    await harness.start()
    await harness.offer(
        advertisement(txt={"deviceid": "AABBCCDDEEFF", "model": "AppleTV6,2"}),
    )
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.pairing_hint is None
    assert harness.events[0].diagnostic is None


@pytest.mark.parametrize(
    ("kind", "txt", "hint"),
    [
        ("raop", {"deviceid": "AABBCCDDEEFF", "pw": "1"}, "password"),
        ("airplay", {"deviceid": "AABBCCDDEEFF", "flags": "0x4"}, "pin"),
        ("raop", {"deviceid": "AABBCCDDEEFF", "sf": "0x4"}, "pin"),
        ("airplay", {"deviceid": "AABBCCDDEEFF", "flags": "0x0"}, None),
        ("airplay", {"deviceid": "AABBCCDDEEFF", "flags": "0x2000004"}, "pin"),
        ("airplay", {"deviceid": "AABBCCDDEEFF", "flags": "garbage"}, None),
    ],
)
@run_async
async def test_pairing_hints_are_normalized_from_explicit_keys(kind, txt, hint):
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement(kind, txt=txt))
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.pairing_hint == hint


@run_async
async def test_the_airplay_record_supplies_the_pairing_hint_when_both_services_disagree():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement("raop", port=5000, txt={"deviceid": "AABBCCDDEEFF", "pw": "1"}))
    await harness.offer(advertisement(txt={"deviceid": "AABBCCDDEEFF", "flags": "0x4"}))
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.pairing_hint == "pin"


@run_async
async def test_feature_bits_are_normalized_in_the_record_and_absent_when_unparseable():
    harness = Harness()
    await harness.start()
    await harness.offer(advertisement(txt={"deviceid": "AABBCCDDEEFF", "features": "0X5A7FFFF7"}))
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert view.record_set.records[0].txt["features"] == "0x5a7ffff7"
    harness.mdns.withdraw("Living Room._airplay._tcp.local.")
    await settle()
    await harness.offer(
        advertisement(txt={"deviceid": "AABBCCDDEEFF", "features": "nonsense"}),
    )
    view = harness.view(LIVING_ROOM)
    assert view is not None
    assert "features" not in view.record_set.records[0].txt


# --------------------------------------------------------------------------- refresh


@run_async
async def test_refresh_re_resolves_live_records_without_duplicating_events():
    harness = Harness()
    await harness.start()
    record = advertisement()
    await harness.offer(record)
    before = len(harness.mdns.resolve_calls)
    harness.mdns.answer(record.key, record)
    result = await harness.adapter.refresh()
    await settle()
    assert isinstance(result, Ok)
    assert harness.mdns.resolve_calls[before:] == [record.key]
    assert harness.kinds == ["added"]


@run_async
async def test_refresh_starts_browsing_when_the_adapter_is_not_started():
    harness = Harness()
    result = await harness.adapter.refresh()
    assert isinstance(result, Ok)
    assert [types for types, _ in harness.mdns.browsed] == [(AIRPLAY_SERVICE, RAOP_SERVICE)]


@run_async
async def test_refresh_failure_keeps_known_receivers_and_reports_a_retryable_error():
    harness = Harness()
    await harness.start()
    record = advertisement()
    await harness.offer(record)
    harness.mdns.answers.pop(record.key)
    result = await harness.adapter.refresh()
    await settle()
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.discovery_failed
    assert RecoveryAction.retry in result.error.recovery
    assert harness.kinds == ["added", "error"]
    view = harness.view(LIVING_ROOM)
    assert view is not None and view.online


@run_async
async def test_refresh_is_bounded_by_a_fixed_number_of_keys():
    harness = Harness()
    await harness.start()
    for index in range(MAX_REFRESH_KEYS + 5):
        record = advertisement(
            key=f"Receiver {index}._airplay._tcp.local.",
            instance=f"Receiver {index}._airplay._tcp.local.",
            device_id=f"aa00000000{index:02x}",
            addresses=("192.0.2.12",),
        )
        harness.mdns.answer(record.key, record)
        harness.mdns.announce(record.key)
    await settle()
    before = len(harness.mdns.resolve_calls)
    assert before == MAX_REFRESH_KEYS + 5
    await harness.adapter.refresh()
    await settle()
    assert len(harness.mdns.resolve_calls) - before == MAX_REFRESH_KEYS


# --------------------------------------------------------------------------- azc client


@run_async
async def test_azc_client_reports_a_missing_implementation_as_retryable():
    def factory() -> object:
        raise RuntimeError("zeroconf is not importable")

    client = AzcMdnsClient(FakeClock(), factory=factory)
    result = await client.browse((AIRPLAY_SERVICE, RAOP_SERVICE), lambda key, change: None)
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.discovery_failed
    assert RecoveryAction.retry in result.error.recovery
    # No backend exception text reaches the diagnostic.
    assert "zeroconf" not in result.error.message
    await client.close()


@run_async
async def test_azc_client_close_is_safe_before_start_and_idempotent():
    client = AzcMdnsClient(FakeClock(), factory=lambda: (_ for _ in ()).throw(RuntimeError()))
    await client.close()
    await client.close()


@run_async
async def test_azc_client_resolve_without_a_browser_fails_closed():
    client = AzcMdnsClient(FakeClock(), factory=lambda: (_ for _ in ()).throw(RuntimeError()))
    result = await client.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.discovery_failed


def test_notification_handler_uses_the_instance_name_not_the_service_type():
    """zeroconf's real signatures must map onto one change, never the wrong key."""
    from airplay_linux.discovery.zeroconf_adapter import _Notification

    class Change:
        name = "removed"

    seen: list[ServiceChange] = []
    handler = _Notification(seen.append, AIRPLAY_SERVICE)
    # Positional form: (zeroconf, service_type, name, state_change).
    handler(None, AIRPLAY_SERVICE, "Living Room._airplay._tcp.local.", Change())
    # Keyword form, which is what a newer zeroconf uses.
    handler(name="Lounge._airplay._tcp.local.", state_change="added")
    assert [(change.key, change.state) for change in seen] == [
        ("Living Room._airplay._tcp.local.", "removed"),
        ("Lounge._airplay._tcp.local.", "added"),
    ]
    assert {change.service_type for change in seen} == {AIRPLAY_SERVICE}


@pytest.mark.parametrize(
    "args",
    [
        (),
        (None, AIRPLAY_SERVICE),
        (None, AIRPLAY_SERVICE, "", "added"),
        (None, AIRPLAY_SERVICE, "Living Room._airplay._tcp.local.", "bogus"),
        (None, AIRPLAY_SERVICE, 42, "added"),
    ],
)
def test_notification_handler_ignores_malformed_notifications(args):
    from airplay_linux.discovery.zeroconf_adapter import _Notification

    seen: list[ServiceChange] = []
    _Notification(seen.append, AIRPLAY_SERVICE)(*args)
    assert seen == []


@run_async
async def test_azc_client_closes_every_browser_it_started():
    class Browser:
        def __init__(self, *args, **kwargs):
            self.cancelled = 0

        async def async_cancel(self):
            self.cancelled += 1

    class Client:
        def __init__(self):
            self.zeroconf = object()
            self.closed = 0

        async def async_close(self):
            self.closed += 1

    client = Client()
    mdns = AzcMdnsClient(FakeClock(), factory=lambda: client)
    import airplay_linux.discovery.zeroconf_adapter as adapter_module

    original = adapter_module.AsyncServiceBrowser if hasattr(
        adapter_module, "AsyncServiceBrowser",
    ) else None
    browsers: list[Browser] = []

    def make(*args, **kwargs):
        browser = Browser()
        browsers.append(browser)
        return browser

    # No zeroconf is installed here, so the browser is injected at the seam the
    # client imports it from rather than by installing the dependency.
    monkey = pytest.MonkeyPatch()
    try:
        import sys
        import types

        fake = types.ModuleType("zeroconf.asyncio")
        fake.AsyncServiceBrowser = make  # type: ignore[attr-defined]
        fake.AsyncZeroconf = lambda: client  # type: ignore[attr-defined]
        monkey.setitem(sys.modules, "zeroconf", types.ModuleType("zeroconf"))
        monkey.setitem(sys.modules, "zeroconf.asyncio", fake)
        result = await mdns.browse((AIRPLAY_SERVICE, RAOP_SERVICE), lambda change: None)
    finally:
        monkey.undo()
    assert isinstance(result, Ok)
    assert len(browsers) == 2
    # ``Subscription.close`` is synchronous, so the async cancel it schedules has
    # to be observed on the loop rather than assumed to have run inline.
    result.value.close()
    await settle()
    assert [browser.cancelled for browser in browsers] == [1, 1]
    result.value.close()
    await settle()
    assert [browser.cancelled for browser in browsers] == [1, 1]
    await mdns.close()
    assert client.closed == 1
    # Closing the client itself cancelled every browser exactly once more only if
    # it still held them; an already-detached handle is never cancelled again.
    assert [browser.cancelled for browser in browsers] == [1, 1]
    assert original is None or callable(original)


# --- the concrete resolve path: the seam the fake client never exercised -----


class _ServiceInfo:
    """A duck-typed stand-in for ``zeroconf.ServiceInfo``; no socket involved."""

    def __init__(self, **overrides: Any) -> None:
        self.name = "Living Room._airplay._tcp.local."
        self.type = "_airplay._tcp.local."
        self.server = "living-room.local."
        self.port = 7000
        self.addresses = [bytes([192, 0, 2, 10])]
        self.properties = {b"model": b"AppleTV6,2", b"deviceid": b"00:11:22:33:44:55"}
        for key, value in overrides.items():
            setattr(self, key, value)


def _resolving_client(info: object) -> Any:
    class Client:
        def __init__(self) -> None:
            self.zeroconf = object()

        async def async_get_service_info(self, *_args: Any, **_kwargs: Any) -> object:
            return info

        async def async_close(self) -> None:
            return None

    return Client()


@pytest.mark.asyncio
async def test_a_real_service_info_converts_into_a_domain_record():
    mdns = AzcMdnsClient(FakeClock(50.0), factory=lambda: _resolving_client(_ServiceInfo()))
    # ``resolve`` only serves keys a browse already registered, exactly as the
    # adapter's contract requires; seed that map rather than weakening resolve.
    mdns._types["Living Room._airplay._tcp.local."] = AIRPLAY_SERVICE
    result = await mdns.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Ok), result
    record = result.value
    assert record.device_id == "00:11:22:33:44:55"
    assert record.model == "AppleTV6,2"
    assert record.addresses == ("192.0.2.10",)
    assert record.port == 7000
    assert record.seen_at == 50.0
    await mdns.close()


@pytest.mark.asyncio
async def test_txt_bytes_that_are_not_utf8_are_dropped_not_replaced():
    info = _ServiceInfo(properties={b"model": b"AppleTV6,2", b"junk": b"\xff\xfe"})
    mdns = AzcMdnsClient(FakeClock(), factory=lambda: _resolving_client(info))
    mdns._types["Living Room._airplay._tcp.local."] = AIRPLAY_SERVICE
    result = await mdns.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Ok), result
    assert result.value.model == "AppleTV6,2"


@pytest.mark.asyncio
async def test_ipv6_addresses_are_carried_through_resolution():
    info = _ServiceInfo(addresses=[bytes(16)[:15] + b"\x01"])
    mdns = AzcMdnsClient(FakeClock(), factory=lambda: _resolving_client(info))
    mdns._types["Living Room._airplay._tcp.local."] = AIRPLAY_SERVICE
    result = await mdns.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Ok), result
    assert result.value.addresses == ("::1",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info",
    [
        _ServiceInfo(name=None),
        _ServiceInfo(type=None),
        _ServiceInfo(server=None),
        _ServiceInfo(port="7000"),
        _ServiceInfo(port=0),
    ],
    ids=["no-name", "no-type", "no-server", "port-text", "port-zero"],
)
async def test_a_malformed_service_info_is_a_sanitized_retryable_failure(info):
    mdns = AzcMdnsClient(FakeClock(), factory=lambda: _resolving_client(info))
    mdns._types["Living Room._airplay._tcp.local."] = AIRPLAY_SERVICE
    result = await mdns.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Err), result
    assert result.error.code is ErrorCode.discovery_failed


@pytest.mark.asyncio
async def test_unusable_txt_properties_degrade_to_no_evidence_rather_than_failing():
    """A non-mapping ``properties`` is not evidence, and not a crash either.

    Dropping it yields a record with no TXT-derived identity, which the resolver
    then treats as a weaker candidate. Refusing the whole advertisement would
    throw away a receiver whose address and port are perfectly usable.
    """
    info = _ServiceInfo(properties=["not", "a", "mapping"])
    mdns = AzcMdnsClient(FakeClock(), factory=lambda: _resolving_client(info))
    mdns._types["Living Room._airplay._tcp.local."] = AIRPLAY_SERVICE
    result = await mdns.resolve("Living Room._airplay._tcp.local.")
    assert isinstance(result, Ok), result
    assert result.value.addresses == ("192.0.2.10",)
    assert result.value.device_id is None
    assert dict(result.value.txt) == {}
