"""Tests for configuration module."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from relay_server.config import Config, load_config, _deep_merge


class TestDeepMerge:
    def test_simple_override(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3}

    def test_nested_merge(self) -> None:
        base = {"server": {"host": "0.0.0.0", "port": 8474}}
        override = {"server": {"port": 9443}}
        result = _deep_merge(base, override)
        assert result["server"]["host"] == "0.0.0.0"
        assert result["server"]["port"] == 9443

    def test_new_key(self) -> None:
        base = {"a": 1}
        override = {"b": 2}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 2}


class TestConfigDefaults:
    def test_default_values(self) -> None:
        config = Config()
        assert config.server.host == "0.0.0.0"
        assert config.server.port == 8474
        assert config.admin.enabled is True
        assert config.admin.web_host == "127.0.0.1"
        assert config.admin.web_port == 8484
        assert config.auth.enabled is True
        assert config.relay.ping_interval == 30
        assert config.relay.peer_timeout == 120
        assert config.logging.level == "INFO"
        assert config.logging.format == "text"

    def test_merge_method(self) -> None:
        config = Config()
        overrides = {
            "server": {"port": 9443},
            "logging": {"level": "DEBUG"},
        }
        merged = config.merge(overrides)
        assert merged.server.port == 9443
        assert merged.logging.level == "DEBUG"
        # Unchanged values should remain
        assert merged.server.host == "0.0.0.0"
        assert merged.admin.enabled is True


class TestLoadConfig:
    def test_default_when_no_file(self) -> None:
        config = load_config()
        assert config.server.port == 8474
        assert config.server.host == "0.0.0.0"

    def test_load_from_yaml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "test-config.yaml"
            data = {
                "server": {"port": 9443, "host": "10.0.0.1"},
                "logging": {"level": "DEBUG"},
            }
            config_path.write_text(yaml.dump(data))
            config = load_config(str(config_path))
            assert config.server.port == 9443
            assert config.server.host == "10.0.0.1"
            assert config.logging.level == "DEBUG"

    def test_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["RELAY_PORT"] = "9090"
            os.environ["RELAY_LOG_LEVEL"] = "DEBUG"
            try:
                config = load_config()
                assert config.server.port == 9090
                assert config.logging.level == "DEBUG"
            finally:
                del os.environ["RELAY_PORT"]
                del os.environ["RELAY_LOG_LEVEL"]

    def test_yaml_then_env(self) -> None:
        """Env variables should override YAML file values."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "test-config.yaml"
            data = {"server": {"port": 8888}}
            config_path.write_text(yaml.dump(data))

            os.environ["RELAY_PORT"] = "7777"
            try:
                config = load_config(str(config_path))
                assert config.server.port == 7777  # env wins
            finally:
                del os.environ["RELAY_PORT"]

    def test_env_admin_settings(self) -> None:
        os.environ["RELAY_ADMIN_ENABLED"] = "false"
        os.environ["RELAY_ADMIN_HOST"] = "0.0.0.0"
        os.environ["RELAY_ADMIN_PORT"] = "9000"
        try:
            config = load_config()
            assert config.admin.enabled is False
            assert config.admin.web_host == "0.0.0.0"
            assert config.admin.web_port == 9000
        finally:
            del os.environ["RELAY_ADMIN_ENABLED"]
            del os.environ["RELAY_ADMIN_HOST"]
            del os.environ["RELAY_ADMIN_PORT"]

    def test_env_ip_lists(self) -> None:
        os.environ["RELAY_WHITELIST_IPS"] = "10.0.0.0/8,192.168.0.0/16"
        os.environ["RELAY_BLACKLIST_IPS"] = "1.2.3.4"
        try:
            config = load_config()
            assert "10.0.0.0/8" in config.auth.whitelist_ips
            assert "192.168.0.0/16" in config.auth.whitelist_ips
            assert "1.2.3.4" in config.auth.blacklist_ips
        finally:
            del os.environ["RELAY_WHITELIST_IPS"]
            del os.environ["RELAY_BLACKLIST_IPS"]

    def test_config_to_dict(self) -> None:
        config = Config()
        d = config._to_dict()
        assert d["server"]["port"] == 8474
        assert d["admin"]["enabled"] is True
        assert d["logging"]["format"] == "text"
