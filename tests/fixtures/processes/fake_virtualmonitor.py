"""Private, test-only ``krfb-virtualmonitor`` stand-in.

The real helper is deliberately absent on the machines these tests run on, so the
production :class:`VirtualHelper` (probe, owner-evidence launch, ownership scan,
signal-and-reap stop) is exercised against this script instead.  It is *not* a
remote-framebuffer server: it only adds/removes one entry in the private simulated
KScreen document (``fake_kscreen.py``) and records its own PID/owner association
so an explicit test-side association authority can re-prove it.

Failure windows are selectable with ``AIRPLAY_FAKE_HELPER_MODE``:

``normal``
    Append exactly one virtual output, persist the association, wait for a signal,
    then remove exactly that output.
``crash-before-persist``
    Exit immediately, before the compositor document or the PID record is written
    (the launch-to-PID-persistence crash window).
``crash-after-persist``
    Append the output and the PID record, then exit immediately without removing
    it (an orphan that a later live helper cannot clean).
``duplicate-output``
    Append two new outputs of the requested size, so selection is ambiguous.
``unconfigured``
    Append exactly one output of the requested size but leave it disabled and at a
    non-zero position, so the creator must genuinely reconfigure it (the enable
    and position commands change state and their readback is evidence).

The module is importable so tests can share the environment/association helpers.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import fake_kscreen

OWNER_ENV = "AIRPLAY_LINUX_OWNER_UUID"
RUNTIME_ENV = "AIRPLAY_LINUX_RUNTIME_DIRECTORY"
STATE_ENV = fake_kscreen.STATE_ENV
PIDS_ENV = "AIRPLAY_FAKE_HELPER_PIDS"
MODE_ENV = "AIRPLAY_FAKE_HELPER_MODE"

HELP_TEXT = """\
Usage: krfb-virtualmonitor [options]
  --help            show this message
  --resolution WxH  resolution of the virtual monitor
  --name NAME       monitor name
  --scale FACTOR    scale factor
  --desktopfile F   desktop entry name
  --port PORT       listening port
  --password PASS   connection password
"""

VERSION_TEXT = "krfb-virtualmonitor 24.08.2"


def helper_env(state: str | os.PathLike[str], pids: str | os.PathLike[str], *, mode="normal"):
    return {STATE_ENV: str(state), PIDS_ENV: str(pids), MODE_ENV: mode}


def _read_pids(path: Path) -> list[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _write_pids(path: Path, records: list[dict]) -> None:
    target = path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
    os.replace(temporary, target)


def _next_id(layout: dict) -> int:
    used = [int(entry["id"]) for entry in layout.get("outputs", []) if str(entry.get("id")).isdigit()]
    return max(used, default=1) + 1


def _size(text: str) -> tuple[int, int]:
    width, height = text.lower().split("x", 1)
    return int(width), int(height)


def _remove_own_outputs(state: Path, pids: Path) -> None:
    mine = [record for record in _read_pids(pids) if record["pid"] == os.getpid()]
    if not mine:
        return
    layout = fake_kscreen.read_layout(state)
    owned = {(record["connector"], record["output_id"]) for record in mine}
    layout["outputs"] = [
        entry for entry in layout.get("outputs", [])
        if (entry.get("name"), str(entry.get("id"))) not in owned
    ]
    fake_kscreen.write_layout(state, layout)
    _write_pids(pids, [record for record in _read_pids(pids) if record["pid"] != os.getpid()])


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--shim-name"]:
        arguments = arguments[2:]
    if "--help" in arguments or "-h" in arguments:
        sys.stdout.write(HELP_TEXT)
        return 0
    if "--version" in arguments:
        sys.stdout.write(VERSION_TEXT + "\n")
        return 0
    if "--resolution" not in arguments:
        print("private test fixture: --resolution is required", file=sys.stderr)
        return 64
    resolution = arguments[arguments.index("--resolution") + 1]
    width, height = _size(resolution)
    requested_name = arguments[arguments.index("--name") + 1] if "--name" in arguments else None

    owner = os.environ.get(OWNER_ENV)
    runtime = os.environ.get(RUNTIME_ENV)
    state = os.environ.get(STATE_ENV)
    pids = os.environ.get(PIDS_ENV)
    mode = os.environ.get(MODE_ENV, "normal")
    if not owner or not runtime or not state or not pids:
        # Refuse exactly like the documented helper refuses without its ownership
        # environment: nothing is persisted and no PID is claimed.
        print("private test fixture: ownership environment is required", file=sys.stderr)
        return 2
    if mode == "crash-before-persist":
        return 1

    state_path, pids_path = Path(state), Path(pids)
    layout = fake_kscreen.read_layout(state_path)
    count = 2 if mode == "duplicate-output" else 1
    created: list[tuple[str, str]] = []
    for index in range(count):
        output_id = _next_id(layout)
        connector = requested_name or f"Virtual-{output_id}"
        if index and requested_name:
            connector += f"-{index + 1}"
        layout["outputs"].append({
            "id": output_id,
            "name": connector,
            "type": fake_kscreen.UNKNOWN_TYPE,
            "connected": True,
            "enabled": mode != "unconfigured",
            "pos": {"x": 640, "y": 480} if mode == "unconfigured" else {"x": 0, "y": 0},
            "scale": 1,
            "currentModeId": str(2000 + output_id),
            "size": {"width": width, "height": height},
            "preferredModes": [str(2000 + output_id)],
            "modes": [{
                "id": str(2000 + output_id),
                "name": f"{width}x{height}@60",
                "refreshRate": 60,
                "size": {"width": width, "height": height},
            }],
        })
        created.append((connector, str(output_id)))
    fake_kscreen.write_layout(state_path, layout)
    records = _read_pids(pids_path)
    for connector, output_id in created:
        records.append({
            "pid": os.getpid(),
            "owner_uuid": owner,
            "runtime_directory": runtime,
            "connector": connector,
            "output_id": output_id,
        })
    _write_pids(pids_path, records)
    if mode == "crash-after-persist":
        return 1

    def stop(signum, frame):  # noqa: ARG001 - signal handler signature
        try:
            _remove_own_outputs(state_path, pids_path)
        finally:
            os._exit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    while True:
        time.sleep(1.0)


if __name__ == "__main__":  # pragma: no cover - exercised as a real process
    raise SystemExit(main())
