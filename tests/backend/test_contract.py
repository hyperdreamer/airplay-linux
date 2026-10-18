"""Contract negotiation tests for the published doubletake surface."""

from __future__ import annotations

import asyncio
import json
import re
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


def test_status_fixtures_are_exact_published_response_objects():
    """A conformance fixture must be a response the pinned daemon can emit.

    ``internal/daemon/daemon.go`` defines the response members; provenance lives
    in this repository's documentation, not inside the captured body, because a
    response object with invented top-level members is not source-conformant.
    """
    published = {
        "ok", "state", "device", "device_ip", "has_audio", "audio_muted",
        "needs_pin", "needs_credential", "credential_kind", "error",
        "devices", "streams",
    }
    published_stream = {
        "device", "device_ip", "state", "has_audio", "audio_muted",
        "credential_kind",
    }

    for name in ("status-0.5.json", "status-0.5-password.json"):
        payload = json.loads((FIXTURES / name).read_text())
        assert set(payload) <= published, f"{name} has non-response members"
        for stream in payload["streams"]:
            assert set(stream) <= published_stream, f"{name} has a non-StreamInfo member"
        assert not isinstance(decode_status(payload), Err), f"{name} must decode"


def test_status_fixtures_obey_the_published_projection_rules():
    """Reproduce ``statusResponseLocked`` and require each fixture to match it.

    Streams are sorted by device IP; the legacy device and audio fields come from
    the first *streaming* entry; the device and credential fields are then
    overwritten from the first *pin_required* entry, and ``needs_pin`` /
    ``needs_credential`` / ``credential_kind`` derive from that waiting entry
    alone.  A fixture that no build of the pinned daemon can produce certifies
    an impossible response.
    """
    for name in ("status-0.5.json", "status-0.5-password.json"):
        payload = json.loads((FIXTURES / name).read_text())
        streams = sorted(payload["streams"], key=lambda stream: stream["device_ip"])

        state = "idle"
        if any(stream["state"] == "pin_required" for stream in streams):
            state = "pin_required"
        elif any(stream["state"] == "streaming" for stream in streams):
            state = "streaming"
        elif any(stream["state"] == "connecting" for stream in streams):
            state = "connecting"

        device = device_ip = credential_kind = None
        has_audio = audio_muted = False
        for stream in streams:
            if stream["state"] == "streaming":
                device, device_ip = stream["device"], stream["device_ip"]
                has_audio, audio_muted = stream["has_audio"], stream["audio_muted"]
                break
        for stream in streams:
            if stream["state"] == "pin_required":
                device, device_ip = stream["device"], stream["device_ip"]
                credential_kind = stream.get("credential_kind") or "pin"
                break

        assert payload["state"] == state, name
        assert payload.get("device") == device, name
        assert payload.get("device_ip") == device_ip, name
        assert payload["has_audio"] == has_audio, name
        assert payload["audio_muted"] == audio_muted, name
        assert payload.get("credential_kind") == credential_kind, name
        assert payload.get("needs_credential", False) is (credential_kind is not None), name
        assert payload.get("needs_pin", False) is (credential_kind == "pin"), name
        # Only a waiting stream may carry a credential kind (omitempty).
        for stream in streams:
            if stream["state"] != "pin_required":
                assert "credential_kind" not in stream, name


def test_published_help_fixtures_are_verbatim_help_captures():
    """The committed help fixtures must be what the pinned release prints.

    The pinned 0.5.0 binaries print no version banner and have no ``--version``
    flag: ``doubletake --help`` is Go's flag package usage block and
    ``doubletake-ctl --help`` is a hand-written usage string.  A fixture holding
    a ``doubletake 0.5.0`` line is therefore not a capture, and a decoder that
    requires such a banner can never verify the very release it pins.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()

    assert sender.startswith("Usage of "), sender.splitlines()[:1]
    assert ctl.startswith("Usage: doubletake-ctl "), ctl.splitlines()[:1]
    for text in (sender, ctl):
        assert not re.search(r"(?im)^\s*doubletake(?:-ctl)?\s+v?\d", text), (
            "the pinned release prints no version banner"
        )
    # Go's flag package indents each description with a single tab.
    assert any(line.startswith("    \t") for line in sender.splitlines())


def test_captured_help_proves_the_documented_public_surface():
    """The release's own usage text proves its documented control surface.

    Capability proof must rest on affirmative declarations in the pinned
    release's output, not on a version banner it never prints.  Everything the
    application needs privately (ownership, source typing, trust transactions)
    stays false regardless, because public help cannot prove it.
    """
    features = features_from_help(
        (FIXTURES / "doubletake-0.5-help.txt").read_text(),
        (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text(),
    )

    assert features.multi_target
    assert features.target_mute
    assert features.audio_probe
    assert not features.foreground_control
    assert not features.owner_handshake
    assert not features.physical_source
    assert not features.virtual_source
    assert not features.audio_restart
    assert not features.keyring
    assert not features.provisional_commit
    assert not features.cancel_pairing
    assert not features.revoke_pairing
    assert not features.query_modes


def test_a_merely_mentioned_or_negated_contract_proves_nothing():
    """Only an affirmative usage-table row proves a command."""
    sender = "Usage of doubletake:\n  -socket string\n    \tUnix socket path\n"
    ctl = (
        "Usage: doubletake-ctl [-socket path] <command> [args]\n\n"
        "Unsupported commands:\n"
        "  status\n  discover\n  devices\n  connect [target]\n"
        "  disconnect [target]\n  mute [target]\n  unmute [target]\n"
        "  all streams, or only the given IP\n"
        "  -socket path\n"
    )

    features = features_from_help(sender, ctl)

    assert not features.multi_target
    assert not features.target_mute
    assert not features.audio_probe
    assert features.profile == "doubletake-unknown"


def test_a_cleanly_parsed_but_different_flag_set_is_not_the_pinned_release():
    """Parsing successfully is not proof: the surface must match exactly.

    A later release that adds one flag still emits a perfectly valid Go usage
    block, so a decoder that gates on parseability alone would verify it and
    enable capabilities the pinned release never proved.  Only equality with
    the transcribed surface is evidence.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    later_release = sender.replace(
        "  -bitrate int",
        "  -adaptive int\n    \tAdaptive mode\n  -bitrate int",
        1,
    )
    assert later_release != sender

    features = features_from_help(later_release, ctl)

    assert features.profile == "doubletake-unknown"
    assert not features.multi_target
    assert not features.target_mute
    assert not features.audio_probe


def test_an_unparseable_row_inside_the_commands_table_unverifies_the_table():
    """A partially readable table is not a proof of the table's contents.

    Skipping a row that does not fit the usage grammar would let a truncated,
    mangled, or hand-edited capture pass as the pinned surface merely because
    the remaining rows happen to be familiar.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    mangled = ctl.replace(
        "  devices                             List cached discovered devices\n",
        "  devices                             List cached discovered devices\n"
        "  frobnicate\n",
        1,
    )
    assert mangled != ctl

    features = features_from_help(sender, mangled)

    assert features.profile == "doubletake-unknown"
    assert not features.multi_target
    assert not features.target_mute
    assert not features.audio_probe


def test_target_scoped_rows_without_the_scope_statement_do_not_prove_scope():
    """A ``[target]`` argument alone does not prove other streams are untouched.

    The published usage states that these commands address only the given IP.
    Without that statement the same rows could mean "target, or every stream",
    so per-target mute and concurrent multi-target control stay unproven even
    though the rest of the surface matches the pinned release exactly.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    unscoped = ctl.replace("all streams, or only the given IP", "a stream")
    assert unscoped != ctl
    assert "only the given IP" not in unscoped

    features = features_from_help(sender, unscoped)

    # The surface itself is still recognised: only the scope claim is withdrawn.
    assert features.profile == "doubletake-0.5"
    assert features.audio_probe
    assert not features.multi_target
    assert not features.target_mute



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
