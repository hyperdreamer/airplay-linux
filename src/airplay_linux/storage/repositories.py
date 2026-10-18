"""JSON repositories over the atomic file boundary, and preference continuity.

Each repository implements one of the frozen :mod:`airplay_linux.domain.ports`
store protocols and translates every storage failure into a ``persistence_failed``
diagnostic. The store never raises for a durable failure: a caller that cannot
tell "written" from "not written" cannot apply the design's rule that a failed
removal-intent publication blocks destructive teardown.

Reading is deliberately asymmetric between the two families of store.

* A *session* record is evidence of intent. One unreadable session is
  quarantined as a diagnostic and the other sessions stay loadable, because a
  corrupt record for one receiver must not make every other receiver unusable.
* An *ownership*, *daemon*, or *received-metadata* record is authoritative.
  Returning a partial set would understate owned resources, so any unreadable
  record fails the whole read closed: an unowned output is never inferred from a
  list that silently dropped one entry.

Nothing here deletes a record it could not read. Quarantine is a diagnostic, not
an erasure: the bytes stay on disk as evidence for the user's cleanup decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverRecordSet
from airplay_linux.domain.operations import InstanceId, OwnerId
from airplay_linux.domain.records import (
    DaemonRecord,
    OwnershipRecord,
    SavedReceiver,
    SessionSnapshot,
)
from airplay_linux.domain.resources import Diagnostic, Err, ErrorCode, Ok, Result

from .atomic import AtomicFiles, StorageError
from .codecs import (
    RecordError,
    decode_daemon,
    decode_ownership,
    decode_receiver,
    decode_session,
    encode_daemon,
    encode_ownership,
    encode_receiver,
    encode_session,
    is_ambiguous_identity,
)

#: Directory names under the private state root.
SESSION_DIRECTORY: Final[str] = "sessions"
RECEIVER_DIRECTORY: Final[str] = "receivers"
OWNERSHIP_DIRECTORY: Final[str] = "ownership"
DAEMON_DIRECTORY: Final[str] = "daemon"

#: The record suffix; every name is the encoded identity plus this suffix.
SUFFIX: Final[str] = ".json"


# --------------------------------------------------------------------------- helpers


def _record_name(token: str) -> str:
    """A record file name for an identity token; percent-encoding is reversible.

    ``':'`` is the separator in every derived receiver identity and is legal in a
    file name, so it is quoted rather than relied on: the name stays one plain
    component, unique per identity, and decodes back to the identity it names.
    """
    return quote(token, safe="") + SUFFIX


def _with_reason(reason: str) -> Diagnostic:
    """The catalog diagnostic for a durable failure, tagged with a fixed reason.

    The catalog owns the message and the recovery actions; ``detail`` adds the
    closed reason token written at the call site (``malformed_json``, ``rename``,
    ``directory_fsync``, ``ambiguous_identity``, ...). Neither part can hold
    record content, a filesystem path, or an operating-system message, so a
    quarantine report is safe to show and to log.
    """
    base = diagnostic(ErrorCode.persistence_failed, None)
    return Diagnostic(
        code=base.code,
        message=base.message,
        recovery=base.recovery,
        receiver_id=base.receiver_id,
        detail=reason,
    )


def _err(reason: str) -> Err:
    return Err(error=_with_reason(reason))


# --------------------------------------------------------------------------- session store


class JsonSessionStore:
    """``SessionStore`` over one durable JSON record per receiver."""

    def __init__(self, files: AtomicFiles) -> None:
        self._files = files
        self._quarantine: list[Diagnostic] = []

    def quarantined(self) -> tuple[Diagnostic, ...]:
        """Diagnostics for records that could not be read on the last load."""
        return tuple(self._quarantine)

    async def save(self, snapshot: SessionSnapshot) -> Result[None]:
        try:
            payload = encode_session(snapshot)
        except RecordError as error:
            return _err(error.reason)
        try:
            self._files.write(
                f"{SESSION_DIRECTORY}/{_record_name(snapshot.receiver_id)}", payload,
            )
        except StorageError as error:
            # A failed directory fsync is an uncertain durable write: the bytes
            # may be visible, but the caller must not treat them as published.
            return _err(error.step)
        return Ok(value=None)

    async def load_all(self) -> tuple[SessionSnapshot, ...]:
        self._quarantine = []
        loaded: list[SessionSnapshot] = []
        try:
            names = self._files.names(SESSION_DIRECTORY)
        except StorageError as error:
            # Enumerating the session directory failed, so the set of sessions is
            # *unknown*, not empty. The frozen contract returns a tuple, so the
            # failure is surfaced through the quarantine list rather than by
            # raising: one unreachable directory must not make a storage failure
            # escape session recovery.
            self._quarantine.append(_with_reason(error.step))
            return ()
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            try:
                payload = self._files.read(f"{SESSION_DIRECTORY}/{name}")
            except StorageError as error:
                self._quarantine.append(_with_reason(error.step))
                continue
            if payload is None:
                continue
            try:
                loaded.append(decode_session(payload))
            except RecordError as error:
                # Quarantined, never deleted: the bytes are the evidence a later
                # recovery decision needs.
                self._quarantine.append(_with_reason(error.reason))
        return tuple(loaded)


# --------------------------------------------------------------------------- receiver store


class JsonReceiverStore:
    """``ReceiverStore`` for non-secret saved receivers; never an ambiguous one."""

    def __init__(self, files: AtomicFiles) -> None:
        self._files = files

    async def get(self, receiver_id: ReceiverId) -> Result[SavedReceiver | None]:
        if is_ambiguous_identity(receiver_id):
            return Ok(value=None)
        name = _record_name(receiver_id)
        try:
            payload = self._files.read(f"{RECEIVER_DIRECTORY}/{name}")
        except StorageError as error:
            return _err(error.step)
        if payload is None:
            return Ok(value=None)
        try:
            return Ok(value=decode_receiver(payload))
        except RecordError as error:
            return _err(error.reason)

    async def all(self) -> Result[tuple[SavedReceiver, ...]]:
        stored: list[SavedReceiver] = []
        try:
            names = self._files.names(RECEIVER_DIRECTORY)
        except StorageError as error:
            return _err(error.step)
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            try:
                payload = self._files.read(f"{RECEIVER_DIRECTORY}/{name}")
                if payload is None:
                    continue
                stored.append(decode_receiver(payload))
            except StorageError as error:
                return _err(error.step)
            except RecordError as error:
                # A partial set would understate what the user saved, so an
                # unreadable sibling fails the whole read closed.
                return _err(error.reason)
        return Ok(value=tuple(stored))

    async def put(self, saved: SavedReceiver) -> Result[None]:
        # An ambiguous identity has no durable record: it is ephemeral by
        # construction and must not inherit trust or a remembered resolution.
        if is_ambiguous_identity(saved.receiver_id):
            return _err("ambiguous_identity")
        try:
            payload = encode_receiver(saved)
        except RecordError as error:
            return _err(error.reason)
        try:
            self._files.write(
                f"{RECEIVER_DIRECTORY}/{_record_name(saved.receiver_id)}", payload,
            )
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)

    async def remove_metadata(self, receiver_id: ReceiverId) -> Result[None]:
        if is_ambiguous_identity(receiver_id):
            return Ok(value=None)
        try:
            self._files.delete(f"{RECEIVER_DIRECTORY}/{_record_name(receiver_id)}")
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)


# --------------------------------------------------------------------------- ownership store


class JsonOwnershipStore:
    """``OwnershipStore``; the authoritative record for virtual-output recovery."""

    def __init__(self, files: AtomicFiles) -> None:
        self._files = files

    async def write(self, record: OwnershipRecord) -> Result[None]:
        try:
            payload = encode_ownership(record)
        except RecordError as error:
            return _err(error.reason)
        try:
            self._files.write(
                f"{OWNERSHIP_DIRECTORY}/{_record_name(str(record.owner_uuid))}", payload,
            )
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)

    async def all(self) -> Result[tuple[OwnershipRecord, ...]]:
        records: list[OwnershipRecord] = []
        try:
            names = self._files.names(OWNERSHIP_DIRECTORY)
        except StorageError as error:
            return _err(error.step)
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            try:
                payload = self._files.read(f"{OWNERSHIP_DIRECTORY}/{name}")
                if payload is None:
                    continue
                records.append(decode_ownership(payload))
            except StorageError as error:
                return _err(error.step)
            except RecordError as error:
                # Fails closed: dropping an unreadable ownership record would let
                # a caller conclude an output it still owns is unowned.
                return _err(error.reason)
        return Ok(value=tuple(records))

    async def delete(self, owner_uuid: OwnerId) -> Result[None]:
        try:
            self._files.delete(
                f"{OWNERSHIP_DIRECTORY}/{_record_name(str(owner_uuid))}",
            )
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)


# --------------------------------------------------------------------------- daemon store


class JsonDaemonStore:
    """``DaemonStore`` for private runtime records, written before/after launch."""

    def __init__(self, files: AtomicFiles) -> None:
        self._files = files

    async def write(self, record: DaemonRecord) -> Result[None]:
        try:
            payload = encode_daemon(record)
        except RecordError as error:
            return _err(error.reason)
        try:
            self._files.write(
                f"{DAEMON_DIRECTORY}/{_record_name(str(record.instance_uuid))}", payload,
            )
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)

    async def all(self) -> Result[tuple[DaemonRecord, ...]]:
        records: list[DaemonRecord] = []
        try:
            names = self._files.names(DAEMON_DIRECTORY)
        except StorageError as error:
            return _err(error.step)
        for name in names:
            if not name.endswith(SUFFIX):
                continue
            try:
                payload = self._files.read(f"{DAEMON_DIRECTORY}/{name}")
                if payload is None:
                    continue
                records.append(decode_daemon(payload))
            except StorageError as error:
                return _err(error.step)
            except RecordError as error:
                # A daemon record is authentication evidence: a partial set could
                # let a live private daemon look unattributable, so fail closed.
                return _err(error.reason)
        return Ok(value=tuple(records))

    async def delete(self, instance_uuid: InstanceId) -> Result[None]:
        try:
            self._files.delete(f"{DAEMON_DIRECTORY}/{_record_name(str(instance_uuid))}")
        except StorageError as error:
            return _err(error.step)
        return Ok(value=None)


# --------------------------------------------------------------------------- preferences


@dataclass(frozen=True, kw_only=True)
class PreferenceAccess:
    """Decide whether saved preferences still describe *this* identity.

    A saved record may be used only while the identity's continuity justifies it.
    The hashed receiver ID alone is never justification: a fallback identity is a
    hash of a service-instance label or of an endpoint, so a changed endpoint or
    an unverified first advertisement can carry the same hash while describing a
    different device. Reusing the record then would silently hand a remembered
    resolution or a trust reference to a replaced receiver.
    """

    @staticmethod
    def for_record_set(
        record_set: ReceiverRecordSet, saved: SavedReceiver | None,
    ) -> SavedReceiver | None:
        """The stored preferences this record set may use, or ``None``."""
        if saved is None:
            return None
        if record_set.ambiguous or record_set.identity_kind == "ambiguous":
            return None
        if not record_set.persistable:
            return None
        if record_set.replacement_of is not None:
            return None
        if record_set.receiver_id != saved.receiver_id:
            return None
        if is_ambiguous_identity(saved.receiver_id):
            return None
        if record_set.identity_kind == "device":
            # A stable device identity survives a compatible endpoint change: the
            # record describes the device, not where it was last seen.
            return saved
        # A fallback identity is a hash of mutable advertisement data, so an equal
        # ID is not continuity. Only a verified continuity claim at the endpoint
        # the record was saved for may inherit trust or a remembered resolution.
        if not record_set.continuity_verified:
            return None
        if saved.continuity_fingerprint != record_set.endpoint_fingerprint:
            return None
        return saved


__all__ = [
    "DAEMON_DIRECTORY",
    "JsonDaemonStore",
    "JsonOwnershipStore",
    "JsonReceiverStore",
    "JsonSessionStore",
    "OWNERSHIP_DIRECTORY",
    "PreferenceAccess",
    "RECEIVER_DIRECTORY",
    "SESSION_DIRECTORY",
]

