"""Wayland ScreenCast portal access for physical-monitor mirror sources.

The portal is asked for exactly one monitor source.  A returned stream is only
bound to the requested connector when its ``source_type`` is ``MONITOR`` and its
``mapping_id`` names that exact connector; Plasma's portal populates
``mapping_id`` with the KWin output name, so no geometry guess or opaque token
is ever treated as a monitor identity.  Any window, virtual-output, region, or
unmappable stream is reported as :attr:`ErrorCode.unsupported_source` instead of
being silently substituted.

Requests are correlated by the Request handle the portal returns, and the
outcome arrives as a ``Response`` signal.  A late response for an abandoned
request therefore cannot satisfy a different acquisition; if a session appears
after its caller was cancelled, this adapter closes it.  Restore tokens stay
inside the adapter-private :class:`PortalMonitor` and are never rendered.

Restore-token expiry is defined as **the portal did not accept the offered
token**, because the ``org.freedesktop.portal.ScreenCast`` contract has no expiry
notification and no distinct expiry error: a token it cannot restore is simply
ignored and the user is prompted normally.  Expiry is therefore never inferred
from a token's contents or from a general ``InvalidArgument`` error, and it never
gets its own :class:`ErrorCode` -- the frozen contract has none, and inventing
one from a generic portal failure would misclassify real denials.  Every failed
selection erases the cached token, so the next attempt reacquires permission
through the portal with no token at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol
from uuid import uuid4

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

from airplay_linux.diagnostics import diagnostic
from airplay_linux.domain.resources import Err, ErrorCode, Ok, Result

PORTAL_DESTINATION: Final[str] = "org.freedesktop.portal.Desktop"
PORTAL_PATH: Final[str] = "/org/freedesktop/portal/desktop"
SCREEN_CAST_INTERFACE: Final[str] = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE: Final[str] = "org.freedesktop.portal.Request"
SESSION_INTERFACE: Final[str] = "org.freedesktop.portal.Session"

#: ``SourceType`` bits from the ScreenCast contract.
SCREEN_CAST_MONITOR: Final[int] = 1
SCREEN_CAST_WINDOW: Final[int] = 2
SCREEN_CAST_VIRTUAL: Final[int] = 4

#: Keep permission only for this application run; this is what yields a token.
PERSIST_WHILE_RUNNING: Final[int] = 1

#: Control calls time out after five seconds (design global constraint).  The
#: deadline bounds the method reply only: the lifetime of a user permission
#: dialog is awaited with no deadline and is bounded by abandonment instead.
CONTROL_DEADLINE: Final[float] = 5.0

_INVALID: Final[object] = object()

#: Sentinel resolved into an abandoned request's future.  ``Request.Close`` makes
#: the portal suppress the ``Response`` signal entirely, so a job that is still
#: waiting for one has to be released by this adapter, never by the portal.
_ABANDONED: Final[object] = object()

#: Sentinel for a control call that neither replied nor was abandoned.
_TIMED_OUT: Final[object] = object()


class PortalPeer(Protocol):
    """The narrow slice of a D-Bus connection this adapter uses.

    A real ``dbus_next.aio.MessageBus`` implements it; the tests supply an
    in-process peer that emits the same messages without opening a bus.
    """

    @property
    def unique_name(self) -> str: ...

    async def call(self, message: Message) -> Message | None: ...

    def add_message_handler(self, handler: Callable[[Message], bool | None]) -> None: ...

    def remove_message_handler(self, handler: Callable[[Message], bool | None]) -> None: ...

    def disconnect(self) -> None: ...


async def _connect_session_bus() -> PortalPeer:
    return await MessageBus().connect()


@dataclass(frozen=True, kw_only=True)
class PortalMonitor:
    """One acquired monitor stream; adapter-private, never a public capture DTO."""

    connector: str
    source_type: Literal["physical_monitor"]
    restore_token: str | None = field(default=None, repr=False)
    portal_session_id: str

    def __post_init__(self) -> None:
        if type(self.connector) is not str or not self.connector:
            raise ValueError("a portal monitor needs a connector identity")
        if self.source_type != "physical_monitor":
            raise ValueError("a portal monitor source type must be physical_monitor")
        if type(self.portal_session_id) is not str or not self.portal_session_id:
            raise ValueError("a portal monitor needs a session identity")
        if self.restore_token is not None and type(self.restore_token) is not str:
            raise TypeError("a restore token must be text or None")

    def __repr__(self) -> str:
        token = "<redacted>" if self.restore_token is not None else None
        return (
            "PortalMonitor("
            f"connector={self.connector!r}, source_type={self.source_type!r}, "
            f"restore_token={token!r}, portal_session_id={self.portal_session_id!r})"
        )


class _Pending:
    """One outstanding Request handle and its correlated Response future."""

    __slots__ = ("token", "paths", "future", "connector", "abandoned", "settled_path")

    def __init__(
        self,
        token: str,
        paths: set[str],
        future: asyncio.Future[tuple[Any, Any]],
        connector: str,
    ) -> None:
        self.token = token
        self.paths = paths
        self.future = future
        self.connector = connector
        self.abandoned = False
        #: The exact request path the buffered ``Response`` arrived on, so the
        #: cleanup of a forgotten waiter still retires the right handle.
        self.settled_path: str | None = None


class _Acquisition:
    """State that must survive cancellation of the caller's await."""

    __slots__ = (
        "connector",
        "restore_token",
        "abandoned",
        "rolled_back",
        "session",
        "requests",
        "pending",
        "released",
    )

    def __init__(self, connector: str, restore_token: str | None) -> None:
        self.connector = connector
        self.restore_token = restore_token
        self.abandoned = False
        self.rolled_back = False
        self.session: str | None = None
        self.requests: list[str] = []
        self.pending: list[_Pending] = []
        #: Set once the caller is gone; every acquisition wait races this, so no
        #: job can stay blocked on a reply or a signal that will never arrive.
        self.released = asyncio.Event()

    def release_waiters(self) -> None:
        """Unblock everything this acquisition can still be waiting on."""
        self.abandoned = True
        self.released.set()
        for pending in tuple(self.pending):
            pending.abandoned = True
            if not pending.future.done():
                pending.future.set_result((_ABANDONED, None))
        self.pending.clear()


def _failure(code: ErrorCode) -> Err:
    return Err(error=diagnostic(code))


def _unwrap(value: object) -> object:
    if type(value) is Variant:
        return value.value
    return value


def _optional_text(value: object) -> str | None | object:
    value = _unwrap(value)
    if value is None:
        return None
    if type(value) is str:
        return value or None
    return _INVALID


def _new_token() -> str:
    # A valid object-path element; it is a request handle token, not a secret.
    return "airplay" + uuid4().hex


def _response_code(response: object) -> Result[None]:
    code = _unwrap(response)
    if type(code) is not int:
        return _failure(ErrorCode.dependency_incompatible)
    if code == 0:
        return Ok(value=None)
    if code == 1:
        return _failure(ErrorCode.portal_cancelled)
    if code == 2:
        return _failure(ErrorCode.portal_denied)
    return _failure(ErrorCode.dependency_incompatible)


def _error_code(error_name: object) -> ErrorCode:
    if type(error_name) is not str or not error_name:
        return ErrorCode.dependency_incompatible
    suffix = error_name.rsplit(".", 1)[-1].lower()
    if suffix == "cancelled":
        return ErrorCode.portal_cancelled
    if suffix in {"denied", "notallowed", "notpermitted", "failed", "invalidargument", "notfound"}:
        return ErrorCode.portal_denied
    if suffix in {"serviceunknown", "namehasnoowner"}:
        return ErrorCode.dependency_missing
    return ErrorCode.dependency_incompatible


def _already_gone(error_name: object) -> bool:
    if type(error_name) is not str:
        return False
    return error_name.rsplit(".", 1)[-1].lower() in {
        "serviceunknown",
        "namehasnoowner",
        "unknownobject",
    }


def _returned_handle(reply: object) -> str | None:
    """The Request object path a portal method-return handed back, if any."""
    if type(reply) is not Message or reply.message_type is not MessageType.METHOD_RETURN:
        return None
    body = reply.body
    if type(body) not in (list, tuple) or len(body) != 1:
        return None
    handle = body[0]
    if type(handle) is not str or not handle.startswith("/"):
        return None
    return handle


def _bind_monitor(connector: str, streams: object) -> Result[None]:
    """Accept exactly one monitor stream whose mapping names ``connector``."""
    streams = _unwrap(streams)
    if type(streams) not in (list, tuple) or len(streams) != 1:
        return _failure(ErrorCode.unsupported_source)
    stream = streams[0]
    if type(stream) not in (list, tuple) or len(stream) != 2:
        return _failure(ErrorCode.unsupported_source)
    node_id, properties = stream
    if type(_unwrap(node_id)) is not int:
        return _failure(ErrorCode.unsupported_source)
    properties = _unwrap(properties)
    if type(properties) is not dict:
        return _failure(ErrorCode.unsupported_source)
    source_type = _unwrap(properties.get("source_type"))
    if type(source_type) is not int or source_type != SCREEN_CAST_MONITOR:
        return _failure(ErrorCode.unsupported_source)
    mapping_id = _unwrap(properties.get("mapping_id"))
    if type(mapping_id) is not str or mapping_id != connector:
        # No reliable connector/type verification means no physical source.
        return _failure(ErrorCode.unsupported_source)
    return Ok(value=None)


class PortalClient:
    """Acquire and release monitor-only ScreenCast sessions.

    ``peer`` is a test seam for an existing connection.  Without it the client
    opens the session bus on first use and owns (and disconnects) that peer.
    """

    def __init__(
        self,
        peer: PortalPeer | None = None,
        *,
        connect: Callable[[], Awaitable[PortalPeer]] | None = None,
        deadline: float = CONTROL_DEADLINE,
    ) -> None:
        if peer is not None and connect is not None:
            raise ValueError("provide a peer or a connect callable, not both")
        self._peer = peer
        self._connect = connect or _connect_session_bus
        self._deadline = deadline
        self._owns_peer = False
        self._handler_installed = False
        self._pending: dict[str, _Pending] = {}
        self._sessions: dict[str, str] = {}
        #: Request handles this client still owns: the portal has not confirmed
        #: their Close, so a session they create still needs an exact owner.
        self._requests: dict[str, str] = {}
        #: Request handles that are finished: the portal confirmed their Close or
        #: the ``Response`` was consumed.  ``Request.Close`` suppresses a pending
        #: Response, but one already in flight is still reaped; nothing here
        #: blocks finalisation.
        self._retired: dict[str, str] = {}
        #: Request handles with a Close method call currently in flight.
        self._closing_requests: set[str] = set()
        self._jobs: set[asyncio.Task[Any]] = set()
        self._acquisitions: dict[asyncio.Task[Any], _Acquisition] = {}
        self._replies: set[asyncio.Task[Any]] = set()
        self._abandoned: set[asyncio.Task[Any]] = set()
        self._cleanup: set[asyncio.Task[Any]] = set()
        self._closed = False
        self._destroyed = False
        self._close_lock = asyncio.Lock()

    # -- public interface ---------------------------------------------------

    async def select_monitor(
        self, connector: str, restore_token: str | None
    ) -> Result[PortalMonitor]:
        """Open one monitor-only session and bind it to ``connector``."""
        if self._closed:
            return _failure(ErrorCode.unsupported_operation)
        if type(connector) is not str or not connector:
            return _failure(ErrorCode.invalid_input)
        if restore_token is not None and type(restore_token) is not str:
            return _failure(ErrorCode.invalid_input)
        try:
            await self._ensure_peer()
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure(ErrorCode.dependency_missing)

        acquisition = _Acquisition(connector, restore_token)
        job = asyncio.ensure_future(self._acquire(acquisition))
        self._jobs.add(job)
        self._acquisitions[job] = acquisition
        job.add_done_callback(self._job_complete)
        try:
            return await asyncio.shield(job)
        except asyncio.CancelledError:
            self._abandon(acquisition, job)
            if self._closed:
                return _failure(ErrorCode.portal_cancelled)
            raise

    async def release_monitor(self, monitor: PortalMonitor) -> Result[None]:
        """Close exactly the session represented by ``monitor``, idempotently."""
        if type(monitor) is not PortalMonitor:
            return _failure(ErrorCode.invalid_input)
        connector = self._sessions.get(monitor.portal_session_id)
        if connector is None:
            return Ok(value=None)
        if connector != monitor.connector or monitor.source_type != "physical_monitor":
            return _failure(ErrorCode.stale_event)
        return await self._close_session(monitor.portal_session_id)

    def is_open(self, monitor: PortalMonitor) -> bool:
        """Whether this exact monitor's session is still tracked as open."""
        if self._closed:
            # A closing client cannot vouch for a resource it still has to clean up.
            return False
        return (
            type(monitor) is PortalMonitor
            and monitor.source_type == "physical_monitor"
            and self._sessions.get(monitor.portal_session_id) == monitor.connector
        )

    async def drain(self) -> None:
        """Await acquisitions and cleanup that outlived a cancelled caller."""
        while self._jobs or self._cleanup:
            tasks = tuple(self._jobs | self._cleanup)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)
            # ``gather`` returns without suspending when every task already
            # finished, and the completion callbacks that reap these records run
            # on the next loop pass.  Yield so draining cannot spin the loop.
            await asyncio.sleep(0)

    @property
    def _orphaned(self) -> dict[str, str]:
        """Request handles still awaiting confirmation (read-only legacy view)."""
        return {**self._retired, **self._requests}

    async def close(self) -> Result[None]:
        """Refuse new selections and confirm every resource this client owns.

        ``Ok`` means every exact session was confirmed closed and every request
        this client opened is either settled by its ``Response`` or confirmed
        closed.  Otherwise the uncertain records are retained and the handler
        stays installed, so the portal can still report a late completion and
        calling ``close()`` again advances the same cleanup instead of
        finalising over an unconfirmed resource.

        A barrier that has to abandon a request whose ``Response`` never
        arrived cannot prove the portal suppresses that completion, so it
        reports ``cleanup_timeout`` once; the next ``close()`` observes the
        confirmed state and finalises.  A request abandoned before the barrier
        (by a caller's own cancellation) is already reconciled by the time the
        barrier runs.
        """
        async with self._close_lock:
            if self._destroyed:
                return Ok(value=None)
            self._closed = True
            abandoned_by_barrier: set[str] = set()
            for job in tuple(self._jobs):
                acquisition = self._acquisitions.get(job)
                if acquisition is not None:
                    # Reconcile in flight work instead of dropping its handler.
                    abandoned_by_barrier |= self._abandon(acquisition, job)
                elif not job.done():
                    job.cancel()
            await self.drain()

            unconfirmed = False
            for session in tuple(self._sessions):
                if isinstance(await self._close_session(session), Err):
                    unconfirmed = True
            # Retry every request whose Close is not confirmed yet; a bounded
            # retry is what lets a later call advance an uncertain record.
            for path in tuple(self._requests):
                await self._close_request(path)

            # A request this barrier had to abandon is only sure to be free of a
            # late session once a following barrier observes no new completion.
            barrier_uncertain = any(
                path in self._retired for path in abandoned_by_barrier
            )
            if (
                unconfirmed
                or barrier_uncertain
                or bool(self._jobs)
                or bool(self._cleanup)
                or bool(self._requests)
                or bool(self._replies)
            ):
                return _failure(ErrorCode.cleanup_timeout)
            self._finalise()
            return Ok(value=None)

    def _finalise(self) -> None:
        """Detach the handler and drop a connection this client owns."""
        peer = self._peer
        if peer is not None and self._handler_installed:
            peer.remove_message_handler(self._on_message)
            self._handler_installed = False
        if peer is not None and self._owns_peer:
            try:
                peer.disconnect()
            except Exception:
                pass
        self._peer = None
        self._destroyed = True
        self._pending.clear()
        self._sessions.clear()
        self._requests.clear()
        self._retired.clear()

    # -- acquisition --------------------------------------------------------

    async def _ensure_peer(self) -> PortalPeer:
        if self._peer is None:
            self._peer = await self._connect()
            self._owns_peer = True
        assert self._peer is not None
        if not self._handler_installed:
            self._peer.add_message_handler(self._on_message)
            self._handler_installed = True
        return self._peer

    def _abandon(
        self, acquisition: _Acquisition, job: asyncio.Task[Any]
    ) -> set[str]:
        """Stop waiting for a portal that will never answer this request again.

        Ownership is kept, never dropped: a ``Response`` already buffered in the
        pending future, and every request handle the acquisition opened, stay
        with this client until an exact Close is confirmed.  The returned paths
        are the ones this call had to take over, so a shutdown barrier can tell
        "already reconciled" from "this barrier races a completion".
        """
        abandoned: set[str] = set()
        for pending in tuple(acquisition.pending):
            self._retain_pending(pending, acquisition)
            abandoned.update(path for path in pending.paths if path in self._requests)
        for path in tuple(acquisition.requests):
            if path not in self._requests:
                continue
            abandoned.add(path)
            self._own_request(path, acquisition.connector)
        self._kick_request_closes()
        acquisition.release_waiters()
        self._abandoned.add(job)
        if job.done():
            # The outcome raced the cancellation and the caller will never see it.
            self._reconcile(job)
        return abandoned

    def _job_complete(self, task: asyncio.Task[Any]) -> None:
        self._jobs.discard(task)
        self._acquisitions.pop(task, None)
        if task not in self._abandoned:
            return
        self._abandoned.discard(task)
        self._reconcile(task)

    def _reconcile(self, task: asyncio.Task[Any]) -> None:
        """Release an acquisition that succeeded even though its caller left."""
        if not task.done() or task.cancelled():
            return
        try:
            result = task.result()
        except Exception:
            return
        if isinstance(result, Ok) and type(result.value) is PortalMonitor:
            # A success that raced the caller's cancellation is still releasable.
            self._track_cleanup(self.release_monitor(result.value))

    def _track_cleanup(self, awaitable: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(awaitable)
        self._cleanup.add(task)
        task.add_done_callback(self._cleanup.discard)
        return task

    async def _acquire(self, acquisition: _Acquisition) -> Result[PortalMonitor]:
        try:
            created = await self._request(
                acquisition,
                "CreateSession",
                options={"session_handle_token": Variant("s", _new_token())},
            )
            if isinstance(created, Err):
                return created
            session = _unwrap(created.value.get("session_handle"))
            if type(session) is not str or not session.startswith("/"):
                return await self._fail(acquisition, ErrorCode.dependency_incompatible)
            acquisition.session = session
            self._sessions[session] = acquisition.connector
            if (abandoned := await self._abandoned_result(acquisition)) is not None:
                return abandoned

            options: dict[str, Variant] = {
                "types": Variant("u", SCREEN_CAST_MONITOR),
                "multiple": Variant("b", False),
                "persist_mode": Variant("u", PERSIST_WHILE_RUNNING),
            }
            if acquisition.restore_token is not None:
                options["restore_token"] = Variant("s", acquisition.restore_token)
            selected = await self._request(
                acquisition, "SelectSources", options=options, session=session
            )
            if isinstance(selected, Err):
                return await self._fail(acquisition, selected.error.code)
            if (abandoned := await self._abandoned_result(acquisition)) is not None:
                return abandoned

            started = await self._request(
                acquisition, "Start", options={}, session=session, parent_window=""
            )
            if isinstance(started, Err):
                return await self._fail(acquisition, started.error.code)
            if (abandoned := await self._abandoned_result(acquisition)) is not None:
                return abandoned

            bound = _bind_monitor(acquisition.connector, started.value.get("streams"))
            if isinstance(bound, Err):
                return await self._fail(acquisition, bound.error.code)
            token = _optional_text(started.value.get("restore_token"))
            if token is _INVALID:
                return await self._fail(acquisition, ErrorCode.dependency_incompatible)
            monitor = PortalMonitor(
                connector=acquisition.connector,
                source_type="physical_monitor",
                restore_token=token,  # type: ignore[arg-type]
                portal_session_id=session,
            )
            if (abandoned := await self._abandoned_result(acquisition)) is not None:
                return abandoned
            return Ok(value=monitor)
        except asyncio.CancelledError:
            await self._rollback(acquisition)
            raise
        except Exception:
            await self._rollback(acquisition)
            return _failure(ErrorCode.dependency_incompatible)

    async def _abandoned_result(self, acquisition: _Acquisition) -> Result[PortalMonitor] | None:
        if not acquisition.abandoned:
            return None
        await self._rollback(acquisition)
        return _failure(ErrorCode.portal_cancelled)

    async def _fail(
        self, acquisition: _Acquisition, code: ErrorCode
    ) -> Result[PortalMonitor]:
        closed = await self._rollback(acquisition)
        if not closed:
            return _failure(ErrorCode.cleanup_timeout)
        return _failure(code)

    async def _rollback(self, acquisition: _Acquisition) -> bool:
        """Close the session and hand every open request to exact cleanup."""
        if acquisition.rolled_back:
            return True
        acquisition.rolled_back = True
        session, acquisition.session = acquisition.session, None
        closed = True
        if session is not None:
            result = await self._close_session(session)
            closed = isinstance(result, Ok)
        self._retain_pending_requests(acquisition)
        await self._drain_request_closes()
        return closed

    def _retain_pending_requests(self, acquisition: _Acquisition) -> None:
        """Keep exact ownership of every request this acquisition still owns."""
        for path in tuple(acquisition.requests):
            self._own_request(path, acquisition.connector)
        self._kick_request_closes()

    async def _drain_request_closes(self) -> None:
        """Await the request closes this client just scheduled."""
        tasks = tuple(self._cleanup)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- request/response correlation --------------------------------------

    async def _request(
        self,
        acquisition: _Acquisition,
        member: str,
        *,
        options: dict[str, Variant] | None = None,
        session: str | None = None,
        parent_window: str | None = None,
    ) -> Result[dict[str, Any]]:
        peer = self._peer
        assert peer is not None
        if acquisition.abandoned:
            # The caller is gone: never open another portal request for it.
            return _failure(ErrorCode.portal_cancelled)
        token = _new_token()
        merged = dict(options or {})
        merged["handle_token"] = Variant("s", token)
        expected = self._request_path(token)
        pending = _Pending(
            token=token,
            paths={expected},
            future=asyncio.get_running_loop().create_future(),
            connector=acquisition.connector,
        )
        self._pending[token] = pending
        acquisition.pending.append(pending)
        acquisition.requests.append(expected)
        try:
            message = self._call_message(member, merged, session, parent_window)
            call = asyncio.ensure_future(peer.call(message))
            self._replies.add(call)
            call.add_done_callback(
                lambda settled: self._settle_call(settled, pending, acquisition)
            )
            reply = await self._watch(acquisition, call)
        except asyncio.CancelledError:
            self._retain_pending(pending, acquisition)
            self._forget(pending, acquisition)
            raise
        except Exception:
            self._forget(pending, acquisition)
            self._release_requests(pending.paths)
            return _failure(ErrorCode.dependency_incompatible)
        if reply is _ABANDONED or reply is _TIMED_OUT:
            # Abandonment and the control deadline both leave the request's real
            # outcome unknown, so keep it owned.  A success already buffered in
            # the future is reaped rather than forgotten with the waiter.
            self._retain_pending(pending, acquisition)
            self._forget(pending, acquisition)
            if reply is _ABANDONED:
                return _failure(ErrorCode.portal_cancelled)
            return _failure(ErrorCode.cleanup_timeout)
        if reply is None:
            self._forget(pending, acquisition)
            self._release_requests(pending.paths)
            return _failure(ErrorCode.dependency_incompatible)
        if reply.message_type is MessageType.ERROR:
            # The method call failed: the portal never handed us a Request.
            self._forget(pending, acquisition)
            self._release_requests(pending.paths)
            return _failure(_error_code(reply.error_name))
        if reply.message_type is not MessageType.METHOD_RETURN:
            self._retain_pending(pending, acquisition)
            self._forget(pending, acquisition)
            return _failure(ErrorCode.dependency_incompatible)
        body = reply.body
        if type(body) not in (list, tuple) or len(body) != 1 or type(body[0]) is not str:
            self._retain_pending(pending, acquisition)
            self._forget(pending, acquisition)
            return _failure(ErrorCode.dependency_incompatible)
        returned = body[0]
        if not returned:
            self._retain_pending(pending, acquisition)
            self._forget(pending, acquisition)
            return _failure(ErrorCode.dependency_incompatible)
        if returned != expected:
            # The portal may hand back a different Request handle; its Response
            # arrives on that path, so correlate on both known paths.
            pending.paths.add(returned)
            acquisition.requests.append(returned)
            self._own_request(returned, acquisition.connector)
        try:
            response, results = await pending.future
        finally:
            self._forget(pending, acquisition)
        if response is _ABANDONED:
            # The caller left while the request was open: the completion that
            # owns the resource has already been taken over by ``_abandon``.
            self._retain_pending(pending, acquisition)
            return _failure(ErrorCode.portal_cancelled)
        # A delivered Response finishes this Request: nothing is left to close.
        self._retire_settled(pending.paths)
        mapped = _response_code(response)
        if isinstance(mapped, Err):
            return mapped
        if type(results) is not dict:
            return _failure(ErrorCode.dependency_incompatible)
        return Ok(value=results)

    def _own_request(self, path: str, connector: str) -> None:
        """Take exact ownership of one Request handle, idempotently."""
        if not path or path in self._retired:
            return
        self._requests.setdefault(path, connector)

    def _release_requests(self, paths: object) -> None:
        """Drop ownership of handles the portal never created for this client."""
        for path in paths:  # type: ignore[union-attr]
            if path in self._requests and path not in self._closing_requests:
                self._requests.pop(path, None)

    def _retire_settled(self, paths: object) -> None:
        """A consumed Response finishes a Request; keep no cleanup obligation."""
        for path in paths:  # type: ignore[union-attr]
            if path in self._closing_requests:
                continue
            connector = self._requests.pop(path, None)
            if connector is not None:
                self._retired[path] = connector

    def _retain_pending(self, pending: _Pending, acquisition: _Acquisition) -> None:
        """Keep everything one abandoned request can still own.

        ``Response`` may already have been delivered into the pending future
        before the caller left; that success names a live session, so it is
        reaped here instead of being discarded with the waiter.
        """
        if pending.future.done() and not pending.future.cancelled():
            buffered = pending.future.result()
            if (
                type(buffered) is tuple
                and len(buffered) == 2
                and buffered[0] is not _ABANDONED
            ):
                path = pending.settled_path or next(iter(pending.paths), "")
                self._reap_response(buffered, pending.connector, path)
        for path in pending.paths:
            self._own_request(path, acquisition.connector)
        self._kick_request_closes()

    def _settle_call(
        self,
        call: asyncio.Task[Any],
        pending: _Pending,
        acquisition: _Acquisition,
    ) -> None:
        """Learn a late Request handle and cancel it if nobody is waiting."""
        self._replies.discard(call)
        if call.cancelled():
            return
        try:
            reply = call.result()
        except BaseException:
            return
        returned = _returned_handle(reply)
        if returned is None:
            return
        pending.paths.add(returned)
        if returned not in acquisition.requests:
            acquisition.requests.append(returned)
        unwatched = (
            self._closed
            or pending.abandoned
            or self._pending.get(pending.token) is not pending
        )
        if not unwatched:
            # A live waiter correlates this handle and delays its Close until the
            # Response arrives; owning it early keeps it from escaping.
            self._own_request(returned, acquisition.connector)
            return
        self._own_request(returned, acquisition.connector)
        self._kick_request_closes()

    def _forget(self, pending: _Pending, acquisition: _Acquisition) -> None:
        self._pending.pop(pending.token, None)
        try:
            acquisition.pending.remove(pending)
        except ValueError:
            return

    async def _watch(
        self,
        acquisition: _Acquisition | None,
        call: asyncio.Task[Any],
    ) -> Message | None | object:
        """Await one portal call and stop at abandonment or the control deadline.

        Abandonment matters because ``Request.Close`` suppresses the ``Response``
        a job may be waiting for; the deadline matters because an unresponsive
        portal must never stop Stop, exact release, or shutdown.  The caller
        keeps ownership of whatever the overrunning call may still create.
        """
        peer = self._peer
        if peer is None:
            return None
        waiters: set[asyncio.Task[Any]] = {call}
        released: asyncio.Task[Any] | None = None
        if acquisition is not None:
            released = asyncio.ensure_future(acquisition.released.wait())
            waiters.add(released)
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._deadline,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if released is not None and not released.done():
                released.cancel()
        if call in done:
            try:
                return call.result()
            except asyncio.CancelledError:
                raise
            except Exception:
                return None
        return _ABANDONED if released is not None and released in done else _TIMED_OUT

    def _call_message(
        self,
        member: str,
        options: dict[str, Variant],
        session: str | None,
        parent_window: str | None,
    ) -> Message:
        if member == "CreateSession":
            signature, body = "a{sv}", [options]
        elif member == "SelectSources":
            signature, body = "oa{sv}", [session, options]
        elif member == "Start":
            signature, body = "osa{sv}", [session, parent_window or "", options]
        else:
            raise ValueError(f"unsupported ScreenCast member: {member}")
        return Message(
            destination=PORTAL_DESTINATION,
            path=PORTAL_PATH,
            interface=SCREEN_CAST_INTERFACE,
            member=member,
            signature=signature,
            body=body,
        )

    def _request_path(self, token: str) -> str:
        peer = self._peer
        name = peer.unique_name if peer is not None else ""
        sender = name.removeprefix(":").replace(".", "_") or "unknown"
        return f"{PORTAL_PATH}/request/{sender}/{token}"

    def _on_message(self, message: Message) -> bool:
        if message.message_type is not MessageType.SIGNAL:
            return False
        path = message.path
        if type(path) is not str:
            return False
        if message.interface == SESSION_INTERFACE and message.member == "Closed":
            # The portal may close a session at any time; that lease is dead.
            return self._on_session_closed(path)
        if message.interface != REQUEST_INTERFACE or message.member != "Response":
            return False
        body = message.body
        if type(body) not in (list, tuple) or len(body) != 2:
            return False
        pending = self._pending_for(path)
        if pending is not None:
            if not pending.future.done():
                pending.settled_path = path
                pending.future.set_result((body[0], body[1]))
            elif pending.abandoned:
                # Settled by abandonment: a success that still arrives owns a
                # live session, so reap it instead of dropping the signal.
                self._reap_response(body, pending.connector, path)
            return True
        connector = self._requests.get(path) or self._retired.get(path)
        if connector is not None:
            # No caller is waiting, but this exact handle still owns whatever
            # the completion creates.
            self._reap_response(body, connector, path)
            return True
        return False

    def _reap_response(self, body: object, connector: str, path: str) -> None:
        """Take ownership of a completion no caller is waiting for."""
        self._track_cleanup(self._reap_request(body, connector, path))

    async def _reap_request(self, body: object, connector: str, path: str) -> None:
        """Close a session created by a response no caller is waiting for."""
        if path:
            self._retired.pop(path, None)
            self._requests.pop(path, None)
        if type(body) not in (list, tuple) or len(body) != 2:
            return
        if isinstance(_response_code(body[0]), Err):
            return
        results = _unwrap(body[1])
        if type(results) is not dict:
            return
        session = _unwrap(results.get("session_handle"))
        if type(session) is not str or not session.startswith("/"):
            return
        self._sessions.setdefault(session, connector)
        await self._close_session(session)

    def _on_session_closed(self, path: str) -> bool:
        """Drop liveness for an exact session the portal reports as closed."""
        return self._sessions.pop(path, None) is not None

    def _pending_for(self, path: str) -> _Pending | None:
        """Correlate by the exact request path, including a returned handle."""
        for pending in self._pending.values():
            if path in pending.paths:
                return pending
        return None

    # -- teardown -----------------------------------------------------------

    async def _close_session(self, session: str) -> Result[None]:
        if session not in self._sessions:
            return Ok(value=None)
        peer = self._peer
        if peer is None:
            return _failure(ErrorCode.cleanup_timeout)
        try:
            message = Message(
                destination=PORTAL_DESTINATION,
                path=session,
                interface=SESSION_INTERFACE,
                member="Close",
            )
            reply = await self._watch(None, asyncio.ensure_future(peer.call(message)))
        except asyncio.CancelledError:
            raise
        except Exception:
            return _failure(ErrorCode.cleanup_timeout)
        if reply is _ABANDONED or reply is _TIMED_OUT:
            # Unconfirmed release: keep the exact session so a retry can confirm it.
            return _failure(ErrorCode.cleanup_timeout)
        if not isinstance(reply, Message):
            return _failure(ErrorCode.cleanup_timeout)
        if reply.message_type is MessageType.ERROR:
            if _already_gone(reply.error_name):
                self._sessions.pop(session, None)
                return Ok(value=None)
            return _failure(ErrorCode.cleanup_timeout)
        if reply.message_type is not MessageType.METHOD_RETURN:
            return _failure(ErrorCode.cleanup_timeout)
        self._sessions.pop(session, None)
        return Ok(value=None)

    def _kick_request_closes(self) -> None:
        """Start a bounded Close for every owned request that has none in flight."""
        for path in tuple(self._requests):
            if path in self._closing_requests:
                continue
            self._track_cleanup(self._close_request(path))

    async def _close_request(self, path: str) -> bool:
        """Close one exact Request handle; report whether the portal confirmed it.

        A confirmed Close is the transition that retires an abandoned request:
        the contract says its ``Response`` is suppressed, so waiting for one
        would keep ``close()`` from ever finalising.  Any reply (a method return
        or an error such as ``UnknownObject``) is a confirmation; a timeout or a
        transport failure keeps the handle owned so a retry can advance it.
        """
        if not path:
            return True
        if path in self._closing_requests:
            return path not in self._requests
        peer = self._peer
        if peer is None:
            return False
        self._closing_requests.add(path)
        try:
            message = Message(
                destination=PORTAL_DESTINATION,
                path=path,
                interface=REQUEST_INTERFACE,
                member="Close",
            )
            reply = await self._watch(None, asyncio.ensure_future(peer.call(message)))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A peer-supplied handle that is not a valid object path cannot be
            # closed; cancellation cleanup stays best-effort and never raises.
            return False
        finally:
            self._closing_requests.discard(path)
        if not isinstance(reply, Message):
            return False
        connector = self._requests.pop(path, None)
        if connector is None:
            connector = self._retired.get(path)
        if connector is not None:
            self._retired[path] = connector
        return True


__all__ = ["PortalClient"]
