"""
Configuration management for the relay server.

Supports three-layer cascade: CLI args > environment variables > YAML config file.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_CONFIG_DIR = Path.home() / ".opendesk"
_CONFIG_FILE_CANDIDATES = [
    Path("relay-config.yaml"),
    Path("relay-config.yml"),
    _CONFIG_DIR / "relay-config.yaml",
    _CONFIG_DIR / "relay-config.yml",
]

# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ServerConfig:
    """Relay server socket configuration."""

    host: str = "0.0.0.0"
    port: int = 8474
    tls_cert: str = ""
    tls_key: str = ""


@dataclass
class AdminConfig:
    """Web dashboard configuration."""

    enabled: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8484
    username: str = "admin"
    password_hash: str = ""
    api_token: str = ""


@dataclass
class AuthConfig:
    """Authentication and access control configuration."""

    enabled: bool = True
    whitelist_ips: list[str] = field(default_factory=list)
    blacklist_ips: list[str] = field(default_factory=list)


@dataclass
class RelayConfig:
    """Relay behaviour tuning."""

    ping_interval: int = 30
    peer_timeout: int = 120
    max_message_size: int = 100 * 1024 * 1024  # 100 MB


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    file: str = ""
    format: str = "text"  # "text" or "json"
    max_size: int = 50 * 1024 * 1024  # 50 MB
    backup_count: int = 5


@dataclass
class Config:
    """Top-level configuration."""

    server: ServerConfig = field(default_factory=ServerConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def merge(self, overrides: dict[str, Any]) -> Config:
        """Return a new Config with overrides applied.

        Parameters
        ----------
        overrides
            Nested dict matching the config structure, e.g.::

                {"server": {"port": 9443}, "logging": {"level": "DEBUG"}}
        """
        current = self._to_dict()
        merged = _deep_merge(current, overrides)
        return _dict_to_config(merged)

    def _to_dict(self) -> dict[str, Any]:
        """Convert the entire config to a nested dict."""
        return {
            "server": {"host": self.server.host, "port": self.server.port,
                        "tls_cert": self.server.tls_cert, "tls_key": self.server.tls_key},
            "admin": {"enabled": self.admin.enabled, "web_host": self.admin.web_host,
                       "web_port": self.admin.web_port, "username": self.admin.username,
                       "password_hash": self.admin.password_hash,
                       "api_token": self.admin.api_token},
            "auth": {"enabled": self.auth.enabled,
                      "whitelist_ips": self.auth.whitelist_ips,
                      "blacklist_ips": self.auth.blacklist_ips},
            "relay": {"ping_interval": self.relay.ping_interval,
                       "peer_timeout": self.relay.peer_timeout,
                       "max_message_size": self.relay.max_message_size},
            "logging": {"level": self.logging.level, "file": self.logging.file,
                         "format": self.logging.format, "max_size": self.logging.max_size,
                         "backup_count": self.logging.backup_count},
        }

    def configure_logging(self) -> None:
        """Apply the logging configuration."""
        level = getattr(logging, self.logging.level.upper(), logging.INFO)

        handlers: list[logging.Handler] = []

        # Console handler
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(level)
        handlers.append(console)

        # File handler (if configured)
        if self.logging.file:
            log_path = Path(self.logging.file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                filename=str(log_path),
                maxBytes=self.logging.max_size,
                backupCount=self.logging.backup_count,
            )
            file_handler.setLevel(level)
            handlers.append(file_handler)

        # Formatter
        if self.logging.format == "json":
            formatter = _JsonFormatter()
        else:
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

        for handler in handlers:
            handler.setFormatter(formatter)

        # Configure root logger
        root = logging.getLogger()
        root.setLevel(level)
        for handler in handlers:
            root.addHandler(handler)

        # Suppress noisy libs
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
        logging.getLogger("fastapi").setLevel(logging.WARNING)

        logger.info("Logging configured: level=%s, format=%s, file=%s",
                     self.logging.level, self.logging.format,
                     self.logging.file or "(console only)")


# ---------------------------------------------------------------------------
# JSON formatter (for structured logging)
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "name": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def _find_config_file(path: str | None = None) -> Path | None:
    """Find the config file, searching candidate paths."""
    if path:
        p = Path(path)
        if p.exists():
            return p
        logger.warning("Config file not found: %s", path)
        return None

    for candidate in _CONFIG_FILE_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML config file."""
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _load_env() -> dict[str, Any]:
    """Load configuration from environment variables."""
    env = os.environ
    config: dict[str, Any] = {}

    # Relay server settings
    if "RELAY_HOST" in env:
        config.setdefault("server", {})["host"] = env["RELAY_HOST"]
    if "RELAY_PORT" in env:
        config.setdefault("server", {})["port"] = int(env["RELAY_PORT"])
    if "RELAY_TLS_CERT" in env:
        config.setdefault("server", {})["tls_cert"] = env["RELAY_TLS_CERT"]
    if "RELAY_TLS_KEY" in env:
        config.setdefault("server", {})["tls_key"] = env["RELAY_TLS_KEY"]

    # Admin dashboard
    if "RELAY_ADMIN_ENABLED" in env:
        config.setdefault("admin", {})["enabled"] = env["RELAY_ADMIN_ENABLED"].lower() in ("1", "true", "yes")
    if "RELAY_ADMIN_HOST" in env:
        config.setdefault("admin", {})["web_host"] = env["RELAY_ADMIN_HOST"]
    if "RELAY_ADMIN_PORT" in env:
        config.setdefault("admin", {})["web_port"] = int(env["RELAY_ADMIN_PORT"])
    if "RELAY_ADMIN_USERNAME" in env:
        config.setdefault("admin", {})["username"] = env["RELAY_ADMIN_USERNAME"]
    if "RELAY_ADMIN_PASSWORD_HASH" in env:
        config.setdefault("admin", {})["password_hash"] = env["RELAY_ADMIN_PASSWORD_HASH"]
    if "RELAY_API_TOKEN" in env:
        config.setdefault("admin", {})["api_token"] = env["RELAY_API_TOKEN"]

    # Auth
    if "RELAY_AUTH_ENABLED" in env:
        config.setdefault("auth", {})["enabled"] = env["RELAY_AUTH_ENABLED"].lower() in ("1", "true", "yes")
    if "RELAY_WHITELIST_IPS" in env:
        config.setdefault("auth", {})["whitelist_ips"] = [
            ip.strip() for ip in env["RELAY_WHITELIST_IPS"].split(",") if ip.strip()
        ]
    if "RELAY_BLACKLIST_IPS" in env:
        config.setdefault("auth", {})["blacklist_ips"] = [
            ip.strip() for ip in env["RELAY_BLACKLIST_IPS"].split(",") if ip.strip()
        ]

    # Relay behaviour
    if "RELAY_PING_INTERVAL" in env:
        config.setdefault("relay", {})["ping_interval"] = int(env["RELAY_PING_INTERVAL"])
    if "RELAY_PEER_TIMEOUT" in env:
        config.setdefault("relay", {})["peer_timeout"] = int(env["RELAY_PEER_TIMEOUT"])
    if "RELAY_MAX_MSG_SIZE" in env:
        config.setdefault("relay", {})["max_message_size"] = int(env["RELAY_MAX_MSG_SIZE"])

    # Logging
    if "RELAY_LOG_LEVEL" in env:
        config.setdefault("logging", {})["level"] = env["RELAY_LOG_LEVEL"]
    if "RELAY_LOG_FILE" in env:
        config.setdefault("logging", {})["file"] = env["RELAY_LOG_FILE"]
    if "RELAY_LOG_FORMAT" in env:
        config.setdefault("logging", {})["format"] = env["RELAY_LOG_FORMAT"]
    if "RELAY_LOG_MAX_SIZE" in env:
        config.setdefault("logging", {})["max_size"] = int(env["RELAY_LOG_MAX_SIZE"])
    if "RELAY_LOG_BACKUP_COUNT" in env:
        config.setdefault("logging", {})["backup_count"] = int(env["RELAY_LOG_BACKUP_COUNT"])

    return config


def load_config(path: str | None = None) -> Config:
    """Load configuration from file + env, merged with defaults.

    Priority (highest wins): CLI args (applied later) > env > YAML file > defaults.
    """
    config = Config()  # start with defaults

    # 1. YAML file
    config_file = _find_config_file(path)
    if config_file:
        logger.info("Loading config from %s", config_file)
        yaml_data = _load_yaml(config_file)
        config = _dict_to_config(yaml_data)
    else:
        logger.info("No config file found, using defaults")

    # 2. Environment variables (override file)
    env_data = _load_env()
    if env_data:
        merged = _deep_merge(config._to_dict(), env_data)
        config = _dict_to_config(merged)

    return config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge two dicts. ``override`` values win."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dict_to_config(data: dict[str, Any]) -> Config:
    """Convert a nested dict to a Config dataclass."""
    server_data = data.get("server", {})
    admin_data = data.get("admin", {})
    auth_data = data.get("auth", {})
    relay_data = data.get("relay", {})
    log_data = data.get("logging", {})

    return Config(
        server=ServerConfig(
            host=str(server_data.get("host", "0.0.0.0")),
            port=int(server_data.get("port", 8474)),
            tls_cert=str(server_data.get("tls_cert", "")),
            tls_key=str(server_data.get("tls_key", "")),
        ),
        admin=AdminConfig(
            enabled=bool(admin_data.get("enabled", True)),
            web_host=str(admin_data.get("web_host", "127.0.0.1")),
            web_port=int(admin_data.get("web_port", 8484)),
            username=str(admin_data.get("username", "admin")),
            password_hash=str(admin_data.get("password_hash", "")),
            api_token=str(admin_data.get("api_token", "")),
        ),
        auth=AuthConfig(
            enabled=bool(auth_data.get("enabled", True)),
            whitelist_ips=list(auth_data.get("whitelist_ips", [])),
            blacklist_ips=list(auth_data.get("blacklist_ips", [])),
        ),
        relay=RelayConfig(
            ping_interval=int(relay_data.get("ping_interval", 30)),
            peer_timeout=int(relay_data.get("peer_timeout", 120)),
            max_message_size=int(relay_data.get("max_message_size", 100 * 1024 * 1024)),
        ),
        logging=LoggingConfig(
            level=str(log_data.get("level", "INFO")),
            file=str(log_data.get("file", "")),
            format=str(log_data.get("format", "text")),
            max_size=int(log_data.get("max_size", 50 * 1024 * 1024)),
            backup_count=int(log_data.get("backup_count", 5)),
        ),
    )
