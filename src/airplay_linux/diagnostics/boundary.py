"""One hook-free normalization boundary for every diagnostics value.

Every caller value the diagnostics package accepts — text, mappings and frozen
vocabulary members — crosses this module exactly once, because a per-site check
is a per-site defect waiting to be found. The rules are deliberately narrow, and
each one closes a whole class of defect rather than one instance of it:

* Only exact built-in types are accepted (``type(value) is str``). A caller
  object that is not an exact accepted builtin is rejected without inspection,
  so no ``__class__``, metaclass, ``__eq__``, ``__hash__``, ``__iter__``,
  ``items`` or ``copy`` hook of a caller-supplied object can run inside the
  diagnostics package, and no copy of a subclass's rendering can be mistaken for
  its data.
* Membership in a frozen vocabulary or enum is decided by ``is`` against members
  captured at import time. Equality, hashing and dictionary lookup are not proof
  of membership: ``str.__new__(ErrorCode, ...)`` has the exact enum type and
  compares and hashes like the genuine member, yet the enumeration never
  registered it.
* Every public entry point is wrapped, so any raised object — including
  ``BaseException`` — becomes a fresh rejection built from source-written text.
  Nothing the caller supplied survives in ``args``, ``__cause__``,
  ``__context__`` or ``__notes__``, and the original failure is never chained —
  including the exception the *caller* was already handling, which Python would
  otherwise attach at the ``raise`` itself.
* Provenance is object identity, not a marker on the failure. Every rejection
  this module builds is recorded in a private, bounded registry that keeps the
  text this module wrote; a wrapped entry point recognizes its own failure with
  ``is`` and re-issues *that recorded text*. An exact built-in ``ValueError`` is
  something any caller can construct, fill in and raise, so a mutable attribute
  — or any other caller-visible field — could never prove that text was ours.
  The registry keeps no weak references, because built-in exceptions are not
  weakly referenceable; it is bounded instead, and a miss degrades to the fixed
  :data:`UNEXPECTED_FAILURE` literal rather than to caller text.
"""

from __future__ import annotations

import gc
import re
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import wraps
from types import MappingProxyType
from typing import Callable, Final, TypeVar, cast

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import (
    OpaqueBackendToken,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    SensitiveText,
)
from airplay_linux.domain.resources import ErrorCode

_Wrapped = TypeVar("_Wrapped", bound=Callable[..., object])

#: Text of the rejection that stands in for an unforeseen raised object.
UNEXPECTED_FAILURE: Final[str] = (
    "the value could not be normalized at the diagnostics boundary"
)

#: Message of the rejection for anything that is not a registered enum member.
NOT_AN_ERROR_CODE: Final[str] = (
    "code must be an ErrorCode from the diagnostics catalog; "
    "arbitrary error text is not a diagnostic"
)

#: Message of the rejection for a value that is not an exact mapping.
NOT_A_MAPPING: Final[str] = "fields must be a mapping of allowlisted names to values"

#: Message of the rejection for anything that is not a receiver identity.
NOT_A_RECEIVER_ID: Final[str] = "receiver_id must be a ReceiverId or None"

#: Message of the rejection for a receiver identity outside the design grammar.
NOT_CANONICAL: Final[str] = (
    "receiver_id must be a canonical public receiver identity"
)

#: How many of this module's own failures stay recognizable.
#:
#: Only the failures still in flight need to be: a wrapped entry point re-issues
#: its replacement immediately as it propagates, so nesting consumes a handful of
#: entries and the window never has to reach across a call boundary. Keeping the
#: window bounded is what makes the registry worth its memory — a rejected value
#: is otherwise retained for as long as the failure it produced is.
_REGISTRY_CAPACITY: Final[int] = 64

#: ``(failure, text, kind)`` for every rejection this module built, newest last.
#:
#: The text is stored *here* rather than read back from the failure, so a caller
#: that catches a rejection and rewrites its ``args`` or ``__dict__`` still cannot
#: change what an enclosing entry point propagates. The entries are exact tuples
#: this module created, and recognition compares them with ``is``, so no method,
#: attribute or container protocol of a caller-supplied object is ever consulted.
_ISSUED: Final[deque[tuple[Exception, str, type]]] = deque(maxlen=_REGISTRY_CAPACITY)

#: Types whose instances are transient secrets; none of them has a recorded
#: shape, and each is rejected before any shape check inspects it.
PRIVATE_VALUE_TYPES: Final[tuple[type, ...]] = (
    SensitiveText,
    OpaqueCredentialRef,
    OpaqueProvisionalCredentialRef,
    OpaqueBackendToken,
)

#: A lowercase ``sha256`` digest, which is the whole payload of every
#: hash-derived receiver identity the approved design defines.
_SHA256_DIGEST: Final[str] = r"[0-9a-f]{64}"

#: One grammar per design-defined receiver identity prefix, and no others.
#:
#: The design derives an identity from either a normalized device identifier or
#: a ``sha256`` digest of the discovery evidence, so a diagnostics boundary has
#: to accept exactly those four forms to be able to record what discovery emits.
_RECEIVER_ID_BODY: Final[Mapping[str, str]] = MappingProxyType(
    {
        "airplay-device": (
            r"(?:[0-9a-f]{1,64}|"
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
        ),
        "airplay-fingerprint": _SHA256_DIGEST,
        "airplay-endpoint": _SHA256_DIGEST,
        "airplay-ambiguous": _SHA256_DIGEST,
    }
)

#: The four forms as one expression; each alternative is whole-string anchored
#: by :func:`re.fullmatch`, so no prefix can borrow another's payload.
_RECEIVER_ID: Final[re.Pattern[str]] = re.compile(
    "|".join(f"{prefix}:{body}" for prefix, body in _RECEIVER_ID_BODY.items()),
)


def rejection(message: str) -> ValueError:
    """A fixed :class:`ValueError` carrying only source-written text.

    ``message`` is always a literal, or text derived from a closed vocabulary, at
    the call site. The value carries exactly one argument — that message — and no
    chaining of its own; :func:`rejecting` raises it outside its ``except`` suite
    and clears the interpreter-attached context in the raising ``try``'s
    ``finally``, so neither a caught failure nor one the caller was already
    handling is attached as ``__context__``. Nothing is written onto the value's
    ``__dict__``: the text that proves provenance is kept in this module, not in
    state the caller can read or edit.
    """
    return cast(ValueError, _rejection_of(ValueError, message))


def type_rejection(message: str) -> TypeError:
    """A fixed :class:`TypeError`; a wrong type stays a type error, not a value one."""
    return cast(TypeError, _rejection_of(TypeError, message))


def _rejection_of(kind: type[Exception], message: str) -> Exception:
    """Build, scrub and record one rejection of ``kind``.

    Only the two built-in kinds exist here, so the type of a rejection this module
    raised is itself source-written; nothing about the failure is inferred later.
    Recording the failure object is what lets an enclosing entry point recognize
    it, and recording the text beside it is what keeps that text out of the
    exception's own caller-visible fields.
    """
    rejection_type: type[Exception] = TypeError if kind is TypeError else ValueError
    failure = rejection_type(message)
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    _ISSUED.append((failure, message, rejection_type))
    return failure


def exact_text(
    value: object,
    *,
    kind: type[Exception] = ValueError,
    message: str = "a diagnostics value must be an exact string",
) -> str:
    """The character data of an exact ``str``; every other object is rejected.

    ``type`` is read directly — never ``__class__`` and never an MRO — so a
    class that lies about its type is not consulted, and a subclass carrying
    legitimate data is still rejected rather than copied. The exact built-in
    string that is returned is the object that was accepted, so the characters
    that were validated are the characters that are stored.
    """
    if type(value) is not str:
        raise _rejection_of(kind, message)
    return value


def exact_mapping_items(
    value: object,
    *,
    message: str = NOT_A_MAPPING,
) -> tuple[tuple[object, object], ...]:
    """The items of an exact ``dict``, or of a proxy over one, and nothing else.

    A ``dict`` subclass is rejected before anything is read from it: its
    ``items``, ``keys``, ``copy`` and iteration hooks are caller code, and the
    boundary may not run them to discover what it holds. An exact
    :class:`MappingProxyType` is unwrapped through the garbage collector's own
    reference list, because the proxy type offers no other way to reach the
    mapping without dispatching into it; the wrapped object's exact type is
    decided before any of its items are read.
    """
    return tuple(dict.items(_exact_dict(value, message)))


def _exact_dict(value: object, message: str) -> dict:
    if type(value) is dict:
        return value
    if type(value) is MappingProxyType:
        referents = gc.get_referents(value)
        if len(referents) == 1 and type(referents[0]) is dict:
            return referents[0]
    raise type_rejection(message)


def exact_only(
    value: object,
    expected: type,
    *,
    kind: type[Exception] = TypeError,
    message: str,
) -> object:
    """``value`` when its type *is* ``expected``, or a fixed rejection.

    Identity of type, never ``isinstance``: a subclass, a look-alike advertising
    ``__class__``, and an object whose type cannot even be described without
    running a hook are all the same answer here — rejected without inspection.
    """
    if type(value) is not expected:
        raise _rejection_of(kind, message)
    return value


def is_private_value(value: object) -> bool:
    """Whether ``value`` is exactly one of the transient secret record types.

    Exact types only: a subclass is not this record, and it is rejected by the
    shape check that follows, so nothing here walks a class hierarchy.
    """
    return any(type(value) is private_type for private_type in PRIVATE_VALUE_TYPES)


def exact_bounded_int(value: object, *, low: int, high: int, message: str) -> int:
    """A whole number of the exact built-in ``int`` type inside ``low..high``."""
    if type(value) is not int or not low <= value <= high:
        raise rejection(message)
    return value


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """A closed value set captured at import time.

    ``members`` and ``texts`` are parallel, so a genuine member is recognized by
    identity and its own captured text is returned without reading anything back
    from the object the caller passed in.
    """

    members: tuple[object, ...]
    texts: tuple[str, ...]
    permitted: frozenset[str]

    def text_for(self, value: object) -> str | None:
        """The captured text of the member ``value`` *is*, or ``None``."""
        for index, member in enumerate(self.members):
            if value is member:
                return self.texts[index]
        return None


def captured_enum(enum: type) -> Vocabulary:
    """The frozen members of ``enum`` and their own text, captured once."""
    members = tuple(enum)
    texts = tuple(member.value for member in members)
    return Vocabulary(members=members, texts=texts, permitted=frozenset(texts))


def captured_texts(values: Iterable[str]) -> Vocabulary:
    """A vocabulary whose only accepted values are these exact strings.

    No member objects: a str-shaped vocabulary is matched on character data, so
    each accepted value already *is* a plain built-in string.
    """
    permitted = frozenset(values)
    return Vocabulary(members=(), texts=(), permitted=permitted)


#: The frozen error-code vocabulary, captured once at import time so the enum's
#: own members and text are never re-read from a caller-held object.
ERROR_CODE_VOCABULARY: Final[Vocabulary] = captured_enum(ErrorCode)


def vocabulary_text(
    value: object,
    vocabulary: Vocabulary,
    *,
    kind: type[Exception] = ValueError,
    message: str,
) -> str:
    """``value`` as the vocabulary's own exact text, or a fixed rejection.

    A captured genuine member is recognized with ``is``. Anything else must be
    an exact ``str`` whose characters the vocabulary already lists; equality,
    hashing and dictionary lookup never decide membership, and the accepted
    characters are the vocabulary's own text rather than a re-rendering.
    """
    captured = vocabulary.text_for(value)
    if captured is not None:
        return captured
    if type(value) is str and value in vocabulary.permitted:
        return value
    raise _rejection_of(kind, message)


def genuine_error_code(value: object) -> ErrorCode:
    """The captured :class:`ErrorCode` member ``value`` is identical to.

    Identity, not type, equality or hashing: an object forged with
    ``str.__new__(ErrorCode, ...)`` has the exact enum type and compares equal to
    the genuine member, yet it is a different object the enumeration never
    registered.
    """
    for member in ERROR_CODE_VOCABULARY.members:
        if value is member:
            return cast(ErrorCode, member)
    raise type_rejection(NOT_AN_ERROR_CODE)


def exact_receiver_id(value: object) -> ReceiverId | None:
    """A canonical design receiver identity, or ``None``; nothing is echoed.

    The value must be an exact ``str``: its characters are matched as a whole
    string, and those same characters become the retained identity, so a caller
    cannot render one identity and carry another.
    """
    if value is None:
        return None
    text = exact_text(value, kind=TypeError, message=NOT_A_RECEIVER_ID)
    if _RECEIVER_ID.fullmatch(text) is None:
        raise rejection(NOT_CANONICAL)
    return ReceiverId(text)


def exact_command(value: object) -> str | None:
    """An allowlisted executable name, or ``None``; never a caller rendering."""
    if value is None:
        return None
    return vocabulary_text(
        value,
        COMMAND_VOCABULARY,
        message=(
            "command must be one of the allowlisted executable names; "
            "arbitrary backend text is not a diagnostic"
        ),
    )


#: The allowlisted executables, captured so membership is decided on data.
COMMAND_VOCABULARY: Final[Vocabulary] = captured_texts(
    (
        "doubletake",
        "doubletake-ctl",
        "kscreen-doctor",
        "krfb-virtualmonitor",
    )
)


def _fixed_failure(error: BaseException) -> Exception:
    """A fresh, scrubbed rejection standing in for ``error``.

    Only a failure *identical to* one this module recorded lends its text, so a
    caller ``BaseException``, a hand-built ``ValueError`` dressed up with a
    plausible attribute, and a library error whose message quotes a caller value
    are all the same answer: the fixed :data:`UNEXPECTED_FAILURE` literal. The
    text comes from the record, never from the failure, so rewriting ``args`` or
    ``__dict__`` on a caught rejection cannot change what propagates.

    The snapshot is taken before scanning because a ``deque`` cannot be iterated
    while another thread mutates it; a snapshot failure degrades to a literal,
    which is the safe direction. Nothing else here can raise — the scan compares
    object identity, which consults no hook of the failure being examined.
    """
    try:
        issued = tuple(_ISSUED)
    except BaseException:
        issued = ()
    for candidate, text, kind in issued:
        if candidate is error:
            return _rejection_of(kind, text)
    return rejection(UNEXPECTED_FAILURE)


def rejecting(function: _Wrapped) -> _Wrapped:
    """Wrap one public entry point so no raised object carries caller data.

    Two things have to be released from the replacement, and only one of them is
    under this function's control at the moment it builds the failure:

    * the failure that triggered the wrapper, which is why the replacement is
      built inside the ``except`` suite and raised after it has finished; and
    * the exception the *caller* was already handling when it entered this
      wrapper. Python attaches that one as ``__context__`` at the ``raise``
      itself, after every check the wrapper could make, so an application that
      records a diagnostic from inside an ``except`` handler would otherwise
      hand its backend failure to the rejection one step after the value was
      scrubbed. ``from None`` sets ``__suppress_context__`` — which only hides
      the link from a rendered traceback — and sets ``__cause__``, so it is not
      a scrub: the chain survives in the object and in ``repr``.

    The link is therefore removed *after* the interpreter makes it, from the
    ``finally`` of the raising ``try``: on the way in the replacement is already
    built and recorded, so nothing about the original failure is inspected
    here, and on the way out the same object that propagates has been cleared.
    """

    @wraps(function)
    def boundary_entry(*args: object, **kwargs: object) -> object:
        failure: BaseException
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            failure = _fixed_failure(error)
        try:
            raise failure
        finally:
            # Only the chaining fields of this module's own replacement are
            # written, so no caller object is touched and no method of one runs.
            failure.__cause__ = None
            failure.__context__ = None
            failure.__suppress_context__ = True

    return cast(_Wrapped, boundary_entry)
