"""Target-bound transport and pairing on one authority object."""

from .doubletake import DoubleTakeTransport
from .targets import TargetRegistry

__all__ = ["DoubleTakeTransport", "TargetRegistry"]
