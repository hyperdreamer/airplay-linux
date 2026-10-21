"""Deterministic session-manager rig for actor and fencing tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.ports import (
    ActivitySink,
    AudioProbe,
    CapturePort,
    Clock,
    DiscoveryPort,
    DisplayPort,
    IdFactory,
    OwnershipStore,
    PairingPort,
    ReceiverStore,
    Scheduler,
    SessionStore,
    TransportPort,
)
from airplay_linux.domain.resources import CapabilityReport, Diagnostic, Ok, Result
from airplay_linux.testing.adapters import (
    FakeAudioProbe,
    FakeCapture,
    FakeDiscovery,
    FakeDisplay,
    FakePairing,
    FakeTransport,
    Journal,
    MemoryActivity,
    MemoryOwnershipStore,
    MemoryReceiverStore,
    MemorySessionStore,
    SequenceIds,
)
from airplay_linux.testing.builders import target
from airplay_linux.testing.time import FakeClock, FakeScheduler
from airplay_linux.trust.fences import TrustFenceRegistry

from .engine import Handler, ManagerDeps, Route, SessionManager


class _Restart:
    async def __call__(self) -> Result[Any]:
        return Ok(value=None)


def _capabilities() -> CapabilityReport:
    return CapabilityReport(
        environment="KDE Plasma 6 Wayland",
        discovery=True,
        mirror=True,
        virtual_display=True,
        audio=True,
        remembered_pairing=True,
        additional_virtual_outputs=True,
        diagnostics=(),
    )


class SessionRig:
    """Build all existing fakes and a manager with optional test-local handlers."""

    def __init__(self, handlers_factory: Callable[[ManagerDeps], Mapping[Route, Handler]] | None = None) -> None:
        self.clock = FakeClock()
        self.scheduler = FakeScheduler(self.clock)
        self.ids = SequenceIds(seed=2000)
        self.transport = FakeTransport()
        self.pairing = FakePairing(self.clock)
        self.display = FakeDisplay()
        self.capture = FakeCapture()
        self.discovery = FakeDiscovery()
        self.audio = FakeAudioProbe()
        self.sessions = MemorySessionStore()
        self.receivers = MemoryReceiverStore()
        self.ownerships = self.display.ownership
        self.activity = MemoryActivity()
        self.fences = TrustFenceRegistry()
        self.journal = Journal()
        self.receiver_id: ReceiverId = target().receiver_id
        self.deps = ManagerDeps(
            transport=self.transport,
            pairing=self.pairing,
            display=self.display,
            capture=self.capture,
            discovery=self.discovery,
            audio_probe=self.audio,
            sessions=self.sessions,
            receivers=self.receivers,
            ownerships=self.ownerships,
            fences=self.fences,
            clock=self.clock,
            scheduler=self.scheduler,
            ids=self.ids,
            activity=self.activity,
            capabilities=_capabilities,
            restart_daemon=_Restart(),
        )
        handlers = handlers_factory(self.deps) if handlers_factory is not None else {}
        self.manager = SessionManager(self.deps, handlers)

    async def settle(self) -> None:
        await self.manager.drain()


__all__ = ["SessionRig"]
