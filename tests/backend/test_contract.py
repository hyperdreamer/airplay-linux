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


@pytest.mark.parametrize(
    ("published_summary", "contradictory_summary"),
    [
        (
            "Mute mirrored audio (all streams, or only the given IP)",
            "Mute all streams; target is ignored",
        ),
        (
            "Unmute mirrored audio (all streams, or only the given IP)",
            "Unmute all streams; target is ignored",
        ),
    ],
)
def test_mute_scope_evidence_is_bound_to_each_operation_row(
    published_summary: str, contradictory_summary: str,
):
    """Disconnect's target wording cannot prove mute or unmute scope."""
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    mutated = ctl.replace(published_summary, contradictory_summary, 1)
    assert mutated != ctl

    features = features_from_help(sender, mutated)

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


def _streams(
    *, stdout: str = "", stderr: str = "", exit_code: int = 0,
) -> Ok:
    """Build a command result with an explicitly chosen stream per channel."""
    return Ok(value=CommandOutput(
        exit_code=exit_code, stdout=stdout.encode(), stderr=stderr.encode(),
    ))


# Go's flag package prints this before the usage block when a build has no
# ``--version`` flag, which is exactly what the pinned 0.5.0 binaries do.
_REJECTED_VERSION_FLAG = "flag provided but not defined: -version\n"


def _assert_pinned_public_surface(features: BackendFeatures) -> None:
    """Assert the recognized profile and the public capabilities it proves."""
    assert features.profile == "doubletake-0.5"
    assert features.version == "0.5.0"
    assert features.multi_target
    assert features.target_mute
    assert features.audio_probe
    # Public help proves nothing about application-private obligations.
    assert not features.foreground_control
    assert not features.owner_handshake
    assert not features.physical_source
    assert not features.virtual_source
    assert not features.audio_restart
    assert not features.target_bound_pairing
    assert not features.keyring
    assert not features.provisional_commit
    assert not features.cancel_pairing
    assert not features.revoke_pairing
    assert not features.query_modes


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
    _assert_pinned_public_surface(result.value)
    assert all(stdin is None for _, _, stdin in process.calls)
    assert all(spec.argv[-1] in {"--help", "--version", "-capabilities"} for spec, _, _ in process.calls)


def test_probe_recognizes_the_pinned_surface_when_version_is_rejected():
    """The observed 0.5.0 sequence must yield the profile, not merely ``Ok``.

    The installed binaries write usage to stderr and leave stdout empty; they
    have no ``--version`` flag, so that probe exits 2 with Go's
    ``flag provided but not defined: -version`` banner followed by the usage
    block.  A probe that splices that rejected banner in front of the help text
    destroys the fingerprint's first line and silently reports an unknown
    backend, so the recognized profile and public capabilities are asserted
    here rather than the presence of a result object.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    process = _ProbeProcess([
        _streams(stderr=sender),
        _streams(stderr=ctl, exit_code=2),
        _streams(stderr="flag provided but not defined: -capabilities\n", exit_code=2),
        _streams(stderr=_REJECTED_VERSION_FLAG + sender, exit_code=2),
    ])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Ok)
    _assert_pinned_public_surface(result.value)
    assert result.value.supports("multi_target")
    assert result.value.supports("target_mute")
    assert result.value.supports("audio_probe")
    assert not result.value.supports("owner_handshake")
    assert [spec.argv for spec, _, _ in process.calls] == [
        ("doubletake", "--help"),
        ("doubletake-ctl", "--help"),
        ("doubletake", "-capabilities"),
        ("doubletake", "--version"),
    ]


def test_a_declared_version_still_recognizes_the_surface_it_ships_with():
    """A build that does answer ``--version`` must not gain a fake fingerprint.

    Version evidence is optional and separate: when the banner names the pinned
    release the surface is still fingerprinted from the usage block alone, and
    the declared version is reported without promoting any private capability.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    process = _ProbeProcess([
        _streams(stderr=sender),
        _streams(stderr=ctl, exit_code=2),
        _streams(stdout="doubletake 0.5.0\n", exit_code=0),
    ])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Ok)
    _assert_pinned_public_surface(result.value)


def test_probe_rejects_a_successful_contradictory_version_for_the_pinned_surface():
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    process = _ProbeProcess([
        _streams(stderr=sender),
        _streams(stderr=ctl, exit_code=2),
        _streams(stderr="flag provided but not defined: -capabilities\n", exit_code=2),
        _streams(stdout="doubletake version 9.9.9\n", exit_code=0),
    ])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible


def test_a_usage_block_on_stderr_is_fingerprinted_despite_empty_stdout():
    """The blank-line hazard: an empty stdout must not precede the usage block.

    Go's flag package writes ``--help`` to stderr and nothing to stdout.  Joining
    the two streams puts an empty line before ``Usage of doubletake:``, so the
    first line stops being the usage header and the surface is never recognized.
    The same help text is therefore required to fingerprint identically whether
    it arrives on either stream, including when stdout carries a stray newline.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()

    def probe(sender_streams: dict, ctl_streams: dict) -> BackendFeatures:
        process = _ProbeProcess([
            _streams(**sender_streams),
            _streams(**ctl_streams, exit_code=2),
            _streams(stderr=_REJECTED_VERSION_FLAG + sender, exit_code=2),
        ])
        result = asyncio.run(probe_backend(process))
        assert isinstance(result, Ok), result
        return result.value

    on_stdout = probe({"stdout": sender}, {"stdout": ctl})
    on_stderr = probe({"stderr": sender}, {"stderr": ctl})
    blank_stdout = probe({"stdout": "\n", "stderr": sender}, {"stdout": "\n", "stderr": ctl})
    stray_stdout = probe(
        {"stdout": "deprecation: prefer -x11-window-id\n", "stderr": sender},
        {"stdout": "deprecation: prefer -x11-window-id\n", "stderr": ctl},
    )

    for features in (on_stdout, on_stderr, blank_stdout, stray_stdout):
        _assert_pinned_public_surface(features)


def test_version_evidence_is_reported_as_a_version_and_never_as_the_raw_banner():
    """A ``--version`` line is extracted, not spliced, and an error is not a version.

    ``--version`` output is optional evidence that arrives on its own stream.  It
    must be reduced to the version it declares: the banner text itself belongs
    nowhere in the decoded features, and Go's ``flag provided but not defined``
    rejection must contribute no version at all rather than being read as one.
    When the surface itself is recognized its own fingerprint names the version,
    so an unrecognized banner can never overwrite it.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    later_release = sender.replace(
        "  -bitrate int",
        "  -adaptive int\n    \tAdaptive mode\n  -bitrate int",
        1,
    )

    def probe(sender_help: str, version_stdout: str, version_stderr: str) -> BackendFeatures:
        process = _ProbeProcess([
            _streams(stderr=sender_help),
            _streams(stderr=ctl, exit_code=2),
            _streams(stderr="flag provided but not defined: -capabilities\n", exit_code=2),
            _streams(stdout=version_stdout, stderr=version_stderr),
        ])
        result = asyncio.run(probe_backend(process))
        assert isinstance(result, Ok), result
        return result.value

    declared = probe(later_release, "doubletake 9.9.9\n", "")
    on_stderr = probe(later_release, "", "doubletake 9.9.9\n")
    rejected = probe(later_release, "", _REJECTED_VERSION_FLAG)
    pinned_with_rejected = probe(sender, "", _REJECTED_VERSION_FLAG)

    assert declared.version == "9.9.9"
    assert on_stderr.version == "9.9.9"
    # Go's rejected-flag banner is not a version declaration.
    assert rejected.version == "unknown"
    assert pinned_with_rejected.version == "0.5.0"
    _assert_pinned_public_surface(pinned_with_rejected)
    for features in (declared, on_stderr, rejected):
        # Version evidence never promotes a capability on an unrecognized surface.
        assert features.profile == "doubletake-unknown"
        assert not features.multi_target
        assert not features.target_mute
        assert not features.audio_probe


def test_probe_still_fails_closed_on_an_unrecognized_help_surface():
    """Recognizing the pinned stderr capture must not weaken the fingerprint.

    A later release emits a perfectly valid Go usage block plus one new flag, and
    a foreign build emits prose that is not a Go usage block at all.  Both are
    served on the same stderr stream as the real capture and must still decode as
    the unknown profile with every capability off, including the public ones.
    """
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    later_release = sender.replace(
        "  -bitrate int",
        "  -adaptive int\n    \tAdaptive mode\n  -bitrate int",
        1,
    )
    assert later_release != sender

    for unknown_sender in (later_release, "Usage of doubletake:\n  not a flag row\n"):
        process = _ProbeProcess([
            _streams(stderr=unknown_sender),
            _streams(stderr=ctl, exit_code=2),
            _streams(stderr=_REJECTED_VERSION_FLAG + unknown_sender, exit_code=2),
        ])

        result = asyncio.run(probe_backend(process))

        assert isinstance(result, Ok), result
        assert result.value.profile == "doubletake-unknown"
        assert not result.value.multi_target
        assert not result.value.target_mute
        assert not result.value.audio_probe
        assert not result.value.supports("multi_target")
        assert not result.value.supports("audio_probe")


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


# --- the vendored source-reporting surface ---------------------------------------
#
# The vendored patch (docs/backend-fork.md) adds ``-capabilities`` and reports the
# captured display in ``status``.  Recognition must require BOTH the patched help
# surface AND the machine-readable advertisement, must never flip a different gate,
# and must fail closed when either is missing.

_SOURCE_SENDER = (FIXTURES / "doubletake-0.5-source-help.txt").read_text()
_CTL = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
_ADVERTISEMENT = frozenset({"capabilities-v1", "status-source"})


def test_patched_surface_with_advertisement_reports_source_reporting_only():
    features = features_from_help(_SOURCE_SENDER, _CTL, advertised=_ADVERTISEMENT)

    assert features.profile == "doubletake-0.5+source"
    assert features.version == "0.5.0"
    assert features.source_reporting is True
    # Reporting which display was captured proves nothing about ownership, source
    # typing, audio or the credential service; those stay closed.
    assert features.physical_source is False
    assert features.foreground_control is False
    assert features.owner_handshake is False
    assert features.keyring is False
    assert features.virtual_source is False
    # The pinned release's own proven behaviours are unaffected.
    assert features.multi_target is True
    assert features.target_mute is True
    assert features.audio_probe is True


def test_patched_surface_without_advertisement_fails_closed():
    """An extra flag is not an advertisement."""

    features = features_from_help(_SOURCE_SENDER, _CTL)

    assert features.profile == "doubletake-unknown"
    assert features.source_reporting is False
    assert features.multi_target is False


def test_patched_surface_with_partial_advertisement_fails_closed():
    features = features_from_help(_SOURCE_SENDER, _CTL, advertised=frozenset({"capabilities-v1"}))

    assert features.profile == "doubletake-unknown"
    assert features.source_reporting is False


def test_advertisement_cannot_promote_an_unpatched_surface():
    """Tokens alone prove nothing: the help surface must match a known build."""

    features = features_from_help(
        (FIXTURES / "doubletake-0.5-help.txt").read_text(), _CTL, advertised=_ADVERTISEMENT
    )

    assert features.profile == "doubletake-0.5"
    assert features.source_reporting is False
    assert features.multi_target is True


def test_probe_reads_the_patched_advertisement_end_to_end():
    patched = (FIXTURES / "doubletake-0.5-source-help.txt").read_text()
    process = _ProbeProcess([
        _streams(stderr=patched),
        _streams(stderr=_CTL, exit_code=2),
        _streams(stdout="capabilities-v1\nstatus-source\n"),
        _streams(stderr=_REJECTED_VERSION_FLAG + patched, exit_code=2),
    ])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Ok), result
    assert result.value.profile == "doubletake-0.5+source"
    assert result.value.source_reporting is True
    assert result.value.physical_source is False
    assert [spec.argv for spec, _, _ in process.calls] == [
        ("doubletake", "--help"),
        ("doubletake-ctl", "--help"),
        ("doubletake", "-capabilities"),
        ("doubletake", "--version"),
    ]


def test_probe_ignores_a_capability_banner_from_a_nonzero_exit():
    """A rejected flag prints a banner; that is not an advertisement."""

    patched = (FIXTURES / "doubletake-0.5-source-help.txt").read_text()
    process = _ProbeProcess([
        _streams(stderr=patched),
        _streams(stderr=_CTL, exit_code=2),
        _streams(stdout="capabilities-v1\nstatus-source\n", exit_code=2),
        _streams(stderr=_REJECTED_VERSION_FLAG + patched, exit_code=2),
    ])

    result = asyncio.run(probe_backend(process))

    assert isinstance(result, Ok), result
    assert result.value.profile == "doubletake-unknown"
    assert result.value.source_reporting is False
