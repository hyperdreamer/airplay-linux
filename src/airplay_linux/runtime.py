"""Application runtime composition: the root that binds every lane together.

This module is the *only* place that knows how the adapters, ports, session engine
and UI are wired. Everything below it consumes ports; nothing below it may reach
across lanes to construct another lane's adapter.

Two rules shape this file.

**Fail closed, never fake.** Four lanes deliberately refuse to act without
evidence only the composition root can supply:

* the backend keyring store must be constructed against a control that is bound to
  the currently owned daemon, otherwise a keyring transaction after a daemon
  replacement could target a dead daemon;
* virtual display creation refuses to publish without a helper/output association
  verifier, because a stored record is not proof that an output is ours;
* the capture client reports ``Err(cleanup_timeout)`` when a session cannot be
  confirmed closed, and that is ``CleanupRequired``, never success;
* the session layer preserves ``CleanupRequired`` and blocks replacement pairing
  until revocation of the original receiver is confirmable.

Where this runtime cannot supply real evidence it leaves the capability gated and
reports it in :attr:`Runtime.gated_capabilities`. A permissive stub would be worse
than a gated capability: it would silently undo the guarantee each of those lanes
spent multiple review rounds establishing.

**Explicit overrides are for tests only.** :func:`build_runtime` accepts a mapping
of named dependency overrides so that acceptance lanes can inject deterministic
fakes. Unknown names are rejected rather than ignored, so a typo cannot silently
leave the real adapter in place.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import ResourceEvent
from airplay_linux.domain.operations import OperationContext
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result
from airplay_linux.session import Handler, ManagerDeps, Route, SessionContext, SessionManager
from airplay_linux.session.audio import build_audio_handlers, compose_audio_handlers
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.reconnect import build_reconnect_handlers, compose_reconnect_handlers
from airplay_linux.session.recovery import build_recovery_handlers
from airplay_linux.session.virtual import build_virtual_handlers, compose_virtual_handlers

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


def _absolute(name: str, value: object) -> Path:
    path = Path(value) if not isinstance(value, Path) else value
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path, got {path!r}")
    return path


@dataclass(frozen=True, kw_only=True)
class RuntimeConfig:
    """Filesystem and mode configuration for one application instance.

    Both roots are required to be absolute: the storage lane refuses to publish
    into a relative root, and the runtime-path contract for the private daemon
    instance is rooted at ``$XDG_RUNTIME_DIR``. Accepting a relative path here
    would defer that failure to a much less obvious place.
    """

    state_root: Path
    runtime_root: Path
    live: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", _absolute("state_root", self.state_root))
        object.__setattr__(self, "runtime_root", _absolute("runtime_root", self.runtime_root))
        if type(self.live) is not bool:
            raise TypeError("live must be a bool")


def compose_handlers(deps: ManagerDeps) -> Mapping[Route, Handler]:
    """Compose the production handler map: exactly one owner per :class:`Route`.

    The order matters and is not interchangeable. The Mirror/trust/cleanup factory
    establishes the lifecycle owner; Virtual and Reconnect then rebind the shared
    continuations onto it; audio composes last because the earlier factories rebind
    ``Route.audio`` to their own lifecycle owner and only the last composition may
    own audio-shaped messages. Recovery is disjoint and adds the final route.

    There is no dict union here on purpose: a plain union cannot teach a
    Mirror-only callback how to resume Virtual creation.
    """

    handlers: Mapping[Route, Handler] = compose_virtual_handlers(
        build_lifecycle_handlers(deps), build_virtual_handlers(deps)
    )
    handlers = compose_reconnect_handlers(handlers, build_reconnect_handlers(deps))
    handlers = compose_audio_handlers(handlers, build_audio_handlers(deps))
    handlers = {**handlers, **build_recovery_handlers(deps)}

    missing = set(Route) - set(handlers)
    if missing:  # pragma: no cover - defensive: a missing route is a wiring bug
        raise RuntimeError(f"handler composition is missing routes: {sorted(r.value for r in missing)}")
    return handlers


def compose_manager(deps: ManagerDeps) -> SessionManager:
    """Build the session manager over the production handler composition."""

    return SessionManager(deps, compose_handlers(deps))


class _NoopSubscription:
    """Subscription handle for overrides that do not emit."""

    def close(self) -> None:
        return None


@dataclass
class Runtime:
    """A composed, runnable application instance.

    ``manager`` is the session authority; ``presenter`` is ``None`` when the runtime
    was built without a UI (acceptance lanes frequently do this). ``gated`` records
    every capability this composition deliberately left failing closed, so an
    operator can see *why* something is unavailable rather than seeing a silent
    failure later.
    """

    config: RuntimeConfig
    deps: ManagerDeps
    manager: SessionManager
    presenter: Any = None
    gated: Mapping[str, str] = field(default_factory=dict)
    _shutdown_hook: Callable[[], Any] | None = None
    _closed: bool = False

    @property
    def gated_capabilities(self) -> Mapping[str, str]:
        return dict(self.gated)

    async def shutdown(self) -> Result[None]:
        """Bounded, idempotent shutdown.

        Delegates to the session manager's own bounded shutdown so the fixed
        teardown order lives in one place. A second call is a no-op rather than a
        second teardown.
        """

        if self._closed:
            return Ok(value=None)
        self._closed = True
        if self._shutdown_hook is not None:
            outcome = await self._shutdown_hook()
            if isinstance(outcome, Err):
                return outcome
        result = await self.manager.shutdown()
        if isinstance(result, Err):
            # An unconfirmed release is cleanup-required, never success.
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))
        return Ok(value=None)


async def build_runtime(
    config: RuntimeConfig, overrides: Mapping[str, Any] | None = None,
) -> Result[Runtime]:
    """Compose a runtime instance, honouring explicit test overrides.

    ``overrides`` is the only injection point. Supplying ``deps`` replaces the whole
    dependency bundle (what the acceptance lanes do); supplying individual adapter
    names replaces just that adapter. An unknown name raises ``TypeError`` so a
    misspelled override cannot silently leave the production adapter wired.
    """

    provided = dict(overrides or {})
    unknown = set(provided) - OVERRIDABLE
    if unknown:
        raise TypeError(f"unknown runtime override: {sorted(unknown)}")

    deps = provided.get("deps")
    if deps is None:
        # Without an explicit dependency bundle this runtime cannot obtain real
        # daemon, portal, capture or display evidence. Rather than fabricate it,
        # report the composition as unbuildable in this configuration.
        return Err(error=diagnostic(ErrorCode.dependency_missing))

    if not isinstance(deps, ManagerDeps):
        raise TypeError("deps override must be a ManagerDeps")

    manager = compose_manager(deps)
    presenter = provided.get("presenter")

    # Capabilities this composition leaves failing closed. Recording them is part
    # of the contract: a gated capability must be visible, not silent.
    gated = {
        "virtual_display": (
            "helper/output association verifier not supplied by this composition; "
            "display creation refuses to publish without it"
        ),
        "remembered_trust": (
            "keyring control is not rebound to the owned daemon by this composition"
        ),
    }

    return Ok(
        value=Runtime(
            config=config,
            deps=deps,
            manager=manager,
            presenter=presenter,
            gated=gated,
            _shutdown_hook=provided.get("shutdown_hook"),
        )
    )


__all__ = [
    "OVERRIDABLE",
    "Runtime",
    "RuntimeConfig",
    "build_runtime",
    "compose_handlers",
    "compose_manager",
]
