"""Offline probes exercise the production witness, filesystem and composition."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from airplay_linux import runtime as composition
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.domain.records import ProbeVerdict
from airplay_linux.domain.resources import CapabilityReport, Err, ErrorCode, Ok
from airplay_linux.policy.capabilities import EnvironmentProbe
from airplay_linux.process import ownership
from airplay_linux.process.procfs import IdentityEvidence, IdentityState
from airplay_linux.storage.atomic import AtomicFiles, state_root
from airplay_linux.storage.codecs import RecordError, decode_probe_verdict, encode_probe_verdict
from airplay_linux.testing.adapters import ProbeProcessRecorder

FIXTURES = Path(__file__).parents[1] / "fixtures" / "backend"


def probe_module():
    return importlib.import_module("airplay_linux.process.credential_probe")


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        return self.value

    async def sleep(self, delay):
        assert delay > 0
        self.sleeps.append(delay)
        self.value += delay
        await asyncio.sleep(0)


class NamespaceFacts(ownership.EndpointObservations):
    """Only low level kernel facts are fake; receipt selection/proofs are real."""

    def __init__(self, clock):
        self.clock = clock
        self.process = None
        self.specs = {}
        self.entries = {}
        self.rows = {}
        self.peers = {}
        self.checked = []
        self.bad_peer = False
        self.bad_rows = False
        self.unreadable = False
        self.bound_after = 0.0
        self.boot = str(UUID(int=333))
        self.supervisor = ProcessIdentity(pid=3000, start_ticks=30)

    def identity(self, pid):
        self.checked.append(("identity", pid))
        if pid == self.supervisor.pid:
            return IdentityEvidence(pid=pid, state=IdentityState.present,
                                    identity=self.supervisor, parent=2999, uid=os.getuid())
        identity = self.process.identity(pid)
        if self.unreadable:
            return IdentityEvidence(pid=pid, state=IdentityState.unreadable)
        if identity is None:
            return IdentityEvidence(pid=pid, state=IdentityState.absent)
        return IdentityEvidence(pid=pid, state=IdentityState.present, identity=identity,
                                parent=self.supervisor.pid, uid=os.getuid())

    def stat(self, name, *, dir_fd, follow_symlinks):
        directory = os.readlink(f"/proc/self/fd/{dir_fd}")
        path = str(Path(directory) / name)
        if self.clock.now() < self.bound_after or path not in self.entries:
            raise FileNotFoundError
        return self.entries[path]

    def peer(self, descriptor, name):
        path = str(Path(os.readlink(f"/proc/self/fd/{descriptor}")) / name)
        self.checked.append(("peer", path))
        peer = self.peers[path]
        if self.process.identity(peer.pid) is None:
            raise ConnectionRefusedError(111, "offline")
        return (peer.pid + int(self.bad_peer), os.getuid(), os.getgid())

    def unix_inodes(self, path):
        self.checked.append(("row", path))
        rows = self.rows.get(path, ())
        return rows + rows if self.bad_rows else rows

    def holds_socket(self, pid, inode):
        self.checked.append(("fd", pid))
        return any(peer.pid == pid and inode in self.rows.get(path, ())
                   for path, peer in self.peers.items())

    def cmdline(self, pid):
        self.checked.append(("argv", pid))
        return b"\0".join(os.fsencode(a) for a in self.specs[pid].argv) + b"\0"

    def boot_id(self):
        return self.boot

    def unlink(self, name, *, dir_fd):
        path = str(Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / name)
        self.entries.pop(path)


class InnerProcess(ProbeProcessRecorder):
    def __init__(self, facts, clock):
        super().__init__()
        self.facts, self.clock = facts, clock
        facts.process = self
        self.launches = self.inner_launches
        self.idle_after = 0.0
        self.status_cost = 0.0
        self.confirmation_fails = False
        self.negative_hangs = False
        self.negative_code = 1
        self.negative_socket = False
        self.negative_file = False
        self.on_post_open = None
        self.terminate_error = None
        self.termination_deadlines = []
        self.before_signal = []
        self.record_root = None

    async def spawn(self, spec):
        result = await super().spawn(spec)
        assert isinstance(result, Ok)
        identity = result.value
        self.facts.specs[identity.pid] = spec
        path = spec.argv[spec.argv.index("-socket") + 1]
        leg = Path(path).parent.name
        observation = self.inner_launches[-1]
        assert observation.leg == leg
        observation.socket_exists = False
        observation.credentials_file_exists = False
        if leg == "positive" or self.negative_socket:
            self.facts.entries[path] = SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid(), st_dev=1, st_ino=1234,
            )
            self.facts.rows[path] = (5678,)
            self.facts.peers[path] = identity
        if leg == "negative" and not self.negative_hangs:
            if self.negative_file:
                (Path(spec.env["HOME"]) / "credentials.json").write_text("temporary fake data")
            def finish():
                observation.exit_code = self.negative_code
                observation.socket_exists = path in self.facts.entries
                observation.credentials_file_exists = self.negative_file
                self.exit(identity, self.negative_code)
                self.observe_probe_absences(identity, socket_exists=observation.socket_exists,
                                            credentials_file_exists=observation.credentials_file_exists)
                self.facts.rows[path] = ()
            asyncio.get_running_loop().call_soon(finish)
        return result

    async def run(self, spec, timeout, stdin=None):
        self.observe_invocation("run", spec)
        self.runs.append((spec, timeout))
        assert "-daemonize" not in spec.argv, "backend legs must use spawn"
        assert spec.argv[-1] == "status", spec.argv
        self.clock.value += self.status_cost
        state = "idle" if self.clock.now() >= self.idle_after else "discovering"
        if self.confirmation_fails and self.clock.now() >= 2:
            state = "discovering"
        return Ok(value=CommandOutput(exit_code=0, stdout=json.dumps({
            "ok": True, "state": state, "streams": [], "has_audio": False, "audio_muted": False,
        }).encode(), stderr=b""))

    async def terminate(self, identity, owner_uuid, runtime_directory, deadline, *,
                        strict=False, evidence=None, revalidate=None):
        assert strict is True and revalidate is not None
        self.termination_deadlines.append(deadline)
        # This boundary models pidfd-open: the callback must read the receipt
        # again after this hook, before FakeProcess records any signal.
        if self.on_post_open is not None:
            self.on_post_open()
        self.before_signal.append(((self.record_root or Path(runtime_directory).parent) / "receipts" /
                                   f"{owner_uuid}.json").read_bytes())
        if self.terminate_error is not None:
            return Err(error=composition.diagnostic(self.terminate_error))
        result = await super().terminate(
            identity, owner_uuid, runtime_directory, deadline, strict=strict,
            evidence=evidence, revalidate=revalidate,
        )
        if isinstance(result, Ok):
            for path, peer in self.facts.peers.items():
                if peer == identity:
                    self.facts.rows[path] = ()
            for observation in self.launches:
                if observation.identity == identity:
                    observation.exit_code = -15
                    observation.socket_exists = False
        return result


@pytest.fixture
def private_socket_root():
    """Own a short socket base independently of pytest roots and TMPDIR."""
    with TemporaryDirectory(prefix="ap-tests-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def namespace_probe_witness(private_socket_root, monkeypatch):
    p = probe_module()
    clock = Clock()
    facts = NamespaceFacts(clock)
    inner = InnerProcess(facts, clock)
    service_calls = []

    async def service():
        service_calls.append("introspect")
        return True

    monkeypatch.setattr(ownership, "_observations", facts)
    monkeypatch.setattr(p, "_witness_environment", lambda: p.WitnessEnvironment(
        observations=facts, service=service, now=clock.now, sleep=clock.sleep,
    ))
    # The child appends a UUID and leg name; pytest roots can exceed sun_path.
    root = private_socket_root / str(uuid4())
    root.mkdir(mode=0o700)
    config = p.prepare_config(root, backend_executable="/fake/doubletake",
                              supervisor=facts.supervisor, boot_id=facts.boot)
    return SimpleNamespace(module=p, config=config, facts=facts, inner=inner, clock=clock,
                           service_calls=service_calls, socket_root=private_socket_root,
                           namespace=lambda argv: Ok(value=facts.supervisor))


@pytest.fixture
def probe_verdict_documents():
    return ProbeVerdict(schema="airplay-linux/credential-service-proof", version=1,
                        service_leg="passed", backend_positive_leg="passed",
                        backend_negative_leg="passed", failure=None)


def test_probe_verdict_codec_rejection_matrix(probe_verdict_documents):
    # Import through the production seam also pins the canonical DTO identity.
    p = probe_module()
    assert p.ProbeVerdict is ProbeVerdict
    valid = probe_verdict_documents
    payload = encode_probe_verdict(valid)
    assert decode_probe_verdict(payload) == valid
    document = json.loads(payload)
    malformed = [payload + b" ", payload + b"\n", payload + b"{}", payload[:-1],
                 payload.replace(b'"version":1', b'"version":1,"version":1')]
    changes = {"schema": ["unknown", None], "version": [2, True, "1"],
               "service_leg": [True, "unknown", None],
               "backend_positive_leg": ["failed"], "backend_negative_leg": [0],
               "failure": ["ownership_mismatch", "arbitrary backend output"]}
    for key, values in changes.items():
        for value in values:
            malformed.append(json.dumps({**document, key: value}).encode())
    for key in document:
        malformed.append(json.dumps({k: v for k, v in document.items() if k != key}).encode())
    malformed.append(json.dumps({**document, "extra": 1}).encode())
    for raw in malformed:
        with pytest.raises(RecordError):
            decode_probe_verdict(raw)


def test_probe_evidence_and_teardown_stay_in_child_namespace(namespace_probe_witness):
    c = namespace_probe_witness
    result = c.module.run(c.config, process=c.inner, namespace=c.namespace)
    assert (result.service_leg, result.backend_positive_leg, result.backend_negative_leg) == (
        "passed", "passed", "passed")
    assert c.service_calls == ["introspect"]
    assert len(c.inner.launches) == 2
    positive, negative = c.inner.launches
    assert positive.leg == "positive" and negative.leg == "negative"
    assert positive.launch_uuid != negative.launch_uuid
    assert "DBUS_SESSION_BUS_ADDRESS" not in positive.env
    assert negative.env["DBUS_SESSION_BUS_ADDRESS"] == c.module.UNREACHABLE_BUS
    assert negative.exit_code != 0 and not negative.socket_exists and not negative.credentials_file_exists
    assert c.inner.terminated == [positive.identity]
    assert c.inner.strict_terminations[0].stage == "bound"
    from airplay_linux.storage.codecs import decode_launch_receipt
    assert decode_launch_receipt(c.inner.before_signal[0]).state == "stopping"
    receipts = list((c.config.record_root / "receipts").glob("*.json"))
    assert len(receipts) == 2
    assert all(decode_launch_receipt(path.read_bytes()).state == "stopped" for path in receipts)
    assert all(str(c.config.record_root) in str(value) for kind, value in c.facts.checked if kind in {"peer", "row"})
    assert not (c.config.record_root / "daemon").exists()
    assert not c.facts.entries
    assert (c.config.record_root / "verdict.json").is_file()
    assert c.clock.now() >= 2.0


@pytest.mark.parametrize("leg", ["positive", "negative"])
async def test_each_backend_probe_leg_has_exactly_one_inner_launch(namespace_probe_witness, leg):
    c = namespace_probe_witness
    outcome = await c.module.run_backend_leg(c.config, leg, process=c.inner,
                                              supervisor=c.facts.supervisor)
    assert outcome.passed and outcome.cleanup_confirmed
    assert [item.leg for item in c.inner.launches] == [leg]
    receipts = list((c.config.record_root / "receipts").glob("*.json"))
    assert [path.stem for path in receipts] == [str(c.inner.launches[0].launch_uuid)]
    if leg == "positive":
        assert c.inner.strict_terminations and c.inner.terminated
    else:
        assert not c.inner.terminated
        assert c.inner.launches[0].exit_code == 1


@pytest.mark.parametrize("failure", ["bad_peer", "bad_rows", "unreadable", "receipt_after_open"])
def test_child_uncertainty_retains_receipts_and_never_publishes_success(namespace_probe_witness, failure):
    c = namespace_probe_witness
    if failure == "receipt_after_open":
        def corrupt():
            path = next((c.config.record_root / "receipts").glob("*.json"))
            path.write_bytes(path.read_bytes() + b" ")
        c.inner.on_post_open = corrupt
    else:
        setattr(c.facts, failure, True)
    result = c.module.run(c.config, process=c.inner, namespace=c.namespace)
    assert result.failure is not None
    assert c.inner.terminated == []
    assert len(c.inner.launches) == 1, "negative cannot start while positive cleanup is uncertain"
    assert (c.config.record_root / "receipts").is_dir()
    assert not (c.config.record_root / "verdict.json").exists()


@pytest.fixture
def probe_fake_clock(namespace_probe_witness):
    return namespace_probe_witness


@pytest.mark.parametrize("failure", ["socket", "idle", "confirmation", "termination", "negative_hang",
                                     "negative_zero", "negative_socket", "negative_file"])
def test_probe_socket_idle_confirmation_and_termination_bounds(probe_fake_clock, failure):
    c = probe_fake_clock
    if failure == "socket":
        c.facts.bound_after = 4.0
    elif failure == "idle":
        c.facts.bound_after = 1.7
        c.inner.idle_after = 3.1
        c.inner.status_cost = 0.3
    elif failure == "confirmation":
        c.inner.confirmation_fails = True
    elif failure == "termination":
        c.inner.terminate_error = ErrorCode.cleanup_timeout
    elif failure == "negative_hang":
        c.inner.negative_hangs = True
    elif failure == "negative_zero":
        c.inner.negative_code = 0
    elif failure == "negative_socket":
        c.inner.negative_socket = True
    elif failure == "negative_file":
        c.inner.negative_file = True
    result = c.module.run(c.config, process=c.inner, namespace=c.namespace)
    assert result.failure is not None
    assert result.backend_positive_leg != "passed" or result.backend_negative_leg != "passed"
    assert all(deadline == 3.0 for deadline in c.inner.termination_deadlines)
    if failure in {"socket", "idle"}:
        assert c.clock.now() < 3.7, "socket and idle must share one deadline"
    if failure == "confirmation":
        assert c.clock.now() >= 2.0
    if failure == "termination":
        assert not (c.config.record_root / "verdict.json").exists()


@pytest.fixture
def recording_probe_processes(namespace_probe_witness, monkeypatch):
    """Full build_runtime graph; the outer fake executes real run in a thread."""
    c = namespace_probe_witness
    from dbus_next.aio import MessageBus

    async def forbidden(*args, **kwargs):
        raise AssertionError("offline probe reached a host boundary")

    from airplay_linux.process.linux import LinuxProcessRunner
    monkeypatch.setattr(LinuxProcessRunner, "run", forbidden)
    monkeypatch.setattr(LinuxProcessRunner, "spawn", forbidden)
    monkeypatch.setattr(MessageBus, "connect", forbidden)
    monkeypatch.setattr(ownership.EndpointObservations, "peer", forbidden)

    class Outer(ProbeProcessRecorder):
        async def run(self, spec, timeout, stdin=None):
            self.observe_invocation("run", spec)
            self.runs.append((spec, timeout))
            if Path(spec.argv[0]).name == "unshare":
                c.events.append("witness")
                root = Path(spec.argv[spec.argv.index("--record-root") + 1])
                runtime_root = Path(spec.argv[spec.argv.index("--socket") + 1]).parent.parent
                c.config = c.module.prepare_config(root, backend_executable="/fake/doubletake",
                                                   supervisor=c.facts.supervisor, boot_id=c.facts.boot,
                                                   runtime_root=runtime_root)
                c.inner.record_root = root
                c.probe_runtime_root = runtime_root
                await asyncio.to_thread(c.module.run, c.config, process=c.inner,
                                        namespace=c.namespace)
                if c.after_child is not None:
                    c.after_child()
                return Ok(value=CommandOutput(exit_code=0, stdout=b"diagnostic only", stderr=b""))
            responses = {
                ("doubletake", "--help"): (FIXTURES / "doubletake-0.5-source-help.txt").read_text(),
                ("doubletake-ctl", "--help"): (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text(),
                ("doubletake", "--version"): "doubletake 0.5.0\n",
                ("doubletake", "-capabilities"): "capabilities-v1\nstatus-source\nstatus-source-complete\n",
            }
            assert spec.argv in responses, spec.argv
            return Ok(value=CommandOutput(exit_code=0, stdout=responses[spec.argv].encode(), stderr=b""))

    async def capabilities(_):
        return CapabilityReport(environment="kde-plasma-6-wayland", discovery=True, mirror=False,
                                virtual_display=False, audio=False, remembered_pairing=False,
                                additional_virtual_outputs=False, diagnostics=())

    async def nothing():
        return Ok(value=None)

    monkeypatch.setattr(EnvironmentProbe, "check", capabilities)
    monkeypatch.setattr(composition, "PortalClient", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "AzcMdnsClient", lambda clock: SimpleNamespace(close=nothing))
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", lambda: SimpleNamespace())
    monkeypatch.setattr(composition, "_resolve_unshare", lambda: "/fake/unshare", raising=False)
    original_registry = composition.DaemonRegistry
    original_derive = composition.derive_composition_capabilities

    def registry(*args, **kwargs):
        c.events.append("registry")
        return original_registry(*args, **kwargs)

    def derive(*args, **kwargs):
        c.events.append("derive")
        return original_derive(*args, **kwargs)

    monkeypatch.setattr(composition, "DaemonRegistry", registry)
    monkeypatch.setattr(composition, "derive_composition_capabilities", derive)
    c.events = []
    c.after_child = None
    c.inner_launches = c.inner.launches
    c.unreachable_bus = c.module.UNREACHABLE_BUS
    c.outer = Outer()
    c.outer_unshare = c.outer.outer_unshare
    c.runtime = None
    runtime_root = c.socket_root / "r"
    runtime_root.mkdir(mode=0o700)
    c.runtime_config = composition.RuntimeConfig(state_root=c.socket_root / "s", runtime_root=runtime_root, live=False)

    async def build(*, activate=True):
        monkeypatch.setattr(composition, "_OWNED_DAEMON_EPOCH_ACTIVE", activate)
        result = await composition.build_runtime(c.runtime_config, overrides={"process": c.outer})
        assert isinstance(result, Ok), result
        c.runtime = result.value
        return result.value

    c.build_complete_production_and_probe = build
    return c


@pytest.mark.parametrize("environment,old_socket_bytes", [
    ({"HOME": "/home/abc"}, 107),
    ({"HOME": "/home/user"}, 108),
    ({"HOME": "/home/henry"}, 109),
    ({"HOME": "/home/henry", "XDG_STATE_HOME": "/srv/desktop/users/henry/persistent/application-state"}, 138),
], ids=["short-control-107", "home-user-108", "home-henry-109", "long-xdg-state-home"])
async def test_default_state_layout_reaches_production_probe_process_boundary(
    recording_probe_processes, environment, old_socket_bytes,
):
    """A-1: the real parent/child witness must work at ordinary state-root lengths."""
    c = recording_probe_processes
    suffix = Path("daemon/probe") / str(UUID(int=1)) / "positive/daemon.sock"
    configured = state_root(environment)
    assert len(os.fsencode(configured / suffix)) == old_socket_bytes
    # Preserve the exact default path length without writing anyone's real home.
    padding = len(os.fsencode(configured)) - len(os.fsencode(c.socket_root)) - 1
    assert padding > 0
    root = c.socket_root / ("s" * padding)
    assert len(os.fsencode(root / suffix)) == old_socket_bytes
    c.runtime_config = replace(c.runtime_config, state_root=root)
    runtime = await c.build_complete_production_and_probe()
    try:
        assert len(c.outer_unshare) == 1, "A-1: state layout blocked the production process boundary"
        assert len(c.inner_launches) == 2
        assert runtime.deps.transport._features.keyring is True
        paths = [item.argv[item.argv.index("-socket") + 1]
                 for item in c.inner.invocations if item.kind == "direct_backend"]
        assert all(len(os.fsencode(path)) < 108 for path in paths)
        assert c.probe_runtime_root.name == str(c.config.probe_uuid)
        assert not c.probe_runtime_root.parent.exists()
        assert not c.config.record_root.exists()
        archive = root / "archives/probe" / str(c.config.probe_uuid)
        assert len(list((archive / "receipts").glob("*.json"))) == 2
        assert decode_probe_verdict((archive / "verdict.json").read_bytes()).failure is None
        print(f"A-1 {environment}: old_socket_bytes={old_socket_bytes}, "
              f"runtime_socket_bytes={[len(os.fsencode(path)) for path in paths]}, "
              "outer_calls=1, inner_launches=2, keyring=True")
    finally:
        await runtime.shutdown()


async def test_probe_run_seam_observes_outer_unshare_and_inner_backend_launch(recording_probe_processes):
    c = recording_probe_processes
    runtime = await c.build_complete_production_and_probe()
    assert len(c.outer_unshare) == 1 and len(c.inner_launches) == 2
    assert sum(item.kind == "outer_namespace" for item in c.outer.invocations) == 1
    assert not any(item.kind == "direct_backend" for item in c.outer.invocations)
    assert sum(item.kind == "direct_backend" for item in c.inner.invocations) == 2
    assert not c.outer.spawned
    assert "airplay_linux.process.credential_probe" in c.outer_unshare[0].argv
    positive, negative = c.inner_launches
    assert (positive.leg, negative.leg) == ("positive", "negative")
    assert positive.launch_uuid != negative.launch_uuid
    assert negative.env["DBUS_SESSION_BUS_ADDRESS"] == c.unreachable_bus
    assert negative.exit_code != 0
    assert not negative.socket_exists and not negative.credentials_file_exists
    assert runtime.deps.transport._features.keyring is True
    archive = c.runtime_config.state_root / "archives" / "probe" / str(c.config.probe_uuid)
    assert {path.stem for path in (archive / "receipts").glob("*.json")} == {
        str(positive.launch_uuid), str(negative.launch_uuid)}
    assert not c.config.record_root.exists()
    assert not c.probe_runtime_root.parent.exists()
    assert c.events == ["witness", "derive", "registry"]
    assert runtime.registry._process is c.outer
    await runtime.shutdown()


@pytest.fixture
def bootstrap_event_log(recording_probe_processes):
    return recording_probe_processes


@pytest.mark.parametrize("missing", [None, "owned daemon control", "source verifier", "receipt production",
                                     "endpoint verifier", "strict teardown", "shared authority",
                                     "credential proof producer", "unshare"])
async def test_probe_bootstrap_precedes_registry_and_does_not_require_keyring(bootstrap_event_log, monkeypatch, missing):
    c = bootstrap_event_log
    if missing == "unshare":
        monkeypatch.setattr(composition, "_resolve_unshare", lambda: None)
    elif missing == "owned daemon control":
        monkeypatch.setattr(composition.ProductionLaunchCapability, "available", lambda _: False)
    elif missing:
        original = composition._install_owned_daemon_epoch
        fields = {"source verifier": "source_verifier", "receipt production": "receipts",
                  "endpoint verifier": "endpoint_verifier", "strict teardown": "strict_teardown",
                  "shared authority": "shared_authority", "credential proof producer": "credential_proof"}
        def incomplete(*args, **kwargs):
            return replace(original(*args, **kwargs), **{fields[missing]: None})
        monkeypatch.setattr(composition, "_install_owned_daemon_epoch", incomplete)
    runtime = await c.build_complete_production_and_probe()
    if missing:
        assert c.outer_unshare == [] and c.inner_launches == []
        assert missing in runtime.gated_capabilities["owned_daemon_control"]
        assert runtime.deps.transport._features.keyring is False
    else:
        assert c.events == ["witness", "derive", "registry"]
        assert len(c.outer_unshare) == 1 and len(c.inner_launches) == 2
        assert runtime.deps.transport._features.keyring is True
    await runtime.shutdown()


@pytest.fixture
def probe_verdict_files(namespace_probe_witness):
    c = namespace_probe_witness
    verdict = c.module.run(c.config, process=c.inner, namespace=c.namespace)
    assert verdict.failure is None
    return c


@pytest.mark.parametrize("damage", [None, "root_mode", "root_owner", "root_uuid", "root_symlink", "root_replace",
                                    "missing", "oversized", "short_read", "trailing", "mutated", "symlink", "fifo",
                                    "owner", "nonregular_stat", "digest", "digest_size", "st_size", "schema", "extra", "exit", "bool_exit",
                                    "stdout_only"])
def test_verdict_file_requires_dirfd_digest_exact_size_and_zero_exit(probe_verdict_files, monkeypatch, damage):
    c = probe_verdict_files
    p, root = c.module, c.config.record_root
    expected_uuid = c.config.probe_uuid
    descriptor = p.open_probe_root(root, expected_uuid)
    original_fstat, original_read, original_open = os.fstat, os.read, os.open
    def relative_open(name, flags, *args, **kwargs):
        if name in {"verdict.json", "verdict.sha256"}:
            assert kwargs.get("dir_fd") == descriptor
            assert flags & os.O_NOFOLLOW and flags & os.O_NONBLOCK
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(p.os, "open", relative_open)
    verdict_path, digest_path = root / "verdict.json", root / "verdict.sha256"
    if damage == "root_mode":
        root.chmod(0o755)
    elif damage in {"root_owner", "owner", "st_size", "nonregular_stat"}:
        inode = root.stat().st_ino if damage == "root_owner" else verdict_path.stat().st_ino
        def fstat(fd):
            found = original_fstat(fd)
            if found.st_ino != inode:
                return found
            values = {name: getattr(found, name) for name in (
                "st_uid", "st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")}
            if damage == "nonregular_stat":
                values["st_mode"] = stat.S_IFIFO | 0o600
            else:
                values["st_uid" if damage != "st_size" else "st_size"] += 1
            return SimpleNamespace(**values)
        monkeypatch.setattr(p.os, "fstat", fstat)
    elif damage == "root_uuid":
        expected_uuid = uuid4()
    elif damage in {"root_symlink", "root_replace"}:
        old = root.with_name("original")
        root.rename(old)
        if damage == "root_symlink":
            root.symlink_to(old, target_is_directory=True)
        else:
            root.mkdir(mode=0o700)
    elif damage in {"missing", "stdout_only"}:
        verdict_path.unlink()
    elif damage == "oversized":
        verdict_path.write_bytes(b"x" * 4097)
    elif damage == "short_read":
        payload = verdict_path.read_bytes()
        verdict_path.write_bytes(payload + b"X")
        inode = verdict_path.stat().st_ino
        observed = []
        def short_read(fd, limit):
            if original_fstat(fd).st_ino != inode:
                return original_read(fd, limit)
            if observed:
                return b""
            observed.append(True)
            return original_read(fd, min(limit, len(payload)))
        monkeypatch.setattr(p.os, "read", short_read)
    elif damage == "trailing":
        verdict_path.write_bytes(verdict_path.read_bytes() + b" ")
        digest_path.write_bytes(hashlib.sha256(verdict_path.read_bytes()).hexdigest().encode() + b"\n")
    elif damage == "mutated":
        verdict_path.write_bytes(verdict_path.read_bytes().replace(b"passed", b"failed", 1))
    elif damage in {"symlink", "fifo"}:
        old = root / "other.json"
        verdict_path.rename(old)
        if damage == "symlink":
            verdict_path.symlink_to(old)
        else:
            os.mkfifo(verdict_path)
    elif damage == "digest":
        digest_path.write_bytes(b"0" * 64 + b"\n")
    elif damage == "digest_size":
        digest_path.write_bytes(digest_path.read_bytes().rstrip())
    elif damage in {"schema", "extra"}:
        doc = json.loads(verdict_path.read_bytes())
        doc["schema" if damage == "schema" else "extra"] = "unknown"
        verdict_path.write_bytes(json.dumps(doc).encode())
        digest_path.write_bytes(hashlib.sha256(verdict_path.read_bytes()).hexdigest().encode() + b"\n")
    output = CommandOutput(exit_code=1 if damage == "exit" else False if damage == "bool_exit" else 0,
                           stdout=encode_probe_verdict(ProbeVerdict(
                               schema="airplay-linux/credential-service-proof", version=1,
                               service_leg="passed", backend_positive_leg="passed",
                               backend_negative_leg="passed", failure=None)), stderr=b"")
    try:
        result = p.read_probe_verdict(root, expected_uuid, descriptor, output)
        if damage is None:
            assert isinstance(result, Ok) and result.value.backend_negative_leg == "passed"
        else:
            assert isinstance(result, Err), damage
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("failure", ["receipt", "verdict", "directory_fsync"])
async def test_parent_failed_verdict_and_archive_failure_retain_originals(recording_probe_processes, monkeypatch, failure):
    c = recording_probe_processes
    original = AtomicFiles.write
    def unavailable(self, relative, payload):
        from airplay_linux.storage.atomic import StorageError
        if relative.startswith("archives/probe/"):
            if failure == "directory_fsync":
                original(self, relative, payload)
                raise StorageError("directory_fsync")
            if failure == "verdict" and not relative.endswith("verdict.json"):
                return original(self, relative, payload)
            raise StorageError("file_fsync")
        return original(self, relative, payload)
    monkeypatch.setattr(AtomicFiles, "write", unavailable)
    runtime = await c.build_complete_production_and_probe()
    assert len(c.inner_launches) == 2
    assert c.config.record_root.is_dir()
    assert not c.probe_runtime_root.parent.exists()
    assert (c.config.record_root / "verdict.json").is_file()
    assert len(list((c.config.record_root / "receipts").glob("*.json"))) == 2
    assert runtime.deps.transport._features.keyring is False
    await runtime.shutdown()


@pytest.mark.parametrize("leg", ["service", "positive", "negative", "cleanup"])
async def test_failed_witness_retains_evidence_and_names_owning_leg(recording_probe_processes, monkeypatch, leg):
    c = recording_probe_processes
    if leg == "service":
        original = c.module._witness_environment
        async def unavailable():
            return False
        monkeypatch.setattr(c.module, "_witness_environment",
                            lambda: replace(original(), service=unavailable))
    elif leg == "positive":
        c.inner.confirmation_fails = True
    elif leg == "negative":
        c.inner.negative_file = True
    else:
        c.inner.terminate_error = ErrorCode.cleanup_timeout
    runtime = await c.build_complete_production_and_probe()
    try:
        assert len(c.outer_unshare) == 1
        assert len(c.inner_launches) == {"service": 0, "positive": 2, "negative": 2, "cleanup": 1}[leg]
        assert c.config.record_root.is_dir()
        assert not c.probe_runtime_root.parent.exists()
        assert not runtime.deps.transport._features.keyring
        if leg == "cleanup":
            assert not (c.config.record_root / "verdict.json").exists()
        else:
            verdict = decode_probe_verdict((c.config.record_root / "verdict.json").read_bytes())
            assert verdict.failure is not None
            assert leg in runtime.gated_capabilities["owned_daemon_control"]
        if leg == "negative":
            assert (c.config.record_root / "negative" / "credentials.json").read_text() == "temporary fake data"
        refused = await runtime.registry.ensure()
        assert isinstance(refused, Err) and refused.error.code is ErrorCode.dependency_incompatible
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("damage", ["missing", "partial", "filename", "duplicate_leg", "failed"])
async def test_parent_archives_only_two_verified_terminal_leg_receipts(recording_probe_processes, damage):
    c = recording_probe_processes
    def tamper():
        path = next((c.config.record_root / "receipts").glob("*.json"))
        if damage == "missing":
            path.unlink()
        elif damage == "partial":
            path.write_bytes(path.read_bytes() + b" ")
        elif damage == "filename":
            path.rename(path.with_name(str(uuid4()) + ".json"))
        else:
            doc = json.loads(path.read_bytes())
            if damage == "duplicate_leg":
                other = next(item for item in path.parent.glob("*.json") if item != path)
                doc["runtime_directory"] = json.loads(other.read_bytes())["runtime_directory"]
            else:
                doc["state"], doc["failure"] = "failed", "cleanup_timeout"
            path.write_bytes(json.dumps(doc).encode())
    c.after_child = tamper
    runtime = await c.build_complete_production_and_probe()
    try:
        assert len(c.inner_launches) == 2
        assert not runtime.deps.transport._features.keyring
        assert c.config.record_root.is_dir()
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("separate_runtime", [False, True], ids=["colocated-control", "short-runtime"])
def test_production_main_uses_same_child_seam_and_requires_complete_files(
    namespace_probe_witness, monkeypatch, separate_runtime,
):
    c = namespace_probe_witness
    from airplay_linux.process import linux
    root = c.socket_root / str(uuid4())
    root.mkdir(mode=0o700)
    runtime_root = root
    if separate_runtime:
        runtime_root = c.socket_root / "sockets" / root.name
        runtime_root.mkdir(mode=0o700, parents=True)
    c.inner.record_root = root
    monkeypatch.setattr(c.module, "_namespace_identity", c.namespace)
    monkeypatch.setattr(c.module.shutil, "which", lambda _: "/fake/doubletake")
    monkeypatch.setattr(ownership.EndpointObservations, "boot_id", staticmethod(lambda: c.facts.boot))
    monkeypatch.setattr(linux, "LinuxProcessRunner", lambda: c.inner)
    argv = ["--socket", str(runtime_root / "positive/daemon.sock"), "--record-root", str(root)]
    assert c.module.main(argv) == 0
    assert len(c.inner.launches) == 2
    assert decode_probe_verdict((root / "verdict.json").read_bytes()).failure is None


@pytest.mark.parametrize("damage", ["mode", "replacement"])
async def test_runtime_root_change_refuses_before_archiving_original_evidence(recording_probe_processes, damage):
    c = recording_probe_processes

    def tamper():
        root = c.probe_runtime_root
        if damage == "mode":
            root.chmod(0o755)
        else:
            root.rename(root.with_name("replaced"))
            root.mkdir(mode=0o700)

    c.after_child = tamper
    runtime = await c.build_complete_production_and_probe()
    try:
        assert len(c.inner_launches) == 2
        assert not runtime.deps.transport._features.keyring
        assert c.config.record_root.is_dir()
        assert len(list((c.config.record_root / "receipts").glob("*.json"))) == 2
        assert (c.config.record_root / "verdict.json").is_file()
        assert not c.probe_runtime_root.parent.exists()
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("failure", ["process_refusal", "process_error", "cancelled"])
async def test_short_runtime_cleanup_preserves_durable_evidence_on_dispatch_failure(tmp_path, failure):
    p = probe_module()
    observed = {}

    class Recorder:
        async def run(self, spec, timeout):
            records = Path(spec.argv[spec.argv.index("--record-root") + 1])
            sockets = Path(spec.argv[spec.argv.index("--socket") + 1]).parent.parent
            observed.update(records=records, sockets=sockets)
            assert records.name == sockets.name
            for directory in (records, sockets, sockets.parent):
                assert directory.stat().st_uid == os.getuid()
                assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            AtomicFiles(records).write("receipts/retained.json", b"retained child evidence")
            if failure == "cancelled":
                raise asyncio.CancelledError
            if failure == "process_error":
                raise OSError("recorded process failure")
            return Err(error=composition.diagnostic(ErrorCode.daemon_failed))

    probe = p.CredentialServiceProbe(process=Recorder(), record_root=tmp_path / "state", unshare="unshare")
    if failure == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await probe.check()
    else:
        assert isinstance(await probe.check(), Err)
    assert not observed["sockets"].parent.exists()
    assert (observed["records"] / "receipts/retained.json").read_bytes() == b"retained child evidence"


async def test_caller_cancellation_leaves_witness_running_until_cleanup(tmp_path):
    p = probe_module()
    entered, finish = asyncio.Event(), asyncio.Event()
    observed = []

    class Recorder:
        async def run(self, spec, timeout):
            observed.append(Path(spec.argv[spec.argv.index("--socket") + 1]).parent.parent)
            entered.set()
            await finish.wait()
            return Err(error=composition.diagnostic(ErrorCode.daemon_failed))

    probe = p.CredentialServiceProbe(process=Recorder(), record_root=tmp_path / "state", unshare="unshare")
    caller = asyncio.create_task(probe.check())
    await asyncio.wait_for(entered.wait(), 1)
    try:
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert observed[0].is_dir()
    finally:
        finish.set()
        assert isinstance(await probe.check(), Err)
    assert not observed[0].parent.exists()
    assert len(observed) == 1


@pytest.mark.parametrize("damage", ["mode", "owner", "symlink"])
def test_short_runtime_parent_must_be_owned_private_and_not_symlinked(monkeypatch, damage):
    p = probe_module()
    parents = []
    original_fstat = os.fstat

    @contextmanager
    def damaged_parent(**kwargs):
        assert kwargs == {"prefix": "ap-", "dir": "/tmp"}
        with TemporaryDirectory(**kwargs) as directory:
            parent = Path(directory)
            parents.append(parent)
            if damage == "mode":
                parent.chmod(0o755)
            elif damage == "owner":
                identity = parent.stat().st_ino
                def wrong_owner(descriptor):
                    info = original_fstat(descriptor)
                    if info.st_ino == identity:
                        return SimpleNamespace(st_uid=os.getuid() + 1, st_mode=info.st_mode)
                    return info
                monkeypatch.setattr(os, "fstat", wrong_owner)
            else:
                (parent / "real").mkdir(mode=0o700)
                (parent / "link").symlink_to(parent / "real", target_is_directory=True)
                directory = str(parent / "link")
            try:
                yield directory
            finally:
                monkeypatch.setattr(os, "fstat", original_fstat)

    monkeypatch.setattr(p, "TemporaryDirectory", damaged_parent)
    with pytest.raises((p._Refused, OSError)):
        with p._short_runtime_root(uuid4()):
            pytest.fail("unsafe runtime parent was accepted")
    assert len(parents) == 1 and not parents[0].exists()


def test_credential_probe_file_passes_with_deep_basetemp(tmp_path):
    """Run every probe assertion with a temp root longer than an AF_UNIX path."""
    nested_basetemp = os.environ.get("_AIRPLAY_CREDENTIAL_PROBE_DEEP_BASETEMP")
    if nested_basetemp is not None:
        # The child runs this test too; verify its actual root without recursing.
        assert len(os.fsencode(nested_basetemp)) > 108
        assert tmp_path.is_relative_to(Path(nested_basetemp))
        return

    deep_basetemp = tmp_path / ("deep-" + "x" * 120) / "pytest"
    deep_basetemp.parent.mkdir()
    assert len(os.fsencode(deep_basetemp)) > 108
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).resolve()),
         "--basetemp", str(deep_basetemp), "-q", "--tb=short", "--color=no"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "_AIRPLAY_CREDENTIAL_PROBE_DEEP_BASETEMP": str(deep_basetemp),
             "TMPDIR": str(deep_basetemp.parent)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
