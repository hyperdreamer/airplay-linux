"""Current endpoint admission and conservative target-only cleanup routing."""

from __future__ import annotations

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.identity import ReceiverId, ReceiverTarget
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result


def _identity(target: ReceiverTarget) -> tuple:
    # Discovery timestamps and service TXT refreshes do not supersede an endpoint.
    return (
        target.receiver_id, target.endpoint_fingerprint, target.endpoint_generation,
        target.canonical_host, target.service_port, target.protocol,
    )


class TargetRegistry:
    """One current receiver per backend address; remember replacements for cleanup.

    The backend accepts an address, not an exact stream identifier. Removing a
    replacement from discovery cannot restore proof that an older stream still
    owns that address, so address ownership history outlives discovery removal.
    """

    def __init__(self) -> None:
        self._targets: dict[ReceiverId, ReceiverTarget] = {}
        self._addresses: dict[str, ReceiverTarget] = {}

    def update(self, target: ReceiverTarget | None, receiver_id: ReceiverId) -> None:
        if target is None:
            self._targets.pop(receiver_id, None)
            return
        if target.receiver_id != receiver_id:
            # A mismatched update must not admit either supplied identity.
            self._targets.pop(receiver_id, None)
            return
        for other_id, other in tuple(self._targets.items()):
            if other_id != receiver_id and other.canonical_host == target.canonical_host:
                del self._targets[other_id]
        self._targets[receiver_id] = target
        self._addresses[target.canonical_host] = target

    def require_current(self, target: ReceiverTarget) -> Result[None]:
        current = self._targets.get(target.receiver_id)
        if current is None or _identity(current) != _identity(target):
            return Err(error=diagnostic(ErrorCode.stale_target))
        return Ok(value=None)

    def require_cleanup(self, target: ReceiverTarget) -> Result[None]:
        last = self._addresses.get(target.canonical_host)
        if last is None or _identity(last) != _identity(target):
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))
        return Ok(value=None)


__all__ = ["TargetRegistry"]
