"""Private, test-only KScreen stand-in used by the adapter contract tests.

This is **not** a KScreen implementation and it must never be reachable from
production code.  It exists so the real :class:`KScreenObserver` parser, the
configuration token builder and the readback verification can run against a real
process without touching a desktop: the whole "compositor" is one JSON document
inside ``tmp_path`` (``AIRPLAY_FAKE_KSCREEN_STATE``).

Two modes exist so failure windows are explicit:

* ``AIRPLAY_FAKE_KSCREEN_MODE=malformed`` prints a document that is not JSON;
* ``AIRPLAY_FAKE_KSCREEN_MODE=fail`` exits non-zero without changing anything.

The module is also importable (``sys.path`` entry ``tests/fixtures/processes``) so
tests can build layouts and read them back with the same helpers.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

STATE_ENV = "AIRPLAY_FAKE_KSCREEN_STATE"
MODE_ENV = "AIRPLAY_FAKE_KSCREEN_MODE"

PHYSICAL_TYPE = 14
UNKNOWN_TYPE = 0


def output(
    name: str,
    output_id: int,
    *,
    kind: str = "physical",
    enabled: bool = True,
    connected: bool = True,
    width: int = 1920,
    height: int = 1080,
    scale: int | float = 1,
) -> dict:
    """One KScreen output entry, shaped like ``kscreen-doctor -j``."""
    mode_id = str(1000 + output_id)
    return {
        "id": output_id,
        "name": name,
        "type": PHYSICAL_TYPE if kind == "physical" else UNKNOWN_TYPE,
        "connected": connected,
        "enabled": enabled,
        "pos": {"x": 0, "y": 0},
        "scale": scale,
        "currentModeId": mode_id,
        "size": {"width": width, "height": height},
        "preferredModes": [mode_id],
        "modes": [{
            "id": mode_id,
            "name": f"{width}x{height}@60",
            "refreshRate": 60,
            "size": {"width": width, "height": height},
        }],
    }


def virtual_output(name: str, width: int, height: int, *, output_id: int | None = None) -> dict:
    if output_id is None:
        output_id = abs(hash(name)) % 1000 + 2
    entry = output(
        name, output_id, kind="virtual", width=width, height=height,
    )
    entry["type"] = UNKNOWN_TYPE
    return entry


def baseline_layout() -> dict:
    """The default private desktop: exactly one proven physical monitor."""
    return {
        "outputs": [output("DP-1", 1)],
        "screen": {
            "id": 0,
            "currentSize": {"width": 1920, "height": 1080},
            "minSize": {"width": 0, "height": 0},
            "maxSize": {"width": 64000, "height": 64000},
            "maxActiveOutputsCount": 3,
        },
        "tabletModeAvailable": False,
        "tabletModeEngaged": False,
    }


def read_layout(path: str | os.PathLike[str]) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_layout(path: str | os.PathLike[str], layout: dict) -> None:
    """Atomically replace the whole simulated compositor document."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".kscreen-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(layout, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def apply_tokens(layout: dict, tokens: list[str]) -> None:
    """Apply the ``output.<id>.<setting>.<value>`` contract to the document."""
    for token in tokens:
        parts = token.split(".")
        if len(parts) < 3 or parts[0] != "output":
            raise SystemExit(f"unsupported kscreen token: {token!r}")
        _, output_id, setting, *value = parts
        entry = next(
            (item for item in layout.get("outputs", []) if str(item.get("id")) == output_id),
            None,
        )
        if entry is None:
            raise SystemExit(f"unknown kscreen output: {output_id!r}")
        if setting == "enable":
            entry["enabled"] = True
        elif setting == "disable":
            entry["enabled"] = False
        elif setting == "mode":
            mode_id = value[0]
            mode = next(
                (item for item in entry.get("modes", []) if item["id"] == mode_id), None,
            )
            if mode is None:
                raise SystemExit(f"unknown kscreen mode: {mode_id!r}")
            entry["currentModeId"] = mode_id
            entry["size"] = dict(mode["size"])
        elif setting == "position":
            x, y = value[0].split(",")
            entry["pos"] = {"x": int(x), "y": int(y)}
        elif setting == "scale":
            entry["scale"] = float(".".join(value))
        else:
            raise SystemExit(f"unsupported kscreen setting: {setting!r}")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--shim-name"]:
        arguments = arguments[2:]
    state = os.environ.get(STATE_ENV)
    if not state:
        print("private test fixture: missing state path", file=sys.stderr)
        return 64
    mode = os.environ.get(MODE_ENV, "normal")
    if mode == "malformed":
        sys.stdout.write("{not json")
        return 0
    if mode == "fail":
        print("private test fixture: simulated kscreen failure", file=sys.stderr)
        return 3
    if arguments == ["-j"]:
        sys.stdout.write(json.dumps(read_layout(state), sort_keys=True))
        return 0
    if not arguments:
        print("private test fixture: no operation", file=sys.stderr)
        return 64
    layout = read_layout(state)
    try:
        apply_tokens(layout, arguments)
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return 64
    write_layout(state, layout)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a real process
    raise SystemExit(main())
