"""Versioned JSON codecs with explicit field allowlists.

Only the fields named in this module are ever written, and a document whose key
set differs from that allowlist is refused in both directions. That is the whole
point: ``asdict``, ``pickle`` or a generic projection would serialize whatever a
DTO happens to hold, including a live pairing challenge or a capture token that
must never reach durable metadata. Here every field is enumerated, and the ones
the design keeps transient are simply absent.

Three rules shape the decoders:

* an unknown version is refused rather than interpreted, so a future format can
  never be read as this one;
* a missing field is refused rather than defaulted, so a truncated or hand-edited
  record cannot silently acquire a zero generation or an idle resource state;
* a leaf must have the exact type the domain's own constructors require, so
  ``true`` is never accepted as a generation and ``"3"`` is never accepted as an
  integer.

Two pieces of state are deliberately *derived* rather than stored. A stored
physical source keeps its connector but drops the portal restore token and the
capture lease, which belong to the capture adapter's private runtime memory. A
stored error keeps its typed code and receiver, and its message and recovery
actions are rebuilt from the diagnostics catalog on load, so backend text has no
path into metadata and no path out of it. A saved receiver likewise keeps every
non-secret preference and no credential reference: the committed reference is
re-derived from the keyring adapter by receiver id, per the decided resolution
recorded as :data:`CREDENTIAL_REFERENCE_CONFLICT`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, NamedTuple, NoReturn, TypeVar
from uuid import UUID

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import (
    ReceiverId,
    ReceiverTarget,
    ServiceRecord,
)
from airplay_linux.domain.operations import (
    DisplayHandle,
    InstanceId,
    LeaseId,
    OperationId,
    OwnerId,
    ProcessIdentity,
    StreamHandle,
)
from airplay_linux.domain.records import (
    AuthorityOwner,
    DaemonCleanupIntent,
    DaemonReservation,
    LaunchReceipt,
    ProbeVerdict,
    SupervisorReleaseIntent,
    DaemonRecord,
    LayoutSnapshot,
    OwnershipRecord,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    AudioState,
    Diagnostic,
    DisplayState,
    ErrorCode,
    IntentStage,
    Mode,
    PhysicalMonitorSource,
    ResolutionDecision,
    ResolutionEvidence,
    SessionPhase,
    Size,
    StartRequest,
    TransportState,
    TrustState,
    VirtualOutputSource,
)

#: Default document version; receiver preferences have their own v2 writer.
VERSION: Final[int] = 1

SESSION_SCHEMA: Final[str] = "airplay-linux/session"
OWNERSHIP_SCHEMA: Final[str] = "airplay-linux/ownership"
DAEMON_SCHEMA: Final[str] = "airplay-linux/daemon"
RECEIVER_SCHEMA: Final[str] = "airplay-linux/receiver"

_ACCEPTED_VERSIONS = {
    SESSION_SCHEMA: frozenset({1}), OWNERSHIP_SCHEMA: frozenset({1}),
    DAEMON_SCHEMA: frozenset({1}), RECEIVER_SCHEMA: frozenset({1, 2}),
}
_WRITE_VERSIONS = {RECEIVER_SCHEMA: 2}

#: The design's reservation stages, exactly.
OWNERSHIP_STAGES: Final[frozenset[str]] = frozenset(
    {"reserved", "helper_started", "output_observed", "published", "removing"},
)
DESIRED_STATES: Final[frozenset[str]] = frozenset({"active", "removing"})
DAEMON_LIFECYCLES: Final[frozenset[str]] = frozenset({"starting", "running", "stopping"})
RESOLUTION_SOURCES: Final[frozenset[str]] = frozenset({"stored", "discovery", "user"})
SERVICE_TYPES: Final[frozenset[str]] = frozenset({"_airplay._tcp", "_raop._tcp"})

#: A public receiver identity, using the design's four derived forms. This is a
#: re-statement of the identity grammar, not the diagnostics boundary's: storage
#: owns its own record validation, and a durable receiver key must never accept
#: free-form text a secret could ride in.
_RECEIVER_ID: Final[re.Pattern[str]] = re.compile(
    r"airplay-(?:device:[0-9a-z][0-9a-z-]{0,63}"
    r"|(?:fingerprint|endpoint|ambiguous):[0-9a-f]{64})",
)

AMBIGUOUS_PREFIX: Final[str] = "airplay-ambiguous:"

#: Bounds that keep one record from becoming an unbounded payload.
MAX_TEXT: Final[int] = 4096
MAX_ITEMS: Final[int] = 512
MAX_LAYOUT_PROPERTIES: Final[int] = 128
MAX_IDENTIFIER: Final[int] = 256


class RecordError(Exception):
    """A record that could not be encoded or decoded, tagged with a reason.

    ``reason`` is one of a closed set of source-written tokens: ``malformed_json``,
    ``not_an_object``, ``unknown_schema``, ``unknown_version``, ``unknown_field``,
    ``missing_field``, ``invalid_value``, or ``unsupported_credential_ref``. It
    never carries the offending value, so a quarantined record's diagnostics
    cannot echo what corrupted it.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"record rejected: {reason}")

    def __repr__(self) -> str:
        return f"RecordError(reason={self.reason!r})"


def _invalid() -> NoReturn:
    raise RecordError("invalid_value")


def is_ambiguous_identity(receiver_id: object) -> bool:
    """True when this identity is an ephemeral, non-persistable candidate."""
    return type(receiver_id) is str and receiver_id.startswith(AMBIGUOUS_PREFIX)


# --------------------------------------------------------------------------- leaves


def _text(value: object, *, allow_empty: bool = True) -> str:
    if type(value) is not str or "\x00" in value or len(value) > MAX_TEXT:
        _invalid()
    if not allow_empty and not value:
        _invalid()
    return value


def _identifier(value: object) -> str:
    text = _text(value, allow_empty=False)
    if len(text) > MAX_IDENTIFIER:
        _invalid()
    return text


def _receiver(value: object) -> ReceiverId:
    text = _identifier(value)
    if is_ambiguous_identity(text) or _RECEIVER_ID.fullmatch(text) is None:
        _invalid()
    return ReceiverId(text)


def _record_receiver(value: object) -> ReceiverId:
    """A receiver identity as it may be persisted: never the ambiguous form."""
    return _receiver(value)


def _flag(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _integer(value: object, *, low: int = 0, high: int = 2**63 - 1) -> int:
    if type(value) is not int:
        _invalid()
    if not low <= value <= high:
        _invalid()
    return value


def _number(value: object) -> float:
    if type(value) not in (int, float):
        _invalid()
    try:
        # A JSON integer is unbounded, so ``10**400`` is a perfectly legal
        # document that floats cannot represent. That is a rejected value, not an
        # exception escaping the codec: one out-of-range number must quarantine
        # its own record instead of aborting a whole load.
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        _invalid()
    if not math.isfinite(number):
        _invalid()
    return number


def _identifier_uuid(value: object) -> UUID:
    """A canonical UUID: a real ``UUID`` when dumping, its exact text when loading."""
    if isinstance(value, UUID):
        return value
    text = _text(value, allow_empty=False)
    try:
        parsed = UUID(text)
    except (ValueError, AttributeError, TypeError):
        _invalid()
    if str(parsed) != text.lower():
        _invalid()
    return parsed


def _uuid_text(value: object) -> str:
    """The canonical text form of a UUID-valued domain field."""
    return str(_identifier_uuid(value))


class _Codec(NamedTuple):
    """One field's JSON projection in both directions."""

    dump: Callable[[object], object]
    load: Callable[[object], object]


def _plain(step: Callable[[object], object]) -> _Codec:
    """A codec whose dump and load are the same validated normalization."""
    return _Codec(dump=step, load=step)


def _nullable(codec: _Codec) -> _Codec:
    def dump(value: object) -> object:
        return None if value is None else codec.dump(value)

    def load(value: object) -> object:
        return None if value is None else codec.load(value)

    return _Codec(dump=dump, load=load)


def _sequence(codec: _Codec) -> _Codec:
    def steps(values: object) -> object:
        # A JSON array arrives as a list; the domain field is a tuple. The length
        # bound keeps one record from becoming an unbounded payload.
        if type(values) is not list or len(values) > MAX_ITEMS:
            _invalid()
        return tuple(codec.load(item) for item in values)

    def writes(values: object) -> object:
        if type(values) is not tuple or len(values) > MAX_ITEMS:
            _invalid()
        return [codec.dump(item) for item in values]

    return _Codec(dump=writes, load=steps)


def _enum(table: Mapping[str, object]) -> _Codec:
    """A closed enum vocabulary: values out, members in, and nothing in between.

    Membership is decided on the member's own ``value`` text, so an unknown or
    invented spelling is refused instead of being coerced. ``dump`` recognises a
    member by identity rather than by equality, so only a genuine member of the
    frozen enumeration can be written.
    """
    member_of = dict(table)
    value_of = {id(member): value for value, member in table.items()}

    def dump(value: object) -> object:
        known = value_of.get(id(value))
        if known is None:
            _invalid()
        return known

    def load(value: object) -> object:
        if type(value) is not str:
            _invalid()
        member = member_of.get(value)
        if member is None:
            _invalid()
        return member

    return _Codec(dump=dump, load=load)


def _frozen(values: frozenset[str]) -> Callable[[object], str]:
    def step(value: object) -> str:
        if type(value) is not str or value not in values:
            _invalid()
        return value

    return step


def _enum_table(cls: type) -> Mapping[str, object]:
    """The members of ``cls`` keyed by their exact value, captured once."""
    return {member.value: member for member in cls}  # type: ignore[attr-defined]


_MODE = _enum(_enum_table(Mode))
_PHASE = _enum(_enum_table(SessionPhase))
_TRANSPORT = _enum(_enum_table(TransportState))
_DISPLAY = _enum(_enum_table(DisplayState))
_AUDIO = _enum(_enum_table(AudioState))
_TRUST = _enum(_enum_table(TrustState))
_STAGE = _enum(_enum_table(IntentStage))
_ERROR_CODE = _enum(_enum_table(ErrorCode))
_OWNERSHIP_STAGE = _plain(_frozen(OWNERSHIP_STAGES))
_DESIRED_STATE = _plain(_frozen(DESIRED_STATES))
_LIFECYCLE = _plain(_frozen(DAEMON_LIFECYCLES))
_RESOLUTION_SOURCE = _plain(_frozen(RESOLUTION_SOURCES))
_SERVICE_TYPE = _plain(_frozen(SERVICE_TYPES))
_UUID = _plain(_uuid_text)


# --------------------------------------------------------------------------- nested DTOs


def _object(value: object, fields: tuple[tuple[str, _Codec], ...]) -> dict[str, object]:
    """Load a nested object with an exact key set, or refuse it."""
    if type(value) is not dict:
        _invalid()
    expected = {name for name, _ in fields}
    if set(value) != expected:
        _invalid()
    return {name: codec.load(value[name]) for name, codec in fields}


def _fields_of(record: object, fields: tuple[tuple[str, _Codec], ...]) -> dict[str, object]:
    return {name: codec.dump(getattr(record, name)) for name, codec in fields}


def _construct(factory: Callable[..., object], values: dict[str, object]) -> object:
    try:
        return factory(**values)
    except (TypeError, ValueError):
        _invalid()


_SIZE_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("width", _plain(lambda value: _integer(value, low=1, high=16384))),
    ("height", _plain(lambda value: _integer(value, low=1, high=16384))),
)


def _dump_size(value: object) -> object:
    if not isinstance(value, Size):
        _invalid()
    return _fields_of(value, _SIZE_FIELDS)


def _load_size(value: object) -> Size:
    return _construct(Size, _object(value, _SIZE_FIELDS))  # type: ignore[return-value]


SizeCodec = _Codec(dump=_dump_size, load=_load_size)

_PROCESS_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("pid", _plain(lambda value: _integer(value, low=0, high=2**31 - 1))),
    ("start_ticks", _plain(lambda value: _integer(value, low=0))),
)


def _dump_process(value: object) -> object:
    if not isinstance(value, ProcessIdentity):
        _invalid()
    return _fields_of(value, _PROCESS_FIELDS)


def _load_process(value: object) -> ProcessIdentity:
    return _construct(  # type: ignore[return-value]
        ProcessIdentity, _object(value, _PROCESS_FIELDS),
    )


ProcessCodec = _Codec(dump=_dump_process, load=_load_process)

_SIZE_OR_NONE = _nullable(SizeCodec)


def _properties(value: object) -> dict[str, str | int | bool]:
    """One output's flat property map: a bounded set of primitive shapes."""
    if type(value) is not dict or len(value) > MAX_LAYOUT_PROPERTIES:
        _invalid()
    clean: dict[str, str | int | bool] = {}
    for key, item in value.items():
        name = _identifier(key)
        if type(item) is bool:
            clean[name] = item
        elif type(item) is int:
            clean[name] = _integer(item, low=-(2**31), high=2**31 - 1)
        elif type(item) is str:
            clean[name] = _text(item)
        else:
            _invalid()
    return dict(sorted(clean.items()))


def _dump_properties(value: object) -> object:
    if not isinstance(value, Mapping):
        _invalid()
    return _properties(dict(value))


PropertiesCodec = _Codec(dump=_dump_properties, load=_properties)


def _dump_outputs(value: object) -> object:
    if not isinstance(value, Mapping) or len(value) > MAX_ITEMS:
        _invalid()
    return {
        name: _dump_properties(properties)
        for name, properties in sorted(value.items(), key=lambda item: _identifier(item[0]))
    }


def _load_outputs(value: object) -> Mapping[str, Mapping[str, str | int | bool]]:
    if type(value) is not dict or len(value) > MAX_ITEMS:
        _invalid()
    return {
        _identifier(name): _properties(properties)
        for name, properties in value.items()
    }


OutputsCodec = _Codec(dump=_dump_outputs, load=_load_outputs)

_LAYOUT_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("outputs", OutputsCodec),
    ("baseline_hash", _plain(_identifier)),
)


def _dump_layout(value: object) -> object:
    if not isinstance(value, LayoutSnapshot):
        _invalid()
    return _fields_of(value, _LAYOUT_FIELDS)


def _load_layout(value: object) -> LayoutSnapshot:
    return _construct(  # type: ignore[return-value]
        LayoutSnapshot, _object(value, _LAYOUT_FIELDS),
    )


LayoutCodec = _Codec(dump=_dump_layout, load=_load_layout)


def _dump_txt(value: object) -> object:
    if not isinstance(value, Mapping) or len(value) > MAX_ITEMS:
        _invalid()
    return {_identifier(key): _text(item) for key, item in sorted(value.items())}


def _load_txt(value: object) -> Mapping[str, str]:
    if type(value) is not dict or len(value) > MAX_ITEMS:
        _invalid()
    return {_identifier(key): _text(item) for key, item in value.items()}


TxtCodec = _Codec(dump=_dump_txt, load=_load_txt)

_SERVICE_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("key", _plain(_identifier)),
    ("service_type", _SERVICE_TYPE),
    ("instance", _plain(_text)),
    ("model", _plain(_text)),
    ("device_id", _nullable(_plain(_text))),
    ("host", _plain(_text)),
    ("addresses", _sequence(_plain(_text))),
    ("port", _plain(lambda value: _integer(value, low=0, high=65535))),
    ("txt", TxtCodec),
    ("seen_at", _plain(_number)),
)


def _dump_service(value: object) -> object:
    if not isinstance(value, ServiceRecord):
        _invalid()
    return _fields_of(value, _SERVICE_FIELDS)


def _load_service(value: object) -> ServiceRecord:
    return _construct(  # type: ignore[return-value]
        ServiceRecord, _object(value, _SERVICE_FIELDS),
    )


ServiceCodec = _Codec(dump=_dump_service, load=_load_service)

_TARGET_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("receiver_id", _plain(_record_receiver)),
    ("service_records", _sequence(ServiceCodec)),
    ("canonical_host", _plain(_text)),
    ("service_port", _plain(lambda value: _integer(value, low=0, high=65535))),
    ("protocol", _plain(_identifier)),
    ("endpoint_fingerprint", _plain(_identifier)),
    ("endpoint_generation", _plain(_integer)),
)


def _dump_target(value: object) -> object:
    if not isinstance(value, ReceiverTarget):
        _invalid()
    return _fields_of(value, _TARGET_FIELDS)


def _load_target(value: object) -> ReceiverTarget:
    return _construct(  # type: ignore[return-value]
        ReceiverTarget, _object(value, _TARGET_FIELDS),
    )


TargetCodec = _Codec(dump=_dump_target, load=_load_target)

_VIRTUAL_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("connector", _plain(_identifier)),
    ("kscreen_output_id", _plain(_identifier)),
    ("size", SizeCodec),
)


def _dump_virtual(value: object) -> object:
    if not isinstance(value, VirtualOutputSource):
        _invalid()
    return _fields_of(value, _VIRTUAL_FIELDS)


def _load_virtual(value: object) -> VirtualOutputSource:
    return _construct(  # type: ignore[return-value]
        VirtualOutputSource, _object(value, _VIRTUAL_FIELDS),
    )


VirtualCodec = _Codec(dump=_dump_virtual, load=_load_virtual)

_HANDLE_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("owner_uuid", _UUID),
    ("source", VirtualCodec),
    ("helper", ProcessCodec),
)


def _dump_handle(value: object) -> object:
    if not isinstance(value, DisplayHandle):
        _invalid()
    return _fields_of(value, _HANDLE_FIELDS)


def _load_handle(value: object) -> DisplayHandle:
    fields = _object(value, _HANDLE_FIELDS)
    fields["owner_uuid"] = OwnerId(UUID(str(fields["owner_uuid"])))
    return _construct(DisplayHandle, fields)  # type: ignore[return-value]


HandleCodec = _Codec(dump=_dump_handle, load=_load_handle)

_LEGACY_STREAM_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("receiver_id", _plain(_record_receiver)),
    ("endpoint_generation", _plain(_integer)),
    ("endpoint_fingerprint", _plain(_identifier)),
    ("backend_stream_id", _plain(_identifier)),
    ("daemon_instance_uuid", _UUID),
    ("lease_id", _UUID),
    ("start_operation_id", _UUID),
)
_STREAM_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    *_LEGACY_STREAM_FIELDS,
    ("source_connector", _nullable(_plain(_identifier))),
)


def _dump_stream(value: object) -> object:
    if not isinstance(value, StreamHandle):
        _invalid()
    return _fields_of(value, _STREAM_FIELDS)


def _load_stream(value: object) -> StreamHandle:
    if type(value) is dict and set(value) == {name for name, _ in _LEGACY_STREAM_FIELDS}:
        fields = _object(value, _LEGACY_STREAM_FIELDS)
        fields["source_connector"] = None
    else:
        fields = _object(value, _STREAM_FIELDS)
    fields["daemon_instance_uuid"] = InstanceId(UUID(str(fields["daemon_instance_uuid"])))
    fields["lease_id"] = LeaseId(UUID(str(fields["lease_id"])))
    fields["start_operation_id"] = OperationId(UUID(str(fields["start_operation_id"])))
    return _construct(StreamHandle, fields)  # type: ignore[return-value]


StreamCodec = _Codec(dump=_dump_stream, load=_load_stream)

_EVIDENCE_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("origin", _plain(_identifier)),
    ("reliable", _plain(_flag)),
    ("receiver_id", _plain(_record_receiver)),
    ("endpoint_fingerprint", _nullable(_plain(_identifier))),
)


def _dump_evidence(value: object) -> object:
    if not isinstance(value, ResolutionEvidence):
        _invalid()
    return _fields_of(value, _EVIDENCE_FIELDS)


def _load_evidence(value: object) -> ResolutionEvidence:
    return _construct(  # type: ignore[return-value]
        ResolutionEvidence, _object(value, _EVIDENCE_FIELDS),
    )


EvidenceCodec = _Codec(dump=_dump_evidence, load=_load_evidence)

_DECISION_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("size", SizeCodec),
    ("source", _RESOLUTION_SOURCE),
    ("evidence", EvidenceCodec),
)


def _dump_decision(value: object) -> object:
    if not isinstance(value, ResolutionDecision):
        _invalid()
    return _fields_of(value, _DECISION_FIELDS)


def _load_decision(value: object) -> ResolutionDecision:
    return _construct(  # type: ignore[return-value]
        ResolutionDecision, _object(value, _DECISION_FIELDS),
    )


DecisionCodec = _Codec(dump=_dump_decision, load=_load_decision)

_AUDIO_POLICY_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("enabled", _plain(_flag)),
)


def _dump_audio_policy(value: object) -> object:
    if not isinstance(value, AudioPolicy):
        _invalid()
    return _fields_of(value, _AUDIO_POLICY_FIELDS)


def _load_audio_policy(value: object) -> AudioPolicy:
    return _construct(  # type: ignore[return-value]
        AudioPolicy, _object(value, _AUDIO_POLICY_FIELDS),
    )


AudioPolicyCodec = _Codec(dump=_dump_audio_policy, load=_load_audio_policy)

#: A stored physical source keeps its connector and nothing else. The portal
#: restore token and the capture lease stay in the capture adapter's private
#: runtime memory; a durable record cannot leak them or resurrect them.
_PHYSICAL_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("connector", _plain(_identifier)),
)


def _dump_physical(value: object) -> object:
    if not isinstance(value, PhysicalMonitorSource):
        _invalid()
    return _fields_of(value, _PHYSICAL_FIELDS)


def _load_physical(value: object) -> PhysicalMonitorSource:
    fields = _object(value, _PHYSICAL_FIELDS)
    return PhysicalMonitorSource(
        connector=str(fields["connector"]),
        portal_restore_token=None,
        capture_lease_id=None,
    )


PhysicalCodec = _Codec(dump=_dump_physical, load=_load_physical)

_START_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("mode", _MODE),
    ("source", _nullable(PhysicalCodec)),
    ("resolution", _nullable(DecisionCodec)),
    ("audio", AudioPolicyCodec),
)


def _dump_start(value: object) -> object:
    if not isinstance(value, StartRequest):
        _invalid()
    return _fields_of(value, _START_FIELDS)


def _load_start(value: object) -> StartRequest:
    return _construct(  # type: ignore[return-value]
        StartRequest, _object(value, _START_FIELDS),
    )


StartCodec = _Codec(dump=_dump_start, load=_load_start)

#: A stored error is its typed code plus its receiver. Message and recovery
#: actions are catalog copy, rebuilt on load, so no free-form text is durable.
_ERROR_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("code", _ERROR_CODE),
    ("receiver_id", _nullable(_plain(_record_receiver))),
)


def _dump_error(value: object) -> object:
    if not isinstance(value, Diagnostic):
        _invalid()
    return _fields_of(value, _ERROR_FIELDS)


def _load_error(value: object) -> Diagnostic:
    fields = _object(value, _ERROR_FIELDS)
    try:
        return diagnostic(  # type: ignore[arg-type]
            fields["code"], fields["receiver_id"],
        )
    except (TypeError, ValueError):
        _invalid()


ErrorCodec = _Codec(dump=_dump_error, load=_load_error)


# --------------------------------------------------------------------------- documents


def _dump_document(schema: str, values: dict[str, object]) -> bytes:
    document = {"schema": schema, "version": _WRITE_VERSIONS.get(schema, VERSION), **values}
    try:
        text = json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        _invalid()
    return text.encode("ascii")


def _reject_constant(token: str) -> NoReturn:
    """``NaN``/``Infinity`` are not JSON; a record holding one is malformed."""
    raise RecordError("malformed_json")


def _load_document(payload: object, schema: str, fields: tuple[tuple[str, _Codec], ...]) -> dict[str, object]:
    if type(payload) is not bytes:
        raise RecordError("malformed_json")
    try:
        text = payload.decode("utf-8")
        document = json.loads(text, parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RecordError("malformed_json") from None
    if type(document) is not dict:
        raise RecordError("not_an_object")
    if document.get("schema") != schema:
        raise RecordError("unknown_schema")
    version = document.get("version")
    if type(version) is not int or version not in _ACCEPTED_VERSIONS[schema]:
        raise RecordError("unknown_version")
    if schema == RECEIVER_SCHEMA and version == 2:
        fields = _RECEIVER_V2_FIELDS
    expected = {name for name, _ in fields} | {"schema", "version"}
    present = set(document)
    if present - expected:
        raise RecordError("unknown_field")
    if expected - present:
        raise RecordError("missing_field")
    return {name: codec.load(document[name]) for name, codec in fields}


# --------------------------------------------------------------------------- session

_SESSION_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("receiver_id", _plain(_record_receiver)),
    ("target", _nullable(TargetCodec)),
    ("online", _plain(_flag)),
    ("mode", _nullable(_MODE)),
    ("intent_active", _plain(_flag)),
    ("intent_stage", _STAGE),
    ("phase", _PHASE),
    ("transport", _TRANSPORT),
    ("display", _DISPLAY),
    ("audio", _AUDIO),
    ("trust", _TRUST),
    ("physical_source", _nullable(PhysicalCodec)),
    ("display_handle", _nullable(HandleCodec)),
    ("ownership_uuid", _nullable(_UUID)),
    ("resolution", _nullable(DecisionCodec)),
    ("observed_resolution", _SIZE_OR_NONE),
    ("accepted_resolution", _SIZE_OR_NONE),
    ("audio_policy", AudioPolicyCodec),
    ("muted", _plain(_flag)),
    ("stream", _nullable(StreamCodec)),
    ("last_error", _nullable(ErrorCodec)),
    ("reconnect_attempt", _plain(_integer)),
    ("next_retry_at", _nullable(_plain(_number))),
    ("operation_id", _nullable(_UUID)),
    ("session_generation", _plain(_integer)),
    ("transport_lease_id", _nullable(_UUID)),
    ("trust_generation", _plain(_integer)),
    ("audio_operation_id", _nullable(_UUID)),
    ("queued_start", _nullable(StartCodec)),
    ("cleanup_pending", _plain(_flag)),
)


def encode_session(snapshot: SessionSnapshot) -> bytes:
    """The durable projection of one session snapshot; never its challenge."""
    if not isinstance(snapshot, SessionSnapshot):
        _invalid()
    return _dump_document(SESSION_SCHEMA, _fields_of(snapshot, _SESSION_FIELDS))


def decode_session(payload: bytes) -> SessionSnapshot:
    """Rebuild a snapshot, refusing any document this version does not define."""
    values = _load_document(payload, SESSION_SCHEMA, _SESSION_FIELDS)
    values["ownership_uuid"] = _optional_uuid(OwnerId, values["ownership_uuid"])
    values["operation_id"] = _optional_uuid(OperationId, values["operation_id"])
    values["transport_lease_id"] = _optional_uuid(LeaseId, values["transport_lease_id"])
    values["audio_operation_id"] = _optional_uuid(OperationId, values["audio_operation_id"])
    return _construct(SessionSnapshot, values)  # type: ignore[return-value]


def _optional_uuid(wrapper: Callable[[UUID], object], value: object) -> object:
    return None if value is None else wrapper(UUID(str(value)))


# --------------------------------------------------------------------------- ownership

_OWNERSHIP_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("schema_version", _plain(lambda value: _integer(value, low=VERSION, high=VERSION))),
    ("owner_uuid", _UUID),
    ("app_instance_uuid", _UUID),
    ("runtime_directory", _plain(_text)),
    ("creation_operation_id", _UUID),
    ("receiver_id", _plain(_record_receiver)),
    ("endpoint_fingerprint", _plain(_identifier)),
    ("desired_state", _DESIRED_STATE),
    ("stage", _OWNERSHIP_STAGE),
    ("requested_size", SizeCodec),
    ("observed_size", _SIZE_OR_NONE),
    ("helper", _nullable(ProcessCodec)),
    ("connector", _nullable(_plain(_identifier))),
    ("output_id", _nullable(_plain(_identifier))),
    ("created_utc", _plain(_identifier)),
    ("last_configuration_step", _plain(_text)),
    ("baseline", LayoutCodec),
    ("last_applied", OutputsCodec),
    ("provider_version", _plain(_text)),
    ("capability_fingerprint", _plain(_identifier)),
)


def encode_ownership(record: OwnershipRecord) -> bytes:
    """The durable ownership record; its stage and desired state are closed sets."""
    if not isinstance(record, OwnershipRecord):
        _invalid()
    return _dump_document(OWNERSHIP_SCHEMA, _fields_of(record, _OWNERSHIP_FIELDS))


def decode_ownership(payload: bytes) -> OwnershipRecord:
    values = _load_document(payload, OWNERSHIP_SCHEMA, _OWNERSHIP_FIELDS)
    values["owner_uuid"] = OwnerId(UUID(str(values["owner_uuid"])))
    values["app_instance_uuid"] = InstanceId(UUID(str(values["app_instance_uuid"])))
    values["creation_operation_id"] = OperationId(UUID(str(values["creation_operation_id"])))
    return _construct(OwnershipRecord, values)  # type: ignore[return-value]


# --------------------------------------------------------------------------- daemon

_DAEMON_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("schema_version", _plain(lambda value: _integer(value, low=VERSION, high=VERSION))),
    ("instance_uuid", _UUID),
    ("owner_token", _plain(_identifier)),
    ("app", ProcessCodec),
    ("supervisor", _nullable(ProcessCodec)),
    ("daemon", _nullable(ProcessCodec)),
    ("protocol_version", _plain(_identifier)),
    ("socket_path", _plain(_text)),
    ("lifecycle", _LIFECYCLE),
    ("runtime_directory", _plain(_text)),
)


def encode_daemon(record: DaemonRecord) -> bytes:
    """The runtime daemon record; written under the instance's private directory."""
    if not isinstance(record, DaemonRecord):
        _invalid()
    return _dump_document(DAEMON_SCHEMA, _fields_of(record, _DAEMON_FIELDS))


def decode_daemon(payload: bytes) -> DaemonRecord:
    values = _load_document(payload, DAEMON_SCHEMA, _DAEMON_FIELDS)
    values["instance_uuid"] = InstanceId(UUID(str(values["instance_uuid"])))
    return _construct(DaemonRecord, values)  # type: ignore[return-value]


# --------------------------------------------------------------------------- receiver

_RECEIVER_FIELDS: Final[tuple[tuple[str, _Codec], ...]] = (
    ("receiver_id", _plain(_record_receiver)),
    ("service_metadata", _sequence(ServiceCodec)),
    ("display_name", _plain(_text)),
    ("last_endpoint", _nullable(TargetCodec)),
    ("continuity_fingerprint", _plain(_identifier)),
    ("remembered_resolution", _nullable(DecisionCodec)),
)


_RECEIVER_V2_FIELDS = _RECEIVER_FIELDS + (
    ("remembered_volume", _Codec(
        dump=lambda value: None if value is None else _integer(value, low=1, high=100),
        load=lambda value: value,
    )),
)


def encode_receiver(saved: SavedReceiver) -> bytes:
    """Non-secret receiver preferences, including resolution and positive volume.

    ``credential_ref`` is deliberately *not* representable in version 1 of this
    record, and a saved receiver that still holds one is refused rather than
    silently stripped. This is the enforced form of a decided resolution, not a
    gap: :data:`CREDENTIAL_REFERENCE_CONFLICT` records the ruling, which follows
    the approved design -- the credential adapter is the owner of the committed
    reference and hands it back through ``load_credential_ref(receiver_id)``,
    while secrets and provisional credentials stay inside the backend/keyring
    adapter and are never copied into application metadata. A
    ``SavedReceiver.credential_ref`` is therefore an in-memory convenience the
    ``CredentialStore`` populates, and storage's job is everything else in the
    record. The frozen ``OpaqueCredentialRef`` also exposes no representation, so
    the refusal closes the only other path: no private value is serialized.
    """
    if not isinstance(saved, SavedReceiver):
        _invalid()
    if is_ambiguous_identity(saved.receiver_id):
        _invalid()
    if saved.credential_ref is not None:
        raise RecordError(CREDENTIAL_REFERENCE_CONFLICT.reason)
    return _dump_document(RECEIVER_SCHEMA, _fields_of(saved, _RECEIVER_V2_FIELDS))


def decode_receiver(
    payload: bytes, *, on_diagnostic: Callable[[Diagnostic], None] | None = None,
) -> SavedReceiver:
    values = _load_document(payload, RECEIVER_SCHEMA, _RECEIVER_FIELDS)
    # Only this leaf is droppable, after all structural and old-member checks.
    volume = values.get("remembered_volume")
    invalid_volume = volume is not None and (type(volume) is not int or not 1 <= volume <= 100)
    values["remembered_volume"] = None if invalid_volume else volume
    # Credentials are always re-derived from the keyring, never metadata.
    values["credential_ref"] = None
    saved = _construct(SavedReceiver, values)
    if invalid_volume and on_diagnostic is not None:
        try:
            on_diagnostic(diagnostic(ErrorCode.volume_preference_invalid, saved.receiver_id))
        except Exception:
            pass
    return saved  # type: ignore[return-value]


# -------------------------------------------------------------- recorded resolutions


@dataclass(frozen=True, kw_only=True)
class _ResolvedInterfaceConflict:
    """A decided boundary ruling recorded beside the code that enforces it.

    This is not an open finding: a routing decision was made against the approved
    design, and the code now implements it. The record exists so the ruling, its
    authority, and the alternatives it rejected stay readable next to the refusal
    that depends on them -- otherwise a later round sees only a refusal and
    re-opens a question that has already been settled.

    ``history`` keeps what earlier rounds attempted, and ``rejected_alternatives``
    keeps the proposals the ruling declined, so neither can be quietly
    rediscovered as new.
    """

    id: str
    status: str
    producer: str
    consumer: str
    requirement: str
    observed: str
    evidence: str
    impact: str
    reason: str
    resolution: str
    decision_authority: str
    history: str
    rejected_alternatives: tuple[str, ...]
    resolution_owner: str
    forbidden_workarounds: tuple[str, ...]


#: The decided ruling on the committed credential reference: storage does not
#: persist it. Recorded here, beside the refusal that enforces the decision.
CREDENTIAL_REFERENCE_CONFLICT: Final[_ResolvedInterfaceConflict] = _ResolvedInterfaceConflict(
    id="credential-reference-persistence",
    status="resolved",
    producer=(
        "airplay_linux.domain.records.SavedReceiver.credential_ref is typed "
        "OpaqueCredentialRef | None (frozen, foundation lane). "
        "OpaqueCredentialRef subclasses _PrivateValue, which stores its value in a "
        "name-mangled slot and refuses __getstate__/__reduce__/__reduce_ex__ with "
        "TypeError; it defines no accessor, and its repr is '<redacted>'."
    ),
    consumer=(
        "airplay_linux.storage.encode_receiver / JsonReceiverStore.put, which "
        "persists non-secret receiver metadata for ReceiverStore"
    ),
    requirement=(
        "The task brief asks that a receiver record carry an opaque keyring "
        "reference identifier for a committed credential. The design constrains "
        "who owns that reference and where it may live."
    ),
    observed=(
        "Version 1 of the receiver record has no field for a committed reference. "
        "Writing one is impossible without a representation the frozen type does "
        "not expose, and decoding would have to invent a value the document does "
        "not contain, so the encoder refuses the write instead of dropping the "
        "field."
    ),
    evidence=(
        "Reproduce against the frozen domain type: OpaqueCredentialRef('x') has no "
        "public attribute or method other than '_PrivateValue__value'; "
        "__getstate__, __reduce__, and __reduce_ex__ each raise TypeError; "
        "repr() renders 'OpaqueCredentialRef(<redacted>)'; only SensitiveText "
        "exposes reveal_for_backend, and only for transient user input at the "
        "backend boundary. tests/storage/test_atomic_records.py pins each of these "
        "claims, so this record cannot drift from the plant it describes."
    ),
    impact=(
        "A SavedReceiver that still holds an in-memory reference is not a metadata "
        "record: JsonReceiverStore.put returns persistence_failed with detail "
        "'unsupported_credential_ref' and publishes nothing, rather than saving a "
        "record that omits a field the caller passed in. Callers persist "
        "metadata with credential_ref cleared and re-derive the reference on load."
    ),
    reason="unsupported_credential_ref",
    resolution=(
        "Storage does not persist the committed credential reference at all. The "
        "approved design makes the credential adapter the owner of that reference "
        "and gives it the lookup contract "
        "load_credential_ref(receiver_id) -> OpaqueCredentialRef or Missing, keyed "
        "by receiver id rather than carried in the receiver metadata record; it "
        "also requires that secrets and provisional credentials remain inside the "
        "backend/keyring adapter and are 'never copied into application "
        "metadata'. SavedReceiver.credential_ref is therefore an in-memory "
        "convenience the CredentialStore populates when a session needs it, not a "
        "storage field: after a load credential_ref is None and the caller "
        "resolves the reference through CredentialStore.load_credential_ref("
        "receiver_id). Because the record has no such field, encoding a "
        "SavedReceiver whose credential_ref is set is refused with "
        "unsupported_credential_ref rather than silently dropped. That refusal is "
        "the enforced decision. Authority: "
        "docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md (Credential "
        "adapter section; the stored-receiver paragraph stating the application "
        "never stores a PIN or credential in this record), at the approved design "
        "revision a621d8f3cb77fa2fe1cff51d849aeef743fa4716."
    ),
    decision_authority=(
        "docs/superpowers/specs/2026-10-17-airplay-mirroring-design.md"
        "#credential-adapter, approved design revision "
        "a621d8f3cb77fa2fe1cff51d849aeef743fa4716; adjudicated by the run "
        "controller after fix rounds 1 and 2 escalated the conflict instead of "
        "inventing a representation for a frozen private value."
    ),
    history=(
        "Round 1 made the loss explicit: a non-null reference is refused with "
        "unsupported_credential_ref instead of being stripped from a successful "
        "save. Round 2 recorded the boundary as an open integration conflict with "
        "the evidence and proposed a foundation-lane accessor "
        "OpaqueCredentialRef.persistence_key() so the reference could be written to "
        "a new record field. The ruling declined that proposal: it changes a "
        "frozen domain type to duplicate data the keyring already owns, while the "
        "design already provides load_credential_ref(receiver_id) for exactly this "
        "lookup. Storage keeps round 1's refusal and now records the decision."
    ),
    rejected_alternatives=(
        "Add a public non-secret accessor to the frozen OpaqueCredentialRef -- "
        "persistence_key() -> str -- and write a credential_reference field in "
        "version 1 of the receiver record. Declined: it widens a frozen private "
        "value outside its owner so storage can duplicate a keyring lookup the "
        "design already assigns to load_credential_ref(receiver_id), and it would "
        "also require deciding what a stale written reference means when the "
        "keyring entry has been revoked.",
        "Widen SavedReceiver to carry a CredentialStore-owned opaque key type "
        "instead of OpaqueCredentialRef. Declined: it changes the frozen record "
        "type and every consumer of it, to persist a value the design keeps out of "
        "application metadata.",
        "Persist the reference as an optional field and report success when it is "
        "dropped. Declined: that is silent loss, and it makes a record that cannot "
        "distinguish 'never paired' from 'reference discarded'.",
    ),
    resolution_owner=(
        "storage lane, enforcing an adjudicated decision that leaves "
        "src/airplay_linux/domain unchanged"
    ),
    forbidden_workarounds=(
        "Serializing a private value by re-implementing __getstate__, pickling, or "
        "reading the mangled '_PrivateValue__value' slot.",
        "Adding a generic projection such as asdict/vars to reach the value.",
        "Persisting the field as optional and reporting success when it is dropped.",
        "Persisting provisional references, backend tokens, or secret content.",
    ),
)


# --------------------------------------------------------------------------- strict lifecycle documents
# These projections deliberately do not change legacy helpers/ingestion. In particular,
# legacy ProcessCodec permits PID zero and legacy JSON tolerates duplicate/whitespace.

DAEMON_RESERVATION_SCHEMA: Final = "airplay-linux/daemon-reservation"
LAUNCH_RECEIPT_SCHEMA: Final = "airplay-linux/launch-receipt"
DAEMON_CLEANUP_INTENT_SCHEMA: Final = "airplay-linux/daemon-cleanup-intent"
SUPERVISOR_RELEASE_INTENT_SCHEMA: Final = "airplay-linux/supervisor-release-intent"
AUTHORITY_OWNER_SCHEMA: Final = "airplay-linux/daemon-authority-owner/v1"
PROBE_VERDICT_SCHEMA: Final = "airplay-linux/credential-service-proof"


def _new_text(value: object, *, limit: int = MAX_TEXT) -> str:
    text = _text(value, allow_empty=False)
    if len(text) > limit:
        _invalid()
    try:
        text.encode("utf-8")
    except UnicodeError:
        _invalid()
    return text


def _new_identifier(value: object) -> str:
    return _new_text(value, limit=MAX_IDENTIFIER)


def _new_uuid(value: object) -> UUID:
    text = _new_identifier(value)
    try:
        parsed = UUID(text)
    except (ValueError, TypeError, AttributeError):
        _invalid()
    if str(parsed) != text:
        _invalid()
    return parsed


def _new_uuid_dump(value: object) -> str:
    if type(value) is not UUID:
        _invalid()
    return str(value)


_NEW_UUID = _Codec(dump=_new_uuid_dump, load=_new_uuid)


def _new_boot(value: object) -> str:
    return str(_new_uuid(value))


def _new_path(value: object) -> str:
    text = _new_text(value)
    if not text.startswith("/") or any(part in {"", ".", ".."} for part in text.split("/")[1:]):
        _invalid()
    return text


def _new_basename(value: object) -> str:
    text = _new_identifier(value)
    if "/" in text or text in {".", ".."}:
        _invalid()
    return text


def _new_time(value: object) -> float:
    return _number(value)


_NEW_PROCESS_FIELDS = (
    ("pid", _plain(lambda value: _integer(value, low=1, high=2**31 - 1))),
    ("start_ticks", _plain(_integer)),
)


def _new_process_dump(value: object) -> object:
    if type(value) is not ProcessIdentity:
        _invalid()
    return _fields_of(value, _NEW_PROCESS_FIELDS)


def _new_process_load(value: object) -> ProcessIdentity:
    return ProcessIdentity(**_object(value, _NEW_PROCESS_FIELDS))


_NEW_PROCESS = _Codec(dump=_new_process_dump, load=_new_process_load)
_NEW_VERSION = _plain(lambda value: _integer(value, low=1, high=1))
_NEW_RESERVATION_FIELDS = (
    ("schema_version", _NEW_VERSION),
    ("launch_uuid", _NEW_UUID),
    ("instance_uuid", _NEW_UUID),
    ("app", _NEW_PROCESS),
    ("owner_uid", _plain(_integer)),
    ("boot_id", _plain(_new_boot)),
    ("runtime_directory", _plain(_new_path)),
    ("runtime_device", _plain(_integer)),
    ("runtime_inode", _plain(lambda value: _integer(value, low=1))),
    ("endpoint_generation", _plain(lambda value: _integer(value, low=1))),
    ("socket_name", _plain(_new_basename)),
    ("protocol_version", _plain(_new_identifier)),
)
_NEW_RECEIPT_FIELDS = _NEW_RESERVATION_FIELDS + (
    ("state", _plain(_frozen(frozenset({"reserved", "detached", "bound", "ready", "stopping", "stopped", "failed"})))),
    ("daemon_instance_uuid", _nullable(_NEW_UUID)),
    ("supervisor", _nullable(_NEW_PROCESS)),
    ("daemon", _nullable(_NEW_PROCESS)),
    ("entry_device", _nullable(_plain(_integer))),
    ("entry_inode", _nullable(_plain(lambda value: _integer(value, low=1)))),
    ("socket_inode", _nullable(_plain(lambda value: _integer(value, low=1)))),
    ("peer_pid", _nullable(_plain(lambda value: _integer(value, low=1, high=2**31 - 1)))),
    ("peer_start_ticks", _nullable(_plain(_integer))),
    ("peer_uid", _nullable(_plain(_integer))),
    ("status_state", _nullable(_plain(_frozen(frozenset({"idle", "discovering", "connecting", "streaming", "pin_required"}))))),
    ("credential_service", _plain(_frozen(frozenset({"keyring_proven", "file_refused", "unknown"})))),
    ("failure", _nullable(_ERROR_CODE)),
    ("updated_at", _plain(_new_time)),
)


def _new_outcome_dump(value: object) -> object:
    if value is None or type(value) is str and value == "stopped":
        return value
    return _ERROR_CODE.dump(value)


def _new_outcome_load(value: object) -> object:
    if value is None or type(value) is str and value == "stopped":
        return value
    return _ERROR_CODE.load(value)


_NEW_CLEANUP_FIELDS = (
    ("launch_uuid", _NEW_UUID), ("reason", _ERROR_CODE), ("intent_at", _plain(_new_time)),
    ("completed_at", _nullable(_plain(_new_time))),
    ("outcome", _Codec(dump=_new_outcome_dump, load=_new_outcome_load)),
)
_NEW_SUPERVISOR_FIELDS = (
    ("schema_version", _NEW_VERSION), ("launch_uuid", _NEW_UUID), ("instance_uuid", _NEW_UUID),
    ("app", _NEW_PROCESS), ("supervisor", _NEW_PROCESS), ("daemon", _NEW_PROCESS),
    ("reason", _plain(_frozen(frozenset({"application-exited", "parent-mismatch"})))),
    ("intent_at", _plain(_new_time)),
)
_NEW_OWNER_FIELDS = (
    ("schema", _plain(_frozen(frozenset({AUTHORITY_OWNER_SCHEMA})))),
    ("instance_uuid", _NEW_UUID), ("app", _NEW_PROCESS),
    ("boot_id", _plain(_new_boot)), ("acquired_at", _plain(_new_time)),
)
_NEW_LEG = _plain(_frozen(frozenset({"passed", "failed", "not_run"})))
_NEW_VERDICT_FIELDS = (
    ("schema", _plain(_frozen(frozenset({PROBE_VERDICT_SCHEMA})))), ("version", _NEW_VERSION),
    ("service_leg", _NEW_LEG), ("backend_positive_leg", _NEW_LEG),
    ("backend_negative_leg", _NEW_LEG), ("failure", _nullable(_ERROR_CODE)),
)


def _new_receipt_consistency(values: dict[str, object]) -> None:
    state = values["state"]
    identities = tuple(values[name] for name in ("daemon_instance_uuid", "supervisor", "daemon"))
    endpoint = tuple(values[name] for name in (
        "entry_device", "entry_inode", "socket_inode", "peer_pid", "peer_start_ticks", "peer_uid",
    ))
    has_identity = all(value is not None for value in identities)
    has_endpoint = all(value is not None for value in endpoint)
    # A lone complete group cannot conceal a partial sibling group.
    if any(value is not None for value in identities) and not has_identity:
        _invalid()
    if any(value is not None for value in endpoint) and not has_endpoint:
        _invalid()
    if has_identity and values["daemon_instance_uuid"] in (values["instance_uuid"], values["launch_uuid"]):
        _invalid()
    if has_endpoint:
        if not has_identity:
            _invalid()
        daemon = values["daemon"]
        if (values["peer_pid"], values["peer_start_ticks"], values["peer_uid"]) != (
            daemon.pid, daemon.start_ticks, values["owner_uid"],
        ):
            _invalid()
    observed_status = values["status_state"] is not None or values["credential_service"] != "unknown"
    if observed_status and not has_endpoint:
        _invalid()
    if state == "reserved" and (has_identity or has_endpoint or observed_status):
        _invalid()
    if state == "detached" and (not has_identity or has_endpoint or observed_status):
        _invalid()
    if state in {"bound", "ready"} and not has_endpoint:
        _invalid()
    if state == "ready" and (values["status_state"] != "idle" or values["credential_service"] != "keyring_proven"):
        _invalid()
    if (state == "failed") != (values["failure"] is not None):
        _invalid()


def _new_cleanup_consistency(values: dict[str, object]) -> None:
    completed, outcome = values["completed_at"], values["outcome"]
    if (completed is None) != (outcome is None):
        _invalid()


def _new_verdict_consistency(values: dict[str, object]) -> None:
    legs = tuple(values[name] for name in ("service_leg", "backend_positive_leg", "backend_negative_leg"))
    if "failed" in legs and values["failure"] is None:
        _invalid()
    if all(leg == "passed" for leg in legs) and values["failure"] is not None:
        _invalid()


def _new_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for name, value in pairs:
        if name in document:
            raise RecordError("malformed_json")
        document[name] = value
    return document


def _new_json(payload: bytes, *, bounded_verdict: bool = False) -> dict[str, object]:
    if type(payload) is not bytes or not payload or bounded_verdict and len(payload) > MAX_TEXT:
        raise RecordError("malformed_json")
    try:
        text = payload.decode("utf-8")
        decoder = json.JSONDecoder(object_pairs_hook=_new_pairs, parse_constant=lambda value: _invalid())
        document, end = decoder.raw_decode(text, len(text) - len(text.lstrip(" \t\n\r")))
        if end != len(text):
            raise RecordError("malformed_json")
    except (ValueError, UnicodeError, RecursionError):
        raise RecordError("malformed_json") from None
    if type(document) is not dict:
        raise RecordError("not_an_object")
    return document


_NewRecord = TypeVar("_NewRecord")


def _new_members(
    document: dict[str, object], schema: str,
    fields: tuple[tuple[str, _Codec], ...], envelope: bool,
) -> dict[str, object]:
    expected = {name for name, _ in fields} | ({"schema", "version"} if envelope else set())
    if set(document) - expected:
        raise RecordError("unknown_field")
    if expected - set(document):
        raise RecordError("missing_field")
    if type(document["schema"]) is not str or document["schema"] != schema:
        raise RecordError("unknown_schema")
    if "version" in expected and (type(document["version"]) is not int or document["version"] != 1):
        raise RecordError("unknown_version")
    return _object({name: document[name] for name, _ in fields}, fields)


def _new_encode(
    record: object, cls: type[_NewRecord], schema: str,
    fields: tuple[tuple[str, _Codec], ...], *, envelope: bool = True,
    consistency: Callable[[dict[str, object]], None] | None = None,
) -> bytes:
    if type(record) is not cls:
        _invalid()
    # Each leaf is validated before projection dereferences an identity or enum.
    document = _fields_of(record, fields)
    if envelope:
        document = {"schema": schema, "version": 1, **document}
    values = _new_members(document, schema, fields, envelope)
    if consistency is not None:
        consistency(values)
    try:
        raw = json.dumps(document, separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError):
        _invalid()
    if cls is ProbeVerdict and len(raw) > MAX_TEXT:
        _invalid()
    return raw


def _new_decode(
    payload: bytes, cls: type[_NewRecord], schema: str,
    fields: tuple[tuple[str, _Codec], ...], *, envelope: bool = True,
    consistency: Callable[[dict[str, object]], None] | None = None,
) -> _NewRecord:
    document = _new_json(payload, bounded_verdict=cls is ProbeVerdict)
    values = _new_members(document, schema, fields, envelope)
    if consistency is not None:
        consistency(values)
    return cls(**values)


def encode_daemon_reservation(record: DaemonReservation) -> bytes:
    return _new_encode(record, DaemonReservation, DAEMON_RESERVATION_SCHEMA, _NEW_RESERVATION_FIELDS)


def decode_daemon_reservation(payload: bytes) -> DaemonReservation:
    return _new_decode(payload, DaemonReservation, DAEMON_RESERVATION_SCHEMA, _NEW_RESERVATION_FIELDS)


def encode_launch_receipt(record: LaunchReceipt) -> bytes:
    return _new_encode(record, LaunchReceipt, LAUNCH_RECEIPT_SCHEMA, _NEW_RECEIPT_FIELDS, consistency=_new_receipt_consistency)


def decode_launch_receipt(payload: bytes) -> LaunchReceipt:
    return _new_decode(payload, LaunchReceipt, LAUNCH_RECEIPT_SCHEMA, _NEW_RECEIPT_FIELDS, consistency=_new_receipt_consistency)


def encode_daemon_cleanup_intent(record: DaemonCleanupIntent) -> bytes:
    return _new_encode(record, DaemonCleanupIntent, DAEMON_CLEANUP_INTENT_SCHEMA, _NEW_CLEANUP_FIELDS, consistency=_new_cleanup_consistency)


def decode_daemon_cleanup_intent(payload: bytes) -> DaemonCleanupIntent:
    return _new_decode(payload, DaemonCleanupIntent, DAEMON_CLEANUP_INTENT_SCHEMA, _NEW_CLEANUP_FIELDS, consistency=_new_cleanup_consistency)


def encode_supervisor_release_intent(record: SupervisorReleaseIntent) -> bytes:
    return _new_encode(record, SupervisorReleaseIntent, SUPERVISOR_RELEASE_INTENT_SCHEMA, _NEW_SUPERVISOR_FIELDS)


def decode_supervisor_release_intent(payload: bytes) -> SupervisorReleaseIntent:
    return _new_decode(payload, SupervisorReleaseIntent, SUPERVISOR_RELEASE_INTENT_SCHEMA, _NEW_SUPERVISOR_FIELDS)


def encode_authority_owner(record: AuthorityOwner) -> bytes:
    return _new_encode(record, AuthorityOwner, AUTHORITY_OWNER_SCHEMA, _NEW_OWNER_FIELDS, envelope=False)


def decode_authority_owner(payload: bytes) -> AuthorityOwner:
    return _new_decode(payload, AuthorityOwner, AUTHORITY_OWNER_SCHEMA, _NEW_OWNER_FIELDS, envelope=False)


def encode_probe_verdict(record: ProbeVerdict) -> bytes:
    return _new_encode(record, ProbeVerdict, PROBE_VERDICT_SCHEMA, _NEW_VERDICT_FIELDS, envelope=False, consistency=_new_verdict_consistency)


def decode_probe_verdict(payload: bytes) -> ProbeVerdict:
    return _new_decode(payload, ProbeVerdict, PROBE_VERDICT_SCHEMA, _NEW_VERDICT_FIELDS, envelope=False, consistency=_new_verdict_consistency)


__all__ = [
    "DAEMON_RESERVATION_SCHEMA", "LAUNCH_RECEIPT_SCHEMA", "DAEMON_CLEANUP_INTENT_SCHEMA",
    "SUPERVISOR_RELEASE_INTENT_SCHEMA", "AUTHORITY_OWNER_SCHEMA", "PROBE_VERDICT_SCHEMA",
    "encode_daemon_reservation", "decode_daemon_reservation", "encode_launch_receipt", "decode_launch_receipt",
    "encode_daemon_cleanup_intent", "decode_daemon_cleanup_intent",
    "encode_supervisor_release_intent", "decode_supervisor_release_intent",
    "encode_authority_owner", "decode_authority_owner", "encode_probe_verdict", "decode_probe_verdict",
    "CREDENTIAL_REFERENCE_CONFLICT",
    "DAEMON_SCHEMA",
    "DESIRED_STATES",
    "OWNERSHIP_SCHEMA",
    "OWNERSHIP_STAGES",
    "RECEIVER_SCHEMA",
    "RecordError",
    "SESSION_SCHEMA",
    "VERSION",
    "decode_daemon",
    "decode_ownership",
    "decode_receiver",
    "decode_session",
    "encode_daemon",
    "encode_ownership",
    "encode_receiver",
    "encode_session",
    "is_ambiguous_identity",
]
