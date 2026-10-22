"""Replay review mutations in disposable source copies; never mutate the checkout.

Run with the task interpreter and environment, e.g.:
    python tests/display/run_creation_mutations.py /tmp/private-mutation-evidence
Each child pytest invocation has a unique basetemp and the KDE execution guard.
"""
from __future__ import annotations

import concurrent.futures
import difflib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def run_one(definition, control: Path, evidence: Path):
    mid = definition["id"]
    copy = evidence / mid
    shutil.copytree(control, copy)
    path = copy / definition["path"]
    original = changed = path.read_text()
    for old, new in definition["edits"]:
        # The removal helper was extracted for T014-004. This is the same
        # rollback-stage mutation at its new indentation, scoped away from
        # prepare_teardown's independent stage write (M09).
        if mid == "M08":
            old = '\n                    stage="removing",\n                    helper=state.helper or record.helper,'
            new = '\n                    stage="published",\n                    helper=state.helper or record.helper,'
        if mid == "M09":
            old = '                    stage="removing",\n                    last_configuration_step="removing",'
            new = '                    stage="published",\n                    last_configuration_step="removing",'
        if changed.count(old) != 1:
            raise AssertionError(f"{mid}: mutation target count {changed.count(old)}: {old!r}")
        changed = changed.replace(old, new)
    compile(changed, str(path), "exec")
    path.write_text(changed)
    (evidence / f"{mid}.diff").write_text("".join(difflib.unified_diff(
        original.splitlines(True), changed.splitlines(True),
        fromfile=definition["path"], tofile=definition["path"],
    )))
    env = dict(os.environ, PYTHONPATH="src", QT_QPA_PLATFORM="offscreen",
               PYTHONHASHSEED="0", LC_ALL="C.UTF-8", PYTHONDONTWRITEBYTECODE="1")
    selection = ["tests/display"]
    if definition["storage"]:
        selection.append("tests/storage/test_atomic_records.py")
    argv = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
            "-p", "tests.display.kde_guard", "-o", f"pythonpath={copy}/src",
            f"--basetemp={evidence}/temp-{mid}", f"--junitxml={evidence}/{mid}.xml", *selection]
    with (evidence / f"{mid}.log").open("w") as log:
        process = subprocess.run(argv, cwd=copy, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, timeout=50)
    tree = ET.parse(evidence / f"{mid}.xml").getroot()
    cases = list(tree.iter("testcase"))
    failed = [f"{case.get('classname')}::{case.get('name')}" for case in cases
              if case.find("failure") is not None]
    errors = [f"{case.get('classname')}::{case.get('name')}" for case in cases
              if case.find("error") is not None]
    result = dict(id=mid, definition=definition, argv=argv, cwd=str(copy),
                  returncode=process.returncode, cases=len(cases), failures=failed, errors=errors,
                  killed=process.returncode == 1 and bool(failed) and not errors)
    (evidence / f"{mid}.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    evidence = Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[2]
    control = evidence / "control"
    control.mkdir()
    for name in ("src", "tests"):
        shutil.copytree(project / name, control / name,
                        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    shutil.copyfile(project / "pyproject.toml", control / "pyproject.toml")
    definitions = json.loads((Path(__file__).with_name("creation_mutations.json")).read_text())
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(run_one, item, control, evidence): item["id"] for item in definitions}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                result = dict(id=futures[future], killed=False, harness_error=repr(error))
            results.append(result)
            (evidence / "results.json").write_text(json.dumps(sorted(results, key=lambda r: r["id"]), indent=2) + "\n")
            print(json.dumps(result), flush=True)
    return 0 if len(results) == 24 and all(result["killed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
