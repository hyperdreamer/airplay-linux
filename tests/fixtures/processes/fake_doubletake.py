"""Private, test-only backend process fixtures (never an installed doubletake).

The adapter contract tests must exercise the real control/registry/runner code over
*real* child processes without ever starting the installed backend.  This module
provides that private world in two roles, both deliberately excluded from any
production import path:

``daemon`` (shim name ``doubletake``)
    A long-lived foreground process that binds an ``AF_UNIX`` socket inside the
    test's private runtime directory and serves newline-delimited JSON.  It accepts
    a supervisor config payload either on the inherited config file descriptor
    (``AIRPLAY_LINUX_CONFIG_FD``, the production supervisor channel) or from
    ``AIRPLAY_FAKE_CONFIG_FILE``.  Every request must carry the exact ``schema``,
    ``request_id``, ``owner_token`` and ``instance_uuid`` this instance published;
    refusals are recorded in a private journal.  It is *not* wire-compatible with
    doubletake and never says so.

``ctl`` (shim names ``doubletake-ctl`` / ``application-contract-v1``)
    One-shot client used by :class:`PublishedDoubletakeControl`.  It correlates the
    reply against its own request id and refuses anything else with a fixed exit
    code, so a stale/foreign reply can never become an answer.  Failure modes
    (malformed output, hang, crash, foreign owner token) are selectable per shim
    through environment variables baked into the shim.

The journal contains request metadata only -- never payload values, argv secrets
or environment values -- so tests can assert that a typed PIN never left the
application.

The module is importable (add ``tests/fixtures/processes`` to ``sys.path``) and is
also executed as a real process through the generated shims.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SCHEMA = 1

#: Present in every test-only command surface, and absent from the real one.
TEST_ONLY_SURFACE = "airplay-linux test-only application contract fixture"

#: Fixed exit code/stderr for a reply that does not correlate with our request.
EXIT_UNCORRELATED = 3
UNCORRELATED_STDERR = b"uncorrelated reply refused\n"

CONFIG_FD_ENV = "AIRPLAY_LINUX_CONFIG_FD"
CONFIG_FILE_ENV = "AIRPLAY_FAKE_CONFIG_FILE"
DAEMON_STALE_REPLY_ENV = "AIRPLAY_FAKE_DAEMON_STALE_REPLY"
DAEMON_STALE_REPLY_OP_ENV = "AIRPLAY_FAKE_DAEMON_STALE_REPLY_OP"
DAEMON_DENY_PAIRING_ENV = "AIRPLAY_FAKE_DAEMON_DENY_PAIRING"
DAEMON_FOREIGN_MUTE_ENV = "AIRPLAY_FAKE_DAEMON_FOREIGN_MUTE_TARGET"
CTL_MODE_ENV = "AIRPLAY_FAKE_CTL_MODE"
CTL_PID_FILE_ENV = "AIRPLAY_FAKE_CTL_PID_FILE"

JOURNAL_NAME = "fake-doubletake.jsonl"
TOKEN_NAME = "fake-owner-token"
INSTANCE_NAME = "fake-instance-uuid"
CONFIG_NAME = "fake-daemon-config.json"
SOCKET_NAME = "daemon.sock"

DAEMON_COMMANDS = ("doubletake",)
CTL_COMMANDS = ("doubletake-ctl", "application-contract-v1")

OPERATIONS = frozenset({
    "status", "discover", "devices", "connect", "disconnect", "mute", "unmute",
    "query_modes", "start_audio", "restart_audio", "begin_pairing",
    "submit_pairing", "cancel_pairing", "prepare_pairing", "commit_pairing",
    "mark_invalid", "revoke_pairing", "shutdown",
})


# ---------------------------------------------------------------------------
# Helpers shared by the tests
# ---------------------------------------------------------------------------


def owner_uuid() -> uuid.UUID:
    return uuid.uuid4()


def journal(path: str | os.PathLike[str]) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return []
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def daemon_journal_path(runtime: str | os.PathLike[str]) -> Path:
    return Path(runtime) / JOURNAL_NAME


def private_path(monkeypatch, directory: str | os.PathLike[str]) -> str:
    """Put the private fake commands first on PATH for spawned children."""
    updated = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", updated)
    return updated


def _default_scripts() -> dict[str, Path]:
    here = Path(__file__).resolve().parent
    return {
        "doubletake": here / "fake_doubletake.py",
        "doubletake-ctl": here / "fake_doubletake.py",
        "application-contract-v1": here / "fake_doubletake.py",
        "kscreen-doctor": here / "fake_kscreen.py",
        "krfb-virtualmonitor": here / "fake_virtualmonitor.py",
    }


def install(
    directory: str | os.PathLike[str],
    *,
    python: str | None = None,
    env: dict[str, dict[str, str]] | None = None,
    scripts: dict[str, Path] | None = None,
) -> dict[str, Path]:
    """Create private executable shims; returns command name -> shim path."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    interpreter = python or sys.executable
    mapping = dict(_default_scripts())
    if scripts:
        mapping.update({name: Path(path) for name, path in scripts.items()})
    environment = dict(env or {})
    installed: dict[str, Path] = {}
    for name, script in mapping.items():
        shim = target / name
        lines = [
            "#!/bin/sh",
            f"# private test-only shim: {name}",
        ]
        for key, value in sorted(environment.get(name, {}).items()):
            lines.append(f"export {key}={shlex.quote(str(value))}")
        lines.append(
            f"exec {shlex.quote(interpreter)} {shlex.quote(str(script))} "
            f"--shim-name {shlex.quote(name)} \"$@\""
        )
        shim.write_text("\n".join(lines) + "\n", encoding="utf-8")
        shim.chmod(0o700)
        installed[name] = shim
    return installed


def write_config(
    path: str | os.PathLike[str],
    *,
    owner_token: str,
    instance_uuid: str,
    socket_path: str,
    protocol_version: str = "v1",
) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "owner_token": owner_token,
        "instance_uuid": instance_uuid,
        "socket_path": socket_path,
        "protocol_version": protocol_version,
    }), encoding="utf-8")
    target.chmod(0o600)
    return target


def _start_ticks(pid: int | None = None) -> int:
    pid = os.getpid() if pid is None else pid
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    return int(raw.rsplit(") ", 1)[1].split()[19])


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode())
    finally:
        os.close(descriptor)


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


@dataclass
class FakeDaemon:
    runner: object
    socket_path: str
    owner_token: str
    instance_uuid: str
    identity: object
    journal_path: Path
    config_path: Path
    runtime: Path
    shims: dict[str, Path]

    @property
    def directory(self) -> Path:
        return Path(self.socket_path).parent


async def start_daemon(
    runner,
    *,
    shims: dict[str, Path],
    runtime: str | os.PathLike[str],
    owner_token: str | None = None,
    instance_uuid: str | None = None,
    protocol_version: str = "v1",
    timeout: float = 8.0,
) -> FakeDaemon:
    """Start the private daemon through the real runner and wait for its socket."""
    from airplay_linux.domain.ports import CommandSpec

    directory = Path(runtime)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = str(directory / SOCKET_NAME)
    token = owner_token or uuid.uuid4().hex
    instance = instance_uuid or str(uuid.uuid4())
    config_path = write_config(
        directory / CONFIG_NAME,
        owner_token=token,
        instance_uuid=instance,
        socket_path=socket_path,
        protocol_version=protocol_version,
    )
    spawned = await runner.spawn(CommandSpec(
        argv=(str(shims["doubletake"]), "-socket", socket_path, "-cred-backend", "keyring"),
        env={CONFIG_FILE_ENV: str(config_path)},
    ))
    from airplay_linux.domain.resources import Err

    if isinstance(spawned, Err):
        raise RuntimeError(f"private fake daemon could not start: {spawned.error.code}")
    daemon = FakeDaemon(
        runner=runner,
        socket_path=socket_path,
        owner_token=token,
        instance_uuid=instance,
        identity=spawned.value,
        journal_path=daemon_journal_path(directory),
        config_path=config_path,
        runtime=directory,
        shims=shims,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(socket_path).exists() and (directory / TOKEN_NAME).exists():
            return daemon
        await _sleep(0.02)
    await stop_daemon(runner, daemon)
    raise RuntimeError("private fake daemon did not publish its socket in time")


async def _sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)


async def stop_daemon(runner, daemon: FakeDaemon) -> None:
    await terminate(runner, daemon.identity)
    for name in (SOCKET_NAME, TOKEN_NAME, INSTANCE_NAME):
        try:
            (Path(daemon.runtime) / name).unlink()
        except FileNotFoundError:
            pass


async def terminate(runner, identity, *, timeout: float = 5.0) -> None:
    """Terminate exactly this private fixture identity, never a stranger."""
    from airplay_linux.domain.resources import Ok
    from airplay_linux.process.procfs import IdentityState, read_identity_evidence

    if read_identity_evidence(identity.pid).state in {
        IdentityState.absent, IdentityState.terminated,
    }:
        return
    if runner.identity(identity.pid) != identity:
        return
    try:
        os.kill(identity.pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if read_identity_evidence(identity.pid).state in {
            IdentityState.absent, IdentityState.terminated,
        }:
            return
        await _sleep(0.02)
    try:
        os.kill(identity.pid, signal.SIGKILL)
    except OSError:
        pass


def reap_private(needle: str) -> None:
    """Final safety net: kill only this run's processes (identified by path)."""
    if not needle:
        return
    marker = needle.encode()
    mine = os.getpid()
    uid = os.getuid()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == mine:
            continue
        root = Path("/proc") / entry
        try:
            if root.stat().st_uid != uid:
                continue
            command = (root / "cmdline").read_bytes()
        except OSError:
            continue
        if marker not in command:
            try:
                environment = (root / "environ").read_bytes()
            except OSError:
                environment = b""
            if marker not in environment:
                continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test-side client
# ---------------------------------------------------------------------------


async def request(
    daemon: FakeDaemon,
    operation: str,
    *,
    target: str | None = None,
    owner_token: str | None = None,
    instance_uuid: str | None = None,
    timeout: float = 5.0,
) -> dict:
    """Send one correlated request from the test process; return the reply body."""
    request_id = uuid.uuid4().hex
    payload = {
        "schema": SCHEMA,
        "request_id": request_id,
        "owner_token": owner_token or daemon.owner_token,
        "instance_uuid": instance_uuid or daemon.instance_uuid,
        "operation": operation,
        "target": target,
    }
    envelope = raw_envelope(daemon, payload, timeout=timeout)
    if envelope.get("request_id") != request_id:
        raise AssertionError("private daemon reply was not correlated")
    return envelope.get("body", {})


async def raw_request(daemon: FakeDaemon, payload: dict, *, timeout: float = 5.0) -> dict:
    return raw_envelope(daemon, payload, timeout=timeout).get("body", {})


def raw_envelope(daemon: FakeDaemon, payload: dict, *, timeout: float = 5.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(daemon.socket_path)
        client.sendall((json.dumps(payload) + "\n").encode())
        buffer = b""
        while b"\n" not in buffer:
            chunk = client.recv(65536)
            if not chunk:
                break
            buffer += chunk
    if not buffer.strip():
        raise AssertionError("private daemon closed the connection without a reply")
    return json.loads(buffer.split(b"\n", 1)[0])


async def run_ctl(
    runner,
    daemon: FakeDaemon,
    operation: str,
    *,
    shims: dict[str, Path] | None = None,
    target: str | None = None,
    timeout: float = 5.0,
):
    from airplay_linux.domain.ports import CommandSpec

    chosen = shims or daemon.shims

    argv = [str(chosen["doubletake-ctl"]), "-socket", daemon.socket_path, operation]
    if target is not None:
        argv.append(target)
    return await runner.run(CommandSpec(argv=tuple(argv), env={}), timeout)


# ---------------------------------------------------------------------------
# Daemon implementation
# ---------------------------------------------------------------------------


class _Daemon:
    def __init__(self, config: dict, socket_path: str) -> None:
        self.owner_token = str(config["owner_token"])
        self.instance_uuid = str(config["instance_uuid"])
        self.protocol_version = str(config.get("protocol_version", "v1"))
        self.socket_path = socket_path
        self.directory = Path(socket_path).parent
        self.journal = self.directory / JOURNAL_NAME
        self.pid = os.getpid()
        self.start_ticks = _start_ticks()
        self.streams: dict[str, dict] = {}
        self.pending: set[str] = set()
        self.stopping = False
        self.stale_reply_sent = False
        self.foreign_mute_target = os.environ.get(DAEMON_FOREIGN_MUTE_ENV)

    # -- reply construction ------------------------------------------------

    def _status(self) -> dict:
        waiting = [
            self._entry(target, "pin_required") for target in sorted(self.pending)
        ]
        streaming = [
            self._entry(target, "streaming") for target in sorted(self.streams)
        ]
        credential = None
        if waiting:
            state = "pin_required"
            device, device_ip = waiting[0]["device"], waiting[0]["device_ip"]
            credential = "pin"
        elif streaming:
            state = "streaming"
            device, device_ip = streaming[0]["device"], streaming[0]["device_ip"]
        else:
            state = "idle"
            device = device_ip = None
        has_audio = streaming[0]["has_audio"] if streaming else False
        audio_muted = streaming[0]["audio_muted"] if streaming else False
        reply = {
            "ok": True,
            "state": state,
            "device": device,
            "device_ip": device_ip,
            "has_audio": has_audio,
            "audio_muted": audio_muted,
            "owner_token": self.owner_token,
            "instance_uuid": self.instance_uuid,
            "socket_path": self.socket_path,
            "protocol_version": self.protocol_version,
            "daemon_pid": self.pid,
            "daemon_start_ticks": self.start_ticks,
            "streams": streaming + waiting,
        }
        if credential is not None:
            reply["needs_pin"] = True
            reply["needs_credential"] = True
            reply["credential_kind"] = credential
        return reply

    def _entry(self, target: str, state: str) -> dict:
        entry = {
            "device": "Fake receiver",
            "device_ip": self.foreign_mute_target or target,
            "state": state,
            "has_audio": False,
            "audio_muted": False,
        }
        if state == "streaming":
            stream = self.streams.get(target, {})
            entry["has_audio"] = bool(stream.get("has_audio", True))
            entry["audio_muted"] = bool(stream.get("audio_muted", False))
        else:
            entry["credential_kind"] = "pin"
        return entry

    def _ack(self, state: str = "idle") -> dict:
        return {"ok": True, "state": state}

    def _reject(self, code: str) -> dict:
        return {"ok": False, "code": code}

    def _handle(self, request: object) -> tuple[dict, bool]:
        if not isinstance(request, dict):
            return self._reject("invalid_request"), False
        operation = request.get("operation")
        request_id = request.get("request_id")
        target = request.get("target")
        if request.get("schema") != SCHEMA:
            return self._reject("schema_unsupported"), False
        if not isinstance(request_id, str) or not request_id:
            return self._reject("correlation_required"), False
        if operation not in OPERATIONS:
            return self._reject("operation_unsupported"), False
        if (
            request.get("owner_token") != self.owner_token
            or request.get("instance_uuid") != self.instance_uuid
        ):
            return self._reject("ownership_mismatch"), False
        if target is not None and type(target) is not str:
            return self._reject("invalid_request"), False

        if operation == "status":
            return self._status(), False
        if operation in {"discover", "devices"}:
            return {"ok": True, "devices": []}, False
        if operation == "connect":
            if target is None:
                return self._reject("invalid_request"), False
            self.streams[target] = {"has_audio": True, "audio_muted": False}
            self.pending.discard(target)
            return self._ack("streaming"), False
        if operation == "disconnect":
            self.streams.pop(target, None)
            self.pending.discard(target)
            return self._ack("idle"), False
        if operation in {"mute", "unmute"}:
            if target not in self.streams:
                return self._reject("receiver_rejected"), False
            self.streams[target]["audio_muted"] = operation == "mute"
            return self._status(), False
        if operation in {"start_audio", "restart_audio"}:
            if target in self.streams:
                self.streams[target]["has_audio"] = True
            return self._ack("streaming"), False
        if operation == "begin_pairing":
            if os.environ.get(DAEMON_DENY_PAIRING_ENV) == "1":
                return {"ok": False, "state": "idle"}, False
            if target is None:
                return self._reject("invalid_request"), False
            self.pending.add(target)
            return self._ack("pin_required"), False
        if operation in {"submit_pairing", "prepare_pairing", "commit_pairing"}:
            return self._ack("pin_required" if target in self.pending else "idle"), False
        if operation in {"cancel_pairing", "revoke_pairing", "mark_invalid"}:
            self.pending.discard(target)
            if operation in {"revoke_pairing", "mark_invalid"}:
                self.streams.pop(target, None)
            return self._ack("idle"), False
        if operation == "query_modes":
            return self._ack("streaming"), False
        if operation == "shutdown":
            self.stopping = True
            return self._ack("idle"), True
        return self._reject("operation_unsupported"), False

    # -- serving -----------------------------------------------------------

    def serve(self) -> int:
        _write_private(self.directory / TOKEN_NAME, self.owner_token)
        _write_private(self.directory / INSTANCE_NAME, self.instance_uuid)
        _append(self.journal, {
            "process": "daemon", "outcome": "start", "pid": self.pid,
            "instance_uuid": self.instance_uuid, "protocol_version": self.protocol_version,
        })
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(self.socket_path)
            server.listen(16)
            server.settimeout(0.05)
            while not self.stopping:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                with connection:
                    self._serve_connection(connection)
        finally:
            server.close()
            try:
                Path(self.socket_path).unlink()
            except FileNotFoundError:
                pass
            _append(self.journal, {
                "process": "daemon", "outcome": "stop", "pid": self.pid,
            })
        return 0

    def _serve_connection(self, connection: socket.socket) -> None:
        connection.settimeout(10.0)
        buffer = b""
        while b"\n" not in buffer:
            try:
                chunk = connection.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
        line = buffer.split(b"\n", 1)[0]
        try:
            request = json.loads(line)
        except ValueError:
            _append(self.journal, {
                "process": "daemon", "outcome": "refused", "reason": "invalid_request",
            })
            self._reply(connection, None, self._reject("invalid_request"))
            return
        request_id = request.get("request_id") if isinstance(request, dict) else None
        body, exit_after = self._handle(request)
        correlated = body.get("ok") is True or body.get("code") not in {
            "correlation_required", "schema_unsupported",
        }
        if (
            os.environ.get(DAEMON_STALE_REPLY_ENV) == "1"
            and not self.stale_reply_sent
            and os.environ.get(DAEMON_STALE_REPLY_OP_ENV) in (None, "", request.get("operation"))
            and isinstance(request_id, str)
            and request_id
        ):
            self.stale_reply_sent = True
            correlated = False
            _append(self.journal, {
                "process": "daemon", "outcome": "served",
                "operation": request.get("operation"), "target": request.get("target"),
                "request_id": request_id, "correlated": False,
            })
            self._reply(connection, "00000000-0000-4000-8000-000000000000", body)
            return
        if body.get("ok") is False:
            _append(self.journal, {
                "process": "daemon", "outcome": "refused",
                "reason": body.get("code"), "operation": request.get("operation"),
                "target": request.get("target"), "request_id": request_id,
                "correlated": True,
            })
        else:
            _append(self.journal, {
                "process": "daemon", "outcome": "served",
                "operation": request.get("operation"), "target": request.get("target"),
                "cleanup": request.get("operation") == "disconnect",
                "request_id": request_id, "correlated": True,
            })
        self._reply(connection, request_id, body)

    @staticmethod
    def _reply(connection: socket.socket, request_id: object, body: dict) -> None:
        envelope = {"schema": SCHEMA, "request_id": request_id, "body": body}
        try:
            connection.sendall((json.dumps(envelope, sort_keys=True) + "\n").encode())
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ctl implementation
# ---------------------------------------------------------------------------


def _ctl(argv: list[str]) -> int:
    journal_path = None
    mode = os.environ.get(CTL_MODE_ENV, "normal")
    operation = None
    request_id = uuid.uuid4().hex
    try:
        if "--help" in argv or "-h" in argv:
            sys.stdout.write(
                f"{TEST_ONLY_SURFACE}\n"
                "This private executable speaks a test-only newline-delimited JSON "
                "protocol; it is not a backend command.\n"
            )
            return 0
        if "--version" in argv:
            sys.stdout.write(f"{TEST_ONLY_SURFACE} protocol-v1\n")
            return 0
        if "-socket" in argv:
            index = argv.index("-socket")
            socket_path = argv[index + 1]
            journal_path = daemon_journal_path(Path(socket_path).parent)
            rest = argv[index + 2:]
            if rest:
                operation = rest[0]
                target = rest[1] if len(rest) > 1 else None
            else:
                operation, target = None, None
        else:
            return 64
        _append(journal_path, {
            "process": "ctl", "outcome": "start", "operation": operation,
            "request_id": request_id, "argv": argv, "env_keys": sorted(os.environ),
        })
        if mode == "crash":
            _append(journal_path, {
                "process": "ctl", "outcome": "crash", "operation": operation,
                "request_id": request_id,
            })
            return 7
        if mode == "malformed":
            sys.stdout.write("not-json")
            _append(journal_path, {
                "process": "ctl", "outcome": "malformed", "operation": operation,
                "request_id": request_id,
            })
            return 0
        if mode == "timeout":
            pid_file = os.environ.get(CTL_PID_FILE_ENV)
            if pid_file:
                Path(pid_file).write_text(str(os.getpid()), encoding="utf-8")
            _append(journal_path, {
                "process": "ctl", "outcome": "hung", "operation": operation,
                "request_id": request_id,
            })
            time.sleep(3600)
            return 0

        token = (Path(socket_path).parent / TOKEN_NAME).read_text(encoding="utf-8")
        instance = (Path(socket_path).parent / INSTANCE_NAME).read_text(encoding="utf-8")
        if mode == "wrong-owner":
            token = uuid.uuid4().hex
        payload = {
            "schema": SCHEMA,
            "request_id": request_id,
            "owner_token": token,
            "instance_uuid": instance,
            "operation": operation,
            "target": target,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(socket_path)
            client.sendall((json.dumps(payload) + "\n").encode())
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
        if not buffer.strip():
            _append(journal_path, {
                "process": "ctl", "outcome": "unavailable", "operation": operation,
                "request_id": request_id,
            })
            print("private fake daemon closed the connection", file=sys.stderr)
            return 4
        envelope = json.loads(buffer.split(b"\n", 1)[0])
        if envelope.get("request_id") != request_id:
            _append(journal_path, {
                "process": "ctl", "outcome": "uncorrelated", "operation": operation,
                "request_id": request_id,
            })
            sys.stderr.buffer.write(UNCORRELATED_STDERR)
            sys.stderr.flush()
            return EXIT_UNCORRELATED
        body = envelope.get("body", {})
        sys.stdout.write(json.dumps(body, sort_keys=True))
        _append(journal_path, {
            "process": "ctl",
            "outcome": "ok" if body.get("ok") is True else "rejected",
            "operation": operation, "request_id": request_id,
        })
        return 0 if body.get("ok") is True else 1
    except (OSError, ValueError, KeyError) as error:
        if journal_path is not None:
            _append(journal_path, {
                "process": "ctl", "outcome": "unavailable", "operation": operation,
                "request_id": request_id,
            })
        print(f"private fake ctl unavailable: {type(error).__name__}", file=sys.stderr)
        return 4


def _read_config() -> dict:
    descriptor = os.environ.get(CONFIG_FD_ENV)
    if descriptor:
        raw = os.read(int(descriptor), 65536)
        return json.loads(raw.decode("utf-8"))
    path = os.environ.get(CONFIG_FILE_ENV)
    if not path:
        raise SystemExit("private test fixture: no config channel")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    shim = "doubletake"
    if arguments[:1] == ["--shim-name"]:
        shim = arguments[1]
        arguments = arguments[2:]
    if shim in CTL_COMMANDS:
        return _ctl(arguments)
    if shim in DAEMON_COMMANDS:
        config = _read_config()
        socket_path = str(config.get("socket_path") or "")
        if "-socket" in arguments:
            socket_path = arguments[arguments.index("-socket") + 1]
        if not socket_path:
            return 64
        daemon = _Daemon(config, socket_path)

        def stop(signum, frame):  # noqa: ARG001 - signal handler signature
            daemon.stopping = True

        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, stop)
        return daemon.serve()
    return 64


if __name__ == "__main__":  # pragma: no cover - exercised as a real process
    raise SystemExit(main())
