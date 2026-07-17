"""
Message protocol for the relay server.

Defines message types, serialization (MessagePack), and framing
for the OpenDesk relay protocol.

Message format
--------------
All messages follow this structure::

    [ 4 bytes length (big-endian) ][ payload (MessagePack) ]

The payload is a MessagePack map containing the message type and data.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import msgpack

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROTOCOL_VERSION = 1
_MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100 MB
_HEADER_FORMAT = "!I"  # 4 bytes unsigned int (network byte order)
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)

# ---------------------------------------------------------------------------
# Message types (relay subset)
# ---------------------------------------------------------------------------


class MessageType(IntEnum):
    """Message types relevant to the relay protocol."""

    # ── Signalling / handshake ──
    HELLO = 0x01
    HELLO_ACK = 0x02
    KEY_EXCHANGE = 0x03
    KEY_EXCHANGE_ACK = 0x04
    AUTH_REQUEST = 0x05
    AUTH_RESPONSE = 0x06
    AUTH_OK = 0x07
    AUTH_FAIL = 0x08
    SESSION_INFO = 0x09

    # ── Video ──
    VIDEO_FRAME = 0x10
    VIDEO_REQUEST_KEYFRAME = 0x11
    VIDEO_TILE = 0x12

    # ── Input ──
    MOUSE_EVENT = 0x20
    KEYBOARD_EVENT = 0x21
    TEXT_INPUT = 0x22

    # ── Clipboard ──
    CLIPBOARD_TEXT = 0x30
    CLIPBOARD_IMAGE = 0x31
    CLIPBOARD_SYNC = 0x32

    # ── File transfer ──
    FILE_REQUEST = 0x40
    FILE_ACCEPT = 0x41
    FILE_REJECT = 0x42
    FILE_CHUNK = 0x43
    FILE_COMPLETE = 0x44
    FILE_ERROR = 0x45
    FILE_PROGRESS = 0x46
    FILE_LIST_REQUEST = 0x47
    FILE_LIST_RESPONSE = 0x48
    FILE_DOWNLOAD_REQUEST = 0x49
    FILE_DOWNLOAD_ACCEPT = 0x4A
    FILE_DOWNLOAD_REJECT = 0x4B

    # ── Audio ──
    AUDIO_FRAME = 0x50

    # ── Chat ──
    CHAT_MESSAGE = 0x60
    CHAT_TYPING = 0x61

    # ── Keep-alive ──
    PING = 0x70
    PONG = 0x71
    DISCONNECT = 0x72
    ERROR = 0x73

    # ── Relay ──
    RELAY_REGISTER = 0x80
    RELAY_ROUTE = 0x81
    RELAY_PEER_LIST = 0x82
    RELAY_DEVICE_LIST = 0x83
    RELAY_DEVICE_UPDATE = 0x84


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single protocol message with typed payload."""

    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)

    # ── serialisation ───────────────────────────────────────────────

    def encode(self) -> bytes:
        """Encode message to bytes for transport."""
        type_val = self.type.value if isinstance(self.type, MessageType) else self.type
        body = msgpack.packb(
            {
                "t": type_val,
                "p": self.payload,
            }
        )
        header = struct.pack(_HEADER_FORMAT, len(body))
        return header + body

    @classmethod
    def decode(cls, data: bytes) -> Message:
        """Decode message from raw bytes (wire format with header).

        Parameters
        ----------
        data : bytes
            Full wire-format message (header + body) as produced by
            :meth:`encode`.

        Returns
        -------
        Message
        """
        body_len = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])[0]
        body = data[_HEADER_SIZE : _HEADER_SIZE + body_len]
        obj = msgpack.unpackb(body)
        
        # Preserve the raw type value even if not in our enum.
        # The receiving peer has the full MessageType and can decode
        # it correctly.  We just forward it blindly.
        raw_type = obj["t"]
        try:
            msg_type = MessageType(raw_type)
        except ValueError:
            logger.debug(
                "decode: unknown message type 0x%02x, preserving raw value",
                raw_type,
            )
            msg_type = raw_type  # keep as raw int for forwarding

        return cls(
            type=msg_type,
            payload=obj.get("p", {}),
        )

    @classmethod
    async def from_reader(cls, reader: Any) -> Message:  # noqa: ANN401
        """Read and decode a message from an asyncio StreamReader."""
        header_data = b""
        while len(header_data) < _HEADER_SIZE:
            chunk = await reader.read(_HEADER_SIZE - len(header_data))
            if not chunk:
                logger.debug(
                    "from_reader: got empty chunk while reading header "
                    "(had %d/%d bytes) — connection closed",
                    len(header_data), _HEADER_SIZE,
                )
                raise ConnectionError("Connection closed while reading header")
            header_data += chunk

        body_len = struct.unpack(_HEADER_FORMAT, header_data)[0]
        if body_len > _MAX_MESSAGE_SIZE:
            logger.warning(
                "from_reader: message too large: %d bytes (max %d)",
                body_len, _MAX_MESSAGE_SIZE,
            )
            raise ValueError(f"Message too large: {body_len} bytes")

        body_buf = bytearray(body_len)
        bytes_read = 0
        while bytes_read < body_len:
            chunk = await reader.read(body_len - bytes_read)
            if not chunk:
                logger.debug(
                    "from_reader: got empty chunk while reading body "
                    "(had %d/%d bytes) — connection closed",
                    bytes_read, body_len,
                )
                raise ConnectionError("Connection closed while reading body")
            body_buf[bytes_read : bytes_read + len(chunk)] = chunk
            bytes_read += len(chunk)

        wire_data = header_data + bytes(body_buf)
        msg = cls.decode(wire_data)
        type_name = getattr(msg.type, 'name', str(msg.type))
        type_value = msg.type.value if isinstance(msg.type, MessageType) else msg.type
        logger.debug(
            "from_reader: decoded type=0x%02x (%s) body_len=%d",
            type_value, type_name, body_len,
        )
        return msg

    @staticmethod
    def write(writer: Any, msg: Message) -> None:  # noqa: ANN401
        """Write a message to an asyncio StreamWriter."""
        data = msg.encode()
        writer.write(data)

    # ── factory helpers ─────────────────────────────────────────────

    @classmethod
    def ping(cls, seq: int = 0) -> Message:
        return cls(MessageType.PING, {"seq": seq})

    @classmethod
    def pong(cls, seq: int = 0) -> Message:
        return cls(MessageType.PONG, {"seq": seq})

    @classmethod
    def disconnect(cls, reason: str = "") -> Message:
        return cls(MessageType.DISCONNECT, {"reason": reason})

    @classmethod
    def error(cls, code: int, message: str) -> Message:
        return cls(MessageType.ERROR, {"code": code, "message": message})

    @classmethod
    def relay_register(
        cls,
        session_id: str = "",
        device_id: str = "",
        device_name: str = "",
        lookup_device: str = "",
    ) -> Message:
        """Register with a relay server or create a new session."""
        payload: dict[str, str] = {}
        if session_id:
            payload["session_id"] = session_id
        if device_id:
            payload["device_id"] = device_id
        if device_name:
            payload["device_name"] = device_name
        if lookup_device:
            payload["lookup_device"] = lookup_device
        return cls(MessageType.RELAY_REGISTER, payload)

    @classmethod
    def relay_route(cls, inner_type: int, inner_payload: dict) -> Message:
        """Route a message through the relay to the paired peer."""
        return cls(
            MessageType.RELAY_ROUTE,
            {"inner_type": inner_type, "inner_payload": inner_payload},
        )

    @classmethod
    def relay_device_list(cls, devices: list[dict]) -> Message:
        """Report the current list of connected devices (relay → peers)."""
        return cls(MessageType.RELAY_DEVICE_LIST, {"devices": devices})

    @classmethod
    def relay_device_update(cls, device: dict, online: bool) -> Message:
        """Notify peers that a device went online or offline."""
        return cls(
            MessageType.RELAY_DEVICE_UPDATE,
            {"device": device, "online": online},
        )
