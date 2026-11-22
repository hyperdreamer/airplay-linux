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

from airplay_linux.backend.contract import (
    BackendFeatures,
    CredentialDispatchFailure,
    CredentialSubmissionReply,
    features_from_help,
)
from airplay_linux.backend.control import (
    ApplicationContractProfile,
    BackendOperation,
    CredentialPayload,
    EmptyPayload,
    ProfileCommand,
    PublishedDoubletakeControl,
    PublishedDoubletakeProfile,
    OwnershipProof,
)
from airplay_linux.domain.operations import ProcessIdentity, SensitiveText
from airplay_linux.domain.ports import CommandOutput, CommandSpec, CredentialProcessUncertain
from airplay_linux.domain.resources import AudioPolicy, Diagnostic, Err, ErrorCode, Ok, VolumeLevel
from airplay_linux.testing.adapters import Deferred
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


@pytest.mark.parametrize("table", ["legacy", "extended"])
def test_relay_rejects_contradictory_connect_scope_wording(relay_help_tables, table):
    if table == "legacy":
        sender, ctl, advertised = (
            relay_help_tables.sender,
            relay_help_tables.legacy_ctl,
            frozenset(),
        )
    else:
        sender, ctl, advertised = (
            relay_help_tables.source_sender,
            relay_help_tables.extended_ctl,
            relay_help_tables.extended_tokens,
        )
    contradictory = ctl.replace(
        "Start mirroring (to target IP, or first free device)",
        "Start mirroring while ignoring the target and using any receiver",
    )
    features = features_from_help(sender, contradictory, advertised=advertised)

    assert features.credential_relay is False
    assert features.multi_target is False
    assert features.profile == "doubletake-unknown"
    profile = PublishedDoubletakeProfile(
        socket_path="/run/user/1000/airplay-linux/doubletake.sock",
        features=features,
    )
    process = _CredentialRecordingProcess(CommandOutput(
        exit_code=0, stdout=b'{"ok": true}', stderr=b"",
    ))
    control = PublishedDoubletakeControl(process, profile)
    result = asyncio.run(control.submit_credential(
        target(),
        CredentialPayload(value=SensitiveText(f"{table}-scope-canary")),
        SimpleNamespace(),
        dispatch_fence=lambda: Ok(value=None),
    ))

    assert isinstance(result, CredentialDispatchFailure)
    assert result.dispatch_state == "no_spawn"
    assert process.credential_calls == []


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

    # Option-shaped values are rejected before command construction for both
    # recognized profiles.
    for value in ("-secret", "--audio", "--volume", "--"):
        shaped = profile.try_argv(
            BackendOperation.submit_pairing, receiver,
            CredentialPayload(value=SensitiveText(value)),
        )
        assert isinstance(shaped, Err), value
        assert shaped.error.code is ErrorCode.invalid_input


@pytest.mark.parametrize("table", ["legacy", "extended"])
def test_relay_command_preserves_nonempty_whitespace(relay_help_tables, table):
    profile = _relay_profile(relay_help_tables, table)
    receiver = target()
    value = "  "

    result = profile.try_argv(
        BackendOperation.submit_pairing, receiver,
        CredentialPayload(value=SensitiveText(value)),
    )

    assert isinstance(result, Ok)
    assert result.value.argv == (
        "doubletake-ctl", "-socket", profile.socket_path, "connect",
        receiver.canonical_host, value,
    )
    assert result.value.secret_indices == (len(result.value.argv) - 1,)
    assert result.value.redacted_argv[-1] == "<redacted>"


def test_relay_command_rejects_unrepresentable_values_before_building_argv(relay_help_tables):
    for table in ("legacy", "extended"):
        profile = _relay_profile(relay_help_tables, table)
        for value in ("", "contains\x00nul", "-leading-option", "unencodable-\ud800"):
            result = profile.try_argv(
                BackendOperation.submit_pairing,
                target(),
                CredentialPayload(value=SensitiveText(value)),
            )
            assert isinstance(result, Err)
            assert result.error.code is ErrorCode.invalid_input
            if value:
                assert value not in repr(result)

    # Credential shape and ordinary Unicode remain profile independent.
    legacy = _relay_profile(relay_help_tables, "legacy")
    ordinary = legacy.try_argv(
        BackendOperation.submit_pairing,
        target(),
        CredentialPayload(value=SensitiveText("letters and symbols ! 雪")),
    )
    assert isinstance(ordinary, Ok)
    assert ordinary.value.argv[-1] == "letters and symbols ! 雪"


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


class _RelayAuthority:
    def __init__(self):
        self.proof = OwnershipProof(
            kind="evidence", daemon=ProcessIdentity(pid=71, start_ticks=9),
            socket_entry=(1, 2), generation=1, handshake=None,
        )
        self.fence_calls = 0

    def allows(self, operation):
        return operation is BackendOperation.submit_pairing

    async def authorize(self, record, operation, target, fence):
        self.fence_calls += 1
        checked = fence()
        if isinstance(checked, Err):
            return checked
        return Ok(value=self.proof)

    async def prove(self, record):
        return Ok(value=self.proof)

    async def confirm(self, record, proof):
        return getattr(self, "confirmation", Ok(value=None))


class _CredentialRecordingProcess(_RecordingProcess):
    def __init__(self, output=None, process_result=None):
        super().__init__()
        self.output = output
        self.process_result = process_result
        self.credential_calls = []

    async def run_credential(self, spec, timeout, dispatch_fence):
        observed = CommandSpec(
            argv=spec.redacted_argv,
            env=spec.env,
            cwd=spec.cwd,
            pass_fds=spec.pass_fds,
            secret_indices=spec.secret_indices,
        )
        self.credential_calls.append(observed)
        checked = dispatch_fence()
        from airplay_linux.domain.ports import CredentialProcessCompleted, CredentialProcessNotSpawned
        if isinstance(checked, Err):
            return CredentialProcessNotSpawned(diagnostic=checked.error)
        if self.process_result is not None:
            if isinstance(self.process_result, Deferred):
                return await self.process_result.wait()
            return self.process_result
        return CredentialProcessCompleted(output=self.output)


def test_submit_credential_uses_the_fenced_process_boundary(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _CredentialRecordingProcess(CommandOutput(
        exit_code=0, stdout=b'{"ok": true, "error": "discard-me"}', stderr=b"",
    ))
    authority = _RelayAuthority()
    control = PublishedDoubletakeControl(
        process, profile, record=object(), authority=authority,
    )
    fence_calls = []

    def fence():
        fence_calls.append("fence")
        return Ok(value=None)

    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("relay-submit-canary")),
        SimpleNamespace(), dispatch_fence=fence,
    ))

    assert isinstance(result, CredentialSubmissionReply)
    assert result.accepted is True
    assert authority.fence_calls == 1
    assert len(fence_calls) == 3
    assert process.credential_calls[0].redacted_argv[-1] == "<redacted>"
    assert process.credential_calls[0].argv[-1] == "<redacted>"
    assert "relay-submit-canary" not in repr(process.credential_calls[0].argv)
    assert "relay-submit-canary" not in repr(process.credential_calls[0].env)
    assert "relay-submit-canary" not in repr(process.credential_calls)


@pytest.mark.parametrize(("exit_code", "stdout", "expected_state", "expected_accepted", "reason"), [
    (0, b'{"ok": true, "error": "receiver-private-error"}', "reply", True, None),
    (0, b'{"ok": false, "error": "receiver-private-error"}', "acknowledged", False, None),
    (1, b'{"ok": false, "error": "receiver-private-error"}', "acknowledged", False, None),
    (2, b'{"ok": true}', "uncertain", None, "incomplete_reply"),
    (0, b'{"ok": 1}', "uncertain", None, "malformed_reply"),
    (0, b'{broken', "uncertain", None, "malformed_reply"),
])
def test_submit_credential_classifies_complete_envelopes_and_discards_text(
    relay_help_tables, exit_code, stdout, expected_state, expected_accepted, reason,
):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _CredentialRecordingProcess(CommandOutput(
        exit_code=exit_code, stdout=stdout, stderr=b"",
    ))
    control = PublishedDoubletakeControl(
        process, profile, record=object(), authority=_RelayAuthority(),
    )
    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("relay-classification-canary")),
        SimpleNamespace(), dispatch_fence=lambda: Ok(value=None),
    ))

    if expected_state == "reply":
        assert isinstance(result, CredentialSubmissionReply)
        assert result.accepted is expected_accepted
    else:
        assert isinstance(result, CredentialDispatchFailure)
        assert result.dispatch_state == expected_state
        assert result.reason == reason
        assert result.reply_accepted is expected_accepted
        if expected_state == "acknowledged":
            assert result.refusal == "rejected"
    assert "receiver-private-error" not in repr(result)
    assert "relay-classification-canary" not in repr(result)


def test_submit_credential_contains_decoder_integer_limit_errors(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _CredentialRecordingProcess(CommandOutput(
        exit_code=0,
        stdout=b'{"ok": true, "unexpected": ' + b"9" * 5000 + b"}",
        stderr=b"",
    ))
    control = PublishedDoubletakeControl(
        process, profile, record=object(), authority=_RelayAuthority(),
    )

    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("decoder-exception-canary")),
        SimpleNamespace(), dispatch_fence=lambda: Ok(value=None),
    ))

    assert isinstance(result, CredentialDispatchFailure)
    assert result.dispatch_state == "uncertain"
    assert result.reason == "malformed_reply"
    assert "decoder-exception-canary" not in repr(result)


def test_submit_credential_preserves_process_uncertainty(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _CredentialRecordingProcess(
        process_result=CredentialProcessUncertain(reason="child_cleanup_uncertain"),
    )
    control = PublishedDoubletakeControl(
        process, profile, record=object(), authority=_RelayAuthority(),
    )

    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("relay-uncertain-canary")),
        SimpleNamespace(), dispatch_fence=lambda: Ok(value=None),
    ))

    assert isinstance(result, CredentialDispatchFailure)
    assert result.dispatch_state == "uncertain"
    assert result.reason == "child_cleanup_uncertain"
    assert result.reply_at is None and result.reply_accepted is None
    assert "relay-uncertain-canary" not in repr(result)


def test_submit_credential_reports_acknowledged_when_post_reply_ownership_fails(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _CredentialRecordingProcess(CommandOutput(
        exit_code=0, stdout=b'{"ok": true}', stderr=b"",
    ))
    authority = _RelayAuthority()
    authority.confirmation = Err(error=Diagnostic(
        code=ErrorCode.ownership_mismatch,
        message="fixed ownership refusal",
        recovery=(),
    ))
    control = PublishedDoubletakeControl(
        process, profile, record=object(), authority=authority,
    )

    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("relay-post-reply-canary")),
        SimpleNamespace(), dispatch_fence=lambda: Ok(value=None),
    ))

    assert isinstance(result, CredentialDispatchFailure)
    assert result.dispatch_state == "acknowledged"
    assert result.reply_accepted is True
    assert result.refusal == "stale"
    assert "relay-post-reply-canary" not in repr(result)


def test_relay_authorization_rechecks_replaced_generation_after_await(relay_help_tables):
    async def scenario():
        profile = _relay_profile(relay_help_tables, "legacy")
        entered = asyncio.Event()
        release = asyncio.Event()
        generation = {"value": 1}

        class BarrierAuthority(_RelayAuthority):
            async def authorize(self, record, operation, receiver, fence):
                entered.set()
                await release.wait()
                checked = fence()
                if isinstance(checked, Err):
                    return checked
                return Ok(value=self.proof)

        process = _CredentialRecordingProcess(CommandOutput(
            exit_code=0, stdout=b'{"ok": true}', stderr=b"",
        ))
        control = PublishedDoubletakeControl(
            process, profile, record=object(), authority=BarrierAuthority(),
        )

        def fence():
            if generation["value"] != 1:
                return Err(error=Diagnostic(
                    code=ErrorCode.stale_target,
                    message="fixed stale target",
                    recovery=(),
                ))
            return Ok(value=None)

        submission = asyncio.create_task(control.submit_credential(
            target(), CredentialPayload(value=SensitiveText("relay-barrier-canary")),
            SimpleNamespace(), dispatch_fence=fence,
        ))
        await entered.wait()
        generation["value"] = 2
        release.set()
        result = await submission

        assert isinstance(result, CredentialDispatchFailure)
        assert result.dispatch_state == "no_spawn"
        assert result.error.code is ErrorCode.stale_target
        assert process.credential_calls == []
        assert "relay-barrier-canary" not in repr(result)

    asyncio.run(scenario())


def test_relay_fence_is_rechecked_after_final_ownership_proof(relay_help_tables):
    async def scenario():
        profile = _relay_profile(relay_help_tables, "legacy")
        entered = asyncio.Event()
        release = asyncio.Event()
        generation = {"value": 1}

        class FinalProofBarrierAuthority(_RelayAuthority):
            async def prove(self, record):
                entered.set()
                await release.wait()
                return Ok(value=self.proof)

        process = _CredentialRecordingProcess(CommandOutput(
            exit_code=0, stdout=b'{"ok": true}', stderr=b"",
        ))
        control = PublishedDoubletakeControl(
            process, profile, record=object(), authority=FinalProofBarrierAuthority(),
        )

        def fence():
            if generation["value"] != 1:
                return Err(error=Diagnostic(
                    code=ErrorCode.stale_target,
                    message="fixed stale target",
                    recovery=(),
                ))
            return Ok(value=None)

        submission = asyncio.create_task(control.submit_credential(
            target(), CredentialPayload(value=SensitiveText("final-proof-canary")),
            SimpleNamespace(), dispatch_fence=fence,
        ))
        await entered.wait()
        generation["value"] = 2
        release.set()
        result = await submission

        assert isinstance(result, CredentialDispatchFailure)
        assert result.dispatch_state == "no_spawn"
        assert result.error.code is ErrorCode.stale_target
        assert process.credential_calls == []
        assert "final-proof-canary" not in repr(result)

    asyncio.run(scenario())


def test_credential_test_process_retains_only_redacted_stale_and_deferred_specs():
    from airplay_linux.domain.ports import (
        CommandSpec,
        CredentialProcessCompleted,
        CredentialProcessNotSpawned,
    )

    async def scenario():
        stale_canary = "stale-recorder-canary-9f34"
        stale_process = _CredentialRecordingProcess()
        stale_result = await stale_process.run_credential(
            CommandSpec(
                argv=("doubletake-ctl", "connect", "192.0.2.10", stale_canary),
                env={}, secret_indices=(3,),
            ),
            1.0,
            lambda: Err(error=Diagnostic(
                code=ErrorCode.stale_target, message="fixed", recovery=(),
            )),
        )
        assert isinstance(stale_result, CredentialProcessNotSpawned)
        assert stale_process.credential_calls[0].argv[-1] == "<redacted>"
        assert stale_canary not in repr(stale_process.credential_calls[0].argv)
        assert stale_canary not in repr(stale_process.credential_calls[0].env)

        deferred_canary = "deferred-recorder-canary-71aa"
        deferred = Deferred()
        deferred_process = _CredentialRecordingProcess(process_result=deferred)
        submission = asyncio.create_task(deferred_process.run_credential(
            CommandSpec(
                argv=("doubletake-ctl", "connect", "192.0.2.10", deferred_canary),
                env={}, secret_indices=(3,),
            ),
            1.0,
            lambda: Ok(value=None),
        ))
        await asyncio.sleep(0)
        retained = deferred_process.credential_calls[0]
        assert retained.argv[-1] == "<redacted>"
        assert deferred_canary not in repr(retained.argv)
        assert deferred_canary not in repr(retained.env)
        deferred.resolve(CredentialProcessCompleted(output=CommandOutput(
            exit_code=0, stdout=b'{"ok": true}', stderr=b"",
        )))
        assert isinstance(await submission, CredentialProcessCompleted)
        assert deferred_canary not in repr(deferred_process.credential_calls)

    asyncio.run(scenario())


def test_credential_dispatch_failure_and_reply_are_typed_and_secret_free():
    reply = CredentialSubmissionReply(reply_at=1.25, accepted=False)
    assert reply.accepted is False
    failure = CredentialDispatchFailure(
        error=Err(error=Diagnostic(
            code=ErrorCode.dependency_incompatible,
            message="fixed",
            recovery=(),
        )).error,
        dispatch_state="uncertain",
        reason="malformed_reply",
    )
    assert failure.reply_at is None
    assert failure.reply_accepted is None
    assert "malformed_reply" in repr(failure)


def test_submit_credential_requires_fence_and_never_uses_generic_process_run(relay_help_tables):
    profile = _relay_profile(relay_help_tables, "legacy")
    process = _RecordingProcess()
    control = PublishedDoubletakeControl(process, profile)
    result = asyncio.run(control.submit_credential(
        target(), CredentialPayload(value=SensitiveText("relay-submit-canary")),
        SimpleNamespace(),
        dispatch_fence=lambda: Ok(value=None),
    ))
    assert isinstance(result, CredentialDispatchFailure)
    assert result.dispatch_state == "no_spawn"
    assert process.calls == []
    assert "relay-submit-canary" not in repr(result)


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
