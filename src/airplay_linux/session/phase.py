"""Derived session phases.

The session snapshot stores independent resource states.  The phase is a view of
those states and the current intent stage; callers never get to make the view
contradict the resources it describes.
"""

from __future__ import annotations

from airplay_linux.domain.records import SessionSnapshot
from airplay_linux.domain.resources import (
    DisplayState,
    IntentStage,
    SessionPhase,
    TransportState,
    TrustState,
)


def derive_phase(state: SessionSnapshot) -> SessionPhase:
    """Derive the single user-facing phase from independent resource facts.

    The order is deliberate.  A resource that might still exist wins over a
    convenient-looking idle or failed label, and a trust or resolution decision
    remains visible even when another resource has a less interesting state.
    """
    if state.cleanup_pending or state.display is DisplayState.CleanupRequired:
        return SessionPhase.CleanupRequired

    if state.intent_stage is IntentStage.stopping:
        return SessionPhase.Stopping
    if state.intent_stage is IntentStage.reconfiguring:
        return SessionPhase.Reconfiguring
    if state.transport is TransportState.Stopping:
        return SessionPhase.Stopping
    if state.display is DisplayState.Reconfiguring:
        return SessionPhase.Reconfiguring

    if state.trust in (TrustState.RevocationPending, TrustState.CleanupRequired):
        return SessionPhase.TrustCleanupRequired

    if state.intent_stage is IntentStage.resolution or state.display is DisplayState.Resolving:
        return SessionPhase.ResolutionRequired
    if state.intent_stage is IntentStage.audio_decision:
        return SessionPhase.AudioDecisionRequired

    if state.intent_stage is IntentStage.recovering:
        return SessionPhase.Recovering

    if (
        state.trust is TrustState.Pairing
        or state.challenge is not None
        or state.transport is TransportState.Pairing
    ):
        return SessionPhase.Pairing

    if state.display in (DisplayState.Resolving, DisplayState.Creating):
        return SessionPhase.CreatingDisplay

    if state.transport is TransportState.Reconnecting:
        return SessionPhase.Reconnecting
    if state.transport is TransportState.Connecting:
        return SessionPhase.Connecting
    if state.transport is TransportState.Streaming:
        if state.mode is not None and state.mode.value == "virtual":
            return SessionPhase.VirtualDisplay
        return SessionPhase.Mirroring

    if (
        state.transport is TransportState.Failed
        or state.display is DisplayState.Failed
    ):
        return SessionPhase.Failed

    return SessionPhase.Available if state.online else SessionPhase.Offline


__all__ = ["derive_phase"]
