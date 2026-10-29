"""Application runtime composition: the root that binds every lane together.

This module is the *only* place that knows how the adapters, ports, session engine
and UI are wired. Everything below it consumes ports; nothing below it may reach
across lanes to construct another lane's adapter.

The composition root is deliberately fail-closed. It constructs the concrete
adapters even when local evidence is missing, and records the resulting gates in
the runtime instead of substituting a permissive fake.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from airplay_linux.backend.contract import BackendFeatures, probe_backend
from airplay_linux.backend.control import (
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
    UnsupportedControl,
)
from airplay_linux.capture.physical import KscreenConnectorInventory, PhysicalCaptureProvider
from airplay_linux.capture.portal import PortalClient
from airplay_linux.diagnostics.activity import ActivityLog
from airplay_linux.diagnostics import diagnostic
from airplay_linux.discovery.identity import IdentityResolver
from airplay_linux.discovery.zeroconf_adapter import AzcMdnsClient, ZeroconfDiscovery
from airplay_linux.display.association import KScreenOutputVerifier
from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kde import KdeVirtualDisplayProvider
from airplay_linux.display.kscreen import KScreenObserver
from airplay_linux.display.locking import CreationLock
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.display.removal import DisplayRemover
from airplay_linux.domain.operations import InstanceId, OperationContext
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.policy.audio import PipeWirePortalAudioProbe
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.session import Handler, ManagerDeps, Route, SessionManager
from airplay_linux.session.audio import build_audio_handlers, compose_audio_handlers
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.persistence import SessionPersistence
from airplay_linux.session.reconnect import build_reconnect_handlers, compose_reconnect_handlers
from airplay_linux.session.recovery import (
    StartupRecovery,
    build_recovery_handlers,
)
from airplay_linux.session.shutdown import ShutdownCoordinator
from airplay_linux.session.virtual import build_virtual_handlers, compose_virtual_handlers
from airplay_linux.storage import (
    AtomicFiles,
    JsonDaemonStore,
    JsonOwnershipStore,
    JsonReceiverStore,
    JsonSessionStore,
)
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore

#: Dependency names :func:`build_runtime` accepts as test overrides. Kept explicit
#: so an unknown keyword fails loudly instead of silently wiring the real adapter.
OVERRIDABLE = frozenset(
    {
        "deps",
        "capabilities",
        "transport",
        "pairing",
        "display",
        "capture",
        "discovery",
        "audio_probe",
        "sessions",
        "receivers",
        "ownerships",
        "fences",
        "clock",
        "scheduler",
        "ids",
        "activity",
        "restart_daemon",
        "presenter",
        "shutdown_hook",
    }
)

_ADAPTER_OVERRIDES = frozenset(
    {
        "transport",
        "pairing",
        "display",
        "capture",
        "discovery",
        "audio_probe",
        "sessions",
        "receivers",
        "ownerships",
        "fences",
        "clock",
        "scheduler",
        "ids",
        "activity",
        "capabilities",
        "restart_daemon",
    }
)


def _absolute(name: str, value: object) -> Path:
    path = Path(value) if not isinstance(value, Path) else value
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {path!r}")
    return path


@dataclass(frozen=True, kw_only=True)
class RuntimeConfig:
    """Filesystem and mode configuration for one application instance."""

    state_root: Path
    runtime_root: Path
    live: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", _absolute("state_root", self.state_root))
        object.__setattr__(self, "runtime_root", _absolute("runtime_root", self.runtime_root))
        if type(self.live) is not bool:
            raise TypeError("live must be a bool")


def compose_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    """Compose the production handler map: exactly one owner per :class:`Route`."""

    # Reconnect and audio explicitly replace their lifecycle continuations. Every
    # other factory claim must be disjoint, including claims made by an earlier
    # factory for a route that a later factory will own. Validate before any of
    # the specialized composers can overwrite a route or obscure its provenance.
    factories = (
        ("build_lifecycle_handlers", build_lifecycle_handlers, ()),
        ("build_virtual_handlers", build_virtual_handlers, ()),
        ("build_reconnect_handlers", build_reconnect_handlers, (Route.reconnect,)),
        ("build_audio_handlers", build_audio_handlers, (Route.audio,)),
        ("build_recovery_handlers", build_recovery_handlers, ()),
    )
    owners: dict[Route, str] = {}
    maps: list[Mapping[Route, Handler]] = []
    for name, factory, rebindings in factories:
        claims = factory(deps)
        for route in claims:
            if route in owners and route not in rebindings:
                raise ValueError(
                    f"session route {route.value!r} collision: {owners[route]} and {name}"
                )
            owners[route] = name
        maps.append(claims)

    lifecycle, virtual, reconnect, audio, recovery = maps
    handlers = compose_virtual_handlers(lifecycle, virtual)
    handlers = compose_reconnect_handlers(handlers, reconnect)
    handlers = compose_audio_handlers(handlers, audio)
    handlers = {**handlers, **recovery}

    missing = set(Route) - set(handlers)
    if missing:  # pragma: no cover - defensive: a missing route is a wiring bug
        raise RuntimeError(f"handler composition is missing routes: {sorted(r.value for r in missing)}")
    return handlers


def compose_manager(deps: ManagerDeps) -> SessionManager:
    """Build the session manager over the production handler composition."""

    return SessionManager(deps, compose_handlers(deps))


class _SystemClock:
    def now(self) -> float:
        return time.monotonic()


class _SystemScheduler:
    def call_at(self, when: float, callback: Callable[[], None]):
        return asyncio.get_running_loop().call_at(when, callback)


class _SystemIds:
    def new(self):
        return uuid4()


class _NoopRegistry:
    """Registry boundary for an explicitly injected test dependency bundle."""

    async def reconcile_previous(self) -> Result[tuple[object, ...]]:
        return Ok(value=())

    async def shutdown(self) -> Result[None]:
        return Ok(value=None)


@dataclass
class _ProductionGraph:
    deps: ManagerDeps
    manager: SessionManager
    registry: DaemonRegistry
    startup: StartupRecovery
    persistence: SessionPersistence
    report: Any
    gated: Mapping[str, str]


def _unavailable_features() -> BackendFeatures:
    return BackendFeatures(
        profile="unavailable",
        version="0",
        foreground_control=False,
        owner_handshake=False,
        multi_target=False,
        target_bound_pairing=False,
        physical_source=False,
        virtual_source=False,
        audio_probe=False,
        audio_restart=False,
        target_mute=False,
        keyring=False,
        provisional_commit=False,
        cancel_pairing=False,
        revoke_pairing=False,
        query_modes=False,
    )


def _capability_gates(report: Any) -> dict[str, str]:
    gated: dict[str, str] = {}
    labels = {
        "environment": "desktop environment is not proven for the supported capability set",
        "discovery": "receiver discovery evidence is unavailable",
        "mirror": "physical-source transport capability is not proven",
        "virtual_display": "virtual-display helper and output evidence are not proven",
        "audio": "PipeWire and portal audio evidence is unavailable",
        "remembered_pairing": "backend-owned keyring capability is not proven",
        "additional_virtual_outputs": "additional virtual outputs are not proven",
    }
    for name, reason in labels.items():
        if name != "environment" and getattr(report, name, False) is not True:
            gated[name] = reason
    # The production graph supplies a verifier that consumes fresh KScreen
    # evidence and checks the exact launched helper identity against its owner
    # environment. The environment report still gates virtual display when the
    # helper or supporting environment evidence is unavailable.
    if getattr(report, "remembered_pairing", False) is not True:
        gated["remembered_trust"] = "keyring capability was not proven by the environment probe"
    return gated


def _control_factory(process: LinuxProcessRunner, features: BackendFeatures):
    def make(record) -> PublishedDoubletakeControl:
        profile = PublishedDoubletakeProfile(
            socket_path=record.socket_path,
            name=features.profile,
            version=features.version,
            features=features,
            owner_token=record.owner_token,
            instance_uuid=record.instance_uuid,
            protocol_version=record.protocol_version,
        )
        return PublishedDoubletakeControl(process, profile)

    return make


async def _compose_production(config: RuntimeConfig) -> _ProductionGraph:
    """Construct all real adapters, leaving unsupported evidence gated."""

    process = LinuxProcessRunner()
    clock = _SystemClock()
    scheduler = _SystemScheduler()
    ids = _SystemIds()

    files = AtomicFiles(config.state_root)
    sessions = JsonSessionStore(files)
    receivers = JsonReceiverStore(files)
    ownerships = JsonOwnershipStore(files)
    daemons = JsonDaemonStore(files)

    backend = await probe_backend(process)
    features = backend.value if isinstance(backend, Ok) else _unavailable_features()

    resolver = IdentityResolver()
    discovery = ZeroconfDiscovery(resolver, clock, AzcMdnsClient(clock))
    portal = PortalClient()
    connectors = KscreenConnectorInventory(process)
    capture = PhysicalCaptureProvider(portal, connectors, features, ids=ids)

    helper = VirtualHelper(process, clock)
    observer = KScreenObserver(process, clock, scheduler)
    output_verifier = KScreenOutputVerifier(helper, observer, ownerships)
    creation_lock = CreationLock(config.runtime_root)
    app_instance_uuid = InstanceId(uuid4())

    transport_ref: dict[str, DoubleTakeTransport] = {}

    async def transport_released(record, context: OperationContext) -> Result[None]:
        transport = transport_ref.get("transport")
        if transport is None:
            return Err(error=diagnostic(ErrorCode.cleanup_timeout, record.receiver_id))
        # A stream remaining in either active or retired storage is still an
        # unconfirmed physical transport lease and must block display release.
        for stream in (*transport._streams.values(), *transport._retired.values()):
            handle = getattr(stream, "handle", None)
            if handle is not None and handle.receiver_id == record.receiver_id:
                return Err(error=diagnostic(ErrorCode.cleanup_timeout, record.receiver_id))
        return Ok(value=None)

    remover = DisplayRemover(
        helper,
        observer,
        ownerships,
        creation_lock,
        clock,
        process=process,
        output_verifier=output_verifier,
        transport_released=transport_released,
    )
    recovery = DisplayRecovery(
        helper,
        observer,
        ownerships,
        creation_lock,
        clock,
        remover,
        output_verifier=output_verifier,
    )
    creator = DisplayCreator(
        helper,
        observer,
        ownerships,
        creation_lock,
        runtime_directory=config.runtime_root,
        app_instance_uuid=app_instance_uuid,
        clock=clock,
        scheduler=scheduler,
        ids=ids,
        output_verifier=output_verifier,
    )
    display = KdeVirtualDisplayProvider(creator, recovery, remover, observer, helper)

    registry = DaemonRegistry(
        process=process,
        store=daemons,
        control_factory=_control_factory(process, features),
        features=features,
        runtime_directory=config.runtime_root,
        record_root=config.state_root,
        ids=ids,
        clock=clock,
        scheduler=scheduler,
    )

    fences = TrustFenceRegistry()
    credentials = BackendKeyringStore(UnsupportedControl(), features)
    transport = DoubleTakeTransport(
        _control_factory(process, features),
        registry,
        capture,
        credentials,
        receivers,
        fences,
        features,
        clock,
        scheduler,
        ids,
    )
    # DoubleTakeTransport owns the only control that is bound to a concrete
    # receiver/daemon lease. Rebind the real store to that control immediately;
    # UnsupportedControl is only the constructor-time fail-closed sentinel.
    credentials._control = transport._pairing._control
    transport_ref["transport"] = transport

    audio_probe = PipeWirePortalAudioProbe()
    probe = EnvironmentProbe.from_environment(
        process=process,
        helper=helper,
        capture=capture,
        audio_probe=audio_probe,
    )
    report = await probe.check()
    report_holder: dict[str, Any] = {"report": report}
    capabilities = lambda: report_holder["report"]
    activity = ActivityLog(max_entries=1000, capabilities=report)
    deps = ManagerDeps(
        transport=transport,
        pairing=transport,
        display=display,
        capture=capture,
        discovery=discovery,
        audio_probe=audio_probe,
        sessions=sessions,
        receivers=receivers,
        ownerships=ownerships,
        fences=fences,
        clock=clock,
        scheduler=scheduler,
        ids=ids,
        activity=activity,
        capabilities=capabilities,
        restart_daemon=registry.restart,
    )

    manager = compose_manager(deps)
    persistence = SessionPersistence(manager, sessions)
    startup = StartupRecovery(
        manager, deps, registry, credential_loader=credentials.load_credential_ref,
    )
    gated = _capability_gates(report)
    return _ProductionGraph(deps, manager, registry, startup, persistence, report, gated)


@dataclass
class Runtime:
    """A composed, runnable application instance."""

    config: RuntimeConfig
    deps: ManagerDeps
    manager: SessionManager
    registry: Any
    persistence: SessionPersistence
    startup_recovery: StartupRecovery
    shutdown_coordinator: ShutdownCoordinator
    presenter: Any = None
    dialogs: Any = None
    window: Any = None
    qt_app: Any = None
    gated: Mapping[str, str] = field(default_factory=dict)
    _diagnostics: Any = None
    _shutdown_hook: Callable[[], Awaitable[Result[None]]] | None = None
    _start_task: asyncio.Task[Result[None]] | None = field(default=None, init=False, repr=False)
    _shutdown_task: asyncio.Task[Result[None]] | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False)
    _closed: bool = field(default=False, init=False)
    _window_closed: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    @property
    def gated_capabilities(self) -> Mapping[str, str]:
        return dict(self.gated)

    def diagnostics(self):
        """Return the latest capability snapshot used by the session/UI layers."""

        if callable(self._diagnostics):
            return self._diagnostics()
        return self._diagnostics

    async def _start_once(self) -> Result[None]:
        self.persistence.start()
        recovery = await self.startup_recovery.run()
        if isinstance(recovery, Err):
            return recovery
        started = await self.deps.discovery.start()
        if isinstance(started, Err):
            return started
        refreshed = await self.deps.discovery.refresh()
        if isinstance(refreshed, Err):
            return refreshed
        if self.presenter is not None:
            await self.presenter.start()
        if self.window is not None:
            self.window.show()
        self._started = True
        return Ok(value=None)

    async def start(self) -> Result[None]:
        """Start recovery, discovery and the optional presenter once."""

        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start_once())
        return await asyncio.shield(self._start_task)

    async def wait_closed(self) -> None:
        """Keep the live application alive until the dashboard accepts its close.

        A close request is deferred by the presenter while bounded shutdown runs;
        failed cleanup leaves the window visible and does not end this wait.
        """

        await self._window_closed.wait()

    async def _shutdown_once(self) -> Result[None]:
        if self._shutdown_hook is not None:
            result = await self._shutdown_hook()
            return result if isinstance(result, (Ok, Err)) else Err(
                error=diagnostic(ErrorCode.cleanup_timeout)
            )
        return await self.shutdown_coordinator.run()

    async def shutdown(self) -> Result[None]:
        """Join one bounded shutdown transaction and replay its exact result."""

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown_once())
        result = await asyncio.shield(self._shutdown_task)
        self._closed = isinstance(result, Ok)
        return result


def _with_overrides(deps: ManagerDeps, provided: Mapping[str, Any]) -> ManagerDeps:
    changes = {
        name: provided[name]
        for name in _ADAPTER_OVERRIDES
        if name in provided and provided[name] is not getattr(deps, name)
    }
    return replace(deps, **changes) if changes else deps


def _build_live_presenter(runtime: Runtime, report_provider: Callable[[], Any]) -> None:
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QApplication

    from airplay_linux.ui.presenter import Presenter, QtDialogFactory
    from airplay_linux.ui.window import MainWindow

    class ApplicationWindow(MainWindow):
        def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
            super().closeEvent(event)
            if event.isAccepted():
                runtime._window_closed.set()

    qt_app = QApplication.instance() or QApplication([])
    window = ApplicationWindow()
    dialogs = QtDialogFactory(window)
    presenter = Presenter(
        window,
        runtime.manager,
        runtime.deps.discovery,
        runtime.deps.capture,
        runtime.deps.receivers,
        report_provider,
        runtime.deps.activity,
        runtime.deps.ownerships,
        dialogs,
        runtime.shutdown,
    )
    runtime.qt_app = qt_app
    runtime.window = window
    runtime.dialogs = dialogs
    runtime.presenter = presenter


async def build_runtime(
    config: RuntimeConfig, overrides: Mapping[str, Any] | None = None,
) -> Result[Runtime]:
    """Compose a runtime, using explicit dependency bundles only for tests."""

    provided = dict(overrides or {})
    unknown = set(provided) - OVERRIDABLE
    if unknown:
        raise TypeError(f"unknown runtime override: {sorted(unknown)}")

    deps_override = provided.get("deps")
    individual = set(provided) & _ADAPTER_OVERRIDES
    if deps_override is None and individual:
        raise TypeError("individual runtime adapter overrides require a deps override")

    if deps_override is None:
        graph = await _compose_production(config)
        deps = graph.deps
        registry = graph.registry
        startup = graph.startup
        persistence = graph.persistence
        diagnostics_provider = deps.capabilities
        gated = graph.gated
    else:
        if not isinstance(deps_override, ManagerDeps):
            raise TypeError("deps override must be a ManagerDeps")
        deps = _with_overrides(deps_override, provided)
        manager_for_persistence = compose_manager(deps)
        registry = _NoopRegistry()
        persistence = SessionPersistence(manager_for_persistence, deps.sessions)
        startup = StartupRecovery(manager_for_persistence, deps, registry)
        diagnostics_provider = deps.capabilities
        gated = {
            "virtual_display": (
                "helper/output association verifier not supplied by this composition; "
                "display creation refuses to publish without it"
            ),
            "remembered_trust": (
                "keyring control is not rebound to the owned daemon by this composition"
            ),
        }

    manager = graph.manager if deps_override is None else manager_for_persistence
    coordinator = ShutdownCoordinator(manager, deps, registry, persistence, deadline_seconds=15)
    presenter = provided["presenter"] if "presenter" in provided else None
    runtime = Runtime(
        config=config,
        deps=deps,
        manager=manager,
        registry=registry,
        persistence=persistence,
        startup_recovery=startup,
        shutdown_coordinator=coordinator,
        presenter=presenter,
        gated=gated,
        _diagnostics=diagnostics_provider,
        _shutdown_hook=provided.get("shutdown_hook"),
    )
    if presenter is None and config.live:
        _build_live_presenter(runtime, diagnostics_provider)
    return Ok(value=runtime)


__all__ = [
    "OVERRIDABLE",
    "Runtime",
    "RuntimeConfig",
    "build_runtime",
    "compose_handlers",
    "compose_manager",
]
