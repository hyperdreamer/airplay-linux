from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID

import pytest

from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import ErrorCode, Ok
from airplay_linux.storage import AtomicFiles
from airplay_linux.storage import repositories, codecs

from test_reservation_codecs import (
    APP_ID, LAUNCH_ID, RESERVATION_KEYS, PublicationFault, assert_bad_leaf,
    assert_refused, assert_store_leaf_refused, malformed_documents, persistence_refusal, receipt_states, reservation,
    reservation_bad_leaves, wire,
)


@pytest.fixture
def receipt_generation_37():
    return receipt_states()["reserved"]


@pytest.fixture
def receipt_state_documents():
    return receipt_states()


def store_at(root, filesystem=None):
    assert hasattr(repositories, "JsonLaunchReceiptStore"), "missing receipt store"
    return repositories.JsonLaunchReceiptStore(AtomicFiles(root, filesystem=filesystem))


def receipt_key(receipt):
    return f"daemon/receipts/{receipt.launch_uuid}.json"


async def test_receipt_generation_roundtrip_and_missing_member_refused(tmp_path, receipt_generation_37):
    item = receipt_generation_37
    raw = codecs.encode_launch_receipt(item)
    doc = json.loads(raw)
    assert doc["daemon_instance_uuid"] is None
    assert doc["endpoint_generation"] == 37
    assert set(doc) == RESERVATION_KEYS | {
        "schema", "version", "state", "daemon_instance_uuid", "supervisor", "daemon",
        "entry_device", "entry_inode", "socket_inode", "peer_pid", "peer_start_ticks",
        "peer_uid", "status_state", "credential_service", "failure", "updated_at",
    }
    assert codecs.decode_launch_receipt(raw).endpoint_generation == 37
    for member in ("endpoint_generation", "daemon_instance_uuid"):
        assert_refused(codecs.decode_launch_receipt, wire({k: v for k, v in doc.items() if k != member}))
    assert isinstance(await store_at(tmp_path).write(item), Ok)
    reopened = await store_at(tmp_path).all()
    assert reopened == Ok(value=(item,))
    assert max(r.endpoint_generation for r in reopened.value) == 37
    assert [r for r in reopened.value if r.state not in {"stopped", "failed"}] == [item]
    assert {name: getattr(reopened.value[0], name) for name in RESERVATION_KEYS} == {
        name: getattr(reservation(), name) for name in RESERVATION_KEYS
    }
    store = store_at(tmp_path)
    terminal = replace(item, state="failed", failure=ErrorCode.daemon_failed)
    assert isinstance(await store.write(terminal), Ok)
    next_receipt = replace(item, launch_uuid=UUID(int=99), endpoint_generation=38)
    assert isinstance(await store.write(next_receipt), Ok)
    history = (await store_at(tmp_path).all()).value
    assert len(history) == 2 and terminal in history and next_receipt in history
    maximum = max(receipt.endpoint_generation for receipt in history)
    assert maximum == 38
    assert [receipt for receipt in history if receipt.state not in {"stopped", "failed"}] == [next_receipt]
    assert isinstance(await store.write(replace(next_receipt, state="stopped")), Ok)
    history = (await store_at(tmp_path).all()).value
    assert len(history) == 2
    assert not [receipt for receipt in history if receipt.state not in {"stopped", "failed"}]
    assert max(receipt.endpoint_generation for receipt in history) == 38


async def test_receipt_codec_rejection_matrix_by_state(tmp_path, receipt_state_documents):
    for state, item in receipt_state_documents.items():
        raw = codecs.encode_launch_receipt(item)
        files = AtomicFiles(tmp_path / state)
        store = repositories.JsonLaunchReceiptStore(files)
        async def bad_leaf(name, value):
            await assert_store_leaf_refused(item, name, value, codecs.encode_launch_receipt,
                                           codecs.decode_launch_receipt, files, store, receipt_key(item))
        assert codecs.decode_launch_receipt(raw) == item
        for label, payload in malformed_documents(raw):
            assert_refused(codecs.decode_launch_receipt, payload)
            files = AtomicFiles(tmp_path / state)
            files.write(receipt_key(item), payload)
            persistence_refusal(await repositories.JsonLaunchReceiptStore(files).all())
            assert files.read(receipt_key(item)) == payload
        for name, value in reservation_bad_leaves():
            await bad_leaf(name, value)
        for name in ("entry_device", "entry_inode", "socket_inode", "peer_pid", "peer_start_ticks", "peer_uid"):
            for value in (True, "1", [], -1, 2**63):
                await bad_leaf(name, value)
        for name in ("entry_inode", "socket_inode", "peer_pid"):
            await bad_leaf(name, 0)
        for name in ("supervisor", "daemon"):
            for value in ([], "invalid", ProcessIdentity(pid=True, start_ticks=1),
                          ProcessIdentity(pid=2**31, start_ticks=1), ProcessIdentity(pid=1, start_ticks=-1)):
                await bad_leaf(name, value)
        for name, values in {
            "state": (None, True, [], "unknown"),
            "daemon_instance_uuid": ([], True, "invalid", APP_ID, LAUNCH_ID),
            "status_state": ([], True, "unknown"),
            "credential_service": (None, [], True, "unknown-service"),
            "failure": ([], True, "not-an-error"),
            "updated_at": (None, [], True, float("nan"), float("inf")),
        }.items():
            for value in values:
                await bad_leaf(name, value)
        files.write(receipt_key(item), raw)
        assert await store.all() == Ok(value=(item,))
        assert isinstance(await store.write(item), Ok)
        if state in {"failed", "stopped"}:
            persistence_refusal(await store.write(replace(item, updated_at=5.0)))
            assert files.read(receipt_key(item)) == raw
    item = receipt_state_documents["ready"]
    files = AtomicFiles(tmp_path / "filename")
    files.write(f"daemon/receipts/{UUID(int=99)}.json", codecs.encode_launch_receipt(item))
    persistence_refusal(await repositories.JsonLaunchReceiptStore(files).all())


async def test_all_partial_states_and_unknown_versions_refuse(tmp_path, receipt_state_documents):
    reserved = receipt_state_documents["reserved"]
    raw = codecs.encode_launch_receipt(reserved)
    document = json.loads(raw)
    assert_refused(
        codecs.decode_launch_receipt,
        wire({name: value for name, value in document.items() if name != "endpoint_generation"}),
    )
    assert_refused(
        codecs.decode_launch_receipt,
        wire(document | {"schema": "airplay-linux/launch-receipt/future"}),
    )
    assert_refused(codecs.decode_launch_receipt, wire(document | {"version": 2}))

    transition_store = store_at(tmp_path / "reserved-to-stopping")
    assert isinstance(await transition_store.write(reserved), Ok)
    assert isinstance(await transition_store.write(receipt_state_documents["stopping"]), Ok)

    ready = receipt_state_documents["ready"]
    identity = ("daemon_instance_uuid", "supervisor", "daemon")
    endpoint = ("entry_device", "entry_inode", "socket_inode", "peer_pid", "peer_start_ticks", "peer_uid")
    for state, earlier_stage in (("reserved", "detached"), ("reserved", "bound"), ("reserved", "ready"),
                                 ("detached", "reserved"), ("detached", "bound"), ("detached", "ready"),
                                 ("bound", "reserved"), ("bound", "detached")):
        earlier = receipt_state_documents[earlier_stage]
        bad = replace(earlier, state=state)
        with pytest.raises(codecs.RecordError):
            codecs.encode_launch_receipt(bad)
        assert_refused(codecs.decode_launch_receipt, wire(json.loads(codecs.encode_launch_receipt(earlier)) | {"state": state}))
    for state in ("reserved", "detached"):
        item = receipt_state_documents[state]
        forbidden = (*endpoint, "status_state", "credential_service")
        if state == "reserved":
            forbidden += identity
        for field in forbidden:
            assert_bad_leaf(item, field, getattr(ready, field), codecs.encode_launch_receipt, codecs.decode_launch_receipt)
        bad = replace(item, socket_inode=93, peer_pid=31003, peer_start_ticks=81003, peer_uid=1000)
        with pytest.raises(codecs.RecordError):
            codecs.encode_launch_receipt(bad)
        assert_refused(codecs.decode_launch_receipt, wire(json.loads(codecs.encode_launch_receipt(item)) | {
            name: getattr(bad, name) for name in endpoint if name not in {"entry_device", "entry_inode"}
        }))
    for state in ("detached", "bound", "ready", "stopping", "stopped", "failed"):
        item = receipt_state_documents[state]
        for field in identity:
            assert_bad_leaf(item, field, None, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
    for state in ("bound", "ready", "stopping", "stopped", "failed"):
        item = receipt_state_documents[state]
        for field in endpoint:
            assert_bad_leaf(item, field, None, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
        for field, value in (("peer_pid", 31004), ("peer_start_ticks", 99), ("peer_uid", 1001)):
            assert_bad_leaf(item, field, value, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
    for field, value in (("status_state", None), ("credential_service", "unknown")):
        assert_bad_leaf(ready, field, value, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
    assert_bad_leaf(ready, "failure", ErrorCode.daemon_failed, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
    failed = receipt_state_documents["failed"]
    assert_bad_leaf(failed, "failure", None, codecs.encode_launch_receipt, codecs.decode_launch_receipt)
    for state in ("stopping", "stopped", "failed"):
        for stage in ("reserved", "detached", "bound"):
            item = replace(receipt_state_documents[stage], state=state,
                           failure=ErrorCode.daemon_failed if state == "failed" else None)
            assert codecs.decode_launch_receipt(codecs.encode_launch_receipt(item)) == item
        without_daemon = replace(receipt_state_documents["reserved"], state=state,
                                status_state="idle", credential_service="keyring_proven",
                                failure=ErrorCode.daemon_failed if state == "failed" else None)
        with pytest.raises(codecs.RecordError):
            codecs.encode_launch_receipt(without_daemon)
    for terminal in ("failed", "stopped"):
        root = tmp_path / f"terminal-{terminal}"
        store = store_at(root)
        assert isinstance(await store.write(reserved), Ok)
        terminal_record = receipt_state_documents[terminal]
        assert isinstance(await store.write(terminal_record), Ok)
        before = AtomicFiles(root).read(receipt_key(terminal_record))
        persistence_refusal(await store.write(replace(terminal_record, updated_at=5.0)))
        assert AtomicFiles(root).read(receipt_key(terminal_record)) == before

    raw = codecs.encode_launch_receipt(reserved)
    assert codecs.decode_launch_receipt(raw) == reserved
    for suffix in (b" ", b"\n", b"\t", b"{}"):
        assert_refused(codecs.decode_launch_receipt, raw + suffix)


async def test_store_enforces_allowed_transitions_and_never_erases_published_evidence(tmp_path):
    states = receipt_states()
    allowed = {
        "reserved": {"detached", "stopping", "stopped", "failed"},
        "detached": {"bound", "stopping", "stopped", "failed"},
        "bound": {"ready", "stopping", "stopped", "failed"},
        "ready": {"stopping", "stopped", "failed"},
        "stopping": {"stopped", "failed"}, "stopped": set(), "failed": set(),
    }
    chain = ("reserved", "detached", "bound", "ready", "stopping")
    for source, destinations in allowed.items():
        for destination in states:
            root = tmp_path / source / destination
            store = store_at(root)
            for phase in chain[:chain.index(source) + 1] if source in chain else ("reserved", source):
                assert isinstance(await store.write(states[phase]), Ok)
            before = AtomicFiles(root).read(receipt_key(states[source]))
            result = await store.write(states[destination])
            if destination == source or destination in destinations:
                assert isinstance(result, Ok), (source, destination)
            else:
                persistence_refusal(result)
                assert AtomicFiles(root).read(receipt_key(states[source])) == before
    for source in ("detached", "bound", "ready", "stopping"):
        root = tmp_path / "erasure" / source
        store = store_at(root)
        for phase in chain[:chain.index(source) + 1]:
            assert isinstance(await store.write(states[phase]), Ok)
        before = AtomicFiles(root).read(receipt_key(states[source]))
        terminal_state = "stopped" if source == "stopping" else "failed"
        proposed = replace(states["reserved"], state=terminal_state,
                           failure=ErrorCode.daemon_failed if terminal_state == "failed" else None)
        persistence_refusal(await store.write(proposed))
        assert AtomicFiles(root).read(receipt_key(states[source])) == before
        for name, value in (("daemon_instance_uuid", APP_ID), ("daemon", ProcessIdentity(pid=32000, start_ticks=99)),
                            ("supervisor", ProcessIdentity(pid=32001, start_ticks=99)),
                            ("entry_inode", 123), ("status_state", "streaming")):
            if getattr(states[source], name) is None:
                continue
            persistence_refusal(await store.write(replace(states[source], state="failed", failure=ErrorCode.daemon_failed,
                                                          **{name: value})))
        terminal = replace(states[source], state="failed", failure=ErrorCode.daemon_failed, updated_at=2.0)
        assert isinstance(await store.write(terminal), Ok)
        assert await store_at(root).all() == Ok(value=(terminal,))
    # Retention must also hold when only endpoint/status fields are cleared, while
    # the complete daemon identity group remains intact and codec-valid.
    for source, destination in (("ready", "failed"), ("stopping", "stopped")):
        root = tmp_path / "endpoint-loss" / source
        store = store_at(root)
        for phase in chain[:chain.index(source) + 1]:
            assert isinstance(await store.write(states[phase]), Ok)
        original = AtomicFiles(root).read(receipt_key(states[source]))
        loss = replace(states["detached"], state=destination,
                       failure=ErrorCode.daemon_failed if destination == "failed" else None)
        assert codecs.decode_launch_receipt(codecs.encode_launch_receipt(loss)) == loss
        persistence_refusal(await store.write(loss))
        assert AtomicFiles(root).read(receipt_key(states[source])) == original


async def test_store_rejects_immutable_drift_and_invalid_files_before_writing(tmp_path):
    item = receipt_states()["reserved"]
    store = store_at(tmp_path)
    assert isinstance(await store.write(item), Ok)
    files = AtomicFiles(tmp_path)
    original = files.read(receipt_key(item))
    persistence_refusal(await store.write(replace(item, endpoint_generation=38)))
    assert files.read(receipt_key(item)) == original
    files.write("daemon/receipts/not-a-uuid.json", codecs.encode_launch_receipt(item))
    persistence_refusal(await store.all())


@pytest.mark.parametrize("reopen", (False, True))
@pytest.mark.parametrize("terminal", ("stopped", "failed"))
async def test_terminal_identical_repeat_confirms_durability_after_uncertain_publication(tmp_path, reopen, terminal):
    states = receipt_states()
    fault = PublicationFault(step=None)
    store = store_at(tmp_path, fault)
    assert isinstance(await store.write(states["reserved"]), Ok)
    fault.step = "directory_fsync"
    persistence_refusal(await store.write(states[terminal]))
    assert AtomicFiles(tmp_path).read(receipt_key(states[terminal])) == codecs.encode_launch_receipt(states[terminal])
    confirmations = fault.confirmations
    if reopen:
        store = store_at(tmp_path, fault)
    persistence_refusal(await store.write(states[terminal]))
    assert fault.confirmations > confirmations
    fault.step = None
    assert isinstance(await store.write(states[terminal]), Ok)
    assert await store_at(tmp_path).all() == Ok(value=(states[terminal],))


@pytest.mark.parametrize("step", ("write", "fsync", "rename", "directory_fsync"))
async def test_receipt_store_atomic_step_failures_are_typed(tmp_path, step):
    fault = PublicationFault(step=step)
    store = store_at(tmp_path, fault)
    persistence_refusal(await store.write(receipt_states()["reserved"]))
    fault.step = None
    assert isinstance(await store.write(receipt_states()["reserved"]), Ok)
    fault.step = step
    persistence_refusal(await store.write(receipt_states()["detached"]))


async def test_failed_cleanup_intent_roundtrip_and_one_completion_preserve_receipt(tmp_path):
    from test_reservation_codecs import cleanup_documents
    states = receipt_states()
    store = store_at(tmp_path)
    assert isinstance(await store.write(states["reserved"]), Ok)
    assert isinstance(await store.write(states["failed"]), Ok)
    files = AtomicFiles(tmp_path)
    before = files.read(receipt_key(states["failed"]))

    initial, completed, timed_out = cleanup_documents()
    initial_raw = codecs.encode_daemon_cleanup_intent(initial)
    initial_document = json.loads(initial_raw)
    assert set(initial_document) == {
        "schema", "version", "launch_uuid", "reason", "intent_at", "completed_at", "outcome",
    }
    assert initial_document["completed_at"] is None
    assert initial_document["outcome"] is None
    assert codecs.decode_daemon_cleanup_intent(initial_raw) == initial
    for _, payload in malformed_documents(initial_raw):
        assert_refused(codecs.decode_daemon_cleanup_intent, payload)

    cleanup_key = f"daemon/cleanup-intents/{initial.launch_uuid}.json"
    intents = repositories.JsonDaemonCleanupIntentStore(files)
    assert isinstance(await intents.write(initial), Ok)
    assert files.read(cleanup_key) == initial_raw
    assert isinstance(await intents.write(completed), Ok)
    completed_raw = codecs.encode_daemon_cleanup_intent(completed)
    assert files.read(cleanup_key) == completed_raw
    for changed in (timed_out, replace(completed, completed_at=None, outcome=None)):
        persistence_refusal(await intents.write(changed))
        assert files.read(cleanup_key) == completed_raw
    assert isinstance(await repositories.JsonDaemonCleanupIntentStore(files).write(completed), Ok)
    assert await repositories.JsonDaemonCleanupIntentStore(files).all() == Ok(value=(completed,))

    persistence_refusal(await store.write(replace(states["failed"], updated_at=5.0)))
    assert files.read(receipt_key(states["failed"])) == before
    persistence_refusal(await store.write(replace(states["failed"], state="stopping", failure=None)))
    assert files.read(receipt_key(states["failed"])) == before

    stopped_root = tmp_path / "stopped-receipt"
    stopped_store = store_at(stopped_root)
    stopped_files = AtomicFiles(stopped_root)
    assert isinstance(await stopped_store.write(states["reserved"]), Ok)
    assert isinstance(await stopped_store.write(states["stopped"]), Ok)
    stopped_before = stopped_files.read(receipt_key(states["stopped"]))
    persistence_refusal(await stopped_store.write(replace(states["stopped"], updated_at=5.0)))
    assert stopped_files.read(receipt_key(states["stopped"])) == stopped_before
    assert await store_at(tmp_path).all() == Ok(value=(states["failed"],))


@pytest.fixture
def public_storage_graph(tmp_path, monkeypatch):
    """All process attempts reach the injected port; live boundary fallbacks are fatal."""
    import socket
    from pathlib import Path
    from types import SimpleNamespace
    import dbus_next.aio
    from airplay_linux import runtime as runtime_module
    from airplay_linux.backend.contract import probe_backend
    from airplay_linux.diagnostics import diagnostic
    from airplay_linux.domain.ports import CommandOutput, CommandSpec
    from airplay_linux.domain.resources import Err
    from airplay_linux.policy.capabilities import EnvironmentProbe
    from airplay_linux.process.linux import LinuxProcessRunner

    flags = "bitrate code cred-backend creds daemonize debug direct-key fps hwaccel no-audio no-cursor no-encrypt pair port port-range socket target target-latency-ms test video-codec x11-window-id x11-window-name capabilities".split()
    sender = "Usage of doubletake:\n" + "".join(f"  -{name} string\n    \tvalue\n" for name in flags)
    ctl = "Usage: doubletake-ctl command\nCommands:\n" + "\n".join((
        "  status  Status", "  discover  Discover", "  devices  Devices",
        "  connect [target] [PIN-or-password]  Connect only the given IP",
        "  pin <PIN-or-password>  PIN", "  disconnect [target]  Disconnect only the given IP",
        "  mute [target]  Mute only the given IP", "  unmute [target]  Unmute only the given IP",
    ))
    class Recorder:
        def __init__(self):
            self.observations = []
            self.launches = []
            self.unshare = []
            self.signals = []
        async def run(self, spec, timeout, stdin=None):
            argv = spec.argv
            command = Path(argv[0]).name
            if command == "unshare":
                self.unshare.append(argv)
            if command == "doubletake" and "-daemonize" in argv:
                self.launches.append(argv)
            table = {
                ("doubletake", "--help"): (0, sender.encode()),
                ("doubletake-ctl", "--help"): (0, ctl.encode()),
                ("doubletake", "-capabilities"): (0, b"capabilities-v1\nstatus-source\nstatus-source-complete\n"),
                ("doubletake", "--version"): (2, b""),
                ("kscreen-doctor", "-j"): (0, b'{"outputs":[]}'),
            }
            self.observations.append(argv)
            if argv in table:
                code, stdout = table[argv]
                return Ok(value=CommandOutput(exit_code=code, stdout=stdout, stderr=b""))
            return Err(error=diagnostic(ErrorCode.dependency_incompatible))
        async def spawn(self, spec):
            if Path(spec.argv[0]).name == "unshare":
                self.unshare.append(spec.argv)
            self.launches.append(spec.argv)
            return Err(error=diagnostic(ErrorCode.dependency_incompatible))
        def identity(self, pid):
            return None
        def owned(self, *args):
            return False
        async def terminate(self, *args, **kwargs):
            self.signals.append(args)
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        def subscribe(self, callback):
            return SimpleNamespace(close=lambda: None)
    recorder = Recorder()

    def blocked(*args, **kwargs):
        raise AssertionError("unexpected live boundary")
    async def blocked_process(*args, **kwargs):
        blocked()
    monkeypatch.setattr(LinuxProcessRunner, "run", blocked_process)
    monkeypatch.setattr(LinuxProcessRunner, "spawn", blocked_process)
    monkeypatch.setattr(dbus_next.aio, "MessageBus", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    # Exercise the offline tripwires before entering real composition.
    with pytest.raises(AssertionError, match="unexpected live boundary"):
        dbus_next.aio.MessageBus()
    sock = socket.socket(socket.AF_UNIX)
    try:
        with pytest.raises(AssertionError, match="unexpected live boundary"):
            sock.connect("/unused-offline-test.sock")
    finally:
        sock.close()

    async def affirmative(*args):
        return True
    async def audio():
        return Ok(value=None)
    def environment(cls, *, process, helper, capture=None, **kwargs):
        return cls(environment={"XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE", "KDE_SESSION_VERSION": "6"},
                   command_finder=lambda name: "/offline/" + name, module_finder=lambda name: True,
                   backend_probe=lambda: probe_backend(process), helper_probe=helper.probe, capture=capture,
                   audio_probe=SimpleNamespace(check=audio), keyring_probe=affirmative,
                   dbus=SimpleNamespace(interfaces=blocked))
    monkeypatch.setattr(EnvironmentProbe, "from_environment", classmethod(environment))
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    return SimpleNamespace(recorder=recorder, module=runtime_module,
                           config=runtime_module.RuntimeConfig(state_root=tmp_path / "state", runtime_root=root, live=False),
                           blocked_process=blocked_process)


async def test_malformed_receipt_cannot_reach_launch_writer(tmp_path, monkeypatch, public_storage_graph):
    from airplay_linux.backend.contract import probe_backend
    from airplay_linux.domain.ports import CommandSpec
    from airplay_linux.domain.resources import Err
    from airplay_linux.process.launcher import ProductionLaunchCapability
    from airplay_linux.process.linux import LinuxProcessRunner
    from airplay_linux.storage import PosixFilesystem
    graph = public_storage_graph
    process = graph.recorder
    with pytest.raises(AssertionError, match="unexpected live boundary"):
        await LinuxProcessRunner().run(CommandSpec(argv=("unshare", "-rn"), env={}), 1.0)
    compatible = await probe_backend(process)
    assert isinstance(compatible, Ok)
    assert compatible.value.profile == "doubletake-0.5+source"
    assert compatible.value.source_reporting
    assert ProductionLaunchCapability(compatible.value, graph.config.runtime_root, production_graph=True).available()
    built = await graph.module.build_runtime(graph.config, overrides={"process": process})
    assert isinstance(built, Ok)
    runtime = built.value
    assert runtime.registry._process is process
    files = AtomicFiles(graph.config.state_root)
    store = repositories.JsonLaunchReceiptStore(files)
    unlinks = []
    original_unlink = PosixFilesystem.unlink
    def observe_unlink(self, parent, name):
        unlinks.append(name)
        return original_unlink(self, parent, name)
    monkeypatch.setattr(PosixFilesystem, "unlink", observe_unlink)
    try:
        for item in receipt_states().values():
            raw = codecs.encode_launch_receipt(item)
            for label, payload in malformed_documents(raw):
                files.write(receipt_key(item), payload)
                persistence_refusal(await store.all())
                result = await runtime.registry.ensure()
                assert isinstance(result, Err)
                assert result.error.code is ErrorCode.dependency_incompatible
                assert files.read(receipt_key(item)) == payload
        assert await runtime.shutdown() == Ok(value=None)
    finally:
        assert process.launches == []
        assert process.unshare == []
        assert process.signals == []
        assert unlinks == []
    assert ("doubletake", "--help") in process.observations
    assert ("doubletake", "-capabilities") in process.observations
