"""Opt-in, read-only KDE Plasma 6 X11 checks (never part of the ordinary suite).

Nothing in this module runs without ``AIRPLAY_LIVE_X11=1`` **and** a confirmed
KDE Plasma 6 X11 session; otherwise every test skips before a host command is
looked up.  The three checks only observe:

* the classifier's profile token for this session;
* one ``kscreen-doctor -j`` inventory parsed by the existing physical-connector
  projection;
* one ``busctl`` read of the ScreenCast portal's advertised source types.

No output is created, no capture is selected or requested, and no stream is
started.

Manual command::

    AIRPLAY_LIVE_X11=1 pytest -q tests/environment/test_live_x11.py
"""

from __future__ import annotations

import os
import re

import pytest

from airplay_linux.capture.physical import (
    InventoryProbeError,
    KscreenConnectorInventory,
)
from airplay_linux.capture.portal import SCREEN_CAST_MONITOR
from airplay_linux.domain.ports import CommandSpec
from airplay_linux.domain.resources import Ok
from airplay_linux.policy.capabilities import classify_environment
from airplay_linux.process.linux import LinuxProcessRunner

LIVE_X11_ENV = "AIRPLAY_LIVE_X11"

#: The only profile token a KDE Plasma 6 X11 session may classify as.
PROFILE_TOKEN = "kde-plasma-6-x11"

#: The exact read-only portal introspection; it never requests capture.
PORTAL_SOURCE_TYPES_PROBE = (
    "busctl", "--user", "get-property",
    "org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
    "org.freedesktop.portal.ScreenCast", "AvailableSourceTypes",
)

#: Every probe here is a local observation and shares the control deadline.
PROBE_TIMEOUT = 5.0

#: ``busctl get-property`` renders one unsigned integer as ``u <value>``.
_U_INTEGER = re.compile(r"u\s+([0-9]+)")


def _skip_reason() -> str | None:
    """Why this module must skip, or ``None`` for a confirmed target session.

    Only environment reads happen here: the guard must decide before any host
    command is looked up or run.
    """
    if os.environ.get(LIVE_X11_ENV) != "1":
        return (
            f"live KDE Plasma 6 X11 checks are opt-in; set {LIVE_X11_ENV}=1 to run "
            "them against this KDE Plasma 6 X11 session"
        )
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session != "x11":
        return (
            "the current session is not X11 "
            f"(XDG_SESSION_TYPE={session or 'unset'!r})"
        )
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
    tokens = {token.strip().lower() for token in desktop.split(":")}
    if "kde" not in tokens and "plasma" not in tokens:
        return (
            "the current desktop is not KDE Plasma "
            f"(XDG_CURRENT_DESKTOP={desktop or 'unset'!r})"
        )
    version = os.environ.get("KDE_SESSION_VERSION", "").strip()
    if version != "6":
        return (
            "the current KDE session is not Plasma 6 "
            f"(KDE_SESSION_VERSION={version or 'unset'!r})"
        )
    return None


@pytest.fixture(scope="module")
def live_x11():
    """The one guard every check requests; it skips before any host command."""
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)
    return True


def test_live_x11_profile_classifies_as_kde_plasma_6_x11(live_x11):
    assert classify_environment(os.environ) == PROFILE_TOKEN


async def test_live_x11_physical_inventory_is_nonempty(live_x11):
    inventory = KscreenConnectorInventory(LinuxProcessRunner())
    try:
        connectors = await inventory()
    except InventoryProbeError as error:
        pytest.fail(
            f"kscreen-doctor did not produce a verified physical inventory: {error}"
        )
    assert isinstance(connectors, tuple)
    assert connectors, "the live compositor reported no verified physical connector"
    assert all(type(connector) is str and connector for connector in connectors)


def _advertised_source_types(output) -> int:
    text = output.stdout.decode("utf-8", "replace").strip()
    match = _U_INTEGER.fullmatch(text)
    assert match is not None, (
        f"the portal returned an unexpected AvailableSourceTypes value: {text!r}"
    )
    return int(match.group(1))


async def test_live_x11_portal_advertises_monitor(live_x11):
    result = await LinuxProcessRunner().run(
        CommandSpec(argv=PORTAL_SOURCE_TYPES_PROBE, env={}), PROBE_TIMEOUT,
    )
    assert isinstance(result, Ok), result.error.message
    output = result.value
    assert output.exit_code == 0, (
        "busctl could not read the ScreenCast AvailableSourceTypes property: "
        + output.stderr.decode("utf-8", "replace").strip()
    )
    advertised = _advertised_source_types(output)
    assert (advertised & SCREEN_CAST_MONITOR) != 0
