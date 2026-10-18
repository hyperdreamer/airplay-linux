"""Linux process execution, exact ownership evidence, and procfs reads.

Import :class:`LinuxProcessRunner` for the ``ProcessPort`` implementation, and
the procfs helpers when ownership has to be decided from process evidence alone
(a startup scan for a helper that was launched but never recorded).
"""

from __future__ import annotations

from .linux import (
    INHERITED_ENVIRONMENT,
    KILL_TIMEOUT,
    OUTPUT_LIMIT,
    SPAWN_EVIDENCE_TIMEOUT,
    LinuxProcessRunner,
    inherited_environment,
)
from .procfs import (
    MAX_ENVIRONMENT_BYTES,
    OWNER_TOKEN_ENV,
    RUNTIME_DIRECTORY_ENV,
    TERMINAL_STATES,
    IdentityEvidence,
    IdentityState,
    owner_evidence,
    owned_processes,
    read_environment,
    read_identity,
    read_identity_evidence,
    read_parent,
    read_uid,
)

__all__ = [
    "INHERITED_ENVIRONMENT",
    "IdentityEvidence",
    "IdentityState",
    "KILL_TIMEOUT",
    "LinuxProcessRunner",
    "MAX_ENVIRONMENT_BYTES",
    "OUTPUT_LIMIT",
    "OWNER_TOKEN_ENV",
    "RUNTIME_DIRECTORY_ENV",
    "SPAWN_EVIDENCE_TIMEOUT",
    "TERMINAL_STATES",
    "inherited_environment",
    "owner_evidence",
    "owned_processes",
    "read_environment",
    "read_identity",
    "read_identity_evidence",
    "read_parent",
    "read_uid",
]
