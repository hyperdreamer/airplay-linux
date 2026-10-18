"""Bounded command execution and evidence-checked termination of owned children.

Every command this application runs is small and explicit: no shell, a bounded
environment, bounded output, and a deadline. The reason is not tidiness. The
child processes here are a transport daemon and virtual-display helpers, so the
runner is the one place that can both *start* a process and *signal* one, and a
mistake there either hangs the GUI or kills a stranger's process.

The rules this module implements:

* ``asyncio.create_subprocess_exec`` only, so argv is never re-parsed by a shell.
* Both output streams are drained while the child runs -- a child that fills a
  pipe would otherwise deadlock the waiter -- and each is truncated to
  :data:`OUTPUT_LIMIT`. Truncation is not an error: the caller asked for a
  command's answer, and the trailing bytes past the cap are not part of it.
* The environment is a fixed allowlist of session and locale names, plus exactly
  what the caller overlays. Nothing else is inherited, so a token in the parent's
  environment cannot leak into a child. Neither the environment nor stdin is ever
  logged: stdin is the channel a PIN would travel on.
* A deadline is a *typed* outcome (:attr:`ErrorCode.cleanup_timeout`), not a
  truncated success, and the child is killed and reaped before the result is
  returned, so nothing outlives the call that started it.
* Termination is refused unless the target still has the exact recorded start
  time, is owned by this user, and carries this app instance's owner UUID with
  its exact runtime directory in the environment. Missing or unreadable evidence
  is a refusal, never a kill: a recycled PID belongs to someone else.
* When the kernel offers it, a ``pidfd`` is opened for the validated process and
  every signal and every wait goes through that descriptor. A pidfd names one
  process instance, so a PID recycled between the check and the signal cannot
  redirect the signal -- which is exactly the race an ownership check alone
  cannot close.
* A ``pidfd`` is opened for a freshly launched child as well, before the runner
  awaits anything else, and deadline/cancellation cleanup signals that descriptor
  rather than the child handle's PID. Without a descriptor the runner does not
  degrade to a raw PID signal -- it reports cleanup uncertainty instead, because
  a kill that could land on a recycled PID is worse than a known-leaked child.
* Cleanup of a launch that never became a tracked child keeps the launch itself
  when it cannot prove the child gone. A descriptor makes a signal exact; failing
  that, the launch identity is re-read and validated, because a read that failed
  at launch time is often readable a moment later. With neither, or when the
  validated child survives its kill, the obligation is *retained*: the handle
  stays in a reaper the runner owns and the PID is recorded as cleanup-uncertain
  until the kernel confirms that exact child was reaped. Finishing a cleanup
  there would return while a process the runner started may still be alive.
* Evidence is read immediately before *each* signal, never once per operation,
  and the identity is re-read after the environment read: two file accesses from
  two different moments are not evidence about one process instance.

The exit channel reports *spawned* children only. ``run`` already returns its
child's exit code to the caller, and a spawn that could not start has no
identity to report, so an event always means "a long-lived child you are relying
on is gone", including when it died from a signal nobody sent.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from airplay_linux.diagnostics import ALLOWED_COMMANDS, diagnostic
from airplay_linux.domain import ports
from airplay_linux.domain.operations import ProcessIdentity
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result

from .procfs import (
    IdentityState,
    owner_evidence,
    read_identity,
    read_identity_evidence,
)

#: Bytes retained per stream; anything past this is drained and discarded.
OUTPUT_LIMIT: Final[int] = 1 << 20

#: How long a killed child has to disappear before cleanup is unconfirmed.
#:
#: ``SIGKILL`` cannot be ignored, so a child still present afterwards is
#: unkillable (stuck in uninterruptible I/O) rather than slow, and waiting longer
#: cannot help. The value is the design's helper-appearance bound, reused so a
#: teardown step cannot outlive the interval the UI was promised.
KILL_TIMEOUT: Final[float] = 5.0

#: How long a freshly forked child may take to appear in procfs.
SPAWN_EVIDENCE_TIMEOUT: Final[float] = 2.0

#: Environment names inherited from the parent; everything else is dropped.
#:
#: These are the names compositor and session tools need, and the locale and
#: timezone names that change a tool's output format. Credential carriers --
#: agent sockets, cloud keys, tokens -- are deliberately absent, and a caller that
#: needs another name overlays it explicitly.
INHERITED_ENVIRONMENT: Final[frozenset[str]] = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "XDG_CURRENT_DESKTOP",
        "XDG_DATA_DIRS",
        "WAYLAND_DISPLAY",
        "DISPLAY",
        "XAUTHORITY",
        "DBUS_SESSION_BUS_ADDRESS",
        "KDE_FULL_SESSION",
    }
)

_READ_CHUNK: Final[int] = 64 * 1024
_POLL_INTERVAL: Final[float] = 0.02

#: How long a dead child's pipes may take to reach EOF before they are dropped.
#:
#: Everything the child wrote is already in the pipe buffer at this point, so a
#: normal exit finishes in one scheduler turn. The bound exists for the abnormal
#: case where a grandchild inherited a pipe and never closes it.
_DRAIN_GRACE: Final[float] = 0.5

#: Maximum time allowed for the parent's stdin transport to report closed.
_FEED_CLOSE_TIMEOUT: Final[float] = 0.25

_DEVNULL = asyncio.subprocess.DEVNULL
_PIPE = asyncio.subprocess.PIPE

_GONE: Final[str] = "gone"
_OWNED: Final[str] = "owned"
_MISMATCH: Final[str] = "mismatch"


def inherited_environment(overlay: Mapping[str, str]) -> dict[str, str]:
    """The child environment: this module's allowlist, then the caller's overlay.

    Names and values that are not plain text are dropped rather than coerced, and
    so is anything carrying a NUL, because the kernel rejects such an environment
    and the resulting failure would name the value.
    """
    environment = {
        name: os.environ[name] for name in INHERITED_ENVIRONMENT if name in os.environ
    }
    for name, value in overlay.items():
        if type(name) is not str or type(value) is not str:
            continue
        if not name or "\0" in name or "\0" in value:
            continue
        environment[name] = value
    return environment


def _validated_argv(argv: object) -> tuple[str, ...] | None:
    """``argv`` as exact text, or ``None`` when it could not be executed at all."""
    try:
        items = tuple(argv)  # type: ignore[arg-type]
    except TypeError:
        return None
    if not items:
        return None
    for item in items:
        if type(item) is not str or not item or "\0" in item:
            return None
    return items


def _command_name(argv: tuple[str, ...]) -> str | None:
    """The allowlisted executable name of ``argv``, for a catalog message."""
    if not argv:
        return None
    name = os.path.basename(argv[0])
    return name if name in ALLOWED_COMMANDS else None


def _missing_command(argv: tuple[str, ...]) -> Err:
    """The typed failure for a command that could not be started."""
    return Err(error=diagnostic(ErrorCode.dependency_missing, command=_command_name(argv)))


def _timed_out() -> Err:
    """The typed failure for a deadline, carrying no captured output."""
    return Err(error=diagnostic(ErrorCode.cleanup_timeout))


@dataclass
class _Child:
    """One launched child this runner is responsible for reaping."""

    identity: ProcessIdentity
    process: asyncio.subprocess.Process
    finished: asyncio.Future[None] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future(),
    )


@dataclass
class _Subscription:
    """Idempotent exit-channel subscription."""

    action: Callable[[], None] | None
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        action, self.action = self.action, None
        if action is not None:
            action()


class LinuxProcessRunner:
    """The Linux :class:`~airplay_linux.domain.ports.ProcessPort` implementation."""

    #: Escalation bound after ``SIGKILL``; see :data:`KILL_TIMEOUT`.
    kill_timeout: float = KILL_TIMEOUT

    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self._proc_root = proc_root
        self._children: dict[int, _Child] = {}
        self._subscribers: list[Callable[[ports.ProcessExit], None]] = []
        #: Reaper tasks the runner keeps alive itself.
        #:
        #: ``asyncio`` holds a task only weakly, so a detached reaper whose only
        #: reference was the caller's local variable can be collected before it
        #: publishes the exit -- and then a long-lived child's death goes
        #: unreported. The runner therefore owns every reaper until it finishes.
        self._reapers: set[asyncio.Task[None]] = set()
        #: Stream cleanup tasks that reached the terminal deadline still retain
        #: ownership until their cancellation finalizer eventually finishes.
        self._stream_cleanups: set[asyncio.Future[None]] = set()
        #: PIDs whose cleanup could not be proven, kept until they are reaped.
        #:
        #: A cleanup with no exact evidence has no target it may signal, so it
        #: can neither confirm that child gone nor pretend it finished. The PID
        #: is recorded here -- and only here, with no captured child text -- so
        #: the retained obligation is observable instead of silent.
        self._cleanup_uncertain: set[int] = set()

    # -- ProcessPort --------------------------------------------------------

    async def run(
        self, spec: ports.CommandSpec, timeout: float, stdin: bytes | None = None,
    ) -> Result[ports.CommandOutput]:
        argv = _validated_argv(spec.argv)
        if argv is None:
            return _missing_command(spec.argv if isinstance(spec.argv, tuple) else ())
        try:
            process = await self._exec(
                argv,
                spec,
                stdin=_PIPE if stdin is not None else _DEVNULL,
                stdout=_PIPE,
                stderr=_PIPE,
            )
        except (OSError, ValueError):
            return _missing_command(argv)
        # Opened and named before the next await, so they identify the instance
        # this call launched even if the child exits and its PID is recycled
        # before a deadline or a cancellation has to clean it up.
        pidfd = self._open_pidfd(process.pid)
        launch_identity = self._launch_identity(process.pid)
        stdout = bytearray()
        stderr = bytearray()
        streams: list[asyncio.Future[None]] = [
            asyncio.ensure_future(self._drain(process.stdout, stdout)),
            asyncio.ensure_future(self._drain(process.stderr, stderr)),
        ]
        if stdin is not None:
            streams.append(asyncio.ensure_future(self._feed(process, stdin)))
        try:
            await asyncio.wait_for(process.wait(), max(0.0, float(timeout)))
        except TimeoutError:
            await self._kill_direct_child(process, pidfd, launch_identity)
            await self._finish(streams, _DRAIN_GRACE)
            return _timed_out()
        except asyncio.CancelledError:
            await self._kill_direct_child(process, pidfd, launch_identity)
            await self._finish(streams, _DRAIN_GRACE)
            raise
        finally:
            self._close_pidfd(pidfd)
        await self._finish(streams, _DRAIN_GRACE)
        return Ok(
            value=ports.CommandOutput(
                exit_code=process.returncode if process.returncode is not None else -1,
                stdout=bytes(stdout),
                stderr=bytes(stderr),
            ),
        )

    async def spawn(self, spec: ports.CommandSpec) -> Result[ProcessIdentity]:
        argv = _validated_argv(spec.argv)
        if argv is None:
            return _missing_command(spec.argv if isinstance(spec.argv, tuple) else ())
        launch = asyncio.ensure_future(
            self._exec(
                argv, spec, stdin=_DEVNULL, stdout=_DEVNULL, stderr=_DEVNULL,
            )
        )
        try:
            process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            cleanup = asyncio.ensure_future(self._cleanup_cancelled_launch(launch))
            self._retain_reaper(cleanup)
            await asyncio.shield(cleanup)
            raise
        except (OSError, ValueError):
            return _missing_command(argv)
        pidfd = self._open_pidfd(process.pid)
        cleanup_started = False
        try:
            try:
                identity = await self._read_spawned_identity(process.pid)
            except asyncio.CancelledError:
                # The child is still unowned at this point.  Transfer the
                # descriptor and cleanup obligation to a task that remains
                # strongly held even if cancellation arrives again, then
                # propagate the caller's cancellation after that obligation is
                # complete (or retained).
                cleanup_started = True
                await self._cleanup_untracked_child(process, pidfd)
                raise
            if identity is None:
                # A child that cannot be named is a child that can never be
                # released safely, so it is killed here rather than handed out
                # as an identity. No identity means no fallback evidence either,
                # so only the descriptor may be signalled -- never a bare PID.
                cleanup_started = True
                await self._cleanup_untracked_child(process, pidfd)
                return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        finally:
            if not cleanup_started:
                self._close_pidfd(pidfd)
        child = _Child(identity=identity, process=process)
        self._children[process.pid] = child
        reaper = asyncio.ensure_future(self._monitor(child))
        self._retain_reaper(reaper)
        return Ok(value=identity)

    def _retain_reaper(self, task: asyncio.Task[None]) -> None:
        """Keep cleanup/reaper tasks alive until their child obligation ends."""
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    async def _cleanup_cancelled_launch(
        self, launch: asyncio.Future[asyncio.subprocess.Process],
    ) -> None:
        """Finish a cancelled launch, then clean it before losing its handle."""
        try:
            process = await launch
        except Exception:
            return
        pidfd = self._open_pidfd(process.pid)
        await self._cleanup_untracked_child(process, pidfd)

    async def _cleanup_untracked_child(
        self, process: asyncio.subprocess.Process, pidfd: int | None,
    ) -> None:
        """Kill/reap an unowned launch while retaining the launch descriptor."""
        async def cleanup() -> None:
            try:
                await self._kill_direct_child(process, pidfd, None)
            finally:
                self._close_pidfd(pidfd)

        task = asyncio.ensure_future(cleanup())
        self._retain_reaper(task)
        await asyncio.shield(task)

    @property
    def retained_reapers(self) -> int:
        """How many reapers this runner is still holding, for observability."""
        return len(self._reapers)

    def identity(self, pid: int) -> ProcessIdentity | None:
        return read_identity(pid, self._proc_root)

    def owned(
        self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
    ) -> bool:
        return owner_evidence(identity, owner_uuid, runtime_directory, self._proc_root)

    async def terminate(
        self, identity: ProcessIdentity, owner_uuid: UUID,
        runtime_directory: str, deadline: float,
    ) -> Result[None]:
        classified = self._classify(identity, owner_uuid, runtime_directory)
        if classified == _GONE:
            return Ok(value=None)
        if classified != _OWNED:
            return Err(error=diagnostic(ErrorCode.ownership_mismatch))
        child = self._tracked(identity)
        pidfd = self._open_pidfd(identity.pid)
        try:
            if pidfd is not None:
                # The descriptor names one process instance, so re-validating
                # after opening proves the fd and the evidence agree. From here
                # recycling the PID cannot redirect a signal.
                reclassified = self._classify(identity, owner_uuid, runtime_directory)
                if reclassified == _GONE:
                    return Ok(value=None)
                if reclassified != _OWNED:
                    return Err(error=diagnostic(ErrorCode.ownership_mismatch))
            delivered = self._deliver(
                identity, owner_uuid, runtime_directory, pidfd, signal.SIGTERM,
            )
            if delivered == _GONE:
                return Ok(value=None)
            if delivered != _OWNED:
                return Err(error=diagnostic(ErrorCode.ownership_mismatch))
            if await self._await_exit(identity, pidfd, child, max(0.0, float(deadline))):
                return Ok(value=None)
            reclassified = self._classify(identity, owner_uuid, runtime_directory)
            if reclassified == _GONE:
                return Ok(value=None)
            if reclassified != _OWNED:
                return Err(error=diagnostic(ErrorCode.ownership_mismatch))
            delivered = self._deliver(
                identity, owner_uuid, runtime_directory, pidfd, signal.SIGKILL,
            )
            if delivered == _GONE:
                return Ok(value=None)
            if delivered != _OWNED:
                return Err(error=diagnostic(ErrorCode.ownership_mismatch))
            if await self._await_exit(identity, pidfd, child, self.kill_timeout):
                return Ok(value=None)
            return _timed_out()
        finally:
            self._close_pidfd(pidfd)

    def subscribe(self, callback: Callable[[ports.ProcessExit], None]) -> ports.Subscription:
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return _Subscription(action=unsubscribe)

    # -- launch -------------------------------------------------------------

    async def _exec(
        self,
        argv: tuple[str, ...],
        spec: ports.CommandSpec,
        *,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> asyncio.subprocess.Process:
        """Start exactly ``argv``; no shell, bounded environment, no console input."""
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            cwd=spec.cwd,
            env=inherited_environment(spec.env),
            pass_fds=tuple(spec.pass_fds),
        )

    async def _read_spawned_identity(self, pid: int) -> ProcessIdentity | None:
        """The identity of a just-forked child, or ``None`` if it never appeared."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + SPAWN_EVIDENCE_TIMEOUT
        while True:
            identity = read_identity(pid, self._proc_root)
            if identity is not None:
                return identity
            if loop.time() >= give_up:
                return None
            await asyncio.sleep(_POLL_INTERVAL)

    # -- streams ------------------------------------------------------------

    @staticmethod
    async def _drain(stream: asyncio.StreamReader | None, sink: bytearray) -> None:
        """Read ``stream`` to EOF, keeping at most :data:`OUTPUT_LIMIT` bytes.

        Reading never stops at the cap: the child keeps writing, and a pipe
        nobody reads is a pipe that blocks the child forever.
        """
        if stream is None:
            return
        while True:
            try:
                chunk = await stream.read(_READ_CHUNK)
            except (ConnectionResetError, ValueError):
                return
            if not chunk:
                return
            room = OUTPUT_LIMIT - len(sink)
            if room > 0:
                sink.extend(chunk[:room])

    @staticmethod
    def _observe_task_exception(task: asyncio.Future[object]) -> None:
        """Consume a detached close waiter's exception without blocking teardown."""
        if task.cancelled():
            return
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _close_feed_writer(
        writer: asyncio.StreamWriter, *, abort: bool,
    ) -> None:
        """Close stdin without waiting indefinitely for inherited readers."""
        try:
            if abort:
                writer.transport.abort()
            else:
                writer.close()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass

        close_wait = asyncio.ensure_future(writer.wait_closed())
        close_wait.add_done_callback(LinuxProcessRunner._observe_task_exception)
        try:
            await asyncio.wait_for(
                asyncio.shield(close_wait), _FEED_CLOSE_TIMEOUT,
            )
            return
        except asyncio.CancelledError:
            # A second cancellation must not strand a wait_closed task or leave
            # the transport trying to drain a pipe whose reader is a grandchild.
            try:
                writer.transport.abort()
            except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                pass
            close_wait.cancel()
            raise
        except (BrokenPipeError, ConnectionResetError, OSError, TimeoutError, ValueError):
            pass

        # A close that did not report completion in time is no longer useful as
        # a graceful close.  Abort only our stdin transport; never signal the
        # process group or an inherited descendant.
        try:
            writer.transport.abort()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        if not close_wait.done():
            _, pending = await asyncio.wait(
                {close_wait}, timeout=_FEED_CLOSE_TIMEOUT,
            )
            if pending:
                close_wait.cancel()

    @staticmethod
    async def _feed(process: asyncio.subprocess.Process, payload: bytes) -> None:
        """Write stdin once and close it, so the child sees EOF."""
        writer = process.stdin
        if writer is None:
            return
        cancelled = False
        try:
            writer.write(payload)
            await writer.drain()
        except asyncio.CancelledError:
            cancelled = True
            raise
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            await LinuxProcessRunner._close_feed_writer(writer, abort=cancelled)

    def _retain_stream_cleanup(self, task: asyncio.Future[None]) -> None:
        """Keep unfinished stream cleanup owned after the run's deadline."""
        self._stream_cleanups.add(task)
        task.add_done_callback(self._stream_cleanups.discard)

    async def _finish(self, tasks: list[asyncio.Future[None]], grace: float) -> None:
        """Finish streams under a grace period plus a terminal deadline."""
        if not tasks:
            return
        loop = asyncio.get_running_loop()
        terminal_deadline = loop.time() + max(0.0, grace) + _FEED_CLOSE_TIMEOUT
        _, pending = await asyncio.wait(tasks, timeout=max(0.0, grace))
        if pending:
            for task in pending:
                task.cancel()
            remaining = max(0.0, terminal_deadline - loop.time())
            if remaining:
                _, pending = await asyncio.wait(pending, timeout=remaining)
            if pending:
                # Do not gather without a deadline: a cancellation finalizer is
                # allowed to outlive this call, but never to hold it hostage.
                for task in pending:
                    task.cancel()
                    self._retain_stream_cleanup(task)
        completed = [task for task in tasks if task.done()]
        if completed:
            await asyncio.gather(*completed, return_exceptions=True)

    async def _kill_direct_child(
        self,
        process: asyncio.subprocess.Process,
        pidfd: int | None,
        identity: ProcessIdentity | None,
    ) -> None:
        """Kill the exact child this call started, then confirm it was reaped.

        The descriptor captured at launch is preferred because it names one
        process instance. Without one, the launch identity is reacquired when it
        was never read, and re-read once more immediately before the signal --
        the exact start time and this process as parent are the only things
        standing between a recycled PID and this ``SIGKILL``. When no exact
        evidence can be obtained, nothing is signalled and the launch is
        *retained*: uncertainty is recorded and the handle stays watched by a
        reaper this runner owns, so the obligation is observable instead of
        silently completed. A failed signal or an incomplete bounded wait is
        retained the same way, including when a pidfd sender is unavailable.
        """
        if pidfd is None:
            if identity is None:
                identity = self._reacquire_launched_identity(process.pid)
            if identity is None or not self._is_launched_child(identity):
                self._retain_cleanup_uncertainty(process)
                return
        signalled = self._signal(pidfd, process.pid, signal.SIGKILL)
        if not signalled:
            self._retain_cleanup_uncertainty(process)
        try:
            await asyncio.wait_for(asyncio.shield(process.wait()), self.kill_timeout)
        except (TimeoutError, asyncio.CancelledError):
            self._retain_cleanup_uncertainty(process)

    def _reacquire_launched_identity(self, pid: int) -> ProcessIdentity | None:
        """The identity of the direct child at ``pid``, read again for cleanup.

        A read that failed at launch time is often readable a moment later, so
        cleanup re-reads rather than concluding it has no target. Only a live
        entry naming this process as parent can be the child this runner forked;
        :meth:`_is_launched_child` revalidates it once more immediately before
        the signal, with no ``await`` in between.
        """
        evidence = read_identity_evidence(pid, self._proc_root)
        if evidence.state is not IdentityState.present or evidence.parent != os.getpid():
            return None
        return evidence.identity

    def _retain_cleanup_uncertainty(self, process: asyncio.subprocess.Process) -> None:
        """Keep a launch whose cleanup cannot be proven, until it is reaped.

        A cleanup with no exact target may not signal, and a signalled child
        that outlives its deadline cannot be reported gone. Both end in the same
        obligation: the PID is recorded as cleanup-uncertain, and a watcher this
        runner owns keeps the child handle alive so only the kernel's own reaping
        of that exact child clears the record. Completing here instead would
        return while a process the runner started may still be alive.
        """
        pid = process.pid
        if process.returncode is not None:
            self._cleanup_uncertain.discard(pid)
            return
        if pid in self._cleanup_uncertain:
            return
        self._cleanup_uncertain.add(pid)
        self._retain_reaper(asyncio.ensure_future(self._await_reaped(process)))

    async def _await_reaped(self, process: asyncio.subprocess.Process) -> None:
        """Hold a cleanup-uncertain child until its reap is confirmed.

        Cancellation is not confirmation: a watcher stopped before the child is
        reaped leaves the uncertainty recorded, because the runner still cannot
        prove that child gone.
        """
        try:
            await process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        self._cleanup_uncertain.discard(process.pid)

    def _is_launched_child(self, identity: ProcessIdentity) -> bool:
        """Whether ``identity`` still names the direct child this runner forked.

        Re-read now, immediately before the fallback signal: a different start
        time is a recycled PID, and a different parent means this is no longer
        the child that was launched here.
        """
        evidence = read_identity_evidence(identity.pid, self._proc_root)
        return evidence.identity == identity and evidence.parent == os.getpid()

    # -- exit channel -------------------------------------------------------

    async def _monitor(self, child: _Child) -> None:
        """Reap one child and publish its exit, however it ended.

        Even an unexpected failure inside ``wait`` publishes: a caller waiting on
        this child has to learn that it is gone, and a reaper that dies silently
        would look exactly like a child that never exits.
        """
        try:
            code = await child.process.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            # The exit status could not be collected; the child is still gone.
            code = -1
        finally:
            if self._children.get(child.identity.pid) is child:
                del self._children[child.identity.pid]
            if not child.finished.done():
                child.finished.set_result(None)
        self._publish(ports.ProcessExit(identity=child.identity, exit_code=code))

    def _publish(self, event: ports.ProcessExit) -> None:
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception:
                # One broken listener must not stop the notification reaching the
                # others, and its failure text never travels with the event.
                continue

    # -- ownership ----------------------------------------------------------

    def _tracked(self, identity: ProcessIdentity) -> _Child | None:
        child = self._children.get(identity.pid)
        if child is None or child.identity != identity:
            return None
        return child

    def _classify(
        self, identity: ProcessIdentity, owner_uuid: UUID, runtime_directory: str,
    ) -> str:
        """Decide whether ``identity`` is ours, already gone, or not provable."""
        if type(identity) is not ProcessIdentity:
            return _MISMATCH
        evidence = read_identity_evidence(identity.pid, self._proc_root)
        if evidence.state is IdentityState.absent:
            # Only a confirmed kernel answer means gone; ``unreadable`` falls
            # through to the mismatch below instead of guessing a cleanup.
            return _GONE
        if evidence.identity != identity:
            # A different process instance holds this PID now.
            return _MISMATCH
        if evidence.state is IdentityState.terminated:
            # Our exact process is a zombie: nothing of ours can run or be
            # signalled any more, so there is nothing left to terminate.
            return _GONE
        if evidence.state is IdentityState.unreadable:
            return _MISMATCH
        if not owner_evidence(identity, owner_uuid, runtime_directory, self._proc_root):
            return _MISMATCH
        return _OWNED

    def _launch_identity(self, pid: int) -> ProcessIdentity | None:
        """The procfs identity of a child this call has just forked.

        A seam like :meth:`_open_pidfd`: the launch path needs one read of the
        evidence it will later re-verify before any bare-PID signal, and a test
        needs to be able to say "that read failed".
        """
        return read_identity(pid, self._proc_root)

    @staticmethod
    def _open_pidfd(pid: int) -> int | None:
        opener = getattr(os, "pidfd_open", None)
        if opener is None:
            return None
        try:
            return opener(pid, 0)
        except OSError:
            return None

    @staticmethod
    def _close_pidfd(pidfd: int | None) -> None:
        if pidfd is None:
            return
        try:
            os.close(pidfd)
        except OSError:
            pass

    def _deliver(
        self,
        identity: ProcessIdentity,
        owner_uuid: UUID,
        runtime_directory: str,
        pidfd: int | None,
        signum: int,
    ) -> str:
        """Revalidate the exact evidence now, then deliver ``signum``.

        Returns the classification that was re-read immediately before the
        signal: :data:`_OWNED` after the signal was sent, or :data:`_GONE` /
        :data:`_MISMATCH` when nothing was sent. The revalidation and the signal
        are adjacent statements with no ``await`` between them, so nothing of
        ours can hand the PID to another process in between -- which is the
        requirement every signal in this module has to meet, whether it travels
        through a pidfd or through the bare-PID fallback.
        """
        classified = self._classify(identity, owner_uuid, runtime_directory)
        if classified != _OWNED:
            return classified
        self._signal(pidfd, identity.pid, signum)
        return classified

    def _signal(self, pidfd: int | None, pid: int, signum: int) -> bool:
        """Signal the validated instance: through its pidfd when there is one.

        Every caller reaches this from :meth:`_deliver` or
        :meth:`_kill_direct_child`, each of which re-reads the exact evidence
        immediately before the call and is separated from it by no ``await``.
        The bare-PID branch exists for kernels that offer no ``pidfd`` at all;
        it is never a convenience fallback for a descriptor that failed to open
        for an already-checked process.
        """
        sender = getattr(signal, "pidfd_send_signal", None)
        try:
            if pidfd is not None:
                if sender is None:
                    return False
                sender(pidfd, signum)
            else:
                os.kill(pid, signum)
        except OSError:
            return False
        return True

    async def _await_exit(
        self, identity: ProcessIdentity, pidfd: int | None, child: _Child | None, timeout: float,
    ) -> bool:
        """Wait up to ``timeout`` for the exact process to stop running."""
        if child is not None:
            return await self._await_future(child.finished, timeout)
        if pidfd is not None:
            return await self._await_pidfd(pidfd, identity, timeout)
        return await self._poll_gone(identity, timeout)

    @staticmethod
    async def _await_future(future: asyncio.Future[None], timeout: float) -> bool:
        try:
            # Shielded: a timeout must not cancel the monitor's own completion.
            await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            return False
        return True

    async def _await_pidfd(self, pidfd: int, identity: ProcessIdentity, timeout: float) -> bool:
        """Wait for the pidfd to become readable, then confirm the process is gone."""
        loop = asyncio.get_running_loop()
        readable: asyncio.Future[None] = loop.create_future()

        def notify() -> None:
            if not readable.done():
                readable.set_result(None)

        loop.add_reader(pidfd, notify)
        try:
            await asyncio.wait_for(readable, timeout)
        except TimeoutError:
            return False
        finally:
            loop.remove_reader(pidfd)
        return await self._poll_gone(identity, _DRAIN_GRACE)

    async def _poll_gone(self, identity: ProcessIdentity, timeout: float) -> bool:
        """Poll procfs for this exact instance, as the fallback evidence path."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + max(0.0, timeout)
        while True:
            state = read_identity_evidence(identity.pid, self._proc_root).state
            if state in (IdentityState.absent, IdentityState.terminated):
                return True
            if loop.time() >= give_up:
                return False
            await asyncio.sleep(_POLL_INTERVAL)
