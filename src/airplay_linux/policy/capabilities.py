"""Affirmative startup capability evidence and per-mode start gates.

Capability is never inferred from the absence of an error. Every boolean in the
:class:`~airplay_linux.domain.resources.CapabilityReport` is backed by a probe
that positively proved the operation, and every failure is isolated to the
capability it belongs to:

* discovery only depends on the selected mDNS implementation;
* both modes first need the backend operations the real transport proves before
  it dispatches anything: an owned foreground daemon with a proven owner
  handshake *and* a working backend keyring operation, because
  ``DoubleTakeTransport._admit`` refuses a start without all three. A mode gate
  that omits one of them would advertise a start the transport rejects;
* a start then needs the transport's proven multi-target operation, which
  ``DoubleTakeTransport.start_stream`` checks after admission. This requirement
  is distinct from pairing: remembered pairing still only needs admission plus
  its trust transactions, so a backend without multi-target loses streaming but
  keeps its pairing capability;
* mirror needs a KDE Plasma 6 session on either Wayland or X11, the proven
  physical-source backend capability, a physical capture provider, and a
  non-empty physical monitor inventory;
* virtual display needs the KDE Plasma 6 Wayland provider, a proven
  virtual-source backend, ``kscreen-doctor``, and a probed helper contract;
* audio needs both the backend's proven audio surface and the repeatable
  PipeWire/portal probe;
* remembered pairing needs both an available keyring and the backend's proven
  trust transactions;
* a second virtual output additionally needs the helper profile to prove
  distinct outputs with an exact ``True``; a truthy non-boolean is not proof and
  never blocks the first output.

The guaranteed v1 environments are KDE Plasma 6 Wayland and KDE Plasma 6 X11.
Portal-backed Mirror is available on both, because the portal owns the capture on
either session; Virtual display remains Wayland-only because the KDE
``krfb-virtualmonitor`` provider creates a compositor output. Every other desktop,
and a KDE session whose version is not positively reported, is named but never
enables a mode from command presence alone: no alternate display provider is part
of this probe, so such a session stays gated until one is added and proves the
requested operation. Each withheld mode also carries the first unmet requirement
as a :class:`~airplay_linux.domain.resources.ModeRefusal`, and the report's
``environment`` field carries the human-readable session label. Startup probes
are read-only: they run help/version commands, introspect the portal, and list
physical monitors, but they never create an output, start a helper, open a
stream, or prompt for a PIN or audio permission.

The UI runtime is probed from import metadata only; this module never imports
PySide6. That keeps the evidence usable for a bootstrap that prints the exact
dependency diagnostic to stderr before any UI import is attempted.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from enum import StrEnum
from typing import Final

from airplay_linux.backend.contract import BackendFeatures
from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.helper import HelperFeatures
from airplay_linux.domain.ports import AudioProbe, CapturePort, ProcessPort
from airplay_linux.domain.resources import (
    SESSION_PROFILE_DETAIL_PREFIX,
    CapabilityReport,
    Diagnostic,
    Err,
    ErrorCode,
    Mode,
    ModeRefusal,
    Ok,
    PhysicalMonitorSource,
    Result,
    TrustState,
)

from .audio import (
    DBusCapabilityClient,
    InterfaceCapabilityClient,
    PipeWirePortalAudioProbe,
    secret_service_available,
)


class DesktopEnvironment(StrEnum):
    """The desktop/session profiles the startup probe can distinguish."""

    KDE_PLASMA_6_WAYLAND = "kde-plasma-6-wayland"
    KDE_PLASMA_6_X11 = "kde-plasma-6-x11"
    KDE_PLASMA_5_WAYLAND = "kde-plasma-5-wayland"
    KDE_PLASMA_5_X11 = "kde-plasma-5-x11"
    KDE_WAYLAND_UNVERIFIED = "kde-wayland-unverified"
    KDE_X11_UNVERIFIED = "kde-x11-unverified"
    GNOME_WAYLAND = "gnome-wayland"
    X11 = "x11"
    UNKNOWN = "unknown"


#: The KDE Plasma 6 Wayland profile token, the only virtual-display provider path.
KDE_PLASMA_6_WAYLAND: Final[str] = DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value

#: The profiles where portal-backed Mirror may be offered. The portal owns the
#: capture on both KDE Plasma 6 sessions, so no direct X11 capture path exists.
MIRROR_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    {
        DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value,
        DesktopEnvironment.KDE_PLASMA_6_X11.value,
    }
)

#: The profiles where the KDE virtual-output provider may be offered.
VIRTUAL_DISPLAY_ENVIRONMENTS: Final[frozenset[str]] = frozenset(
    {
        DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value,
    }
)

#: The one token-to-label map. Every consumer reports these exact strings, and
#: a token outside it is exactly ``"Unknown"``.
_ENVIRONMENT_LABELS: Final[dict[str, str]] = {
    DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value: "KDE Plasma 6 Wayland",
    DesktopEnvironment.KDE_PLASMA_6_X11.value: "KDE Plasma 6 X11",
    DesktopEnvironment.KDE_PLASMA_5_WAYLAND.value: "KDE Plasma 5 Wayland",
    DesktopEnvironment.KDE_PLASMA_5_X11.value: "KDE Plasma 5 X11",
    DesktopEnvironment.KDE_WAYLAND_UNVERIFIED.value: "KDE Wayland (version unverified)",
    DesktopEnvironment.KDE_X11_UNVERIFIED.value: "KDE X11 (version unverified)",
    DesktopEnvironment.GNOME_WAYLAND.value: "GNOME Wayland",
    DesktopEnvironment.X11.value: "X11 (other desktop)",
    DesktopEnvironment.UNKNOWN.value: "Unknown",
}


def environment_label(profile: str) -> str:
    """The human-readable session label for one classified profile token."""
    return _ENVIRONMENT_LABELS.get(profile, "Unknown")


#: Backend operations every stream start needs before any backend I/O.
#: ``DoubleTakeTransport._admit`` refuses with ``dependency_incompatible``
#: unless public owned-daemon control and keyring evidence are proven. Fixture
#: profiles retain their foreground/handshake comparison inside the transport.
ADMISSION_BACKEND_CAPABILITIES: Final[tuple[str, ...]] = (
    "owned_daemon_control",
    "keyring",
)

LEGACY_ADMISSION_BACKEND_CAPABILITIES: Final[tuple[str, ...]] = (
    "foreground_control",
    "owner_handshake",
    "keyring",
)
#: ``DoubleTakeTransport.start_stream`` refuses with ``unsupported_operation``
#: without it. Pairing does not need it, so it must not gate trust operations.
START_BACKEND_CAPABILITIES: Final[tuple[str, ...]] = ("multi_target",)

#: Backend operations remembered pairing depends on, all of which must be proven.
PAIRING_BACKEND_CAPABILITIES: Final[tuple[str, ...]] = (
    "target_bound_pairing",
    "keyring",
    "provisional_commit",
    "cancel_pairing",
    "revoke_pairing",
)

#: Trust states that make a new start unsafe until the exact resource is resolved.
_TRUST_BLOCKERS: Final[frozenset[TrustState]] = frozenset(
    {TrustState.CleanupRequired, TrustState.RevocationPending},
)


def _text(value: object) -> str:
    """An exact string environment value, or empty text for anything else."""
    return value if type(value) is str else ""


def classify_environment(environment: Mapping[str, str]) -> str:
    """Classify the session without guessing a desktop or compositor version.

    ``XDG_SESSION_TYPE`` decides X11/Wayland; both can use the portal capture
    path, so Mirror is no longer Wayland-only, while the KDE virtual-output
    provider still is. KDE additionally needs a positively reported Plasma
    session, because "KDE" alone does not prove a supported profile. Every
    desktop or version we cannot name positively is ``unknown`` rather than
    assumed.
    """
    if not isinstance(environment, Mapping):
        raise TypeError("environment must be a mapping of session values")
    session = _text(environment.get("XDG_SESSION_TYPE")).strip().lower()
    raw_desktop = _text(environment.get("XDG_CURRENT_DESKTOP"))
    desktop = {
        token.strip().lower() for token in raw_desktop.split(":") if token.strip()
    }
    version = _text(environment.get("KDE_SESSION_VERSION")).strip()
    kde = "kde" in desktop or "plasma" in desktop

    if session == "x11":
        if kde:
            if version == "6":
                return DesktopEnvironment.KDE_PLASMA_6_X11.value
            if version == "5":
                return DesktopEnvironment.KDE_PLASMA_5_X11.value
            return DesktopEnvironment.KDE_X11_UNVERIFIED.value
        # GNOME is recognised only in the Wayland branch; every other X11
        # desktop shares the same profile token.
        return DesktopEnvironment.X11.value
    if session != "wayland":
        return DesktopEnvironment.UNKNOWN.value
    if kde:
        if version == "6":
            return DesktopEnvironment.KDE_PLASMA_6_WAYLAND.value
        if version == "5":
            return DesktopEnvironment.KDE_PLASMA_5_WAYLAND.value
        return DesktopEnvironment.KDE_WAYLAND_UNVERIFIED.value
    if "gnome" in desktop:
        return DesktopEnvironment.GNOME_WAYLAND.value
    return DesktopEnvironment.UNKNOWN.value


def _module_available(name: str) -> bool:
    """Whether the selected Python implementation is importable metadata-wise."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _capability_failure(detail: str) -> Diagnostic:
    """A typed, actionable "this operation is not proven" failure."""
    return replace(diagnostic(ErrorCode.unsupported_operation), detail=detail)


def _session_profile_failure(detail: str) -> Diagnostic:
    """One per-mode session-profile refusal, sharing the UI's detail prefix."""
    return _capability_failure(f"{SESSION_PROFILE_DETAIL_PREFIX}{detail}")


def _incompatible(detail: str) -> Diagnostic:
    return replace(diagnostic(ErrorCode.dependency_incompatible), detail=detail)


def _module_failure(name: str) -> Diagnostic:
    return replace(diagnostic(ErrorCode.dependency_missing), detail=name)


def _command_failure(name: str) -> Diagnostic:
    return replace(diagnostic(ErrorCode.dependency_missing, command=name), detail=name)


def _audio_failure(detail: str) -> Diagnostic:
    return replace(diagnostic(ErrorCode.audio_unavailable), detail=detail)


def _keyring_failure(detail: str) -> Diagnostic:
    return replace(diagnostic(ErrorCode.keyring_unavailable), detail=detail)


async def _settled(
    call: Callable[[], Awaitable[Result[object]]],
) -> Result[object] | None:
    """Run one injected probe; ``None`` means it raised unexpectedly.

    A raised adapter exception is unproven capability, never support, and its
    text is never copied into a diagnostic.
    """
    try:
        return await call()
    except Exception:  # noqa: BLE001 - adapter failures are unproven capability
        return None


def _physical_inventory(value: object) -> tuple[PhysicalMonitorSource, ...] | None:
    """The proven physical inventory, or ``None`` when the shape is not proof."""
    if not isinstance(value, tuple):
        return None
    if not all(isinstance(source, PhysicalMonitorSource) for source in value):
        return None
    return value


def _refuse_mirror(
    *,
    environment_supported: bool,
    capture_present: bool,
    mode_ready: bool,
    physical_source: bool,
    inventory: tuple[PhysicalMonitorSource, ...],
) -> ModeRefusal | None:
    """The first unmet Mirror requirement, in the closed precedence table.

    The rows are ordered and mutually exclusive in effect: simultaneous failed
    evidence always resolves to the earlier row.
    """
    if not environment_supported:
        return ModeRefusal.session
    if not capture_present:
        return ModeRefusal.dependency
    if not mode_ready:
        return ModeRefusal.admission
    if not physical_source:
        return ModeRefusal.backend
    if not inventory:
        return ModeRefusal.inventory
    return None


def _refuse_virtual_display(
    *,
    environment_supported: bool,
    kscreen: bool,
    mode_ready: bool,
    virtual_source: bool,
    helper: HelperFeatures | None,
) -> ModeRefusal | None:
    """The first unmet Virtual display requirement, in the same table shape."""
    if not environment_supported:
        return ModeRefusal.session
    if not kscreen:
        return ModeRefusal.dependency
    if not mode_ready:
        return ModeRefusal.admission
    if not virtual_source:
        return ModeRefusal.backend
    if helper is None:
        return ModeRefusal.helper
    return None


class EnvironmentProbe:
    """Read-only startup evidence for every independently gated capability.

    All evidence sources are injected. Tests therefore never read the real
    session, find real executables, contact D-Bus, inspect a real socket, or
    start a helper. ``check`` memoizes the report gathered at startup, while
    ``refresh`` re-runs every probe so a dependency installed after a fix is
    observed and a failure is never cached permanently.
    """

    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        command_finder: Callable[[str], str | None] | None = None,
        module_finder: Callable[[str], bool] | None = None,
        backend_probe: Callable[[], Awaitable[Result[BackendFeatures]]] | None = None,
        helper_probe: Callable[[], Awaitable[Result[HelperFeatures]]] | None = None,
        capture: CapturePort | None = None,
        audio_probe: AudioProbe | None = None,
        keyring_probe: Callable[[], Awaitable[bool]] | None = None,
        dbus: InterfaceCapabilityClient | None = None,
    ) -> None:
        if environment is not None and not isinstance(environment, Mapping):
            raise TypeError("environment must be a mapping of session values")
        self._environment: Mapping[str, str] = dict(
            os.environ if environment is None else environment,
        )
        self._command_finder = (
            shutil.which if command_finder is None else command_finder
        )
        self._module_finder = (
            _module_available if module_finder is None else module_finder
        )
        self._backend_probe = backend_probe
        self._helper_probe = helper_probe
        self._capture = capture
        self._dbus = DBusCapabilityClient() if dbus is None else dbus
        self._audio_probe = (
            PipeWirePortalAudioProbe(
                portal=self._dbus, environment=self._environment,
            )
            if audio_probe is None
            else audio_probe
        )
        self._keyring_probe = (
            (lambda: secret_service_available(self._dbus))
            if keyring_probe is None
            else keyring_probe
        )
        self._audio_capability: bool | None = None
        self._audio_capability_detail: str | None = None
        self._report: CapabilityReport | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        process: ProcessPort,
        helper: object,
        capture: CapturePort | None = None,
        environment: Mapping[str, str] | None = None,
        dbus: InterfaceCapabilityClient | None = None,
        audio_probe: AudioProbe | None = None,
        keyring_probe: Callable[[], Awaitable[bool]] | None = None,
        backend_probe: Callable[[], Awaitable[Result[BackendFeatures]]] | None = None,
    ) -> EnvironmentProbe:
        """Compose the frozen Task 7/8/13 probes without touching a desktop.

        ``process`` is the bounded :class:`ProcessPort`; backend capability comes
        from Task 8's :func:`probe_backend`, and helper capability from Task 13's
        ``VirtualHelper.probe``. The caller decides which capture provider and
        D-Bus client exist, so composition remains the only place that chooses a
        real desktop adapter.

        ``backend_probe`` overrides that public probe with the capability view a
        composition root already proved. Capabilities no help text can affirm
        (``owned_daemon_control``, ``keyring``, ``physical_source``) exist only in
        that derived view, so passing it is what makes this report able to
        describe a start the real transport would admit.
        """
        from airplay_linux.backend.contract import probe_backend

        return cls(
            environment=environment,
            backend_probe=(
                (lambda: probe_backend(process))
                if backend_probe is None
                else backend_probe
            ),
            helper_probe=helper.probe,  # type: ignore[attr-defined]
            capture=capture,
            dbus=dbus,
            audio_probe=audio_probe,
            keyring_probe=keyring_probe,
        )

    # -- public checks ------------------------------------------------------

    async def check(self) -> CapabilityReport:
        """The startup report, gathered once and then served from memory."""
        if self._report is None:
            self._report = await self._probe()
        return self._report

    async def refresh(self) -> CapabilityReport:
        """Re-run every probe after the user fixed a dependency."""
        self._report = await self._probe()
        return self._report

    def register_audio_probe_capability(
        self, available: bool, detail: str | None = None,
    ) -> None:
        """Record the outcome of the actual audio request, not introspection.

        Introspection proves that PipeWire and the portal interface exist; it
        cannot prove that the user granted audio for this request. When the real
        start request is denied or cancelled, the caller records that outcome
        here and the next ``check`` reports audio as unavailable, while video
        capabilities remain untouched. A previously cached report is discarded.
        """
        if type(available) is not bool:
            raise TypeError("audio capability availability must be a boolean")
        if detail is not None and type(detail) is not str:
            raise TypeError("audio capability detail must be text or None")
        self._audio_capability = available
        self._audio_capability_detail = detail
        self._report = None

    # -- evidence helpers ---------------------------------------------------

    def _command(self, name: str) -> bool:
        try:
            found = self._command_finder(name)
        except Exception:  # noqa: BLE001 - a failed lookup proves nothing
            return False
        return type(found) is str and bool(found)

    def _module(self, name: str) -> bool:
        try:
            return self._module_finder(name) is True
        except Exception:  # noqa: BLE001 - a failed lookup proves nothing
            return False

    async def _physical_capture(self) -> Result[object] | None:
        if self._capture is None:
            return None
        return await _settled(self._capture.list_physical)

    async def _audio(self) -> Result[object] | None:
        if self._audio_probe is None:
            return None
        return await _settled(self._audio_probe.check)

    async def _keyring(self) -> bool:
        if self._keyring_probe is None:
            return False
        try:
            value = self._keyring_probe()
            if inspect.isawaitable(value):
                value = await value
            return value is True
        except Exception:  # noqa: BLE001 - an unreadable keyring proves nothing
            return False

    # -- report ------------------------------------------------------------

    async def _probe(self) -> CapabilityReport:
        environment = classify_environment(self._environment)
        mirror_environment_supported = environment in MIRROR_ENVIRONMENTS
        virtual_display_environment_supported = (
            environment in VIRTUAL_DISPLAY_ENVIRONMENTS
        )
        diagnostics: list[Diagnostic] = []

        def note(item: Diagnostic) -> None:
            if item not in diagnostics:
                diagnostics.append(item)

        # The UI runtime and discovery are independent of transports and modes.
        if not self._module("PySide6"):
            note(_module_failure("PySide6"))
        discovery = self._module("zeroconf")
        if not discovery:
            note(_module_failure("zeroconf"))

        # One diagnostic per unmet mode, each naming the exact profile token.
        # The two details stay distinct, so neither replaces the other.
        if not mirror_environment_supported:
            note(_session_profile_failure(
                "mirror: needs KDE Plasma 6 on Wayland or X11; "
                f"observed {environment}",
            ))
        if not virtual_display_environment_supported:
            note(_session_profile_failure(
                "virtual-display: needs KDE Plasma 6 on Wayland; "
                f"observed {environment}",
            ))

        # Backend evidence. A failure here is reported as its own typed cause;
        # an absent or malformed probe is never treated as support.
        backend: BackendFeatures | None = None
        if self._backend_probe is None:
            note(_capability_failure("doubletake backend probe"))
        else:
            result = await _settled(self._backend_probe)
            if isinstance(result, Err):
                note(result.error)
            elif isinstance(result, Ok) and isinstance(result.value, BackendFeatures):
                backend = result.value
            else:
                note(_incompatible("doubletake backend contract"))

        public_admitted = backend is not None and all(
            backend.supports(name) for name in ADMISSION_BACKEND_CAPABILITIES
        )
        fixture_admitted = backend is not None and all(
            backend.supports(name) for name in LEGACY_ADMISSION_BACKEND_CAPABILITIES
        )
        # The public activated graph uses owned-daemon evidence. Delivered
        # fixture profiles retain their foreground/handshake admission so the
        # policy probe remains a useful contract fixture while activation
        # keeps those legacy flags false.
        admitted = public_admitted or fixture_admitted
        mode_ready = admitted and all(
            backend.supports(name) for name in START_BACKEND_CAPABILITIES
        )
        physical_source = backend is not None and backend.supports("physical_source")
        virtual_source = backend is not None and backend.supports("virtual_source")
        backend_audio = backend is not None and backend.supports("audio_probe")
        backend_pairing = backend is not None and all(
            backend.supports(name) for name in PAIRING_BACKEND_CAPABILITIES
        )
        if backend is not None:
            if not public_admitted and not fixture_admitted:
                # Report both public and fixture admission evidence when no
                # path is usable; this keeps the exact missing legacy details
                # used by the delivered policy diagnostics.
                for name in ADMISSION_BACKEND_CAPABILITIES + LEGACY_ADMISSION_BACKEND_CAPABILITIES:
                    if not backend.supports(name):
                        note(_incompatible(name))
            for name in START_BACKEND_CAPABILITIES:
                if not backend.supports(name):
                    note(_incompatible(name))
            if not physical_source:
                note(_incompatible("physical_source"))
            if not virtual_source:
                note(_incompatible("virtual_source"))
            if not backend_audio:
                note(_audio_failure("doubletake audio"))
            if not backend_pairing:
                note(_incompatible("target_bound_pairing"))

        # Physical capture: an Okay-but-empty inventory is not proof of a source.
        inventory: tuple[PhysicalMonitorSource, ...] = ()
        if self._capture is None:
            note(_capability_failure("physical capture provider"))
        else:
            capture_result = await self._physical_capture()
            if isinstance(capture_result, Err):
                note(capture_result.error)
            elif isinstance(capture_result, Ok):
                proven = _physical_inventory(capture_result.value)
                if proven:
                    inventory = proven
                else:
                    note(_capability_failure("physical capture source"))
            else:
                note(_capability_failure("physical capture source"))

        # Virtual-display provider: installed command plus a probed helper.
        kscreen = self._command("kscreen-doctor")
        if not kscreen:
            note(_command_failure("kscreen-doctor"))

        helper: HelperFeatures | None = None
        if self._helper_probe is None:
            note(_capability_failure("krfb-virtualmonitor probe"))
        else:
            result = await _settled(self._helper_probe)
            if isinstance(result, Err):
                note(result.error)
            elif isinstance(result, Ok) and isinstance(result.value, HelperFeatures):
                helper = result.value
            else:
                note(_incompatible("krfb-virtualmonitor contract"))

        # Audio and remembered pairing have their own evidence and their own
        # failures; neither is derived from a mode gate.
        audio_evidence = False
        if self._audio_probe is None:
            note(_capability_failure("audio probe"))
        else:
            audio_result = await self._audio()
            if isinstance(audio_result, Err):
                note(audio_result.error)
            elif isinstance(audio_result, Ok) and audio_result.value is None:
                audio_evidence = True
            else:
                note(_capability_failure("audio probe result"))
        audio = audio_evidence and backend_audio
        if self._audio_capability is not None:
            if not self._audio_capability:
                note(_audio_failure(self._audio_capability_detail or "audio permission"))
            audio = audio and self._audio_capability

        keyring = await self._keyring()
        if not keyring:
            note(_keyring_failure("Secret Service"))
        # Pairing is admitted without the multi-target start operation: losing
        # streaming must not silently disable the saved-device trust path.
        remembered_pairing = keyring and backend_pairing and admitted

        mirror = bool(
            mirror_environment_supported
            and mode_ready
            and inventory
            and physical_source
        )
        virtual_display = bool(
            virtual_display_environment_supported
            and mode_ready
            and kscreen
            and helper is not None
            and virtual_source
        )
        mirror_refusal = _refuse_mirror(
            environment_supported=mirror_environment_supported,
            capture_present=self._capture is not None,
            mode_ready=mode_ready,
            physical_source=physical_source,
            inventory=inventory,
        )
        virtual_display_refusal = _refuse_virtual_display(
            environment_supported=virtual_display_environment_supported,
            kscreen=kscreen,
            mode_ready=mode_ready,
            virtual_source=virtual_source,
            helper=helper,
        )
        # An exact boolean is the only proof of distinct concurrent outputs. A
        # truthy string or integer is malformed evidence, and it only withholds
        # the *additional* output, never the first one.
        additional_outputs = bool(
            virtual_display and helper is not None and helper.distinct_outputs is True,
        )

        return CapabilityReport(
            environment=environment_label(environment),
            discovery=discovery,
            mirror=mirror,
            virtual_display=virtual_display,
            audio=audio,
            remembered_pairing=remembered_pairing,
            additional_virtual_outputs=additional_outputs,
            diagnostics=tuple(diagnostics),
            volume=backend is not None and backend.target_volume,
            mirror_refusal=mirror_refusal,
            virtual_display_refusal=virtual_display_refusal,
        )


def can_start(report: CapabilityReport, mode: Mode, trust: TrustState) -> Result[None]:
    """Gate one attempted mode, and no unrelated capability.

    The report's mode booleans decide support, and each one already includes the
    transport's own admission prerequisites (owned foreground daemon, backend
    keyring operation, multi-target operation); permission here therefore never
    exceeds what the real transport will admit. A mode blocked by a missing
    dependency does not block the other mode. Unconfirmed trust cleanup is a
    separate safety lockout for every new start. Audio is intentionally not
    consulted here: a pre-ready audio failure is an explicit
    ``AudioDecisionRequired`` decision, not a start refusal and never a silent
    video-only downgrade.
    """
    if not isinstance(report, CapabilityReport):
        raise TypeError("report must be a CapabilityReport")
    if not isinstance(mode, Mode):
        raise TypeError("mode must be a Mode")
    if not isinstance(trust, TrustState):
        raise TypeError("trust must be a TrustState")
    if trust in _TRUST_BLOCKERS:
        return Err(error=diagnostic(ErrorCode.trust_cleanup_required))
    supported = report.mirror if mode is Mode.MIRROR else report.virtual_display
    if not supported:
        return Err(error=diagnostic(ErrorCode.unsupported_operation))
    return Ok(value=None)


__all__ = [
    "ADMISSION_BACKEND_CAPABILITIES",
    "DesktopEnvironment",
    "EnvironmentProbe",
    "KDE_PLASMA_6_WAYLAND",
    "MIRROR_ENVIRONMENTS",
    "PAIRING_BACKEND_CAPABILITIES",
    "START_BACKEND_CAPABILITIES",
    "VIRTUAL_DISPLAY_ENVIRONMENTS",
    "can_start",
    "classify_environment",
    "environment_label",
]
