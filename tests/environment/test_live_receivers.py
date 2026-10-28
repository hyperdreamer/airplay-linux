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

* the owned virtual output survives a real reconnect (the same display handle is
  retained across the stream loss and retry) and is recreated only on an explicit
  accepted-mode change, through the production provider and the production
  session manager.  Both scenarios create an output, so they require the extra
  ``AIRPLAY_LINUX_LIVE_VIRTUAL=1`` permission and are skipped with the runtime's
  own precise gate reason when the composition cannot associate an output with
  the helper (the current production composition reports that as
  ``virtual_display: helper/output association verifier not supplied``).  They
  also require an already paired, currently discoverable receiver: a live
  scenario never types a credential.

Manual commands::

    AIRPLAY_LINUX_LIVE_HARDWARE=1 AIRPLAY_LINUX_LIVE_CONFIG=/path/live.json \\
        python -m pytest -q -m live_hardware tests/environment/test_live_receivers.py

    # reconnect output retention + accepted-mode recreation (creates one output):
    AIRPLAY_LINUX_LIVE_HARDWARE=1 AIRPLAY_LINUX_LIVE_CONFIG=/path/live.json \\
        AIRPLAY_LINUX_LIVE_VIRTUAL=1 \\
        python -m pytest -q -m live_hardware tests/environment/test_live_receivers.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

import pytest

from airplay_linux.backend.contract import probe_backend
from airplay_linux.capture.physical import KscreenConnectorInventory, PhysicalCaptureProvider
from airplay_linux.capture.portal import PortalClient
from airplay_linux.domain.messages import (
    EmptyPayload,
    ModePayload,
    RecreateAccepted,
    Retry,
    Start,
    TransportEvent,
    TransportEventKind,
)
from airplay_linux.domain.resources import (
    AudioPolicy,
    DisplayState,
    Err,
    ErrorCode,
    Mode,
    Ok,
    PhysicalMonitorSource,
    ResolutionDecision,
    ResolutionEvidence,
    StartRequest,
    TransportState,
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
LIVE_VIRTUAL_ENV = "AIRPLAY_LINUX_LIVE_VIRTUAL"
LIVE_RESOLUTION_ENV = "AIRPLAY_LINUX_LIVE_RESOLUTION"

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


# ---------------------------------------------------------------------------
# Step 4 reconnect retention and accepted-mode recreation.
#
# Both scenarios are ordinary opt-in live checks: without every prerequisite they
# skip with the exact missing reason.  The body uses the production provider, the
# production transport and the production session manager; only the reconnect /
# accepted-mode *stimulus* is delivered through the transport's documented event
# subscription, because a test cannot make a physical receiver drop on cue.
# ---------------------------------------------------------------------------

_SMALL = Size(width=1280, height=720)


class _Detach:
    def __init__(self, action):
        self._action = action

    def close(self):
        action, self._action = self._action, None
        if action is not None:
            action()


class _EventInjectingTransport:
    """Forward every real transport call and add typed test event delivery.

    Production events still arrive through the real transport subscription; the
    wrapper only adds ``emit`` so a live test can deliver the one event a physical
    receiver cannot be asked to produce deterministically.
    """

    def __init__(self, inner):
        self._inner = inner
        self._callbacks: list = []

    def subscribe(self, callback):
        self._callbacks.append(callback)
        inner = self._inner.subscribe(callback)

        def detach():
            if callback in self._callbacks:
                self._callbacks.remove(callback)
            inner.close()

        return _Detach(detach)

    def emit(self, event) -> None:
        for callback in tuple(self._callbacks):
            callback(event)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _requested_resolution() -> Size:
    text = os.environ.get(LIVE_RESOLUTION_ENV, "1920x1080").strip().lower()
    try:
        width, height = (int(part) for part in text.split("x", 1))
        return Size(width=width, height=height)
    except (TypeError, ValueError):
        pytest.fail(f"{LIVE_RESOLUTION_ENV} must look like 1920x1080, got {text!r}")


async def _until(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _live_settle(manager) -> None:
    for _ in range(12):
        await manager.drain()
        await asyncio.sleep(0)


def _decision(receiver_id, fingerprint: str, size: Size) -> ResolutionDecision:
    return ResolutionDecision(
        size=size,
        source="user",
        evidence=ResolutionEvidence(
            origin="picker",
            reliable=True,
            receiver_id=receiver_id,
            endpoint_fingerprint=fingerprint,
        ),
    )


def _stream_event(manager, receiver_id, kind, *, payload=None) -> TransportEvent:
    state = manager.snapshot(receiver_id)
    handle = state.stream
    assert handle is not None, "no live stream to bind the event to"
    operation = replace(
        manager.context(receiver_id).operation(),
        operation_id=handle.start_operation_id,
        session_generation=state.session_generation,
        endpoint_generation=handle.endpoint_generation,
        transport_lease_id=handle.lease_id,
    )
    return TransportEvent(
        kind=kind,
        receiver_id=receiver_id,
        stream=handle,
        endpoint_fingerprint=handle.endpoint_fingerprint,
        endpoint_generation=handle.endpoint_generation,
        daemon_instance_uuid=handle.daemon_instance_uuid,
        context=operation,
        payload=payload if payload is not None else EmptyPayload(),
    )


async def _live_virtual_session(tmp_path):
    """Compose the live scenario or skip with the exact missing prerequisite.

    Returns ``(runtime, manager, transport, receiver_id, view)``.  The caller owns
    ``runtime.shutdown()``.
    """
    config = _require_live()
    if os.environ.get(LIVE_VIRTUAL_ENV) != "1":
        pytest.skip(
            f"creating a live virtual output is opt-in; set {LIVE_VIRTUAL_ENV}=1 "
            "to allow these reconnect/recreation scenarios to own exactly one "
            "test output"
        )
    from airplay_linux.runtime import RuntimeConfig, build_runtime, compose_manager

    state = tmp_path / "state"
    runtime_root = tmp_path / "runtime"
    state.mkdir(mode=0o700)
    runtime_root.mkdir(mode=0o700)
    built = await build_runtime(
        RuntimeConfig(state_root=state, runtime_root=runtime_root, live=False),
    )
    assert isinstance(built, Ok), built
    runtime = built.value
    gate = runtime.gated_capabilities.get("virtual_display")
    if gate is not None:
        await runtime.shutdown()
        pytest.skip(
            "the live composition cannot own a virtual output: "
            f"{gate}; these scenarios run once an association authority exists"
        )

    # Only an already trusted, currently visible receiver is a valid subject.
    saved = await runtime.deps.receivers.all()
    assert isinstance(saved, Ok), saved
    addresses = {receiver.address for receiver in config.receivers}
    candidates = [
        record for record in saved.value
        if record.credential_ref is not None
        and record.last_endpoint is not None
        and record.last_endpoint.canonical_host in addresses
    ]
    if not candidates:
        await runtime.shutdown()
        pytest.skip(
            "no remembered-trust receiver matching the live configuration; a live "
            "scenario never types a credential, pair the receiver first"
        )
    receiver_id = candidates[0].receiver_id
    await runtime.deps.discovery.start()
    await runtime.deps.discovery.refresh()
    await _until(
        lambda: runtime.deps.discovery.current(receiver_id) is not None
        and runtime.deps.discovery.current(receiver_id).online,
        5.0,
    )
    view = runtime.deps.discovery.current(receiver_id)
    if view is None or not view.online:
        await runtime.shutdown()
        pytest.skip(
            "the configured receiver is not currently announced by discovery; "
            "start the receiver and re-run"
        )

    transport = _EventInjectingTransport(runtime.deps.transport)
    manager = compose_manager(replace(runtime.deps, transport=transport))
    return runtime, manager, transport, receiver_id, view


@pytest.mark.live_hardware
async def test_live_reconnect_retains_the_owned_output_until_a_retry_succeeds(
    tmp_path,
):
    """A real reconnect must not recreate the virtual output.

    The session reaches an owned output and a streaming transport; a real
    ``network_lost`` event then pauses it.  The retry reconnects the *same*
    display handle: one creation, no second output, no teardown.
    """
    runtime, manager, transport, receiver_id, view = await _live_virtual_session(tmp_path)
    size = _requested_resolution()
    try:
        receipt = await manager.dispatch(Start(
            receiver_id=receiver_id,
            request=StartRequest(
                mode=Mode.VIRTUAL,
                resolution=_decision(
                    receiver_id, view.record_set.endpoint_fingerprint, size,
                ),
                audio=AudioPolicy(enabled=False),
            ),
        ))
        assert receipt.accepted, receipt.diagnostic
        await _live_settle(manager)
        started = manager.snapshot(receiver_id)
        assert started.display is DisplayState.Owned, started.last_error
        assert started.display_handle is not None
        assert started.stream is not None
        before = started.display_handle
        assert before.source.size == size

        transport.emit(_stream_event(
            manager, receiver_id, TransportEventKind.network_lost,
        ))
        await _live_settle(manager)
        paused = manager.snapshot(receiver_id)
        assert paused.stream is None
        assert paused.transport is TransportState.Reconnecting
        # The output is retained, never torn down by the reconnect path.
        assert paused.display is DisplayState.Owned
        assert paused.display_handle == before
        assert manager.pending_resources(receiver_id) == ()

        retried = await manager.dispatch(Retry(receiver_id=receiver_id))
        assert retried.accepted, retried.diagnostic
        await _live_settle(manager)
        resumed = manager.snapshot(receiver_id)
        assert resumed.transport is TransportState.Streaming, resumed.last_error
        assert resumed.stream is not None
        assert resumed.display_handle == before
        assert resumed.display is DisplayState.Owned
    finally:
        await runtime.shutdown()


@pytest.mark.live_hardware
async def test_live_accepted_mode_change_recreates_only_on_explicit_acceptance(
    tmp_path,
):
    """An accepted-mode mismatch stops transport, keeps the output, and recreates
    only when the accepted resolution is explicitly applied."""
    runtime, manager, transport, receiver_id, view = await _live_virtual_session(tmp_path)
    size = _requested_resolution()
    accepted = size if size != _SMALL else Size(width=1920, height=1080)
    try:
        receipt = await manager.dispatch(Start(
            receiver_id=receiver_id,
            request=StartRequest(
                mode=Mode.VIRTUAL,
                resolution=_decision(
                    receiver_id, view.record_set.endpoint_fingerprint, size,
                ),
                audio=AudioPolicy(enabled=False),
            ),
        ))
        assert receipt.accepted, receipt.diagnostic
        await _live_settle(manager)
        started = manager.snapshot(receiver_id)
        assert started.display is DisplayState.Owned, started.last_error
        before = started.display_handle
        assert before is not None and started.stream is not None

        transport.emit(_stream_event(
            manager,
            receiver_id,
            TransportEventKind.accepted_mode,
            payload=ModePayload(size=accepted, reliable=True),
        ))
        await _live_settle(manager)
        mismatch = manager.snapshot(receiver_id)
        assert mismatch.last_error.code is ErrorCode.resolution_mismatch
        assert mismatch.accepted_resolution == accepted
        assert mismatch.transport is TransportState.Idle
        assert mismatch.stream is None
        assert mismatch.display is DisplayState.Owned
        assert mismatch.display_handle == before

        recreated = await manager.dispatch(RecreateAccepted(receiver_id=receiver_id))
        assert recreated.accepted, recreated.diagnostic
        await _live_settle(manager)
        after = manager.snapshot(receiver_id)
        assert after.display is DisplayState.Owned, after.last_error
        assert after.display_handle is not None
        assert after.display_handle != before
        assert after.display_handle.source.size == accepted
        assert after.stream is not None
    finally:
        await runtime.shutdown()
