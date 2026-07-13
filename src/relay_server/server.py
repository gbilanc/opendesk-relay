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

        logger.info("Peer connected: %s from %s (total_peers=%d)", peer_id, addr_str, len(self._peers))

        try:
            logger.debug("[%s] Entering _peer_loop", peer_id)
            await self._peer_loop(peer)
            logger.debug("[%s] _peer_loop exited normally", peer_id)
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            logger.debug("[%s] Peer disconnected (ConnectionError): %s", peer_id, e)
        except asyncio.CancelledError:
            logger.debug("[%s] Peer loop cancelled", peer_id)
        except Exception as e:
            logger.exception("[%s] ❌ Exception in _peer_loop: %s", peer_id, e)
            self.metrics.on_error(500)
        finally:
            logger.debug("[%s] Entering _remove_peer from _handle_client finally block", peer_id)
            self._remove_peer(peer_id)
            self.metrics.on_disconnection()
            logger.debug("[%s] Peer fully cleaned up (total_peers=%d)", peer_id, len(self._peers))

    async def _peer_loop(self, peer: RelayPeer) -> None:
        """Main loop for an individual peer."""
        while True:
            msg = await Message.from_reader(peer.reader)
            peer.last_activity = time.time()
            # Handle both MessageType enum and raw int types
            type_name = getattr(msg.type, 'name', str(msg.type))
            type_value = msg.type.value if isinstance(msg.type, MessageType) else msg.type
            payload_preview = {k: (str(v)[:80] if isinstance(v, (bytes, str)) and len(str(v)) > 80 else v)
                               for k, v in msg.payload.items()}
            logger.debug("[%s] ← RECV type=0x%02x (%s) payload=%s",
                         peer.peer_id, type_value, type_name, payload_preview)
            self.metrics.on_message_received(type_name)
            self.metrics.on_bytes_received(len(msg.encode()))
            await self._handle_message(peer, msg)

    async def _handle_message(self, peer: RelayPeer, msg: Message) -> None:
        """Route an incoming message appropriately."""
        msg_type = msg.type
        payload = msg.payload
        type_name = getattr(msg_type, 'name', str(msg_type))
        type_value = msg_type.value if isinstance(msg_type, MessageType) else msg_type

        logger.debug("[%s] HANDLE type=0x%02x (%s) paired_to=%s",
                     peer.peer_id, type_value, type_name, peer.paired_peer_id)

        if msg_type == MessageType.RELAY_REGISTER:
            logger.debug("[%s] → _handle_register (device_id=%s, session_id=%s, lookup=%s)",
                         peer.peer_id,
                         payload.get("device_id", ""),
                         payload.get("session_id", ""),
                         payload.get("lookup_device", ""))
            await self._handle_register(peer, payload)
        elif msg_type == MessageType.RELAY_ROUTE:
            inner = payload.get("inner_type", 0)
            try:
                inner_name = MessageType(inner).name
            except ValueError:
                inner_name = f"0x{inner:02x}"
            logger.debug("[%s] → _handle_route (inner_type=0x%02x / %s)",
                         peer.peer_id, inner, inner_name)
            await self._handle_route(peer, payload)
        elif msg_type == MessageType.PING:
            seq = payload.get("seq", 0)
            logger.debug("[%s] → PONG (seq=%d)", peer.peer_id, seq)
            await self._send(peer, Message.pong(seq))
        elif msg_type == MessageType.DISCONNECT:
            reason = payload.get("reason", "")
            logger.debug("[%s] → DISCONNECT requested: %s", peer.peer_id, reason)
            raise ConnectionError(f"Peer requested disconnect: {reason}")
        elif msg_type == MessageType.AUTH_REQUEST:
            # Forward auth messages to paired peer
            target_id = peer.paired_peer_id
            logger.debug("[%s] → AUTH_REQUEST forwarding to paired=%s", peer.peer_id, target_id)
            if target_id:
                target = self._peers.get(target_id)
                if target:
                    await self._send(target, msg)
                    self.metrics.on_message_forwarded(type_name)
                else:
                    logger.warning("[%s] AUTH_REQUEST: paired peer %s not found", peer.peer_id, target_id)
            else:
                logger.warning("[%s] AUTH_REQUEST: not paired, cannot forward", peer.peer_id)
        elif msg_type == MessageType.AUTH_RESPONSE:
            target_id = peer.paired_peer_id
            logger.debug("[%s] → AUTH_RESPONSE forwarding to paired=%s", peer.peer_id, target_id)
            if target_id:
                target = self._peers.get(target_id)
                if target:
                    await self._send(target, msg)
                    self.metrics.on_message_forwarded(type_name)
                else:
                    logger.warning("[%s] AUTH_RESPONSE: paired peer %s not found", peer.peer_id, target_id)
            else:
                logger.warning("[%s] AUTH_RESPONSE: not paired, cannot forward", peer.peer_id)
        elif msg_type == MessageType.AUTH_OK:
            target_id = peer.paired_peer_id
            logger.debug("[%s] → AUTH_OK forwarding to paired=%s", peer.peer_id, target_id)
            if target_id:
                target = self._peers.get(target_id)
                if target:
                    await self._send(target, msg)
                    self.metrics.on_message_forwarded(type_name)
                else:
                    logger.warning("[%s] AUTH_OK: paired peer %s not found", peer.peer_id, target_id)
            else:
                logger.warning("[%s] AUTH_OK: not paired, cannot forward", peer.peer_id)
        elif msg_type == MessageType.AUTH_FAIL:
            target_id = peer.paired_peer_id
            logger.debug("[%s] → AUTH_FAIL forwarding to paired=%s", peer.peer_id, target_id)
            if target_id:
                target = self._peers.get(target_id)
                if target:
                    await self._send(target, msg)
                    self.metrics.on_message_forwarded(type_name)
                else:
                    logger.warning("[%s] AUTH_FAIL: paired peer %s not found", peer.peer_id, target_id)
            else:
                logger.warning("[%s] AUTH_FAIL: not paired, cannot forward", peer.peer_id)
        else:
            # Forward to paired peer if any
            if peer.paired_peer_id:
                paired = self._peers.get(peer.paired_peer_id)
                if paired:
                    logger.debug("[%s] → forwarding 0x%02x (%s) to %s",
                                 peer.peer_id, type_value, type_name, peer.paired_peer_id)
                    await self._send(paired, msg)
                    self.metrics.on_message_forwarded(type_name)
                else:
                    logger.warning(
                        "[%s] Cannot forward 0x%02x (%s): paired peer %s not found in _peers (keys=%s)",
                        peer.peer_id, type_value, type_name, peer.paired_peer_id,
                        list(self._peers.keys()),
                    )
            else:
                logger.warning(
                    "[%s] Unhandled message 0x%02x (%s): not paired with anyone",
                    peer.peer_id, type_value, type_name,
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
            logger.info("[%s] Device registered: %s (%s)", peer.peer_id, device_id, device_name)
            logger.debug("[%s] Devices now: %s", peer.peer_id, list(self._devices.keys()))

        session_id = payload.get("session_id", "")

        # ── Device lookup ──
        lookup_device = payload.get("lookup_device", "")
        if lookup_device and not session_id:
            logger.debug("[%s] Lookup: searching for device/session '%s' (devices=%s, sessions=%s)",
                         peer.peer_id, lookup_device, list(self._devices.keys()), list(self._sessions.keys()))
            target = self._devices.get(lookup_device)
            if target is None:
                host_id = self._sessions.get(lookup_device)
                logger.debug("[%s] Lookup: not a device, checking sessions → host_id=%s",
                             peer.peer_id, host_id)
                if host_id is not None:
                    target = self._peers.get(host_id)
                    logger.debug("[%s] Lookup: target peer from session=%s",
                                 peer.peer_id, target.peer_id if target else None)

            if target is not None and target.session_id \
               and target.writer and not target.writer.is_closing():
                session_id = target.session_id
                host_id = self._sessions.get(session_id)
                logger.debug("[%s] Lookup: target found, session_id=%s, host_id=%s",
                             peer.peer_id, session_id, host_id)
                if host_id and host_id != peer.peer_id:
                    host_peer = self._peers.get(host_id)
                    if host_peer:
                        logger.debug("[%s] Lookup: pairing %s ↔ %s",
                                     peer.peer_id, host_id, peer.peer_id)
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
                            "[%s] Peers paired via lookup %s: %s ↔ %s",
                            peer.peer_id, lookup_device, host_id, peer.peer_id,
                        )
                        return
                    else:
                        logger.warning("[%s] Lookup: host_peer from session is None (bug?)", peer.peer_id)
                else:
                    logger.warning("[%s] Lookup: host_id=%s is self or not found", peer.peer_id, host_id)
            else:
                reason = "target is None" if target is None else "target writer is closing"
                logger.debug("[%s] Lookup: target not usable: %s", peer.peer_id, reason)

            await self._send(
                peer,
                Message(MessageType.ERROR, {
                    "code": 404,
                    "message": f"Device/session {lookup_device} offline or not found",
                }),
            )
            logger.info("[%s] Lookup failed: %s", peer.peer_id, lookup_device)
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
            logger.info("[%s] Session created (legacy): %s", peer.peer_id, session_id)
            await self._broadcast_device_list()
            return

        # ── Session exists → join as client ──
        host_id = self._sessions.get(session_id)
        if host_id is not None:
            logger.debug("[%s] Join: session '%s' exists, host=%s", peer.peer_id, session_id, host_id)
            host_peer = self._peers.get(host_id)
            if host_peer is None:
                logger.debug("[%s] Join: host peer gone, taking over session", peer.peer_id)
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
                logger.info("[%s] Session taken over: %s", peer.peer_id, session_id)
                return

            peer.session_id = session_id
            peer.paired_peer_id = host_id
            host_peer.paired_peer_id = peer.peer_id
            logger.debug("[%s] Join: paired with %s", peer.peer_id, host_id)

            await self._send(
                peer,
                Message(MessageType.RELAY_REGISTER,
                        {"session_id": session_id, "paired": True, "mode": "client"}),
            )
            await self._send(
                host_peer,
                Message(MessageType.RELAY_PEER_LIST, {"peers": [peer.peer_id]}),
            )
            logger.info("[%s] Peers paired in session %s: %s ↔ %s",
                        peer.peer_id, session_id, host_id, peer.peer_id)
            return

        # ── New session → register as host ──
        logger.debug("[%s] Create new session: %s", peer.peer_id, session_id)
        self._sessions[session_id] = peer.peer_id
        peer.session_id = session_id
        self.metrics.on_session_created()
        await self._send(
            peer,
            Message(MessageType.RELAY_REGISTER, {"session_id": session_id, "mode": "host"}),
        )
        logger.info("[%s] Session registered: %s", peer.peer_id, session_id)
        await self._broadcast_device_list()
        self._debug_state(f"after new session {peer.peer_id} as host")

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
            logger.debug("[%s] RELAY_ROUTE: not paired, dropping", peer.peer_id)
            return

        target = self._peers.get(peer.paired_peer_id)
        if target is None:
            logger.warning("[%s] RELAY_ROUTE: paired peer %s not found (peers=%s)",
                           peer.peer_id, peer.paired_peer_id, list(self._peers.keys()))
            return

        inner_type = payload.get("inner_type", 0)
        try:
            inner_type_enum = MessageType(inner_type)
            inner_name = inner_type_enum.name
        except ValueError:
            inner_name = f"0x{inner_type:02x}"
            inner_type_enum = inner_type

        inner_payload = payload.get("inner_payload", {})
        inner_msg = Message(inner_type_enum, inner_payload)

        logger.debug("[%s] RELAY_ROUTE: wrapping inner_type=0x%02x (%s) → %s",
                     peer.peer_id, inner_type, inner_name, peer.paired_peer_id)
        await self._send(target, inner_msg)
        self.metrics.on_message_forwarded(inner_name)

    # ── utilities ───────────────────────────────────────────────────

    async def _send(self, peer: RelayPeer, msg: Message) -> None:
        """Send a message to a peer."""
        type_name = getattr(msg.type, 'name', str(msg.type))
        type_value = msg.type.value if isinstance(msg.type, MessageType) else msg.type
        try:
            data = msg.encode()
            peer.writer.write(data)
            await peer.writer.drain()
            self.metrics.on_bytes_sent(len(data))
            logger.debug("[→→→→ %s] SEND type=0x%02x (%s) peer_closing=%s len=%d",
                         peer.peer_id, type_value, type_name,
                         peer.writer.is_closing(), len(data))
        except ConnectionError as e:
            logger.warning("[%s] ❌ Send failed (connection closed): type=0x%02x (%s) err=%s",
                           peer.peer_id, type_value, type_name, e)
            self._remove_peer(peer.peer_id)
        except Exception as e:
            logger.exception("[%s] ❌ Send failed: type=0x%02x (%s) err=%s",
                             peer.peer_id, type_value, type_name, e)
            self._remove_peer(peer.peer_id)

    def _remove_peer(self, peer_id: str) -> None:
        """Remove a peer and clean up associated state."""
        peer = self._peers.pop(peer_id, None)
        if peer is None:
            logger.debug("[remove] Peer '%s' not found in _peers (already removed? keys=%s)",
                         peer_id, list(self._peers.keys()))
            return

        logger.info(
            "[%s] REMOVE peer: device=%s session=%s paired=%s was_auth=%s",
            peer_id, peer.device_id, peer.session_id, peer.paired_peer_id, peer.authenticated,
        )

        # Close writer if still open
        try:
            if peer.writer and not peer.writer.is_closing():
                logger.debug("[%s] Closing writer", peer_id)
                peer.writer.close()
            else:
                logger.debug("[%s] Writer already closed or None", peer_id)
        except Exception as e:
            logger.debug("[%s] Error closing writer: %s", peer_id, e)

        # Remove from device registry
        was_device = False
        if peer.device_id and self._devices.get(peer.device_id) is peer:
            logger.debug("[%s] Removing from devices registry (device_id=%s)", peer_id, peer.device_id)
            del self._devices[peer.device_id]
            was_device = True
            self.metrics.on_device_offline()
            logger.info("[%s] Device went offline: %s (%s)", peer_id, peer.device_id, peer.device_name)
        elif peer.device_id:
            logger.debug("[%s] Device '%s' not in _devices or points to different peer",
                         peer_id, peer.device_id)

        # Notify paired peer
        if peer.paired_peer_id:
            logger.debug("[%s] Peer was paired with %s, notifying them", peer_id, peer.paired_peer_id)
            paired = self._peers.get(peer.paired_peer_id)
            if paired:
                logger.debug("[%s] Unpairing %s (paired.paired was %s)",
                             peer_id, peer.paired_peer_id, paired.paired_peer_id)
                paired.paired_peer_id = None
                asyncio.ensure_future(
                    self._send(
                        paired,
                        Message.error(410, "Peer disconnected"),
                    )
                )
            else:
                logger.debug("[%s] Paired peer %s no longer in _peers",
                             peer_id, peer.paired_peer_id)
        else:
            logger.debug("[%s] Peer was not paired with anyone", peer_id)

        # Remove session if host
        if peer.session_id:
            current_host = self._sessions.get(peer.session_id)
            if current_host == peer_id:
                logger.debug("[%s] Removing session %s (was host)", peer_id, peer.session_id)
                del self._sessions[peer.session_id]
                self.metrics.on_session_closed()
                logger.info("[%s] Session %s closed", peer_id, peer.session_id)
            else:
                logger.debug("[%s] Session %s exists but host is %s (not us)",
                             peer_id, peer.session_id, current_host)

        # Broadcast updated device list
        if was_device:
            logger.debug("[%s] Broadcasting updated device list after removal", peer_id)
            asyncio.ensure_future(self._broadcast_device_list())

        logger.info("[%s] ✅ Peer fully removed (total_peers=%d, total_sessions=%d, total_devices=%d)",
                    peer_id, len(self._peers), len(self._sessions), len(self._devices))

    # ── diagnostics ─────────────────────────────────────────────────

    def _debug_state(self, context: str = "") -> None:
        """Log the full server state for debugging."""
        peers_info = []
        for pid, p in self._peers.items():
            peers_info.append({
                "id": pid,
                "device": f"{p.device_id[:16] if p.device_id else ''} ({p.device_name})",
                "session": p.session_id[:12] if p.session_id else "",
                "paired": p.paired_peer_id,
                "auth": p.authenticated,
                "addr": p.address,
                "idle": f"{time.time() - p.last_activity:.1f}s" if p.last_activity else "?",
            })
        logger.debug(
            "[STATE] %s — total_peers=%d, total_sessions=%d, total_devices=%d, peers=%s",
            context,
            len(self._peers), len(self._sessions), len(self._devices),
            peers_info,
        )

    async def _cleanup_loop(self) -> None:
        """Periodically disconnect stale peers and clean up expired sessions."""
        ping_interval = self.config.relay.ping_interval
        peer_timeout = self.config.relay.peer_timeout

        while True:
            await asyncio.sleep(ping_interval)
            now = time.time()
            self._debug_state(f"cleanup tick (next in {ping_interval}s)")

            # Clean stale peers
            stale = [
                pid for pid, p in self._peers.items()
                if now - p.last_activity > peer_timeout
            ]
            if stale:
                logger.warning(
                    "Cleanup: found %d stale peers: %s (timeout=%ds)",
                    len(stale), stale, peer_timeout,
                )
            for pid in stale:
                peer = self._peers.get(pid)
                if peer:
                    logger.info(
                        "[%s] Removing stale peer: inactive for %.1fs (timeout=%ds)",
                        pid, now - peer.last_activity, peer_timeout,
                    )
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
