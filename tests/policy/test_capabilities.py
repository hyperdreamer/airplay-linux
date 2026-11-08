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
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from airplay_linux.backend.contract import BackendFeatures, features_from_help
from airplay_linux.diagnostics import diagnostic
from airplay_linux.display.helper import HelperFeatures, VirtualHelper
from airplay_linux.domain.resources import (
    SESSION_PROFILE_DETAIL_PREFIX,
    Err,
    ErrorCode,
    Mode,
    ModeRefusal,
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
    MIRROR_ENVIRONMENTS,
    VIRTUAL_DISPLAY_ENVIRONMENTS,
    EnvironmentProbe,
    can_start,
    classify_environment,
    environment_label,
)
from airplay_linux.testing.adapters import FakeCapture, FakeProcess
from airplay_linux.testing.time import FakeClock

PLASMA = {
    "XDG_SESSION_TYPE": "wayland",
    "XDG_CURRENT_DESKTOP": "KDE",
    "KDE_SESSION_VERSION": "6",
    "XDG_RUNTIME_DIR": "/run/user/1000",
}

#: The complete 27-cell profile truth table: nine desktop/version rows by the
#: three normalized session columns (``x11``, ``wayland``, other/empty).
PROFILE_MATRIX: tuple[tuple[str, str, str, str], ...] = (
    # desktop, version, session, expected profile token
    ("KDE", "6", "x11", "kde-plasma-6-x11"),
    ("KDE", "6", "wayland", "kde-plasma-6-wayland"),
    ("KDE", "6", "", "unknown"),
    ("KDE", "5", "x11", "kde-plasma-5-x11"),
    ("KDE", "5", "wayland", "kde-plasma-5-wayland"),
    ("KDE", "5", "", "unknown"),
    ("KDE", "", "x11", "kde-x11-unverified"),
    ("KDE", "", "wayland", "kde-wayland-unverified"),
    ("KDE", "", "", "unknown"),
    ("GNOME", "6", "x11", "x11"),
    ("GNOME", "6", "wayland", "gnome-wayland"),
    ("GNOME", "6", "", "unknown"),
    ("GNOME", "5", "x11", "x11"),
    ("GNOME", "5", "wayland", "gnome-wayland"),
    ("GNOME", "5", "", "unknown"),
    ("GNOME", "", "x11", "x11"),
    ("GNOME", "", "wayland", "gnome-wayland"),
    ("GNOME", "", "", "unknown"),
    ("XFCE", "6", "x11", "x11"),
    ("XFCE", "6", "wayland", "unknown"),
    ("XFCE", "6", "", "unknown"),
    ("XFCE", "5", "x11", "x11"),
    ("XFCE", "5", "wayland", "unknown"),
    ("XFCE", "5", "", "unknown"),
    ("XFCE", "", "x11", "x11"),
    ("XFCE", "", "wayland", "unknown"),
    ("XFCE", "", "", "unknown"),
)

#: Each of the nine profile tokens with an environment that proves it.
PROFILE_ENVIRONMENTS: dict[str, dict[str, str]] = {
    "kde-plasma-6-wayland": {
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
    },
    "kde-plasma-6-x11": {
        "XDG_SESSION_TYPE": "x11",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
    },
    "kde-plasma-5-wayland": {
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "5",
    },
    "kde-plasma-5-x11": {
        "XDG_SESSION_TYPE": "x11",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "5",
    },
    "kde-wayland-unverified": {
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "plasma",
    },
    "kde-x11-unverified": {
        "XDG_SESSION_TYPE": "x11",
        "XDG_CURRENT_DESKTOP": "plasma",
    },
    "gnome-wayland": {
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    },
    "x11": {
        "XDG_SESSION_TYPE": "x11",
        "XDG_CURRENT_DESKTOP": "XFCE",
    },
    "unknown": {
        "XDG_SESSION_TYPE": "tty",
        "XDG_CURRENT_DESKTOP": "XFCE",
    },
}

#: The fixed catalog copy every session-profile diagnostic carries.
UNSUPPORTED_OPERATION_MESSAGE = (
    "The installed backend does not expose this operation. Update doubletake or go back "
    "and choose an operation it reports as available."
)

#: Section 3's table: the exact session-profile details each profile adds, in
#: order. Written out per token so the probe cannot agree with itself by reading
#: the same gate set the assertion reads.
SESSION_DETAIL_TABLE: dict[str, tuple[str, ...]] = {
    "kde-plasma-6-wayland": (),
    "kde-plasma-6-x11": (
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed kde-plasma-6-x11",
    ),
    "kde-plasma-5-wayland": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; "
        "observed kde-plasma-5-wayland",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed kde-plasma-5-wayland",
    ),
    "kde-plasma-5-x11": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; "
        "observed kde-plasma-5-x11",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed kde-plasma-5-x11",
    ),
    "kde-wayland-unverified": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; "
        "observed kde-wayland-unverified",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed kde-wayland-unverified",
    ),
    "kde-x11-unverified": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; "
        "observed kde-x11-unverified",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed kde-x11-unverified",
    ),
    "gnome-wayland": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; "
        "observed gnome-wayland",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; "
        "observed gnome-wayland",
    ),
    "x11": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed x11",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed x11",
    ),
    "unknown": (
        "session-profile mirror: needs KDE Plasma 6 on Wayland or X11; observed unknown",
        "session-profile virtual-display: needs KDE Plasma 6 on Wayland; observed unknown",
    ),
}
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


_DEFAULT: object = object()


class _RaisingFinder:
    """A command finder whose lookup raises, so nothing is proven."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or RuntimeError("command lookup exploded")
        self.looked_up: list[str] = []

    def __call__(self, name: str) -> str | None:
        self.looked_up.append(name)
        raise self.error


class _MalformedFinder:
    """A command finder that returns a value that is not a path string."""

    def __init__(self, value: object = 17) -> None:
        self.value = value
        self.looked_up: list[str] = []

    def __call__(self, name: str):
        self.looked_up.append(name)
        return self.value


def scenario(
    *,
    environment: dict[str, str] | None = None,
    backend_probe: object = _DEFAULT,
    backend_features: Mapping[str, object] | None = None,
    capture_port: object = _DEFAULT,
    command: object = _DEFAULT,
    helper_probe: object = _DEFAULT,
) -> EnvironmentProbe:
    """A probe where every evidence source is a proven double unless overridden.

    ``_DEFAULT`` keeps the proven double; an explicit ``None`` means the probe
    itself is absent, which is evidence the composition never installed it.
    """
    if backend_probe is _DEFAULT:
        backend_probe = backend(Ok(value=features(**(backend_features or {}))))
    return EnvironmentProbe(
        environment=PLASMA if environment is None else environment,
        command_finder=_Missing() if command is _DEFAULT else command,
        module_finder=_Modules(),
        backend_probe=backend_probe,  # type: ignore[arg-type]
        helper_probe=helper() if helper_probe is _DEFAULT else helper_probe,  # type: ignore[arg-type]
        capture=capture() if capture_port is _DEFAULT else capture_port,  # type: ignore[arg-type]
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )


#: The evidence failures a probe can be built from, one token per probe input.
#: A scenario is the set of tokens it breaks, so first-match precedence can be
#: read off the refusal table instead of a hand-written probe per combination.
_EVIDENCE_TOKENS = frozenset(
    {
        "session",
        "capture",
        "inventory",
        "kscreen",
        "admission",
        "multi_target",
        "physical_source",
        "virtual_source",
        "helper",
    },
)


def evidence_probe(
    *,
    profile: str = "kde-plasma-6-wayland",
    broken: frozenset[str] = frozenset(),
) -> EnvironmentProbe:
    """A probe whose unproven evidence is exactly the named ``broken`` tokens."""
    assert broken <= _EVIDENCE_TOKENS, broken - _EVIDENCE_TOKENS
    environment = (
        PROFILE_ENVIRONMENTS["gnome-wayland"]
        if "session" in broken
        else PROFILE_ENVIRONMENTS[profile]
    )
    if "capture" in broken:
        capture_port: FakeCapture | None = None
    elif "inventory" in broken:
        capture_port = capture(Ok(value=()))
    else:
        capture_port = capture()
    command: object = _Missing("kscreen-doctor") if "kscreen" in broken else _Missing()
    if "helper" in broken:
        helper_probe: object = helper(
            Err(error=diagnostic(ErrorCode.dependency_incompatible)),
        )
    else:
        helper_probe = helper()
    overrides: dict[str, object] = {}
    if "admission" in broken:
        overrides.update(foreground_control=False, owner_handshake=False)
    if "multi_target" in broken:
        overrides["multi_target"] = False
    if "physical_source" in broken:
        overrides["physical_source"] = False
    if "virtual_source" in broken:
        overrides["virtual_source"] = False
    return EnvironmentProbe(
        environment=environment,
        command_finder=command,  # type: ignore[arg-type]
        module_finder=_Modules(),
        backend_probe=backend(Ok(value=features(**overrides))),
        helper_probe=helper_probe,  # type: ignore[arg-type]
        capture=capture_port,
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )


def _codes(report) -> set[ErrorCode]:
    return {item.code for item in report.diagnostics}


def _details(report) -> str:
    return " | ".join(item.detail or "" for item in report.diagnostics)


def _messages(report) -> str:
    return " | ".join(item.message for item in report.diagnostics)


# ---------------------------------------------------------------------- matrix


@pytest.mark.parametrize("capable", [False, True])
async def test_volume_report_uses_proven_backend_only_and_preserves_other_gates(capable):
    result = features(target_volume=capable)
    report = await probe(backend_probe=backend(Ok(value=result))).check()
    baseline = await probe().check()
    assert report.volume is capable
    for field in ("discovery", "mirror", "virtual_display", "audio", "remembered_pairing",
                  "additional_virtual_outputs", "diagnostics"):
        assert getattr(report, field) == getattr(baseline, field)


async def test_volume_report_check_cache_and_refresh_follow_new_proof():
    backend_probe = backend(
        Ok(value=features()), Ok(value=features(target_volume=True)),
        Err(error=diagnostic(ErrorCode.dependency_incompatible)),
    )
    builder = probe(backend_probe=backend_probe)
    legacy = await builder.check()
    assert legacy.volume is False and backend_probe.calls == 1
    assert await builder.check() is legacy
    capable = await builder.refresh()
    assert capable.volume is True and backend_probe.calls == 2
    refused = await builder.refresh()
    assert refused.volume is False and backend_probe.calls == 3
    assert refused.discovery is True


async def test_volume_report_does_not_promote_audio_or_mode_or_trust_gates():
    report = await probe(
        backend_probe=backend(Ok(value=features(target_volume=True, physical_source=False,
            virtual_source=False, owned_daemon_control=False, foreground_control=False, owner_handshake=False))),
        audio_probe=audio(Err(error=diagnostic(ErrorCode.audio_unavailable))),
    ).check()
    assert report.volume is True
    assert report.audio is report.mirror is report.virtual_display is report.remembered_pairing is False
    assert isinstance(can_start(report, Mode.MIRROR, TrustState.Trusted), Err)
    assert isinstance(can_start(report, Mode.VIRTUAL, TrustState.RevocationPending), Err)


@pytest.mark.parametrize("mode", ["capable", "legacy", "incoherent"])
async def test_volume_report_from_classified_surface(mode):
    fixtures = Path(__file__).parents[1] / "fixtures" / "backend"
    sender = (fixtures / "doubletake-0.5-source-help.txt").read_text()
    ctl = (fixtures / "doubletake-ctl-0.5-help.txt").read_text()
    tokens = frozenset({"capabilities-v1", "status-source", "status-source-complete"})
    if mode != "legacy":
        old = "  connect [target] [PIN-or-password]  Start mirroring (to target IP, or first free device)"
        new = (
            "  connect [target] [--audio on|off] [--volume <0-100>] [PIN-or-password]  Start mirroring (to target IP, or first free device)\n"
            "  volume <0-100> [target]             Set mirrored audio level (all streams, or only the given IP)\n"
            "  audio off [target]                  Close mirrored audio (all streams, or only the given IP)"
        )
        assert ctl.count(old) == 1
        ctl = ctl.replace(old, new)
    if mode == "capable":
        tokens |= {"target-volume"}
    classified = features_from_help(sender, ctl, advertised=tokens)
    report = await probe(backend_probe=backend(Ok(value=classified))).check()
    assert report.volume is (mode == "capable")
    assert report.mirror is report.virtual_display is report.remembered_pairing is False
    assert classified.owned_daemon_control is False


async def test_complete_kde_plasma_6_wayland_enables_every_proven_capability():
    report = await probe().check()

    assert report.environment == environment_label(KDE_PLASMA_6_WAYLAND)
    assert report.environment == "KDE Plasma 6 Wayland"
    assert report.discovery is True
    assert report.mirror is True
    assert report.virtual_display is True
    assert report.audio is True
    assert report.remembered_pairing is True
    assert report.additional_virtual_outputs is True
    assert report.mirror_refusal is None
    assert report.virtual_display_refusal is None
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
    "plausible",
    ["unverified", "true", 1, [True], object()],
    ids=["string", "true-text", "integer", "list", "object"],
)
async def test_a_non_boolean_output_claim_never_enables_an_extra_output(plausible):
    """``HelperFeatures`` has no runtime validation, so a malformed provider can
    hand policy a truthy non-boolean. Only the exact ``True`` proves distinct
    concurrent outputs; anything else withholds the *additional* output while
    the first Virtual output stays available."""
    malformed = Ok(value=HelperFeatures(
        version="24.08.2", distinct_outputs=plausible,  # type: ignore[arg-type]
        capability_fingerprint=FINGERPRINT,
    ))
    report = await probe(helper_probe=helper(malformed)).check()

    assert report.additional_virtual_outputs is False
    assert report.virtual_display is True
    assert report.mirror is True


async def test_an_exact_boolean_True_enables_additional_outputs():
    report = await probe(helper_probe=helper(Ok(value=HelperFeatures(
        version="24.08.2", distinct_outputs=True, capability_fingerprint=FINGERPRINT,
    )))).check()

    assert report.additional_virtual_outputs is True


class _StrSubclass(str):
    """A ``str`` subclass, which is not an exact built-in ``str`` value."""

    __slots__ = ()


def _environment_for(desktop: str, version: str, session: str) -> dict[str, str]:
    """The environment mapping for one row of the normalized profile matrix."""
    values = {"XDG_CURRENT_DESKTOP": desktop}
    if version:
        values["KDE_SESSION_VERSION"] = version
    if session:
        values["XDG_SESSION_TYPE"] = session
    return values


@pytest.mark.parametrize(
    ("desktop", "version", "session", "expected"),
    PROFILE_MATRIX,
    ids=[
        f"{desktop}-{version or 'unverified'}-{session or 'other'}"
        for desktop, version, session, _ in PROFILE_MATRIX
    ],
)
def test_classify_environment_covers_the_complete_profile_matrix(
    desktop: str, version: str, session: str, expected: str,
):
    """All 27 normalized desktop/version/session cells, exactly as tabulated."""
    assert len(PROFILE_MATRIX) == 27
    assert len({row[:3] for row in PROFILE_MATRIX}) == 27
    assert {row[3] for row in PROFILE_MATRIX} == set(PROFILE_ENVIRONMENTS)

    assert classify_environment(_environment_for(desktop, version, session)) == expected


def test_classify_environment_normalizes_all_three_session_values():
    """Trim/lower/colon rules, KDE precedence, and the exact non-string rule."""
    # Session: strip and lowercase, and never colon-split.
    assert classify_environment({
        "XDG_SESSION_TYPE": " X11 ",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
    }) == "kde-plasma-6-x11"
    assert classify_environment({
        "XDG_SESSION_TYPE": " WayLand ",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
    }) == "kde-plasma-6-wayland"
    assert classify_environment({
        "XDG_SESSION_TYPE": "x11:wayland",
        "XDG_CURRENT_DESKTOP": "KDE",
        "KDE_SESSION_VERSION": "6",
    }) == "unknown"

    # Desktop: colon-separated whole tokens, each stripped and lowercased.
    assert classify_environment({
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
    }) == "gnome-wayland"
    assert classify_environment({
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": "kde4:gnome-shell",
        "KDE_SESSION_VERSION": "6",
    }) == "unknown"
    # KDE/Plasma wins over a GNOME token, and the version is trimmed, not parsed.
    assert classify_environment({
        "XDG_SESSION_TYPE": "wayland",
        "XDG_CURRENT_DESKTOP": " gNoMe : pLaSmA ",
        "KDE_SESSION_VERSION": " 6 ",
    }) == "kde-plasma-6-wayland"
    assert classify_environment({
        "XDG_SESSION_TYPE": "x11",
        "XDG_CURRENT_DESKTOP": " gNoMe : pLaSmA ",
        "KDE_SESSION_VERSION": " 6 ",
    }) == "kde-plasma-6-x11"

    # Version: only the exact trimmed "5" and "6" are verified versions.
    for version in ("7", "06", "6.0", "six", "  "):
        assert classify_environment({
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "KDE_SESSION_VERSION": version,
        }) == "kde-wayland-unverified", version
        assert classify_environment({
            "XDG_SESSION_TYPE": "x11",
            "XDG_CURRENT_DESKTOP": "KDE",
            "KDE_SESSION_VERSION": version,
        }) == "kde-x11-unverified", version

    # A non-string value is empty text, separately for each variable.
    for value in (None, 7, object(), _StrSubclass("x11")):
        assert classify_environment({
            "XDG_SESSION_TYPE": value,
            "XDG_CURRENT_DESKTOP": "KDE",
            "KDE_SESSION_VERSION": "6",
        }) == "unknown", value
    for value in (None, 7, object(), _StrSubclass("KDE")):
        assert classify_environment({
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": value,
            "KDE_SESSION_VERSION": "6",
        }) == "unknown", value
        assert classify_environment({
            "XDG_SESSION_TYPE": "x11",
            "XDG_CURRENT_DESKTOP": value,
            "KDE_SESSION_VERSION": "6",
        }) == "x11", value
    for value in (None, 7, object(), _StrSubclass("6")):
        assert classify_environment({
            "XDG_SESSION_TYPE": "wayland",
            "XDG_CURRENT_DESKTOP": "KDE",
            "KDE_SESSION_VERSION": value,
        }) == "kde-wayland-unverified", value
        assert classify_environment({
            "XDG_SESSION_TYPE": "x11",
            "XDG_CURRENT_DESKTOP": "KDE",
            "KDE_SESSION_VERSION": value,
        }) == "kde-x11-unverified", value

    with pytest.raises(TypeError):
        classify_environment(["XDG_SESSION_TYPE"])  # type: ignore[arg-type]


@pytest.mark.parametrize("profile", sorted(PROFILE_ENVIRONMENTS))
async def test_per_mode_environment_diagnostics_have_exact_details_and_counts(
    profile: str,
):
    report = await probe(environment=PROFILE_ENVIRONMENTS[profile]).check()

    expected = list(SESSION_DETAIL_TABLE[profile])

    environment_diagnostics = [
        item for item in report.diagnostics
        if (item.detail or "").startswith(SESSION_PROFILE_DETAIL_PREFIX)
    ]
    assert [item.detail for item in environment_diagnostics] == expected
    assert len(environment_diagnostics) == len(expected)
    # Nothing else is unproven in this fixture, so these are the whole report.
    assert report.diagnostics == tuple(environment_diagnostics)

    for item in environment_diagnostics:
        assert item.code is ErrorCode.unsupported_operation
        assert item.message == UNSUPPORTED_OPERATION_MESSAGE
        assert item.recovery == (RecoveryAction.retry, RecoveryAction.back)
        assert item.receiver_id is None

    # The withdrawn generic environment diagnostic (the bare token) is absent.
    assert profile not in {item.detail for item in report.diagnostics}


async def test_mode_environment_sets_gate_mirror_and_virtual_display_independently():
    """Membership decides each mode's session gate, never the other mode's."""
    # The two closed sets, exactly as the specification tabulates them.
    assert MIRROR_ENVIRONMENTS == {"kde-plasma-6-wayland", "kde-plasma-6-x11"}
    assert VIRTUAL_DISPLAY_ENVIRONMENTS == {"kde-plasma-6-wayland"}

    for profile, environment in PROFILE_ENVIRONMENTS.items():
        report = await probe(environment=environment).check()

        assert report.mirror is (profile in MIRROR_ENVIRONMENTS), profile
        assert report.virtual_display is (
            profile in VIRTUAL_DISPLAY_ENVIRONMENTS
        ), profile
        if report.mirror:
            assert report.mirror_refusal is None, profile
        else:
            assert report.mirror_refusal is ModeRefusal.session, profile
        if report.virtual_display:
            assert report.virtual_display_refusal is None, profile
        else:
            assert report.virtual_display_refusal is ModeRefusal.session, profile

    # With every other proof complete, each mode's own evidence is removed once.
    isolation = (
        (
            "mode-ready",
            {"backend_probe": backend(Ok(value=features(multi_target=False)))},
            ModeRefusal.admission,
            ModeRefusal.admission,
        ),
        (
            "mirror-absent-provider",
            {"capture_port": None},
            ModeRefusal.dependency,
            None,
        ),
        (
            "mirror-physical-source",
            {"backend_probe": backend(Ok(value=features(physical_source=False)))},
            ModeRefusal.backend,
            None,
        ),
        (
            "mirror-empty-inventory",
            {"capture_port": capture(Ok(value=()))},
            ModeRefusal.inventory,
            None,
        ),
        (
            "virtual-kscreen",
            {"command": _Missing("kscreen-doctor")},
            None,
            ModeRefusal.dependency,
        ),
        (
            "virtual-absent-helper",
            {"helper_probe": None},
            None,
            ModeRefusal.helper,
        ),
        (
            "virtual-source",
            {"backend_probe": backend(Ok(value=features(virtual_source=False)))},
            None,
            ModeRefusal.backend,
        ),
    )
    for name, knobs, mirror_refusal, virtual_refusal in isolation:
        report = await scenario(**knobs).check()  # type: ignore[arg-type]

        assert report.mirror is (mirror_refusal is None), name
        assert report.mirror_refusal is mirror_refusal, name
        assert report.virtual_display is (virtual_refusal is None), name
        assert report.virtual_display_refusal is virtual_refusal, name


#: Each mode's closed refusal set, in exactly the order it is evaluated.
MIRROR_REFUSAL_ORDER: tuple[ModeRefusal, ...] = (
    ModeRefusal.session,
    ModeRefusal.dependency,
    ModeRefusal.admission,
    ModeRefusal.backend,
    ModeRefusal.inventory,
)
VIRTUAL_REFUSAL_ORDER: tuple[ModeRefusal, ...] = (
    ModeRefusal.session,
    ModeRefusal.dependency,
    ModeRefusal.admission,
    ModeRefusal.backend,
    ModeRefusal.helper,
)

#: The evidence token that breaks each row of Mirror's refusal table.
MIRROR_ROW_TOKENS: tuple[frozenset[str], ...] = (
    frozenset({"session"}),
    frozenset({"capture"}),
    frozenset({"admission"}),
    frozenset({"physical_source"}),
    frozenset({"inventory"}),
)
#: The evidence token that breaks each row of Virtual display's table.
VIRTUAL_ROW_TOKENS: tuple[frozenset[str], ...] = (
    frozenset({"session"}),
    frozenset({"kscreen"}),
    frozenset({"multi_target"}),
    frozenset({"virtual_source"}),
    frozenset({"helper"}),
)


async def test_mode_refusal_uses_the_closed_precedence_table():
    """Every row is reachable, and simultaneous failures resolve by first match.

    The ten pairwise unions per mode are the complete pairwise cross product of
    each table, so a row that could be shadowed by an earlier one is exercised
    exactly as the ordered table says it must resolve.
    """
    # Every single refusal is reachable from its own evidence failure.
    for row, refusal in enumerate(MIRROR_REFUSAL_ORDER):
        report = await evidence_probe(broken=MIRROR_ROW_TOKENS[row]).check()
        assert report.mirror is False, refusal
        assert report.mirror_refusal is refusal
    for row, refusal in enumerate(VIRTUAL_REFUSAL_ORDER):
        report = await evidence_probe(broken=VIRTUAL_ROW_TOKENS[row]).check()
        assert report.virtual_display is False, refusal
        assert report.virtual_display_refusal is refusal

    # All ten pairwise unions per mode, each resolving to the earlier row.
    for earlier in range(len(MIRROR_REFUSAL_ORDER)):
        for later in range(earlier + 1, len(MIRROR_REFUSAL_ORDER)):
            report = await evidence_probe(
                broken=MIRROR_ROW_TOKENS[earlier] | MIRROR_ROW_TOKENS[later],
            ).check()
            assert report.mirror_refusal is MIRROR_REFUSAL_ORDER[earlier], (
                earlier, later,
            )
    for earlier in range(len(VIRTUAL_REFUSAL_ORDER)):
        for later in range(earlier + 1, len(VIRTUAL_REFUSAL_ORDER)):
            report = await evidence_probe(
                broken=VIRTUAL_ROW_TOKENS[earlier] | VIRTUAL_ROW_TOKENS[later],
            ).check()
            assert report.virtual_display_refusal is VIRTUAL_REFUSAL_ORDER[earlier], (
                earlier, later,
            )

    # Backend probes: absent, failed, malformed raw, malformed ``Ok`` and no
    # admission all resolve through ``mode_ready`` to ``admission``.
    backend_failures = (
        ("probe-absent", None),
        (
            "probe-error",
            backend(Err(error=diagnostic(ErrorCode.dependency_incompatible))),
        ),
        ("malformed-raw-result", backend("doubletake looks fine")),
        ("malformed-ok", backend(Ok(value={"profile": "doubletake-0.5"}))),
        (
            "no-admission",
            backend(Ok(value=features(foreground_control=False, owner_handshake=False))),
        ),
        (
            "missing-multi-target",
            backend(Ok(value=features(multi_target=False))),
        ),
    )
    for name, backend_probe in backend_failures:
        report = await scenario(backend_probe=backend_probe).check()
        assert report.mirror_refusal is ModeRefusal.admission, name
        assert report.virtual_display_refusal is ModeRefusal.admission, name

    # A source-only backend failure is ``backend``, never ``admission``.
    for name, overrides, mirror_refusal, virtual_refusal in (
        ("physical-source", {"physical_source": False}, ModeRefusal.backend, None),
        ("virtual-source", {"virtual_source": False}, None, ModeRefusal.backend),
    ):
        report = await scenario(
            backend_probe=backend(Ok(value=features(**overrides))),
        ).check()
        assert report.mirror_refusal is mirror_refusal, name
        assert report.virtual_display_refusal is virtual_refusal, name

    # Capture inventory: absent provider, error, malformed raw, malformed ``Ok``
    # and an empty tuple all withhold Mirror; only the absent provider is
    # ``dependency``.
    for name, capture_port, expected in (
        ("provider-absent", None, ModeRefusal.dependency),
        (
            "provider-error",
            capture(Err(error=diagnostic(ErrorCode.portal_denied))),
            ModeRefusal.inventory,
        ),
        ("malformed-raw-result", capture("one monitor"), ModeRefusal.inventory),
        ("malformed-ok", capture(Ok(value="DP-1")), ModeRefusal.inventory),
        ("empty-tuple", capture(Ok(value=())), ModeRefusal.inventory),
    ):
        report = await scenario(capture_port=capture_port).check()
        assert report.mirror_refusal is expected, name
        assert report.virtual_display_refusal is None, name

    # A KScreen lookup that finds nothing, raises, or returns a malformed value
    # is unproven in all three shapes and refuses Virtual display as dependency.
    for name, command in (
        ("command-absent", _Missing("kscreen-doctor")),
        ("finder-raises", _RaisingFinder()),
        ("malformed-result", _MalformedFinder()),
        ("empty-result", _MalformedFinder("")),
    ):
        report = await scenario(command=command).check()
        assert report.virtual_display_refusal is ModeRefusal.dependency, name
        assert report.mirror_refusal is None, name

    # Helper probe: absent, error, malformed raw and malformed ``Ok`` are
    # ``helper``; a valid ``HelperFeatures`` proves the contract.
    for name, helper_probe in (
        ("helper-absent", None),
        ("helper-error", helper(Err(error=diagnostic(ErrorCode.dependency_missing)))),
        ("helper-malformed-raw", helper("24.08.2")),
        ("helper-malformed-ok", helper(Ok(value={"distinct_outputs": True}))),
    ):
        report = await scenario(helper_probe=helper_probe).check()
        assert report.virtual_display_refusal is ModeRefusal.helper, name
        assert report.mirror_refusal is None, name

    valid = await scenario(helper_probe=helper()).check()
    assert valid.virtual_display_refusal is None
    assert valid.virtual_display is True
    assert valid.mirror_refusal is None

    # No unreachable pair exists in either closed set.
    assert set(MIRROR_REFUSAL_ORDER) == {
        ModeRefusal.session,
        ModeRefusal.dependency,
        ModeRefusal.admission,
        ModeRefusal.backend,
        ModeRefusal.inventory,
    }
    assert set(VIRTUAL_REFUSAL_ORDER) == {
        ModeRefusal.session,
        ModeRefusal.dependency,
        ModeRefusal.admission,
        ModeRefusal.backend,
        ModeRefusal.helper,
    }


class _HelperSource:
    """The composition seam's helper shape: only ``.probe`` is consumed."""

    def __init__(self, probe: Callable[[], object] | None) -> None:
        self.probe = probe


class _RawOkCapture:
    """A capture provider whose ``Ok`` payload is not a monitor inventory."""

    async def list_physical(self):
        return Ok(value="DP-1")


class _RawResultCapture:
    """A capture provider that returns something that is not a ``Result``."""

    async def list_physical(self):
        return "one physical monitor"


#: Backend evidence cases: absent probe, typed failure, malformed results, and
#: every partial proof the refusal table distinguishes.
BACKEND_CASE_FACTORIES: dict[str, Callable[[], object]] = {
    "probe-absent": lambda: None,
    "probe-error": lambda: backend(Err(error=diagnostic(ErrorCode.dependency_incompatible))),
    "malformed-raw-result": lambda: backend("doubletake-0.5"),
    "malformed-ok": lambda: backend(Ok(value=("doubletake-0.5", "0.5.0"))),
    "no-admission": lambda: backend(
        Ok(value=features(foreground_control=False, owner_handshake=False)),
    ),
    "no-multi-target": lambda: backend(Ok(value=features(multi_target=False))),
    "no-physical-source": lambda: backend(Ok(value=features(physical_source=False))),
    "no-virtual-source": lambda: backend(Ok(value=features(virtual_source=False))),
    "all-mode-evidence": lambda: backend(Ok(value=features())),
}

#: Capture evidence cases, each a fresh provider so no script is consumed twice.
CAPTURE_CASE_FACTORIES: dict[str, Callable[[], object]] = {
    "provider-absent": lambda: None,
    "provider-error": lambda: capture(Err(error=diagnostic(ErrorCode.portal_denied))),
    "malformed-raw-result": lambda: _RawResultCapture(),
    "malformed-ok": lambda: _RawOkCapture(),
    "empty-inventory": lambda: capture(Ok(value=())),
    "one-physical-monitor": lambda: capture(),
}

#: KScreen command outcomes, from the same ``_command`` evidence ``_probe`` uses.
KSCREEN_CASE_FACTORIES: dict[str, Callable[[], object]] = {
    "found": lambda: _Missing(),
    "finder-raises": lambda: _RaisingFinder(),
    "malformed-result": lambda: _MalformedFinder(),
}

#: Helper evidence cases, including a valid ``HelperFeatures`` contract.
HELPER_CASE_FACTORIES: dict[str, Callable[[], object]] = {
    "probe-absent": lambda: None,
    "probe-error": lambda: helper(Err(error=diagnostic(ErrorCode.dependency_missing))),
    "malformed-raw-result": lambda: helper("24.08.2"),
    "malformed-ok": lambda: helper(Ok(value={"distinct_outputs": True})),
    "valid-features": lambda: helper(),
}


async def test_probe_mode_refusals_are_total_over_the_evidence_matrix():
    """Every environment/evidence combination yields one closed mode/refusal pair.

    ``9 x 9 x 6 x 3 x 5`` reports are produced from injected probes only, so no
    host command runs, no module is imported, and no D-Bus call is made.
    ``EnvironmentProbe`` at this seam owns no process runner at all; the runner
    the composition installs is exercised by the bootstrap test below.
    """
    reports = 0
    for profile in sorted(PROFILE_ENVIRONMENTS):
        for backend_name, make_backend in BACKEND_CASE_FACTORIES.items():
            for capture_name, make_capture in CAPTURE_CASE_FACTORIES.items():
                for kscreen_name, make_command in KSCREEN_CASE_FACTORIES.items():
                    for helper_name, make_helper in HELPER_CASE_FACTORIES.items():
                        builder = EnvironmentProbe(
                            environment=PROFILE_ENVIRONMENTS[profile],
                            command_finder=make_command(),  # type: ignore[arg-type]
                            module_finder=_Modules(),
                            backend_probe=make_backend(),  # type: ignore[arg-type]
                            helper_probe=make_helper(),  # type: ignore[arg-type]
                            capture=make_capture(),  # type: ignore[arg-type]
                            audio_probe=audio(),
                            keyring_probe=_Keyring(),
                            dbus=_FixedDBus(frozenset()),
                        )
                        report = await builder.check()
                        reports += 1
                        case = (
                            profile, backend_name, capture_name, kscreen_name,
                            helper_name,
                        )
                        assert report.mirror is (report.mirror_refusal is None), case
                        assert report.virtual_display is (
                            report.virtual_display_refusal is None
                        ), case
                        if report.mirror_refusal is not None:
                            assert report.mirror_refusal in set(
                                MIRROR_REFUSAL_ORDER
                            ), case
                        if report.virtual_display_refusal is not None:
                            assert report.virtual_display_refusal in set(
                                VIRTUAL_REFUSAL_ORDER
                            ), case

    assert reports == 9 * 9 * 6 * 3 * 5 == 7290


async def test_bootstrap_probe_without_capture_keeps_mirror_false_on_x11():
    """``--check-environment`` composes no capture provider, so Mirror stays off.

    It prints the human-readable session label and the physical-provider
    limitation. Its ``mirror=False`` report is not a capture failure.
    """
    environment = PROFILE_ENVIRONMENTS["kde-plasma-6-x11"]
    process = FakeProcess()
    process.queue_result(
        "run",
        Err(error=diagnostic(ErrorCode.dependency_missing, command="doubletake")),
    )
    process.queue_result(
        "run",
        Err(error=diagnostic(
            ErrorCode.dependency_missing, command="krfb-virtualmonitor",
        )),
    )
    builder = EnvironmentProbe.from_environment(
        process=process,
        helper=VirtualHelper(process, FakeClock()),
        environment=environment,
        audio_probe=audio(),
        keyring_probe=_Keyring(),
    )

    report = await builder.check()

    assert classify_environment(environment) == "kde-plasma-6-x11"
    assert report.environment == "KDE Plasma 6 X11"
    assert report.mirror is False
    assert report.mirror_refusal is ModeRefusal.dependency
    assert "physical capture provider" in {
        item.detail for item in report.diagnostics
    }
    assert [spec.argv[0] for spec, _ in process.runs] == [
        "doubletake", "krfb-virtualmonitor",
    ]


@pytest.mark.parametrize(
    ("environment", "token", "mirror"),
    [
        ({**PLASMA, "XDG_SESSION_TYPE": "x11"}, "kde-plasma-6-x11", True),
        ({**PLASMA, "XDG_CURRENT_DESKTOP": "GNOME"}, "gnome-wayland", False),
        ({"XDG_SESSION_TYPE": "wayland"}, "unknown", False),
        ({**PLASMA, "KDE_SESSION_VERSION": "5"}, "kde-plasma-5-wayland", False),
        ({**PLASMA, "KDE_SESSION_VERSION": ""}, "kde-wayland-unverified", False),
    ],
)
async def test_other_or_unproven_desktops_are_reported_separately_and_gated(
    environment: dict[str, str], token: str, mirror: bool,
):
    report = await probe(environment=environment).check()

    assert classify_environment(environment) == token
    assert report.environment == environment_label(token)
    assert report.mirror is mirror
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
    # Each missing admission prerequisite is named exactly, so a user knows
    # which backend operation to fix rather than seeing a generic failure.
    unavailable = {
        item.detail for item in report.diagnostics
        if item.code is ErrorCode.dependency_incompatible
    }
    assert {"foreground_control", "owner_handshake"} <= unavailable
    assert "keyring" not in unavailable
    assert "multi_target" not in unavailable


async def test_a_backend_without_its_keyring_operation_never_admits_a_mode():
    """The transport's ``_admit`` refuses every start without this operation.

    This backend keyring *operation* flag is not the desktop Secret Service
    probe: pairing additionally needs the service, while the mode gate only
    needs the transport's proven operation.
    """
    report = await probe(
        backend_probe=backend(Ok(value=features(keyring=False))),
    ).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.additional_virtual_outputs is False
    assert report.remembered_pairing is False
    assert report.discovery is True
    assert any(
        item.code is ErrorCode.dependency_incompatible and item.detail == "keyring"
        for item in report.diagnostics
    )


async def test_a_backend_without_multi_target_loses_streaming_but_keeps_pairing():
    """``start_stream`` refuses without multi-target; pairing never needs it."""
    report = await probe(
        backend_probe=backend(Ok(value=features(multi_target=False))),
    ).check()

    assert report.mirror is False
    assert report.virtual_display is False
    assert report.additional_virtual_outputs is False
    assert report.remembered_pairing is True
    assert report.discovery is True
    assert any(
        item.code is ErrorCode.dependency_incompatible
        and item.detail == "multi_target"
        for item in report.diagnostics
    )
    assert isinstance(can_start(report, Mode.MIRROR, TrustState.Trusted), Err)
    assert isinstance(can_start(report, Mode.VIRTUAL, TrustState.Trusted), Err)


async def test_unproven_backend_trust_transactions_disable_remembered_pairing_alone():
    # ``provisional_commit`` is a pairing-only operation: the real transport's
    # admission and start gates never consult it, so it must not affect a mode.
    report = await probe(
        backend_probe=backend(Ok(value=features(provisional_commit=False))),
    ).check()

    assert report.remembered_pairing is False
    assert report.mirror is True
    assert report.virtual_display is True
    assert any(
        item.code is ErrorCode.dependency_incompatible
        and item.detail == "target_bound_pairing"
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
    # ``provisional_commit`` is a pairing-only operation: the real transport's
    # admission and start gates never consult it, so it must not affect a mode.
    report = await probe(
        backend_probe=backend(Ok(value=features(provisional_commit=False))),
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


async def test_an_audio_permission_update_never_releases_an_existing_video_capture():
    """Recording an audio outcome is not a video teardown.

    The startup probe only ever enumerates; an acquired physical capture belongs
    to a running video path and must survive an audio denial, a later retry, and
    the re-probe that each update forces. The assertion is on the exact resource
    identity, not on the capability booleans alone.
    """
    capture_port = capture()
    acquired = await capture_port.select("DP-1")
    assert isinstance(acquired, Ok)
    assert acquired.value == MONITOR
    builder = probe(capture_port=capture_port)

    first = await builder.check()
    assert first.mirror is True

    builder.register_audio_probe_capability(
        False, "org.freedesktop.portal.ScreenCast permission",
    )
    denied = await builder.check()
    assert denied.audio is False
    assert capture_port.released == []
    assert "capture.release" not in capture_port.journal.names()

    # The user's Retry action re-probes and records the new outcome.
    builder.register_audio_probe_capability(True)
    retried = await builder.refresh()

    assert retried.audio is True
    for report in (first, denied, retried):
        assert report.mirror is True
        assert report.virtual_display is True
    assert capture_port.selections == ["DP-1"]
    assert capture_port.released == []
    assert "capture.release" not in capture_port.journal.names()
    assert capture_port.monitors == {"DP-1": MONITOR}


async def test_an_audio_permission_update_never_verifies_or_restarts_the_capture():
    """A permission update adds no capture interaction beyond re-enumeration."""
    capture_port = capture()
    builder = probe(capture_port=capture_port)
    await builder.check()
    capture_port.journal.clear()

    builder.register_audio_probe_capability(False)
    await builder.check()
    builder.register_audio_probe_capability(True)
    await builder.refresh()

    assert capture_port.journal.names() == ["capture.list_physical", "capture.list_physical"]
    assert capture_port.selections == []
    assert capture_port.released == []


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

    assert report.environment == environment_label(KDE_PLASMA_6_WAYLAND)
    assert report.environment == "KDE Plasma 6 Wayland"
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
