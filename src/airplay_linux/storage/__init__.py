"""Durable, non-secret metadata: atomic files, versioned codecs, repositories.

Import the atomic boundary first (``AtomicFiles``), then the codecs for one
record type, then the repository that implements the matching domain port.
Storage never writes a secret: PINs, passwords, portal restore tokens, capture
leases, backend correlation tokens, and provisional credentials have no field in
any record this package can produce.
"""

from __future__ import annotations

from .atomic import (
    AtomicFiles,
    PosixFilesystem,
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    StorageError,
    state_root,
)
from .codecs import (
    encode_daemon_reservation, decode_daemon_reservation,
    encode_launch_receipt, decode_launch_receipt,
    encode_daemon_cleanup_intent, decode_daemon_cleanup_intent,
    encode_supervisor_release_intent, decode_supervisor_release_intent,
    encode_authority_owner, decode_authority_owner,
    encode_probe_verdict, decode_probe_verdict,
    CREDENTIAL_REFERENCE_CONFLICT,
    DAEMON_SCHEMA,
    DESIRED_STATES,
    OWNERSHIP_SCHEMA,
    OWNERSHIP_STAGES,
    RECEIVER_SCHEMA,
    SESSION_SCHEMA,
    VERSION,
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
from .repositories import (
    JsonLaunchReceiptStore,
    JsonDaemonCleanupIntentStore,
    JsonSupervisorReleaseIntentStore,
    DAEMON_DIRECTORY,
    OWNERSHIP_DIRECTORY,
    RECEIVER_DIRECTORY,
    SESSION_DIRECTORY,
    JsonDaemonStore,
    JsonOwnershipStore,
    JsonReceiverStore,
    JsonSessionStore,
    PreferenceAccess,
)

__all__ = [
    "JsonLaunchReceiptStore", "JsonDaemonCleanupIntentStore", "JsonSupervisorReleaseIntentStore",
    "encode_daemon_reservation", "decode_daemon_reservation", "encode_launch_receipt", "decode_launch_receipt",
    "encode_daemon_cleanup_intent", "decode_daemon_cleanup_intent",
    "encode_supervisor_release_intent", "decode_supervisor_release_intent",
    "encode_authority_owner", "decode_authority_owner", "encode_probe_verdict", "decode_probe_verdict",
    "AtomicFiles",
    "CREDENTIAL_REFERENCE_CONFLICT",
    "DAEMON_DIRECTORY",
    "DAEMON_SCHEMA",
    "DESIRED_STATES",
    "JsonDaemonStore",
    "JsonOwnershipStore",
    "JsonReceiverStore",
    "JsonSessionStore",
    "OWNERSHIP_DIRECTORY",
    "OWNERSHIP_SCHEMA",
    "OWNERSHIP_STAGES",
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_FILE_MODE",
    "PosixFilesystem",
    "PreferenceAccess",
    "RECEIVER_DIRECTORY",
    "RECEIVER_SCHEMA",
    "RecordError",
    "SESSION_DIRECTORY",
    "SESSION_SCHEMA",
    "StorageError",
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
    "state_root",
]
