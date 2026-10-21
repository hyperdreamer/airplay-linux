"""Run the review mutations in isolated worktree copies, never the active tree."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def replace_once(path, old, new):
    text = path.read_text()
    if text.count(old) != 1:
        raise AssertionError(f"mutation anchor must occur once in {path}: {old!r}")
    path.write_text(text.replace(old, new))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tests = "tests/session"
    engine_path = "src/airplay_linux/session/engine.py"
    fencing_path = "src/airplay_linux/session/fencing.py"
    mutations = [
        ("M1-effect-write", engine_path, [(
            "            return await effect.work()",
            "            result = await effect.work()\n"
            "            self._contexts[effect.receiver_id].apply(\n"
            "                intent_active=True, intent_stage=IntentStage.active,\n"
            "                transport=TransportState.Streaming,\n"
            "            )\n"
            "            return result",
        )], tests),
        ("M2-force-phase", engine_path, [
            ('        changes.pop("phase", None)', '        forced_phase = changes.pop("phase", None)'),
            ("        next_state = replace(next_state, phase=derive_phase(next_state))",
             "        next_state = replace(next_state, phase=forced_phase or derive_phase(next_state))"),
        ], tests),
        ("M3-remove-cleanup-reroute", engine_path, [(
            "                    route = Route.cleanup\n"
            '                    message = replace(message, kind=f"{route.value}:{effect.name}")',
            "                    route = effect.route\n"
            '                    message = replace(message, kind=f"{route.value}:{effect.name}")',
        )], tests),
        ("M3b-remove-transport-generation", fencing_path, [(
            "        if event.context.session_generation != state.session_generation:\n            return False",
            "        if False:\n            return False",
        )], tests),
        ("M4-retry-supersedes", engine_path, [(
            "        self._ensure(receiver_id)\n        self._queues[receiver_id].append(command)",
            "        self._ensure(receiver_id)\n"
            "        if isinstance(command, Retry):\n"
            '            self._contexts[receiver_id].supersede("start")\n'
            "        self._queues[receiver_id].append(command)",
        )], tests),
        ("M4-actor-retry-supersedes", engine_path, [(
            "                admitted = self._admit(context, message, route)",
            "                if isinstance(message, Retry):\n"
            '                    context.supersede("start")\n'
            "                admitted = self._admit(context, message, route)",
        )], tests),
        ("M5-late-completion-overwrites", engine_path, [(
            "        route = effect.route\n        kind = f\"{route.value}:{effect.name}\"",
            "        current = self._states.get(effect.receiver_id)\n"
            "        if (not effect.acquisition and current is not None\n"
            "                and current.session_generation != effect.session_generation):\n"
            "            self._contexts[effect.receiver_id].apply(\n"
            "                intent_active=True, intent_stage=IntentStage.active,\n"
            "                transport=TransportState.Streaming,\n"
            "            )\n"
            "        route = effect.route\n        kind = f\"{route.value}:{effect.name}\"",
        )], tests),
        ("F8-normal-stop-remove-reroute", engine_path, [(
            "                    route = Route.cleanup\n"
            '                    message = replace(message, kind=f"{route.value}:{effect.name}")',
            "                    route = effect.route\n"
            '                    message = replace(message, kind=f"{route.value}:{effect.name}")',
        )], "tests/session/test_engine.py::test_completed_stale_acquisition_is_routed_to_cleanup_and_remains_pending"),
    ]
    results = []
    for name, source_path, replacements, selected in mutations:
        copy = output / name
        copy.mkdir()
        shutil.copytree(root / "src", copy / "src", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(root / "tests", copy / "tests", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(root / "pyproject.toml", copy / "pyproject.toml")
        for old, new in replacements:
            replace_once(copy / source_path, old, new)
        command = [args.python, "-m", "pytest", "-q", selected, "--tb=short",
                   f"--basetemp={tempfile.mkdtemp(prefix='pytest-', dir=copy)}", "-p", "no:cacheprovider"]
        env = dict(os.environ, PYTHONPATH="src", QT_QPA_PLATFORM="offscreen",
                   PYTHONHASHSEED="0", LC_ALL="C.UTF-8", PYTHONDONTWRITEBYTECODE="1")
        run = subprocess.run(command, cwd=copy, env=env, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, timeout=60)
        log = output / f"{name}.log"
        log.write_text(run.stdout)
        failures = [line for line in run.stdout.splitlines() if line.startswith("FAILED ")]
        result = dict(name=name, command=command, replacements=replacements,
                      returncode=run.returncode, failures=failures, log=str(log))
        results.append(result)
        print(f"{name}: exit {run.returncode}; {len(failures)} failing tests; {log}", flush=True)
    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    if any(result["returncode"] != 1 or not result["failures"] for result in results):
        raise SystemExit("A mutation survived or failed without a test failure; inspect the logs")


if __name__ == "__main__":
    main()
