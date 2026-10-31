"""Captured public-daemon launch eligibility and argv, with no launch side effect."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from airplay_linux.backend.contract import BackendFeatures


class LaunchCapability(Protocol):
    production_graph: bool

    def available(self) -> bool: ...

    def argv(self, socket_path: str) -> tuple[str, ...]: ...

    def ambient_socket(self) -> str | None: ...


def _private_root(root: Path) -> bool:
    """Read evidence once, walking every component without following symlinks."""
    if not root.is_absolute() or root == Path("/") or ".." in root.parts:
        return False
    descriptor = None
    try:
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        for part in root.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        return (
            info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700
            and os.access(".", os.W_OK | os.X_OK, dir_fd=descriptor, effective_ids=True)
        )
    except (OSError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True, init=False)
class ProductionLaunchCapability:
    """Snapshot of verified surface/root evidence; availability never probes.

    The recognized profiles both affirm ``daemonize``, ``socket`` and
    ``cred-backend`` in their exact published help fingerprint. This grants no
    permission to execute: runtime installation and lifecycle authority remain
    independent admission requirements.
    """

    production_graph: bool
    _root: Path = field(repr=False)
    _executable: str = field(repr=False)
    _ambient: str | None = field(repr=False)
    _eligible: bool = field(repr=False)

    def __init__(
        self, probed: BackendFeatures, runtime_root: Path, *,
        executable: str = "doubletake", production_graph: bool = False,
        ambient_socket: str | None = None,
    ) -> None:
        if type(production_graph) is not bool:
            raise TypeError("production graph must be a boolean")
        if type(executable) is not str or not executable or "\0" in executable:
            raise ValueError("invalid backend executable")
        if ambient_socket is not None and type(ambient_socket) is not str:
            raise TypeError("ambient socket must be text or None")
        root = Path(runtime_root)
        surface = (
            type(probed) is BackendFeatures
            and probed.profile in {"doubletake-0.5", "doubletake-0.5+source"}
            and probed.version == "0.5.0"
        )
        eligible = bool(surface and production_graph and ambient_socket is None and _private_root(root))
        object.__setattr__(self, "production_graph", production_graph)
        object.__setattr__(self, "_root", root)
        object.__setattr__(self, "_executable", executable)
        object.__setattr__(self, "_ambient", ambient_socket)
        object.__setattr__(self, "_eligible", eligible)

    def available(self) -> bool:
        return self._eligible

    def ambient_socket(self) -> str | None:
        return self._ambient

    def argv(self, socket_path: str) -> tuple[str, ...]:
        # Validate lexical containment before any possible consumer performs I/O.
        # No resolve/stat here: this object contains captured eligibility only;
        # receipt/endpoint verification must revalidate live filesystem evidence.
        if (
            not self._eligible or type(socket_path) is not str
            or not socket_path.startswith("/") or "\0" in socket_path
            or any(part in {"", ".", ".."} for part in socket_path.split("/")[1:])
        ):
            raise ValueError("invalid owned daemon socket path")
        path = Path(socket_path)
        try:
            relative = path.relative_to(self._root)
            valid = bool(relative.parts) and len(os.fsencode(socket_path)) < 108
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise ValueError("invalid owned daemon socket path")
        return (self._executable, "-daemonize", "-socket", socket_path, "-cred-backend", "keyring")


__all__ = ["LaunchCapability", "ProductionLaunchCapability"]
