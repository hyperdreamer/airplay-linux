"""Typed discovery events from mDNS advertisements; no backend record escapes.

The domain never sees a zeroconf record. This module owns three seams and
nothing else:

* the **raw-service seam**, where a browser's raw advertisement (base64-free
  bytes, padded TXT values, ``_airplay._tcp.local.`` type names) becomes a
  :class:`~airplay_linux.domain.identity.ServiceRecord`. Only the *type* is
  normalized here; the instance name keeps its exact advertised meaning, and
  only the user-visible ``display_name`` strips the service suffix;
* the **revision fence**, which pairs every resolve with the notification that
  started it, so an answer that lands after its key was withdrawn -- or after a
  newer announcement superseded it -- is discarded instead of resurrecting a
  vanished endpoint;
* the **view diff**, which feeds the *complete* live record set to
  :class:`~airplay_linux.discovery.identity.IdentityResolver` before publishing
  anything, so identity is decided against one consistent snapshot rather than
  per notification, and turns the resolver's current record sets into
  added/updated/removed events.

A departed receiver keeps its last-known view with ``online=False`` and a
strictly newer endpoint generation, so the target it used to describe can never
become usable again. That retention is what keeps a known device visible
offline; a *saved* receiver that has never been seen is merged from
``SavedReceiver`` by the caller (``ViewInputs.saved``), because the adapter will
not fabricate a view it cannot back with a current advertisement.

Discovery never claims a resolution: no TXT model, feature word, or display
name produces capability evidence, and an ambiguous record set stays behind
``target_for``'s refusal.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import socket
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from airplay_linux.diagnostics import diagnostic
from airplay_linux.discovery.identity import IdentityResolver
from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverRecordSet,
    ReceiverView,
    ServiceRecord,
)
from airplay_linux.domain.messages import DiscoveryEvent
from airplay_linux.domain.ports import Clock, Subscription
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result

AIRPLAY_SERVICE = "_airplay._tcp.local."
RAOP_SERVICE = "_raop._tcp.local."
BROWSED_SERVICES: tuple[str, ...] = (AIRPLAY_SERVICE, RAOP_SERVICE)

#: A scan re-resolves at most this many known keys, in key order. The bound is
#: what makes ``refresh`` a bounded refresh rather than an open-ended sweep; it
#: never spawns a subprocess and never browses a second time.
MAX_REFRESH_KEYS = 64

_DOMAIN_TYPES: tuple[str, ...] = ("_airplay._tcp", "_raop._tcp")
_SERVICE_SUFFIX = re.compile(r"\._(?:airplay|raop)\._tcp(?:\.local)?\.?$", re.IGNORECASE)
_MAC = re.compile(r"(?:[0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2})", re.IGNORECASE)
_FEATURES = re.compile(r"\s*0x([0-9a-f]{1,16})\s*(?:,\s*0x([0-9a-f]{1,16})\s*)?", re.IGNORECASE)
#: The published status word's lowest bit means "this receiver needs a PIN".
_PIN_FLAG = 0x4
_TRUTHY = frozenset({"1", "true", "yes", "on"})


# --------------------------------------------------------------------------- seam


@dataclass(frozen=True, kw_only=True)
class ServiceChange:
    """One browser notification about one raw service instance.

    The record itself is deliberately absent: a notification only says *what
    changed*, and the adapter resolves the key so the record it adopts is the one
    the cache holds at that moment.
    """

    service_type: str
    key: str
    state: Literal["added", "updated", "removed"]


@dataclass(frozen=True, kw_only=True)
class RawAdvertisement:
    """The adapter-private image of one ``ServiceInfo``; fields keep raw bytes."""

    name: str
    type: str
    server: str
    port: int
    addresses: tuple[bytes, ...]
    txt: Mapping[bytes | str, bytes | str | None]


class MdnsClient(Protocol):
    """The fakeable mDNS seam; the only part of discovery that touches a network."""

    async def browse(
        self, types: tuple[str, ...], callback: Callable[[ServiceChange], None],
    ) -> Result[Subscription]: ...

    async def resolve(self, key: str) -> Result[ServiceRecord]: ...

    async def close(self) -> None: ...


def normalize_service_type(raw: object) -> str | None:
    """Canonicalize a raw service type, stripping ``.local.`` at this seam only.

    The result is the domain form (``_airplay._tcp``), never the raw form, and
    an unrelated type is refused rather than stored as if it were browsed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip().casefold().rstrip(".")
    if text.endswith(".local"):
        text = text[: len(text) - len(".local")]
    return text if text in _DOMAIN_TYPES else None


def _type_suffix(key: str) -> str | None:
    """The service type encoded in a raw instance name, or ``None``."""
    for raw_type in BROWSED_SERVICES:
        if key.casefold().endswith(raw_type.casefold()):
            return raw_type
    return None


def _decode_text(raw: object) -> str | None:
    """Decode one TXT side; ``None`` means the bytes are not usable text."""
    if isinstance(raw, str):
        text = raw
    elif isinstance(raw, (bytes, bytearray)):
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        return None
    # DNS-SD pads short values with NULs; a replacement character is never
    # substituted for a byte the advertisement did not contain.
    text = text.rstrip("\x00")
    return unicodedata.normalize("NFC", text) if text else ""


def decode_txt(values: object) -> dict[str, str]:
    """Decode raw TXT properties, dropping pairs that are not usable text.

    Invalid UTF-8 is dropped rather than replaced, so a value the receiver did
    not actually advertise can never appear as evidence. A key that is present
    with no value decodes to the empty string.
    """
    if not isinstance(values, Mapping):
        raise TypeError("TXT properties must be a mapping of raw bytes or strings")
    decoded: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = _decode_text(raw_key)
        if not key:
            continue
        if raw_value is None:
            decoded[key] = ""
            continue
        value = _decode_text(raw_value)
        if value is None:
            continue
        decoded[key] = value
    return decoded


def normalize_feature_bits(raw: object) -> str | None:
    """Render a feature word canonically, or ``None`` when it is not one.

    One or two comma-separated 32-bit words are what AirPlay actually
    advertises; anything else is not evidence and is dropped instead of being
    guessed at.
    """
    if not isinstance(raw, str):
        return None
    match = _FEATURES.fullmatch(raw)
    if match is None:
        return None
    return ",".join(f"0x{int(word, 16):x}" for word in match.groups() if word is not None)


def _socket_address(payload: object) -> str | None:
    if not isinstance(payload, (bytes, bytearray)):
        return None
    try:
        if len(payload) == 4:
            return socket.inet_ntop(socket.AF_INET, bytes(payload))
        if len(payload) == 16:
            return socket.inet_ntop(socket.AF_INET6, bytes(payload))
    except (OSError, ValueError):
        return None
    return None


def _instance_mac(key: str) -> str | None:
    """The MAC prefix a RAOP instance name carries, when it carries a valid one."""
    prefix, separator, _ = key.partition("@")
    return prefix.strip() if separator and _MAC.fullmatch(prefix.strip()) else None


def record_from_advertisement(raw: RawAdvertisement, seen_at: float) -> ServiceRecord | None:
    """Build a domain record from one raw advertisement, or refuse it.

    The instance and key keep their raw meaning; only the service type is
    canonicalized, and only here.
    """
    service_type = normalize_service_type(raw.type) or normalize_service_type(_type_suffix(raw.name))
    if service_type is None:
        return None
    port = raw.port if type(raw.port) is int else -1
    if not 1 <= port <= 65535:
        return None
    try:
        txt = decode_txt(raw.txt)
    except TypeError:
        return None
    device_id = txt.get("deviceid") or None
    if device_id is None and service_type == "_raop._tcp":
        device_id = _instance_mac(raw.name)
    model = txt.get("model") or txt.get("am") or ""
    addresses = tuple(
        address for address in (_socket_address(item) for item in raw.addresses) if address
    )
    return ServiceRecord(
        key=raw.name,
        service_type=service_type,  # type: ignore[arg-type]
        instance=raw.name,
        model=model,
        device_id=device_id,
        host=raw.server,
        addresses=addresses,
        port=port,
        txt=txt,
        seen_at=float(seen_at),
    )


# --------------------------------------------------------------------------- view


def _ordered(record_set: ReceiverRecordSet) -> tuple[ServiceRecord, ...]:
    """AirPlay records first, then RAOP, each in a deterministic key order."""
    return tuple(sorted(
        record_set.records,
        key=lambda record: (record.service_type != "_airplay._tcp", record.key),
    ))


def _label(record: ServiceRecord) -> str:
    """The advertised instance label with the service suffix removed.

    This is presentation only: the stored instance keeps its exact meaning, and
    no identity decision is ever taken from this text.
    """
    label = _SERVICE_SUFFIX.sub(
        "", unicodedata.normalize("NFC", record.instance),
    ).strip()
    if record.service_type == "_raop._tcp":
        prefix, separator, remainder = label.partition("@")
        if separator and _MAC.fullmatch(prefix.strip()):
            label = remainder.strip()
    return label


def display_name(record_set: ReceiverRecordSet) -> str:
    """A user-visible name; a model or receiver id is only a last resort."""
    for record in _ordered(record_set):
        label = _label(record)
        if label:
            return label
    for record in _ordered(record_set):
        if record.model:
            return record.model
    return str(record_set.receiver_id)


def _text_flag(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    if not text or text in {"0", "false", "no", "off"}:
        return False
    return True if text in _TRUTHY else None


def _int_flag(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return int(value.strip(), 0)
    except ValueError:
        return None


def _hint_for(record: ServiceRecord) -> str | None:
    """Explicit pairing evidence only; a name or model never becomes a hint.

    Both services publish a status word -- ``flags`` on AirPlay, ``sf`` on
    RAOP -- and the same lowest bit means "needs a PIN". No other bit is
    interpreted, and a value that is not an integer is not evidence.
    """
    if _text_flag(record.txt.get("pw")):
        return "password"
    flags = _int_flag(record.txt.get("flags"))
    if flags is None:
        flags = _int_flag(record.txt.get("sf"))
    if flags is not None and flags & _PIN_FLAG:
        return "pin"
    return None


def pairing_hint(record_set: ReceiverRecordSet) -> str | None:
    """The strongest advertised hint, with the AirPlay record taking precedence."""
    for record in _ordered(record_set):
        hint = _hint_for(record)
        if hint is not None:
            return hint
    return None


def _signature(record_set: ReceiverRecordSet) -> tuple[object, ...]:
    """Everything an event may report, excluding the observation time.

    Refreshing an unchanged receiver updates its freshness without pretending
    that the receiver changed, so ``seen_at`` is deliberately not part of the
    comparison.
    """
    return (
        record_set.receiver_id,
        record_set.identity_kind,
        record_set.canonical_address,
        record_set.endpoint_fingerprint,
        record_set.endpoint_generation,
        record_set.persistable,
        record_set.continuity_verified,
        record_set.replacement_of,
        record_set.ambiguous,
        tuple(
            (record.key, record.service_type, record.instance, record.model,
             record.device_id, record.host, record.addresses, record.port,
             tuple(sorted(record.txt.items())))
            for record in record_set.records
        ),
    )


def _view(record_set: ReceiverRecordSet, online: bool) -> ReceiverView:
    return ReceiverView(
        record_set=record_set,
        display_name=display_name(record_set),
        online=online,
        pairing_hint=pairing_hint(record_set),
    )


# --------------------------------------------------------------------------- state


@dataclass
class _Entry:
    """One raw key's current revision and the record most recently adopted for it."""

    revision: int
    record: ServiceRecord | None = None


class _Subscriptions:
    """Detach handle for one discovery subscriber; ``close`` is idempotent."""

    __slots__ = ("_action", "_closed")

    def __init__(self, action: Callable[[], None]) -> None:
        self._action: Callable[[], None] | None = action
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        action, self._action = self._action, None
        if action is not None:
            action()


class ZeroconfDiscovery:
    """DiscoveryPort over an injected :class:`MdnsClient` and identity resolver."""

    def __init__(self, resolver: IdentityResolver, clock: Clock, mdns: MdnsClient) -> None:
        self._resolver = resolver
        self._clock = clock
        self._mdns = mdns
        self._entries: dict[str, _Entry] = {}
        self._views: dict[ReceiverId, ReceiverView] = {}
        self._subscribers: list[Callable[[DiscoveryEvent], None]] = []
        self._browsers: list[Subscription] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._started = False
        self._closed = False

    # -- port ---------------------------------------------------------------

    async def start(self) -> Result[None]:
        """Start both service browsers exactly once; a failure stays retryable."""
        if self._closed:
            return _failure()
        if self._started:
            return Ok(value=None)
        self._started = True
        result = await self._mdns.browse(BROWSED_SERVICES, self._notify)
        if isinstance(result, Err):
            # Nothing is subscribed, so a retry may browse again without
            # leaving a second browser behind.
            self._started = False
            self._report(result.error)
            return result
        self._browsers.append(result.value)
        return Ok(value=None)

    async def refresh(self) -> Result[None]:
        """Re-read the known keys, bounded by :data:`MAX_REFRESH_KEYS`."""
        if self._closed:
            return _failure()
        if not self._started:
            return await self.start()
        keys = sorted(key for key, entry in self._entries.items() if entry.record is not None)
        failure: Diagnostic | None = None
        for key in keys[:MAX_REFRESH_KEYS]:
            entry = self._entries[key]
            revision = entry.revision + 1
            self._entries[key] = _Entry(revision, entry.record)
            result = await self._resolve(key, revision)
            if isinstance(result, Err) and failure is None:
                failure = result.error
        if failure is not None:
            self._report(failure)
            return Err(error=failure)
        return Ok(value=None)

    def current(self, receiver_id: ReceiverId) -> ReceiverView | None:
        return self._views.get(receiver_id)

    def subscribe(self, callback: Callable[[DiscoveryEvent], None]) -> Subscription:
        self._subscribers.append(callback)

        def detach() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _Subscriptions(detach)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        for browser in self._browsers:
            browser.close()
        self._browsers.clear()
        await self._mdns.close()

    # -- notifications ------------------------------------------------------

    def _notify(self, change: ServiceChange) -> None:
        """Take one browser notification; every path ends in a consistent publish."""
        if self._closed:
            return
        previous = self._entries.get(change.key)
        revision = (previous.revision if previous is not None else 0) + 1
        if change.state == "removed":
            self._entries[change.key] = _Entry(revision)
            self._publish()
            return
        # Keep the last-known record while the new one is resolved, so a
        # transient re-announcement does not flap the receiver offline.
        self._entries[change.key] = _Entry(revision, previous.record if previous else None)
        task = asyncio.ensure_future(self._background(change.key, revision))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _background(self, key: str, revision: int) -> None:
        result = await self._resolve(key, revision)
        if isinstance(result, Err) and not self._closed:
            self._report(result.error)

    async def _resolve(self, key: str, revision: int) -> Result[ServiceRecord | None]:
        """Resolve a key and adopt the answer only if the fence still matches.

        ``Ok(None)`` is the superseded outcome: a removal or a newer
        announcement won, so nothing was adopted and nothing should be reported.
        """
        result = await self._mdns.resolve(key)
        if self._closed:
            return Ok(value=None)
        entry = self._entries.get(key)
        if entry is None or entry.revision != revision:
            # A removal, or a newer announcement, superseded this resolve.
            return Ok(value=None)
        if isinstance(result, Err):
            return result
        record = result.value
        if record.key != key:
            # A client that answers for a different instance is not evidence.
            return Err(error=_diagnostic())
        self._entries[key] = _Entry(revision, _adopted(record, self._clock.now()))
        self._publish()
        return Ok(value=record)

    # -- publishing ---------------------------------------------------------

    def _publish(self) -> None:
        live = tuple(
            entry.record for entry in self._entries.values() if entry.record is not None
        )
        resolved = self._resolver.update(live)
        published: dict[ReceiverId, ReceiverView] = {}
        for record_set in resolved:
            # Two identical advertisements can describe one ambiguous identity;
            # the resolver already fails those shut, so one view is enough.
            published.setdefault(record_set.receiver_id, _view(record_set, online=True))
        events: list[DiscoveryEvent] = []
        for receiver_id in sorted(published):
            view = published[receiver_id]
            previous = self._views.get(receiver_id)
            if previous is None or not previous.online:
                view = _fenced(previous, view)
                events.append(DiscoveryEvent(kind="added", receiver=view, diagnostic=None))
            elif _signature(previous.record_set) != _signature(view.record_set) or (
                previous.display_name, previous.pairing_hint
            ) != (view.display_name, view.pairing_hint):
                view = _fenced(previous, view)
                events.append(DiscoveryEvent(kind="updated", receiver=view, diagnostic=None))
            published[receiver_id] = view
        self._views.update(published)
        for receiver_id in sorted(self._views):
            if receiver_id in published:
                continue
            previous = self._views[receiver_id]
            if not previous.online:
                continue
            # Removal invalidates the endpoint rather than commanding transport:
            # the view keeps its identity and reports a newer generation.
            offline = replace(
                previous,
                record_set=replace(
                    previous.record_set,
                    endpoint_generation=previous.record_set.endpoint_generation + 1,
                ),
                online=False,
            )
            self._views[receiver_id] = offline
            events.append(DiscoveryEvent(kind="removed", receiver=offline, diagnostic=None))
        for event in events:
            for callback in tuple(self._subscribers):
                callback(event)

    def _report(self, error: Diagnostic) -> None:
        event = DiscoveryEvent(kind="error", receiver=None, diagnostic=error)
        for callback in tuple(self._subscribers):
            callback(event)


def _fenced(previous: ReceiverView | None, view: ReceiverView) -> ReceiverView:
    """Publish a generation this receiver has never issued before.

    The resolver owns the generation, but a receiver that comes back after a
    removal would otherwise re-issue the number its removal already invalidated,
    and a later refresh would then publish a *lower* number than the one a
    consumer is holding. Both are staleness hazards, so the published value is
    clamped monotonically per receiver and advanced by the reappearance itself.
    """
    if previous is None:
        return view
    issued = previous.record_set.endpoint_generation
    generation = max(view.record_set.endpoint_generation, issued)
    if not previous.online:
        # Removal charged one generation; coming back charges another, so a
        # target fenced at either earlier number stays invalid.
        generation = max(generation, issued + 1)
    if generation == view.record_set.endpoint_generation:
        return view
    return replace(
        view, record_set=replace(view.record_set, endpoint_generation=generation),
    )


def _adopted(record: ServiceRecord, seen_at: float) -> ServiceRecord:
    """Stamp adoption time and canonicalize the feature word a client returned."""
    txt = dict(record.txt)
    if "features" in txt:
        features = normalize_feature_bits(txt["features"])
        if features is None:
            del txt["features"]
        else:
            txt["features"] = features
    return replace(record, txt=txt, seen_at=float(seen_at))


def _diagnostic() -> Diagnostic:
    return diagnostic(ErrorCode.discovery_failed)


def _failure() -> Err:
    return Err(error=_diagnostic())


# --------------------------------------------------------------------------- azc


def _default_factory() -> Any:
    """The one import of the selected mDNS implementation, kept inside the seam."""
    from zeroconf.asyncio import AsyncZeroconf

    return AsyncZeroconf()


async def _maybe_await(value: object) -> None:
    if inspect.isawaitable(value):
        await value


class AzcMdnsClient:
    """``MdnsClient`` over ``AsyncZeroconf``; a missing implementation is retryable.

    Every backend failure is converted to the catalog's ``discovery_failed``
    diagnostic here, so no zeroconf exception text can reach the UI.
    """

    def __init__(
        self,
        clock: Clock,
        factory: Callable[[], Any] | None = None,
        *,
        resolve_timeout: float = 2.0,
    ) -> None:
        self._clock = clock
        self._factory = factory or _default_factory
        self._resolve_timeout = resolve_timeout
        self._client: Any = None
        self._browsers: list[Any] = []
        self._types: dict[str, str] = {}
        self._closed = False

    def _detach(self, browsers: list[Any]) -> None:
        """Forget handles a subscription already cancelled, so close is not double."""
        for browser in browsers:
            try:
                self._browsers.remove(browser)
            except ValueError:
                continue

    async def browse(
        self, types: tuple[str, ...], callback: Callable[[ServiceChange], None],
    ) -> Result[Subscription]:
        if self._closed:
            return _failure()
        client = await self._acquire()
        if client is None:
            return _failure()
        try:
            from zeroconf.asyncio import AsyncServiceBrowser
        except Exception:
            return _failure()
        browsers: list[Any] = []
        try:
            for service_type in types:
                handler = _Notification(callback, service_type)
                browser = AsyncServiceBrowser(client.zeroconf, service_type, handlers=[handler])
                await _maybe_await(browser)
                browsers.append(browser)
        except Exception:
            for browser in browsers:
                await _cancel(browser)
            return _failure()
        self._browsers.extend(browsers)
        return Ok(value=_Browsers(browsers, self._types, self._detach))

    async def resolve(self, key: str) -> Result[ServiceRecord]:
        service_type = self._types.get(key) or _type_suffix(key)
        if service_type is None or self._client is None:
            return _failure()
        try:
            info = await self._client.async_get_service_info(
                service_type, key, timeout=self._resolve_timeout,
            )
        except Exception:
            return _failure()
        if info is None:
            return _failure()
        record = record_from_advertisement(_raw(info), self._clock.now())
        if record is None:
            return _failure()
        self._types[record.key] = service_type
        return Ok(value=record)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        browsers, self._browsers = self._browsers, []
        for browser in browsers:
            await _cancel(browser)
        client, self._client = self._client, None
        if client is not None:
            try:
                await _maybe_await(client.async_close())
            except Exception:
                pass
        self._types.clear()

    async def _acquire(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            self._client = self._factory()
        except Exception:
            self._client = None
        return self._client


class _Notification:
    """Bound browser handler; a malformed notification is ignored, never raised."""

    __slots__ = ("_callback", "_service_type")

    def __init__(self, callback: Callable[[ServiceChange], None], service_type: str) -> None:
        self._callback = callback
        self._service_type = service_type

    def __call__(self, *args: object, **kwargs: object) -> None:
        """Map zeroconf's positional or keyword notification onto one change.

        zeroconf passes ``(zeroconf, service_type, name, state_change)`` to the
        positional signature and names them by keyword otherwise. The instance
        name is the argument that is *not* a service type, so the browser's own
        type can never be mistaken for the key it announces.
        """
        name = kwargs.get("name")
        state = kwargs.get("state_change")
        if name is None:
            for item in args:
                if isinstance(item, str) and normalize_service_type(item) is None \
                        and "._tcp" in item.casefold():
                    name = item
                    break
        if state is None and args:
            state = args[-1]
        if not isinstance(name, str) or not name:
            return
        change = str(getattr(state, "name", state) or "").casefold()
        if change not in {"added", "updated", "removed"}:
            return
        self._callback(ServiceChange(
            service_type=self._service_type, key=name, state=change,  # type: ignore[arg-type]
        ))


class _Browsers:
    """Subscription handle over every browser started for one ``browse`` call."""

    __slots__ = ("_browsers", "_types", "_detach", "_closed")

    def __init__(
        self, browsers: list[Any], types: dict[str, str],
        detach: Callable[[list[Any]], None],
    ) -> None:
        self._browsers = browsers
        self._types = types
        self._detach = detach
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        browsers, self._browsers = self._browsers, []
        # Hand the handles back first, so the owning client never cancels the
        # same browser a second time during its own shutdown.
        self._detach(browsers)
        for browser in browsers:
            _cancel_nowait(browser)


async def _cancel(browser: object) -> None:
    cancel = getattr(browser, "async_cancel", None) or getattr(browser, "cancel", None)
    if cancel is None:
        return
    try:
        await _maybe_await(cancel())
    except Exception:
        pass


def _cancel_nowait(browser: object) -> None:
    """``Subscription.close`` is synchronous, so schedule the async cancel."""
    cancel = getattr(browser, "async_cancel", None) or getattr(browser, "cancel", None)
    if cancel is None:
        return
    try:
        result = cancel()
    except Exception:
        return
    if inspect.isawaitable(result):
        try:
            asyncio.ensure_future(result)
        except RuntimeError:
            result.close()  # type: ignore[union-attr]
