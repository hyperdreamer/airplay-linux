"""Namespace isolation evidence must come from facts each side can obtain.

The witness runs as ``unshare -rn <python> -m airplay_linux.process.credential_probe``.
It cannot read its spawner's ``/proc/<pid>/ns/*`` links: entering a user
namespace puts the caller below that namespace for ptrace purposes, so the
kernel refuses the comparison with ``EACCES``. The spawner therefore records its
own namespace pins in the child's argv and the child compares them with its own.

These tests pin the decision table, the conservative decoding of the spawner's
evidence, both halves of the plumbing, and the fail-closed default when the
spawner evidence is missing or malformed.
"""

from __future__ import annotations

import json
import os

import pytest

from airplay_linux.domain.resources import Err, Ok
from airplay_linux.process import credential_probe as probe

USER = (4, 4026531837)
NET = (4, 4026531833)
OTHER = (4, 4026532000)


def test_isolation_requires_both_namespaces_to_differ() -> None:
    argv = probe._NAMESPACE_ARGV
    assert probe._isolation_proven(argv, 0, (USER, NET), (OTHER, (4, 4026531999))) is True
    # A private user namespace alone would still leave the witness on the real
    # network, which is exactly what this gate forbids.
    assert probe._isolation_proven(argv, 0, (USER, NET), (USER, OTHER)) is False
    assert probe._isolation_proven(argv, 0, (USER, NET), (OTHER, NET)) is False
    assert probe._isolation_proven(argv, 0, (USER, NET), (USER, NET)) is False


@pytest.mark.parametrize("arguments", [
    {"argv": ("unshare",)},
    {"argv": ("unshare", "-n")},
    {"argv": ("unshare", "-r")},
    {"argv": ["unshare", "-rn"]},
    {"uid": 1000},
    {"uid": True},
    {"uid": "0"},
    {"expected": None},
    {"observed": None},
    {"expected": (USER,)},
    {"observed": (OTHER,)},
    {"expected": (USER, NET, OTHER)},
    {"expected": (USER, [OTHER])},
    {"expected": (USER, (OTHER[0], "x"))},
    {"expected": (USER, (-1, NET[1]))},
    {"observed": (OTHER, (True, NET[1]))},
])
def test_isolation_refuses_unproven_shapes(arguments) -> None:
    """Missing, malformed, or wrongly typed evidence is never isolation."""
    values = {
        "argv": probe._NAMESPACE_ARGV,
        "uid": 0,
        "expected": (USER, NET),
        "observed": (OTHER, (4, 4026531999)),
    }
    values.update(arguments)
    assert probe._isolation_proven(
        values["argv"], values["uid"], values["expected"], values["observed"],
    ) is False


@pytest.mark.parametrize("raw,expected", [
    ("[[4, 11], [4, 12]]", ((4, 11), (4, 12))),
    (" [[4, 11], [4, 12]] ", ((4, 11), (4, 12))),
    (None, None),
    ("", None),
    ("not json", None),
    ("null", None),
    ("{}", None),
    ("[]", None),
    ("[[4, 11]]", None),
    ("[[4, 11], [4, 12], [4, 13]]", None),
    ("[[4, 11], 12]", None),
    ('[["4", 11], [4, 12]]', None),
    ("[[4, true], [4, 12]]", None),
    ("[[4, -1], [4, 12]]", None),
    ("[[4, 11, 0], [4, 12]]", None),
])
def test_spawner_namespace_pins_decode_conservatively(raw, expected) -> None:
    assert probe._parent_namespaces(raw) == expected


class _OsShim:
    """Replace only this module's ``os`` handle for one decision-table test."""

    def __init__(self, *, uid: int = 0) -> None:
        self.uid = uid

    def getuid(self) -> int:
        return self.uid

    def getpid(self) -> int:
        return os.getpid()


def test_namespace_identity_compares_own_pins_with_the_spawner(monkeypatch) -> None:
    observed = dict.fromkeys(probe._NAMESPACE_KINDS, OTHER)
    monkeypatch.setattr(probe, "os", _OsShim())
    monkeypatch.setattr(probe, "_namespace_pin", lambda name: observed[name])

    isolated = probe._namespace_identity(probe._NAMESPACE_ARGV, expected=(USER, NET))
    assert isinstance(isolated, Ok)
    assert isolated.value.pid > 0

    # The spawner's own pins are this process's pins: no isolation at all.
    matching = probe._namespace_identity(probe._NAMESPACE_ARGV, expected=(OTHER, OTHER))
    assert isinstance(matching, Err)
    assert matching.error.code.value == "dependency_incompatible"

    # No spawner evidence, no isolation claim.
    missing = probe._namespace_identity(probe._NAMESPACE_ARGV)
    assert isinstance(missing, Err)
    assert missing.error.code.value == "dependency_incompatible"

    # A non-root uid is not the ``-r`` mapping this evidence is about.
    monkeypatch.setattr(probe, "os", _OsShim(uid=1000))
    unmapped = probe._namespace_identity(probe._NAMESPACE_ARGV, expected=(USER, NET))
    assert isinstance(unmapped, Err)


def test_main_forwards_the_spawner_namespace_pins(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def refuse(argv, *, expected=None):
        seen["argv"], seen["expected"] = argv, expected
        return Err(error=probe.diagnostic(probe.ErrorCode.dependency_incompatible))

    monkeypatch.setattr(probe, "_namespace_identity", refuse)
    socket = tmp_path / "positive" / "daemon.sock"
    socket.parent.mkdir()
    root = tmp_path / "probe"
    root.mkdir()
    status = probe.main([
        "--socket", str(socket), "--record-root", str(root),
        "--parent-namespaces", json.dumps([[4, 11], [4, 12]]),
    ])
    assert status == 1
    assert seen == {"argv": probe._NAMESPACE_ARGV, "expected": ((4, 11), (4, 12))}


def test_main_refuses_when_the_spawner_evidence_is_absent(tmp_path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def observe(argv, *, expected=None):
        seen["expected"] = expected
        return Err(error=probe.diagnostic(probe.ErrorCode.dependency_incompatible))

    monkeypatch.setattr(probe, "_namespace_identity", observe)
    socket = tmp_path / "positive" / "daemon.sock"
    socket.parent.mkdir()
    root = tmp_path / "probe"
    root.mkdir()
    assert probe.main(["--socket", str(socket), "--record-root", str(root)]) == 1
    assert seen["expected"] is None


async def test_dispatch_records_the_spawner_namespace_pins(tmp_path, monkeypatch) -> None:
    """The spawner's own pins travel in the child's argv."""
    from airplay_linux.domain.ports import CommandOutput

    recorded: dict[str, object] = {}
    monkeypatch.setattr(probe, "_self_namespace_pins", lambda: [[4, 11], [4, 12]])

    class Capture:
        async def run(self, spec, timeout, stdin=None):
            recorded["argv"] = tuple(spec.argv)
            return Ok(value=CommandOutput(exit_code=1, stdout=b"", stderr=b""))

    instance = probe.CredentialServiceProbe(
        process=Capture(), record_root=tmp_path / "state", unshare="unshare",
    )
    result = await instance.check()
    assert isinstance(result, Err)
    argv = recorded["argv"]
    assert argv[argv.index("--parent-namespaces") + 1] == "[[4, 11], [4, 12]]"


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("0", 0),
    ("1000", 1000),
    ("4294967295", 4294967295),
    ("-1", None),
    ("4294967296", None),
    ("", None),
    ("text", None),
])
def test_spawner_uid_decodes_conservatively(raw, expected) -> None:
    assert probe._parent_uid(raw) == expected


def test_child_import_environment_names_the_running_package_root() -> None:
    """The bounded witness child must import the code its parent is running."""
    environment = probe._child_import_environment()
    assert set(environment) == {"PYTHONPATH"}
    assert os.path.isfile(os.path.join(
        environment["PYTHONPATH"], "airplay_linux", "__init__.py",
    ))
