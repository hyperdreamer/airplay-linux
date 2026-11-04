"""The remembered ScreenCast approvals: read them, then remove them by token."""

from __future__ import annotations

from collections import deque

import pytest

from airplay_linux.display.permissions import (
    FlatpakScreenCastPermissions,
    RememberedScreen,
    parse_permissions,
)
from airplay_linux.domain.ports import CommandOutput, CommandSpec
from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.resources import Err, ErrorCode, Ok


class FakeProcess:
    """A runner that replays canned outcomes and journals every argv."""

    def __init__(self, *results: object) -> None:
        self.results = deque(results)
        self.calls: list[tuple[str, ...]] = []

    async def run(self, spec: CommandSpec, timeout: float, stdin: bytes | None = None):
        self.calls.append(spec.argv)
        if not self.results:
            raise AssertionError(f"unexpected command: {spec.argv}")
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def _output(text: str, *, code: int = 0) -> Ok:
    return Ok(value=CommandOutput(exit_code=code, stdout=text.encode(), stderr=b""))


PERMISSIONS = (
    "screencast\tabc123\t\tyes\t('KDE', 1, <...>)\n"
    "screencast\tdef456\tcom.example.App\tyes\t('KDE', 1, <...>)\n"
    "screenshot\tghi789\t\tyes\t0x00\n"
    "screencast\t\t\t\tbroken row\n"
    "not-a-row\n"
)


def test_parse_keeps_only_screencast_rows_with_a_token():
    parsed = parse_permissions(PERMISSIONS)

    assert parsed == (
        RememberedScreen(token="abc123", app_id=""),
        RememberedScreen(token="def456", app_id="com.example.App"),
    )


@pytest.mark.asyncio
async def test_remembered_reads_the_permission_store():
    process = FakeProcess(_output(PERMISSIONS))
    store = FlatpakScreenCastPermissions(process)

    result = await store.remembered()

    assert isinstance(result, Ok)
    assert [entry.token for entry in result.value] == ["abc123", "def456"]
    assert process.calls == [("flatpak", "permissions")]


@pytest.mark.asyncio
async def test_forget_all_removes_each_token_and_reports_the_count():
    process = FakeProcess(
        _output(PERMISSIONS),
        _output(""),
        _output(""),
    )
    store = FlatpakScreenCastPermissions(process)

    result = await store.forget_all()

    assert result == Ok(value=2)
    assert process.calls == [
        ("flatpak", "permissions"),
        ("flatpak", "permission-remove", "screencast", "abc123"),
        ("flatpak", "permission-remove", "screencast", "def456"),
    ]


@pytest.mark.asyncio
async def test_a_missing_permission_store_is_a_typed_refusal():
    process = FakeProcess(Err(error=diagnostic(ErrorCode.dependency_missing)))
    store = FlatpakScreenCastPermissions(process)

    store_result = await store.remembered()

    assert isinstance(store_result, Err)
    assert store_result.error.code is ErrorCode.dependency_missing


@pytest.mark.asyncio
async def test_a_failed_removal_keeps_the_remaining_approvals_untouched():
    process = FakeProcess(
        _output(PERMISSIONS),
        _output("", code=1),
    )
    store = FlatpakScreenCastPermissions(process)

    result = await store.forget_all()

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.persistence_failed
    # It stopped at the first refusal instead of claiming a partial success.
    assert process.calls == [
        ("flatpak", "permissions"),
        ("flatpak", "permission-remove", "screencast", "abc123"),
    ]


@pytest.mark.asyncio
async def test_available_reports_whether_flatpak_answers():
    assert await FlatpakScreenCastPermissions(FakeProcess(_output("flatpak 1.0\n"))).available()
    refused = FlatpakScreenCastPermissions(FakeProcess(Err(error=diagnostic(ErrorCode.dependency_missing))))
    assert await refused.available() is False
