"""Tests for the core relay server."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from relay_server.auth import RelayAuth
from relay_server.config import Config
from relay_server.protocol import Message, MessageType
from relay_server.server import RelayServer


@pytest.fixture
def config() -> Config:
    c = Config()
    c.server.host = "127.0.0.1"
    c.server.port = 0  # random port
    c.admin.enabled = False
    c.auth.enabled = False
    return c


@pytest.fixture
def server(config: Config) -> RelayServer:
    return RelayServer(config=config)


class TestRelayServer:
    @pytest.mark.asyncio
    async def test_start_stop(self, server: RelayServer) -> None:
        """Server should start and stop without errors."""
        # Wrap in asyncio.task with timeout to avoid blocking
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass  # Expected: serve_forever blocks

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        # Server should be running
        assert server._server is not None
        sockets = server._server.sockets
        assert len(sockets) > 0
        port = sockets[0].getsockname()[1]
        assert port > 0

        # Stop the server
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        assert len(server._peers) == 0
        assert len(server._sessions) == 0

    @pytest.mark.asyncio
    async def test_peer_connect_and_register(self, server: RelayServer) -> None:
        """A peer should be able to connect and register."""
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        sockets = server._server.sockets
        port = sockets[0].getsockname()[1]

        # Connect a client
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Send RELAY_REGISTER
        register_msg = Message.relay_register()
        Message.write(writer, register_msg)
        await writer.drain()

        # Read response
        response = await Message.from_reader(reader)
        assert response.type == MessageType.RELAY_REGISTER
        assert "session_id" in response.payload

        writer.close()
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_peer_pairing(self, server: RelayServer) -> None:
        """Two peers should be able to pair via session ID."""
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        sockets = server._server.sockets
        port = sockets[0].getsockname()[1]

        # Host connects
        host_r, host_w = await asyncio.open_connection("127.0.0.1", port)
        host_msg = Message.relay_register()
        Message.write(host_w, host_msg)
        await host_w.drain()
        host_resp = await Message.from_reader(host_r)
        assert host_resp.type == MessageType.RELAY_REGISTER
        session_id = host_resp.payload["session_id"]

        # Guest connects with session ID
        guest_r, guest_w = await asyncio.open_connection("127.0.0.1", port)
        guest_msg = Message.relay_register(session_id=session_id)
        Message.write(guest_w, guest_msg)
        await guest_w.drain()

        # Guest should get paired response
        guest_resp = await Message.from_reader(guest_r)
        assert guest_resp.type == MessageType.RELAY_REGISTER
        assert guest_resp.payload.get("paired") is True

        # Host should get device list first (broadcast), then peer list
        host_devices = await Message.from_reader(host_r)
        assert host_devices.type == MessageType.RELAY_DEVICE_LIST

        host_list = await Message.from_reader(host_r)
        assert host_list.type == MessageType.RELAY_PEER_LIST

        # Cleanup
        host_w.close()
        guest_w.close()
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_message_routing(self, server: RelayServer) -> None:
        """Messages should be forwarded between paired peers."""
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        sockets = server._server.sockets
        port = sockets[0].getsockname()[1]

        # Set up host and guest
        host_r, host_w = await asyncio.open_connection("127.0.0.1", port)
        resp = await _send_recv(host_r, host_w, Message.relay_register())
        session_id = resp.payload["session_id"]

        guest_r, guest_w = await asyncio.open_connection("127.0.0.1", port)
        await _send_recv(guest_r, guest_w, Message.relay_register(session_id=session_id))

        # Consume device list + peer list sent to host
        msg1 = await Message.from_reader(host_r)
        msg2 = await Message.from_reader(host_r)
        assert msg1.type in (MessageType.RELAY_DEVICE_LIST, MessageType.RELAY_PEER_LIST)
        assert msg2.type in (MessageType.RELAY_DEVICE_LIST, MessageType.RELAY_PEER_LIST)

        # Send a RELAY_ROUTE with PING as inner type
        route_msg = Message.relay_route(
            inner_type=0x70,  # PING
            inner_payload={"seq": 100},
        )
        Message.write(host_w, route_msg)
        await host_w.drain()

        # Guest should receive the forwarded inner message (PING)
        forwarded = await Message.from_reader(guest_r)
        assert forwarded.type == MessageType.PING
        assert forwarded.payload["seq"] == 100

        # Cleanup
        host_w.close()
        guest_w.close()
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_ping_pong(self, server: RelayServer) -> None:
        """PING should be answered with PONG."""
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        sockets = server._server.sockets
        port = sockets[0].getsockname()[1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        resp = await _send_recv(reader, writer, Message.ping(seq=42))
        assert resp.type == MessageType.PONG
        assert resp.payload["seq"] == 42

        writer.close()
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    @pytest.mark.asyncio
    async def test_disconnect_notifies_paired(self, server: RelayServer) -> None:
        """When one peer disconnects, the other should be notified."""
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(server.start(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        task = asyncio.create_task(run_with_timeout())
        await asyncio.sleep(0.5)

        sockets = server._server.sockets
        port = sockets[0].getsockname()[1]

        # Set up host and guest
        host_r, host_w = await asyncio.open_connection("127.0.0.1", port)
        resp = await _send_recv(host_r, host_w, Message.relay_register())
        session_id = resp.payload["session_id"]

        guest_r, guest_w = await asyncio.open_connection("127.0.0.1", port)
        await _send_recv(guest_r, guest_w, Message.relay_register(session_id=session_id))

        # Consume device list + peer list sent to host
        msg1 = await Message.from_reader(host_r)  # RELAY_DEVICE_LIST or RELAY_PEER_LIST
        msg2 = await Message.from_reader(host_r)  # the other one
        assert msg1.type in (MessageType.RELAY_DEVICE_LIST, MessageType.RELAY_PEER_LIST)
        assert msg2.type in (MessageType.RELAY_DEVICE_LIST, MessageType.RELAY_PEER_LIST)
        assert msg1.type != msg2.type

        # Guest disconnects
        Message.write(guest_w, Message.disconnect("bye"))
        await guest_w.drain()
        await asyncio.sleep(0.3)

        # Host should get peer list first (guest removed), then error
        host_peer_update = await Message.from_reader(host_r)
        if host_peer_update.type == MessageType.RELAY_PEER_LIST:
            # Consume peer list, then read error
            error = await Message.from_reader(host_r)
        else:
            error = host_peer_update

        assert error.type == MessageType.ERROR
        assert error.payload["code"] == 410

        host_w.close()
        guest_w.close()
        await server.stop()
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


async def _send_recv(reader, writer, msg: Message) -> Message:
    """Send a message and wait for the response."""
    Message.write(writer, msg)
    await writer.drain()
    return await Message.from_reader(reader)


async def _drain_messages(reader, count: int) -> list[Message]:
    """Read multiple messages from a reader."""
    msgs = []
    for _ in range(count):
        try:
            msg = await asyncio.wait_for(Message.from_reader(reader), timeout=1.0)
            msgs.append(msg)
        except asyncio.TimeoutError:
            break
    return msgs
