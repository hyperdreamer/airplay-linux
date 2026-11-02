"""Backend-reported capture-source provenance verification."""

from __future__ import annotations

from airplay_linux.display.inventory import (
    KScreenReportedSourceVerifier,
    MonitoredOutput,
    SourceObservation,
    SourceRefusal,
    SourceVerificationRefusal,
    verify_reported_source,
)

ReportedSourceVerifier = KScreenReportedSourceVerifier

__all__ = [
    "KScreenReportedSourceVerifier",
    "MonitoredOutput",
    "ReportedSourceVerifier",
    "SourceObservation",
    "SourceRefusal",
    "SourceVerificationRefusal",
    "verify_reported_source",
]
