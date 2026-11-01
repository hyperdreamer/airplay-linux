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

    def shell_tokens(program):
        # Keep quote information: reserved words and operators only have syntax
        # when unquoted. A '#' starts a comment only at the start of a word.
        tokens = []
        word = []
        started = quoted = assignment = brace = False

        def finish_word():
            nonlocal started, quoted, assignment, brace
            if started:
                value = "".join(word)
                if brace and value not in {"{", "}"}:
                    refuse("backend")
                kind = "assignment" if assignment else "quoted" if quoted else "word"
                tokens.append((value, kind))
                word.clear()
            started = quoted = assignment = brace = False

        index = 0
        while index < len(program):
            char = program[index]
            if char in " \t\n":
                finish_word()
                if char == "\n":
                    tokens.append((";", "operator"))
                index += 1
            elif char == "#" and not started:
                end = program.find("\n", index)
                index = len(program) if end < 0 else end
            elif char in {"'", '"'}:
                quote = char
                started = quoted = True
                index += 1
                while index < len(program) and program[index] != quote:
                    char = program[index]
                    if quote == '"' and char in "$`":
                        refuse("backend")
                    if (quote == '"' and char == "\\" and index + 1 < len(program)
                            and program[index + 1] in '$`"\\\n'):
                        index += 1
                        if program[index] != "\n":
                            word.append(program[index])
                    else:
                        word.append(char)
                    index += 1
                if index == len(program):
                    refuse("backend")
                index += 1
            elif char == "\\":
                index += 1
                if index == len(program):
                    refuse("backend")
                if program[index] != "\n":
                    started = quoted = True
                    word.append(program[index])
                index += 1
            elif char in ";&|()<>":
                # Adjacent unquoted digits are an IO number, not an executable.
                if char in "<>" and not quoted and "".join(word).isdigit():
                    word.clear()
                    started = False
                finish_word()
                operator = next((op for op in (
                    "&>>", "<<-", "<<<", ";;&", "&&", "||", ">>", "<<",
                    "<&", ">&", "<>", ">|", "&>", "|&", ";;", ";&",
                ) if program.startswith(op, index)), char)
                tokens.append((operator, "operator"))
                index += len(operator)
            else:
                # Active expansion/evaluation remains unsupported. Quoted or
                # escaped literal data reaches this lexer without these checks.
                if char in "$`*?[]":
                    refuse("backend")
                if char == "=" and not quoted and "".join(word).isidentifier():
                    assignment = True
                brace |= char in "{}"
                word.append(char)
                started = True
                index += 1
        finish_word()
        return tokens

    def check_shell(program, *, env=None):
        if opted_in:
            return

        def command(argv):
            if not argv:
                return
            name = "." if argv[0] == "." else Path(argv[0]).name
            # These can evaluate code or change lookup for a later command.
            if name in {"eval", "source", ".", "cd", "pushd", "popd", "unset", "alias"}:
                refuse("backend")
            if name == "read" and "PATH" in argv[1:]:
                refuse("backend")
            if name in {"export", "declare", "typeset", "local", "readonly"} and any(
                value.split("=", 1)[0] == "PATH" for value in argv[1:]
            ):
                refuse("backend")
            if name == "hash" and any(value.startswith("-p") for value in argv[1:]):
                refuse("backend")
            if name in {"env", "command", "exec", "builtin", "time"}:
                tail = argv[1:]
                if name == "exec":
                    while tail:
                        if tail[0] == "--":
                            tail = tail[1:]
                            break
                        if tail[0] == "-a" and len(tail) > 1:
                            tail = tail[2:]
                        elif tail[0] in {"-c", "-l", "-cl", "-lc"}:
                            tail = tail[1:]
                        else:
                            break
                elif name == "time" and tail[:1] == ["-p"]:
                    tail = tail[1:]
                if tail and tail[0] == "--":
                    tail = tail[1:]
                elif tail and (tail[0].startswith("-")
                               or ("=" in tail[0] and tail[0].split("=", 1)[0].isidentifier())):
                    refuse("backend")
                command(tail)
            else:
                # Arguments, including a private socket called 'sh', never
                # become separate executable candidates.
                check_process(argv, env=env, observation=False)

        argv = []
        redirect = False
        for_header = None
        cases = []
        for value, kind in shell_tokens(os.fsdecode(program)):
            # Loop variables/values and case subjects/patterns are shell data.
            # Their bodies resume command parsing at 'do' or the pattern's ')'.
            if for_header is not None:
                if kind == "operator" and value == ";" and for_header != "variable":
                    for_header = None
                elif kind == "operator":
                    refuse("backend")
                elif for_header == "variable":
                    if not value.isidentifier() or value == "PATH":
                        refuse("backend")
                    for_header = "in"
                elif for_header == "in" and kind == "word" and value == "in":
                    for_header = "values"
                elif for_header == "in" and kind == "word" and value == "do":
                    for_header = None
                elif for_header != "values":
                    refuse("backend")
                continue
            if cases and cases[-1] in {"subject", "in", "pattern"}:
                state = cases[-1]
                if state == "subject" and kind != "operator":
                    cases[-1] = "in"
                elif state == "in" and kind == "word" and value == "in":
                    cases[-1] = "pattern"
                elif state == "in" and kind == "operator" and value == ";":
                    continue
                elif state == "pattern":
                    if kind == "operator" and value == ")":
                        cases[-1] = "body"
                    elif kind == "word" and value == "esac":
                        cases.pop()
                    elif kind == "operator" and value not in {"(", "|", ";"}:
                        refuse("backend")
                else:
                    refuse("backend")
                continue
            if cases and kind == "operator" and value in {";;", ";&", ";;&"}:
                if redirect:
                    refuse("backend")
                command(argv)
                argv = []
                cases[-1] = "pattern"
            elif kind == "operator":
                if redirect:
                    refuse("backend")
                if value in {"<", ">", ">>", "<&", ">&", "<>", ">|", "&>", "&>>"}:
                    redirect = True
                elif value in {";", "&", "&&", "||", "|", "|&", "(", ")"}:
                    command(argv)
                    argv = []
                else:
                    refuse("backend")
            elif redirect:
                redirect = False
            elif not argv and kind == "word" and value in {"for", "select"}:
                for_header = "variable"
            elif not argv and kind == "word" and value == "case":
                cases.append("subject")
            elif not argv and kind == "word" and value == "esac" and cases:
                cases.pop()
            elif not argv and kind == "word" and value in {
                "if", "then", "elif", "else", "while", "until", "do", "!", "{",
                "fi", "done", "}",
            }:
                continue
            elif not argv and kind == "assignment":
                refuse("backend")
            else:
                argv.append(value)
        if redirect or for_header is not None or cases:
            refuse("backend")
        command(argv)

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
            if len(argv) > 2 and argv[1] == "-c":
                check_shell(argv[2], env=env)
            else:
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
        shell = kwargs.get("shell", positional[7] if len(positional) > 7 else False)
        env = kwargs.get("env")
        if shell:
            program = args[0] if isinstance(args, (tuple, list)) and args else args
            check_shell(program, env=env)
        else:
            check_process(args, env=env)
        executable = kwargs.get("executable", positional[1] if len(positional) > 1 else None)
        if executable is not None:
            tail = ("-c", program) if shell else args[1:] if isinstance(args, (tuple, list)) else ()
            check_process((executable, *tail), env=env, observation=not shell)
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
