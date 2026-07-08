"""
Core relay server for OpenDesk.

Provides TCP fallback connectivity when direct P2P (WebRTC) fails.
Peers connect to the relay, authenticate, and the relay forwards
messages between them.

Usage::

    relay-server --port 8474
    relay-server --config /path/to/config.yaml
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from relay_server.auth import RelayAuth, generate_session_id, hash_password
from relay_server.config import Config
from relay_server.monitoring.metrics import MetricsCollector
from relay_server.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Peer state
# ---------------------------------------------------------------------------


@dataclass
class RelayPeer:
    """A peer connected to the relay server."""

    peer_id: str
    writer: asyncio.StreamWriter
    reader: asyncio.StreamReader
    session_id: str = ""
    device_id: str = ""
    device_name: str = ""
    last_activity: float = field(default_factory=time.time)
    authenticated: bool = False
    paired_peer_id: str | None = None
    address: str = ""


# ---------------------------------------------------------------------------
# Relay server
# ---------------------------------------------------------------------------


class RelayServer:
    """TCP relay server that forwards messages between peers.

    Peers connect, optionally authenticate via session ID, and are
    paired together to exchange messages.

    Parameters
    ----------
    config
        Server configuration.
    auth
        Optional authentication manager. If not provided, one is created.
    """

    def __init__(
        self,
        config: Config | None = None,
        auth: RelayAuth | None = None,
    ) -> None:
        self.config = config or Config()
        self._auth = auth or RelayAuth()

        # Apply IP access rules from config
        if self.config.auth.whitelist_ips or self.config.auth.blacklist_ips:
            self._auth.configure_ip_rules(
                whitelist=self.config.auth.whitelist_ips,
                blacklist=self.config.auth.blacklist_ips,
            )

        self._peers: dict[str, RelayPeer] = {}
        self._sessions: dict[str, str] = {}  # session_id → host_peer_id
        self._devices: dict[str, RelayPeer] = {}  # device_id → peer
        self._server: asyncio.AbstractServer | None = None

        # Metrics
        self.metrics = MetricsCollector()

        # Reference to optional web app
        self.web_app: Any = None

        # TLS support
        self._tls_context: Any = None

    @property
    def auth(self) -> RelayAuth:
        """Return the authentication manager."""
        return self._auth

    # ── startup / shutdown ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the relay server.

        If the web dashboard is enabled (via ``web_app``), it is started
        alongside the relay server.
        """
        # TLS support
        tls_cert = self.config.server.tls_cert
        tls_key = self.config.server.tls_key
        ssl_ctx = None
        if tls_cert and tls_key:
            import ssl
            ssl_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_ctx.load_cert_chain(tls_cert, tls_key)
            self._tls_context = ssl_ctx
            logger.info("TLS enabled (cert=%s)", tls_cert)

        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.config.server.host,
            port=self.config.server.port,
            ssl=ssl_ctx,
        )
        addr = self._server.sockets[0].getsockname()
        proto = "TLS" if ssl_ctx else "TCP"
        logger.info(
            "Relay server listening on %s:%d (%s)",
            addr[0], addr[1], proto,
        )

        # Start periodic cleanup
        asyncio.create_task(self._cleanup_loop())

        # Start web dashboard if configured
        if self.config.admin.enabled and self.web_app:
            from relay_server.web.app import run_web_server
            await run_web_server(self, self.config)

        # Keep the server running
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the relay server and disconnect all peers."""
        logger.info("Shutting down relay server...")

        for peer in list(self._peers.values()):
            try:
                peer.writer.close()
            except Exception:
                pass

        self._peers.clear()
        self._sessions.clear()
        self._devices.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Relay server stopped")

    # ── client handling ─────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming peer connection."""
        addr = writer.get_extra_info("peername")
        addr_str = f"{addr[0]}:{addr[1]}" if addr else "?"

        # IP access control
        if addr:
            allowed, reason = self._auth.is_ip_allowed(addr[0])
            if not allowed:
                logger.warning("Connection rejected: %s (%s)", addr_str, reason)
                writer.close()
                return

        peer_id = f"peer-{id(writer):x}"
        peer = RelayPeer(
            peer_id=peer_id,
            writer=writer,
            reader=reader,
            address=addr_str,
        )
        self._peers[peer_id] = peer
        self.metrics.on_connection("tls" if self._tls_context else "tcp")

        logger.info("Peer connected: %s from %s", peer_id, addr_str)

        try:
            await self._peer_loop(peer)
        except (ConnectionError, asyncio.IncompleteReadError):
            logger.debug("Peer disconnected: %s", peer_id)
        except Exception as e:
            logger.exception("Error handling peer %s: %s", peer_id, e)
            self.metrics.on_error(500)
        finally:
            self._remove_peer(peer_id)
            self.metrics.on_disconnection()

    async def _peer_loop(self, peer: RelayPeer) -> None:
        """Main loop for an individual peer."""
        while True:
            msg = await Message.from_reader(peer.reader)
            peer.last_activity = time.time()
            self.metrics.on_message_received(msg.type.name)
            self.metrics.on_bytes_received(len(msg.encode()))
            await self._handle_message(peer, msg)

    async def _handle_message(self, peer: RelayPeer, msg: Message) -> None:
        """Route an incoming message appropriately."""
        msg_type = msg.type
        payload = msg.payload

        if msg_type == MessageType.RELAY_REGISTER:
            await self._handle_register(peer, payload)
        elif msg_type == MessageType.RELAY_ROUTE:
            await self._handle_route(peer, payload)
        elif msg_type == MessageType.PING:
            seq = payload.get("seq", 0)
            await self._send(peer, Message.pong(seq))
        elif msg_type == MessageType.DISCONNECT:
            raise ConnectionError("Peer requested disconnect")
        else:
            # Forward to paired peer if any
            if peer.paired_peer_id:
                paired = self._peers.get(peer.paired_peer_id)
                if paired:
                    await self._send(paired, msg)
                    self.metrics.on_message_forwarded(msg_type.name)
            else:
                logger.warning(
                    "Unhandled message from %s: %s (not paired)",
                    peer.peer_id, msg_type,
                )

    # ── message routing ─────────────────────────────────────────────

    async def _handle_register(self, peer: RelayPeer, payload: dict) -> None:
        """Handle peer registration.

        Flow:
        1. Device ID present, no session ID → device lookup
        2. Session ID present → join or create session
        3. Neither → auto-generate new session (legacy)
        """
        device_id = payload.get("device_id", "")
        device_name = payload.get("device_name", "")
        if device_id:
            peer.device_id = device_id
            peer.device_name = device_name
            self._devices[device_id] = peer
            self.metrics.on_device_registered()
            logger.info("Device registered: %s (%s)", device_id, device_name)

        session_id = payload.get("session_id", "")

        # ── Device lookup ──
        lookup_device = payload.get("lookup_device", "")
        if lookup_device and not session_id:
            target = self._devices.get(lookup_device)
            if target is None:
                host_id = self._sessions.get(lookup_device)
                if host_id is not None:
                    target = self._peers.get(host_id)

            if target is not None and target.session_id \
               and target.writer and not target.writer.is_closing():
                session_id = target.session_id
                host_id = self._sessions.get(session_id)
                if host_id and host_id != peer.peer_id:
                    host_peer = self._peers.get(host_id)
                    if host_peer:
                        peer.session_id = session_id
                        peer.paired_peer_id = host_id
                        host_peer.paired_peer_id = peer.peer_id
                        await self._send(
                            peer,
                            Message(MessageType.RELAY_REGISTER,
                                    {"session_id": session_id, "paired": True,
                                     "mode": "client", "device_name": target.device_name}),
                        )
                        await self._send(
                            host_peer,
                            Message(MessageType.RELAY_PEER_LIST, {"peers": [peer.peer_id]}),
                        )
                        logger.info(
                            "Peers paired via lookup %s: %s ↔ %s",
                            lookup_device, host_id, peer.peer_id,
                        )
                        return

            await self._send(
                peer,
                Message(MessageType.ERROR, {
                    "code": 404,
                    "message": f"Device/session {lookup_device} offline or not found",
                }),
            )
            logger.info("Lookup failed: %s", lookup_device)
            self.metrics.on_error(404)
            return

        # ── No session_id → auto-generate (legacy) ──
        if not session_id:
            session_id = generate_session_id()
            self._sessions[session_id] = peer.peer_id
            peer.session_id = session_id
            self.metrics.on_session_created()
            await self._send(
                peer,
                Message(MessageType.RELAY_REGISTER, {"session_id": session_id}),
            )
            logger.info("Session created (legacy): %s for peer %s",
                        session_id, peer.peer_id)
            await self._broadcast_device_list()
            return

        # ── Session exists → join as client ──
        host_id = self._sessions.get(session_id)
        if host_id is not None:
            host_peer = self._peers.get(host_id)
            if host_peer is None:
                del self._sessions[session_id]
                self.metrics.on_session_closed()
                peer.session_id = session_id
                self._sessions[session_id] = peer.peer_id
                self.metrics.on_session_created()
                await self._send(
                    peer,
                    Message(MessageType.RELAY_REGISTER,
                            {"session_id": session_id, "mode": "host"}),
                )
                logger.info("Session taken over: %s by %s", session_id, peer.peer_id)
                return

            peer.session_id = session_id
            peer.paired_peer_id = host_id
            host_peer.paired_peer_id = peer.peer_id

            await self._send(
                peer,
                Message(MessageType.RELAY_REGISTER,
                        {"session_id": session_id, "paired": True, "mode": "client"}),
            )
            await self._send(
                host_peer,
                Message(MessageType.RELAY_PEER_LIST, {"peers": [peer.peer_id]}),
            )
            logger.info("Peers paired in session %s: %s ↔ %s",
                        session_id, host_id, peer.peer_id)
            return

        # ── New session → register as host ──
        self._sessions[session_id] = peer.peer_id
        peer.session_id = session_id
        self.metrics.on_session_created()
        await self._send(
            peer,
            Message(MessageType.RELAY_REGISTER, {"session_id": session_id, "mode": "host"}),
        )
        logger.info("Session registered: %s for peer %s", session_id, peer.peer_id)
        await self._broadcast_device_list()

    async def _broadcast_device_list(self) -> None:
        """Send the current list of connected devices to all peers."""
        devices = [
            {
                "device_id": p.device_id,
                "device_name": p.device_name or p.device_id[:8],
                "session_id": p.session_id,
            }
            for p in self._devices.values()
            if p.device_id and p.writer and not p.writer.is_closing()
        ]
        msg = Message.relay_device_list(devices)
        tasks = [
            self._send(p, msg)
            for p in self._peers.values()
            if p.writer and not p.writer.is_closing()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_route(self, peer: RelayPeer, payload: dict) -> None:
        """Forward a message to the paired peer."""
        if not peer.paired_peer_id:
            return

        target = self._peers.get(peer.paired_peer_id)
        if target is None:
            return

        inner_type = MessageType(payload.get("inner_type", 0))
        inner_payload = payload.get("inner_payload", {})
        inner_msg = Message(inner_type, inner_payload)
        await self._send(target, inner_msg)
        self.metrics.on_message_forwarded(inner_type.name)

    # ── utilities ───────────────────────────────────────────────────

    async def _send(self, peer: RelayPeer, msg: Message) -> None:
        """Send a message to a peer."""
        try:
            data = msg.encode()
            peer.writer.write(data)
            await peer.writer.drain()
            self.metrics.on_bytes_sent(len(data))
        except Exception as e:
            logger.warning("Failed to send to %s: %s", peer.peer_id, e)
            self._remove_peer(peer.peer_id)

    def _remove_peer(self, peer_id: str) -> None:
        """Remove a peer and clean up associated state."""
        peer = self._peers.pop(peer_id, None)
        if peer is None:
            return

        try:
            if peer.writer and not peer.writer.is_closing():
                peer.writer.close()
        except Exception:
            pass

        # Remove from device registry
        was_device = False
        if peer.device_id and self._devices.get(peer.device_id) is peer:
            del self._devices[peer.device_id]
            was_device = True
            self.metrics.on_device_offline()
            logger.info("Device went offline: %s (%s)", peer.device_id, peer.device_name)

        # Notify paired peer
        if peer.paired_peer_id:
            paired = self._peers.get(peer.paired_peer_id)
            if paired:
                paired.paired_peer_id = None
                asyncio.ensure_future(
                    self._send(
                        paired,
                        Message.error(410, "Peer disconnected"),
                    )
                )

        # Remove session if host
        if peer.session_id and self._sessions.get(peer.session_id) == peer_id:
            del self._sessions[peer.session_id]
            self.metrics.on_session_closed()

        # Broadcast updated device list
        if was_device:
            asyncio.ensure_future(self._broadcast_device_list())

        logger.info("Peer removed: %s", peer_id)

    async def _cleanup_loop(self) -> None:
        """Periodically disconnect stale peers and clean up expired sessions."""
        ping_interval = self.config.relay.ping_interval
        peer_timeout = self.config.relay.peer_timeout

        while True:
            await asyncio.sleep(ping_interval)
            now = time.time()

            # Clean stale peers
            stale = [
                pid for pid, p in self._peers.items()
                if now - p.last_activity > peer_timeout
            ]
            for pid in stale:
                peer = self._peers.get(pid)
                if peer:
                    logger.info("Removing stale peer: %s (inactive >%ds)",
                                pid, peer_timeout)
                    self.metrics.on_peer_timeout()
                    try:
                        peer.writer.close()
                    except Exception:
                        pass
                    self._remove_peer(pid)

            # Clean expired sessions
            expired_count = self._auth.cleanup_expired()
            if expired_count:
                logger.debug("Cleaned %d expired sessions", expired_count)
