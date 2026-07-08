"""
Authentication and authorisation for the relay server.

Provides:
- Password hashing with Argon2id
- Session ID generation (like AnyDesk/TeamViewer numeric ID)
- Credential store with JSON persistence
- API token generation and verification
- IP whitelist/blacklist
"""

from __future__ import annotations

import json
import logging
import os
import random
import secrets
import string
import time
from dataclasses import dataclass, field
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_ID_LENGTH = 9
_SESSION_ID_BLOCKS = 3
_SESSION_ID_BLOCK_SIZE = 3
_OTP_LENGTH = 8
_OTP_VALIDITY_SECONDS = 300  # 5 minutes
_TOKEN_BYTES = 32  # 256-bit API tokens

# ---------------------------------------------------------------------------
# Password hasher (Argon2id)
# ---------------------------------------------------------------------------

_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Hash a password with Argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, hash_str: str) -> bool:
    """Verify a password against its Argon2id hash."""
    try:
        return _hasher.verify(hash_str, password)
    except (VerificationError, VerifyMismatchError):
        return False


def needs_rehash(hash_str: str) -> bool:
    """Check if the hash uses outdated parameters."""
    return _hasher.check_needs_rehash(hash_str)


# ---------------------------------------------------------------------------
# Session ID
# ---------------------------------------------------------------------------


def generate_session_id() -> str:
    """Generate a human-friendly session ID like "123 456 789"."""
    digits = [str(random.randint(0, 9)) for _ in range(_SESSION_ID_LENGTH)]
    blocks = [
        "".join(digits[i : i + _SESSION_ID_BLOCK_SIZE])
        for i in range(0, _SESSION_ID_LENGTH, _SESSION_ID_BLOCK_SIZE)
    ]
    return " ".join(blocks)


def generate_otp() -> str:
    """Generate an 8-character alphanumeric one-time password."""
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choices(alphabet, k=_OTP_LENGTH))


def generate_api_token() -> str:
    """Generate a cryptographically secure API token.

    Returns
    -------
    str
        A hex-encoded 256-bit token (64 hex chars), prefixed with ``relay_``.
    """
    token_bytes = os.urandom(_TOKEN_BYTES)
    return "relay_" + token_bytes.hex()


# ---------------------------------------------------------------------------
# Credential store
# ---------------------------------------------------------------------------


@dataclass
class StoredCredential:
    """A stored credential entry."""

    username: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    last_used_at: float | None = None


@dataclass
class PendingSession:
    """A session waiting for remote acceptance."""

    session_id: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    is_one_time: bool = False
    otp: str | None = None
    expires_at: float = field(default_factory=lambda: time.time() + _OTP_VALIDITY_SECONDS)


class RelayAuth:
    """Manages authentication and authorisation for the relay server.

    Supports:
    - Credential storage (in-memory / JSON file)
    - Session ID generation for incoming connections
    - One-time password for unattended access
    - API token verification
    - IP whitelist / blacklist
    """

    def __init__(self, config_path: str | Path | None = None) -> None:
        self._config_path = Path(config_path) if config_path else Path.home() / ".opendesk" / "credentials.json"
        self._credentials: dict[str, StoredCredential] = {}
        self._pending_sessions: dict[str, PendingSession] = {}
        self._api_tokens: set[str] = set()

        # IP access control
        self._whitelist: list[str] = []
        self._blacklist: list[str] = []
        self._auth_enabled: bool = True

        self._load()

    # ── IP access control ───────────────────────────────────────────

    def configure_ip_rules(self, whitelist: list[str] | None = None,
                           blacklist: list[str] | None = None) -> None:
        """Configure IP whitelist and blacklist.

        Parameters
        ----------
        whitelist
            List of IP addresses or CIDR networks to allow.
            If non-empty, only these IPs can connect.
        blacklist
            List of IP addresses or CIDR networks to block.
        """
        self._whitelist = whitelist or []
        self._blacklist = blacklist or []

    def is_ip_allowed(self, peer_ip: str) -> tuple[bool, str]:
        """Check if an IP address is allowed to connect.

        Returns
        -------
        tuple[bool, str]
            (allowed, reason)
        """
        try:
            addr = ip_address(peer_ip)
        except ValueError:
            return False, f"Invalid IP: {peer_ip}"

        # Blacklist check first
        for entry in self._blacklist:
            try:
                network = ip_network(entry, strict=False)
                if addr in network:
                    return False, f"IP {peer_ip} is blacklisted (matched {entry})"
            except ValueError:
                continue

        # Whitelist check (only if configured)
        if self._whitelist:
            allowed = False
            for entry in self._whitelist:
                try:
                    network = ip_network(entry, strict=False)
                    if addr in network:
                        allowed = True
                        break
                except ValueError:
                    continue
            if not allowed:
                return False, f"IP {peer_ip} is not in whitelist"

        return True, "allowed"

    # ── Credential management ───────────────────────────────────────

    def set_password(self, username: str, password: str) -> None:
        """Set or update a user's password."""
        h = hash_password(password)
        existing = self._credentials.get(username)
        if existing:
            existing.password_hash = h
            logger.info("Password updated for '%s'", username)
        else:
            self._credentials[username] = StoredCredential(
                username=username, password_hash=h,
            )
            logger.info("Password created for '%s'", username)
        self._save()

    def authenticate(self, username: str, password: str) -> bool:
        """Verify credentials.

        Automatically re-hashes if the parameters have changed.
        """
        if not self._auth_enabled:
            return True

        cred = self._credentials.get(username)
        if cred is None:
            return False

        if not verify_password(password, cred.password_hash):
            return False

        cred.last_used_at = time.time()

        if needs_rehash(cred.password_hash):
            cred.password_hash = hash_password(password)
            self._save()
            logger.info("Re-hashed password for '%s' (parameter upgrade)", username)

        return True

    def remove_user(self, username: str) -> bool:
        """Remove a user's credentials."""
        if username in self._credentials:
            del self._credentials[username]
            self._save()
            return True
        return False

    def list_users(self) -> list[str]:
        """Return all registered usernames."""
        return list(self._credentials.keys())

    def has_users(self) -> bool:
        """Check if any credentials exist."""
        return len(self._credentials) > 0

    # ── API token management ────────────────────────────────────────

    def add_api_token(self) -> str:
        """Generate and register a new API token.

        Returns
        -------
        str
            The new token (show once, cannot be retrieved later).
        """
        token = generate_api_token()
        self._api_tokens.add(token)
        self._save()
        logger.info("API token added")
        return token

    def remove_api_token(self, token: str) -> bool:
        """Remove an API token."""
        if token in self._api_tokens:
            self._api_tokens.discard(token)
            self._save()
            return True
        return False

    def verify_api_token(self, token: str) -> bool:
        """Check if an API token is valid."""
        if not self._auth_enabled:
            return True
        return token in self._api_tokens

    def list_api_tokens(self) -> list[str]:
        """Return all registered API tokens (first 12 chars only)."""
        return [t[:12] + "..." for t in self._api_tokens]

    # ── Session management ──────────────────────────────────────────

    def create_session(self, password: str, one_time: bool = False) -> PendingSession:
        """Create a pending session (like AnyDesk waiting for connection)."""
        session_id = generate_session_id()
        while session_id in self._pending_sessions:
            session_id = generate_session_id()

        otp = generate_otp() if one_time else None

        session = PendingSession(
            session_id=session_id,
            password_hash=hash_password(password),
            is_one_time=one_time,
            otp=otp,
            expires_at=time.time() + _OTP_VALIDITY_SECONDS if one_time else 0,
        )
        self._pending_sessions[session_id] = session
        logger.info("Session %s created (one_time=%s)", session_id, one_time)
        return session

    def verify_session(self, session_id: str, password: str) -> bool:
        """Verify a session password."""
        session = self._pending_sessions.get(session_id)
        if session is None:
            return False

        if session.is_one_time and time.time() > session.expires_at:
            self._pending_sessions.pop(session_id, None)
            logger.warning("Session %s expired", session_id)
            return False

        if not verify_password(password, session.password_hash):
            return False

        if session.is_one_time:
            self._pending_sessions.pop(session_id, None)
            logger.info("Session %s consumed (one-time)", session_id)

        return True

    def remove_session(self, session_id: str) -> None:
        """Remove a pending session."""
        self._pending_sessions.pop(session_id, None)

    def cleanup_expired(self) -> int:
        """Remove all expired one-time sessions."""
        now = time.time()
        expired = [
            sid for sid, s in self._pending_sessions.items()
            if s.is_one_time and now > s.expires_at
        ]
        for sid in expired:
            del self._pending_sessions[sid]
        if expired:
            logger.info("Cleaned up %d expired sessions", len(expired))
        return len(expired)

    # ── Persistence ─────────────────────────────────────────────────

    def _load(self) -> None:
        """Load credentials and tokens from the JSON config file."""
        if not self._config_path.exists():
            return
        try:
            data = json.loads(self._config_path.read_text())
            for username, cred_data in data.get("credentials", {}).items():
                self._credentials[username] = StoredCredential(
                    username=username,
                    password_hash=cred_data["password_hash"],
                    created_at=cred_data.get("created_at", 0),
                    last_used_at=cred_data.get("last_used_at"),
                )
            self._api_tokens = set(data.get("api_tokens", []))
            logger.info(
                "Loaded %d credentials, %d tokens from %s",
                len(self._credentials), len(self._api_tokens), self._config_path,
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load credentials: %s", e)

    def _save(self) -> None:
        """Save credentials and tokens to the JSON config file."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "credentials": {
                u: {
                    "password_hash": c.password_hash,
                    "created_at": c.created_at,
                    "last_used_at": c.last_used_at,
                }
                for u, c in self._credentials.items()
            },
        }
        if self._api_tokens:
            data["api_tokens"] = list(self._api_tokens)

        self._config_path.write_text(json.dumps(data, indent=2))
        logger.debug("Credentials saved to %s", self._config_path)
