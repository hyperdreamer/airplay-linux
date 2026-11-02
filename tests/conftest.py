"""Default host-boundary controls for the offline test suite."""
from __future__ import annotations

import os
import shlex
import shutil
import socket
import subprocess
from contextlib import contextmanager
from tempfile import TemporaryDirectory, mkdtemp
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
def private_legacy_supervisor_ctl(
    offline_boundary_tripwires, monkeypatch, request, tmp_path_factory,
):
    # These legacy real-process tests inject socket-daemon/fake-daemon children,
    # but their supervisor subprocess uses its default control factory. Give
    # that subprocess the existing private crashing ctl fixture, so teardown
    # exercises its fallback without looking up an installed control client.
    if request.path != Path(__file__).parent / "process/test_supervisor.py":
        return
    import sys

    directory = tmp_path_factory.mktemp("supervisor-ctl")
    script = Path(__file__).parent / "fixtures/processes/fake_doubletake.py"
    shim = directory / "doubletake-ctl"
    shim.write_text(
        "#!/bin/sh\n# private test-only shim: doubletake-ctl\n"
        "export AIRPLAY_FAKE_CTL_MODE=crash\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
        '--shim-name doubletake-ctl "$@"\n',
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")


@pytest.fixture(autouse=True)
def offline_boundary_tripwires(monkeypatch, request, tmp_path_factory, offline_live_attempt_root):
    """Refuse operational host I/O; explicitly injected fakes remain usable.

    Direct execution is checked before lookup. Shells resolve bare backend names
    through private refusal shims; their bodies are never parsed here. A -c
    shell also starts with readonly PATH so builtins cannot retarget an allowed
    fake. This is a lookup tripwire, not a sandbox for arbitrary child programs.
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

    # The shell itself chooses executable positions. Each shim records before
    # doing anything else and never starts a backend or another executable.
    # Use a separate short directory/log for every test, including nested pytest
    # runs. Session cleanup happens after all monkeypatches have been undone.
    shim_root = Path(mkdtemp(prefix="tripwire-", dir=offline_live_attempt_root))
    shell_log = shim_root / "attempts"
    shell_log.touch(mode=0o600)
    for name, boundary in (("doubletake", "backend"), ("doubletake-ctl", "ctl")):
        shim = shim_root / name
        shim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {boundary} >> {shlex.quote(str(shell_log))}\n"
            "exit 125\n",
        )
        shim.chmod(0o700)
    # Keep the observation handle open: tests deliberately replace os.open/stat.
    log_reader = shell_log.open()
    accepted_shell_attempts = []

    def shell_attempts():
        log_reader.seek(0)
        return log_reader.read().splitlines()

    def accept_shell_attempts(expected):
        assert shell_attempts() == expected
        accepted_shell_attempts[:] = expected

    def private_shim(candidate, name):
        if not private_path(candidate):
            return False
        try:
            with candidate.open("rb") as stream:
                header = stream.read(8192)
            return (f"# private test-only shim: {name}\n".encode() in header
                    and b"fake_doubletake.py" in header)
        except OSError:
            return False

    def execution_environment(env):
        environment = dict(os.environ if env is None else env)
        if opted_in:
            return environment
        path = environment.get("PATH", os.defpath).split(os.pathsep)
        path = [entry for entry in path if entry != str(shim_root)]
        # Only an explicitly marked, test-owned directory can precede the
        # tripwires. Validate both names so a marked backend cannot authorise
        # an unmarked ctl (or vice versa) in the same directory.
        first = Path(path[0]) if path and path[0] else Path(".")
        candidates = [first / name for name in ("doubletake", "doubletake-ctl")]
        allowed = [private_shim(candidate, candidate.name) for candidate in candidates]
        private_first = any(allowed) and all(
            marked or not candidate.exists() for candidate, marked in zip(candidates, allowed)
        )
        index = 1 if private_first else 0
        path.insert(index, str(shim_root))
        environment["PATH"] = os.pathsep.join(path)
        return environment

    if not opted_in:
        monkeypatch.setenv("PATH", execution_environment(None)["PATH"])

    def check_process(argv, *, env=None, observation=True):
        if isinstance(argv, (str, bytes)):
            argv = shlex.split(os.fsdecode(argv))
        elif isinstance(argv, os.PathLike):
            argv = (os.fspath(argv),)
        if opted_in or not isinstance(argv, (tuple, list)) or not argv:
            return
        executable = os.fsdecode(argv[0])
        name = Path(executable).name
        if name in {"sh", "bash", "dash"}:
            if len(argv) <= 2 or argv[1] != "-c":
                # Script files, stdin and other shell option forms are opaque.
                refuse("backend")
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
        if private_shim(candidate, name):
            return
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
        shell = kwargs.get("shell", positional[7] if len(positional) > 7 else False)
        env = kwargs.get("env", positional[9] if len(positional) > 9 else None)
        executable = kwargs.get("executable", positional[1] if len(positional) > 1 else None)
        if not shell:
            check_process(args, env=env)
        if executable is not None:
            tail = ("-c", "") if shell else args[1:] if isinstance(args, (tuple, list)) else ()
            check_process((executable, *tail), env=env, observation=not shell)
        if opted_in:
            return popen_init(self, args, *positional, **kwargs)

        environment = execution_environment(env)
        argv = list(args) if isinstance(args, (tuple, list)) else None
        if shell:
            program = argv[0] if argv else args
            program = os.fsdecode(program)
            guarded = "readonly PATH\n" + program
            args = [guarded, *argv[1:]] if argv else guarded
        elif argv and Path(os.fsdecode(executable or argv[0])).name in {"sh", "bash", "dash"}:
            argv[2] = "readonly PATH\n" + os.fsdecode(argv[2])
            args = argv
        elif argv and len(argv) == 2 and argv[1] in {"--help", "--version", "-capabilities"}:
            name = Path(os.fsdecode(executable or argv[0])).name
            if name in {"doubletake", "doubletake-ctl"}:
                # Exact read-only observations retain the original installed
                # lookup, independently of operational shell interception.
                path = environment["PATH"].split(os.pathsep)
                environment["PATH"] = os.pathsep.join(entry for entry in path if entry != str(shim_root))
                if "/" not in os.fsdecode(argv[0]) or Path(argv[0]).parent == shim_root:
                    candidate = shutil.which(name, path=environment["PATH"])
                    if candidate is not None:
                        argv[0] = candidate
                        args = argv

        positional = list(positional)
        if len(positional) > 9:
            positional[9] = environment
        else:
            kwargs["env"] = environment
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

    class Tripwires(SimpleNamespace):
        @property
        def attempted(self):
            return attempted + shell_attempts()

    try:
        yield Tripwires(
            refusal_type=OfflineBoundaryRefusal, root=root, shim_root=shim_root,
            accept_shell_attempts=accept_shell_attempts,
            attempts={"backend": backend, "ctl": ctl, "session_bus": session_bus,
                      "socket_lease": socket_lease},
        )
    finally:
        unaccepted = shell_attempts() != accepted_shell_attempts
        log_reader.close()
        if unaccepted:
            raise OfflineBoundaryRefusal("unmarked live boundary attempted through shell")
