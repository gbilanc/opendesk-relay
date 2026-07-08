"""Tests for the relay protocol (Message, MessageType)."""

from __future__ import annotations

import struct

import pytest

from relay_server.protocol import Message, MessageType, _HEADER_SIZE, _MAX_MESSAGE_SIZE


class TestMessageType:
    def test_values(self) -> None:
        assert MessageType.PING.value == 0x70
        assert MessageType.PONG.value == 0x71
        assert MessageType.RELAY_REGISTER.value == 0x80
        assert MessageType.RELAY_ROUTE.value == 0x81
        assert MessageType.RELAY_PEER_LIST.value == 0x82
        assert MessageType.ERROR.value == 0x73
        assert MessageType.DISCONNECT.value == 0x72

    def test_from_name(self) -> None:
        assert MessageType["PING"] == MessageType.PING
        assert MessageType["RELAY_REGISTER"] == MessageType.RELAY_REGISTER


class TestMessageEncodeDecode:
    def test_roundtrip_simple(self) -> None:
        original = Message(MessageType.PING, {"seq": 42})
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.type == MessageType.PING
        assert decoded.payload == {"seq": 42}

    def test_roundtrip_register(self) -> None:
        original = Message.relay_register(session_id="123 456 789")
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.type == MessageType.RELAY_REGISTER
        assert decoded.payload["session_id"] == "123 456 789"

    def test_roundtrip_error(self) -> None:
        original = Message.error(404, "Not found")
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.type == MessageType.ERROR
        assert decoded.payload["code"] == 404
        assert decoded.payload["message"] == "Not found"

    def test_empty_message(self) -> None:
        original = Message(MessageType.PONG)
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.type == MessageType.PONG
        assert decoded.payload == {}

    def test_large_payload(self) -> None:
        payload = {"data": "x" * 10000}
        original = Message(MessageType.RELAY_ROUTE, payload)
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.payload["data"] == payload["data"]

    def test_max_message_size(self) -> None:
        with pytest.raises((ValueError, MemoryError, RuntimeError)):
            huge = b"\x00" * (_HEADER_SIZE + _MAX_MESSAGE_SIZE + 1)
            Message.decode(huge)

    def test_encode_decode_binary(self) -> None:
        payload = {"bytes": bytes(range(256))}
        original = Message(MessageType.RELAY_REGISTER, payload)
        data = original.encode()
        decoded = Message.decode(data)
        assert decoded.payload["bytes"] == bytes(range(256))


class TestMessageFactories:
    def test_ping_pong(self) -> None:
        ping = Message.ping(seq=99)
        assert ping.type == MessageType.PING
        assert ping.payload["seq"] == 99

        pong = Message.pong(seq=99)
        assert pong.type == MessageType.PONG
        assert pong.payload["seq"] == 99

    def test_disconnect(self) -> None:
        msg = Message.disconnect("bye")
        assert msg.type == MessageType.DISCONNECT
        assert msg.payload["reason"] == "bye"

    def test_relay_device_list(self) -> None:
        devices = [{"device_id": "abc", "device_name": "test", "session_id": "123"}]
        msg = Message.relay_device_list(devices)
        assert msg.type == MessageType.RELAY_DEVICE_LIST
        assert msg.payload["devices"] == devices
