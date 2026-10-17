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
  Nothing the caller supplied survives in ``args``, ``__cause__`` or
  ``__context__``, and the original failure is never chained.
"""

from __future__ import annotations

import gc
import re
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

#: Attribute carrying a rejection's own fixed text, so only text written in this
#: module survives :func:`rejecting`.
_FIXED_TEXT: Final[str] = "_diagnostics_fixed_rejection"

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
    so a caught failure is never attached as ``__context__``.
    """
    failure = ValueError(message)
    _fixed(failure, message)
    return failure


def type_rejection(message: str) -> TypeError:
    """A fixed :class:`TypeError`; a wrong type stays a type error, not a value one."""
    failure = TypeError(message)
    _fixed(failure, message)
    return failure


def _fixed(failure: Exception, message: str) -> None:
    """Scrub a rejection and record that its text is this module's own."""
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    # The mark is an attribute rather than an encoding of the message, so the
    # text that propagates is looked up in this module rather than parsed out of
    # a caller-visible field. A hostile *object* cannot reach an exception the
    # boundary raises, so it cannot set this mark on one.
    failure.__dict__[_FIXED_TEXT] = message


def _rejection_of(kind: type[Exception], message: str) -> Exception:
    return type_rejection(message) if kind is TypeError else rejection(message)


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

    Only a rejection built by this module lends its text, and only through its
    exact built-in type, so a caller ``BaseException``, an exception subclass,
    or a library error whose message quotes a caller value is never repeated.
    The replacement is built here rather than reused, so it carries no ``args``,
    ``__cause__`` or ``__context__`` from the failure it replaces, and it is
    marked again so an enclosing entry point recognizes it too.
    """
    kind = type(error)
    if kind is ValueError or kind is TypeError:
        marked = error.__dict__.get(_FIXED_TEXT)
        if type(marked) is str:
            return _rejection_of(kind, marked)
    return rejection(UNEXPECTED_FAILURE)


def rejecting(function: _Wrapped) -> _Wrapped:
    """Wrap one public entry point so no raised object carries caller data.

    The replacement is raised after the ``except`` suite has finished, so the
    failure that triggered it is neither chained as ``__context__`` nor attached
    as ``__cause__``.
    """

    @wraps(function)
    def boundary_entry(*args: object, **kwargs: object) -> object:
        failure: BaseException
        try:
            return function(*args, **kwargs)
        except BaseException as error:
            failure = _fixed_failure(error)
        raise failure

    return cast(_Wrapped, boundary_entry)
