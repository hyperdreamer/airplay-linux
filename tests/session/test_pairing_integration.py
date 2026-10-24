"""Session discovery against production transport/trust, with a fake backend."""
from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest

from airplay_linux.backend.contract import AckReply
from airplay_linux.backend.control import BackendOperation
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.messages import Pair, RetryRevocation, SubmitPairing
from airplay_linux.domain.operations import OpaqueBackendToken, SensitiveText
from airplay_linux.domain.resources import Err, ErrorCode, Ok, TrustState
from airplay_linux.session.engine import SessionManager
from airplay_linux.session.mirror import build_lifecycle_handlers
from airplay_linux.session.testing import SessionRig
from airplay_linux.testing.adapters import Deferred, FakeCredentialStore
from airplay_linux.testing.builders import target
from airplay_linux.trust.coordinator import PairingCoordinator
from airplay_linux.trust.keyring import KeyringPairing


# Reuse the transport's deterministic backend/daemon seam. Importing this test
# module starts no processes, receiver connections, portal or desktop tooling.
_spec = importlib.util.spec_from_file_location(
    "session_transport_fakes", Path(__file__).parents[1] / "transport/test_doubletake.py",
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)


def production_rig(tmp_path):
    backend = _fakes.Harness(tmp_path)
    rig = SessionRig()
    deps = replace(
        rig.deps, transport=backend.transport, pairing=backend.transport,
        fences=backend.fences, clock=backend.clock, scheduler=backend.scheduler,
    )
    rig.manager = SessionManager(deps, build_lifecycle_handlers(deps))
    return rig, backend


async def settle_backend(rig):
    # The real coordinator/transport intentionally cross several task barriers.
    for _ in range(30):
        await rig.settle()


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_event", [True, False], ids=["discovered", "cached-view"])
async def test_idle_pair_registers_fresh_target_before_backend_begin(tmp_path, discovery_event):
    rig, backend = production_rig(tmp_path)
    fresh = target("new", address="192.0.2.50")
    if discovery_event:
        rig.discovery.add_target(fresh)
    else:
        rig.discovery.views[fresh.receiver_id] = rig.discovery.build_view(fresh, online=True)
    await rig.manager.dispatch(Pair(receiver_id=fresh.receiver_id))
    await settle_backend(rig)
    state = rig.manager.snapshot(fresh.receiver_id)
    assert state.challenge is not None, state.last_error
    assert backend.control.calls(BackendOperation.begin_pairing)
    assert state.stream is None
    assert not rig.display.handles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "revoke_fails"),
    [("endpoint", False), ("endpoint", True), ("removed", False), ("removed", True), ("receiver", False)],
)
async def test_discovery_retires_fence_before_held_coordinator_commit(tmp_path, change, revoke_fails):
    rig, backend = production_rig(tmp_path)
    rig.discovery.add_target(backend.a)
    await rig.manager.dispatch(Pair(receiver_id=backend.a.receiver_id))
    await settle_backend(rig)
    before = rig.manager.snapshot(backend.a.receiver_id)
    assert before.challenge is not None
    gate = _fakes.Gate()
    backend.control.gates[(BackendOperation.commit_pairing, backend.a.receiver_id)] = gate
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=backend.a.receiver_id, challenge_id=before.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await asyncio.wait_for(gate.entered.wait(), 1)
    if revoke_fails:
        # Keep revocation uncertain until the explicit retry, even if coordinator
        # cancellation and the retired submit both reach the same cleanup path.
        backend.control.results[BackendOperation.revoke_pairing].extend(
            Err(error=diagnostic(ErrorCode.keyring_unavailable)) for _ in range(8)
        )
    if change == "endpoint":
        fresh = target(endpoint_generation=2, address="192.0.2.77")
        rig.discovery.add_target(fresh)
    elif change == "receiver":
        rig.discovery.add_target(target("replacement", address=backend.a.canonical_host))
    else:
        rig.discovery.remove_target(backend.a.receiver_id)
    # No await: discovery must retire the fence before a suspended commit can run.
    fence = backend.fences.get(backend.a.receiver_id)
    assert fence is not None
    assert (fence.endpoint_fingerprint, fence.endpoint_generation) != (
        before.challenge.endpoint_fingerprint, before.challenge.endpoint_generation,
    )
    gate.release()
    await settle_backend(rig)
    state = rig.manager.snapshot(backend.a.receiver_id)
    assert state.trust is not TrustState.Trusted
    if change == "receiver":
        # The frozen transport cannot address a replaced receiver safely at its
        # former address. It must retain uncertainty, never revoke its successor.
        assert state.trust is TrustState.CleanupRequired
        assert not backend.control.calls(BackendOperation.revoke_pairing)
        await rig.manager.dispatch(Pair(receiver_id=target("replacement").receiver_id))
        await settle_backend(rig)
        replacement = rig.manager.snapshot(target("replacement").receiver_id)
        assert replacement.challenge is None
        assert replacement.trust is TrustState.CleanupRequired
        return
    assert backend.control.calls(BackendOperation.revoke_pairing)
    if revoke_fails:
        assert state.trust is TrustState.CleanupRequired
        backend.control.results[BackendOperation.revoke_pairing].clear()
        await rig.manager.dispatch(RetryRevocation(receiver_id=backend.a.receiver_id))
        await settle_backend(rig)
        assert rig.manager.snapshot(backend.a.receiver_id).trust is TrustState.Untrusted
    loaded = await backend.credentials.load_credential_ref(backend.a.receiver_id)
    assert isinstance(loaded, Ok)
    assert loaded.value is None


@pytest.mark.asyncio
async def test_separate_pairing_port_observes_discovery_fence_before_commit():
    class Credentials(FakeCredentialStore):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = Deferred()

        async def begin_pairing_token(self, destination, operation):
            return Ok(value=KeyringPairing(
                backend_token=OpaqueBackendToken("session-test-token"), credential_kind="pin",
            ))

        async def commit_pairing(self, provisional, generation):
            self.entered.set()
            await self.release.wait()
            return await super().commit_pairing(provisional, generation)

    class Control:
        async def request(self, operation, destination, payload, operation_context):
            return Ok(value=AckReply(operation=operation.value, target=destination.canonical_host))

    rig = SessionRig()
    credentials = Credentials()
    coordinator = PairingCoordinator(
        Control(), credentials, rig.receivers, rig.fences, rig.clock, rig.scheduler, rig.ids,
    )
    deps = replace(rig.deps, pairing=coordinator)
    rig.manager = SessionManager(deps, build_lifecycle_handlers(deps))
    destination = target()
    rig.discovery.add_target(destination)
    await rig.manager.dispatch(Pair(receiver_id=destination.receiver_id))
    await settle_backend(rig)
    before = rig.manager.snapshot(destination.receiver_id)
    assert before.challenge is not None
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=destination.receiver_id, challenge_id=before.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await asyncio.wait_for(credentials.entered.wait(), 1)
    replacement = target(endpoint_generation=2, address="192.0.2.22")
    rig.discovery.add_target(replacement)
    fence = rig.fences.get(destination.receiver_id)
    assert fence.endpoint_fingerprint == replacement.endpoint_fingerprint
    assert fence.active_challenge_id is None
    assert fence.trust_generation > before.trust_generation
    assert rig.manager.snapshot(destination.receiver_id).challenge is None
    assert rig.manager.snapshot(destination.receiver_id).session_generation == before.session_generation
    credentials.release.resolve(None)
    await settle_backend(rig)
    assert destination.receiver_id not in credentials.stored
    assert not coordinator.pending_cleanup(destination.receiver_id)
    assert rig.manager.snapshot(destination.receiver_id).trust is TrustState.Untrusted


@pytest.mark.asyncio
async def test_service_metadata_refresh_preserves_current_pairing_authority(tmp_path):
    rig, backend = production_rig(tmp_path)
    rig.discovery.add_target(backend.a)
    await rig.manager.dispatch(Pair(receiver_id=backend.a.receiver_id))
    await settle_backend(rig)
    before = rig.manager.snapshot(backend.a.receiver_id)
    assert before.challenge is not None
    refreshed = replace(backend.a, service_records=tuple(
        replace(record, seen_at=record.seen_at + 10, txt={"model": "refreshed"})
        for record in backend.a.service_records
    ))
    rig.discovery.add_target(refreshed)
    await settle_backend(rig)
    after = rig.manager.snapshot(backend.a.receiver_id)
    assert after.challenge == before.challenge
    assert after.trust_generation == before.trust_generation
    assert after.trust is TrustState.Pairing
    await rig.manager.dispatch(SubmitPairing(
        receiver_id=backend.a.receiver_id, challenge_id=before.challenge.challenge_id,
        value=SensitiveText("1234"),
    ))
    await settle_backend(rig)
    assert rig.manager.snapshot(backend.a.receiver_id).trust is TrustState.Trusted
