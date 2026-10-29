"""Opt-in live KDE Wayland checks (never part of ordinary CI).

Nothing in this module runs without ``AIRPLAY_LINUX_LIVE_KDE=1``; without the
opt-in every test skips with the exact reason.  When opted in, the checks are
read-mostly against the real compositor, and the only mutation is one
*explicitly configured* helper-owned output whose exact connector is created,
observed, and removed by this test (``AIRPLAY_LINUX_LIVE_KDE_CREATE=1``).

The live checks are deliberately limited to what KDE can actually prove:

* ``krfb-virtualmonitor`` documents the ``--resolution`` contract we depend on;
* ``kscreen-doctor -j`` parses into a baseline;
* a launched helper appears as exactly one new virtual output of the requested
  size, carries this process's ownership environment, and disappears again after
  the exact owned helper is stopped;
* a provider without an explicitly configured output verifier fails closed. The
  production composition supplies a KScreen verifier, but this standalone check
  intentionally does not exercise production association evidence;
* without ``AIRPLAY_LINUX_LIVE_KDE_CREATE=1`` every live check here is read-only.

Manual commands::

    AIRPLAY_LINUX_LIVE_KDE=1 python -m pytest -q -m live_kde tests/environment/test_live_kde.py
    AIRPLAY_LINUX_LIVE_KDE=1 AIRPLAY_LINUX_LIVE_KDE_CREATE=1 \\
        python -m pytest -q -m live_kde tests/environment/test_live_kde.py
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import pytest

from airplay_linux.display.creation import DisplayCreator
from airplay_linux.display.helper import VirtualHelper
from airplay_linux.display.kde import KdeVirtualDisplayProvider
from airplay_linux.display.kscreen import KScreenObserver
from airplay_linux.display.locking import CreationLock
from airplay_linux.display.recovery import DisplayRecovery
from airplay_linux.display.removal import DisplayRemover
from airplay_linux.domain.identity import ReceiverId
from airplay_linux.domain.operations import OperationContext, OperationId
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Size
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.storage import AtomicFiles, JsonOwnershipStore

LIVE_KDE_ENV = "AIRPLAY_LINUX_LIVE_KDE"
LIVE_KDE_CREATE_ENV = "AIRPLAY_LINUX_LIVE_KDE_CREATE"
LIVE_SIZE_ENV = "AIRPLAY_LINUX_LIVE_KDE_SIZE"

#: Every test in this module is a live-environment check; none runs by default.
pytestmark = pytest.mark.live_kde


class _Clock:
    import time as _time

    now = staticmethod(_time.monotonic)


class _Scheduler:
    import asyncio as _asyncio

    def call_at(self, when, callback):
        return self._asyncio.get_running_loop().call_at(when, callback)


class _Ids:
    new = staticmethod(uuid.uuid4)


def _skip_reason() -> str | None:
    if os.environ.get(LIVE_KDE_ENV) != "1":
        return (
            f"live KDE checks are opt-in; set {LIVE_KDE_ENV}=1 to run them against "
            "this KDE Wayland session"
        )
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session != "wayland":
        return f"the current session is not Wayland (XDG_SESSION_TYPE={session or 'unset'!r})"
    desktops = " ".join(
        value.strip().lower()
        for value in (
            os.environ.get("XDG_CURRENT_DESKTOP", ""),
            os.environ.get("XDG_SESSION_DESKTOP", ""),
        )
        if value.strip()
    )
    if "kde" not in desktops:
        return f"the current desktop is not KDE (XDG_CURRENT_DESKTOP={desktops or 'unset'!r})"
    for command in ("kscreen-doctor", "krfb-virtualmonitor"):
        if shutil.which(command) is None:
            return f"required KDE command is not on PATH: {command}"
    return None


@pytest.fixture(scope="module")
def live_kde():
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    return True


def _requested_size() -> Size:
    text = os.environ.get(LIVE_SIZE_ENV, "1280x720").strip().lower()
    try:
        width, height = (int(part) for part in text.split("x", 1))
        return Size(width=width, height=height)
    except (TypeError, ValueError):
        pytest.fail(f"{LIVE_SIZE_ENV} must look like 1280x720, got {text!r}")


async def test_live_helper_documents_the_resolution_contract(live_kde):
    helper = VirtualHelper(LinuxProcessRunner(), _Clock())
    probed = await helper.probe()
    assert isinstance(probed, Ok), (
        "the live krfb-virtualmonitor does not document the required --resolution "
        f"contract: {probed.error.message}"
    )
    assert probed.value.distinct_outputs is False
    assert probed.value.capability_fingerprint


async def test_live_kscreen_baseline_is_parseable(live_kde):
    observer = KScreenObserver(LinuxProcessRunner(), _Clock(), _Scheduler())
    baseline = await observer.snapshot()
    assert isinstance(baseline, Ok), baseline
    assert baseline.value.outputs, "the live compositor reported no outputs"
    outputs = await observer.outputs()
    assert isinstance(outputs, Ok), outputs


async def test_live_owned_output_appears_and_disappears_exactly(live_kde):
    if os.environ.get(LIVE_KDE_CREATE_ENV) != "1":
        pytest.skip(
            f"creating a live virtual output is opt-in; set {LIVE_KDE_CREATE_ENV}=1 "
            "to allow this test to create and remove exactly one owned test output",
        )
    size = _requested_size()
    runner = LinuxProcessRunner()
    clock = _Clock()
    observer = KScreenObserver(runner, clock, _Scheduler(), poll_interval=0.25)
    helper = VirtualHelper(runner, clock)
    owner = uuid.uuid4()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        pytest.skip("XDG_RUNTIME_DIR is unavailable, so no private helper directory exists")

    baseline = await observer.outputs()
    assert isinstance(baseline, Ok), baseline
    before = {(item.output_id, item.connector) for item in baseline.value}
    identity = None
    try:
        launched = await helper.launch(owner, runtime, size)
        assert isinstance(launched, Ok), (
            "the live helper could not be launched with exact ownership: "
            f"{launched.error.message}"
        )
        identity = launched.value
        assert runner.owned(identity, owner, runtime) is True

        appeared = await observer.wait_for(
            lambda outputs: len([
                item for item in outputs
                if (item.output_id, item.connector) not in before
                and item.size == size
                and item.virtual is True
            ]) == 1,
            clock.now() + 10.0,
        )
        assert isinstance(appeared, Ok), "the live helper output never appeared once"
        new = [
            item for item in appeared.value
            if (item.output_id, item.connector) not in before
        ]
        assert len(new) == 1
        assert new[0].size == size
    finally:
        if identity is not None:
            stopped = await helper.stop(identity, owner, runtime, 10.0)
            assert isinstance(stopped, Ok), (
                "the exact owned live helper could not be stopped: "
                f"{stopped.error.message}"
            )
            disappeared = await observer.wait_for(
                lambda outputs: all(
                    (item.output_id, item.connector) in before for item in outputs
                ),
                clock.now() + 10.0,
            )
            assert isinstance(disappeared, Ok), (
                "the live helper output did not disappear after the helper stopped"
            )


async def test_live_provider_without_verifier_refuses_publication(
    live_kde, tmp_path,
):
    """A provider without an explicitly configured verifier must refuse.

    This check launches a real helper as part of the transaction, so it is
    gated by the same explicit create permission as the mutation test above.
    """
    if os.environ.get(LIVE_KDE_CREATE_ENV) != "1":
        pytest.skip(
            f"creating a live virtual output is opt-in; set {LIVE_KDE_CREATE_ENV}=1 "
            "to allow this test to attempt one owned test output",
        )
    runner = LinuxProcessRunner()
    clock = _Clock()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    ownership = JsonOwnershipStore(AtomicFiles(tmp_path / "state"))
    lock = CreationLock(runtime)
    observer = KScreenObserver(runner, clock, _Scheduler())
    helper = VirtualHelper(runner, clock)

    async def released(record, context):
        return Ok(value=None)

    remover = DisplayRemover(
        helper, observer, ownership, lock, clock, process=runner,
        transport_released=released,
    )
    recovery = DisplayRecovery(helper, observer, ownership, lock, clock, remover)
    creator = DisplayCreator(
        helper, observer, ownership, lock,
        runtime_directory=str(runtime), app_instance_uuid=uuid.uuid4(),
        clock=clock, scheduler=_Scheduler(), id_factory=_Ids(),
    )
    provider = KdeVirtualDisplayProvider(creator, recovery, remover, observer, helper)
    context = OperationContext(
        operation_id=OperationId(uuid.uuid4()),
        session_generation=1,
        endpoint_generation=1,
    )
    result = await provider.create(
        ReceiverId("airplay-environment-probe"), "endpoint-live", _requested_size(), context,
    )
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_operation
    assert await ownership.all() == Ok(value=())
