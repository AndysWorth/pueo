#!/usr/bin/env python3
"""Config key loading tests — verifies config.py reads YAML values and defaults correctly."""

import asyncio
import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).parent.parent

# ── config.py ───────────────────────────────────────────────────────────────────


class TestConfigDefaults:
    def test_defaults_when_no_file(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_HOST == "homeassistant.local"
        assert config.HA_USER == "root"
        assert config.OLLAMA_MODEL == "qwen2.5-coder:7b"
        assert config.CONFIDENCE_THRESHOLD == 0.7
        assert config.SELF_HEALING_ENABLED is True

    def test_loads_values_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"host": "myha.local", "user": "admin"},
                    "ollama": {"model": "llama3:8b"},
                    "agent": {"log_confidence_threshold": 0.9, "db_path": "test.db"},
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_HOST == "myha.local"
        assert config.HA_USER == "admin"
        assert config.OLLAMA_MODEL == "llama3:8b"
        assert config.CONFIDENCE_THRESHOLD == 0.9
        assert config.DB_PATH == "test.db"

    def test_partial_config_falls_back_to_defaults(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"host": "partial.local"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_HOST == "partial.local"
        assert config.HA_USER == "root"  # default
        assert config.OLLAMA_MODEL == "qwen2.5-coder:7b"  # default

    def test_ssh_key_path_expands_tilde(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"ssh_key_path": "~/.ssh/id_rsa"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert not config.SSH_KEY_PATH.startswith("~")
        assert "id_rsa" in config.SSH_KEY_PATH

    def test_config_remote_path_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CONFIG_REMOTE_PATH == "/config/configuration.yaml"

    def test_db_path_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DB_PATH == "ha_agent_state.db"

    def test_self_healing_disabled_via_config(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"self_healing_enabled": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.SELF_HEALING_ENABLED is False

    def test_ha_known_version_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_KNOWN_VERSION == ""

    def test_ha_known_version_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"known_version": "2026.7.2"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_KNOWN_VERSION == "2026.7.2"

    def test_ha_api_token_default_empty(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_API_TOKEN == ""

    def test_ha_api_token_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"api_token": "my-secret-token"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_API_TOKEN == "my-secret-token"

    def test_ollama_endpoint_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.OLLAMA_ENDPOINT == "http://localhost:11434"


class TestRateLimiterConfig:
    def test_config_defaults(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBOUNCE_WINDOW_SECONDS == 30
        assert config.REPAIR_COOLDOWN_SECONDS == 300
        assert config.MAX_REPAIRS_PER_HOUR == 10

    def test_config_values_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "agent": {
                        "debounce_window_seconds": 15,
                        "repair_cooldown_seconds": 120,
                        "max_repairs_per_hour": 5,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEBOUNCE_WINDOW_SECONDS == 15
        assert config.REPAIR_COOLDOWN_SECONDS == 120
        assert config.MAX_REPAIRS_PER_HOUR == 5


# ── utils/logging.py ─────────────────────────────────────────────────────────────


class TestLoggingConfig:
    def test_log_level_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_LEVEL == "INFO"

    def test_log_file_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_FILE == "pueo.log"

    def test_log_level_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"log_level": "DEBUG"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_LEVEL == "DEBUG"

    def test_log_file_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"log_file": "/var/log/pueo.log"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_FILE == "/var/log/pueo.log"


class TestMaxPromptTokensConfig:
    def test_default_is_7000(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.MAX_PROMPT_TOKENS == 7000

    def test_configurable_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"max_prompt_tokens": 4096}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.MAX_PROMPT_TOKENS == 4096


# ── HITL config keys ─────────────────────────────────────────────────────────────


class TestHitlConfigKeys:
    def test_notifier_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFIER == "file"

    def test_notify_url_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_URL == ""

    def test_notify_watch_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_WATCH_DIR == "hitl/"

    def test_notifier_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"notifier": "ntfy"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFIER == "ntfy"

    def test_notify_url_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notify_url": "http://ntfy.sh/pueo"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_URL == "http://ntfy.sh/pueo"

    def test_notify_watch_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notify_watch_dir": "/var/pueo/hitl/"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NOTIFY_WATCH_DIR == "/var/pueo/hitl/"

    def test_hitl_always_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_ALWAYS is False

    def test_hitl_always_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"hitl_always": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.HITL_ALWAYS is True


# ── AutonomyGate config keys ──────────────────────────────────────────────────────


class TestAutonomyConfigKeys:
    def test_autonomy_level_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 2

    def test_autonomy_level_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"autonomy_level": 4}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 4

    def test_netalertx_mode_diagnose_maps_to_level1(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "diagnose"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 1

    def test_netalertx_mode_auto_fix_maps_to_level3(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "auto_fix"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 3

    def test_netalertx_mode_autonomous_maps_to_level4(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"mode": "autonomous"}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 4

    def test_agent_autonomy_level_takes_precedence_over_netalertx_mode(
        self, isolated_config
    ):
        isolated_config.write_text(
            yaml.dump(
                {"agent": {"autonomy_level": 3}, "netalertx": {"mode": "diagnose"}}
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.AUTONOMY_LEVEL == 3


# ── Dashboard config ──────────────────────────────────────────────────────────────


class TestDashboardConfig:
    def test_dashboard_port_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DASHBOARD_PORT == 8080

    def test_dashboard_port_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"dashboard_port": 9090}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DASHBOARD_PORT == 9090


# ── netalertx.* config keys ─────────────────────────────────────────────────────


class TestNetAlertXConfigKeys:
    def test_netalertx_deployment_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_DEPLOYMENT == "auto"

    def test_netalertx_host_defaults_to_ha_host(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"home_assistant": {"host": "myha.local"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_HOST == "myha.local"

    def test_netalertx_host_override(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"host": "myha.local"},
                    "netalertx": {"host": "nax.local"},
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_HOST == "nax.local"

    def test_netalertx_api_port_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_API_PORT == 20212

    def test_netalertx_api_token_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_API_TOKEN == ""

    def test_netalertx_ssh_defaults_match_ha(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {
                        "host": "ha.local",
                        "user": "admin",
                        "ssh_key_path": "~/.ssh/id_rsa",
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SSH_HOST == "ha.local"
        assert config.NETALERTX_SSH_USER == "admin"
        assert "id_rsa" in config.NETALERTX_SSH_KEY_PATH
        assert not config.NETALERTX_SSH_KEY_PATH.startswith("~")

    def test_netalertx_addon_repository_url_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert "alexbelgium/hassio-addons" in config.NETALERTX_ADDON_REPOSITORY_URL

    def test_netalertx_addon_slug_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_ADDON_SLUG == ""

    def test_netalertx_scan_interface_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SCAN_INTERFACE == ""

    def test_netalertx_auto_generated_name_patterns_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        patterns = config.NETALERTX_AUTO_GENERATED_NAME_PATTERNS
        assert isinstance(patterns, list)
        assert len(patterns) == 2
        assert any("unknown" in p for p in patterns)

    def test_netalertx_max_scan_age_minutes_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_MAX_SCAN_AGE_MINUTES == 20

    def test_netalertx_mqtt_subscribe_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_MQTT_SUBSCRIBE is True

    def test_netalertx_log_container_name_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_LOG_CONTAINER_NAME == "netalertx"

    def test_netalertx_max_db_history_rows_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_MAX_DB_HISTORY_ROWS == 100000

    def test_netalertx_config_overrides(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "netalertx": {
                        "api_port": 9999,
                        "api_token": "tok123",
                        "max_scan_age_minutes": 5,
                        "mqtt_subscribe": False,
                        "max_db_history_rows": 50000,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_API_PORT == 9999
        assert config.NETALERTX_API_TOKEN == "tok123"
        assert config.NETALERTX_MAX_SCAN_AGE_MINUTES == 5
        assert config.NETALERTX_MQTT_SUBSCRIBE is False
        assert config.NETALERTX_MAX_DB_HISTORY_ROWS == 50000
