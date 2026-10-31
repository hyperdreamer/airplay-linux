# Handoff — the owned-daemon epoch (make Mirror work)

This brief exists so a fresh PM/SDD session does not have to rediscover a 30-task run's
lessons. Read it, then `docs/superpowers/plans/2026-10-30-owned-daemon-control.md` (the contract),
then `docs/backend-compatibility.md` and `docs/operations.md`.

## Objective

Implement the owned-daemon control model so **Mirror** actually works on this machine against the
locally patched `doubletake`. Do **not** implement Virtual mode: it needs `krfb-virtualmonitor`
(not installed) and an output-association verifier that the composition still lacks.

## Measured state at handoff

```text
repo            /data/home/guest/Development/airplay-linux   master, clean, no remote
tests           3787 passed, 12 skipped        (pytest, headless)
delivered       30/30 tasks, 16/16 lanes, generation 26, frontier-audited
backend         /home/bin/doubletake, /home/bin/doubletake-ctl   patched, shadows /usr/bin
                profile resolves to doubletake-0.5+source with source_reporting=True
gates           physical_source=False, foreground_control=False, owner_handshake=False, keyring=False
app             /home/bin/airplay-linux -> ~/.venvs/airplay-linux  (pip install of this repo)
toolchain       /home/henry-arch/.local/go  (Go 1.27.3, for rebuilding the patched backend)
```

`source_reporting=True` means the backend *reports* which display each stream captured. It does **not** mean
the capture is the physical monitor we asked for — consuming it is task **T5a** (observe only), and only T7 may
act on the result. `docs/backend-fork.md` records the field's limits: `type` is the portal's classification, size
and position are compositor coordinates, and `has_position` distinguishes an unknown position from a real origin.

## What the epoch must deliver

Follow the contract plan exactly. Its activation invariant is the point of the whole design:

> no production detached spawn until receipt production, verification, strict pidfd teardown and
> cross-process reconciliation all exist; every phase commit carries a test proving no production
> spawn occurs.

Five phases: (1) capability plumbing and receipt codecs, switch closed; (2) verifier, pidfd-only
teardown, cross-process authority, source identity verification (T5a, observe only); (3) isolated idle
launcher with every crash window tested; (4) the single activation commit — gate migration, acting on
source verification, credential-service proof, operator-visible disclosure; (5) offline acceptance with
negative tripwires. The plan's P1 is satisfied; P2 (credential service proof) still blocks phase 4.

## Non-negotiables

- **Do not weaken any gate** — receiver, transport admission, trust, provider, target generation,
  pairing compensation, cleanup. `physical_source` may only become `True` when the source
  verification that consumes `source_reporting` exists and is tested.
- **Per-receiver Stop disconnects only that receiver.** The shared daemon dies only in serialized
  whole-daemon shutdown with a durable `stopping` intent.
- **pidfd only.** Never the numeric-PID fallback for an owned daemon.
- **No live receiver contact** outside `unshare -rn` with a private socket and temporary
  credentials. An earlier uncontrolled probe reached a real TV.
- **Probe and harness tooling never ships in the repo**; keep it in the run root.
- App metadata must never contain a PIN, secret, or credential reference.

## Repository and harness facts

- Interpreter: create a venv and `pip install <repo>` (this is what makes the real-process suite
  pass; without the install, eight tests fail for environmental reasons). `~/.venvs/airplay-linux`
  already exists.
- Run pytest with a **short** `--basetemp` (`/tmp/x`), else ~41 socket/AF_UNIX failures appear.
  Headless: `QT_QPA_PLATFORM=offscreen PYTHONHASHSEED=0 LC_ALL=C.UTF-8`.
- Known flake: `tests/process/test_supervisor.py::test_killing_supervisor_cannot_leave_a_daemon_that_ignores_term`.
- Rebuilding the patched backend: apply `vendor/doubletake/000*.patch` to upstream
  `github.com/omarroth/doubletake` in order, `go build ./cmd/doubletake ./cmd/doubletake-ctl`,
  install to `/home/bin`. See `docs/backend-fork.md`.

## Process rules that cost this run real time

- Children spawned as subsessions **cannot delegate**. The orchestrator must run `sdd-state` and
  render every role prompt itself; a child told to drive SDD will block correctly and waste a cycle.
- Always dispatch the prompt **file** produced by `sdd-state render-prompt`; never hand-write it.
- Paths: `/home/henry-arch` is a symlink to `/data/home/henry-arch`. Event files and dispatch-intent
  paths must use the **same spelling as `state.runRoot`**; `render-prompt` wants the canonical
  `/data` spelling.
- Verify merges against the **first parent**, not by an `echo` in a compound command.
- Reviewers must prove guards by **mutation**, and must state survivors. Two runs were wasted on
  "green but wrong" work, including one probe that redefined the code it was verifying.
- Expect children to die silently: check for a report file and recent activity before reissuing, and
  keep prompts small enough that a partial report survives.
