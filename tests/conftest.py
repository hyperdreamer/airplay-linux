"""Default host-boundary controls for the offline test suite."""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from pathlib import Path

import pytest


class OfflineBoundaryRefusal(pytest.fail.Exception):
    """A test failure that production ``except Exception`` cannot hide."""


@pytest.fixture(scope="session")
def offline_live_attempt_root():
    # Session teardown follows per-test monkeypatch cleanup, including tests
    # which deliberately make every os.open/stat call fail.
    with TemporaryDirectory(prefix="ap-offline-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture(autouse=True)
def offline_boundary_tripwires(monkeypatch, request, tmp_path_factory, offline_live_attempt_root):
    """Refuse operational host I/O; explicitly injected fakes remain usable.

    The delivered installed-surface test uses bounded, read-only help/version/
    capability probes. Only those exact argv shapes are observations. Private
    executable shims must be inside pytest's root and identify the fake script;
    arbitrary PATH entries, including missing commands, confer no permission.
    """
    from dbus_next.aio import MessageBus
    from airplay_linux.capture import portal
    from airplay_linux.domain.operations import ProcessIdentity
    from airplay_linux.domain.ports import CommandSpec
    from airplay_linux.process import ownership
    from airplay_linux.process.linux import LinuxProcessRunner
    from airplay_linux.policy.capabilities import EnvironmentProbe
    from airplay_linux import runtime as composition

    attempted = []
    private_roots = [tmp_path_factory.getbasetemp()]
    if "private_socket_root" in request.fixturenames:
        private_roots.append(request.getfixturevalue("private_socket_root"))

    def private_path(value):
        path = Path(os.fsdecode(value))
        if not path.is_absolute() or ".." in path.parts:
            return False
        # Endpoint verification deliberately connects through an anchored dirfd.
        if str(path).startswith("/proc/self/fd/"):
            try:
                path = path.resolve(strict=True)
            except OSError:
                return False
        # Resolve only paths already below an explicitly test-owned directory.
        # This prevents a symlink in that directory from reaching a host socket.
        for root in private_roots:
            if path.is_relative_to(root):
                return path.resolve(strict=False).is_relative_to(root.resolve())
        return False

    live_markers = {
        "live_backend": "AIRPLAY_LINUX_LIVE_BACKEND",
        "live_kde": "AIRPLAY_LINUX_LIVE_KDE",
        "live_hardware": "AIRPLAY_LINUX_LIVE_HARDWARE",
    }
    opted_in = any(
        request.node.get_closest_marker(marker) is not None and os.environ.get(env) == "1"
        for marker, env in live_markers.items()
    )

    # Composition smoke tests instantiate real adapters. Supply unavailable,
    # private capability evidence at their existing injection seam so startup
    # observations cannot consult the host desktop. Direct client construction
    # remains guarded below, and tests may still inject their own evidence.
    from_environment = EnvironmentProbe.from_environment.__func__

    class OfflineCapabilities:
        async def interfaces(self, service, path):
            return None

    def offline_environment(cls, **kwargs):
        if not opted_in:
            if kwargs.get("dbus") is None:
                kwargs["dbus"] = OfflineCapabilities()
            if kwargs.get("environment") is None:
                kwargs["environment"] = {
                    **os.environ, "XDG_RUNTIME_DIR": str(private_roots[0] / "offline-runtime"),
                }
        return from_environment(cls, **kwargs)

    audio_probe = composition.PipeWirePortalAudioProbe

    def offline_audio(*args, **kwargs):
        if not opted_in:
            if kwargs.get("portal") is None:
                kwargs["portal"] = OfflineCapabilities()
            if kwargs.get("environment") is None:
                kwargs["environment"] = {
                    **os.environ, "XDG_RUNTIME_DIR": str(private_roots[0] / "offline-runtime"),
                }
        return audio_probe(*args, **kwargs)

    monkeypatch.setattr(EnvironmentProbe, "from_environment", classmethod(offline_environment))
    monkeypatch.setattr(composition, "PipeWirePortalAudioProbe", offline_audio)

    def refuse(boundary):
        attempted.append(boundary)
        raise OfflineBoundaryRefusal(f"unmarked live boundary attempted: {boundary}")

    def check_process(argv, *, env=None, observation=True):
        if isinstance(argv, (str, bytes)):
            argv = shlex.split(os.fsdecode(argv))
        elif isinstance(argv, os.PathLike):
            argv = (os.fspath(argv),)
        if opted_in or not isinstance(argv, (tuple, list)) or not argv:
            return
        executable = os.fsdecode(argv[0])
        name = Path(executable).name
        if name in {"sh", "bash", "dash"} and len(argv) > 2 and argv[1] == "-c":
            check_process(argv[2], env=env, observation=False)
            return
        if name in {"env", "exec"}:
            index = 1
            while index < len(argv):
                argument = os.fsdecode(argv[index])
                if argument == "--":
                    index += 1
                    break
                if argument in {"-u", "--unset", "-C", "--chdir", "-a"}:
                    index += 2
                elif argument in {"-S", "--split-string"} and index + 1 < len(argv):
                    check_process((*shlex.split(os.fsdecode(argv[index + 1])), *argv[index + 2:]),
                                  env=env, observation=False)
                    return
                elif argument.startswith("-") or ("=" in argument and argument.split("=", 1)[0].isidentifier()):
                    index += 1
                else:
                    break
            check_process(argv[index:], env=env, observation=False)
            return
        if name == "unshare":
            if "airplay_linux.process.credential_probe" in argv:
                refuse("backend")
            for index, argument in enumerate(argv[1:], 1):
                if Path(os.fsdecode(argument)).name in {"doubletake", "doubletake-ctl"}:
                    check_process(argv[index:], env=env, observation=False)
            return
        if name not in {"doubletake", "doubletake-ctl"}:
            return
        if observation and len(argv) == 2 and argv[1] in {"--help", "--version", "-capabilities"}:
            return
        # Inspect only an explicitly private candidate, never resolve a host
        # executable to decide whether an attempted operational call is allowed.
        search_path = (os.environ if env is None else env).get("PATH", os.defpath)
        candidate = Path(executable) if "/" in executable else Path(search_path.split(os.pathsep)[0]) / name
        if private_path(candidate):
            try:
                with candidate.open("rb") as stream:
                    header = stream.read(8192)
                if (f"# private test-only shim: {name}\n".encode() in header
                        and b"fake_doubletake.py" in header):
                    return
            except OSError:
                pass
        refuse("backend" if name == "doubletake" else "ctl")

    run = LinuxProcessRunner.run
    spawn = LinuxProcessRunner.spawn
    popen_init = subprocess.Popen.__init__

    async def guarded_run(self, spec, timeout, stdin=None):
        check_process(spec.argv, env={**os.environ, **spec.env})
        return await run(self, spec, timeout, stdin)

    async def guarded_spawn(self, spec):
        check_process(spec.argv, env={**os.environ, **spec.env}, observation=False)
        return await spawn(self, spec)

    def guarded_popen(self, args, *positional, **kwargs):
        observation = not kwargs.get("shell", False)
        check_process(args, env=kwargs.get("env"), observation=observation)
        executable = kwargs.get("executable", positional[1] if len(positional) > 1 else None)
        if executable is not None:
            tail = args[1:] if isinstance(args, (tuple, list)) else ()
            check_process((executable, *tail), env=kwargs.get("env"), observation=observation)
        return popen_init(self, args, *positional, **kwargs)

    construct_bus = MessageBus.__init__

    def guarded_bus(self, *args, **kwargs):
        if not opted_in:
            refuse("session_bus")
        construct_bus(self, *args, **kwargs)

    def check_socket(path):
        if not opted_in and not private_path(path):
            refuse("socket_lease")

    anchor = ownership._anchor

    @contextmanager
    def guarded_anchor(reservation, io):
        if type(io) is ownership.EndpointObservations:
            check_socket(reservation.runtime_directory)
        with anchor(reservation, io) as descriptor:
            yield descriptor

    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_connect(self, address):
        if self.family == socket.AF_UNIX:
            check_socket(address)
        return connect(self, address)

    def guarded_connect_ex(self, address):
        if self.family == socket.AF_UNIX:
            check_socket(address)
        return connect_ex(self, address)

    monkeypatch.setattr(LinuxProcessRunner, "run", guarded_run)
    monkeypatch.setattr(LinuxProcessRunner, "spawn", guarded_spawn)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen)
    monkeypatch.setattr(MessageBus, "__init__", guarded_bus)
    monkeypatch.setattr(ownership, "_anchor", guarded_anchor)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    root = offline_live_attempt_root
    runner = LinuxProcessRunner()

    async def backend():
        await runner.spawn(CommandSpec(
            argv=(str(root / "absent" / "doubletake"), "-daemonize"), env={},
        ))

    async def ctl():
        await runner.run(CommandSpec(
            argv=(str(root / "absent" / "doubletake-ctl"), "-status"), env={},
        ), timeout=0.1)

    async def session_bus():
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={root}/absent-bus")
        await portal._connect_session_bus()

    async def socket_lease():
        entry = root.stat()
        receipt = SimpleNamespace(
            daemon=ProcessIdentity(pid=123, start_ticks=1),
            runtime_directory=str(root), socket_name="absent.sock",
            owner_uid=entry.st_uid, runtime_device=entry.st_dev,
            runtime_inode=entry.st_ino,
        )
        ownership.verify_owned_endpoint(receipt, current_generation=1)

    yield SimpleNamespace(
        refusal_type=OfflineBoundaryRefusal, attempted=attempted, root=root,
        attempts={"backend": backend, "ctl": ctl, "session_bus": session_bus,
                  "socket_lease": socket_lease},
    )
