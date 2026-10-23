"""ScreenCast portal client: monitor-only selection, correlation, and cleanup.

The fake peer below speaks the same D-Bus message shapes as the real
``org.freedesktop.portal.ScreenCast`` service: a method call returns a Request
handle and the outcome arrives later as a ``Response`` signal on that request
path.  Nothing here opens a bus, prompts a desktop, or captures a screen.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from dbus_next import Message, MessageType, Variant

from airplay_linux.capture.portal import PortalClient
from airplay_linux.domain.resources import Err, ErrorCode, Ok

PORTAL_DESTINATION = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SCREEN_CAST_INTERFACE = "org.freedesktop.portal.ScreenCast"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"
SESSION_INTERFACE = "org.freedesktop.portal.Session"

MONITOR = 1
WINDOW = 2
VIRTUAL = 4


@dataclass(frozen=True)
class RecordedCall:
    """One method the client sent, with the tokens it chose for the handles."""

    member: str
    message: Message
    handle_token: str | None
    session_handle_token: str | None


class FakePortalPeer:
    """A deterministic in-process peer for the ScreenCast D-Bus surface."""

    def __init__(self, unique_name: str = ":1.42") -> None:
        self.unique_name = unique_name
        self.calls: list[RecordedCall] = []
        self.handlers: list[Any] = []
        self.open_sessions: set[str] = set()
        self.closed_requests: list[str] = []
        self.closed_sessions: list[str] = []
        self.error_replies: dict[str, str] = {}
        self.handle_override: dict[str, str] = {}
        self.held: dict[str, asyncio.Event] = {}
        self.disconnected = False
        self._next_serial = 0
        self._condition = asyncio.Condition()

    # -- request/session identities ----------------------------------------

    @property
    def sender(self) -> str:
        return self.unique_name.removeprefix(":").replace(".", "_")

    def request_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/request/{self.sender}/{token}"

    def session_path(self, token: str) -> str:
        return f"{PORTAL_PATH}/session/{self.sender}/{token}"

    # -- peer protocol ------------------------------------------------------

    def add_message_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def remove_message_handler(self, handler: Any) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)

    def disconnect(self) -> None:
        self.disconnected = True

    async def call(self, message: Message) -> Message | None:
        options: dict[str, Any] = {}
        if message.body and isinstance(message.body[-1], dict):
            options = message.body[-1]
        # A real bus stamps an outgoing method call with a serial before the
        # service sees it; replies are built with ``Message.new_method_return``,
        # which requires that serial as ``reply_serial``.
        if not message.serial:
            self._next_serial += 1
            message.serial = self._next_serial
        handle_token = options.get("handle_token")
        session_token = options.get("session_handle_token")
        handle_token_value = (
            handle_token.value if isinstance(handle_token, Variant) else handle_token
        )
        self.calls.append(
            RecordedCall(
                member=message.member,
                message=message,
                handle_token=handle_token_value,
                session_handle_token=(
                    session_token.value if isinstance(session_token, Variant) else None
                ),
            )
        )
        async with self._condition:
            self._condition.notify_all()

        if message.interface == REQUEST_INTERFACE and message.member == "Close":
            self.closed_requests.append(message.path)
            return Message.new_method_return(message)
        if message.interface == SESSION_INTERFACE and message.member == "Close":
            self.closed_sessions.append(message.path)
            self.open_sessions.discard(message.path)
            return Message.new_method_return(message)

        hold = self.held.get(message.member)
        if hold is not None:
            await hold.wait()

        error = self.error_replies.get(message.member)
        if error is not None:
            return Message.new_error(message, error, "synthetic portal failure")
        handle = self.handle_override.get(message.member) or self.request_path(
            handle_token_value or ""
        )
        return Message.new_method_return(message, "o", [handle])

    # -- test controls ------------------------------------------------------

    def last(self, member: str) -> RecordedCall:
        for call in reversed(self.calls):
            if call.member == member:
                return call
        raise AssertionError(f"{member} was never called")

    def count(self, member: str) -> int:
        return sum(1 for call in self.calls if call.member == member)

    def hold(self, member: str) -> asyncio.Event:
        """Make one method's reply never arrive until the test releases it."""
        event = asyncio.Event()
        self.held[member] = event
        return event

    def release_hold(self, member: str) -> None:
        event = self.held.pop(member, None)
        if event is not None:
            event.set()

    async def wait_for(self, member: str, count: int = 1) -> None:
        async with self._condition:
            await self._condition.wait_for(
                lambda: sum(1 for call in self.calls if call.member == member) >= count
            )

    def respond(
        self,
        member: str,
        response: int,
        results: dict[str, Any] | None = None,
        *,
        call: RecordedCall | None = None,
        path: str | None = None,
    ) -> bool:
        call = call or self.last(member)
        payload = {} if results is None else results
        target = path or self.handle_override.get(member) or self.request_path(
            call.handle_token or ""
        )
        signal = Message(
            message_type=MessageType.SIGNAL,
            path=target,
            interface=REQUEST_INTERFACE,
            member="Response",
            signature="ua{sv}",
            body=[response, payload],
            sender=PORTAL_DESTINATION,
        )
        for handler in tuple(self.handlers):
            if handler(signal):
                # Only a delivered response can hand this client the session;
                # a response on a foreign request path is not ours to track.
                if response == 0 and member == "CreateSession":
                    session = payload.get("session_handle")
                    if isinstance(session, Variant):
                        session = session.value
                    if isinstance(session, str):
                        self.open_sessions.add(session)
                return True
        return False

    def respond_create(self, *, session_token: str = "s1", response: int = 0) -> bool:
        if response != 0:
            return self.respond("CreateSession", response, {})
        return self.respond(
            "CreateSession",
            0,
            {"session_handle": Variant("s", self.session_path(session_token))},
        )

    @staticmethod
    def monitor_streams(
        connector: str,
        *,
        source_type: int = MONITOR,
        mapping_id: str | None | object = ...,
        node_id: int = 7,
    ) -> list[list[Any]]:
        properties: dict[str, Variant] = {
            # dbus-next verifies STRUCT bodies as Python lists, which is also how
            # a struct arrives in a parsed message body.
            "position": Variant("(ii)", [0, 0]),
            "size": Variant("(ii)", [1920, 1080]),
            "source_type": Variant("u", source_type),
        }
        if mapping_id is ...:
            mapping_id = connector
        if mapping_id is not None:
            properties["mapping_id"] = Variant("s", mapping_id)
        return [[node_id, properties]]

    def respond_start(
        self,
        connector: str,
        *,
        source_type: int = MONITOR,
        mapping_id: str | None | object = ...,
        restore_token: str | None = "new-token",
        streams: list[list[Any]] | None = None,
        response: int = 0,
    ) -> bool:
        if response != 0:
            return self.respond("Start", response, {})
        selected = streams if streams is not None else self.monitor_streams(
            connector, source_type=source_type, mapping_id=mapping_id
        )
        results: dict[str, Any] = {"streams": Variant("a(ua{sv})", selected)}
        if restore_token is not None:
            results["restore_token"] = Variant("s", restore_token)
        return self.respond("Start", 0, results)


async def settle(cycles: int = 12) -> None:
    """Let the client reach the point where it waits for the Response signal.

    A real bus delivers the method reply for a Request handle before the portal
    can emit the ``Response`` signal, but the client needs a bounded number of
    scheduler hand-offs to store that correlated path (it does not block its
    event loop, and the acquisition races abandonment internally).  Yielding a
    fixed, generous number of times keeps the test deterministic without
    sleeping on the wall clock.
    """
    for _ in range(cycles):
        await asyncio.sleep(0)


async def complete_selection(
    client: PortalClient,
    peer: FakePortalPeer,
    connector: str = "DP-1",
    *,
    restore_token: str | None = None,
    session_token: str = "s1",
    start_kwargs: dict[str, Any] | None = None,
    start_response: int = 0,
) -> Any:
    """Drive one selection through every request/response exchange.

    Each exchange settles after the method call is observed: a real bus
    delivers the Request-handle reply before the portal can emit the Response
    signal, so the client always knows the correlated path first.  Counts are
    relative to the calls already made, so a peer reused for a second
    selection waits for the new exchange rather than an earlier one.
    """
    created = peer.count("CreateSession")
    selected = peer.count("SelectSources")
    started = peer.count("Start")
    task = asyncio.create_task(client.select_monitor(connector, restore_token))
    await peer.wait_for("CreateSession", created + 1)
    await settle()
    peer.respond_create(session_token=session_token)
    await peer.wait_for("SelectSources", selected + 1)
    await settle()
    peer.respond("SelectSources", 0, {})
    await peer.wait_for("Start", started + 1)
    await settle()
    peer.respond_start(connector, response=start_response, **(start_kwargs or {}))
    return await task


# --------------------------------------------------------------------------- selection


async def test_monitor_selection_is_a_single_monitor_source() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(client, peer)

    assert isinstance(result, Ok)
    monitor = result.value
    assert monitor.connector == "DP-1"
    assert monitor.source_type == "physical_monitor"
    assert monitor.restore_token == "new-token"
    assert monitor.portal_session_id == peer.session_path("s1")

    create = peer.last("CreateSession").message
    assert create.destination == PORTAL_DESTINATION
    assert create.path == PORTAL_PATH
    assert create.interface == SCREEN_CAST_INTERFACE
    assert create.signature == "a{sv}"

    select = peer.last("SelectSources").message
    assert select.signature == "oa{sv}"
    assert select.body[0] == peer.session_path("s1")
    options = select.body[-1]
    assert options["types"].value == MONITOR
    assert options["multiple"].value is False
    assert options["persist_mode"].value == 1
    assert "restore_token" not in options

    start = peer.last("Start").message
    assert start.signature == "osa{sv}"
    assert start.body[0] == peer.session_path("s1")
    assert start.body[1] == ""


async def test_window_stream_cannot_be_bound_as_a_physical_monitor() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client,
        peer,
        start_kwargs={"source_type": WINDOW, "mapping_id": None},
    )

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert peer.session_path("s1") in peer.closed_sessions
    assert peer.session_path("s1") not in peer.open_sessions


async def test_virtual_output_stream_cannot_be_bound_as_a_physical_monitor() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client,
        peer,
        start_kwargs={"source_type": VIRTUAL, "mapping_id": "Virtual-1"},
    )

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert peer.session_path("s1") in peer.closed_sessions


async def test_monitor_without_a_connector_mapping_is_unsupported() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client,
        peer,
        start_kwargs={"source_type": MONITOR, "mapping_id": None},
    )

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert peer.session_path("s1") in peer.closed_sessions


async def test_monitor_mapped_to_a_different_connector_is_unsupported() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client,
        peer,
        "DP-1",
        start_kwargs={"source_type": MONITOR, "mapping_id": "HDMI-A-1"},
    )

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert peer.session_path("s1") in peer.closed_sessions


async def test_more_than_one_stream_is_unsupported() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    streams = peer.monitor_streams("DP-1") + peer.monitor_streams("HDMI-A-1", node_id=8)

    result = await complete_selection(client, peer, start_kwargs={"streams": streams})

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.unsupported_source
    assert peer.session_path("s1") in peer.closed_sessions


# --------------------------------------------------------------------------- failures


async def test_portal_denial_is_a_typed_recoverable_error() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(client, peer, start_response=2)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.portal_denied
    assert peer.session_path("s1") in peer.closed_sessions


async def test_portal_cancellation_is_a_typed_recoverable_error() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(client, peer, start_response=1)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.portal_cancelled
    assert peer.session_path("s1") in peer.closed_sessions


async def test_denied_session_creation_opens_no_session() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    peer.respond_create(response=2)
    result = await task

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.portal_denied
    assert peer.open_sessions == set()
    assert peer.closed_sessions == []


async def test_missing_portal_service_is_a_missing_dependency() -> None:
    peer = FakePortalPeer()
    peer.error_replies["CreateSession"] = "org.freedesktop.DBus.Error.ServiceUnknown"
    client = PortalClient(peer=peer)

    result = await client.select_monitor("DP-1", None)

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_missing


async def test_unknown_response_code_is_not_treated_as_success() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    peer.respond("CreateSession", 3, {})
    result = await task

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.dependency_incompatible


@pytest.mark.parametrize("connector", ["", 7, None])
async def test_invalid_connector_is_rejected_before_the_bus(connector: object) -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await client.select_monitor(connector, None)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.invalid_input
    assert peer.calls == []


async def test_invalid_restore_token_type_is_rejected_before_the_bus() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await client.select_monitor("DP-1", 5)  # type: ignore[arg-type]

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.invalid_input
    assert peer.calls == []


# --------------------------------------------------------------------------- correlation


async def test_response_on_a_foreign_path_is_ignored() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    call = peer.last("CreateSession")

    peer.respond(
        "CreateSession",
        0,
        {"session_handle": Variant("s", peer.session_path("foreign"))},
        call=call,
        path=f"/org/example/request/{call.handle_token}",
    )
    await settle()
    assert not task.done()
    assert peer.open_sessions == set()

    peer.respond_create()
    await peer.wait_for("SelectSources")
    peer.respond("SelectSources", 0, {})
    await peer.wait_for("Start")
    peer.respond_start("DP-1")
    result = await task

    assert isinstance(result, Ok)
    assert result.value.portal_session_id == peer.session_path("s1")


async def test_response_is_read_from_the_returned_request_handle() -> None:
    peer = FakePortalPeer()
    returned = f"{PORTAL_PATH}/request/{peer.sender}/returnedhandle"
    peer.handle_override["CreateSession"] = returned
    client = PortalClient(peer=peer)

    result = await complete_selection(client, peer)

    assert isinstance(result, Ok)
    assert result.value.connector == "DP-1"


async def test_late_response_after_cancel_closes_the_new_session() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    call = peer.last("CreateSession")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert peer.request_path(call.handle_token) in peer.closed_requests

    # The portal answers anyway, long after the caller gave up.
    peer.respond_create(session_token="late")
    await client.drain()

    assert peer.session_path("late") in peer.closed_sessions
    assert peer.session_path("late") not in peer.open_sessions


async def test_cancelled_selection_finishes_without_waiting_for_a_suppressed_response() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    await settle()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    # ``Request.Close`` succeeded, so the portal contract says no ``Response``
    # will ever be emitted for this request.  Nothing may still be waiting on it.
    assert peer.request_path(peer.last("CreateSession").handle_token) in peer.closed_requests
    await asyncio.wait_for(client.drain(), timeout=2.0)

    assert client._jobs == set()
    assert client._cleanup == set()
    assert peer.open_sessions == set()


async def test_cancelled_selection_closes_the_session_it_already_created() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")
    await settle()
    peer.respond_create(session_token="s1")
    await peer.wait_for("SelectSources")
    await settle()
    peer.respond("SelectSources", 0, {})
    await peer.wait_for("Start")
    await settle()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(client.drain(), timeout=2.0)

    assert peer.session_path("s1") in peer.closed_sessions
    assert peer.open_sessions == set()
    assert client._jobs == set()


async def test_abandonment_does_not_wait_for_a_stalled_control_call() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    peer.hold("CreateSession")
    task = asyncio.create_task(client.select_monitor("DP-1", None))
    await peer.wait_for("CreateSession")

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The method reply is still outstanding; the job must not wait for it.
    await asyncio.wait_for(client.drain(), timeout=2.0)
    assert client._jobs == set()

    peer.release_hold("CreateSession")
    await settle()
    assert client._replies == set()


# --------------------------------------------------------------------------- close/tokens


async def test_close_confirms_every_session_and_detaches_the_handler() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    await complete_selection(client, peer, session_token="s1")
    await complete_selection(client, peer, session_token="s2")

    await client.close()

    assert peer.closed_sessions == [peer.session_path("s1"), peer.session_path("s2")]
    assert peer.open_sessions == set()
    assert peer.handlers == []
    assert peer.disconnected is False  # the injected peer is not ours to close


async def test_client_owns_and_disconnects_the_connection_it_opens() -> None:
    peer = FakePortalPeer()

    async def connect() -> FakePortalPeer:
        return peer

    client = PortalClient(connect=connect)
    result = await complete_selection(client, peer)

    assert isinstance(result, Ok)
    await client.close()
    assert peer.disconnected is True
    assert peer.closed_sessions == [peer.session_path("s1")]


async def test_close_is_idempotent() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    await complete_selection(client, peer)

    await client.close()
    await client.close()

    assert peer.closed_sessions == [peer.session_path("s1")]


async def test_restore_token_is_used_but_never_rendered() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client, peer, restore_token="super-secret-token"
    )

    assert isinstance(result, Ok)
    options = peer.last("SelectSources").message.body[-1]
    assert options["restore_token"].value == "super-secret-token"
    assert "super-secret-token" not in repr(result.value)
    assert "super-secret-token" not in str(result.value)


async def test_failed_selection_does_not_echo_the_restore_token() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", "expired-token"))
    await peer.wait_for("CreateSession")
    peer.respond_create()
    await peer.wait_for("SelectSources")
    peer.respond("SelectSources", 2, {})
    result = await task

    assert isinstance(result, Err)
    assert result.error.code is ErrorCode.portal_denied
    rendered = f"{result!r} {result.error.message} {result.error.detail}"
    assert "expired-token" not in rendered
    assert peer.session_path("s1") in peer.closed_sessions


async def test_a_new_restore_token_replaces_the_used_one() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)

    result = await complete_selection(
        client,
        peer,
        restore_token="expired-token",
        start_kwargs={"restore_token": "replacement-token"},
    )

    assert isinstance(result, Ok)
    assert result.value.restore_token == "replacement-token"


async def test_no_restore_token_is_reused_after_a_failed_selection() -> None:
    peer = FakePortalPeer()
    client = PortalClient(peer=peer)
    task = asyncio.create_task(client.select_monitor("DP-1", "expired-token"))
    await peer.wait_for("CreateSession")
    peer.respond_create(response=1)
    result = await task
    assert isinstance(result, Err)

    # The client caches nothing: the next caller decides what token to offer.
    await complete_selection(client, peer, session_token="s2")

    assert "restore_token" not in peer.last("SelectSources").message.body[-1]
