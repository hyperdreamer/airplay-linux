"""The installed production witness remains inert until the activation task."""
from __future__ import annotations

from airplay_linux import runtime as composition
from airplay_linux.domain.resources import Err, ErrorCode
from tests.process.test_credential_probe import (  # shared offline boundaries, real graph
    namespace_probe_witness,
    recording_probe_processes,
)


async def test_installed_probe_stays_closed_before_activation(recording_probe_processes, monkeypatch):
    c = recording_probe_processes
    assert composition._OWNED_DAEMON_EPOCH_ACTIVE is False
    installed = []
    original = composition._install_owned_daemon_epoch

    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        assert all(getattr(result, member) is not None for member in (
            "source_verifier", "receipts", "endpoint_verifier", "strict_teardown",
            "shared_authority", "credential_proof",
        ))
        installed.append(result)
        return result

    monkeypatch.setattr(composition, "_install_owned_daemon_epoch", observe)
    runtime = await c.build_complete_production_and_probe(activate=False)
    try:
        assert len(installed) == 1
        assert c.outer_unshare == [] and c.inner_launches == []
        assert not c.outer.spawned
        assert not any(observation.kind in {"direct_backend", "outer_namespace"}
                       for observation in c.outer.invocations)
        assert runtime.registry._process is c.outer
        assert runtime.deps.transport._features.source_reporting_complete
        assert runtime.deps.transport._features.owned_daemon_control
        assert not runtime.deps.transport._features.keyring
        assert not runtime.deps.transport._features.foreground_control
        assert not runtime.deps.transport._features.owner_handshake
        for attempt in (runtime.registry.ensure, runtime.deps.restart_daemon):
            result = await attempt()
            assert isinstance(result, Err) and result.error.code is ErrorCode.dependency_incompatible
        assert c.events == ["derive", "registry"]
    finally:
        await runtime.shutdown()
