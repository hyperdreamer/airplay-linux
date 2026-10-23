"""Receiver-mode resolution policy: reliable evidence, continuity, explicit choice.

Guessing a receiver mode is a non-goal, so this module never derives a size from
a display name, a model, a service category, a stale endpoint, or a generic
television default. A decision comes from exactly one of three places:

1. a mode remembered for *this* continuous identity
   (:class:`~airplay_linux.storage.PreferenceAccess` decides continuity -- an
   equal label hash alone is never continuity for a fallback identity);
2. one reliable advertised or pre-stream-queried mode;
3. an explicit user choice, including a positive custom dimension.

Multiple equally reliable modes and every unreliable/absent report produce a
:class:`ResolutionRequired` instead of a guess. A remembered mode wins over
current reports only because continuity was verified; a remembered value for an
identity whose continuity is not current is not reused at all, and the picker
reports that rejection rather than silently substituting it.

The module is pure policy: it reads DTOs and returns a typed decision. It never
creates, resizes, or removes an output and never writes a preference itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from airplay_linux.domain.identity import ReceiverRecordSet
from airplay_linux.domain.records import SavedReceiver
from airplay_linux.domain.resources import (
    ReceiverCapabilities,
    ResolutionDecision,
    ResolutionEvidence,
    Size,
)
from airplay_linux.storage import PreferenceAccess, is_ambiguous_identity

#: Evidence origins written by this policy. The origin records *where* the size
#: came from; ``reliable`` records whether that provenance is trustworthy.
_REMEMBERED_ORIGIN = "remembered"
_DISCOVERY_ORIGIN = "discovery"
_QUERY_ORIGIN = "query"
_PICKER_ORIGIN = "picker"
_VERIFIED_ORIGIN = "verified"

#: Capability sources in precedence order: discovery, then the transport
#: adapter's pre-stream ``query_modes`` result.
_CAPABILITY_ORIGINS: tuple[str, str] = (_DISCOVERY_ORIGIN, _QUERY_ORIGIN)


@dataclass(frozen=True, kw_only=True)
class ResolutionRequired:
    """An explicit decision is required before a virtual output is reserved.

    ``choices`` is a supported-mode list only when reliable evidence reported
    those modes. It is empty when nothing trustworthy was reported, so a caller
    cannot mistake a generic default for receiver support. ``remembered_rejected``
    tells the UI that a remembered choice exists for this identity but could not
    be used because continuity with the saved record is not justified.
    """

    choices: tuple[Size, ...] = ()
    remembered_rejected: bool = False

    def __post_init__(self) -> None:
        if type(self.choices) is not tuple:
            raise TypeError("choices must be a tuple of Size values")
        seen: set[Size] = set()
        for choice in self.choices:
            if not isinstance(choice, Size):
                raise TypeError("every resolution choice must be a Size")
            if choice in seen:
                raise ValueError("resolution choices must be distinct")
            seen.add(choice)
        if type(self.remembered_rejected) is not bool:
            raise TypeError("remembered_rejected must be a boolean")


def _evidence(origin: str, record_set: ReceiverRecordSet) -> ResolutionEvidence:
    """Stamp a decision with the exact identity it was resolved for."""
    return ResolutionEvidence(
        origin=origin,
        reliable=True,
        receiver_id=record_set.receiver_id,
        endpoint_fingerprint=record_set.endpoint_fingerprint,
    )


def _distinct(modes: tuple[Size, ...]) -> tuple[Size, ...]:
    """Reported modes in receiver order, without duplicates."""
    distinct: list[Size] = []
    for mode in modes:
        if mode not in distinct:
            distinct.append(mode)
    return tuple(distinct)


def _reliable_modes(
    discovered: ReceiverCapabilities | None,
    queried: ReceiverCapabilities | None,
) -> tuple[str, tuple[Size, ...]] | None:
    """The first reliable, non-empty capability report, with its origin.

    An unreliable report is not evidence: it neither selects a mode nor becomes
    an option, because a picker full of untrusted sizes would invite the user to
    confirm a guess. A reliable report that names nothing is equally no
    evidence, so the next source may still answer.
    """
    sources = (discovered, queried)
    for origin, capabilities in zip(_CAPABILITY_ORIGINS, sources):
        if capabilities is None or not capabilities.reliable:
            continue
        modes = _distinct(capabilities.modes)
        if modes:
            return origin, modes
    return None


def _remembered(
    record_set: ReceiverRecordSet, saved: SavedReceiver | None,
) -> ResolutionDecision | None:
    """The remembered decision this identity may use, or ``None``.

    ``PreferenceAccess`` is the single continuity authority: it withholds an
    ambiguous, non-persistable, replaced, or unverified-fallback record even when
    the receiver ID hash is unchanged. The remembered decision must also name the
    identity it is stored for; a record whose evidence points at another receiver
    is malformed and cannot authorize a size.
    """
    if saved is None:
        return None
    authorized = PreferenceAccess.for_record_set(record_set, saved)
    if authorized is None:
        return None
    remembered = authorized.remembered_resolution
    if remembered is None:
        return None
    if remembered.evidence.receiver_id != record_set.receiver_id:
        return None
    return remembered


def resolve_resolution(
    record_set: ReceiverRecordSet,
    saved: SavedReceiver | None,
    discovered: ReceiverCapabilities | None,
    queried: ReceiverCapabilities | None,
    user: Size | None = None,
) -> ResolutionDecision | ResolutionRequired:
    """Resolve the virtual-output mode, or require an explicit decision.

    An explicit user choice is the current decision and outranks memory and
    reports. Otherwise a mode remembered for the same continuous identity wins;
    one reliable advertised/queried mode is used with its exact evidence; every
    other case returns :class:`ResolutionRequired`.
    """
    if user is not None:
        if not isinstance(user, Size):
            raise TypeError("an explicit user resolution must be a Size")
        return ResolutionDecision(
            size=user, source="user", evidence=_evidence(_PICKER_ORIGIN, record_set),
        )

    had_remembered = saved is not None and saved.remembered_resolution is not None
    remembered = _remembered(record_set, saved)
    rejected = had_remembered and remembered is None
    reliable = _reliable_modes(discovered, queried)

    if remembered is not None:
        # The design's precedence is explicit: a mode remembered for this same
        # stable identity wins over reports, because it was confirmed by an
        # earlier successful setup. A receiver that now accepts a different mode
        # is handled after setup by ``accepted_mode_mismatch`` -- never by
        # silently changing the remembered choice here.
        return ResolutionDecision(
            size=remembered.size,
            source="stored",
            evidence=_evidence(_REMEMBERED_ORIGIN, record_set),
        )

    if reliable is None:
        return ResolutionRequired(choices=(), remembered_rejected=rejected)

    origin, modes = reliable
    if len(modes) == 1:
        return ResolutionDecision(
            size=modes[0],
            source="discovery",
            evidence=_evidence(origin, record_set),
        )

    return ResolutionRequired(choices=modes, remembered_rejected=rejected)


def _rememberable(record_set: ReceiverRecordSet) -> bool:
    """Whether a preference may be keyed by this identity at all."""
    if is_ambiguous_identity(record_set.receiver_id):
        return False
    if record_set.ambiguous or record_set.identity_kind == "ambiguous":
        return False
    if not record_set.persistable or record_set.replacement_of is not None:
        return False
    if record_set.identity_kind == "device":
        # A stable device identifier is its own continuity evidence; it survives
        # a compatible service/port/host change, as in ``PreferenceAccess``.
        return True
    # A fallback identity is a hash of mutable advertisement data, so only a
    # verified continuity claim may attach a durable preference to it.
    return record_set.continuity_verified


def remember_after_success(
    record_set: ReceiverRecordSet,
    decision: ResolutionDecision,
    observed: Size | None,
) -> ResolutionDecision | None:
    """The decision to persist after setup, or ``None`` when it is not proven.

    Call this only after the setup succeeded; it still demands positive evidence
    that the decided size was the one that appeared. ``observed`` is that
    evidence: ``None`` means the output's size was never confirmed, and a
    different size means the decision was not realized (the accepted-mode
    mismatch path owns that case). Nothing is remembered for an ambiguous,
    non-persistable, replaced, unverified-fallback, or foreign identity.
    """
    if observed is None:
        return None
    if not isinstance(observed, Size):
        raise TypeError("an observed resolution must be a Size")
    if observed != decision.size:
        return None
    if not _rememberable(record_set):
        return None
    if decision.evidence.receiver_id != record_set.receiver_id:
        return None
    return replace(decision, evidence=_evidence(_VERIFIED_ORIGIN, record_set))


def accepted_mode_mismatch(
    decision: ResolutionDecision, accepted: Size, reliable: bool,
) -> bool:
    """Whether a reliable accepted mode differs from the resolved decision.

    Only a reliable accepted size is evidence. An unreliable or advertised-only
    value cannot invalidate the output, and policy never changes it: the session
    stops the stream, retains the output, and offers an explicit recreate/stop.
    """
    return bool(reliable) and accepted != decision.size


__all__ = [
    "ResolutionRequired",
    "accepted_mode_mismatch",
    "remember_after_success",
    "resolve_resolution",
]
