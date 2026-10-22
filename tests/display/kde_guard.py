"""Block KDE subprocess execution in disposable review tests."""
import errno
import os
import subprocess
import sys

ATTEMPTS = []
CHECKING = False


def guard(event, args):
    if event == "subprocess.Popen":
        executable = args[0]
    elif event in {"os.posix_spawn", "os.exec"}:
        executable = args[0]
    else:
        return
    try:
        name = os.path.basename(os.fsdecode(executable))
    except (TypeError, ValueError):
        return
    if name in {"kscreen-doctor", "krfb-virtualmonitor"}:
        ATTEMPTS.append(("self-check" if CHECKING else "test", name))
        raise FileNotFoundError(errno.ENOENT, "KDE execution blocked by read-only review")


sys.addaudithook(guard)


def pytest_sessionstart(session):
    global CHECKING
    CHECKING = True
    try:
        for command in ("kscreen-doctor", "krfb-virtualmonitor"):
            try:
                subprocess.Popen([command, "--review-guard-self-check"])
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError("KDE execution guard did not fire")
    finally:
        CHECKING = False


def pytest_terminal_summary(terminalreporter):
    actual = [item for item in ATTEMPTS if item[0] == "test"]
    terminalreporter.write_line(f"REVIEW_KDE_GUARD self_checks=2 blocked_test_attempts={len(actual)} commands={actual!r}")
