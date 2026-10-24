"""Deterministic Virtual scenarios; no desktop, portal, or receiver I/O."""
from __future__ import annotations

from dataclasses import replace

from airplay_linux.domain.messages import EmptyPayload, TransportEvent, TransportEventKind
from airplay_linux.domain.operations import StreamHandle
from airplay_linux.domain.resources import ResolutionDecision, ResolutionEvidence, Size
from airplay_linux.session.engine import SessionManager
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.session.virtual import build_virtual_handlers, compose_virtual_handlers
from airplay_linux.testing.adapters import FakeTransport
from airplay_linux.testing.builders import target

HD = Size(width=1920, height=1080)
SMALL = Size(width=1280, height=720)


def virtual_handlers(deps):
    lifecycle = build_lifecycle_handlers(deps)
    virtual = build_virtual_handlers(deps)
    duplicates = lifecycle.keys() & virtual.keys()
    if duplicates:
        raise ValueError(f"duplicate session routes: {duplicates}")
    return compose_virtual_handlers(lifecycle, virtual)


class ContextTransport(FakeTransport):
    """The frozen fake allocates a lease; sessions must echo the requested lease."""

    def _new_handle(self, destination, operation):
        handle = super()._new_handle(destination, operation)
        self.streams.pop(handle.lease_id)
        handle = replace(handle, lease_id=operation.transport_lease_id)
        self.streams[handle.lease_id] = handle
        return handle


def make_rig(*, transport=None, display=None, ownerships=None):
    rig = SessionRig()
    for subscription in rig.manager._subscriptions:
        subscription.close()
    rig.transport = transport if transport is not None else ContextTransport()
    if display is not None:
        rig.display = display
        rig.ownerships = ownerships if ownerships is not None else display.ownership
    rig.deps = replace(
        rig.deps, transport=rig.transport, display=rig.display, ownerships=rig.ownerships,
    )
    for adapter in (
        rig.transport, rig.display, getattr(rig.display, "process", None), rig.ownerships,
        rig.pairing, rig.audio, rig.receivers, rig.capture,
    ):
        if adapter is not None:
            adapter.journal = rig.journal
    rig.manager = SessionManager(rig.deps, virtual_handlers(rig.deps))
    return rig


def decision(destination=None, size=HD):
    destination = destination or target()
    return ResolutionDecision(
        size=size, source="user",
        evidence=ResolutionEvidence(
            origin="picker", reliable=True, receiver_id=destination.receiver_id,
            endpoint_fingerprint=destination.endpoint_fingerprint,
        ),
    )


def stream_event(rig, kind, destination=None, payload=None):
    destination = destination or target()
    state = rig.manager.snapshot(destination.receiver_id)
    handle = state.stream
    assert isinstance(handle, StreamHandle)
    operation = replace(
        rig.manager.context(destination.receiver_id).operation(),
        operation_id=handle.start_operation_id,
        session_generation=state.session_generation,
        endpoint_generation=handle.endpoint_generation,
        transport_lease_id=handle.lease_id,
    )
    return TransportEvent(
        kind=kind, receiver_id=destination.receiver_id, stream=handle,
        endpoint_fingerprint=handle.endpoint_fingerprint,
        endpoint_generation=handle.endpoint_generation,
        daemon_instance_uuid=handle.daemon_instance_uuid, context=operation,
        payload=payload or EmptyPayload(),
    )


async def settle(rig):
    # Resolution loads, provider progress, cleanup and persistence cross distinct
    # actor/effect boundaries. No wall-clock wait or external service is involved.
    for _ in range(12):
        await rig.settle()


def production_display(tmp_path, *, verifier):
    """Real provider with a private, deterministic process/KScreen world."""
    import json

    from airplay_linux.display.creation import DisplayCreator
    from airplay_linux.display.helper import VirtualHelper
    from airplay_linux.display.kde import KdeVirtualDisplayProvider
    from airplay_linux.display.kscreen import KScreenObserver
    from airplay_linux.display.locking import CreationLock
    from airplay_linux.display.recovery import DisplayRecovery
    from airplay_linux.display.removal import DisplayRemover
    from airplay_linux.domain.ports import CommandOutput
    from airplay_linux.domain.resources import Err, ErrorCode, Ok
    from airplay_linux.diagnostics import diagnostic
    from airplay_linux.testing.adapters import FakeProcess, MemoryOwnershipStore, SequenceIds
    from airplay_linux.testing.time import FakeClock, FakeScheduler
    from uuid import UUID

    class Desktop(FakeProcess):
        def __init__(self):
            super().__init__()
            self.entries = []
            self.associations = {}

        async def run(self, spec, timeout, stdin=None):
            if spec.argv[0] == "krfb-virtualmonitor":
                text = "--resolution <WxH>" if "--help" in spec.argv else "krfb-virtualmonitor 24.08.2"
                return Ok(value=CommandOutput(exit_code=0, stdout=text.encode(), stderr=b""))
            assert spec.argv[0] == "kscreen-doctor"
            if spec.argv[1:] == ("-j",):
                return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps({"outputs": self.entries}).encode(), stderr=b""))
            for token in spec.argv[1:]:
                _, output_id, setting, *value = token.split(".")
                entry = next(item for item in self.entries if str(item["id"]) == output_id)
                if setting in {"enable", "disable"}:
                    entry["enabled"] = setting == "enable"
                elif setting == "position":
                    x, y = map(int, value[0].split(","))
                    entry["pos"] = {"x": x, "y": y}
                elif setting == "scale":
                    entry["scale"] = float(".".join(value))
                elif setting == "mode":
                    entry["currentModeId"] = value[0]
                    entry["size"] = next(mode["size"] for mode in entry["modes"] if mode["id"] == value[0])
                else:
                    raise AssertionError("unexpected KScreen operation")
            return Ok(value=CommandOutput(exit_code=0, stdout=b"", stderr=b""))

        async def spawn(self, spec):
            result = await super().spawn(spec)
            width, height = map(int, spec.argv[spec.argv.index("--resolution") + 1].split("x"))
            self.entries.append(dict(
                id=7, name="Virtual-owned", type="Virtual", enabled=True,
                pos={"x": 0, "y": 0}, scale=1, currentModeId="1" if width == HD.width else "2",
                size={"width": width, "height": height},
                modes=[{"id": str(index), "size": {"width": size.width, "height": size.height}, "refreshRate": 60}
                       for index, size in enumerate((HD, SMALL), 1)],
            ))
            self.associations[result.value] = ("7", "Virtual-owned")
            return result

        async def verify_output(self, owner, runtime, identity, candidate):
            if self.owned(identity, owner, runtime) and self.associations.get(identity) == (candidate.output_id, candidate.connector):
                return Ok(value=None)
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))

        def _forget(self, identity):
            pair = self.associations.pop(identity, None)
            self.entries = [entry for entry in self.entries if (str(entry["id"]), entry["name"]) != pair]
            super()._forget(identity)

    class Observer(KScreenObserver):
        async def _bounded(self, operation, timeout):
            # This fixture's process returns immediately. Exercise the real
            # parser/configuration/readback without installing real timers.
            return await operation

        async def wait_for(self, predicate, deadline):
            result = await self.outputs()
            if isinstance(result, Ok) and predicate(result.value):
                return result
            clock.advance(max(0, deadline - clock.now()))
            return Err(error=diagnostic(ErrorCode.cleanup_timeout))

    process, clock, store = Desktop(), FakeClock(), MemoryOwnershipStore()
    helper = VirtualHelper(process, clock)
    observer = Observer(process, clock, FakeScheduler(clock))
    lock = CreationLock(tmp_path / "runtime")
    verify = process.verify_output if verifier else None

    async def released(record, operation):
        return Ok(value=None)

    remover = DisplayRemover(helper, observer, store, lock, clock, process=process,
                             output_verifier=verify, transport_released=released)
    recovery = DisplayRecovery(helper, observer, store, lock, clock, remover, output_verifier=verify)
    creator = DisplayCreator(
        helper, observer, store, lock, str(tmp_path / "instance"), UUID(int=123),
        clock, FakeScheduler(clock), SequenceIds(seed=2500), output_verifier=verify,
    )
    return KdeVirtualDisplayProvider(creator, recovery, remover, observer, helper), process, store
