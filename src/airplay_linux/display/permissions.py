"""Read and clear the desktop's remembered ScreenCast approvals.

xdg-desktop-portal remembers the screen a user picked for one application id and
replays it for later requests. That memory is why a capture can keep landing on
a monitor nobody selected any more: the request carries no monitor, so the
portal's stored answer decides, and the dialog never appears. The portal exposes
no revocation call of its own, so the supported interface is the permission
store it writes through flatpak.

This module only reports and removes those rows. It never decides which screen
is captured: the portal owns that, and the caller's own request cannot name one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.ports import CommandOutput, CommandSpec, ProcessPort
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result

#: The permission-store table the ScreenCast portal writes.
SCREENCAST_TABLE: Final[str] = "screencast"
#: A permission listing is local state; it must never delay the dashboard.
PERMISSION_TIMEOUT: Final[float] = 10.0


@dataclass(frozen=True, kw_only=True)
class RememberedScreen:
    """One stored ScreenCast approval: the restore token and the app it belongs to."""

    token: str
    app_id: str


def parse_permissions(text: str, *, table: str = SCREENCAST_TABLE) -> tuple[RememberedScreen, ...]:
    """Parse ``flatpak permissions`` output, keeping only this table's rows.

    Columns are tab separated: table, id, application id, permission, data. Rows
    for another table, a missing id, or a malformed line are not approvals this
    application can reason about, so they are dropped rather than guessed at.
    """
    if type(text) is not str:
        return ()
    remembered: list[RememberedScreen] = []
    for line in text.splitlines():
        columns = line.split("\t")
        if len(columns) < 3 or columns[0] != table:
            continue
        token = columns[1].strip()
        app_id = columns[2].strip()
        if not token:
            continue
        remembered.append(RememberedScreen(token=token, app_id=app_id))
    return tuple(remembered)


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


class FlatpakScreenCastPermissions:
    """The permission store's own interface: list approvals, remove them by token."""

    def __init__(
        self, process: ProcessPort, *, executable: str = "flatpak",
        timeout: float = PERMISSION_TIMEOUT,
    ) -> None:
        if type(executable) is not str or not executable or "\0" in executable:
            raise ValueError("invalid permission-store executable")
        if type(timeout) is not float or not timeout > 0:
            raise ValueError("timeout must be a positive number")
        self._process = process
        self._executable = executable
        self._timeout = timeout

    async def _run(self, *arguments: str) -> Result[CommandOutput]:
        try:
            ran = await self._process.run(
                CommandSpec(argv=(self._executable, *arguments), env={}),
                self._timeout,
            )
        except Exception:
            return _failure(ErrorCode.dependency_missing)
        if isinstance(ran, Err):
            # A missing or unrunnable executable is the only cause this adapter
            # can name; the runner's own diagnostic stays out of the operator's
            # view.
            return _failure(ErrorCode.dependency_missing)
        return ran

    async def available(self) -> bool:
        """Whether the permission store can be reached at all."""
        ran = await self._run("--version")
        return isinstance(ran, Ok) and ran.value.exit_code == 0

    async def remembered(self) -> Result[tuple[RememberedScreen, ...]]:
        ran = await self._run("permissions")
        if isinstance(ran, Err):
            return ran
        if ran.value.exit_code != 0:
            return _failure(ErrorCode.dependency_missing)
        return Ok(value=parse_permissions(ran.value.stdout.decode("utf-8", "replace")))

    async def forget_all(self) -> Result[int]:
        """Remove every remembered ScreenCast approval; returns how many went away."""
        listed = await self.remembered()
        if isinstance(listed, Err):
            return listed
        removed = 0
        for entry in listed.value:
            ran = await self._run("permission-remove", SCREENCAST_TABLE, entry.token)
            if isinstance(ran, Err):
                return ran
            if ran.value.exit_code != 0:
                return _failure(ErrorCode.persistence_failed)
            removed += 1
        return Ok(value=removed)
