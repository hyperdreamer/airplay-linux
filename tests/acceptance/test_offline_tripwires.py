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
    "run", "spawn", "popen", "asyncio", "bare", "unshare",
    "pathlike", "relative", "executable", "env",
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
        elif entry == "asyncio":
            await asyncio.create_subprocess_exec(*argv)
        elif entry == "bare":
            subprocess.Popen((name, argv[1]))
        elif entry == "pathlike":
            from pathlib import Path
            subprocess.Popen(Path(executable))
        elif entry == "relative":
            subprocess.Popen(("./" + name, argv[1]))
        elif entry == "executable":
            subprocess.Popen(("alias", argv[1]), executable=executable)
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
async def test_compound_shell_boundaries_are_recorded_at_resolution(
    offline_boundary_tripwires, monkeypatch, name, program, entry,
):
    import asyncio

    c = offline_boundary_tripwires
    monkeypatch.setenv("PATH", str(c.root / "absent") + os.pathsep + os.defpath)
    command = f"{name} {'-daemonize' if name == 'doubletake' else '-status'}"
    script = program.format(command=command)
    # In POSIX mode `time -p` may require a separate executable. Exercise Bash's
    # keyword directly so this proof never depends on an installed time tool.
    shell = "/bin/bash" if program.startswith("time ") else "/bin/sh"
    if entry == "subprocess":
        subprocess.run(script, shell=True, executable=shell, capture_output=True, check=False)
    elif entry == "shell_argv":
        subprocess.run(
            (script, "shell-argument"), shell=True, executable=shell,
            capture_output=True, check=False,
        )
    elif entry == "explicit_shell":
        subprocess.run((shell, "-c", script), capture_output=True, check=False)
    else:
        child = await asyncio.create_subprocess_shell(
            script, executable=shell, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await child.communicate()
    # Resolution happens only on an executed branch. Inactive loop bodies are
    # data until the shell actually evaluates them.
    executed = program not in {"while false;do {command};done", "for value do {command};done"}
    expected = ["backend" if name == "doubletake" else "ctl"] if executed else []
    assert c.attempted == expected
    c.accept_shell_attempts(expected)


@pytest.mark.parametrize(("program", "boundary"), (
    ('command=doubletake; "$command" -daemonize', "backend"),
    ('command=doubletake-ctl; "$command" -status', "ctl"),
    ('eval "true; doubletake -daemonize"', "backend"),
    ('eval "true; doubletake-ctl -status"', "ctl"),
    ('printf "%s" "$(doubletake -daemonize)"', "backend"),
    ('printf "%s" "`doubletake-ctl -status`"', "ctl"),
    ("doubleta?e -daemonize", "backend"),
    ("double{take,take-ctl} -status", "backend"),
    (". ./source-program", "ctl"),
    ("source ./source-program", "ctl"),
    ("shopt -s expand_aliases; alias backend=doubletake\nbackend -status", "backend"),
))
def test_shell_evaluation_uses_resolution_tripwires(
    offline_boundary_tripwires, tmp_path, monkeypatch, program, boundary,
):
    c = offline_boundary_tripwires
    monkeypatch.setenv("PATH", str(c.root / "absent"))
    # Supply a glob match and a sourced program; neither can contact a backend.
    (tmp_path / "doubletake").touch()
    (tmp_path / "source-program").write_text("doubletake-ctl -status\n")
    subprocess.run(
        ("/bin/bash", "-c", program), cwd=tmp_path, capture_output=True, check=False,
    )
    assert c.attempted == [boundary]
    c.accept_shell_attempts([boundary])


@pytest.mark.parametrize("argv", (
    ("/bin/sh", "/absent/shell-program"),
    ("/bin/bash", "-lc", "true"),
    ("/bin/sh",),
))
def test_opaque_shell_entries_refuse_before_child_creation(
    offline_boundary_tripwires, monkeypatch, argv,
):
    c = offline_boundary_tripwires

    def unexpected_child(*args, **kwargs):
        pytest.fail("opaque shell invocation reached child creation before refusal")

    monkeypatch.setattr(subprocess.Popen, "_execute_child", unexpected_child)
    with pytest.raises(c.refusal_type):
        subprocess.run(argv, capture_output=True, check=False)
    assert c.attempted == ["backend"]


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
    "read PATH;{command}",
    "export PATH=a;{command}",
    "for PATH in a;do {command};done",
))
def test_shell_cannot_retarget_a_permitted_private_shim(
    offline_boundary_tripwires, tmp_path, monkeypatch, name, program,
):
    c = offline_boundary_tripwires
    permitted = tmp_path / "permitted"
    permitted.mkdir()
    shim = permitted / name
    shim.write_text(f"#!/bin/sh\n# private test-only shim: {name}\n# fake_doubletake.py\nprintf private\n")
    shim.chmod(0o700)
    alternate = tmp_path / "a"
    alternate.mkdir()
    witness = tmp_path / "unmarked-executed"
    unmarked = alternate / name
    unmarked.write_text(f"#!/bin/sh\n: >'{witness}'\n")
    unmarked.chmod(0o700)
    monkeypatch.setenv("PATH", str(permitted))
    result = subprocess.run(
        program.format(command=f"{name} -status"), shell=True, cwd=tmp_path,
        capture_output=True, text=True, input="a\n", check=False,
    )
    assert not witness.exists()
    assert "PATH" in result.stderr and "readonly" in result.stderr
    assert c.attempted == []


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("program", (
    "function f {{ {name} -status; }}; f",
    "trap {name} EXIT; true",
    "trap '{name} -status' EXIT; true",
))
@pytest.mark.parametrize("entry", ("subprocess", "explicit_shell", "asyncio"))
async def test_deferred_shell_commands_record_without_executing_unmarked_backend(
    offline_boundary_tripwires, tmp_path, monkeypatch, name, program, entry,
):
    import asyncio

    c = offline_boundary_tripwires
    executable = tmp_path / name
    witness = tmp_path / "unmarked-executed"
    executable.write_text(f"#!/bin/sh\nprintf UNMARKED_EXECUTED\n: >'{witness}'\n")
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    script = program.format(name=name)
    shell = "/bin/bash" if program.startswith("function") else "/bin/sh"
    if entry == "subprocess":
        result = subprocess.run(
            script, shell=True, executable=shell, capture_output=True, check=False,
        )
        output = result.stdout
    elif entry == "explicit_shell":
        result = subprocess.run((shell, "-c", script), capture_output=True, check=False)
        output = result.stdout
    else:
        child = await asyncio.create_subprocess_shell(
            script, executable=shell, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await child.communicate()
    boundary = "backend" if name == "doubletake" else "ctl"
    assert c.attempted == [boundary]
    assert b"UNMARKED_EXECUTED" not in output
    assert not witness.exists()
    c.accept_shell_attempts([boundary])


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
@pytest.mark.parametrize("shell", ("/bin/sh", "/bin/bash"))
@pytest.mark.parametrize("entry", ("subprocess", "explicit_shell", "asyncio"))
async def test_getopts_cannot_retarget_a_marked_private_shim(
    offline_boundary_tripwires, tmp_path, monkeypatch, name, shell, entry,
):
    import asyncio

    c = offline_boundary_tripwires
    permitted = tmp_path / "permitted"
    permitted.mkdir()
    shim = permitted / name
    shim.write_text(
        f"#!/bin/sh\n# private test-only shim: {name}\n# fake_doubletake.py\nprintf private\n",
    )
    shim.chmod(0o700)
    alternate = tmp_path / "a"
    alternate.mkdir()
    witness = tmp_path / "unmarked-executed"
    unmarked = alternate / name
    unmarked.write_text(f"#!/bin/sh\nprintf UNMARKED_EXECUTED\n: >'{witness}'\n")
    unmarked.chmod(0o700)
    monkeypatch.setenv("PATH", str(permitted))
    script = f"getopts a PATH -a; {name} -status"
    if entry == "subprocess":
        result = subprocess.run(
            script, shell=True, executable=shell, cwd=tmp_path,
            capture_output=True, check=False,
        )
        output, error = result.stdout, result.stderr
    elif entry == "explicit_shell":
        result = subprocess.run(
            (shell, "-c", script), cwd=tmp_path, capture_output=True, check=False,
        )
        output, error = result.stdout, result.stderr
    else:
        child = await asyncio.create_subprocess_shell(
            script, executable=shell, cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        output, error = await child.communicate()
    assert b"UNMARKED_EXECUTED" not in output
    assert not witness.exists()
    assert b"PATH" in error and b"readonly" in error
    assert c.attempted == []


@pytest.mark.parametrize("name", ("doubletake", "doubletake-ctl"))
def test_unacknowledged_shell_attempt_fails_even_when_shell_returns_success(
    offline_boundary_tripwires, tmp_path, name,
):
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    (tmp_path / "conftest.py").write_bytes((root / "tests/conftest.py").read_bytes())
    test_file = tmp_path / "test_unacknowledged.py"
    test_file.write_text(
        "import subprocess\n"
        "def test_unacknowledged():\n"
        f"    result = subprocess.run('{name} -status; true', shell=True)\n"
        "    assert result.returncode == 0\n",
    )
    result = subprocess.run(
        (sys.executable, "-m", "pytest", str(test_file), "--confcutdir", str(tmp_path),
         "-o", f"pythonpath={root / 'src'}", "--basetemp", str(tmp_path / "nested"), "-q"),
        cwd=root, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "unmarked live boundary attempted through shell" in result.stdout
    assert "1 passed, 1 error" in result.stdout
    assert offline_boundary_tripwires.attempted == []


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
