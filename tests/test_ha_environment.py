"""Tests for utils/ha/ha_environment.py — profile assembly, error visibility, and loading."""

from __future__ import annotations

import asyncio
import dataclasses
import sqlite3

import pytest

from utils.ha.ha_environment import (
    HAEnvironmentProfile,
    build_environment_profile,
    format_profile_summary,
    load_environment_profile,
    save_environment_profile,
)
from utils.ha.ssh_client import FakeSSHClient


# ---------------------------------------------------------------------------
# Minimal fake WebSocket client
# ---------------------------------------------------------------------------


class FakeWsClient:
    def __init__(self, config_entries=None, raise_exc=None):
        self._entries = config_entries or []
        self._raise = raise_exc

    async def get_config_entries(self):
        if self._raise:
            raise self._raise
        return self._entries


# ---------------------------------------------------------------------------
# Happy-path profile assembly
# ---------------------------------------------------------------------------


class TestBuildEnvironmentProfileHappyPath:
    @pytest.fixture()
    def ssh(self):
        return FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\nlogger:\n"},
        )

    @pytest.fixture()
    def ws(self):
        return FakeWsClient(config_entries=[{"domain": "zha", "title": "ZHA"}])

    def test_happy_path_fields(self, ssh, ws):
        profile = asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda url, token: ["zha", "mqtt"],
                _discover_hacs=lambda url, token: [],
            )
        )
        assert profile.ha_version == "2026.8.2"
        assert profile.os_version == "14.1"
        assert profile.supervisor_version == "2024.08.0"
        assert profile.installed_integrations == ["zha", "mqtt"]
        assert profile.config_yaml_top_keys == ["homeassistant", "logger"]
        assert profile.config_entries == [{"domain": "zha", "title": "ZHA"}]
        assert profile.fetch_errors == {}

    def test_supervisor_version_from_ha_supervisor_info(self, ssh, ws):
        """supervisor_version comes from `ha supervisor info`, not `ha os info`."""
        profile = asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: [],
                _discover_hacs=lambda *a: [],
            )
        )
        assert profile.supervisor_version == "2024.08.0"
        assert "ha supervisor info" in ssh.commands_run


# ---------------------------------------------------------------------------
# Error visibility — logging and fetch_errors
# ---------------------------------------------------------------------------


class TestBuildEnvironmentProfileErrors:
    def _run(self, ssh, ws, **kwargs):
        defaults = dict(
            ha_token="tok",
            ha_url="http://ha.local:8123",
            config_remote_path="/config/configuration.yaml",
            _discover_integrations=lambda *a: [],
            _discover_hacs=lambda *a: [],
        )
        defaults.update(kwargs)
        return asyncio.run(
            build_environment_profile(ssh_client=ssh, ws_client=ws, **defaults)
        )

    def test_ssh_failure_logs_and_stores_in_fetch_errors(self, caplog):
        """A failing SSH call logs ha_profile_field_failed and stores in fetch_errors."""
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (1, "", "connection refused"),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )

        class _RaisingSSH(FakeSSHClient):
            async def run(self, command, check=False):
                if "ha core info" in command:
                    raise RuntimeError("connection refused")
                return await super().run(command, check=check)

        raising_ssh = _RaisingSSH(
            command_results={
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()

        with caplog.at_level("WARNING", logger="ha_environment"):
            profile = self._run(raising_ssh, ws)

        assert "ha_version" in profile.fetch_errors
        assert "connection refused" in profile.fetch_errors["ha_version"]
        assert any("ha_profile_field_failed" in r.message for r in caplog.records)

    def test_ws_failure_logs_and_stores_in_fetch_errors(self, caplog):
        """A failing WebSocket call logs ha_profile_field_failed for config_entries."""
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient(raise_exc=RuntimeError("WS timeout"))

        with caplog.at_level("WARNING", logger="ha_environment"):
            profile = self._run(ssh, ws)

        assert "config_entries" in profile.fetch_errors
        assert "WS timeout" in profile.fetch_errors["config_entries"]
        assert any("ha_profile_field_failed" in r.message for r in caplog.records)

    def test_sftp_failure_stored_in_fetch_errors(self, caplog):
        """A missing SFTP file stores an error in fetch_errors for config_yaml_top_keys."""
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={},  # no config file — read_file will raise FileNotFoundError
        )
        ws = FakeWsClient()

        with caplog.at_level("WARNING", logger="ha_environment"):
            profile = self._run(ssh, ws)

        assert "config_yaml_top_keys" in profile.fetch_errors
        assert any("ha_profile_field_failed" in r.message for r in caplog.records)

    def test_partial_failure_does_not_abort_profile(self):
        """One field failure must not prevent other fields from being populated."""
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={},  # config_yaml_top_keys will fail
        )
        ws = FakeWsClient(config_entries=[{"domain": "zha"}])
        profile = self._run(ssh, ws)

        assert profile.ha_version == "2026.8.2"
        assert profile.config_entries == [{"domain": "zha"}]
        assert "config_yaml_top_keys" in profile.fetch_errors


# ---------------------------------------------------------------------------
# load_environment_profile — forward-compat unknown-key filtering
# ---------------------------------------------------------------------------


class TestLoadEnvironmentProfile:
    def test_returns_none_when_table_missing(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE unrelated (id INTEGER)")
        assert load_environment_profile(db) is None

    def test_returns_none_when_no_row(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE ha_environment_profile "
                "(id INTEGER PRIMARY KEY, profile_json TEXT, last_updated REAL)"
            )
        assert load_environment_profile(db) is None

    def test_round_trip(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE ha_environment_profile "
                "(id INTEGER PRIMARY KEY, profile_json TEXT, last_updated REAL)"
            )
        profile = HAEnvironmentProfile(ha_version="2026.8.2", os_version="14.1")
        save_environment_profile(profile, db)
        loaded = load_environment_profile(db)
        assert loaded is not None
        assert loaded.ha_version == "2026.8.2"
        assert loaded.os_version == "14.1"

    def test_ignores_unknown_keys(self, tmp_path):
        """load_environment_profile must not raise on a JSON blob with extra keys."""
        import json

        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE ha_environment_profile "
                "(id INTEGER PRIMARY KEY, profile_json TEXT, last_updated REAL)"
            )
            data = {f.name: f.default for f in dataclasses.fields(HAEnvironmentProfile)}
            # Patch defaults that are not simple values (field(default_factory=...))
            data["installed_integrations"] = []
            data["hacs_integrations"] = []
            data["hacs_unloaded_domains"] = []
            data["hacs_repo_map"] = {}
            data["config_yaml_top_keys"] = []
            data["config_entries"] = []
            data["fetch_errors"] = {}
            data["ha_version"] = "2026.8.2"
            data["future_unknown_field"] = "ignored"
            conn.execute(
                "INSERT INTO ha_environment_profile (id, profile_json, last_updated) "
                "VALUES (1, ?, 0.0)",
                (json.dumps(data),),
            )
        loaded = load_environment_profile(db)
        assert loaded is not None
        assert loaded.ha_version == "2026.8.2"
        assert not hasattr(loaded, "future_unknown_field")

    def test_fetch_errors_field_present_in_loaded_profile(self, tmp_path):
        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE ha_environment_profile "
                "(id INTEGER PRIMARY KEY, profile_json TEXT, last_updated REAL)"
            )
        profile = HAEnvironmentProfile(fetch_errors={"ha_version": "timeout"})
        save_environment_profile(profile, db)
        loaded = load_environment_profile(db)
        assert loaded is not None
        assert loaded.fetch_errors == {"ha_version": "timeout"}

    def test_schema_drift_detected_logs_warning(self, tmp_path, caplog):
        """Unknown fields in persisted profile JSON emit ha_profile_schema_drift warning."""
        import json
        import logging

        db = str(tmp_path / "test.db")
        with sqlite3.connect(db) as conn:
            conn.execute(
                "CREATE TABLE ha_environment_profile "
                "(id INTEGER PRIMARY KEY, profile_json TEXT, last_updated REAL)"
            )
            data = {f.name: f.default for f in dataclasses.fields(HAEnvironmentProfile)}
            data["installed_integrations"] = []
            data["hacs_integrations"] = []
            data["hacs_unloaded_domains"] = []
            data["hacs_repo_map"] = {}
            data["config_yaml_top_keys"] = []
            data["config_entries"] = []
            data["fetch_errors"] = {}
            data["future_field_from_next_version"] = "ignored"
            conn.execute(
                "INSERT INTO ha_environment_profile (id, profile_json, last_updated) "
                "VALUES (1, ?, 0.0)",
                (json.dumps(data),),
            )
        with caplog.at_level(logging.WARNING, logger="ha_environment"):
            loaded = load_environment_profile(db)
        assert loaded is not None
        assert any("ha_profile_schema_drift" in r.message for r in caplog.records)


class TestConfigYamlTopKeysRegex:
    """config_yaml_top_keys must work with HA-specific YAML tags like !include."""

    def _run(self, content):
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": content},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: [],
                _discover_hacs=lambda *a: [],
            )
        )

    def test_include_tags_do_not_raise(self):
        """!include tags must not cause config_yaml_top_keys to fail."""
        content = (
            "homeassistant:\n"
            "  name: Home\n"
            "automation: !include automations.yaml\n"
            "script: !include scripts.yaml\n"
            "logger:\n"
            "  default: warning\n"
        )
        profile = self._run(content)
        assert profile.fetch_errors.get("config_yaml_top_keys") is None
        assert "homeassistant" in profile.config_yaml_top_keys
        assert "automation" in profile.config_yaml_top_keys
        assert "script" in profile.config_yaml_top_keys
        assert "logger" in profile.config_yaml_top_keys

    def test_include_dir_tags_do_not_raise(self):
        """!include_dir_merge_named and similar HA tags must not raise."""
        content = (
            "homeassistant:\n"
            "packages: !include_dir_merge_named packages/\n"
            "sensor: !include_dir_list sensors/\n"
        )
        profile = self._run(content)
        assert profile.fetch_errors.get("config_yaml_top_keys") is None
        assert "packages" in profile.config_yaml_top_keys
        assert "sensor" in profile.config_yaml_top_keys

    def test_deduplicates_keys(self):
        """Duplicate top-level keys should appear only once."""
        content = "homeassistant:\nlogger:\nhomeassistant:\n"
        profile = self._run(content)
        assert profile.config_yaml_top_keys.count("homeassistant") == 1


class TestFormatProfileSummary:
    def _make_profile(self) -> HAEnvironmentProfile:
        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            config_yaml_top_keys=["homeassistant", "mqtt", "recorder"],
            installed_integrations=["zha", "mqtt", "esphome", "cast"],
            hacs_integrations=["hacs_custom"],
            config_entries=[{"id": str(i)} for i in range(5)],
        )

    def test_counts_not_full_lists(self):
        summary = format_profile_summary(self._make_profile())
        assert "4 installed" in summary
        assert "1 via HACS" in summary
        assert "5" in summary  # config_entries count
        # full list values must not appear
        assert "esphome" not in summary
        assert "hacs_custom" not in summary

    def test_versions_shown(self):
        summary = format_profile_summary(self._make_profile())
        assert "2026.8.2" in summary
        assert "13.2" in summary
        assert "2026.08.0" in summary

    def test_config_keys_shown(self):
        summary = format_profile_summary(self._make_profile())
        assert "homeassistant" in summary
        assert "mqtt" in summary

    def test_hint_line_present(self):
        summary = format_profile_summary(self._make_profile())
        assert 'field="installed_integrations"' in summary

    def test_none_returns_not_available(self):
        assert (
            format_profile_summary(None) == "HA environment profile not yet available."
        )

    def test_spook_line_shown_when_installed(self):
        profile = self._make_profile()
        profile.spook_installed = True
        assert "Spook: installed" in format_profile_summary(profile)

    def test_no_spook_installed_line_when_not_installed(self):
        profile = self._make_profile()
        profile.spook_installed = False
        assert "Spook: installed" not in format_profile_summary(profile)


# ---------------------------------------------------------------------------
# Spook detection
# ---------------------------------------------------------------------------


class TestSpookDetection:
    def _run(self, integrations):
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_spook_installed_when_in_integrations(self):
        profile = self._run(["zha", "spook", "mqtt"])
        assert profile.spook_installed is True

    def test_spook_not_installed_when_absent(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.spook_installed is False

    def test_spook_false_when_integrations_empty(self):
        """Empty installed_integrations (e.g. fetch failure) → spook_installed=False."""
        profile = self._run([])
        assert profile.spook_installed is False

    def test_spook_detection_survives_integrations_failure(self):
        """When _discover_integrations raises, spook_installed falls back to False."""
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        profile = asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: (_ for _ in ()).throw(
                    RuntimeError("API unreachable")
                ),
                _discover_hacs=lambda *a: [],
            )
        )
        assert profile.spook_installed is False


# ---------------------------------------------------------------------------
# upgrade_advisor_installed detection
# ---------------------------------------------------------------------------


class TestUpgradeAdvisorDetection:
    def _run(self, integrations):
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_upgrade_advisor_installed_when_in_integrations(self):
        profile = self._run(["zha", "upgrade_advisor", "mqtt"])
        assert profile.upgrade_advisor_installed is True

    def test_upgrade_advisor_not_installed_when_absent(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.upgrade_advisor_installed is False

    def test_upgrade_advisor_false_when_integrations_empty(self):
        profile = self._run([])
        assert profile.upgrade_advisor_installed is False


class TestFormatProfileSummaryUpgradeAdvisor:
    def _make_profile(self, **kwargs) -> HAEnvironmentProfile:
        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            **kwargs,
        )

    def test_advisor_line_shown_when_installed(self):
        profile = self._make_profile(upgrade_advisor_installed=True)
        assert "ha-upgrade-advisor: installed" in format_profile_summary(profile)

    def test_no_advisor_installed_line_when_not_installed(self):
        profile = self._make_profile(upgrade_advisor_installed=False)
        assert "ha-upgrade-advisor: installed" not in format_profile_summary(profile)


# ---------------------------------------------------------------------------
# ai_agent_ha_installed detection
# ---------------------------------------------------------------------------


class TestAiAgentHaDetection:
    def _run(self, integrations):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient as FakeWsClient

        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_ai_agent_ha_installed_when_in_integrations(self):
        profile = self._run(["zha", "ai_agent_ha", "mqtt"])
        assert profile.ai_agent_ha_installed is True

    def test_ai_agent_ha_not_installed_when_absent(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.ai_agent_ha_installed is False

    def test_ai_agent_ha_false_when_integrations_empty(self):
        profile = self._run([])
        assert profile.ai_agent_ha_installed is False


class TestFormatProfileSummaryAiAgentHa:
    def _make_profile(self, **kwargs) -> HAEnvironmentProfile:
        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            **kwargs,
        )

    def test_ai_agent_ha_line_shown_when_installed(self):
        profile = self._make_profile(ai_agent_ha_installed=True)
        assert "ai_agent_ha: installed" in format_profile_summary(profile)

    def test_no_ai_agent_ha_line_when_not_installed(self):
        profile = self._make_profile(ai_agent_ha_installed=False)
        assert "ai_agent_ha: installed" not in format_profile_summary(profile)


# ---------------------------------------------------------------------------
# mqtt_configured detection
# ---------------------------------------------------------------------------


class TestMqttConfiguredDetection:
    def _run(self, integrations):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient as FakeWsClient

        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_mqtt_configured_when_in_integrations(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.mqtt_configured is True

    def test_mqtt_not_configured_when_absent(self):
        profile = self._run(["zha", "spook"])
        assert profile.mqtt_configured is False

    def test_mqtt_false_when_integrations_empty(self):
        profile = self._run([])
        assert profile.mqtt_configured is False


# ---------------------------------------------------------------------------
# sentinel_installed detection
# ---------------------------------------------------------------------------


class TestSentinelInstalledDetection:
    def _run(self, integrations):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient as FakeWsClient

        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_sentinel_installed_when_in_integrations(self):
        profile = self._run(["zha", "ha_sentinel"])
        assert profile.sentinel_installed is True

    def test_sentinel_not_installed_when_absent(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.sentinel_installed is False

    def test_sentinel_false_when_integrations_empty(self):
        profile = self._run([])
        assert profile.sentinel_installed is False


# ---------------------------------------------------------------------------
# battery_notes_installed detection
# ---------------------------------------------------------------------------


class TestBatteryNotesDetection:
    def _run(self, integrations):
        from utils.ha.ha_ws_client import FakeHAWebSocketClient as FakeWsClient

        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: integrations,
                _discover_hacs=lambda *a: [],
            )
        )

    def test_battery_notes_installed_when_in_integrations(self):
        profile = self._run(["zha", "battery_notes"])
        assert profile.battery_notes_installed is True

    def test_battery_notes_not_installed_when_absent(self):
        profile = self._run(["zha", "mqtt"])
        assert profile.battery_notes_installed is False

    def test_battery_notes_false_when_integrations_empty(self):
        profile = self._run([])
        assert profile.battery_notes_installed is False


# ---------------------------------------------------------------------------
# format_profile_summary — new companion lines + nudge line
# ---------------------------------------------------------------------------


class TestFormatProfileSummaryNewCompanions:
    def _make_profile(self, **kwargs) -> HAEnvironmentProfile:
        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
            **kwargs,
        )

    def test_mqtt_configured_line_shown_when_active(self):
        profile = self._make_profile(mqtt_configured=True)
        assert "MQTT: configured" in format_profile_summary(profile)

    def test_mqtt_line_absent_when_not_configured(self):
        profile = self._make_profile(mqtt_configured=False)
        assert "MQTT" not in format_profile_summary(profile)

    def test_sentinel_line_shown_when_installed(self):
        profile = self._make_profile(sentinel_installed=True)
        assert "HA Sentinel: installed" in format_profile_summary(profile)

    def test_sentinel_installed_line_absent_when_not_installed(self):
        profile = self._make_profile(sentinel_installed=False)
        assert "HA Sentinel: installed" not in format_profile_summary(profile)

    def test_battery_notes_line_shown_when_installed(self):
        profile = self._make_profile(battery_notes_installed=True)
        assert "Battery Notes: installed" in format_profile_summary(profile)

    def test_nudge_line_shown_when_companions_absent(self):
        profile = self._make_profile(
            spook_installed=False,
            upgrade_advisor_installed=False,
            sentinel_installed=False,
        )
        summary = format_profile_summary(profile)
        assert "Recommended (not installed):" in summary
        assert "Spook" in summary
        assert "HA Sentinel" in summary

    def test_nudge_line_omitted_when_all_recommended_installed(self):
        profile = self._make_profile(
            spook_installed=True,
            upgrade_advisor_installed=True,
            sentinel_installed=True,
        )
        assert "Recommended (not installed):" not in format_profile_summary(profile)

    def test_nudge_line_partial_when_some_absent(self):
        profile = self._make_profile(
            spook_installed=True,
            upgrade_advisor_installed=False,
            sentinel_installed=True,
        )
        summary = format_profile_summary(profile)
        assert "Recommended (not installed):" in summary
        assert "ha-upgrade-advisor" in summary
        # Spook is installed so it should not appear in the nudge line
        nudge_line = [l for l in summary.splitlines() if "Recommended" in l][0]
        assert "Spook" not in nudge_line


# ---------------------------------------------------------------------------
# os_version via ha os info --raw-json (inactive boot slot fix)
# ---------------------------------------------------------------------------


class TestBuildEnvironmentProfileOsVersionJson:
    """os_version must come from data.version in --raw-json output (#889)."""

    def _build(self, os_info_output: str):
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (0, os_info_output, ""),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=FakeWsClient(),  # type: ignore[arg-type]
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: [],
                _discover_hacs=lambda *a: [],
            )
        )

    def test_parses_version_from_json(self):
        import json

        raw = json.dumps({"result": "ok", "data": {"version": "18.3", "board": "rpi5"}})
        profile = self._build(raw)
        assert profile.os_version == "18.3"

    def test_version_from_data_key(self):
        import json

        # Simulate two-slot output where data.version is the ACTIVE slot
        raw = json.dumps(
            {
                "result": "ok",
                "data": {
                    "version": "18.3",
                    "version_latest": "18.3",
                    "board": "yellow",
                },
            }
        )
        profile = self._build(raw)
        assert profile.os_version == "18.3"

    def test_error_leaves_version_empty(self):
        profile = self._build("not valid json")
        assert profile.os_version == ""
        assert "os_version" in profile.fetch_errors


# ---------------------------------------------------------------------------
# hacs_unloaded_domains computation
# ---------------------------------------------------------------------------


class TestHacsUnloadedDomains:
    """hacs_unloaded_domains = HACS domains not present in installed_integrations."""

    def _run(
        self, installed: list[str], hacs_triples: list[tuple]
    ) -> "HAEnvironmentProfile":
        ssh = FakeSSHClient(
            command_results={
                "ha core info": (0, "version: 2026.8.2\n", ""),
                "ha os info --raw-json": (
                    0,
                    '{"result":"ok","data":{"version":"14.1"}}',
                    "",
                ),
                "ha supervisor info": (0, "version: 2024.08.0\n", ""),
            },
            file_contents={"/config/configuration.yaml": "homeassistant:\n"},
        )
        ws = FakeWsClient()
        return asyncio.run(
            build_environment_profile(
                ssh_client=ssh,
                ws_client=ws,  # type: ignore[arg-type]
                ha_token="tok",
                ha_url="http://ha.local:8123",
                config_remote_path="/config/configuration.yaml",
                _discover_integrations=lambda *a: installed,
                _discover_hacs=lambda *a: hacs_triples,
            )
        )

    def test_unloaded_domain_detected(self):
        """A HACS integration whose domain is not in installed_integrations is unloaded."""
        profile = self._run(
            installed=["zha", "mqtt"],
            hacs_triples=[("spook", "tetele/spook", "spook", "605635573")],
        )
        assert "spook" in profile.hacs_unloaded_domains

    def test_loaded_domain_not_in_unloaded(self):
        """A HACS integration whose domain IS in installed_integrations is not unloaded."""
        profile = self._run(
            installed=["zha", "spook"],
            hacs_triples=[("spook", "tetele/spook", "spook", "605635573")],
        )
        assert profile.hacs_unloaded_domains == []

    def test_multiple_domains_partial_unloaded(self):
        profile = self._run(
            installed=["zha", "browser_mod"],
            hacs_triples=[
                ("browser_mod", "thomasloven/browser_mod", "browser_mod", "111"),
                ("spook", "tetele/spook", "spook", "605635573"),
            ],
        )
        assert profile.hacs_unloaded_domains == ["spook"]

    def test_empty_hacs_gives_empty_unloaded(self):
        profile = self._run(installed=["zha"], hacs_triples=[])
        assert profile.hacs_unloaded_domains == []

    def test_unloaded_domains_sorted(self):
        profile = self._run(
            installed=[],
            hacs_triples=[
                ("z_pkg", "org/z_pkg", "z_pkg", ""),
                ("a_pkg", "org/a_pkg", "a_pkg", ""),
            ],
        )
        assert profile.hacs_unloaded_domains == ["a_pkg", "z_pkg"]

    def test_domain_field_used_not_slug(self):
        """When HACS domain differs from slug, use the domain field for unloaded check."""
        profile = self._run(
            installed=["actual_domain"],
            hacs_triples=[("repo_slug", "org/repo", "actual_domain", "999")],
        )
        assert profile.hacs_unloaded_domains == []

    def test_unloaded_domain_shown_in_format_summary(self):
        profile = self._run(
            installed=["zha"],
            hacs_triples=[("spook", "tetele/spook", "spook", "605635573")],
        )
        summary = format_profile_summary(profile)
        assert "downloaded but not set up" in summary
        assert "spook" in summary

    def test_no_unloaded_note_when_all_loaded(self):
        profile = self._run(
            installed=["zha", "spook"],
            hacs_triples=[("spook", "tetele/spook", "spook", "605635573")],
        )
        summary = format_profile_summary(profile)
        assert "downloaded but not set up" not in summary


# ---------------------------------------------------------------------------
# Spook contradiction fix: hacs_unloaded means "not absent" in Recommended list
# ---------------------------------------------------------------------------


class TestFormatProfileSummarySpookContradiction:
    """Spook in hacs_unloaded_domains must not appear in Recommended (not installed)."""

    def _make_profile(self) -> "HAEnvironmentProfile":
        from utils.ha.ha_environment import HAEnvironmentProfile

        return HAEnvironmentProfile(
            ha_version="2026.8.2",
            os_version="13.2",
            supervisor_version="2026.08.0",
        )

    def test_spook_not_in_recommended_when_unloaded(self):
        """hacs_unloaded_domains=['spook'] → no 'Spook (entity registry health)' entry."""
        from utils.ha.ha_environment import format_profile_summary

        profile = self._make_profile()
        profile.hacs_unloaded_domains = ["spook"]
        profile.spook_installed = False

        summary = format_profile_summary(profile)
        # Must not appear in the "Recommended (not installed)" line
        assert (
            "Recommended" not in summary
            or "Spook" not in summary.split("Recommended")[-1]
        )

    def test_spook_in_recommended_when_truly_absent(self):
        """No spook anywhere → appears in Recommended (not installed)."""
        from utils.ha.ha_environment import format_profile_summary

        profile = self._make_profile()
        profile.hacs_unloaded_domains = []
        profile.spook_installed = False

        summary = format_profile_summary(profile)
        assert "Recommended" in summary
        assert "Spook" in summary

    def test_spook_not_recommended_when_installed(self):
        """spook_installed=True → not in Recommended."""
        from utils.ha.ha_environment import format_profile_summary

        profile = self._make_profile()
        profile.hacs_unloaded_domains = []
        profile.spook_installed = True

        summary = format_profile_summary(profile)
        # Installed Spook shows a dedicated "Spook: installed" line; must not
        # appear in the Recommended absent list.
        if "Recommended" in summary:
            assert "Spook" not in summary.split("Recommended")[-1]
