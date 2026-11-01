"""Actively attempt absent live dependencies; absence cannot satisfy a guard."""
import os
import socket
import subprocess

import pytest

from airplay_linux.domain.ports import CommandSpec
from airplay_linux.process.linux import LinuxProcessRunner


@pytest.mark.parametrize("boundary", ("backend", "ctl", "session_bus", "socket_lease"))
async def test_unmarked_live_boundary_attempts_are_failures(offline_boundary_tripwires, boundary):
    c = offline_boundary_tripwires
    with pytest.raises(c.refusal_type):
        await c.attempts[boundary]()
    assert c.attempted == [boundary]


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("entry", (
    "run", "spawn", "popen", "shell", "asyncio", "bare", "unshare",
    "pathlike", "executable", "shell_exec", "env",
))
async def test_execution_entry_points_refuse_before_lookup(offline_boundary_tripwires, monkeypatch, name, entry):
    import asyncio

    c = offline_boundary_tripwires
    executable = str(c.root / "absent" / name)
    argv = (executable, "-daemonize" if name == "doubletake" else "-status")
    # Even a failed PATH lookup must be preceded by an attempted-boundary failure.
    monkeypatch.setenv("PATH", str(c.root / "absent"))
    with pytest.raises(c.refusal_type):
        if entry == "run":
            await LinuxProcessRunner().run(CommandSpec(argv=argv, env={}), timeout=0.1)
        elif entry == "spawn":
            await LinuxProcessRunner().spawn(CommandSpec(argv=argv, env={}))
        elif entry == "popen":
            subprocess.Popen(argv)
        elif entry == "shell":
            subprocess.Popen(" ".join(argv), shell=True)
        elif entry == "asyncio":
            await asyncio.create_subprocess_exec(*argv)
        elif entry == "bare":
            subprocess.Popen((name, argv[1]))
        elif entry == "pathlike":
            from pathlib import Path
            subprocess.Popen(Path(executable))
        elif entry == "executable":
            subprocess.Popen(("alias", argv[1]), executable=executable)
        elif entry == "shell_exec":
            subprocess.Popen("exec " + " ".join(argv), shell=True)
        elif entry == "env":
            subprocess.Popen(("/usr/bin/env", "--", *argv))
        else:
            subprocess.Popen((str(c.root / "absent" / "unshare"), "-rn", *argv))
    assert c.attempted == ["backend" if name == "doubletake" else "ctl"]


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("program", (
    "true;{command}",
    "true&&{command}",
    "false||{command}",
    "if true;then {command};fi",
    "true\n{command}",
    "true|{command}",
    "({command})",
    "{{ {command}; }}",
    "true & {command}",
    "printf x#y;{command}",
    "printf x#y&&{command}",
    "# ignored comment\n{command}",
    "printf '%s' ';' 'then';{command}",
    "if false;then true;else {command};fi",
    "while {command};do :;done",
    "while false;do {command};done",
    "! {command}",
    "exec {command}",
    "command -- {command}",
    "for value in offline;do {command};done",
    "for value do {command};done",
    "case offline in offline) {command};;esac",
    "case offline in other|offline) {command};;esac",
    "time -p {command}",
    "/bin/sh -c '{command}'",
))
@pytest.mark.parametrize("entry", ("subprocess", "shell_argv", "explicit_shell", "asyncio"))
async def test_compound_shell_boundaries_refuse_before_child_creation(
    offline_boundary_tripwires, monkeypatch, name, program, entry,
):
    import asyncio

    c = offline_boundary_tripwires
    monkeypatch.setenv("PATH", str(c.root / "absent"))

    def unexpected_child(*args, **kwargs):
        pytest.fail("shell boundary reached child creation before refusal")

    monkeypatch.setattr(subprocess.Popen, "_execute_child", unexpected_child)
    command = f"{name} {'-daemonize' if name == 'doubletake' else '-status'}"
    script = program.format(command=command)
    with pytest.raises(c.refusal_type):
        if entry == "subprocess":
            subprocess.run(script, shell=True, capture_output=True, check=False)
        elif entry == "shell_argv":
            subprocess.run((script, "shell-argument"), shell=True, capture_output=True, check=False)
        elif entry == "explicit_shell":
            subprocess.run(("/bin/sh", "-c", script), capture_output=True, check=False)
        else:
            await asyncio.create_subprocess_shell(script)
    assert c.attempted == ["backend" if name == "doubletake" else "ctl"]


@pytest.mark.parametrize("program", (
    'command=doubletake; "$command" -daemonize',
    'command=doubletake-ctl; "$command" -status',
    'eval "true; doubletake -daemonize"',
    'eval "true; doubletake-ctl -status"',
    'printf "%s" "$(doubletake -daemonize)"',
    'printf "%s" "`doubletake-ctl -status`"',
    "doubleta?e -daemonize",
    "double{take,take-ctl} -status",
    ". /absent/shell-program",
    "/bin/sh /absent/shell-program",
))
def test_unsupported_shell_evaluation_refuses_before_child_creation(
    offline_boundary_tripwires, monkeypatch, program,
):
    c = offline_boundary_tripwires
    monkeypatch.setenv("PATH", str(c.root / "absent"))

    def unexpected_child(*args, **kwargs):
        pytest.fail("unsupported shell evaluation reached child creation before refusal")

    monkeypatch.setattr(subprocess.Popen, "_execute_child", unexpected_child)
    with pytest.raises(c.refusal_type):
        subprocess.run(program, shell=True, capture_output=True, check=False)
    assert c.attempted


@pytest.mark.parametrize(("program", "output"), (
    ("true;printf offline", "offline"),
    ("true&&printf offline", "offline"),
    ("if true;then printf offline;fi", "offline"),
    ("(printf offline)", "offline"),
    ("printf '%s' 'doubletake -daemonize'", "doubletake -daemonize"),
    ("printf '%s' 'doubletake'", "doubletake"),
    ("printf '%s' 'doubletake-ctl'", "doubletake-ctl"),
    ("printf '%s' 'sh'", "sh"),
    ("printf '%s' '$HOME'", "$HOME"),
    ("printf '%s' 'foo?bar'", "foo?bar"),
    ("printf '%s' 'a=b'", "a=b"),
    ("printf '%s' 'eval'", "eval"),
    ("printf '%s' 'PATH'", "PATH"),
    ("printf '%s' 'if' 'then' 'do' 'else' 'while' '!' '{' 'exec'", "ifthendoelsewhile!{exec"),
    ("printf '%s' \"foo?bar\"", "foo?bar"),
    ("printf '%s' double\\take", "doubletake"),
    ("printf '%s' 'double'\"take\"", "doubletake"),
    ("printf x#y # doubletake -daemonize", "x#y"),
    ("# doubletake-ctl -status\nprintf offline", "offline"),
    ("printf '%s' 'a;doubletake -daemonize'", "a;doubletake -daemonize"),
    ("for value in doubletake sh eval PATH;do printf offline;done", "offlineofflineofflineoffline"),
    ("case doubletake in doubletake) printf offline;;esac", "offline"),
    ("case doubletake\nin\ndoubletake) printf offline;;esac", "offline"),
    ("read value </dev/null || printf offline", "offline"),
    ("set -- doubletake sh;printf offline", "offline"),
    ("export ordinary;printf offline", "offline"),
    ("exec printf '%s' doubletake", "doubletake"),
    ("command printf '%s' sh", "sh"),
    ("env -- printf '%s' doubletake", "doubletake"),
))
def test_unrelated_shell_programs_remain_available(offline_boundary_tripwires, program, output):
    result = subprocess.run(program, shell=True, capture_output=True, text=True, check=True)
    assert result.stdout == output
    assert offline_boundary_tripwires.attempted == []


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
def test_compound_shell_permits_marked_private_shims(
    offline_boundary_tripwires, tmp_path, monkeypatch, name,
):
    shim = tmp_path / name
    shim.write_text(
        f"#!/bin/sh\n# private test-only shim: {name}\n# fake_doubletake.py\nprintf private\n",
    )
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    socket_path = tmp_path / "sh"
    result = subprocess.run(
        f"true;{name} -status -socket '{socket_path}'",
        shell=True, capture_output=True, text=True, check=True,
    )
    assert result.stdout == "private"
    assert offline_boundary_tripwires.attempted == []


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl", "sh"))
@pytest.mark.parametrize("redirect", (">", "2>", ">>"))
def test_shell_redirection_paths_are_data(
    offline_boundary_tripwires, tmp_path, name, redirect,
):
    output = tmp_path / name
    result = subprocess.run(
        f"{redirect}'{output}' printf offline", shell=True,
        capture_output=True, text=True, check=True,
    )
    assert (result.stdout, output.read_text()) == (("offline", "") if redirect == "2>" else ("", "offline"))
    assert offline_boundary_tripwires.attempted == []


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("program", (
    "/usr/bin/env -u PATH {command}",
    "/usr/bin/env -i {command}",
    "command -p {command}",
    "read PATH;{command}",
    "export PATH;{command}",
    "for PATH in /absent;do {command};done",
))
def test_shell_cannot_retarget_a_permitted_private_shim(
    offline_boundary_tripwires, tmp_path, monkeypatch, name, program,
):
    c = offline_boundary_tripwires
    shim = tmp_path / name
    shim.write_text(f"#!/bin/sh\n# private test-only shim: {name}\n# fake_doubletake.py\nexit 0\n")
    shim.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    def unexpected_child(*args, **kwargs):
        pytest.fail("shell changed executable lookup before refusal")

    monkeypatch.setattr(subprocess.Popen, "_execute_child", unexpected_child)
    with pytest.raises(c.refusal_type):
        subprocess.run(program.format(command=f"{name} -status"), shell=True, capture_output=True, check=False)
    assert c.attempted == ["backend"]


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("flag", ("--help", "--version", "-capabilities"))
def test_direct_read_only_observations_remain_available(offline_boundary_tripwires, tmp_path, name, flag):
    # This is deliberately unmarked: only the exact observation argv permits it.
    executable = tmp_path / name
    executable.write_text("#!/bin/sh\nprintf observation\n")
    executable.chmod(0o700)
    result = subprocess.run((executable, flag), capture_output=True, text=True, check=True)
    assert result.stdout == "observation"
    assert offline_boundary_tripwires.attempted == []


@pytest.mark.parametrize("entry", ("constructor", "capability_probe"))
async def test_session_bus_refuses_before_address_lookup(offline_boundary_tripwires, monkeypatch, entry):
    from dbus_next.aio import MessageBus
    from airplay_linux.policy.audio import DBusCapabilityClient

    c = offline_boundary_tripwires
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(c.refusal_type):
        if entry == "constructor":
            MessageBus()
        else:
            await DBusCapabilityClient().interfaces("org.example.Absent", "/org/example/Absent")
    assert c.attempted == ["session_bus"]


@pytest.mark.parametrize("entry", ("connect", "connect_ex", "symlink"))
def test_socket_entry_points_refuse_before_connect(offline_boundary_tripwires, tmp_path, entry):
    c = offline_boundary_tripwires
    path = c.root / "absent.sock"
    if entry == "symlink":
        link = tmp_path / "host-root"
        link.symlink_to(c.root, target_is_directory=True)
        path = link / "absent.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(c.refusal_type):
            getattr(client, "connect_ex" if entry == "connect_ex" else "connect")(os.fspath(path))
    assert c.attempted == ["socket_lease"]


async def test_composition_capability_observations_use_private_offline_evidence(
    offline_boundary_tripwires, monkeypatch, tmp_path_factory,
):
    from pathlib import Path
    from types import SimpleNamespace

    from airplay_linux.domain.resources import Err
    from airplay_linux.policy import audio
    from airplay_linux.policy.capabilities import EnvironmentProbe
    from airplay_linux.testing.adapters import FakeProcess

    observed = []

    def private_pipewire(path):
        if not Path(path).is_relative_to(tmp_path_factory.getbasetemp()):
            pytest.fail("composition capability probe attempted the host PipeWire socket")
        observed.append(path)
        return False

    monkeypatch.setattr(audio, "_socket_reachable", private_pipewire)
    probe = EnvironmentProbe.from_environment(
        process=FakeProcess(), helper=SimpleNamespace(probe=None),
    )
    assert isinstance(await probe._audio(), Err)
    assert await probe._keyring() is False
    assert len(observed) == 1
    assert offline_boundary_tripwires.attempted == []
