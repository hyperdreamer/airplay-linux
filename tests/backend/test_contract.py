"""Contract negotiation tests for the published doubletake surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from airplay_linux.backend.contract import (
    BackendFeatures,
    decode_status,
    features_from_help,
    probe_backend,
)
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.domain.resources import Err, ErrorCode, Ok


FIXTURES = Path(__file__).parents[1] / "fixtures" / "backend"


def test_legacy_help_does_not_prove_safe_sources_or_pairing():
    features = features_from_help(
        "-socket path\n-cred-backend string\n-daemonize",
        "connect [target] [PIN-or-password]\npin <PIN-or-password>",
    )
    assert not features.physical_source
    assert not features.provisional_commit
    assert not features.owner_handshake
    assert not features.target_bound_pairing
    assert not features.keyring


def test_published_help_keeps_documented_target_controls_but_fails_closed_on_private_obligations():
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()

    features = features_from_help(sender, ctl)

    assert features.version == "0.5.0"
    assert features.multi_target
    assert features.target_mute
    assert not features.foreground_control
    assert not features.owner_handshake
    assert not features.physical_source
    assert not features.virtual_source
    assert not features.audio_restart
    assert not features.provisional_commit
    assert not features.cancel_pairing
    assert not features.revoke_pairing
    assert not features.query_modes


def test_status_fixture_decodes_only_typed_public_fields():
    status = json.loads((FIXTURES / "status-0.5.json").read_text())
    decoded = decode_status(status)

    assert decoded.state == "pin_required"
    assert decoded.needs_credential
    assert decoded.credential_kind == "password"
    assert len(decoded.streams) == 2
    assert decoded.streams[0].target == "192.0.2.10"
    assert decoded.streams[0].credential_kind == "pin"
    assert decoded.error is None


def test_old_status_format_is_incompatible():
    result = decode_status({"ok": True, "state": "streaming", "device_ip": "192.0.2.10"})
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible


def test_malformed_status_is_incompatible_without_echoing_backend_text():
    result = decode_status(b'{"streams": [')
    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
    assert "streams" not in result.error.message


class _ProbeProcess:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[CommandSpec, float, bytes | None]] = []

    async def run(self, spec, timeout, stdin=None):
        self.calls.append((spec, timeout, stdin))
        return self.outcomes.pop(0)


def _output(text: str, *, exit_code: int = 0) -> Ok:
    return Ok(value=CommandOutput(exit_code=exit_code, stdout=text.encode(), stderr=b""))


def test_probe_is_read_only_and_does_not_send_stdin():
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    process = _ProbeProcess([_output(sender), _output(ctl, exit_code=2), _output("", exit_code=2)])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Ok)
    assert all(stdin is None for _, _, stdin in process.calls)
    assert all(spec.argv[-1] in {"--help", "--version"} for spec, _, _ in process.calls)


def test_probe_reports_missing_tool_as_a_fixed_dependency_diagnostic():
    missing = Err(error=type("D", (), {"code": ErrorCode.dependency_missing})())
    process = _ProbeProcess([missing])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_missing
    assert "doubletake" in result.error.message


def test_probe_reports_empty_help_as_incompatible():
    process = _ProbeProcess([_output(""), _output(""), _output("", exit_code=2)])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible
