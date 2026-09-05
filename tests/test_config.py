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

    def test_db_path_default(self, isolated_config, tmp_path):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.DB_PATH).is_absolute()
        assert Path(config.DB_PATH).name == "ha_agent_state.db"

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

    def test_ollama_model_auto_default_false(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.OLLAMA_MODEL_AUTO is False

    def test_ollama_model_auto_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"ollama": {"model_auto": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.OLLAMA_MODEL_AUTO is True


# ── utils/logging.py ─────────────────────────────────────────────────────────────


class TestLoggingConfig:
    def test_log_level_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_LEVEL == "INFO"

    def test_log_file_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.LOG_FILE).is_absolute()
        assert Path(config.LOG_FILE).name == "pueo.log"

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

        assert Path(config.NOTIFY_WATCH_DIR).is_absolute()
        assert Path(config.NOTIFY_WATCH_DIR).name == "hitl"

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

    def test_escalation_preference_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.ESCALATION_PREFERENCE == "hitl"

    def test_escalation_preference_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"escalation_preference": "cloud"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.ESCALATION_PREFERENCE == "cloud"


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

    def test_timeline_page_size_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.TIMELINE_PAGE_SIZE == 25

    def test_timeline_page_size_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"timeline_page_size": 100}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.TIMELINE_PAGE_SIZE == 100


# ── netalertx.* config keys ─────────────────────────────────────────────────────


class TestNetAlertXConfigKeys:
    def test_netalertx_enabled_default_false(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_ENABLED is False

    def test_netalertx_enabled_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"enabled": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_ENABLED is True

    def test_netalertx_enabled_falls_back_to_setup_desired(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"setup_desired": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_ENABLED is True

    def test_netalertx_setup_desired_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SETUP_DESIRED is False

    def test_netalertx_setup_desired_yaml_override(self, isolated_config):
        isolated_config.write_text(yaml.dump({"netalertx": {"setup_desired": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SETUP_DESIRED is True

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

    def test_netalertx_mqtt_user_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_MQTT_USER == ""

    def test_netalertx_mqtt_password_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_MQTT_PASSWORD == ""

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

    def test_netalertx_severity_confidence_threshold_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SEVERITY_CONFIDENCE_THRESHOLD == 0.9

    def test_netalertx_severity_confidence_threshold_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"netalertx": {"severity_confidence_threshold": 0.75}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.NETALERTX_SEVERITY_CONFIDENCE_THRESHOLD == 0.75


class TestResourceSensingConfig:
    def test_resource_poll_interval_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.RESOURCE_POLL_INTERVAL_SECONDS == 300.0

    def test_ha_disk_warn_gb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_DISK_WARN_GB == 5.0

    def test_ha_disk_critical_gb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_DISK_CRITICAL_GB == 3.0

    def test_ha_mem_warn_mb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_MEM_WARN_MB == 256.0

    def test_resource_config_keys_loaded_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "agent": {
                        "resource_poll_interval_seconds": 60,
                        "ha_disk_warn_gb": 10.0,
                        "ha_disk_critical_gb": 3.0,
                        "ha_mem_warn_mb": 512,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RESOURCE_POLL_INTERVAL_SECONDS == 60.0
        assert config.HA_DISK_WARN_GB == 10.0
        assert config.HA_DISK_CRITICAL_GB == 3.0
        assert config.HA_MEM_WARN_MB == 512.0

    def test_backup_offload_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.BACKUP_OFFLOAD_ENABLED is True

    def test_backup_local_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.BACKUP_LOCAL_DIR).is_absolute()
        assert Path(config.BACKUP_LOCAL_DIR).name == "backups"

    def test_backup_offload_config_keys_loaded_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "agent": {
                        "backup_offload_enabled": False,
                        "backup_local_dir": "/mnt/nas/backups/",
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.BACKUP_OFFLOAD_ENABLED is False
        assert config.BACKUP_LOCAL_DIR == "/mnt/nas/backups/"

    def test_backup_retain_on_ha_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.BACKUP_RETAIN_ON_HA == 2

    def test_backup_retain_local_days_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.BACKUP_RETAIN_LOCAL_DAYS == 30

    def test_backup_retain_keys_loaded_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {"agent": {"backup_retain_on_ha": 5, "backup_retain_local_days": 14}}
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.BACKUP_RETAIN_ON_HA == 5
        assert config.BACKUP_RETAIN_LOCAL_DAYS == 14


class TestHAUpdateManagerConfig:
    def test_ha_api_port_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_API_PORT == 8123

    def test_ha_api_port_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"home_assistant": {"api_port": 8124}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_API_PORT == 8124

    def test_update_check_interval_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_UPDATE_CHECK_INTERVAL_HOURS == 0.0

    def test_update_check_interval_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"update_check_interval_hours": 12}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_UPDATE_CHECK_INTERVAL_HOURS == 12.0

    def test_update_notify_on_available_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_UPDATE_NOTIFY_ON_AVAILABLE is True

    def test_update_notify_on_available_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"update_notify_on_available": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_UPDATE_NOTIFY_ON_AVAILABLE is False

    def test_update_config_keys_loaded_together(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "home_assistant": {"api_port": 9000},
                    "agent": {
                        "update_check_interval_hours": 6,
                        "update_notify_on_available": False,
                    },
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_API_PORT == 9000
        assert config.HA_UPDATE_CHECK_INTERVAL_HOURS == 6.0
        assert config.HA_UPDATE_NOTIFY_ON_AVAILABLE is False

    def test_update_release_notes_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR).is_absolute()
        assert Path(config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR).name == "ha_release_notes"

    def test_update_release_notes_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"update_release_notes_cache_dir": "/tmp/ha_notes/"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_UPDATE_RELEASE_NOTES_CACHE_DIR == "/tmp/ha_notes/"

    def test_notification_poll_interval_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_NOTIFICATION_POLL_INTERVAL_MINUTES == 5.0

    def test_notification_poll_interval_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notification_poll_interval_minutes": 10}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_NOTIFICATION_POLL_INTERVAL_MINUTES == 10.0

    def test_notification_enrich_auth_failures_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_NOTIFICATION_ENRICH_AUTH_FAILURES is True

    def test_notification_enrich_auth_failures_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"notification_enrich_auth_failures": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_NOTIFICATION_ENRICH_AUTH_FAILURES is False

    def test_agent_max_tool_calls_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_TOOL_CALLS == 30

    def test_agent_max_tool_calls_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"agent_max_tool_calls": 10}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_TOOL_CALLS == 10

    def test_agent_max_wall_seconds_deprecated_alias(self, isolated_config):
        # AGENT_MAX_WALL_SECONDS is a deprecated alias for the per-call min timeout.
        importlib.reload(sys.modules["config"])
        import config

        assert (
            config.AGENT_MAX_WALL_SECONDS == config.AGENT_PER_CALL_MIN_TIMEOUT_SECONDS
        )

    def test_agent_per_call_timeout_factor_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_PER_CALL_TIMEOUT_FACTOR == 5.0

    def test_agent_per_call_timeout_factor_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"agent_per_call_timeout_factor": 3.0}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_PER_CALL_TIMEOUT_FACTOR == 3.0

    def test_agent_per_call_min_timeout_seconds_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_PER_CALL_MIN_TIMEOUT_SECONDS == 300.0

    def test_agent_per_call_max_timeout_seconds_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_PER_CALL_MAX_TIMEOUT_SECONDS == 1800.0

    def test_agent_llm_latency_lookback_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_LLM_LATENCY_LOOKBACK == 100

    def test_agent_llm_latency_percentile_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_LLM_LATENCY_PERCENTILE == 95.0

    def test_agent_max_extension_calls_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_EXTENSION_CALLS == 15

    def test_agent_max_extension_calls_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"agent_max_extension_calls": 5}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_EXTENSION_CALLS == 5

    def test_agent_max_total_calls_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_TOTAL_CALLS == 60

    def test_agent_max_total_calls_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"agent_max_total_calls": 100}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.AGENT_MAX_TOTAL_CALLS == 100

    def test_chromadb_path_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.CHROMADB_PATH).is_absolute()
        assert Path(config.CHROMADB_PATH).name == "chromadb"

    def test_chromadb_path_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"chromadb_path": "/data/chroma"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.CHROMADB_PATH == "/data/chroma"

    def test_rag_top_k_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_TOP_K == 5

    def test_rag_top_k_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"rag_top_k": 10}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_TOP_K == 10

    def test_rag_embed_model_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_EMBED_MODEL == "nomic-embed-text"

    def test_rag_embed_model_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"ollama": {"embed_model": "mxbai-embed-large"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_EMBED_MODEL == "mxbai-embed-large"

    def test_rag_ha_versions_to_fetch_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_HA_VERSIONS_TO_FETCH == 12

    def test_rag_ha_versions_to_fetch_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"rag_ha_versions_to_fetch": 6}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_HA_VERSIONS_TO_FETCH == 6

    def test_rag_hacs_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.RAG_HACS_CACHE_DIR).is_absolute()
        assert Path(config.RAG_HACS_CACHE_DIR).name == "hacs_changelogs"

    def test_rag_hacs_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"rag_hacs_cache_dir": "/data/hacs"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_HACS_CACHE_DIR == "/data/hacs"

    def test_rag_ha_docs_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.RAG_HA_DOCS_CACHE_DIR).is_absolute()
        assert Path(config.RAG_HA_DOCS_CACHE_DIR).name == "ha_docs"

    def test_rag_ha_docs_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"rag_ha_docs_cache_dir": "/data/ha_docs"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_HA_DOCS_CACHE_DIR == "/data/ha_docs"

    def test_ha_source_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.HA_SOURCE_CACHE_DIR).is_absolute()
        assert Path(config.HA_SOURCE_CACHE_DIR).name == "ha_source"

    def test_ha_source_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ha_source_cache_dir": "/data/ha_source"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_SOURCE_CACHE_DIR == "/data/ha_source"

    def test_ha_concepts_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.HA_CONCEPTS_CACHE_DIR).is_absolute()
        assert Path(config.HA_CONCEPTS_CACHE_DIR).name == "ha_concepts"

    def test_ha_concepts_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ha_concepts_cache_dir": "/data/ha_concepts"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_CONCEPTS_CACHE_DIR == "/data/ha_concepts"

    def test_rag_refresh_interval_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_REFRESH_INTERVAL_HOURS == 168

    def test_rag_refresh_interval_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"rag_refresh_interval_hours": 24}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.RAG_REFRESH_INTERVAL_HOURS == 24

    def test_chat_memory_top_k_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CHAT_MEMORY_TOP_K == 10

    def test_chat_memory_top_k_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"chat_memory_top_k": 5}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.CHAT_MEMORY_TOP_K == 5

    def test_development_mode_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVELOPMENT_MODE is False

    def test_development_mode_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"development_mode": True}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.DEVELOPMENT_MODE is True

    def test_chat_allow_tool_registration_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CHAT_ALLOW_TOOL_REGISTRATION is False

    def test_chat_allow_tool_registration_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"chat_allow_tool_registration": True}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.CHAT_ALLOW_TOOL_REGISTRATION is True

    def test_ha_repair_poll_interval_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_REPAIR_POLL_INTERVAL_MINUTES == 5.0

    def test_ha_repair_poll_interval_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ha_repair_poll_interval_minutes": 10}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_REPAIR_POLL_INTERVAL_MINUTES == 10.0

    def test_ha_lovelace_check_interval_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_LOVELACE_CHECK_INTERVAL_MINUTES == 30

    def test_ha_lovelace_check_interval_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"lovelace_check_interval_minutes": 60}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_LOVELACE_CHECK_INTERVAL_MINUTES == 60

    def test_ha_profile_refresh_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_PROFILE_REFRESH_HOURS == 24

    def test_ha_profile_refresh_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"ha_profile_refresh_hours": 12}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.HA_PROFILE_REFRESH_HOURS == 12

    def test_log_triage_cooldown_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_TRIAGE_COOLDOWN_HOURS == 4

    def test_log_triage_cooldown_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"log_triage_cooldown_hours": 8}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.LOG_TRIAGE_COOLDOWN_HOURS == 8

    def test_rejection_cooldown_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.REJECTION_COOLDOWN_HOURS == 24.0

    def test_rejection_cooldown_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"rejection_cooldown_hours": 48}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.REJECTION_COOLDOWN_HOURS == 48.0

    def test_known_issue_reminder_days_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.KNOWN_ISSUE_REMINDER_DAYS == 7

    def test_known_issue_reminder_days_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"known_issue_reminder_days": 14}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.KNOWN_ISSUE_REMINDER_DAYS == 14

    def test_disk_usage_poll_interval_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_USAGE_POLL_INTERVAL_SECONDS == 300.0

    def test_disk_usage_poll_interval_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"disk_usage_poll_interval_seconds": 60}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_USAGE_POLL_INTERVAL_SECONDS == 60.0

    def test_disk_recovery_auto_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_AUTO_ENABLED is True

    def test_disk_recovery_auto_enabled_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"disk_recovery_auto_enabled": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_AUTO_ENABLED is False

    def test_disk_recovery_recorder_keep_days_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_RECORDER_KEEP_DAYS == 30

    def test_disk_recovery_recorder_keep_days_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"disk_recovery_recorder_keep_days": 14}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_RECORDER_KEEP_DAYS == 14

    def test_disk_recovery_journal_max_mb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_JOURNAL_MAX_MB == 200

    def test_disk_recovery_journal_max_mb_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"disk_recovery_journal_max_mb": 500}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DISK_RECOVERY_JOURNAL_MAX_MB == 500

    def test_pueo_archive_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.PUEO_ARCHIVE_DIR).is_absolute()
        assert Path(config.PUEO_ARCHIVE_DIR).name == "archives"

    def test_archive_ha_log_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.ARCHIVE_HA_LOG_ENABLED is True

    def test_archive_journal_enabled_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.ARCHIVE_JOURNAL_ENABLED is False

    def test_pueo_archive_max_gb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.PUEO_ARCHIVE_MAX_GB == 2.0

    def test_pueo_local_max_gb_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.PUEO_LOCAL_MAX_GB == 20.0


class TestLLMProviderBillingConfig:
    def test_cloud_max_cost_per_incident_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MAX_COST_PER_INCIDENT_USD == 0.50

    def test_cloud_max_cost_per_incident_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"cloud": {"max_cost_per_incident_usd": 1.00}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MAX_COST_PER_INCIDENT_USD == 1.00

    def test_cloud_max_daily_spend_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MAX_DAILY_SPEND_USD == 5.00

    def test_cloud_max_daily_spend_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"cloud": {"max_daily_spend_usd": 10.00}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MAX_DAILY_SPEND_USD == 10.00

    def test_billing_keys_loaded_together(self, isolated_config):
        isolated_config.write_text(
            yaml.dump(
                {
                    "cloud": {
                        "model": "claude-opus-5",
                        "max_cost_per_incident_usd": 0.25,
                        "max_daily_spend_usd": 2.50,
                    }
                }
            )
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.CLOUD_MODEL == "claude-opus-5"
        assert config.CLOUD_MAX_COST_PER_INCIDENT_USD == 0.25
        assert config.CLOUD_MAX_DAILY_SPEND_USD == 2.50


class TestLLMProviderStartupGuards:
    def test_cloud_provider_without_api_key_raises(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            importlib.reload(sys.modules["config"])

    def test_both_provider_without_api_key_raises(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "both"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            importlib.reload(sys.modules["config"])

    def test_local_provider_without_api_key_ok(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "local"}}))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        importlib.reload(sys.modules["config"])
        import config

        assert config.LLM_PROVIDER == "local"

    def test_cloud_provider_with_api_key_ok(self, isolated_config, monkeypatch):
        isolated_config.write_text(yaml.dump({"llm": {"provider": "cloud"}}))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        importlib.reload(sys.modules["config"])
        import config

        assert config.LLM_PROVIDER == "cloud"

    def test_credential_in_yaml_raises(self, isolated_config, monkeypatch):
        isolated_config.write_text("ANTHROPIC_API_KEY: sk-secret\n")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            importlib.reload(sys.modules["config"])

    def test_credential_lowercase_in_yaml_raises(self, isolated_config, monkeypatch):
        isolated_config.write_text("anthropic_api_key: sk-secret\n")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            importlib.reload(sys.modules["config"])


class TestFederatedCasesRepoConfig:
    def test_pueo_kb_repo_default_empty(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.PUEO_KB_REPO == ""

    def test_pueo_kb_repo_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"pueo_kb_repo": "owner/pueo-kb"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.PUEO_KB_REPO == "owner/pueo-kb"

    def test_kb_sync_interval_hours_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.KB_SYNC_INTERVAL_HOURS == 168

    def test_kb_sync_interval_hours_from_yaml(self, isolated_config):
        isolated_config.write_text(yaml.dump({"agent": {"kb_sync_interval_hours": 24}}))
        importlib.reload(sys.modules["config"])
        import config

        assert config.KB_SYNC_INTERVAL_HOURS == 24

    def test_kb_sync_cache_dir_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert Path(config.KB_SYNC_CACHE_DIR).is_absolute()
        assert Path(config.KB_SYNC_CACHE_DIR).name == "kb_sync"

    def test_kb_sync_cache_dir_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"kb_sync_cache_dir": "/tmp/my_kb_sync/"}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.KB_SYNC_CACHE_DIR == "/tmp/my_kb_sync/"

    def test_allow_diagnostic_wan_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.ALLOW_DIAGNOSTIC_WAN is True

    def test_allow_diagnostic_wan_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"allow_diagnostic_wan": False}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.ALLOW_DIAGNOSTIC_WAN is False

    def test_diagnostic_wan_timeout_seconds_default(self, isolated_config):
        importlib.reload(sys.modules["config"])
        import config

        assert config.DIAGNOSTIC_WAN_TIMEOUT_SECONDS == 60

    def test_diagnostic_wan_timeout_seconds_from_yaml(self, isolated_config):
        isolated_config.write_text(
            yaml.dump({"agent": {"diagnostic_wan_timeout_seconds": 30}})
        )
        importlib.reload(sys.modules["config"])
        import config

        assert config.DIAGNOSTIC_WAN_TIMEOUT_SECONDS == 30
