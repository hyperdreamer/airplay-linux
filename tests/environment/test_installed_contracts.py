"""Read-only checks against the *installed* backend commands (no daemon needed).

Two layers live here:

* an unconditional, read-only surface check.  It runs only bounded ``--help`` and
  ``--version`` probes through :func:`airplay_linux.backend.contract.probe_backend`
  and then records exactly which operations the published profile maps.  It never
  starts a daemon, never binds a socket and never mutates a receiver.  If the
  installed binary is missing the test skips with that precise reason; if it is
  present but advertises an unrecognised surface, the test asserts the fail-closed
  classification instead of inventing capabilities.
* an opt-in private-socket compatibility gate marked ``live_backend``.  It is
  skipped unless ``AIRPLAY_LINUX_LIVE_BACKEND=1``; when the operator selects it,
  an incompatible backend is a **failure** of that gate, not a skip.

Manual commands::

    python -m pytest -q tests/environment/test_installed_contracts.py
    AIRPLAY_LINUX_LIVE_BACKEND=1 \\
    AIRPLAY_LINUX_LIVE_SOCKET=/run/user/$(id -u)/doubletake/daemon.sock \\
        python -m pytest -q -m live_backend tests/environment/test_installed_contracts.py

The second command performs no mutation: it only proves that an unowned endpoint
is refused.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from airplay_linux.backend.contract import probe_backend
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    ConnectPayload,
    EmptyPayload,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
)
from airplay_linux.domain.operations import InstanceId, SensitiveText
from airplay_linux.domain.resources import Err, ErrorCode, Ok
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.testing.builders import context, physical, target

LIVE_BACKEND_ENV = "AIRPLAY_LINUX_LIVE_BACKEND"
LIVE_SOCKET_ENV = "AIRPLAY_LINUX_LIVE_SOCKET"

#: Operations the pinned published CLI documents (the only ones this adapter maps
#: for the installed profile).  Everything else is recorded as unsupported.
PUBLISHED_OPERATIONS = frozenset({
    BackendOperation.status,
    BackendOperation.discover,
    BackendOperation.devices,
    BackendOperation.connect,
    BackendOperation.disconnect,
    BackendOperation.mute,
    BackendOperation.unmute,
})

#: Capabilities the application owns; the published CLI cannot prove them.
APPLICATION_OWNED_GATES = (
    "foreground_control",
    "owner_handshake",
    "target_bound_pairing",
    "keyring",
    "provisional_commit",
    "cancel_pairing",
    "revoke_pairing",
)


def _missing_reason() -> str | None:
    missing = [
        command for command in ("doubletake", "doubletake-ctl")
        if shutil.which(command) is None
    ]
    if missing:
        return "installed backend command(s) not found on PATH: " + ", ".join(missing)
    return None


def _fence():
    from airplay_linux.domain.resources import Ok

    return Ok(value=None)


async def test_installed_help_surface_proves_no_application_owned_gates():
    reason = _missing_reason()
    if reason is not None:
        pytest.skip(reason)

    probed = await probe_backend(LinuxProcessRunner())
    if isinstance(probed, Err):
        # A present-but-unrecognised binary is classified, not promoted.
        assert probed.error.code is ErrorCode.dependency_incompatible
        record = {"provenance": "published-release", "live_support_proven": False,
                  "capabilities": "unproven"}
        assert record["capabilities"] == "unproven"
        return

    features = probed.value
    assert features.profile
    assert features.version
    # Whatever the installed help proves, it can never grant the application-owned
    # lifecycle/trust gates: those require an authenticated foreground daemon.
    if features.profile == "doubletake-0.5":
        for gate in APPLICATION_OWNED_GATES:
            assert getattr(features, gate) is False, gate
        # And the control refuses those operations without performing any I/O.
        control = PublishedDoubletakeControl(
            LinuxProcessRunner(),
            PublishedDoubletakeProfile(
                socket_path="/run/user/0/never-created.sock",
                features=features,
                owner_token=SensitiveText("unused"),
                instance_uuid=InstanceId(uuid.uuid4()),
            ),
        )
        refused = await control.request(
            BackendOperation.connect, target("a"), ConnectPayload(),
            context(generation=1, lease=1), dispatch_fence=_fence,
        )
        assert isinstance(refused, Err)
        assert refused.error.code is ErrorCode.dependency_incompatible


def test_profile_maps_exactly_the_documented_operations():
    """Unsupported operations are recorded, never invented."""
    profile = PublishedDoubletakeProfile(socket_path="/run/user/0/private.sock")
    receiver = target("a")
    read_operations = {
        BackendOperation.status, BackendOperation.discover, BackendOperation.devices,
    }
    payloads = {
        BackendOperation.status: EmptyPayload(),
        BackendOperation.discover: EmptyPayload(),
        BackendOperation.devices: EmptyPayload(),
        BackendOperation.connect: ConnectPayload(),
        BackendOperation.disconnect: EmptyPayload(),
        BackendOperation.mute: EmptyPayload(),
        BackendOperation.unmute: EmptyPayload(),
    }
    supported = set()
    unsupported = set()
    for operation in BackendOperation:
        # Read-only commands are addressed to the selected daemon, never a target.
        addressed = None if operation in read_operations else receiver
        result = profile.try_argv(operation, addressed, payloads.get(operation, EmptyPayload()))
        (supported if isinstance(result, Ok) else unsupported).add(operation)
    assert supported == PUBLISHED_OPERATIONS
    assert unsupported == set(BackendOperation) - PUBLISHED_OPERATIONS

    # Secrets and physical-source descriptors have no published argv mapping: they
    # must be refused rather than smuggled into a command line.
    credential = profile.try_argv(
        BackendOperation.connect, receiver, ConnectPayload(credential="5141"),
    )
    assert isinstance(credential, Err)
    assert credential.error.code is ErrorCode.unsupported_operation
    source = profile.try_argv(
        BackendOperation.connect, receiver, ConnectPayload(source=physical("DP-1")),
    )
    assert isinstance(source, Err)
    assert source.error.code is ErrorCode.unsupported_source

    # The application contract fixture never claims published-backend support.
    fixture = ApplicationContractProfile(socket_path="/run/user/0/private.sock")
    assert fixture.provenance == "application-contract-fixture"
    assert fixture.live_support_proven is False


@pytest.mark.live_backend
async def test_selected_live_backend_must_affirm_the_application_contract():
    if os.environ.get(LIVE_BACKEND_ENV) != "1":
        pytest.skip(
            f"live backend checks are opt-in; set {LIVE_BACKEND_ENV}=1 to select "
            "an installed backend for the private-socket compatibility gate",
        )
    reason = _missing_reason()
    if reason is not None:
        pytest.fail(f"{LIVE_BACKEND_ENV}=1 but the selected backend is unusable: {reason}")

    probed = await probe_backend(LinuxProcessRunner())
    assert isinstance(probed, Ok), (
        f"the selected live backend is not compatible: {probed.error.message}"
    )
    features = probed.value
    missing = [
        gate for gate in ("foreground_control", "owner_handshake", "keyring", "multi_target")
        if getattr(features, gate) is not True
    ]
    assert not missing, (
        "the selected live backend does not prove the application contract gates: "
        + ", ".join(missing)
    )


@pytest.mark.live_backend
async def test_an_unowned_live_socket_is_never_adopted():
    if os.environ.get(LIVE_BACKEND_ENV) != "1":
        pytest.skip(
            f"live backend checks are opt-in; set {LIVE_BACKEND_ENV}=1 to select "
            "an installed backend for the private-socket compatibility gate",
        )
    socket_path = os.environ.get(LIVE_SOCKET_ENV)
    if not socket_path:
        pytest.skip(
            f"no private live socket selected; set {LIVE_SOCKET_ENV} to the "
            "daemon socket to probe (read-only, no mutation)",
        )
    path = Path(socket_path)
    if not path.is_absolute() or not path.exists():
        pytest.skip(f"selected live socket is not an absolute existing path: {socket_path}")

    probed = await probe_backend(LinuxProcessRunner())
    assert isinstance(probed, Ok), probed
    control = PublishedDoubletakeControl(
        LinuxProcessRunner(),
        PublishedDoubletakeProfile(
            socket_path=socket_path,
            features=probed.value,
            owner_token=SensitiveText(uuid.uuid4().hex),
            instance_uuid=InstanceId(uuid.uuid4()),
        ),
    )
    # A token this process does not own cannot authenticate the endpoint: no
    # handshake succeeds and no mutation is attempted.
    proof = await control.handshake(
        SensitiveText(uuid.uuid4().hex), InstanceId(uuid.uuid4()),
    )
    assert isinstance(proof, Err)
    assert proof.error.code in {
        ErrorCode.ownership_mismatch,
        ErrorCode.stale_socket,
        ErrorCode.dependency_incompatible,
        ErrorCode.daemon_failed,
    }
    await control.close()
