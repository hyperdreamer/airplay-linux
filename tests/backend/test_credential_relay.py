"""Relay capability, authorization, command construction and metadata boundaries.

Relay is the one public credential-bearing mapping: an owned mirror start answers a
receiver's own PIN/password request with a target-bound ``connect T credential``.
These tests pin the proved capability table, the single authorization branch, the
exact targeted argv and the redaction metadata that must travel with it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from airplay_linux.backend.contract import BackendFeatures, features_from_help
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    CredentialPayload,
    EmptyPayload,
    ProfileCommand,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
)
from airplay_linux.domain.operations import SensitiveText
from airplay_linux.domain.ports import CommandOutput
from airplay_linux.domain.resources import AudioPolicy, Err, ErrorCode, Ok, VolumeLevel
from airplay_linux.testing.builders import target


FIXTURES = Path(__file__).parents[1] / "fixtures" / "backend"

_LEGACY_CONNECT_ROW = (
    "  connect [target] [PIN-or-password]  "
    "Start mirroring (to target IP, or first free device)"
)
_EXTENDED_CONNECT_ROWS = (
    "  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  "
    "Start mirroring (to target IP, or first free device)\n"
    "  volume <0-100> [target]             Set mirrored audio level "
    "(all streams, or only the given IP)\n"
    "  audio off [target]                  Close mirrored audio "
    "(all streams, or only the given IP)"
)
_EXTENDED_TOKENS = frozenset(
    {"capabilities-v1", "status-source", "status-source-complete", "target-volume"}
)
_PIN_ROW = "  pin <PIN-or-password>               Submit pairing credentials for a waiting device"
_TRANSACTION_FLAGS = (
    "target_bound_pairing", "provisional_commit", "cancel_pairing", "revoke_pairing",
)


@pytest.fixture
def relay_help_tables():
    """The pinned captures plus the extended volume table used by test_contract."""
    sender = (FIXTURES / "doubletake-0.5-help.txt").read_text()
    source_sender = (FIXTURES / "doubletake-0.5-source-help.txt").read_text()
    legacy_ctl = (FIXTURES / "doubletake-ctl-0.5-help.txt").read_text()
    assert legacy_ctl.count(_LEGACY_CONNECT_ROW) == 1
    assert legacy_ctl.count(_PIN_ROW) == 1
    return SimpleNamespace(
        sender=sender,
        source_sender=source_sender,
        legacy_ctl=legacy_ctl,
        extended_ctl=legacy_ctl.replace(_LEGACY_CONNECT_ROW, _EXTENDED_CONNECT_ROWS),
        extended_tokens=_EXTENDED_TOKENS,
    )


def _surface(tables, case):
    """Return the (sender, ctl, advertised) evidence for one capability case."""
    if case == "legacy":
        return tables.sender, tables.legacy_ctl, frozenset()
    if case == "extended":
        return tables.source_sender, tables.extended_ctl, tables.extended_tokens
    if case == "missing_pin":
        return (
            tables.sender,
            tables.legacy_ctl.replace(_PIN_ROW + "\n", ""),
            frozenset(),
        )
    if case == "added_row":
        extra = "  forget [target]                    Forget stored credentials for the given IP\n"
        return (
            tables.sender,
            tables.legacy_ctl.replace(
                "  disconnect [target]", extra + "  disconnect [target]",
            ),
            frozenset(),
        )
    if case == "wrong_signature":
        return (
            tables.sender,
            tables.legacy_ctl.replace(
                "  pin <PIN-or-password>  ",
                "  pin <target> <PIN-or-password>  ",
            ),
            frozenset(),
        )
    if case == "unverified_sender":
        return (
            tables.sender + "\n  -agent string\n    \tAgent name reported to the receiver\n",
            tables.legacy_ctl,
            frozenset(),
        )
    if case == "failed_source_verification":
        return (
            tables.source_sender,
            tables.extended_ctl,
            tables.extended_tokens - {"target-volume"},
        )
    if case == "inconsistent_token_pair":
        return tables.sender, tables.legacy_ctl, frozenset({"target-volume"})
    raise AssertionError(f"unknown capability case {case}")


def _relay_profile(tables, table):
    if table == "legacy":
        features = features_from_help(tables.sender, tables.legacy_ctl)
    else:
        features = features_from_help(
            tables.source_sender, tables.extended_ctl,
            advertised=tables.extended_tokens,
        )
    assert features.credential_relay is True
    return PublishedDoubletakeProfile(
        socket_path="/run/user/1000/airplay-linux/doubletake.sock", features=features,
    )


@pytest.mark.parametrize("case", [
    "legacy", "extended", "missing_pin", "added_row", "wrong_signature",
    "unverified_sender", "failed_source_verification", "inconsistent_token_pair",
])
def test_relay_capability(relay_help_tables, case):
    sender, ctl, advertised = _surface(relay_help_tables, case)
    features = features_from_help(sender, ctl, advertised=advertised)

    if case in {"legacy", "extended"}:
        # Both exact pinned tables carry the ``pin`` row and therefore prove relay.
        assert features.credential_relay is True
        # Relay is independent: no transaction, ownership, source or trust gate
        # follows from the same help surface.
        for name in _TRANSACTION_FLAGS + (
            "foreground_control", "owner_handshake", "physical_source",
            "virtual_source", "audio_restart", "keyring", "query_modes",
            "owned_daemon_control",
        ):
            assert getattr(features, name) is False, (case, name)
    else:
        # A missing row, an added row, a changed signature, an unverified
        # sender or an incoherent token/table pair proves nothing at all.
        assert features.credential_relay is False
        assert features.profile == "doubletake-unknown"
        assert all(
            getattr(features, name) is False
            for name in features.__dataclass_fields__
            if name not in ("profile", "version")
        ), case

    # Default, unknown and fixture profiles never inherit relay.
    assert PublishedDoubletakeProfile(socket_path="/run/user/1000/x.sock").features.credential_relay is False
    assert ApplicationContractProfile(socket_path="/run/user/1000/x.sock").features.credential_relay is False
    assert features_from_help("", "").credential_relay is False
    assert features_from_help(
        "doubletake 99.1.0\n", "Usage: doubletake-ctl <command>\n",
    ).credential_relay is False


def _relay_features(**overrides):
    values = dict(
        profile="doubletake-0.5+source",
        version="0.5.0",
        foreground_control=False,
        owner_handshake=False,
        multi_target=True,
        target_bound_pairing=False,
        physical_source=True,
        virtual_source=False,
        audio_probe=True,
        audio_restart=False,
        target_mute=True,
        keyring=False,
        provisional_commit=False,
        cancel_pairing=False,
        revoke_pairing=False,
        query_modes=False,
        credential_relay=True,
        source_reporting=True,
        source_reporting_complete=True,
        owned_daemon_control=True,
        target_volume=True,
    )
    values.update(overrides)
    return BackendFeatures(**values)


def _authority(**overrides):
    from airplay_linux.process.ownership import EvidenceAuthority
    from airplay_linux.process.procfs import TerminationEvidence

    return EvidenceAuthority(
        receipts=lambda: (),
        process=object(),
        evidence_factory=TerminationEvidence,
        features=_relay_features(**overrides),
    )


def test_relay_authorization():
    baseline = _relay_features()
    for name in _TRANSACTION_FLAGS:
        assert getattr(baseline, name) is False

    # Public submission is admitted with all four transaction flags false.
    authority = _authority()
    assert authority.allows(BackendOperation.submit_pairing) is True

    # Relay admits only submission; every transactional pairing operation keeps
    # its own requirement (all four flags are false here).
    for operation in (
        BackendOperation.begin_pairing,
        BackendOperation.cancel_pairing,
        BackendOperation.prepare_pairing,
        BackendOperation.commit_pairing,
        BackendOperation.mark_invalid,
        BackendOperation.revoke_pairing,
    ):
        assert authority.allows(operation) is False, operation

    # Each of the four conjuncts is necessary on its own.
    for name in ("credential_relay", "owned_daemon_control", "multi_target", "physical_source"):
        assert _authority(**{name: False}).allows(BackendOperation.submit_pairing) is False, name

    # A virtual source never substitutes for the mirror-only physical gate.
    assert _authority(physical_source=False, virtual_source=True).allows(
        BackendOperation.submit_pairing,
    ) is False


_OPTION_FORMS = [
    pytest.param(None, None, (), id="no-options"),
    pytest.param(AudioPolicy(enabled=False), None, ("--audio", "off"), id="audio-off"),
    pytest.param(AudioPolicy(enabled=True), None, ("--audio", "on"), id="audio-on"),
    pytest.param(
        AudioPolicy(enabled=True), VolumeLevel(value=37),
        ("--audio", "on", "--volume", "37"), id="audio-on-volume",
    ),
    pytest.param(None, VolumeLevel(value=0), ("--volume", "0"), id="volume-only"),
]


@pytest.mark.parametrize("table,proven", [("legacy", False), ("extended", True)])
@pytest.mark.parametrize("audio,level,expected_options", _OPTION_FORMS)
def test_relay_command(relay_help_tables, table, proven, audio, level, expected_options):
    profile = _relay_profile(relay_help_tables, table)
    receiver = target()
    payload = CredentialPayload(
        value=SensitiveText("5141"), audio=audio, initial_volume=level,
    )

    mapped = profile.try_argv(BackendOperation.submit_pairing, receiver, payload)

    if expected_options and not proven:
        # The options exist only on the extended table; without its proof they
        # must not be invented.
        assert isinstance(mapped, Err)
        assert mapped.error.code is ErrorCode.unsupported_operation
        with pytest.raises(ValueError):
            profile.argv(BackendOperation.submit_pairing, receiver, payload)
    else:
        assert isinstance(mapped, Ok)
        command = mapped.value
        assert isinstance(command, ProfileCommand)
        assert command.argv[:5] == (
            "doubletake-ctl", "-socket", profile.socket_path, "connect",
            receiver.canonical_host,
        )
        assert command.argv[5:-1] == expected_options
        assert command.secret_indices == (len(command.argv) - 1,)
        assert command.argv[-1] == "5141"
        assert command.redacted_argv[-1] == "<redacted>"
        assert "5141" not in repr(command)
        assert profile.argv(BackendOperation.submit_pairing, receiver, payload) == command

    # A named target is mandatory: the targetless ``pin`` form is never returned.
    targetless = profile.try_argv(BackendOperation.submit_pairing, None, payload)
    assert isinstance(targetless, Err)
    assert targetless.error.code is ErrorCode.unsupported_operation

    # Relay is never inferred from the payload or the profile name.
    unproved = replace(profile, features=replace(profile.features, credential_relay=False))
    refused = unproved.try_argv(BackendOperation.submit_pairing, receiver, payload)
    assert isinstance(refused, Err) and refused.error.code is ErrorCode.unsupported_operation

    # Only CredentialPayload carries relay input.
    wrong_payload = profile.try_argv(BackendOperation.submit_pairing, receiver, EmptyPayload())
    assert isinstance(wrong_payload, Err) and wrong_payload.error.code is ErrorCode.invalid_input

    if level is not None and proven:
        disabled = profile.try_argv(
            BackendOperation.submit_pairing, receiver,
            CredentialPayload(
                value=SensitiveText("5141"), audio=AudioPolicy(enabled=False),
                initial_volume=level,
            ),
        )
        assert isinstance(disabled, Err) and disabled.error.code is ErrorCode.invalid_input

    # The extended parser has no end-of-options escape, so an option-shaped
    # credential is unrepresentable there; the legacy positional form is unchanged.
    for value in ("-secret", "--audio", "--volume", "--"):
        shaped = profile.try_argv(
            BackendOperation.submit_pairing, receiver,
            CredentialPayload(value=SensitiveText(value)),
        )
        if proven:
            assert isinstance(shaped, Err), value
            assert shaped.error.code is ErrorCode.invalid_input
        else:
            assert isinstance(shaped, Ok), value
            assert shaped.value.argv[-1] == value


class _RecordingProcess:
    """Records each spec and answers with a valid status envelope."""

    def __init__(self):
        self.calls = []

    async def run(self, spec, timeout, stdin=None):
        self.calls.append(spec)
        return Ok(value=CommandOutput(
            exit_code=0, stdout=json.dumps({"ok": True, "state": "idle"}).encode(),
            stderr=b"",
        ))


def test_relay_metadata_refusals(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "extended")
    argv = (
        "doubletake-ctl", "-socket", profile.socket_path, "connect",
        "192.0.2.10", "5141",
    )
    final = len(argv) - 1
    assert ProfileCommand(argv=argv, secret_indices=(final,)).redacted_argv[-1] == "<redacted>"

    # Construction refuses malformed metadata with one fixed, argv-free message.
    for indices in (
        (True,), (-1,), (final + 1,), (final, final), (0, 0), (0.0,), "0",
    ):
        with pytest.raises(ValueError, match="invalid secret indices"):
            ProfileCommand(argv=argv, secret_indices=indices)
    with pytest.raises(ValueError, match="invalid secret indices"):
        ProfileCommand(argv=(), secret_indices=(0,))

    process = _RecordingProcess()
    control = PublishedDoubletakeControl(process, profile)
    valid = ProfileCommand(argv=argv, secret_indices=(final,))
    assert isinstance(
        asyncio.run(control._run(valid, operation=BackendOperation.submit_pairing)), Ok,
    )
    assert len(process.calls) == 1
    assert process.calls[0].secret_indices == (final,)
    assert process.calls[0].redacted_argv[-1] == "<redacted>"
    assert process.calls[0].argv[-1] == "5141"
    process.calls.clear()

    def forged(indices, *, operation=BackendOperation.submit_pairing):
        command = ProfileCommand(argv=argv, secret_indices=(final,))
        object.__setattr__(command, "secret_indices", indices)
        result = asyncio.run(control._run(command, operation=operation))
        assert isinstance(result, Err) and result.error.code is ErrorCode.invalid_input
        assert process.calls == [], (indices, operation)

    # The adapter repeats validation, so a forged command cannot reach a child.
    forged(())
    forged((True,))
    forged((-1,))
    forged((final + 1,))
    forged((final, final))
    forged("0")
    # Indices belong to the relay submission alone.
    forged((final,), operation=BackendOperation.connect)
    forged((final,), operation=BackendOperation.status)

    # The application-contract profile's fence-less transactional submission
    # keeps empty metadata and is admitted unchanged.
    contract = ApplicationContractProfile(socket_path="/run/user/1000/private.sock")
    transactional = ProfileCommand(argv=(
        "application-contract-v1", "-socket", contract.socket_path,
        "submit_pairing", "192.0.2.10",
    ))
    assert isinstance(
        asyncio.run(PublishedDoubletakeControl(process, contract)._run(
            transactional, operation=BackendOperation.submit_pairing,
        )),
        Ok,
    )
    assert len(process.calls) == 1
    assert process.calls[0].secret_indices == ()
