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
