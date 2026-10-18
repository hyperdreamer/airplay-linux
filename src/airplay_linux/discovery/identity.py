"""Resolve complete advertisement snapshots without DNS or other network I/O.

Device IDs are authoritative for grouping. Fallbacks require reciprocal unique
cross-service matches; a label hash alone never establishes endpoint continuity.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from hashlib import sha256
import ipaddress
import json
import re
import unicodedata

from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverRecordSet,
    ReceiverTarget,
    ServiceRecord,
)
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result


_MAC = re.compile(
    r"(?:[0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{2}-){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4})",
    re.IGNORECASE,
)
_SERVICE_SUFFIX = re.compile(r"\._(?:airplay|raop)\._tcp(?:\.local)?\.?$", re.IGNORECASE)


def _text(value: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value).casefold()).strip()


def normalize_device_id(value: str | None) -> str | None:
    """Canonicalize nonempty IDs, removing separators only from valid MACs."""
    if value is None:
        return None
    value = _text(value)
    if _MAC.fullmatch(value):
        return re.sub(r"[:.\-]", "", value)
    return value or None


def _label(record: ServiceRecord) -> str:
    label = _SERVICE_SUFFIX.sub("", _text(record.instance)).strip()
    if record.service_type == "_raop._tcp":
        prefix, separator, remainder = label.partition("@")
        if separator and _MAC.fullmatch(prefix):
            label = remainder.strip()
    return label


def _host(value: str) -> str:
    value = _text(value).rstrip(".")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        try:
            return value.encode("idna").decode("ascii")
        except UnicodeError:
            return ""


def _address_key(value: str) -> tuple[int, int, str]:
    address = ipaddress.ip_address(value)
    return address.version, int(address), getattr(address, "scope_id", None) or ""


def _addresses(record: ServiceRecord) -> tuple[str, ...]:
    addresses = set()
    for value in record.addresses:
        try:
            addresses.add(str(ipaddress.ip_address(value.strip())))
        except ValueError:
            continue
    return tuple(sorted(addresses, key=_address_key))


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _Record:
    original: ServiceRecord
    device_id: str | None
    label: str
    model: str
    host: str
    addresses: tuple[str, ...]
    canonical: str

    @classmethod
    def from_service(cls, record: ServiceRecord) -> _Record:
        device_id = normalize_device_id(record.device_id)
        label, model, host = _label(record), _text(record.model), _host(record.host)
        addresses = _addresses(record)
        # Complete canonical records distinguish ephemeral ambiguous sets.
        # Stable IDs and endpoint fingerprints do not hash observation time.
        canonical = json.dumps(
            [
                _text(record.key), record.service_type, label, model, device_id,
                host, addresses, record.port, sorted(record.txt.items()), record.seen_at,
            ],
            ensure_ascii=True, separators=(",", ":"),
        )
        return cls(record, device_id, label, model, host, addresses, canonical)

    @property
    def address(self) -> str:
        return self.addresses[0] if self.addresses else ""


def _selected(records: tuple[_Record, ...]) -> _Record:
    return min(
        records,
        key=lambda r: (
            r.original.service_type != "_airplay._tcp", not bool(r.address),
            _address_key(r.address) if r.address else (0, 0, ""), r.canonical,
        ),
    )


def _protocol(record: _Record) -> str:
    return "airplay" if record.original.service_type == "_airplay._tcp" else "raop"


def _fallback_match(left: _Record, right: _Record) -> bool:
    return bool(
        left.original.service_type != right.original.service_type
        and left.label and left.model and left.address
        and (left.label, left.model, left.address) == (right.label, right.model, right.address)
    )


def _describe(records: tuple[_Record, ...], ambiguous: bool) -> ReceiverRecordSet:
    selected = _selected(records)
    device_ids = {r.device_id for r in records if r.device_id}
    if device_ids:
        kind = "device"
        identity = next(iter(device_ids))
        ambiguous = False
    elif selected.label and selected.model:
        kind = "fingerprint"
        identity = _digest(f"{selected.label}|{selected.model}")
    elif selected.host:
        kind = "endpoint"
        identity = _digest(f"{selected.host}|{selected.model}")
    else:
        ambiguous = True
    if ambiguous:
        kind = "ambiguous"
        identity = _digest(json.dumps(sorted(r.canonical for r in records), separators=(",", ":")))
    protocol = _protocol(selected)
    endpoint_fingerprint = _digest(f"{selected.address}|{selected.original.port}|{protocol}")
    return ReceiverRecordSet(
        records=tuple(r.original for r in sorted(records, key=lambda r: r.canonical)),
        receiver_id=ReceiverId(f"airplay-{kind}:{identity}"),
        identity_kind=kind,
        canonical_address=selected.address,
        endpoint_fingerprint=endpoint_fingerprint,
        endpoint_generation=1,
        persistable=not ambiguous and bool(selected.address),
        continuity_verified=not ambiguous and bool(selected.address),
        replacement_of=None,
        ambiguous=ambiguous,
    )


def _aggregate(records: tuple[ServiceRecord, ...]) -> tuple[ReceiverRecordSet, ...]:
    groups: list[tuple[_Record, ...]] = []
    identified: dict[str, list[_Record]] = {}
    for record in sorted((_Record.from_service(r) for r in records), key=lambda r: r.canonical):
        if record.device_id:
            identified.setdefault(record.device_id, []).append(record)
        else:
            groups.append((record,))
    groups.extend(tuple(group) for _, group in sorted(identified.items()))

    neighbors: list[set[int]] = [set() for _ in groups]
    for i, left in enumerate(groups):
        for j in range(i + 1, len(groups)):
            right = groups[j]
            if left[0].device_id and right[0].device_id:
                continue
            if any(_fallback_match(a, b) for a in left for b in right):
                neighbors[i].add(j)
                neighbors[j].add(i)

    described = []
    consumed = set()
    for i, group in enumerate(groups):
        if i in consumed:
            continue
        ambiguous = bool(neighbors[i])
        if len(neighbors[i]) == 1:
            j = next(iter(neighbors[i]))
            left_types = {r.original.service_type for r in group}
            right_types = {r.original.service_type for r in groups[j]}
            if neighbors[j] == {i} and left_types.isdisjoint(right_types):
                group += groups[j]
                consumed.add(j)
                ambiguous = False
        described.append((group, _describe(group, ambiguous)))
    counts = Counter(r.receiver_id for _, r in described)
    # The same fallback hash can describe distinct endpoints or same-service
    # duplicates. Neither may become a persistent preference/credential key.
    return tuple(sorted(
        (
            _describe(group, True)
            if counts[result.receiver_id] > 1 and result.identity_kind != "device"
            else result
            for group, result in described
        ),
        key=lambda r: r.receiver_id,
    ))


def target_for(record_set: ReceiverRecordSet) -> Result[ReceiverTarget]:
    """Select an advertised endpoint; ambiguity and missing addresses fail shut."""
    if record_set.ambiguous or not record_set.records:
        return _unavailable(record_set.receiver_id)
    record = _selected(tuple(_Record.from_service(r) for r in record_set.records))
    if not record.address or not 1 <= record.original.port <= 65535:
        return _unavailable(record_set.receiver_id)
    return Ok(value=ReceiverTarget(
        receiver_id=record_set.receiver_id,
        service_records=record_set.records,
        canonical_host=record.address,
        service_port=record.original.port,
        protocol=_protocol(record),
        endpoint_fingerprint=record_set.endpoint_fingerprint,
        endpoint_generation=record_set.endpoint_generation,
    ))


def _unavailable(receiver_id: ReceiverId) -> Err:
    return Err(error=Diagnostic(
        code=ErrorCode.stale_target,
        message="The receiver has no unambiguous current endpoint.",
        recovery=(), receiver_id=receiver_id,
    ))


def _normalized(record_set: ReceiverRecordSet) -> tuple[_Record, ...]:
    return tuple(_Record.from_service(r) for r in record_set.records)


@dataclass
class _Evidence:
    current: ReceiverRecordSet
    anchor: tuple[_Record, ...]
    online: bool
    invalidated: bool = False
    seeded: bool = False
    #: Positive model evidence accepted for this identity so far. It is kept
    #: separately from ``anchor`` because the anchor may be a fallback endpoint
    #: whose first snapshot carried no model at all; continuity must be judged
    #: against the strongest evidence ever observed, not against that snapshot.
    models: tuple[_Record, ...] = ()


def _overlap(left: tuple[_Record, ...], right: tuple[_Record, ...]) -> bool:
    return bool(
        {address for r in left for address in r.addresses}
        & {address for r in right for address in r.addresses}
    )


def _compatible_models(left: tuple[_Record, ...], right: tuple[_Record, ...]) -> bool:
    """Conflicting *positive* model evidence apart, models do not decide continuity.

    A companion service that omits its model has no model evidence: it can neither
    confirm nor contradict the identity, so its arrival or departure must not read
    as a replacement. Two snapshots that both name a model are still compared for
    equality, so genuinely conflicting nonempty evidence stays conservative.

    A single snapshot may also contradict *itself*: if two of its records name
    different models, neither can be trusted as this receiver's model, so the
    snapshot carries no usable evidence and must not authorize continuity. Without
    that check the contradiction would be retained and then accepted against
    itself forever.
    """
    previous = {r.model for r in left if r.model}
    current = {r.model for r in right if r.model}
    if len(current) > 1 or len(previous) > 1:
        return False
    return not previous or not current or previous == current


def _model_evidence(records: tuple[_Record, ...]) -> tuple[_Record, ...]:
    """The records of one snapshot that carry positive model evidence."""
    return tuple(r for r in records if r.model)


def _possible_replacement(left: tuple[_Record, ...], right: tuple[_Record, ...]) -> bool:
    if {r.original.key for r in left} & {r.original.key for r in right}:
        return True
    return _overlap(left, right) and any(
        a.host == b.host and a.model == b.model and a.label == b.label for a in left for b in right
    )


class IdentityResolver:
    """Resolve current records and retain departed identities as generation fences.

    ``update`` returns only current advertisements. Saved evidence and departed
    records remain private, never creating online receivers or credential trust.
    """

    def __init__(self) -> None:
        self._history: dict[ReceiverId, _Evidence] = {}
        self._fence: dict[ReceiverId, int] = {}
        self._unverified_saved: set[ReceiverId] = set()
        self._started = False

    def seed_saved(self, saved: tuple[SavedReceiver, ...]) -> None:
        """Load validated non-secret identity/endpoint evidence before browsing."""
        if self._started:
            raise ValueError("Saved evidence must be seeded before browsing")
        counts = Counter(s.receiver_id for s in saved)
        self._history.clear()
        self._fence = {}
        self._unverified_saved = set(counts)
        for item in saved:
            endpoint = item.last_endpoint
            if endpoint is None:
                continue
            # The generation is a high-water mark, not a reward for valid
            # evidence. Rejecting continuity only withholds authorization; the
            # endpoint it describes was still issued once and must never be
            # reissued, so duplicates keep the highest applicable generation.
            self._fence[item.receiver_id] = max(
                self._fence.get(item.receiver_id, 0), endpoint.endpoint_generation,
            )
            if counts[item.receiver_id] != 1:
                continue
            candidates = _aggregate(item.service_metadata)
            if len(candidates) != 1:
                continue
            candidate = candidates[0]
            target = target_for(candidate)
            if (
                not candidate.persistable or candidate.receiver_id != item.receiver_id
                or endpoint.receiver_id != item.receiver_id or not isinstance(target, Ok)
                or candidate.endpoint_fingerprint != item.continuity_fingerprint
                or endpoint.endpoint_fingerprint != candidate.endpoint_fingerprint
                or (endpoint.canonical_host, endpoint.service_port, endpoint.protocol)
                != (target.value.canonical_host, target.value.service_port, target.value.protocol)
            ):
                continue
            candidate = replace(candidate, endpoint_generation=endpoint.endpoint_generation + 1)
            current = _normalized(candidate)
            self._history[item.receiver_id] = _Evidence(
                candidate, current, online=False, seeded=True,
                models=_model_evidence(current),
            )
            self._unverified_saved.discard(item.receiver_id)

    def update(self, records: tuple[ServiceRecord, ...]) -> tuple[ReceiverRecordSet, ...]:
        self._started = True
        candidates = _aggregate(records)
        current_ids = {r.receiver_id for r in candidates}
        normalized = {r.receiver_id: _normalized(r) for r in candidates}
        replacements = {
            r.receiver_id: [
                receiver_id for receiver_id, previous in self._history.items()
                if receiver_id not in current_ids
                and _possible_replacement(previous.anchor, normalized[r.receiver_id])
            ]
            for r in candidates if r.receiver_id not in self._history
        }
        replacement_counts = Counter(old for matches in replacements.values() for old in matches)
        resolved = []
        for candidate in candidates:
            receiver_id = candidate.receiver_id
            current = normalized[receiver_id]
            previous = self._history.get(receiver_id)
            if previous is None:
                # Issue this identity above any generation saved evidence already
                # consumed, including evidence whose continuity claim was rejected.
                candidate = replace(
                    candidate,
                    endpoint_generation=max(
                        candidate.endpoint_generation, self._fence.get(receiver_id, 0) + 1,
                    ),
                )
                matches = replacements.get(receiver_id, [])
                replacement = matches[0] if len(matches) == 1 and replacement_counts[matches[0]] == 1 else None
                if receiver_id in self._unverified_saved:
                    replacement = replacement or receiver_id
                observed = _model_evidence(current)
                # A snapshot that contradicts itself names no usable model, so it
                # cannot establish continuity on first sight and must not retain
                # the contradiction. Without this, two contradictory records would
                # agree with each other forever after.
                self_consistent = _compatible_models((), current)
                if not self_consistent:
                    replacement = replacement or receiver_id
                if replacement is not None:
                    candidate = replace(candidate, replacement_of=replacement, continuity_verified=False)
                evidence = _Evidence(
                    candidate, current, online=True, invalidated=replacement is not None,
                    models=observed if self_consistent else (),
                )
            else:
                generation = previous.current.endpoint_generation
                if candidate.endpoint_fingerprint != previous.current.endpoint_fingerprint:
                    generation += 1
                # Compare against the strongest model evidence ever accepted for
                # this identity, never against the snapshot that happened to be
                # seen first. A model-less anchor would otherwise accept every
                # later model as if no positive evidence existed.
                observed = _model_evidence(current)
                compatible = _compatible_models(previous.models, current)
                if candidate.identity_kind != "device":
                    overlap = _overlap(previous.anchor, current)
                    before, after = _selected(previous.anchor), _selected(current)
                    same_endpoint_kind = _protocol(before) == _protocol(after)
                    same_port = before.original.port == after.original.port
                    # A RAOP-only first advertisement cannot authorize saved
                    # AirPlay preferences. It also must not poison the later,
                    # complete exact match merely by arriving first.
                    compatible = compatible and overlap and same_endpoint_kind and same_port
                    if not overlap or (same_endpoint_kind and not same_port):
                        previous.invalidated = True
                    if (
                        not previous.seeded and overlap and not same_endpoint_kind
                        and before.original.service_type == "_raop._tcp"
                        and after.original.service_type == "_airplay._tcp"
                    ):
                        previous.anchor = current
                        compatible = True
                verified = candidate.continuity_verified and compatible and not previous.invalidated
                replacement = previous.current.replacement_of
                if not verified and not candidate.ambiguous:
                    replacement = replacement or receiver_id
                elif verified:
                    replacement = None
                candidate = replace(
                    candidate, endpoint_generation=generation,
                    continuity_verified=verified, replacement_of=replacement,
                )
                # Keep positive model evidence whenever it is internally
                # consistent and compatible with what this identity already
                # named — independently of ``verified``, which also requires a
                # currently usable endpoint. A receiver whose address is briefly
                # missing has not stopped being the same receiver, and forgetting
                # its model there would let a later conflicting model pass.
                model_evidence_usable = bool(observed) and _compatible_models(
                    previous.models, current,
                )
                evidence = _Evidence(
                    candidate, previous.anchor, online=True,
                    invalidated=previous.invalidated, seeded=previous.seeded,
                    models=observed if model_evidence_usable else previous.models,
                )
            self._history[receiver_id] = evidence
            resolved.append(candidate)
        for receiver_id, previous in self._history.items():
            if receiver_id not in current_ids and previous.online:
                previous.current = replace(
                    previous.current,
                    endpoint_generation=previous.current.endpoint_generation + 1,
                )
                previous.online = False
        return tuple(resolved)
