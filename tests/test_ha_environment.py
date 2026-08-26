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
                "ha os info": (0, "version: 14.1\n", ""),
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
                "ha os info": (0, "version: 14.1\n", ""),
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
                "ha os info": (0, "version: 14.1\n", ""),
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
                "ha os info": (0, "version: 14.1\n", ""),
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
                "ha os info": (0, "version: 14.1\n", ""),
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
                "ha os info": (0, "version: 14.1\n", ""),
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
            # Patch defaults that are not simple values
            data["installed_integrations"] = []
            data["hacs_integrations"] = []
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
                "ha os info": (0, "version: 14.1\n", ""),
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
