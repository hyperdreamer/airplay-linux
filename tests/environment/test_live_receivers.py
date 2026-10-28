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
  ``AIRPLAY_LINUX_LIVE_VIRTUAL=1`` permission.  Live execution is gated on both a
  helper/output association authority and a remembered credential committed in
  the same runtime's backend credential adapter.  The association gate skips
  first, preserving the runtime's precise reason verbatim (currently
  ``virtual_display: helper/output association verifier not supplied``).  A
  configured receiver must also be announced by discovery, and its committed
  reference must resolve through ``CredentialStore.load_credential_ref``
  (application metadata never carries one).  Each invocation composes a fresh
  runtime whose credential adapter starts empty; this helper never pairs in it.
  Pairing in another process does not satisfy that lookup, so lifting the
  association gate alone still leaves live execution blocked on same-runtime
  remembered trust.  These conditional bodies are implemented and falsifiable;
  they do not establish live acceptance while either prerequisite is absent.
  The accepted-mode scenario requests ``AIRPLAY_LINUX_LIVE_RESOLUTION`` (default
  ``1920x1080``) and delivers a genuinely different accepted mode, because equal
  sizes are a correct no-op with no mismatch.  Both scenarios start the runtime
  with ``runtime.start()``, the production path that runs startup recovery (which
  is what admits any command) before the scenario dispatches one.

Manual commands::

    AIRPLAY_LINUX_LIVE_HARDWARE=1 AIRPLAY_LINUX_LIVE_CONFIG=/path/live.json \\
        python -m pytest -q -m live_hardware tests/environment/test_live_receivers.py

    # Conditional reconnect/recreation checks (creates one output if eligible).
    # Association authority and same-runtime remembered trust are both required;
    # the current fresh-runtime helper does not establish that trust.
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
from typing import Any
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
# Both scenarios are conditional opt-in live checks: they require both an output
# association authority and a credential committed in the same runtime.  The
# fresh-runtime helper never pairs, so lifting the association gate alone still
# leaves the remembered-trust prerequisite unsatisfied.  Missing prerequisites
# skip with their exact reason.  The body uses the production provider, the
# production transport and the runtime's own production session manager; only the
# reconnect / accepted-mode *stimulus* is synthetic, delivered to the same
# ``SessionManager.ingest`` callback the transport subscription calls, because a
# test cannot make a physical receiver drop or renegotiate on cue.
# ---------------------------------------------------------------------------

_SMALL = Size(width=1280, height=720)
_STANDARD = Size(width=1920, height=1080)


def _accepted_mode(requested: Size) -> Size:
    """A size the receiver accepts that genuinely differs from the request.

    The scenario proves that an accepted-mode mismatch stops the transport and
    retains the output, so the accepted mode has to differ from the requested
    decision: an identical size is a correct no-op that leaves ``last_error``
    unset and would make the body assert against a ``None`` diagnostic.
    """
    return _STANDARD if requested == _SMALL else _SMALL


@dataclass(frozen=True, kw_only=True)
class _LiveSession:
    """One live scenario over the runtime that owns its single manager.

    The scenario never composes a manager of its own: ``runtime.manager`` is the
    object ``runtime.shutdown_coordinator`` and ``runtime.persistence`` are bound
    to, so acquisition, stimulus and teardown share exactly one authority.
    """

    runtime: Any
    receiver_id: Any
    view: Any

    @property
    def manager(self):
        return self.runtime.manager

    def deliver(self, kind: TransportEventKind, *, payload=None) -> None:
        """Deliver one typed transport event to the runtime's own manager.

        Production delivers ``TransportEvent`` through ``TransportPort.subscribe``,
        which is wired to ``SessionManager.ingest``. A physical receiver cannot be
        asked to drop a stream or renegotiate a mode on cue, so the scenario calls
        that same resource-event ingress with the same typed event; only the event
        *source* is synthetic.
        """
        self.manager.ingest(
            _stream_event(self.manager, self.receiver_id, kind, payload=payload)
        )


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


async def _remembered_reference(runtime, receiver_id):
    """Resolve committed trust through the credential adapter that owns it.

    Receiver metadata deliberately decodes with ``credential_ref=None``
    (``storage/codecs.py``: the committed reference is adapter-owned state and is
    never persisted into application metadata). The composition root constructs
    the credential adapter and hands it to the transport, whose pairing
    coordinator owns it, and the runtime exposes no other credential port; ask
    that adapter through its documented ``CredentialStore.load_credential_ref``
    lookup instead of requiring a field storage refuses to write.  The adapter
    starts empty in a fresh runtime and only same-adapter ``commit_pairing``
    writes a remembered reference; pairing in another process cannot supply it.
    """
    credentials = getattr(
        getattr(runtime.deps.transport, "_pairing", None), "_credentials", None
    )
    assert credentials is not None and hasattr(credentials, "load_credential_ref"), (
        "the composed transport exposes no credential adapter; the live scenario "
        "cannot prove remembered trust"
    )
    return await credentials.load_credential_ref(receiver_id)


async def _discovery_views(runtime) -> tuple:
    """Every receiver view discovery announces from the runtime start on.

    ``runtime.start()`` is the production start path: it runs startup recovery,
    which is what admits commands at all (a start issued before recovery finishes
    is refused as an ownership mismatch), and it starts discovery. The
    subscription is installed first so announcements published during start are
    not missed.
    """
    announced: dict = {}

    def remember(event) -> None:
        # A removal carries the last-known offline view, so the newest wins.
        announced[event.receiver.record_set.receiver_id] = event.receiver

    subscription = runtime.deps.discovery.subscribe(remember)
    try:
        started = await runtime.start()
        assert isinstance(started, Ok), started.error.message
        refreshed = await runtime.deps.discovery.refresh()
        assert isinstance(refreshed, Ok), refreshed.error.message
        await _until(lambda: any(view.online for view in announced.values()), 5.0)
    except BaseException:
        await runtime.shutdown()
        raise
    finally:
        subscription.close()
    return tuple(announced.values())


async def _live_virtual_session(tmp_path):
    """Compose the live scenario or skip with the exact missing prerequisite.

    The association gate is checked first; same-runtime remembered trust is an
    independent prerequisite.  This helper creates a fresh credential adapter and
    never pairs in it, so clearing the association gate alone is insufficient.
    Returns a :class:`_LiveSession`; the caller owns ``runtime.shutdown()``.
    """
    config = _require_live()
    if os.environ.get(LIVE_VIRTUAL_ENV) != "1":
        pytest.skip(
            f"creating a live virtual output is opt-in; set {LIVE_VIRTUAL_ENV}=1 "
            "to allow these reconnect/recreation scenarios to own exactly one "
            "test output"
        )
    from airplay_linux.runtime import RuntimeConfig, build_runtime

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
            f"{gate}; these scenarios also require a remembered credential "
            "committed in this same runtime; the fresh-runtime helper never pairs"
        )

    # Only a receiver that discovery currently announces at a configured address
    # and that this runtime's credential adapter already remembers is a subject.
    # This fresh runtime has no pairing setup; external pairing cannot supply the
    # same-runtime reference, even if the association gate above is cleared.
    addresses = {receiver.address for receiver in config.receivers}
    views = [
        view for view in await _discovery_views(runtime)
        if view.online and view.record_set.canonical_address in addresses
    ]
    if not views:
        await runtime.shutdown()
        pytest.skip(
            f"no configured receiver is currently announced by discovery at "
            f"{sorted(addresses)}; start the receiver and re-run"
        )
    for view in views:
        remembered = await _remembered_reference(runtime, view.record_set.receiver_id)
        if isinstance(remembered, Err):
            await runtime.shutdown()
            pytest.skip(
                "the backend credential adapter cannot resolve same-runtime "
                f"remembered trust: {remembered.error.code.value}; association "
                "authority and same-runtime remembered trust are both required; "
                "the fresh-runtime helper never pairs"
            )
        if remembered.value is not None:
            return _LiveSession(
                runtime=runtime,
                receiver_id=view.record_set.receiver_id,
                view=view,
            )
    await runtime.shutdown()
    pytest.skip(
        "the backend credential adapter remembers no credential in this runtime "
        f"for the discoverable receiver(s) at {sorted(addresses)}; association "
        "authority and same-runtime remembered trust are both required; the "
        "fresh-runtime helper never pairs, and pairing in another process "
        "cannot satisfy this prerequisite"
    )


@pytest.mark.live_hardware
async def test_live_reconnect_retains_the_owned_output_until_a_retry_succeeds(
    tmp_path,
):
    """A real reconnect must not recreate the virtual output.

    The session reaches an owned output and a streaming transport; a real
    ``network_lost`` event then pauses it.  The retry reconnects the *same*
    display handle: one creation, no second output, no teardown.
    """
    session = await _live_virtual_session(tmp_path)
    manager, receiver_id, view = session.manager, session.receiver_id, session.view
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

        session.deliver(TransportEventKind.network_lost)
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
        await session.runtime.shutdown()


@pytest.mark.live_hardware
async def test_live_accepted_mode_change_recreates_only_on_explicit_acceptance(
    tmp_path,
):
    """An accepted-mode mismatch stops transport, keeps the output, and recreates
    only when the accepted resolution is explicitly applied."""
    session = await _live_virtual_session(tmp_path)
    manager, receiver_id, view = session.manager, session.receiver_id, session.view
    size = _requested_resolution()
    accepted = _accepted_mode(size)
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

        session.deliver(
            TransportEventKind.accepted_mode,
            payload=ModePayload(size=accepted, reliable=True),
        )
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
        await session.runtime.shutdown()
