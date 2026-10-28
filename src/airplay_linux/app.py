"""Application entry point: argument parsing, the single-instance guard, and run.

``main`` is deliberately thin. It parses arguments, takes the single-instance
lock, composes the runtime and hands control to the UI — all the interesting
decisions live in :mod:`airplay_linux.runtime`. Keeping this file small is what
makes the composition testable without a window, a daemon or a network.

``--diagnostics`` and ``--check-environment`` are read-only commands: they report
what the environment supports and exit without composing a session or touching any
receiver state.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from airplay_linux.domain.resources import Err
from airplay_linux.runtime import RuntimeConfig, build_runtime


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the operator command line.

    ``--diagnostics`` and ``--check-environment`` are mutually exclusive read-only
    commands; everything else starts the dashboard.
    """

    parser = argparse.ArgumentParser(
        prog="airplay-linux",
        description="Mirror Linux physical monitors to AirPlay receivers, or give a TV its own virtual display.",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="print the diagnostic catalog and exit without starting the dashboard",
    )
    parser.add_argument(
        "--check-environment",
        action="store_true",
        help="probe desktop, audio and backend capabilities, then exit",
    )
    parser.add_argument("--state-root", type=str, default=None, help="override the metadata root")
    parser.add_argument("--runtime-root", type=str, default=None, help="override the runtime root")
    return parser.parse_args(argv)


def _roots(args: argparse.Namespace) -> RuntimeConfig:
    from pathlib import Path

    state = Path(args.state_root) if args.state_root else _default_state_root()
    runtime = Path(args.runtime_root) if args.runtime_root else _default_runtime_root()
    return RuntimeConfig(state_root=state, runtime_root=runtime)


def _default_state_root():
    from airplay_linux.storage import state_root

    return state_root()


def _default_runtime_root():
    import os
    from pathlib import Path

    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise SystemExit("XDG_RUNTIME_DIR is not set; the private daemon instance needs a runtime root")
    return Path(base) / "airplay-linux"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the application. Returns a process exit status."""

    args = parse_args(argv)

    if args.diagnostics:
        from airplay_linux.diagnostics.catalog import ENTRIES

        for code, entry in sorted(ENTRIES.items(), key=lambda item: str(item[0])):
            print(f"{code}: {entry.message}")
        return 0

    if args.check_environment:
        from airplay_linux.display.helper import VirtualHelper
        from airplay_linux.policy.capabilities import EnvironmentProbe
        from airplay_linux.process.linux import LinuxProcessRunner

        process = LinuxProcessRunner()
        helper = VirtualHelper(process)
        probe = EnvironmentProbe.from_environment(process=process, helper=helper)
        report = asyncio.run(probe.check())
        print(report)
        return 0

    config = _roots(args)
    return _run_guarded(config)


def _run_guarded(config: RuntimeConfig) -> int:
    """Take the single-instance lock, then run.

    The guard owns the lock for the whole process lifetime: releasing it while the
    dashboard is alive would let a second instance start against the same runtime
    root, which is exactly what the guard exists to prevent.
    """

    from airplay_linux.process.instance import InstanceGuard

    from airplay_linux.process.linux import LinuxProcessRunner

    guard = InstanceGuard(config.runtime_root, LinuxProcessRunner())
    instance_uuid = _new_instance_id()
    acquired = guard.acquire(instance_uuid)
    if isinstance(acquired, Err):
        print("another instance already owns this runtime root", file=sys.stderr)
        return 1
    try:
        return asyncio.run(_run(config))
    finally:
        guard.release()


def _new_instance_id():
    from uuid import uuid4

    from airplay_linux.domain.operations import InstanceId

    return InstanceId(uuid4())


async def _run(config: RuntimeConfig) -> int:
    """Compose and run the dashboard.

    When composition cannot obtain real evidence it reports the refused
    capability instead of starting a half-working session: the lanes below this
    point are written to fail closed, and pretending otherwise here would hide
    their refusal from the operator.
    """

    result = await build_runtime(config)
    if isinstance(result, Err):
        print(f"the application cannot start: {result.error.message}", file=sys.stderr)
        return 2

    runtime = result.value
    for capability, reason in runtime.gated_capabilities.items():
        print(f"capability gated: {capability} - {reason}", file=sys.stderr)

    status = 0
    started = await runtime.start()
    if isinstance(started, Err):
        print(f"the application cannot start: {started.error.message}", file=sys.stderr)
        status = 2
    shutdown = await runtime.shutdown()
    if isinstance(shutdown, Err):
        print(f"the application could not shut down cleanly: {shutdown.error.message}", file=sys.stderr)
        status = max(status, 1)
    return status


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
