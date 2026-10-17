"""Deterministic test adapters and an event laboratory for the session manager.

Import the fakes from here; the builders live in :mod:`airplay_linux.testing.builders`
and the controllable clock/scheduler in :mod:`airplay_linux.testing.time`.
"""

from __future__ import annotations

from .adapters import (
    Journal,
    MemoryActivity,
    MemoryDaemonStore,
    MemoryOwnershipStore,
    MemoryReceiverStore,
    MemorySessionStore,
    Redacted,
    RedactedRecord,
    SequenceIds,
    FakeAudioProbe,
    FakeCapture,
    FakeCredentialStore,
    FakeDiscovery,
    FakeDisplay,
    FakeManager,
    FakePairing,
    FakeProcess,
    FakeTransport,
    Deferred,
    redact,
)
from .builders import context, physical, service, target, virtual_handle
from .time import FakeClock, FakeScheduler

__all__ = [
    "Deferred",
    "FakeAudioProbe",
    "FakeCapture",
    "FakeClock",
    "FakeCredentialStore",
    "FakeDiscovery",
    "FakeDisplay",
    "FakeManager",
    "FakePairing",
    "FakeProcess",
    "FakeScheduler",
    "FakeTransport",
    "Journal",
    "MemoryActivity",
    "MemoryDaemonStore",
    "MemoryOwnershipStore",
    "MemoryReceiverStore",
    "MemorySessionStore",
    "Redacted",
    "RedactedRecord",
    "SequenceIds",
    "context",
    "physical",
    "redact",
    "service",
    "target",
    "virtual_handle",
]
