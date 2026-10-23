"""Startup capability evidence is affirmative, isolated, and per-mode gated.

Every test injects the environment mapping and each probe, so nothing here reads
the real session, connects to D-Bus, finds a real executable, or starts desktop
tooling. The matrix pins the rule the design states once: KDE Plasma 6 Wayland is
the guaranteed v1 environment, a missing dependency fails only the capability it
belongs to, discovery and saved-device inspection survive transport/helper
failures, and no mode is enabled because an error was merely absent.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from pathlib import Path

import pytest

from airplay_linux.backend.contract import BackendFeatures, features_from_help
from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.helper import HelperFeatures, VirtualHelper
from airplay_linux.domain.resources import (
    Err,
    ErrorCode,
    Mode,
    Ok,
    PhysicalMonitorSource,
    RecoveryAction,
    Result,
    Size,
    TrustState,
    VirtualOutputSource,
)
from airplay_linux.policy.audio import REQUIRED_PORTAL_INTERFACES
from airplay_linux.policy.capabilities import (
    KDE_PLASMA_6_WAYLAND,
    EnvironmentProbe,
    can_start,
)
from airplay_linux.testing.adapters import FakeCapture, FakeProcess
from airplay_linux.testing.time import FakeClock

PLASMA = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "KDE",
    "KDE_SESSION_VERSION": "6",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}
KDE_ENVIRONMENT = "kde-plasma-6-wayland"
MONITOR = PhysicalMonitorSource(connector="DP-1", portal_restore_token=None)
FINGERPRINT = "a" * 64

# --------------------------------------------------------------------------- fakes


def features(**overrides: object) -> BackendFeatures:
    """The pinned release's proven profile, with opt-in affirmative flags."""
    values: dict[str, object] = {
        "profile": "doubletake-0.5",
        "version": "0.5.0",
        "foreground_control": True,
        "owner_handshake": True,
        "multi_target": True,
        "target_bound_pairing": True,
        "physical_source": True,
        "virtual_source": True,
        "audio_probe": True,
        "audio_restart": False,
        "target_mute": True,
        "keyring": True,
        "provisional_commit": True,
        "cancel_pairing": True,
        "revoke_pairing": True,
        "query_modes": False,
    }
    return BackendFeatures(**(values | overrides))  # type: ignore[arg-type]


def _next(values: deque[object]) -> object:
    return values.popleft() if len(values) > 1 else values[0]


class _ScriptedProbe:
    """A recorded async probe returning scripted results in FIFO order."""

    def __init__(self, *results: object) -> None:
        self._results = deque(results) if results else deque([Ok(value=None)])
        self.calls = 0
        self.failure: Exception | None = None

    async def __call__(self):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return _next(self._results)

    async def check(self):
        """The ``AudioProbe`` shape, sharing this probe's script and counter."""
        return await self()


def backend(*results: object) -> _ScriptedProbe:
    return _ScriptedProbe(*(results or (Ok(value=features()),)))


def helper(*results: object) -> _ScriptedProbe:
    return _ScriptedProbe(
        *(results or (Ok(value=HelperFeatures(
            version="24.08.2", distinct_outputs=True, capability_fingerprint=FINGERPRINT,
        )),)),
    )


def audio(*results: object) -> _ScriptedProbe:
    return _ScriptedProbe(*(results or (Ok(value=None),)))


class _Missing:
    """A command finder where every executable is absent."""

    def __init__(self, *missing: str) -> None:
        self.missing = frozenset(missing)
        self.looked_up: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.looked_up.append(name)
        return None if name in self.missing else f"/usr/bin/{name}"


class _Modules:
    def __init__(self, *missing: str) -> None:
        self.missing = frozenset(missing)
        self.looked_up: list[str] = []

    def __call__(self, name: str) -> bool:
        self.looked_up.append(name)
        return name not in self.missing


def capture(*monitors: PhysicalMonitorSource | Result[None]) -> FakeCapture:
    fake = FakeCapture()
    if not monitors:
        fake.add_physical(MONITOR)
    for item in monitors:
        if isinstance(item, PhysicalMonitorSource):
            fake.add_physical(item)
        else:
            fake.queue_result("list_physical", item)
    return fake


class _Keyring:
    def __init__(self, *values: bool) -> None:
        self._values = deque(values) if values else deque([True])
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return _next(self._values)  # type: ignore[return-value]


def probe(
    *,
    environment: dict[str, str] | None = None,
    command: Callable[[str], str | None] | None = None,
    modules: Callable[[str], bool] | None = None,
    backend_probe: _ScriptedProbe | None = None,
    helper_probe: _ScriptedProbe | None = None,
    capture_port: FakeCapture | None = None,
    audio_probe: _ScriptedProbe | None = None,
    keyring_probe: _Keyring | None = None,
) -> EnvironmentProbe:
    return EnvironmentProbe(
        environment=PLASMA if environment is None else environment,
        command_finder=command if command is not None else _Missing(),
        module_finder=modules if modules is not None else _Modules(),
        backend_probe=backend_probe if backend_probe is not None else backend(),
        helper_probe=helper_probe if helper_probe is not None else helper(),
        capture=capture_port if capture_port is not None else capture(),
        audio_probe=audio_probe if audio_probe is not None else audio(),
        keyring_probe=keyring_probe if keyring_probe is not None else _Keyring(),
    )


def _codes(report) -> set[ErrorCode]:
    return {item.code for item in report.diagnostics}


def _details(report) -> str:
    return " | ".join(item.detail or "" for item in report.diagnostics)


def _messages(report) -> str:
    return " | ".join(item.message for item in report.diagnostics)


# ---------------------------------------------------------------------- matrix


async def test_complete_kde_plasma_6_wayland_enables_every_proven_capability():
    report = await probe().check()

    assert report.environment == KDE_PLASMA_6_WAYLAND == KDE_ENVIRONMENT
    assert report.discovery is True
    assert report.mirror is True
    assert report.virtual_display is True
    assert report.audio is True
    assert report.remembered_pairing is True
    assert report.additional_virtual_outputs is True
    assert report.diagnostics == ()


async def test_missing_krfb_disables_virtual_display_alone():
    missing_helper = Err(error=diagnostic(
        ErrorCode.dependency_missing, command="krfb-virtualmonitor",
    ))
    report = await probe(helper_probe=helper(missing_helper)).check()

    assert report.virtual_display is False
    assert report.additional_virtual_outputs is False
    assert report.mirror is True
    assert report.discovery is True
    assert report.audio is True
    assert report.remembered_pairing is True
    assert "krfb-virtualmonitor" in _messages(report)
    assert ErrorCode.dependency_missing in _codes(report)


async def test_missing_kscreen_disables_virtual_display_alone():
    command = _Missing("kscreen-doctor")
    report = await probe(command=command).check()

    assert report.virtual_display is False
    assert report.mirror is True
    assert report.discovery is True
    assert "kscreen-doctor" in _messages(report)
    assert "kscreen-doctor" in _details(report)
    assert ErrorCode.dependency_missing in _codes(report)


async def test_transport_incompatibility_disables_modes_but_not_discovery():
    incompatible = Err(error=diagnostic(ErrorCode.dependency_incompatible))
    report = await probe(backend_probe=backend(incompatible)).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.additional_virtual_outputs is False
    assert report.discovery is True
    assert report.audio is False
    assert report.remembered_pairing is False
    assert ErrorCode.dependency_incompatible in _codes(report)


async def test_a_backend_probe_failure_never_raises_or_hides_discovery():
    failing = backend()
    failing.failure = RuntimeError("Traceback: gateway 192.0.2.1")
    report = await probe(backend_probe=failing).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.discovery is True
    assert "192.0.2.1" not in _messages(report)
    assert "192.0.2.1" not in _details(report)


async def test_unavailable_discovery_keeps_both_modes_usable():
    modules = _Modules("zeroconf")
    report = await probe(modules=modules).check()

    assert report.discovery is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert report.audio is True
    assert "zeroconf" in _details(report)
    assert ErrorCode.dependency_missing in _codes(report)


async def test_audio_denial_disables_audio_alone_and_preserves_the_decision_actions():
    denial = Err(error=diagnostic(ErrorCode.audio_denied))
    report = await probe(audio_probe=audio(denial)).check()

    assert report.audio is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert report.discovery is True
    diagnostic_entry = next(
        item for item in report.diagnostics if item.code is ErrorCode.audio_denied
    )
    assert set(diagnostic_entry.recovery) >= {
        RecoveryAction.retry_audio,
        RecoveryAction.video_only,
        RecoveryAction.stop,
    }


async def test_an_audio_probe_that_raises_fails_only_audio():
    failing = audio()
    failing.failure = RuntimeError("pipewire core dump")
    report = await probe(audio_probe=failing).check()

    assert report.audio is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert "core dump" not in _messages(report)


async def test_unusable_keyring_disables_remembered_pairing_alone():
    report = await probe(keyring_probe=_Keyring(False)).check()

    assert report.remembered_pairing is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert report.discovery is True
    assert ErrorCode.keyring_unavailable in _codes(report)


async def test_a_synchronous_keyring_probe_is_also_accepted():
    report = await probe(keyring_probe=lambda: True).check()  # type: ignore[arg-type]
    assert report.remembered_pairing is True

    report = await probe(keyring_probe=lambda: False).check()  # type: ignore[arg-type]
    assert report.remembered_pairing is False


async def test_a_keyring_probe_that_raises_is_reported_as_unavailable():
    class Raising:
        async def __call__(self) -> bool:
            raise RuntimeError("dbus secret backend absent")

    report = await probe(keyring_probe=Raising()).check()  # type: ignore[arg-type]

    assert report.remembered_pairing is False
    assert report.mirror is True
    assert "secret backend absent" not in _messages(report)
    assert ErrorCode.keyring_unavailable in _codes(report)


async def test_a_second_virtual_output_limitation_blocks_only_additional_outputs():
    single = Ok(value=HelperFeatures(
        version="24.08.2", distinct_outputs=False, capability_fingerprint=FINGERPRINT,
    ))
    report = await probe(helper_probe=helper(single)).check()

    assert report.additional_virtual_outputs is False
    assert report.virtual_display is True
    assert report.mirror is True


@pytest.mark.parametrize(
    ("environment", "token"),
    [
        ({**PLASMA, "XDG_SESSION_TYPE": "x11"}, "x11"),
        ({**PLASMA, "XDG_CURRENT_DESKTOP": "GNOME"}, "gnome-wayland"),
        ({"XDG_SESSION_TYPE": "wayland"}, "unknown"),
        ({**PLASMA, "KDE_SESSION_VERSION": "5"}, "kde-plasma-5-wayland"),
        ({**PLASMA, "KDE_SESSION_VERSION": ""}, "kde-wayland-unverified"),
    ],
)
async def test_other_or_unproven_desktops_are_reported_separately_and_gated(
    environment: dict[str, str], token: str,
):
    report = await probe(environment=environment).check()

    assert report.environment == token
    assert report.mirror is False
    assert report.virtual_display is False
    assert report.discovery is True
    assert ErrorCode.unsupported_operation in _codes(report)


async def test_missing_pyside6_is_reported_without_disabling_discovery():
    modules = _Modules("PySide6")
    report = await probe(modules=modules).check()

    assert "PySide6" in _details(report)
    assert ErrorCode.dependency_missing in _codes(report)
    assert report.discovery is True
    assert report.mirror is True
    assert report.virtual_display is True


# ------------------------------------------------------- affirmative evidence


async def test_an_absent_capture_provider_never_enables_mirror():
    builder_without_capture = EnvironmentProbe(
        environment=PLASMA,
        command_finder=_Missing(),
        module_finder=_Modules(),
        backend_probe=backend(),
        helper_probe=helper(),
        capture=None,
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )
    report = await builder_without_capture.check()
    assert report.mirror is False
    assert report.virtual_display is True
    assert ErrorCode.unsupported_operation in _codes(report)


async def test_an_empty_physical_monitor_list_does_not_prove_mirror():
    report = await probe(capture_port=capture(Ok(value=()))).check()

    assert report.mirror is False
    assert report.virtual_display is True
    assert ErrorCode.unsupported_operation in _codes(report)


async def test_a_capture_error_does_not_prove_mirror():
    denied = Err(error=diagnostic(ErrorCode.portal_denied))
    report = await probe(capture_port=capture(denied)).check()

    assert report.mirror is False
    assert report.virtual_display is True
    assert ErrorCode.portal_denied in _codes(report)


async def test_a_capture_probe_that_raises_does_not_prove_mirror():
    fake = capture()

    class Raising:
        async def list_physical(self):
            raise RuntimeError("portal socket vanished")

    builder = EnvironmentProbe(
        environment=PLASMA,
        command_finder=_Missing(),
        module_finder=_Modules(),
        backend_probe=backend(),
        helper_probe=helper(),
        capture=Raising(),  # type: ignore[arg-type]
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )
    report = await builder.check()

    assert report.mirror is False
    assert report.virtual_display is True
    assert "portal socket vanished" not in _messages(report)
    assert fake.released == []


async def test_a_backend_without_ownership_evidence_never_enables_a_mode():
    unowned = features(foreground_control=False, owner_handshake=False)
    report = await probe(backend_probe=backend(Ok(value=unowned))).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.remembered_pairing is False
    assert any(
        item.code is ErrorCode.dependency_incompatible
        and item.detail == "doubletake transport"
        for item in report.diagnostics
    )


async def test_a_backend_without_an_audio_surface_disables_audio_alone():
    report = await probe(
        backend_probe=backend(Ok(value=features(audio_probe=False))),
    ).check()

    assert report.audio is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert any(
        item.code is ErrorCode.audio_unavailable and item.detail == "doubletake audio"
        for item in report.diagnostics
    )


async def test_unproven_backend_trust_transactions_disable_remembered_pairing_alone():
    report = await probe(
        backend_probe=backend(Ok(value=features(keyring=False))),
    ).check()

    assert report.remembered_pairing is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert any(
        item.code is ErrorCode.dependency_incompatible
        and item.detail == "target_bound_pairing"
        for item in report.diagnostics
    )


async def test_a_backend_that_cannot_accept_typed_sources_never_enables_a_mode():
    untyped = features(physical_source=False, virtual_source=False)
    report = await probe(backend_probe=backend(Ok(value=untyped))).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.discovery is True


class _FixedCapture:
    """A capture provider that reports an exact, possibly non-physical list."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def list_physical(self):
        return Ok(value=self._value)


async def test_a_virtual_output_descriptor_never_proves_a_physical_source():
    virtual = VirtualOutputSource(
        connector="Virtual-1", kscreen_output_id="9",
        size=Size(width=1920, height=1080),
    )
    report = await probe(capture_port=_FixedCapture((virtual,))).check()

    assert report.mirror is False
    assert report.virtual_display is True
    assert ErrorCode.unsupported_operation in _codes(report)


# --------------------------------------------------------- probe interactions


async def test_startup_probes_are_read_only_and_never_launch_a_helper_or_session():
    capture_port = capture()
    helper_probe = helper()
    backend_probe = backend()

    report = await probe(
        capture_port=capture_port,
        helper_probe=helper_probe,
        backend_probe=backend_probe,
    ).check()

    assert report.mirror is True
    assert capture_port.journal.names() == ["capture.list_physical"]
    assert capture_port.selections == []
    assert capture_port.released == []
    assert capture_port.refused == []
    assert helper_probe.calls == 1
    assert backend_probe.calls == 1


async def test_refresh_reprobes_after_a_dependency_is_installed():
    missing = Err(error=diagnostic(
        ErrorCode.dependency_missing, command="krfb-virtualmonitor",
    ))
    installed = Ok(value=HelperFeatures(
        version="24.08.2", distinct_outputs=True, capability_fingerprint=FINGERPRINT,
    ))
    helper_probe = helper(missing, installed)
    builder = probe(helper_probe=helper_probe)

    first = await builder.check()
    assert first.virtual_display is False

    # ``check`` serves the report the startup path already gathered; the user's
    # Retry-after-fix action calls ``refresh`` and observes the new evidence.
    cached = await builder.check()
    assert cached is first
    assert helper_probe.calls == 1

    refreshed = await builder.refresh()
    assert refreshed.virtual_display is True
    assert refreshed.mirror is True
    assert helper_probe.calls == 2


class _FixedDBus:
    """Portal/keyring introspection double for the production factory test."""

    def __init__(self, names: frozenset[str]) -> None:
        self._names = names
        self.calls: list[tuple[str, str]] = []

    async def interfaces(self, service: str, path: str) -> frozenset[str]:
        self.calls.append((service, path))
        return self._names


async def test_the_production_factory_consumes_the_frozen_backend_and_helper_probes():
    """Task 8 ``probe_backend`` and Task 13 ``VirtualHelper.probe`` are the seams.

    Both are driven here through the deterministic process double; no real
    ``doubletake`` or ``krfb-virtualmonitor`` is installed or started.
    """
    process = FakeProcess()
    process.queue_result(
        "run", Err(error=diagnostic(ErrorCode.dependency_missing, command="doubletake")),
    )
    process.queue_result(
        "run", Err(error=diagnostic(
            ErrorCode.dependency_missing, command="krfb-virtualmonitor",
        )),
    )
    helper = VirtualHelper(process, FakeClock())
    builder = EnvironmentProbe.from_environment(
        process=process,
        helper=helper,
        capture=capture(),
        environment=PLASMA,
        dbus=_FixedDBus(REQUIRED_PORTAL_INTERFACES),
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )

    report = await builder.check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.discovery is True
    assert any("doubletake" in item.message for item in report.diagnostics)
    assert any("krfb-virtualmonitor" in item.message for item in report.diagnostics)
    assert [spec.argv[0] for spec, _ in process.runs] == ["doubletake", "krfb-virtualmonitor"]


async def test_an_explicit_audio_permission_denial_updates_the_report_only_for_audio():
    builder = probe()
    first = await builder.check()
    assert first.audio is True

    builder.register_audio_probe_capability(
        False, "org.freedesktop.portal.ScreenCast permission",
    )
    second = await builder.check()

    assert second.audio is False
    assert second.mirror is True
    assert second.virtual_display is True
    assert any(
        item.code is ErrorCode.audio_unavailable
        and "permission" in (item.detail or "")
        for item in second.diagnostics
    )


async def test_a_granted_audio_permission_keeps_audio_available():
    builder = probe()
    builder.register_audio_probe_capability(True)
    report = await builder.check()

    assert report.audio is True
    assert not any(
        item.code in {ErrorCode.audio_denied, ErrorCode.audio_unavailable}
        for item in report.diagnostics
    )


async def test_audio_permission_registration_rejects_malformed_values():
    builder = probe()
    with pytest.raises(TypeError):
        builder.register_audio_probe_capability(1)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        builder.register_audio_probe_capability(False, 3)  # type: ignore[arg-type]


async def test_a_failed_probe_is_not_cached_in_a_way_that_hides_a_retry():
    denied = Err(error=diagnostic(ErrorCode.audio_denied))
    audio_probe = audio(denied)
    builder = probe(audio_probe=audio_probe)

    assert (await builder.check()).audio is False
    assert (await builder.refresh()).audio is False
    assert audio_probe.calls == 2


# ------------------------------------------------------------------- can_start


async def test_can_start_allows_the_proven_mode_and_trust_state():
    report = await probe().check()

    assert can_start(report, Mode.MIRROR, TrustState.Trusted) == Ok(value=None)
    assert can_start(report, Mode.VIRTUAL, TrustState.Untrusted) == Ok(value=None)


async def test_a_blocked_capability_does_not_block_the_other_mode():
    missing_helper = Err(error=diagnostic(
        ErrorCode.dependency_missing, command="krfb-virtualmonitor",
    ))
    report = await probe(helper_probe=helper(missing_helper)).check()

    blocked = can_start(report, Mode.VIRTUAL, TrustState.Trusted)
    assert isinstance(blocked, Err)
    assert blocked.error.code is ErrorCode.unsupported_operation
    assert can_start(report, Mode.MIRROR, TrustState.Trusted) == Ok(value=None)

    empty_capture = await probe(capture_port=capture(Ok(value=()))).check()
    blocked = can_start(empty_capture, Mode.MIRROR, TrustState.Trusted)
    assert isinstance(blocked, Err)
    assert blocked.error.code is ErrorCode.unsupported_operation
    assert can_start(empty_capture, Mode.VIRTUAL, TrustState.Trusted) == Ok(value=None)


@pytest.mark.parametrize(
    "trust", [TrustState.CleanupRequired, TrustState.RevocationPending],
)
async def test_unconfirmed_trust_cleanup_blocks_both_modes(trust: TrustState):
    report = await probe().check()

    for mode in (Mode.MIRROR, Mode.VIRTUAL):
        result = can_start(report, mode, trust)
        assert isinstance(result, Err)
        assert result.error.code is ErrorCode.trust_cleanup_required
        assert RecoveryAction.retry_revocation in result.error.recovery


async def test_can_start_rejects_malformed_arguments():
    report = await probe().check()

    with pytest.raises(TypeError):
        can_start(report, "mirror", TrustState.Trusted)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        can_start(report, Mode.MIRROR, "Trusted")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        can_start(object(), Mode.MIRROR, TrustState.Trusted)  # type: ignore[arg-type]


async def test_the_current_environment_without_krfb_keeps_shell_and_discovery_usable():
    """The reference machine's pinned backend proves neither typed source.

    ``krfb-virtualmonitor`` is absent there, so Virtual is unavailable, and the
    published 0.5 profile proves no typed physical or virtual source, so Mirror
    is unavailable too. Discovery, saved-device inspection, and the rest of the
    shell are unaffected; both start attempts fail locally with a typed error.
    """
    fixtures = Path(__file__).resolve().parents[1] / "fixtures" / "backend"
    pinned = features_from_help(
        (fixtures / "doubletake-0.5-help.txt").read_text(),
        (fixtures / "doubletake-ctl-0.5-help.txt").read_text(),
    )
    builder = probe(
        backend_probe=backend(Ok(value=pinned)),
        helper_probe=helper(Err(error=diagnostic(
            ErrorCode.dependency_missing, command="krfb-virtualmonitor",
        ))),
        modules=_Modules(),
        command=_Missing("krfb-virtualmonitor"),
        audio_probe=audio(),
        keyring_probe=_Keyring(True),
    )

    report = await builder.check()

    assert report.environment == KDE_PLASMA_6_WAYLAND
    assert report.discovery is True
    assert report.mirror is False
    assert report.virtual_display is False
    assert report.additional_virtual_outputs is False
    assert report.remembered_pairing is False
    assert any(
        item.code is ErrorCode.dependency_missing
        and "krfb-virtualmonitor" in item.message
        for item in report.diagnostics
    )
    assert isinstance(can_start(report, Mode.MIRROR, TrustState.Unknown), Err)
    assert isinstance(can_start(report, Mode.VIRTUAL, TrustState.Unknown), Err)


async def test_can_start_never_uses_a_diagnostic_from_an_unrelated_capability():
    """A denial that names another capability must not become the start error."""
    denial = Err(error=diagnostic(ErrorCode.audio_denied))
    report = await probe(audio_probe=audio(denial)).check()

    result = can_start(report, Mode.MIRROR, TrustState.Trusted)
    assert result == Ok(value=None)
