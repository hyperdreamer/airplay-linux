"""Contract negotiation tests for the published doubletake surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from airplay_linux.backend.contract import (
    BackendFeatures,
    DevicesReply,
    decode_devices,
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
    assert decoded.device == "Bedroom"
    assert decoded.target == "192.0.2.10"
    assert decoded.needs_credential
    assert decoded.needs_pin
    assert decoded.credential_kind == "pin"
    # Legacy device/target fields select the waiting stream, while aggregate
    # audio fields remain from the first streaming stream.
    assert decoded.has_audio is True
    assert decoded.audio_muted is True
    assert len(decoded.streams) == 2
    assert decoded.streams[0].target == "192.0.2.10"
    assert decoded.streams[0].credential_kind == "pin"
    assert decoded.streams[1].target == "192.0.2.11"
    assert decoded.streams[1].audio_muted is True
    assert decoded.error is None


def test_source_conformant_password_status_uses_the_waiting_stream():
    status = json.loads((FIXTURES / "status-0.5-password.json").read_text())
    decoded = decode_status(status)

    assert decoded.state == "pin_required"
    assert decoded.device == "Office"
    assert decoded.target == "192.0.2.12"
    assert decoded.needs_credential
    assert not decoded.needs_pin
    assert decoded.credential_kind == "password"
    assert decoded.has_audio is True
    assert decoded.audio_muted is False
    assert decoded.streams[0].credential_kind == "password"


def test_omitted_devices_is_the_published_empty_success_response():
    decoded = decode_devices({"ok": True, "state": "idle"})

    assert isinstance(decoded, DevicesReply)
    assert decoded.devices == ()


def test_explicitly_malformed_devices_is_not_treated_as_empty():
    result = decode_devices({"ok": True, "state": "idle", "devices": None})

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible


def test_credentials_are_only_valid_on_waiting_streams():
    result = decode_status({
        "ok": True,
        "state": "streaming",
        "has_audio": True,
        "audio_muted": False,
        "streams": [{
            "device": "Living Room",
            "device_ip": "192.0.2.10",
            "state": "streaming",
            "has_audio": True,
            "audio_muted": False,
            "credential_kind": "password",
        }],
    })

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible


def test_help_negations_and_unknown_versions_never_enable_capabilities():
    features = features_from_help(
        "doubletake 99.1.0\\nUnsupported: foreground owner-token instance-uuid handshake "
        "physical source connector verify keyring transaction provisional commit pairing "
        "cancel-pairing revoke-pairing query-modes",
        "Unsupported: status discover devices connect disconnect [target] mute [target] "
        "unmute [target] target-bound pairing credential",
    )

    assert features.version == "99.1.0"
    assert not features.foreground_control
    assert not features.owner_handshake
    assert not features.multi_target
    assert not features.target_mute
    assert not features.physical_source
    assert not features.virtual_source
    assert not features.keyring
    assert not features.provisional_commit
    assert not features.cancel_pairing
    assert not features.revoke_pairing
    assert not features.query_modes


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


def test_compatibility_doc_pins_the_peeled_source_and_real_client_path():
    document = (Path(__file__).parents[2] / "docs" / "backend-compatibility.md").read_text()

    assert "e18f85d07f74a3b9d49f448762d28f3f7feb3938" in document
    assert "internal/daemon/daemon.go" in document
    assert "internal/daemon/daemonclient/client.go" in document
    assert "blob/c896e3689ab85c3ed520e502308dedfbb5a4c221/" not in document


def test_application_contract_fixture_is_explicitly_non_live_and_has_conformance_messages():
    contract = json.loads((FIXTURES / "contract-v1.json").read_text())

    assert contract["provenance"] == "application-contract-fixture"
    assert contract["live_support_proven"] is False
    assert contract["protocol_version"] == "v1"
    assert contract["published_source_commit"] == "e18f85d07f74a3b9d49f448762d28f3f7feb3938"
    assert contract["requests"]
    assert contract["replies"]
    assert contract["notices"]
    request_operations = {item["operation"] for item in contract["requests"]}
    assert {item["operation"] for item in contract["replies"]} == request_operations
    assert {item["kind"] for item in contract["notices"]} == {"status", "stream", "pairing"}
    assert request_operations >= {
        "status", "discover", "devices", "connect", "disconnect", "mute", "unmute", "query_modes",
        "start_audio", "restart_audio", "begin_pairing", "submit_pairing", "cancel_pairing",
        "prepare_pairing", "commit_pairing", "revoke_pairing", "mark_invalid",
        "shutdown",
    }
    assert all("secret" not in json.dumps(item).lower() for item in contract["notices"])


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
