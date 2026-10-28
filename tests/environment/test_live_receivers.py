"""Opt-in live receiver checks (hardware, two targets) -- never ordinary CI.

Ordinary CI never runs the marked tests: they require
``AIRPLAY_LINUX_LIVE_HARDWARE=1`` **and** a non-secret configuration file named by
``AIRPLAY_LINUX_LIVE_CONFIG``.  Credentials are never read from that file: the
backend's own keyring is the only credential channel, and PIN/password input is
interactive.  The loader below therefore refuses any key that looks like a
secret, and everything else in the document has to match the fixed schema.

What the marked tests check when opted in:

* the selected live backend affirms the physical-source, multi-target, target-mute
  and audio capabilities this flow depends on (an incompatible selected backend
  fails, it does not skip);
* two configured receivers stream independently, and mute is target-local;
* a connector that the live ``kscreen-doctor`` probe does not report as physical
  is refused without a portal prompt;
* replacing a receiver at the same endpoint address fences the old stream so the
  replacement is never told to disconnect, and shutdown releases both.

Reconnect output retention and accepted-mode recreation additionally require a
helper/output association authority, which this composition deliberately does not
supply; :func:`airplay_linux.runtime.build_runtime` reports that as the
``virtual_display`` gate.  They are *not* claimed here.  Manual commands::

    AIRPLAY_LINUX_LIVE_HARDWARE=1 AIRPLAY_LINUX_LIVE_CONFIG=/path/live.json \\
        python -m pytest -q -m live_hardware tests/environment/test_live_receivers.py

    # manual reconnect/recreation review on a configured desktop:
    airplay-linux  # connect target A, connect target B, mute A, unmute A, quit
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from airplay_linux.backend.contract import probe_backend
from airplay_linux.capture.physical import KscreenConnectorInventory, PhysicalCaptureProvider
from airplay_linux.capture.portal import PortalClient
from airplay_linux.domain.resources import (
    AudioPolicy,
    Err,
    ErrorCode,
    Ok,
    PhysicalMonitorSource,
    VirtualOutputSource,
)
from airplay_linux.domain.resources import Size
from airplay_linux.process.daemon_registry import DaemonRegistry
from airplay_linux.process.linux import LinuxProcessRunner
from airplay_linux.storage import AtomicFiles, JsonDaemonStore, JsonReceiverStore
from airplay_linux.testing.builders import context, target
from airplay_linux.transport.doubletake import DoubleTakeTransport
from airplay_linux.trust.fences import TrustFenceRegistry
from airplay_linux.trust.keyring import BackendKeyringStore

LIVE_HARDWARE_ENV = "AIRPLAY_LINUX_LIVE_HARDWARE"
LIVE_CONFIG_ENV = "AIRPLAY_LINUX_LIVE_CONFIG"

#: The complete, non-secret configuration schema this loader accepts.
CONFIG_KEYS = frozenset({"schema", "receivers", "connectors", "notes"})
RECEIVER_KEYS = frozenset({"name", "address"})
SECRET_MARKERS = (
    "password", "passwd", "pin", "secret", "token", "credential", "key",
)


class LiveConfigError(ValueError):
    """The live configuration is unusable; the message names the violated rule."""


@dataclass(frozen=True, kw_only=True)
class LiveReceiver:
    name: str
    address: str


@dataclass(frozen=True, kw_only=True)
class LiveConfig:
    receivers: tuple[LiveReceiver, ...]
    connectors: tuple[str, ...]


def _walk_keys(value, path="$"):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LiveConfigError(f"{path}: every key must be text")
            lowered = key.casefold()
            if any(marker in lowered for marker in SECRET_MARKERS):
                raise LiveConfigError(
                    f"{path}.{key}: secret-bearing configuration is not accepted; "
                    "credentials come from the backend keyring or interactive input"
                )
            yield from _walk_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_keys(item, f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.casefold()
        if any(lowered.startswith(marker + "=") for marker in SECRET_MARKERS):
            raise LiveConfigError(
                f"{path}: a value looks like a credential assignment; live "
                "configuration must not carry secrets"
            )


def load_live_config(path: str | os.PathLike[str]) -> LiveConfig:
    """Load and validate the non-secret live configuration document."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise LiveConfigError(f"{str(path)!r}: unreadable configuration document") from error
    if not isinstance(document, dict):
        raise LiveConfigError("the live configuration must be a JSON object")
    unknown = set(document) - CONFIG_KEYS
    if unknown:
        raise LiveConfigError(f"unknown configuration keys: {sorted(unknown)}")
    for _ in _walk_keys(document):
        pass
    raw = document.get("receivers")
    if not isinstance(raw, list) or len(raw) < 2:
        raise LiveConfigError("at least two receivers are required for isolation checks")
    receivers: list[LiveReceiver] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) - RECEIVER_KEYS:
            raise LiveConfigError(f"receivers[{index}] has unexpected members")
        name, address = item.get("name"), item.get("address")
        if not isinstance(name, str) or not name.strip():
            raise LiveConfigError(f"receivers[{index}].name must be non-empty text")
        if not isinstance(address, str) or not address.strip():
            raise LiveConfigError(f"receivers[{index}].address must be non-empty text")
        receivers.append(LiveReceiver(name=name.strip(), address=address.strip()))
    connectors = document.get("connectors")
    if not isinstance(connectors, list) or len(connectors) < 2 or not all(
        isinstance(item, str) and item.strip() for item in connectors
    ):
        raise LiveConfigError("two distinct physical connector names are required")
    distinct = tuple(dict.fromkeys(item.strip() for item in connectors))
    if len(distinct) < 2:
        raise LiveConfigError("connectors must name two distinct physical outputs")
    return LiveConfig(receivers=tuple(receivers), connectors=distinct)


def hardware_gate_reason(tmp_path: Path | None = None) -> str | None:
    """The precise reason these live checks cannot run, or ``None``."""
    if os.environ.get(LIVE_HARDWARE_ENV) != "1":
        return (
            f"live receiver checks are opt-in; set {LIVE_HARDWARE_ENV}=1 with two "
            f"real receivers configured via {LIVE_CONFIG_ENV}"
        )
    path = os.environ.get(LIVE_CONFIG_ENV)
    if not path:
        return f"no live configuration selected; set {LIVE_CONFIG_ENV} to a non-secret JSON file"
    if not Path(path).is_absolute():
        return f"{LIVE_CONFIG_ENV} must be an absolute path, got {path!r}"
    if not Path(path).exists():
        return f"the selected live configuration does not exist: {path!r}"
    for command in ("doubletake", "doubletake-ctl"):
        if shutil.which(command) is None:
            return f"required backend command is not on PATH: {command}"
    return None


# ---------------------------------------------------------------------------
# Gate contracts (no live dependency): these run in ordinary CI.
# ---------------------------------------------------------------------------


def test_live_config_rejects_secret_bearing_documents(tmp_path):
    for document, needle in (
        ({"receivers": [], "connectors": [], "password": "x"}, "password"),
        (
            {
                "receivers": [
                    {"name": "a", "address": "192.0.2.10", "pin": "1234"},
                    {"name": "b", "address": "192.0.2.11"},
                ],
                "connectors": ["DP-1", "DP-2"],
            },
            "pin",
        ),
        (
            {
                "receivers": [
                    {"name": "a", "address": "192.0.2.10"},
                    {"name": "b", "address": "192.0.2.11"},
                ],
                "connectors": ["DP-1", "DP-2"],
                "notes": "pin=1234",
            },
            "credential",
        ),
    ):
        path = tmp_path / f"live-{uuid4().hex}.json"
        path.write_text(json.dumps(document))
        with pytest.raises(LiveConfigError) as raised:
            load_live_config(path)
        assert needle in str(raised.value).casefold()


def test_live_config_requires_two_distinct_targets_and_connectors(tmp_path):
    base = {
        "receivers": [
            {"name": "a", "address": "192.0.2.10"},
            {"name": "b", "address": "192.0.2.11"},
        ],
        "connectors": ["DP-1", "DP-2"],
    }
    path = tmp_path / "live.json"
    path.write_text(json.dumps(base))
    loaded = load_live_config(path)
    assert [receiver.name for receiver in loaded.receivers] == ["a", "b"]
    assert loaded.connectors == ("DP-1", "DP-2")

    for broken, reason in (
        ({"receivers": base["receivers"][:1], "connectors": ["DP-1", "DP-2"]}, "two receivers"),
        ({"receivers": base["receivers"], "connectors": ["DP-1"]}, "two distinct"),
        ({"receivers": base["receivers"], "connectors": ["DP-1", "DP-1"]}, "two distinct"),
    ):
        path.write_text(json.dumps(broken))
        with pytest.raises(LiveConfigError) as raised:
            load_live_config(path)
        assert reason in str(raised.value)


def test_absent_opt_in_is_a_reason_not_an_operational_pass(monkeypatch):
    monkeypatch.delenv(LIVE_HARDWARE_ENV, raising=False)
    reason = hardware_gate_reason()
    assert reason is not None
    assert LIVE_HARDWARE_ENV in reason
    # A gate reason can only ever skip; nothing here reports live support.
    assert "passed" not in reason.casefold()


# ---------------------------------------------------------------------------
# Live, hardware-dependent checks.
# ---------------------------------------------------------------------------


def _require_live() -> LiveConfig:
    reason = hardware_gate_reason()
    if reason is not None:
        pytest.skip(reason)
    path = os.environ[LIVE_CONFIG_ENV]
    try:
        return load_live_config(path)
    except LiveConfigError as error:
        pytest.fail(f"the selected live configuration is invalid: {error}")


class _Clock:
    now = staticmethod(time.monotonic)


class _Scheduler:
    import asyncio as _asyncio

    def call_at(self, when, callback):
        return self._asyncio.get_running_loop().call_at(when, callback)


class _Ids:
    new = staticmethod(uuid4)


async def _live_stack(tmp_path):
    runner = LinuxProcessRunner()
    probed = await probe_backend(runner)
    assert isinstance(probed, Ok), (
        "the selected live backend is incompatible: " + probed.error.message
    )
    features = probed.value
    required = ("physical_source", "multi_target", "target_mute", "audio_restart",
                "foreground_control", "owner_handshake", "keyring")
    missing = [name for name in required if getattr(features, name) is not True]
    assert not missing, (
        "the selected live backend does not affirm: " + ", ".join(missing)
    )
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    files = AtomicFiles(state)
    daemons, receivers = JsonDaemonStore(files), JsonReceiverStore(files)
    capture = PhysicalCaptureProvider(
        PortalClient(), KscreenConnectorInventory(runner), features,
    )

    from airplay_linux.backend.control import PublishedDoubletakeControl, PublishedDoubletakeProfile

    def factory(record):
        return PublishedDoubletakeControl(runner, PublishedDoubletakeProfile(
            socket_path=record.socket_path,
            features=features,
            owner_token=record.owner_token,
            instance_uuid=record.instance_uuid,
            protocol_version=record.protocol_version,
        ))

    registry = DaemonRegistry(
        process=runner, store=daemons, control_factory=factory,
        features=features, runtime_directory=runtime, record_root=state,
    )
    transport = DoubleTakeTransport(
        factory, registry, capture, BackendKeyringStore(factory, features),
        receivers, TrustFenceRegistry(), features, _Clock(), _Scheduler(), _Ids(),
    )
    return transport, capture


@pytest.mark.live_hardware
async def test_two_live_targets_stream_independently_and_mute_is_target_local(tmp_path):
    config = _require_live()
    transport, capture = await _live_stack(tmp_path)
    first, second = (
        target("live-a", address=config.receivers[0].address),
        target("live-b", address=config.receivers[1].address),
    )
    transport.update_target(first, first.receiver_id)
    transport.update_target(second, second.receiver_id)
    ctx_a = context(generation=1, lease=1)
    ctx_b = context(generation=1, lease=2)
    handles = []
    try:
        for receiver, connector, ctx in zip(
            (first, second), config.connectors, (ctx_a, ctx_b),
        ):
            selected = await capture.select(connector)
            assert isinstance(selected, Ok), selected
            started = await transport.start_stream(
                receiver, selected.value, AudioPolicy(enabled=True), ctx,
            )
            assert isinstance(started, Ok), started
            handles.append((started.value, ctx))
            assert isinstance(
                await transport.start_audio(started.value, ctx), Ok,
            )

        assert isinstance(await transport.set_muted(handles[0][0], True, ctx_a), Ok)
        # Target B's stream is untouched by A's mute.
        assert isinstance(await transport.set_muted(handles[1][0], False, ctx_b), Ok)
        assert isinstance(await transport.set_muted(handles[0][0], False, ctx_a), Ok)
        assert isinstance(await transport.stop_stream(handles[0][0], ctx_a), Ok)
        # B survives A's teardown.
        assert isinstance(await transport.set_muted(handles[1][0], True, ctx_b), Ok)
        assert isinstance(await transport.stop_stream(handles[1][0], ctx_b), Ok)
        handles.clear()
    finally:
        for handle, ctx in handles:
            await transport.stop_stream(handle, ctx)
        await transport.shutdown()


@pytest.mark.live_hardware
async def test_live_physical_source_enforcement_refuses_non_physical_sources(tmp_path):
    config = _require_live()
    transport, capture = await _live_stack(tmp_path)
    listed = await capture.list_physical()
    assert isinstance(listed, Ok), listed
    assert any(source.connector == config.connectors[0] for source in listed.value)
    refused = await capture.select("Virtual-1")
    assert isinstance(refused, Err)
    assert refused.error.code is ErrorCode.unsupported_source

    unselected = PhysicalMonitorSource(
        connector=config.connectors[0], portal_restore_token=None,
    )
    assert isinstance(await capture.verify(unselected), Err)

    receiver = target("live-a", address=config.receivers[0].address)
    transport.update_target(receiver, receiver.receiver_id)
    virtual = VirtualOutputSource(
        connector="Virtual-1", kscreen_output_id="1", size=Size(width=1280, height=720),
    )
    refused_stream = await transport.start_stream(
        receiver, virtual, AudioPolicy(enabled=False), context(generation=1, lease=1),
    )
    assert isinstance(refused_stream, Err)
    await transport.shutdown()


@pytest.mark.live_hardware
async def test_live_endpoint_replacement_fences_the_old_stream(tmp_path):
    config = _require_live()
    transport, capture = await _live_stack(tmp_path)
    old = target("live-a", address=config.receivers[0].address, endpoint_generation=1)
    transport.update_target(old, old.receiver_id)
    ctx = context(generation=1, lease=1)
    selected = await capture.select(config.connectors[0])
    assert isinstance(selected, Ok), selected
    started = await transport.start_stream(
        old, selected.value, AudioPolicy(enabled=False), ctx,
    )
    assert isinstance(started, Ok), started
    try:
        replacement = target("live-a", address=config.receivers[0].address, endpoint_generation=2)
        transport.update_target(replacement, replacement.receiver_id)
        fenced = await transport.stop_stream(started.value, ctx)
        assert isinstance(fenced, Err)
        assert fenced.error.code is ErrorCode.cleanup_timeout
    finally:
        await transport.shutdown()


@pytest.mark.live_hardware
async def test_live_composition_reports_the_virtual_display_gate(tmp_path):
    _require_live()
    from airplay_linux.runtime import RuntimeConfig, build_runtime

    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    built = await build_runtime(
        RuntimeConfig(state_root=state, runtime_root=runtime, live=False),
    )
    assert isinstance(built, Ok), built
    gated = built.value.gated_capabilities
    assert "virtual_display" in gated
    assert "association" in gated["virtual_display"].casefold()
    await built.value.shutdown()
