"""Version-checked ``krfb-virtualmonitor`` helpers, owned by exact evidence.

A virtual output is created by a helper process, so owning the output means
owning *that process instance*. This adapter therefore never identifies a helper
by name or by "newest process":

* the launch carries the reservation's owner UUID and the exact app-instance
  runtime directory in the environment, never on the command line;
* the returned identity is the PID **and** its start time, read back after the
  spawn, so a recycled PID cannot inherit the reservation;
* ``find_owned`` reports only processes whose current identity, UID, owner UUID,
  and runtime directory all match, from procfs in production and from the test
  double's environment table under test;
* ``stop`` refuses an identity it cannot prove and delegates the signal to the
  ownership-checked :class:`~airplay_linux.domain.ports.ProcessPort`, which
  re-validates immediately before each signal.

The helper itself is absent on the reference machine, so the supported profile is
the *published* argument contract (``--resolution WxH`` and the single virtual
output it creates). Nothing beyond that contract is claimed: in particular,
concurrent distinct outputs are not reported as supported until a helper profile
demonstrates them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from uuid import UUID

from airplay_linux.diagnostics import ALLOWED_COMMANDS, diagnostic
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.ports import Clock, CommandSpec, ProcessPort
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result, Size
from airplay_linux.process import (
    OWNER_TOKEN_ENV,
    RUNTIME_DIRECTORY_ENV,
    IdentityState,
    OwnedProcessScan,
    owned_processes,
    read_identity_evidence,
)

#: The KDE helper command; it is in the diagnostics allowlist.
HELPER_COMMAND: Final[str] = "krfb-virtualmonitor"

#: Helper appearance/termination bound from the design.
HELPER_TIMEOUT: Final[float] = 5.0

#: The one flag this adapter depends on in the published helper contract.
REQUIRED_HELPER_FLAGS: Final[tuple[str, ...]] = ("--resolution",)

#: Arguments the published contract documents; only ``--resolution`` is used.
DOCUMENTED_HELPER_FLAGS: Final[tuple[str, ...]] = (
    "--help",
    "--resolution",
    "--name",
    "--scale",
    "--desktopfile",
    "--port",
    "--password",
)

#: Identifies the documented argument contract this profile was built from.
_HELPER_PROFILE: Final[str] = "krfb-virtualmonitor/documented/v1"
_VERSION_TOKEN: Final[re.Pattern[str]] = re.compile(r"\d+(?:\.\d+)+(?:[-+~][0-9A-Za-z.]+)?")
_FLAG_TOKEN: Final[re.Pattern[str]] = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
_SAFE_VERSION: Final[re.Pattern[str]] = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+~_-]{0,63}")


class _MonotonicClock:
    """The default clock when a caller has no injected monotonic source."""

    def now(self) -> float:
        return time.monotonic()


@dataclass(frozen=True, kw_only=True)
class HelperFeatures:
    """Only capabilities this profile demonstrates, never an optimistic guess.

    ``distinct_outputs`` is ``False`` for the documented helper contract because
    it offers no per-instance output identity or concurrent-output guarantee. A
    second virtual display is therefore a capability failure for the *new*
    request, decided by the caller without disturbing an existing helper.
    """

    version: str
    distinct_outputs: bool
    capability_fingerprint: str


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _help_failure(command: str) -> Err:
    return Err(error=diagnostic(ErrorCode.dependency_incompatible, command=command))


def _valid_owner(owner_uuid: object) -> bool:
    return type(owner_uuid) is UUID


def _valid_runtime_directory(runtime_directory: object) -> bool:
    return (
        type(runtime_directory) is str
        and 0 < len(runtime_directory) <= 4096
        and runtime_directory.startswith("/")
        and "\0" not in runtime_directory
    )


def _valid_identity(identity: object) -> bool:
    return (
        type(identity) is ProcessIdentity
        and type(identity.pid) is int
        and identity.pid > 0
        and type(identity.start_ticks) is int
        and identity.start_ticks >= 0
    )


def _valid_deadline(deadline: object) -> bool:
    return (
        type(deadline) in (int, float)
        and type(deadline) is not bool
        and math.isfinite(float(deadline))
        and float(deadline) > 0.0
    )


def _decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def _profile_fingerprint(version: str, flags: frozenset[str]) -> str:
    payload = {
        "command": HELPER_COMMAND,
        "profile": _HELPER_PROFILE,
        "version": version,
        "documented_flags": sorted(flags & frozenset(DOCUMENTED_HELPER_FLAGS)),
        "required_flags": list(REQUIRED_HELPER_FLAGS),
        "distinct_outputs": False,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class VirtualHelper:
    """Probe and launch exactly one owned ``krfb-virtualmonitor`` per reservation."""

    def __init__(
        self,
        process: ProcessPort,
        clock: Clock | None = None,
        *,
        command: str = HELPER_COMMAND,
        timeout: float = HELPER_TIMEOUT,
        proc_root: Path = Path("/proc"),
    ) -> None:
        if command not in ALLOWED_COMMANDS:
            raise ValueError("the helper command must be an allowlisted executable name")
        if not (math.isfinite(float(timeout)) and float(timeout) > 0):
            raise ValueError("the helper timeout must be positive and finite")
        self._process = process
        self._clock: Clock = clock if clock is not None else _MonotonicClock()
        self._command = command
        self._timeout = float(timeout)
        self._proc_root = proc_root
        self._features: HelperFeatures | None = None
        self._launch_lock = asyncio.Lock()

    # -- capability profile -------------------------------------------------

    async def probe(self) -> Result[HelperFeatures]:
        """Verify the installed helper documents the argument contract we need.

        A missing binary is ``dependency_missing`` naming the exact command. A
        binary whose help does not document ``--resolution`` is
        ``dependency_incompatible``. Successful profiles are cached; failures are
        not, so installing the helper and retrying works without a restart.
        """
        if self._features is not None:
            return Ok(value=self._features)
        help_result = await self._process.run(
            CommandSpec(argv=(self._command, "--help"), env={}), self._timeout,
        )
        if isinstance(help_result, Err):
            return help_result
        output = help_result.value
        if output.exit_code != 0:
            return _help_failure(self._command)
        text = f"{_decode(output.stdout)}\n{_decode(output.stderr)}"
        flags = frozenset(_FLAG_TOKEN.findall(text))
        if any(flag not in flags for flag in REQUIRED_HELPER_FLAGS):
            return _help_failure(self._command)
        version = await self._version()
        features = HelperFeatures(
            version=version,
            distinct_outputs=False,
            capability_fingerprint=_profile_fingerprint(version, flags),
        )
        self._features = features
        return Ok(value=features)

    async def _version(self) -> str:
        """The reported helper version, or ``unknown`` when it cannot be proven."""
        result = await self._process.run(
            CommandSpec(argv=(self._command, "--version"), env={}), self._timeout,
        )
        if isinstance(result, Err) or result.value.exit_code != 0:
            return "unknown"
        text = _decode(result.value.stdout).strip() or _decode(result.value.stderr).strip()
        matched = re.search(rf"{re.escape(self._command)}\s+(\S+)", text)
        candidate = matched.group(1) if matched is not None else ""
        if not candidate:
            version = _VERSION_TOKEN.search(text)
            candidate = version.group(0) if version is not None else ""
        candidate = candidate.strip()
        return candidate if _SAFE_VERSION.fullmatch(candidate) else "unknown"

    # -- launch -------------------------------------------------------------

    async def launch(
        self, owner_uuid: UUID, runtime_directory: str, size: Size,
    ) -> Result[ProcessIdentity]:
        """Spawn one helper carrying the exact ownership environment.

        The helper is refused unless its documented profile probed successfully
        and this reservation does not already have a live owned helper. The
        ownership scan is consulted inside the launch lock together with its
        completeness: a scan that could not inspect every candidate is
        uncertainty, and uncertainty never authorises a second helper. The
        post-spawn identity is verified again before it is handed out; a child
        that cannot be proven is failed and cleanup is attempted through the
        ownership-checked termination boundary.
        """
        if (
            not _valid_owner(owner_uuid)
            or not _valid_runtime_directory(runtime_directory)
            or not isinstance(size, Size)
        ):
            return _failure(ErrorCode.invalid_input)
        probed = await self.probe()
        if isinstance(probed, Err):
            return probed
        async with self._launch_lock:
            scan = self.owned_scan(owner_uuid, runtime_directory)
            if scan.matches:
                return _failure(ErrorCode.helper_failed)
            if not scan.complete:
                # An unreadable procfs entry can hide an earlier helper for this
                # reservation, so an empty match list is not proof that nothing
                # remains. Creating another helper now would break
                # one-helper-per-reservation while ownership is unknown; retrying
                # the scan is the remedy the caller is offered.
                return _failure(ErrorCode.helper_failed)
            spec = CommandSpec(
                argv=(self._command, "--resolution", f"{size.width}x{size.height}"),
                env={
                    OWNER_TOKEN_ENV: str(owner_uuid),
                    RUNTIME_DIRECTORY_ENV: runtime_directory,
                },
            )
            spawned = await self._process.spawn(spec)
            if isinstance(spawned, Err):
                return spawned
            identity = spawned.value
            exact = (
                self._process.identity(identity.pid) == identity
                and self._process.owned(identity, owner_uuid, runtime_directory)
            )
            if not exact:
                await self._terminate_quietly(identity, owner_uuid, runtime_directory)
                return _failure(ErrorCode.ownership_mismatch)
            return Ok(value=identity)

    def _remaining(self, deadline: float) -> float:
        """The duration left before ``deadline``, in the port's relative units.

        :meth:`~airplay_linux.domain.ports.ProcessPort.terminate` takes a
        *relative* duration (how long to wait for the exit), never an absolute
        clock reading, so an absolute deadline has to be converted at this
        boundary. Passing the reading itself would ask the runner to wait until
        that clock value -- on a machine whose uptime exceeds the bound, longer
        than any operation should run.
        """
        return max(0.0, float(deadline) - self._clock.now())

    async def _terminate_quietly(
        self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
    ) -> None:
        """Attempt cleanup of an unproven launch without masking the original error.

        The order returns an ownership failure; a cleanup that cannot be confirmed
        stays the caller's durable ``CleanupRequired`` evidence, not a success.
        """
        deadline = self._clock.now() + self._timeout
        budget = self._remaining(deadline)
        try:
            await self._process.terminate(identity, owner_uuid, runtime_directory, budget)
        except Exception:  # noqa: BLE001 - the caller already reports the launch failure
            return

    # -- ownership scan -----------------------------------------------------

    def find_owned(
        self, owner_uuid: UUID, runtime_directory: str,
    ) -> tuple[ProcessIdentity, ...]:
        """Every live process proved to carry this reservation's ownership evidence.

        The tuple reports *proved matches only*. An unreadable procfs entry makes
        a scan incomplete rather than empty (see :meth:`owned_scan`), so a caller
        must never treat an empty result alone as proof that nothing remains;
        ``stop`` followed by an observed output disappearance is the authoritative
        cleanup check.
        """
        return self.owned_scan(owner_uuid, runtime_directory).matches

    def owned_scan(
        self, owner_uuid: UUID, runtime_directory: str,
    ) -> OwnedProcessScan:
        """Owner-evidence matches plus whether every candidate could be inspected.

        In production the scan reads procfs; a test double that exposes an
        environment table (the deterministic stand-in for procfs) is read through
        it. Each candidate is re-checked through ``identity`` and ``owned`` so a
        recycled PID or a different runtime directory is never reported, and a
        re-check that cannot prove ownership makes the whole scan incomplete. A
        ``complete=False`` result is cleanup uncertainty, never "nothing there".
        """
        if not _valid_owner(owner_uuid) or not _valid_runtime_directory(runtime_directory):
            return OwnedProcessScan(matches=(), complete=False)
        table = getattr(self._process, "environments", None)
        if callable(table):
            return self._scan_environment_table(table, owner_uuid, runtime_directory)
        return self._scan_procfs(owner_uuid, runtime_directory)

    def _scan_environment_table(
        self,
        table: Callable[[], Mapping[int, Mapping[str, str]]],
        owner_uuid: UUID,
        runtime_directory: str,
    ) -> OwnedProcessScan:
        try:
            environments = table()
        except Exception:  # noqa: BLE001 - an unreadable table proves nothing
            return OwnedProcessScan(matches=(), complete=False)
        if not isinstance(environments, Mapping):
            return OwnedProcessScan(matches=(), complete=False)
        matches: list[ProcessIdentity] = []
        complete = True
        for pid in sorted(environments):
            if type(pid) is not int or pid <= 0:
                continue
            identity = self._process.identity(pid)
            if identity is None or identity.pid != pid:
                # A listed process whose identity cannot be read is unknown, not
                # absent; the scan cannot be called complete.
                complete = False
                continue
            if self._process.owned(identity, owner_uuid, runtime_directory):
                matches.append(identity)
        return OwnedProcessScan(
            matches=tuple(sorted(set(matches), key=lambda item: (item.pid, item.start_ticks))),
            complete=complete,
        )

    def _scan_procfs(
        self, owner_uuid: UUID, runtime_directory: str,
    ) -> OwnedProcessScan:
        scan = owned_processes(owner_uuid, runtime_directory, self._proc_root)
        matches: list[ProcessIdentity] = []
        complete = scan.complete
        for identity in scan.matches:
            evidence = read_identity_evidence(identity.pid, self._proc_root)
            if evidence.state is IdentityState.unreadable:
                complete = False
                continue
            if (
                evidence.state is not IdentityState.present
                or evidence.identity != identity
            ):
                continue
            if self._process.owned(identity, owner_uuid, runtime_directory):
                matches.append(identity)
            else:
                # ``ProcessPort.owned`` is a boolean, so it collapses an
                # unreadable ownership recheck into "not owned". This identity
                # was an exact match for the complete scan a moment ago, and a
                # running process cannot lose its owner environment, so a failed
                # recheck cannot prove non-ownership: it is uncertainty. Reporting
                # a complete empty scan here would authorise a second helper for
                # the same reservation, so the scan stays incomplete and the
                # caller keeps the retry remedy.
                complete = False
        return OwnedProcessScan(
            matches=tuple(sorted(set(matches), key=lambda item: (item.pid, item.start_ticks))),
            complete=complete,
        )

    def _absence_confirmed(self, pid: int) -> bool:
        """Whether the process authority itself confirmed that ``pid`` is gone.

        The deterministic environment table is the whole process world under
        test, so its lack of an entry is a confirmed answer there. In production
        procfs is asked, and only ``ENOENT`` counts: an unreadable entry is an
        unknown resource, which is never reported released.
        """
        if callable(getattr(self._process, "environments", None)):
            return True
        evidence = read_identity_evidence(pid, self._proc_root)
        return evidence.state is IdentityState.absent

    # -- stop ---------------------------------------------------------------

    async def stop(
        self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
        deadline: float,
    ) -> Result[None]:
        """Stop this exact helper, or refuse without signalling anything.

        A wrong owner, runtime directory, PID, or start time is
        ``ownership_mismatch`` and no signal is sent. An identity whose PID no
        longer exists cannot be the recorded instance any more, so its release is
        already confirmed. ``deadline`` is a relative duration of seconds, which
        is what :meth:`~airplay_linux.domain.ports.ProcessPort.terminate`
        contracts for. The actual signal is delegated to the process port's
        ownership-checked termination, which re-reads evidence before each signal.
        """
        if (
            not _valid_identity(identity)
            or not _valid_owner(owner_uuid)
            or not _valid_runtime_directory(runtime_directory)
            or not _valid_deadline(deadline)
        ):
            return _failure(ErrorCode.invalid_input)
        current = self._process.identity(identity.pid)
        if current is None:
            # Only a *confirmed* kernel answer means the exact instance is gone.
            # An unreadable procfs entry proves nothing, so it is never reported
            # as released (nor signalled); the caller retains cleanup uncertainty.
            if self._absence_confirmed(identity.pid):
                return Ok(value=None)
            return _failure(ErrorCode.ownership_mismatch)
        if current != identity:
            return _failure(ErrorCode.ownership_mismatch)
        if not self._process.owned(identity, owner_uuid, runtime_directory):
            return _failure(ErrorCode.ownership_mismatch)
        return await self._process.terminate(identity, owner_uuid, runtime_directory, deadline)


__all__ = [
    "DOCUMENTED_HELPER_FLAGS",
    "HELPER_COMMAND",
    "HELPER_TIMEOUT",
    "HelperFeatures",
    "REQUIRED_HELPER_FLAGS",
    "VirtualHelper",
]
