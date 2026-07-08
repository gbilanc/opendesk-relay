"""Tests for authentication module."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from relay_server.auth import (
    RelayAuth,
    generate_session_id,
    generate_otp,
    generate_api_token,
    hash_password,
    verify_password,
    needs_rehash,
)


class TestPasswordHashing:
    def test_hash_and_verify(self) -> None:
        password = "my-secret-password-123!"
        hashed = hash_password(password)
        assert hashed != password
        assert verify_password(password, hashed)
        assert not verify_password("wrong-password", hashed)

    def test_empty_password(self) -> None:
        hashed = hash_password("")
        assert verify_password("", hashed)
        assert not verify_password("x", hashed)

    def test_needs_rehash(self) -> None:
        hashed = hash_password("test")
        # Fresh hash should not need rehash
        assert not needs_rehash(hashed)


class TestSessionID:
    def test_format(self) -> None:
        sid = generate_session_id()
        parts = sid.split(" ")
        assert len(parts) == 3
        for part in parts:
            assert len(part) == 3
            assert part.isdigit()

    def test_uniqueness(self) -> None:
        ids = {generate_session_id() for _ in range(100)}
        assert len(ids) == 100

    def test_no_leading_zeros_stripped(self) -> None:
        """Session IDs should always be exactly 9 digits."""
        for _ in range(1000):
            sid = generate_session_id()
            digits = sid.replace(" ", "")
            assert len(digits) == 9
            assert digits.isdigit()


class TestOTP:
    def test_format(self) -> None:
        otp = generate_otp()
        assert len(otp) == 8
        assert otp.isalnum()

    def test_uniqueness(self) -> None:
        otps = {generate_otp() for _ in range(50)}
        assert len(otps) == 50


class TestApiToken:
    def test_format(self) -> None:
        token = generate_api_token()
        assert token.startswith("relay_")
        assert len(token) == 6 + 64  # "relay_" + 64 hex chars

    def test_uniqueness(self) -> None:
        tokens = {generate_api_token() for _ in range(50)}
        assert len(tokens) == 50


class TestRelayAuth:
    @pytest.fixture
    def auth(self) -> RelayAuth:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "credentials.json"
            auth = RelayAuth(config_path=config_path)
            yield auth

    def test_set_and_authenticate(self, auth: RelayAuth) -> None:
        auth.set_password("alice", "secret123")
        assert auth.authenticate("alice", "secret123")
        assert not auth.authenticate("alice", "wrong")
        assert not auth.authenticate("bob", "secret123")

    def test_list_users(self, auth: RelayAuth) -> None:
        assert auth.list_users() == []
        auth.set_password("alice", "pw")
        auth.set_password("bob", "pw")
        assert set(auth.list_users()) == {"alice", "bob"}

    def test_remove_user(self, auth: RelayAuth) -> None:
        auth.set_password("alice", "pw")
        assert auth.remove_user("alice")
        assert not auth.remove_user("nonexistent")

    def test_has_users(self, auth: RelayAuth) -> None:
        assert not auth.has_users()
        auth.set_password("alice", "pw")
        assert auth.has_users()

    def test_api_tokens(self, auth: RelayAuth) -> None:
        token = auth.add_api_token()
        assert auth.verify_api_token(token)
        assert not auth.verify_api_token("invalid-token")
        assert auth.remove_api_token(token)
        assert not auth.verify_api_token(token)

    def test_session_flow(self, auth: RelayAuth) -> None:
        session = auth.create_session("host-password")
        assert session.session_id
        assert len(session.session_id.replace(" ", "")) == 9
        assert not session.is_one_time

        assert auth.verify_session(session.session_id, "host-password")
        # Can verify multiple times (not one-time)
        assert auth.verify_session(session.session_id, "host-password")

    def test_one_time_session(self, auth: RelayAuth) -> None:
        session = auth.create_session("otp-password", one_time=True)
        assert session.is_one_time
        assert session.otp is not None

        assert auth.verify_session(session.session_id, "otp-password")
        # Already consumed, should fail
        assert not auth.verify_session(session.session_id, "otp-password")

    def test_session_wrong_password(self, auth: RelayAuth) -> None:
        session = auth.create_session("pw")
        assert not auth.verify_session(session.session_id, "wrong")
        assert auth.verify_session(session.session_id, "pw")  # still valid

    def test_cleanup_expired(self, auth: RelayAuth) -> None:
        auth.create_session("pw1", one_time=True)
        auth.create_session("pw2", one_time=True)
        # Sessions haven't expired yet
        assert auth.cleanup_expired() == 0

    def test_ip_whitelist(self, auth: RelayAuth) -> None:
        auth.configure_ip_rules(whitelist=["192.168.1.0/24"])
        allowed, _ = auth.is_ip_allowed("192.168.1.100")
        assert allowed
        allowed, _ = auth.is_ip_allowed("10.0.0.1")
        assert not allowed

    def test_ip_blacklist(self, auth: RelayAuth) -> None:
        auth.configure_ip_rules(blacklist=["10.0.0.0/8"])
        allowed, reason = auth.is_ip_allowed("10.0.0.1")
        assert not allowed
        assert "blacklisted" in reason
        allowed, _ = auth.is_ip_allowed("192.168.1.1")
        assert allowed

    def test_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "credentials.json"
            auth = RelayAuth(config_path=config_path)
            auth.set_password("alice", "secret")
            auth.add_api_token()

            # Create a new instance to test persistence
            auth2 = RelayAuth(config_path=config_path)
            assert auth2.authenticate("alice", "secret")
            assert not auth2.authenticate("alice", "wrong")
            assert len(auth2.list_api_tokens()) == 1

    def test_auth_disabled(self, auth: RelayAuth) -> None:
        # When auth is disabled, all tokens should be accepted
        auth._auth_enabled = False
        assert auth.verify_api_token("anything")  # auth disabled returns True
        assert auth.authenticate("any", "user")  # auth disabled returns True
        assert auth.is_ip_allowed("10.0.0.1")[0]  # still checked independently
